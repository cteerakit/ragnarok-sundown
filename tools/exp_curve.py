#!/usr/bin/env python3
"""Flatten the rAthena leveling curve in db/re/job_exp.yml.

The official renewal curve grows near-exponentially, so high levels take
disproportionately longer than low levels. This script compresses the curve's
dynamic range while preserving its natural monotonic shape and the level-1
anchor. The max-level ("cap") value is flattened along with every other level.

The flatten is piecewise (two segments) so that early levels can keep their
current pace while only the high-level tail is bent down further. For each
BaseExp / JobExp list, anchored on its level-1 value E1, with a pivot level P:

    n <= P:  NewExp(n) = round( E1 * (OrigExp(n) / E1) ** FLATTEN_K )
    n  > P:  NewExp(n) = round( NewExp(P) * (OrigExp(n) / OrigExp(P)) ** FLATTEN_K_HIGH )

The low segment (<= P) is identical to the original single-exponent curve, so
FLATTEN_K governs early levels and FLATTEN_K_HIGH governs the tail. Setting
FLATTEN_K_HIGH == FLATTEN_K reproduces the old single-exponent behavior, and
FLATTEN_K = FLATTEN_K_HIGH = 1.0 reproduces the official curve. Lists that end
at or below the pivot (e.g. short JobExp lists) stay entirely on the low
segment. Lower exponents flatten more.

The script always reads from a pristine ".orig" copy (created on first run),
so it is idempotent and safe to re-run with different tunables.
"""

import os
import re
import shutil

# --- Tunables --------------------------------------------------------------
# Low-segment compression (levels 1..PIVOT_LEVEL). 1.0 = official shape,
# lower = flatter. This governs the early-game pace and is left unchanged so
# those levels match the current curve.
FLATTEN_K = 0.80

# Pivot level where the curve switches from the low segment to the flatter
# tail. This exact level is preserved from the low-segment curve and anchors
# the high segment. Levels at/below it keep their current values.
PIVOT_LEVEL = 100

# High-segment compression (levels above PIVOT_LEVEL). Lower than FLATTEN_K to
# make later leveling progressively faster. Set equal to FLATTEN_K to disable
# the second segment (single-exponent behavior).
FLATTEN_K_HIGH = 0.60

# Paths are resolved relative to the repo root (parent of this tools/ dir).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(REPO_ROOT, "db", "re", "job_exp.yml")
PRISTINE = TARGET + ".orig"
# ---------------------------------------------------------------------------

LEVEL_RE = re.compile(r"^(\s*)-\s*Level:\s*(\d+)\s*$")
EXP_RE = re.compile(r"^(\s*)Exp:\s*(\d+)\s*$")
MAXBASE_RE = re.compile(r"^\s*MaxBaseLevel:\s*(\d+)\s*$")
MAXJOB_RE = re.compile(r"^\s*MaxJobLevel:\s*(\d+)\s*$")
BASEEXP_RE = re.compile(r"^\s*BaseExp:\s*$")
JOBEXP_RE = re.compile(r"^\s*JobExp:\s*$")


def flatten(orig, anchor):
    """Low-segment compression, anchored on the level-1 value. Returns int >= 1."""
    if orig <= anchor:
        return orig
    new = anchor * (orig / anchor) ** FLATTEN_K
    return max(1, round(new))


def flatten_high(orig, orig_pivot, new_pivot):
    """High-segment compression, anchored on the pivot level. Returns int >= 1."""
    if orig <= orig_pivot:
        return min(orig, new_pivot)
    new = new_pivot * (orig / orig_pivot) ** FLATTEN_K_HIGH
    return max(1, round(new))


def main():
    if not os.path.exists(PRISTINE):
        if not os.path.exists(TARGET):
            raise SystemExit(f"Cannot find {TARGET}")
        shutil.copyfile(TARGET, PRISTINE)
        print(f"Created pristine backup: {PRISTINE}")

    with open(PRISTINE, "r", encoding="utf-8", newline="") as fh:
        lines = fh.readlines()

    out = []
    # Per-group / per-list state
    max_base = None
    max_job = None
    section = None        # "base" or "job"
    cap = None            # cap level for the current section
    anchor = None         # level-1 Exp for the current section
    orig_pivot = None     # original Exp at PIVOT_LEVEL for the current list
    new_pivot = None      # flattened Exp at PIVOT_LEVEL for the current list
    pending_level = None  # level number whose Exp line we expect next

    preview = []          # (level, orig, new) rows for the 275-cap base curve

    for line in lines:
        # New job group resets per-group state.
        if re.match(r"^\s*-\s*Jobs:\s*$", line):
            max_base = max_job = None
            section = None
            cap = None
            anchor = None
            orig_pivot = None
            new_pivot = None
            pending_level = None
            out.append(line)
            continue

        m = MAXBASE_RE.match(line)
        if m:
            max_base = int(m.group(1))
            out.append(line)
            continue

        m = MAXJOB_RE.match(line)
        if m:
            max_job = int(m.group(1))
            out.append(line)
            continue

        if BASEEXP_RE.match(line):
            section = "base"
            cap = max_base
            anchor = None
            orig_pivot = None
            new_pivot = None
            pending_level = None
            out.append(line)
            continue

        if JOBEXP_RE.match(line):
            section = "job"
            cap = max_job
            anchor = None
            orig_pivot = None
            new_pivot = None
            pending_level = None
            out.append(line)
            continue

        m = LEVEL_RE.match(line)
        if m and section:
            pending_level = int(m.group(2))
            out.append(line)
            continue

        m = EXP_RE.match(line)
        if m and section and pending_level is not None:
            indent = m.group(1)
            orig = int(m.group(2))
            level = pending_level
            pending_level = None

            if anchor is None:
                # First Exp value in this list is the level-1 anchor.
                anchor = orig

            # Two segments: levels at/below the pivot keep the low-segment
            # curve (unchanged early pace); levels above it are flattened
            # harder, anchored on the pivot. The cap level (e.g. 275) is
            # flattened too, not left as a sentinel. Lists that never reach
            # the pivot stay entirely on the low segment.
            if new_pivot is None or level <= PIVOT_LEVEL:
                new = flatten(orig, anchor)
                if level == PIVOT_LEVEL:
                    orig_pivot = orig
                    new_pivot = new
            else:
                new = flatten_high(orig, orig_pivot, new_pivot)

            out.append(f"{indent}Exp: {new}\n")

            # Record preview rows for the 275-cap base curve if present.
            if section == "base" and cap == 275:
                preview.append((level, orig, new))
            continue

        out.append(line)

    with open(TARGET, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(out)

    print(
        f"Rewrote {TARGET} with FLATTEN_K={FLATTEN_K}, "
        f"PIVOT_LEVEL={PIVOT_LEVEL}, FLATTEN_K_HIGH={FLATTEN_K_HIGH}"
    )

    if preview:
        sample_levels = {1, 50, 100, 150, 200, 250, 256, 270, 275}
        print("\nPreview (275-cap base curve): level | original -> new")
        for level, orig, new in preview:
            if level in sample_levels:
                print(f"  {level:>4} | {orig:>15,} -> {new:>15,}")


if __name__ == "__main__":
    main()
