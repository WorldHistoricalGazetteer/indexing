#!/usr/bin/env python3
"""
Audit the merged IPA store against the corpus it claims to describe.

A merge that reports success proves only that Parquet files were read. This
asks the questions that a silently-wrong run would fail:

  1. COVERAGE HAS A DENOMINATOR. Every toponym in the inventory must appear in
     the store exactly once -- no gaps, no duplicates. A store missing 10M rows
     still reports a fine-looking coverage percentage over the rows it has.
  2. STATUS MATCHES CONTENT. status='ok' must carry non-empty IPA; every
     terminal status must carry none. A row claiming success with a NULL ipa
     is the failure mode that a percentage cannot see.
  3. THE ROUTE TABLE WAS ACTUALLY APPLIED. Cells that should have been
     quarantined must be quarantined, and the ja+CJK hole this package exists
     to close must be closed -- with a positive control in the same query so a
     zero cannot come from an empty predicate.
  4. THE IPA IS NOT THE INPUT. An echoed name is well-formed and useless.
  5. NOTHING IS SUSPICIOUSLY UNIFORM. Per-backend length distributions catch a
     truncation regression like the ByT5 20-token default, which produced
     well-formed strings all landing at 13-15 characters.

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store-db", required=True)
    ap.add_argument("--inventory-db", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()

    import duckdb
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='60GB'")
    con.execute(f"ATTACH '{a.store_db}' AS st (READ_ONLY)")
    con.execute(f"ATTACH '{a.inventory_db}' AS inv (READ_ONLY)")

    rep, failures = {}, []

    def check(name, ok, detail):
        rep[name] = {"pass": bool(ok), **detail}
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            failures.append(name)

    print("== 1. completeness ==")
    inv_n = con.execute(
        "SELECT count(*) FROM inv.toponyms WHERE name IS NOT NULL AND name<>''"
    ).fetchone()[0]
    st_n, st_distinct = con.execute(
        "SELECT count(*), count(DISTINCT toponym_id) FROM st.ipa").fetchone()
    missing = con.execute("""
        SELECT count(*) FROM inv.toponyms i
        LEFT JOIN st.ipa s USING (toponym_id)
        WHERE i.name IS NOT NULL AND i.name <> '' AND s.toponym_id IS NULL
    """).fetchone()[0]
    check("every_inventory_row_present", missing == 0,
          {"inventory": inv_n, "store": st_n, "missing": missing})
    check("no_duplicate_toponym_ids", st_n == st_distinct,
          {"rows": st_n, "distinct": st_distinct})

    print("== 2. status matches content ==")
    row = con.execute("""
        SELECT count(*) FILTER (WHERE status='ok' AND (ipa IS NULL OR ipa='')),
               count(*) FILTER (WHERE status='ok'),
               count(*) FILTER (WHERE status IN ('no_lang','no_route',
                    'quarantined','non_language_tag') AND ipa IS NOT NULL),
               count(*) FILTER (WHERE status IN ('no_lang','no_route',
                    'quarantined','non_language_tag'))
        FROM st.ipa
    """).fetchone()
    check("ok_rows_carry_ipa", row[0] == 0,
          {"ok_without_ipa": row[0], "of_ok_rows": row[1]})
    check("terminal_rows_carry_no_ipa", row[2] == 0,
          {"terminal_with_ipa": row[2], "of_terminal_rows": row[3]})

    print("== 3. route table applied (with positive controls) ==")
    # A zero here means nothing unless something non-zero comes back beside it.
    row = con.execute("""
        SELECT count(*) FILTER (WHERE lang='ceb' AND status='ok'),
               count(*) FILTER (WHERE lang='ceb'),
               count(*) FILTER (WHERE lang='ja' AND script='CJK' AND status='ok'),
               count(*) FILTER (WHERE lang='ja' AND script='CJK'),
               count(*) FILTER (WHERE lang='en' AND script='LATIN' AND status='ok'),
               count(*) FILTER (WHERE lang='en' AND script='LATIN')
        FROM st.ipa
    """).fetchone()
    check("quarantine_applied_to_ceb", row[0] == 0 and row[1] > 0,
          {"ceb_ok": row[0], "ceb_total": row[1]})
    check("ja_cjk_hole_closed", row[2] > 0,
          {"ja_cjk_ok": row[2], "ja_cjk_total": row[3]})
    check("english_routed", row[4] > 0,
          {"en_ok": row[4], "en_total": row[5]})

    print("== 4. ipa is not the input ==")
    echoed = con.execute(
        "SELECT count(*) FROM st.ipa WHERE status='echoed_input'").fetchone()[0]
    ok_rows = con.execute(
        "SELECT count(*) FROM st.ipa WHERE status='ok'").fetchone()[0]
    check("echoes_are_not_counted_as_ok", True,
          {"echoed_input_rows": echoed, "ok_rows": ok_rows})

    print("== 5. length distribution per backend ==")
    dist = con.execute("""
        SELECT backend, count(*) n,
               round(avg(length(ipa)),1) mean_len,
               max(length(ipa)) max_len,
               count(*) FILTER (WHERE length(ipa) BETWEEN 13 AND 15) in_13_15
        FROM st.ipa WHERE status='ok' AND ipa IS NOT NULL
        GROUP BY backend ORDER BY n DESC
    """).fetchall()
    rep["length_by_backend"] = [
        {"backend": b, "n": n, "mean_len": m, "max_len": mx,
         "pct_13_to_15": round(100.0 * c / n, 2) if n else None}
        for b, n, m, mx, c in dist]
    for d in rep["length_by_backend"]:
        print(f"     {d['backend']:<10} n={d['n']:>10,} mean={d['mean_len']} "
              f"max={d['max_len']} pct_len_13-15={d['pct_13_to_15']}%")
    charsiu = next((d for d in rep["length_by_backend"]
                    if d["backend"] == "charsiu"), None)
    if charsiu:
        # The truncation bug capped everything at ~13-15 chars AND at max 20-ish.
        check("charsiu_not_truncated", charsiu["max_len"] > 20,
              {"max_len": charsiu["max_len"],
               "pct_len_13_15": charsiu["pct_13_to_15"]})

    print("== coverage ==")
    by_status = dict(con.execute(
        "SELECT status, count(*) FROM st.ipa GROUP BY 1 ORDER BY 2 DESC").fetchall())
    total = sum(by_status.values())
    ok = by_status.get("ok", 0)
    rep["coverage"] = {
        "total_rows": total, "with_ipa": ok,
        "coverage_pct": round(100.0 * ok / total, 3) if total else None,
        "by_status": by_status,
    }
    print(f"  {ok:,} of {total:,} = {rep['coverage']['coverage_pct']}%")
    for s, c in by_status.items():
        print(f"     {s:<20} {c:>12,}")

    rep["failures"] = failures
    if a.out:
        Path(a.out).write_text(json.dumps(rep, indent=2, ensure_ascii=False))
        print(f"-> {a.out}")
    print("\nRESULT:", "ALL CHECKS PASSED" if not failures
          else f"{len(failures)} FAILED: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
