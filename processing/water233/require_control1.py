#!/usr/bin/env python
"""place#233 — re-derive control 1's VERDICT at the point of use.

Why this exists. The planet stages were gated `afterok` on the job that
verifies the filter. That gate is safe in one direction only: when the job
fails, the stages are held. It is unsafe in the other, and the other is the
dangerous one -- a verification job that exits 0 while having checked
nothing, or having checked the wrong thing, RELEASES the expensive stages,
and nothing in `afterok` can distinguish that from a real pass.

    A dependency on a job is not a dependency on that job's finding.

So each consumer re-derives the verdict from the recorded counts rather than
trusting that some earlier process exited cleanly. This reads the finding,
not the process, and it costs milliseconds.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: require_control1.py <counts.planet.json> <counts.filtered.json>")
        return 2
    try:
        planet = json.load(open(sys.argv[1]))["counts"]
        filt = json.load(open(sys.argv[2]))["counts"]
    except Exception as exc:  # noqa: BLE001
        print(f"REFUSING: cannot read control 1 counts: {exc}")
        return 1

    shared = sorted(set(planet) & set(filt))
    if not shared:
        print("REFUSING: control 1 counts share no keys -- nothing was compared")
        return 1

    compared = 0
    problems: list[str] = []
    for tag in shared:
        for kind in ("way", "relation"):
            p, f = planet[tag][kind], filt[tag][kind]
            if p == 0 and f == 0:
                continue          # absent in both: nothing asserted
            compared += 1
            if p and f / p < 0.9999:
                problems.append(f"{tag}/{kind}: {p:,} planet vs {f:,} filtered "
                                f"({p - f:,} missing)")
            elif p and f / p > 1.0001:
                problems.append(f"{tag}/{kind}: ratio {f / p:.4f} > 1 -- impossible, "
                                f"definitions differ")

    # A control that compared nothing is not a control that passed. This is
    # the same failure as a filter that matched nothing: a clean run over an
    # empty population, reporting success.
    if compared == 0:
        print("REFUSING: control 1 compared 0 non-empty tag/kind pairs. "
              "A control with no denominator has not passed; it has not run.")
        return 1

    if problems:
        print(f"REFUSING: control 1 does not hold ({len(problems)} of {compared} "
              f"comparisons failed):")
        for p in problems:
            print(f"   {p}")
        return 1

    print(f"control 1 re-derived at point of use: {compared} non-empty tag/kind "
          f"pairs, all exact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
