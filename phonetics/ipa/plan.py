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

    plan = {
        "run_id": run_id,
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


if __name__ == "__main__":
    main()
