#!/usr/bin/env python3
"""
Copy IPA from the store into the toponyms DuckDB, so existing consumers reach it.

WHY THIS EXISTS. The store holds verified IPA that NOTHING READS.
`export_training_parquet` and `dump_to_jsonl` both SELECT `t.ipa` from the
toponyms inventory, so until that column is populated the measured coverage is
unreachable — a producer verified at the writer, which is the fault this
repository keeps cataloguing.

🛑 THE STORE REMAINS THE SYSTEM OF RECORD. This target is LOSSY BY
CONSTRUCTION: a NULL `ipa` here means any of `no_lang`, `quarantined`,
`no_route`, `non_language_tag`, `echoed_input`, `empty_output`, or
never-examined. **Seven states flatten to one.**

⚠ **NEVER COMPUTE AN IPA COVERAGE STATISTIC FROM THE TOPONYMS DuckDB.** A
`count(*) FILTER (WHERE ipa IS NOT NULL)` here is not coverage — it cannot
distinguish "no backend exists" from "we never looked", and it silently counts
18.5M rows whose language is unknown alongside rows nobody attempted. Coverage
comes from `/vast/ishi/ipa-v8/store/ipa.duckdb` WITH its status breakdown, or
it is not coverage. This warning lives here, at the write site, because this is
where the wrong query gets written.

WHY `panphon_features` IS NULLED. The rebuild populates it from ITS OWN IPA. If
this writes a corrected `ipa` and leaves that vector, every touched row carries
an `ipa` and a PanPhon vector derived from DIFFERENT computations — individually
plausible, jointly wrong, and undetectable afterwards. `panphon_features` is a
pure deterministic function of `ipa`, so nulling forfeits nothing recoverable,
whereas a stale derivation beside a corrected source is worse than an absent
one: absence is legible, inconsistency is not. Consistent with §10 of
plan-symphonym-v8.md retiring the pooled 192-d vector, and it does not foreclose
per-segment features, which can be regenerated from the corrected IPA.

The nulling is REPORTED, not silent, so the next reader sees a decision rather
than something that failed to compute.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

from phonetics.ipa import store as S


def census(con, alias: str = "st") -> Dict[str, int]:
    return dict(con.execute(
        f"SELECT status, count(*) FROM {alias}.ipa GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall())


def assert_real_inventory(path: str) -> None:
    """Refuse a path that is not actually the inventory.

    🛑 ATTACHING READ-WRITE TO A MISSING PATH CREATES AN EMPTY DATABASE. It does
    not error. On 8 Sep 2026 the inventory was moved from /vast to /ix1 between
    this backfill's dry run and its execute run; the execute run attached
    read-write to the old path, DuckDB created a 12 KB empty database there, and
    the only reason it failed loudly was that a later statement named a table.
    Had the first statement been a CREATE or an INSERT it would have written
    happily into nothing and reported success.

    Worse, the empty file then SITS AT THE CANONICAL PATH as a decoy: any
    resolver that checks existence — including the one in
    evaluation/romanisation_fidelity.py — would have preferred it over the real
    file, because `exists()` is not `is the artefact`.

    So check for content, not presence, before doing anything.
    """
    import duckdb
    probe = duckdb.connect()
    try:
        probe.execute(f"ATTACH '{path}' AS chk (READ_ONLY)")
        n = probe.execute("SELECT count(*) FROM chk.toponyms").fetchone()[0]
    except Exception as exc:
        raise SystemExit(
            f"{path} is not a usable inventory: {type(exc).__name__}: {exc}\n"
            f"⚠ If it exists but has no tables it is probably an EMPTY database "
            f"created by attaching read-write to a path whose file had moved. "
            f"Find the real one before rerunning.")
    finally:
        probe.close()
    if n == 0:
        raise SystemExit(f"{path} has a toponyms table with ZERO rows — "
                         f"refusing to treat it as the inventory.")


def backfill(store_db: str, inventory_db: str, execute: bool = False) -> dict:
    import duckdb

    # BEFORE any read-write attach, which is what can create the decoy.
    assert_real_inventory(inventory_db)

    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='60GB'")
    con.execute(f"ATTACH '{store_db}' AS st (READ_ONLY)")
    con.execute(f"ATTACH '{inventory_db}' AS inv" + ("" if execute else " (READ_ONLY)"))

    rep = {
        "store_db": store_db, "inventory_db": inventory_db,
        "executed": execute,
        "at": datetime.now(timezone.utc).isoformat(),
        "store_census": census(con),
    }

    # How much of the TARGET does the store actually cover? A store built from
    # an older inventory silently leaves new toponyms untouched, and the row
    # count of what was written cannot reveal that on its own.
    row = con.execute("""
        SELECT count(*) AS inv_rows,
               count(*) FILTER (WHERE s.toponym_id IS NOT NULL) AS in_store,
               count(*) FILTER (WHERE s.status = 'ok') AS would_write,
               count(*) FILTER (WHERE s.toponym_id IS NULL) AS absent_from_store
        FROM inv.toponyms i
        LEFT JOIN st.ipa s USING (toponym_id)
    """).fetchone()
    rep["target"] = dict(zip(
        ["inventory_rows", "present_in_store", "rows_with_ipa_to_write",
         "absent_from_store"], row))
    t = rep["target"]
    t["store_coverage_of_target_pct"] = round(
        100.0 * t["present_in_store"] / t["inventory_rows"], 3) if t["inventory_rows"] else None

    # What the target holds now, so the change is a before/after and not an
    # assertion.
    before = con.execute(
        "SELECT count(*) FILTER (WHERE ipa IS NOT NULL AND ipa <> ''), "
        "       count(*) FILTER (WHERE panphon_features IS NOT NULL) "
        "FROM inv.toponyms").fetchone()
    rep["target_before"] = {"with_ipa": before[0], "with_panphon": before[1]}

    if execute:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("""
                UPDATE inv.toponyms
                SET ipa = s.ipa, panphon_features = NULL
                FROM st.ipa s
                WHERE inv.toponyms.toponym_id = s.toponym_id
                  AND s.status = 'ok'
                  AND s.ipa IS NOT NULL AND s.ipa <> ''
            """)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        after = con.execute(
            "SELECT count(*) FILTER (WHERE ipa IS NOT NULL AND ipa <> ''), "
            "       count(*) FILTER (WHERE panphon_features IS NOT NULL) "
            "FROM inv.toponyms").fetchone()
        rep["target_after"] = {"with_ipa": after[0], "with_panphon": after[1]}
        rep["panphon_nulled"] = before[1] - after[1]

    con.close()
    return rep


def _print(rep: dict) -> None:
    t = rep["target"]
    print(f"store     : {rep['store_db']}")
    print(f"inventory : {rep['inventory_db']}")
    print(f"mode      : {'EXECUTE' if rep['executed'] else 'DRY RUN (default)'}")
    print(f"\ninventory rows        : {t['inventory_rows']:,}")
    print(f"  present in store    : {t['present_in_store']:,} "
          f"({t['store_coverage_of_target_pct']}%)")
    print(f"  ABSENT from store   : {t['absent_from_store']:,}"
          + ("   <- these keep whatever the inventory already had"
             if t["absent_from_store"] else ""))
    print(f"  rows with IPA to write: {t['rows_with_ipa_to_write']:,}")
    print("\nstore census (the SYSTEM OF RECORD — coverage comes from here, "
          "never from the inventory):")
    for k, v in rep["store_census"].items():
        print(f"   {k:<20} {v:>12,}")
    b = rep["target_before"]
    print(f"\ninventory before: with_ipa={b['with_ipa']:,} "
          f"with_panphon={b['with_panphon']:,}")
    if rep.get("target_after"):
        a = rep["target_after"]
        print(f"inventory after : with_ipa={a['with_ipa']:,} "
              f"with_panphon={a['with_panphon']:,}")
        # State the nulling as a DECISION, so it is never filed as a gap.
        print(f"\n  {rep['panphon_nulled']:,} rows: ipa written, "
              f"panphon_features NULLED — deliberate, not a failure to compute.")
        print("  panphon_features is a pure function of ipa; leaving a stale "
              "vector beside a")
        print("  corrected source would be an undetectable mismatch. "
              "Regenerate it from the")
        print("  corrected ipa if a teacher survives the v8 design.")
    else:
        print("\n(dry run — nothing written. Pass --execute to apply.)")


def main():
    ap = argparse.ArgumentParser(
        description="Backfill store IPA into the toponyms DuckDB (dry-run by default)")
    ap.add_argument("--store-db", required=True)
    ap.add_argument("--inventory-db", required=True)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--json-out")
    a = ap.parse_args()
    rep = backfill(a.store_db, a.inventory_db, a.execute)
    _print(rep)
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(rep, indent=2, ensure_ascii=False))
        print(f"\n-> {a.json_out}")


if __name__ == "__main__":
    main()
