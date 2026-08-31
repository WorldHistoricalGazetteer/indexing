"""Compare a rebuilt hard-link overlay against the one it would replace.

``publish_hardlinks`` computes ``row_count`` from the *new* database and never
opens the incumbent, so nothing downstream can tell a good rebuild from a
degraded one. That gap is what put nine boundary layers on the live map as
points on 7 August, and what would have replaced a 7.6 M-row overlay with a
fraction of one on 31 August had a human not read ``gn: attempted=0`` off a log.

This module is the missing comparison: it reads both databases, reports total
rows and per-namespace endpoint coverage side by side, and **exits non-zero on
an unexplained shrink** so it can gate a publish step.

"Per-namespace" counts *rows touching* a namespace — a row whose endpoints are
``wd:Q142`` and ``gn:3017382`` counts once for ``wd`` and once for ``gn``, and a
row with both endpoints in ``wd`` counts once. That is the metric the 31 August
audit quotes (``wd`` 7,516,092 of 7,596,959 rows = 98.9%), so its numbers can be
reproduced against a known overlay rather than taken on trust.

A shrink is only a defect when it is *unexplained*: the whg id-map join is meant
to drop ~92% of the contributor layer, because those edges point at places that
do not exist. Name such a namespace with ``--allow-shrink`` and its fall is
reported but not fatal.

Usage::

    python -m processing.compare_hardlink_overlays \
        --incumbent /ix1/ishi/hardlinks/hard_links.sqlite \
        --candidate /ix1/ishi/hardlinks/hard_links_<run-id>.sqlite \
        --allow-shrink whg
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# A rebuild is allowed to differ from its predecessor by this fraction before
# the shrink is called unexplained. The corpus genuinely moves between runs;
# 3% is wide enough for that and far narrower than any of the failures this
# guard exists to catch, which lost 70-100% of a namespace.
DEFAULT_TOLERANCE = 0.03

# Rows touching each namespace, by inclusion-exclusion:
#
#     touching(X) = |place_a in X| + |place_b in X| - |both endpoints in X|
#
# The obvious formulation is a UNION of (rowid, ns) pairs, which dedupes the
# both-endpoints case for free — but it materialises a ~15 M-row temp B-tree
# per database and measured far too slow to gate a publish on. These three
# GROUP BYs are plain scans with a small hash and give the identical answer.
_NS_A_SQL = """
SELECT substr(place_a, 1, instr(place_a, ':') - 1) AS ns, COUNT(*)
  FROM hard_link_assertions GROUP BY ns
"""
_NS_B_SQL = """
SELECT substr(place_b, 1, instr(place_b, ':') - 1) AS ns, COUNT(*)
  FROM hard_link_assertions GROUP BY ns
"""
_NS_BOTH_SQL = """
SELECT substr(place_a, 1, instr(place_a, ':') - 1) AS ns, COUNT(*)
  FROM hard_link_assertions
 WHERE substr(place_a, 1, instr(place_a, ':') - 1)
     = substr(place_b, 1, instr(place_b, ':') - 1)
 GROUP BY ns
"""


def _read(db_path: Path) -> tuple[int, dict[str, int]]:
    """Total rows and per-namespace rows-touching, read-only."""
    if not db_path.exists():
        raise FileNotFoundError(f"overlay not found: {db_path}")
    # mode=ro so a live overlay being read by the gateway is never written to,
    # not even a journal or a hot-journal rollback.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA cache_size=-200000;")
        total = conn.execute("SELECT COUNT(*) FROM hard_link_assertions").fetchone()[0]
        a = {ns: n for ns, n in conn.execute(_NS_A_SQL) if ns}
        b = {ns: n for ns, n in conn.execute(_NS_B_SQL) if ns}
        both = {ns: n for ns, n in conn.execute(_NS_BOTH_SQL) if ns}
        per_ns = {
            ns: a.get(ns, 0) + b.get(ns, 0) - both.get(ns, 0)
            for ns in set(a) | set(b)
        }
        return int(total), per_ns
    finally:
        conn.close()


def compare(
    *,
    incumbent: Path,
    candidate: Path,
    tolerance: float = DEFAULT_TOLERANCE,
    allow_shrink: frozenset[str] = frozenset(),
) -> dict:
    inc_total, inc_ns = _read(incumbent)
    cand_total, cand_ns = _read(candidate)

    failures: list[str] = []
    floor = inc_total * (1.0 - tolerance)
    if cand_total < floor:
        failures.append(
            f"total rows {cand_total:,} is below {floor:,.0f} "
            f"({tolerance:.0%} under the incumbent's {inc_total:,})"
        )

    namespaces = []
    for ns in sorted(set(inc_ns) | set(cand_ns)):
        before = inc_ns.get(ns, 0)
        after = cand_ns.get(ns, 0)
        delta = after - before
        pct = (delta / before) if before else None
        allowed = ns in allow_shrink
        # A namespace that simply did not exist before cannot "shrink".
        bad = (
            before > 0
            and not allowed
            and after < before * (1.0 - tolerance)
        )
        if bad:
            failures.append(
                f"{ns}: {before:,} -> {after:,} ({pct:+.1%}) — unexplained shrink"
            )
        namespaces.append(
            {
                "namespace": ns,
                "incumbent": before,
                "candidate": after,
                "delta": delta,
                "pct": pct,
                "shrink_allowed": allowed,
                "failed": bad,
            }
        )

    return {
        "incumbent_path": str(incumbent),
        "candidate_path": str(candidate),
        "incumbent_rows": inc_total,
        "candidate_rows": cand_total,
        "row_delta": cand_total - inc_total,
        "tolerance": tolerance,
        "allow_shrink": sorted(allow_shrink),
        "namespaces": namespaces,
        "failures": failures,
        "verdict": "PASS" if not failures else "FAIL",
    }


def _render(report: dict) -> str:
    lines = [
        f"incumbent : {report['incumbent_path']}",
        f"candidate : {report['candidate_path']}",
        "",
        f"{'namespace':<12} {'incumbent':>14} {'candidate':>14} {'delta':>14}  ",
        f"{'-' * 12} {'-' * 14} {'-' * 14} {'-' * 14}  ",
    ]
    for row in report["namespaces"]:
        pct = "" if row["pct"] is None else f"{row['pct']:+.1%}"
        flag = ""
        if row["failed"]:
            flag = "  <== UNEXPLAINED SHRINK"
        elif row["shrink_allowed"]:
            flag = "  (shrink allowed)"
        lines.append(
            f"{row['namespace']:<12} {row['incumbent']:>14,} "
            f"{row['candidate']:>14,} {row['delta']:>14,}  {pct:>7}{flag}"
        )
    lines += [
        f"{'-' * 12} {'-' * 14} {'-' * 14} {'-' * 14}  ",
        f"{'TOTAL ROWS':<12} {report['incumbent_rows']:>14,} "
        f"{report['candidate_rows']:>14,} {report['row_delta']:>14,}",
        "",
    ]
    if report["failures"]:
        lines.append("FAILURES:")
        lines += [f"  * {f}" for f in report["failures"]]
        lines.append("")
    lines.append(f"VERDICT: {report['verdict']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare a rebuilt hard-link overlay against the incumbent; "
                    "exit non-zero on an unexplained shrink."
    )
    parser.add_argument("--incumbent", required=True, type=Path,
                        help="the overlay the candidate would replace")
    parser.add_argument("--candidate", required=True, type=Path,
                        help="the freshly built overlay")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                        help=f"fractional shrink tolerated (default {DEFAULT_TOLERANCE})")
    parser.add_argument("--allow-shrink", nargs="*", default=[], metavar="NS",
                        help="namespaces whose shrink is expected and not fatal "
                             "(e.g. whg, whose id-map join drops dangling edges)")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    report = compare(
        incumbent=args.incumbent,
        candidate=args.candidate,
        tolerance=args.tolerance,
        allow_shrink=frozenset(args.allow_shrink),
    )
    print(json.dumps(report, indent=2) if args.json else _render(report))
    sys.exit(0 if report["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
