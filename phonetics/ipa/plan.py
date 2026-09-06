#!/usr/bin/env python3
"""
Decide what needs computing, and write a manifest the merge can check against.

INCREMENTAL BY CONSTRUCTION
---------------------------
A toponym needs work when it is absent from the store, or when its name_sha
differs from what the store recorded, or when a previous attempt failed
transiently. It does NOT need work when a previous run established that no
route exists -- that is a terminal answer, and retrying ~20M of them on every
pass is the difference between a top-up and a full re-run.

`--retry-status` reopens terminal answers deliberately, which is what you want
after adding an Epitran mode or lifting a quarantine.

SHARDING BY MODE, NOT BY ROW COUNT
----------------------------------
Instantiating an Epitran mode costs far more than transliterating a name, so
every row of a shard shares one mode and each worker loads exactly one.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from phonetics.ipa import routes as R
from phonetics.ipa import store as S

logger = logging.getLogger(__name__)

MAX_ROWS_PER_SHARD = 400_000


STAGED_ROOT = "/vast/ishi/staged"


def stale_inventory_namespaces(inventory_db: str,
                               staged_root: str = STAGED_ROOT) -> List[dict]:
    """Namespaces whose staged extract is NEWER than the inventory DuckDB.

    WHY THIS EXISTS. New toponyms do not enter the inventory when a namespace's
    extract finishes -- they enter when `rebuild_toponyms_index` re-runs its
    extract-to-DuckDB step. Run the planner in between and it compares the new
    corpus against an inventory that never saw it, finds nothing to do, and
    returns ZERO WORK. That is indistinguishable from "already up to date",
    which is Fault 12's exact shape: a stage reports success because its input
    never changed.

    Observed live on 6 Sep 2026: /vast/ishi/staged/tgn/extract/places.jsonl at
    08:38 against an inventory built 4 Aug, while `tgn-extract-nofetch` was
    still running.

    ⚠ Compares mtime to mtime, both from the same filesystem clock. It does NOT
    compare a run id to an mtime -- run ids are UTC and hosts are EDT, and that
    4-hour trap has already misattributed one regression in this project.
    """
    inv = Path(inventory_db)
    if not inv.exists():
        return []
    inv_mtime = inv.stat().st_mtime
    out = []
    root = Path(staged_root)
    if not root.is_dir():
        return []
    for ns_dir in sorted(root.iterdir()):
        if not ns_dir.is_dir():
            continue
        for rel in ("extract/places.jsonl", "final/places.parquet"):
            f = ns_dir / rel
            if f.exists() and f.stat().st_mtime > inv_mtime:
                out.append({
                    "namespace": ns_dir.name, "artefact": rel,
                    "artefact_mtime": f.stat().st_mtime,
                    "inventory_mtime": inv_mtime,
                    "newer_by_hours": round(
                        (f.stat().st_mtime - inv_mtime) / 3600.0, 2),
                })
                break
    return out


def build_plan(inventory_db: str, store_db: str, out_dir: Path, run_id: str,
               retry_statuses: Optional[List[str]] = None,
               allow_quarantined: bool = False,
               max_rows_per_shard: int = MAX_ROWS_PER_SHARD,
               work_dir_override: Optional[Path] = None) -> Dict:
    import duckdb

    out_dir.mkdir(parents=True, exist_ok=True)
    table = R.RouteTable(allow_quarantined=allow_quarantined)

    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='60GB'")
    con.execute(f"ATTACH '{inventory_db}' AS inv (READ_ONLY)")
    con.execute(f"ATTACH '{store_db}' AS st")
    con.execute(S.DDL.replace("CREATE TABLE IF NOT EXISTS ",
                              "CREATE TABLE IF NOT EXISTS st."))

    retry = set(retry_statuses or [])
    # A status is "settled" unless we were asked to reopen it.
    settled = (S.TERMINAL_STATUSES | {S.STATUS_OK}) - retry
    settled_sql = ",".join(f"'{s}'" for s in sorted(settled)) or "''"

    # Everything needing work, in one pass. name_sha is recomputed in SQL so a
    # changed name invalidates the stored row without trusting the id format.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE work AS
        SELECT i.toponym_id, i.name, coalesce(i.lang,'') AS lang, i.script,
               substr(sha256(i.name), 1, 16) AS name_sha
        FROM inv.toponyms i
        LEFT JOIN st.ipa s USING (toponym_id)
        WHERE i.name IS NOT NULL AND i.name <> ''
          AND (
                s.toponym_id IS NULL
             OR s.name_sha IS DISTINCT FROM substr(sha256(i.name), 1, 16)
             OR s.status NOT IN ({settled_sql})
          )
    """)
    total_work = con.execute("SELECT count(*) FROM work").fetchone()[0]

    cells = con.execute(
        "SELECT lang, script, count(*) n FROM work GROUP BY lang, script "
        "ORDER BY n DESC").fetchall()

    shards: List[Dict] = []
    terminal_rows: Dict[str, int] = {}
    for lang, script, n in cells:
        route, status = table.resolve(lang, script)
        # Shard ids become FILENAMES, and 431 of the corpus's lang values are
        # not language codes -- '1510/', ' Acland St'. A '/' turns into a
        # directory separator and the write fails mid-array.
        ltok = R.shard_token(lang)
        stok = R.shard_token(script)
        if route is None:
            terminal_rows[status] = terminal_rows.get(status, 0) + n
            shards.append({
                "shard_id": f"terminal-{status}-{ltok}-{stok}",
                "lang": lang, "script": script, "backend": None, "mode": None,
                "status": status, "rows": n, "terminal": True,
            })
            continue
        n_parts = max(1, -(-n // max_rows_per_shard))
        for p in range(n_parts):
            shards.append({
                "shard_id": f"{route.backend}-{route.mode}-{ltok}-{stok}-{p:03d}",
                "lang": lang, "script": script,
                "backend": route.backend, "mode": route.mode,
                "status": "pending", "rows": n, "terminal": False,
                "part": p, "n_parts": n_parts,
            })

    stale = stale_inventory_namespaces(inventory_db)

    plan = {
        "run_id": run_id,
        "stale_inventory_namespaces": stale,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inventory_db": inventory_db,
        "store_db": store_db,
        "rows_needing_work": total_work,
        "route_table": table.summary(),
        "retry_statuses": sorted(retry),
        "terminal_rows": terminal_rows,
        "computable_rows": total_work - sum(terminal_rows.values()),
        "shards": shards,
        "n_shards": len(shards),
        "n_compute_shards": sum(1 for s in shards if not s["terminal"]),
    }
    (out_dir / f"plan-{run_id}.json").write_text(json.dumps(plan, indent=2))

    # The work list itself, partitioned so a worker reads only its own shard.
    if work_dir_override is not None:
        # Re-planning against an already-exported work set: the shard NAMES may
        # need to change (a bad lang made an unwritable filename) while the
        # rows themselves are unchanged, and re-exporting 72.7M rows to fix a
        # naming bug is pure waste.
        work_dir = work_dir_override
        if not work_dir.exists():
            raise SystemExit(f"--reuse-work given but {work_dir} does not exist")
    else:
        work_dir = out_dir / f"work-{run_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"""
            COPY (SELECT * FROM work) TO '{work_dir}'
            (FORMAT PARQUET, PARTITION_BY (lang, script), OVERWRITE_OR_IGNORE 1)
        """)
    plan["work_dir"] = str(work_dir)
    (out_dir / f"plan-{run_id}.json").write_text(json.dumps(plan, indent=2))
    return plan


def main():
    ap = argparse.ArgumentParser(description="Plan an incremental IPA run")
    ap.add_argument("--inventory-db", required=True)
    ap.add_argument("--store-db", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--retry-status", action="append", default=[],
                    help="reopen a settled status, e.g. no_route after adding a mode")
    ap.add_argument("--allow-quarantined", action="store_true")
    ap.add_argument("--max-rows-per-shard", type=int, default=MAX_ROWS_PER_SHARD)
    ap.add_argument("--reuse-work", help="path to an existing work-<run> dir; skips re-exporting the row set")
    ap.add_argument("--allow-no-work", action="store_true",
                    help="accept a plan with zero rows to do; without it a "
                         "zero is REFUSED, because it cannot be told apart "
                         "from a stale inventory")
    ap.add_argument("--ignore-stale-inventory", action="store_true",
                    help="proceed even though a staged extract is newer "
                         "than the inventory DuckDB")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = build_plan(a.inventory_db, a.store_db, Path(a.out_dir), a.run_id,
                   a.retry_status, a.allow_quarantined, a.max_rows_per_shard,
                   Path(a.reuse_work) if a.reuse_work else None)
    print(f"rows needing work : {p['rows_needing_work']:,}")
    print(f"  computable      : {p['computable_rows']:,}")
    print(f"  terminal        : {sum(p['terminal_rows'].values()):,} "
          f"{p['terminal_rows']}")
    print(f"shards            : {p['n_shards']:,} "
          f"({p['n_compute_shards']:,} to compute)")
    print(f"-> {Path(a.out_dir)/f'plan-{a.run_id}.json'}")

    # A zero here is ambiguous and must not pass silently.
    stale = p.get("stale_inventory_namespaces") or []
    if stale:
        print("\n\u26a0 INVENTORY IS BEHIND THE STAGED CORPUS:")
        for d in stale[:10]:
            print(f"   {d['namespace']:<10} {d['artefact']:<24} "
                  f"newer by {d['newer_by_hours']}h")
        if not a.ignore_stale_inventory:
            raise SystemExit(
                "refusing to plan against an inventory older than the staged "
                "corpus: rebuild_toponyms_index must re-run its "
                "extract-to-DuckDB step first, or this plan silently omits "
                "every new toponym. Pass --ignore-stale-inventory to override.")

    if p["rows_needing_work"] == 0 and not a.allow_no_work:
        raise SystemExit(
            "plan has ZERO rows to do. That is indistinguishable from a stale "
            "inventory, so it is refused rather than reported as success. "
            "Confirm the inventory actually contains what you expect, then "
            "pass --allow-no-work.")


if __name__ == "__main__":
    main()
