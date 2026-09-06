#!/usr/bin/env python3
"""
Serial merge of shard Parquet into the store.

WHY THIS REFUSES TO GLOB
------------------------
A directory of Parquet files cannot distinguish a finished run from one whose
array tasks were pre-empted -- both are "some Parquet files", and merging
whichever ones exist writes a store that is quietly missing whole languages
while every log line says success. So the merge reads the PLAN's shard list and
requires each expected shard to be present, reporting units of work done
against units expected.

--allow-partial merges what exists anyway, but records the shortfall in the
runs table and prints it, so a partial merge is a decision on the record rather
than an accident.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from phonetics.ipa import store as S

logger = logging.getLogger(__name__)


def merge(plan_path: Path, shard_dir: Path, store_db: str,
          allow_partial: bool = False) -> dict:
    plan = json.loads(Path(plan_path).read_text())
    expected = [s["shard_id"] for s in plan["shards"]]
    present = [s for s in expected if (shard_dir / f"{s}.parquet").exists()]
    missing = [s for s in expected if not (shard_dir / f"{s}.parquet").exists()]

    print(f"shards expected : {len(expected):,}")
    print(f"shards present  : {len(present):,}")
    print(f"shards MISSING  : {len(missing):,}")
    if missing:
        for sid in missing[:20]:
            print(f"   missing: {sid}")
        if len(missing) > 20:
            print(f"   ... and {len(missing)-20:,} more")
        if not allow_partial:
            raise SystemExit(
                "refusing to merge an incomplete shard set; rerun the missing "
                "shards, or pass --allow-partial to record the shortfall and "
                "merge anyway")

    con = S.connect(store_db)
    paths = [shard_dir / f"{s}.parquet" for s in present]
    started = datetime.now(timezone.utc)
    stats = S.merge_shards(con, paths, plan["run_id"])
    con.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT (run_id) DO UPDATE SET finished_at=excluded.finished_at, "
        "merged_shards=excluded.merged_shards, rows_written=excluded.rows_written, "
        "notes=excluded.notes",
        [plan["run_id"], started, datetime.now(timezone.utc),
         len(expected), len(present), stats.get("rows", 0),
         json.dumps({"missing_shards": len(missing),
                     "allow_partial": allow_partial})],
    )
    cov = S.coverage(con)
    con.close()
    out = {"expected_shards": len(expected), "merged_shards": len(present),
           "missing_shards": len(missing), "merge_stats": stats,
           "coverage": cov}
    print("\n-- merged --")
    print(json.dumps(stats, indent=2))
    print("\n-- store coverage --")
    print(json.dumps(cov, indent=2))
    return out


def main():
    ap = argparse.ArgumentParser(description="Merge IPA shards into the store")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--shard-dir", required=True)
    ap.add_argument("--store-db", required=True)
    ap.add_argument("--allow-partial", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    merge(Path(a.plan), Path(a.shard_dir), a.store_db, a.allow_partial)


if __name__ == "__main__":
    main()
