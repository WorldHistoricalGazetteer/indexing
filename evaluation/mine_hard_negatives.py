#!/usr/bin/env python3
"""
Mine WITHIN-SCRIPT hard negatives, qualified by geographic separation.

WHY THE EXISTING NEGATIVES ARE NOT ENOUGH. The evaluation corpus's negatives are
script-pair matched, and 98.22% of them score EXACTLY ZERO under a lexical
matcher. They test cross-script discrimination and say nothing about
within-script confusability -- `Springfield`/`Springfield`,
`Newton`/`Newtown` -- which is precisely where a widened matcher risks damage.
Every search change therefore looks free, which is a standing blind spot rather
than a fact about our changes.

🛑 THE QUALIFICATION IS EVIDENCE OF DIFFERENCE, NOT ABSENCE OF EVIDENCE OF
SAMENESS. Selecting on "different place_id" would be link-ABSENCE, and this
corpus's defining defect is that co-referent records are not linked -- so
absence is uninformative exactly where it matters. The hard-link overlay does
not rescue it either: ~99% of its endpoints are wd/gn, so two osm or tgn
near-duplicates produce silence, and silence would read as "genuinely
different". Instead a pair qualifies when its two places are FAR APART, which is
a positive reason to believe them distinct however the corpus is linked.

THE THRESHOLD IS DERIVED, NOT CHOSEN. Measured over 7,139,337 pairs asserted
co-referent by sameAs/exactMatch with both endpoints located:

    p50 0.014 km · p90 1.08 km · p99 31.3 km · p99.9 372 km

    separated by >  100 km : 0.3754% of co-referents
    separated by >  500 km : 0.0740%
    separated by > 1000 km : 0.0373%

At 500 km the expected contamination from co-referents recorded far apart is
~0.074%. ⚠ That is measured over ASSERTED co-referents; unlinked ones are not in
it by construction, so it bounds one contamination source rather than all of
them, and the sampled audit below is what covers the rest.

⚠ LIMITATION TO STATE WHEREVER THIS SET IS USED: both sides must have a
repr_point, so the set is biased toward well-attested places. 98.2% of staged
places have geometry, so the exclusion is small, but it is not nothing and it
runs toward exactly the places most likely to be well-linked.
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path

SEPARATION_KM = 500.0
BLOCK_PREFIX = 6
SAMPLE_TOPONYMS = 3_000_000
MAX_BLOCK = 200


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory-db", required=True)
    ap.add_argument("--staged-glob", default="/vast/ishi/staged/*/final/places.parquet")
    ap.add_argument("--separation-km", type=float, default=SEPARATION_KM)
    ap.add_argument("--min-sim", type=float, default=0.80,
                    help="string similarity floor: below this a pair is not HARD")
    ap.add_argument("--sample", type=int, default=SAMPLE_TOPONYMS,
                    help="toponyms to consider. The self-join is quadratic "
                         "IN THE BLOCK, so bounding the input is what bounds "
                         "the work -- 57.8M with a 4-char prefix spilled.")
    ap.add_argument("--max-block", type=int, default=MAX_BLOCK,
                    help="drop blocks larger than this: a few generic prefixes\n"
                         "(sain, new, sant) otherwise dominate the join")
    ap.add_argument("--out", required=True)
    ap.add_argument("--temp-dir", default="/ix1/ishi/tmp/ipa-duckdb")
    ap.add_argument("--max-temp", default="24GB")
    a = ap.parse_args()

    import duckdb
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='48GB'")
    Path(a.temp_dir).mkdir(parents=True, exist_ok=True)
    con.execute(f"PRAGMA temp_directory='{a.temp_dir}'")
    con.execute(f"PRAGMA max_temp_directory_size='{a.max_temp}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"ATTACH '{a.inventory_db}' AS inv (READ_ONLY)")

    print("1/5 locating places…", flush=True)
    con.execute(f"""
        CREATE TEMP TABLE pt AS
        SELECT place_id,
               geometries[1].repr_point.lon AS lon,
               geometries[1].repr_point.lat AS lat,
               CASE WHEN ccodes IS NOT NULL AND len(ccodes) >= 1
                    THEN ccodes[1] END AS cc
        FROM read_parquet('{a.staged_glob}', union_by_name=true)
        WHERE geometries IS NOT NULL AND len(geometries) >= 1
          AND geometries[1].repr_point IS NOT NULL
    """)
    print("    located places:",
          f"{con.execute('SELECT count(*) FROM pt').fetchone()[0]:,}", flush=True)

    # One representative place per toponym. A toponym attesting many places has
    # no single location, and averaging them would invent one.
    print("2/5 giving each toponym a single location…", flush=True)
    con.execute("""
        CREATE TEMP TABLE tl AS
        SELECT t.toponym_id, t.name, t.script,
               any_value(p.lon) AS lon, any_value(p.lat) AS lat,
               any_value(p.cc) AS cc, count(*) AS n_places
        FROM inv.toponyms t
        JOIN inv.toponym_attestations ta ON ta.toponym_id = t.toponym_id
        JOIN pt p ON p.place_id = ta.place_id
        WHERE t.name IS NOT NULL AND length(t.name) >= 4
        GROUP BY t.toponym_id, t.name, t.script
        HAVING count(*) = 1
    """)
    n_tl = con.execute("SELECT count(*) FROM tl").fetchone()[0]
    print(f"    single-place located toponyms: {n_tl:,}", flush=True)

    # Blocking: same script, same folded prefix, similar length. Without this
    # the comparison is all-pairs over tens of millions.
    print("3/5 blocking…", flush=True)
    # ⚠ SAMPLE BEFORE BLOCKING. The self-join is quadratic within each block,
    # so the input size is what bounds the work; 57.8M toponyms on a 4-char
    # prefix spilled past the cap. Deterministic, so a rerun mines the same set.
    con.execute(f"""
        CREATE TEMP TABLE bl AS
        SELECT *, lower(strip_accents(name)) AS folded,
               substr(lower(strip_accents(name)), 1, {BLOCK_PREFIX}) AS blk,
               length(name) AS len
        FROM tl
        WHERE hash(toponym_id) % {max(1, n_tl // max(a.sample, 1))} = 0
    """)
    print(f"    sampled into blocks: "
          f"{con.execute('SELECT count(*) FROM bl').fetchone()[0]:,}", flush=True)

    # A handful of generic prefixes otherwise dominate the join entirely.
    con.execute(f"""
        CREATE TEMP TABLE big AS
        SELECT blk, script, count(*) n FROM bl GROUP BY 1,2
        HAVING count(*) > {a.max_block}
    """)
    nbig = con.execute("SELECT count(*) FROM big").fetchone()[0]
    dropped = con.execute("SELECT coalesce(sum(n),0) FROM big").fetchone()[0]
    print(f"    dropped {nbig:,} oversized blocks ({dropped:,} rows) — "
          f"generic prefixes, not signal", flush=True)
    con.execute("""
        DELETE FROM bl WHERE EXISTS (
            SELECT 1 FROM big WHERE big.blk = bl.blk AND big.script = bl.script)
    """)
    con.execute(f"""
        CREATE TEMP TABLE cand AS
        SELECT x.toponym_id AS a_id, x.name AS a_name, x.script AS a_script,
               x.lon AS a_lon, x.lat AS a_lat, x.cc AS a_cc,
               y.toponym_id AS b_id, y.name AS b_name,
               y.lon AS b_lon, y.lat AS b_lat, y.cc AS b_cc,
               jaro_winkler_similarity(x.folded, y.folded) AS sim
        FROM bl x JOIN bl y
          ON x.blk = y.blk AND x.script = y.script
         AND x.toponym_id < y.toponym_id
         AND abs(x.len - y.len) <= 3
        WHERE jaro_winkler_similarity(x.folded, y.folded) >= {a.min_sim}
    """)
    n_c = con.execute("SELECT count(*) FROM cand").fetchone()[0]
    print(f"    within-script similar pairs: {n_c:,}", flush=True)

    print("4/5 qualifying by separation…", flush=True)
    con.execute(f"""
        CREATE TEMP TABLE qual AS
        SELECT *, 2 * 6371.0 * asin(sqrt(
                 pow(sin(radians(b_lat - a_lat) / 2), 2) +
                 cos(radians(a_lat)) * cos(radians(b_lat)) *
                 pow(sin(radians(b_lon - a_lon) / 2), 2))) AS km
        FROM cand
    """)
    kept = con.execute(
        "SELECT count(*) FROM qual WHERE km > ?", [a.separation_km]).fetchone()[0]
    print(f"    separated by > {a.separation_km:g} km: {kept:,} "
          f"({100.0*kept/max(n_c,1):.2f}% of similar pairs)", flush=True)

    print("5/5 writing…", flush=True)
    rows = con.execute(f"""
        SELECT a_name, b_name, a_script, a_cc, b_cc, round(sim,4) sim,
               round(km,1) km
        FROM qual WHERE km > {a.separation_km}
        ORDER BY sim DESC
    """).fetchall()
    out = [{"query": r[0], "candidate": r[1], "query_script": r[2],
            "candidate_script": r[2], "label": 0,
            "query_cc": r[3], "candidate_cc": r[4],
            "similarity": r[5], "separation_km": r[6],
            "stratum": "hard_negative_within_script"} for r in rows]
    with open(a.out, "w", encoding="utf-8") as fh:
        for d in out:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")

    same_cc = sum(1 for d in out if d["query_cc"] and d["query_cc"] == d["candidate_cc"])
    print(f"\nwrote {len(out):,} hard negatives -> {a.out}")
    print(f"   same country despite >{a.separation_km:g} km: {same_cc:,} "
          f"({100.0*same_cc/max(len(out),1):.1f}%) — large countries, not errors")
    print(f"   expected contamination from co-referents recorded far apart: "
          f"~0.074% at 500 km (measured, asserted co-referents only)")
    print("\n⚠ Both sides required a repr_point, so this set is biased toward "
          "well-attested places.")
    print("⚠ A sampled hand audit is still required: the 0.074% bounds ONE "
          "contamination source, not all of them.")
    print("\nmost confusable examples:")
    for d in out[:12]:
        print(f"   {d['query'][:24]:<26} | {d['candidate'][:24]:<26} "
              f"sim={d['similarity']} {d['separation_km']:>9,.0f} km "
              f"{d['query_cc']}/{d['candidate_cc']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
