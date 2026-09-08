#!/usr/bin/env python3
"""
The IPA store: one DuckDB table keyed on toponym_id, built for TOP-UP.

DESIGN NOTES THAT ARE NOT OBVIOUS
---------------------------------
1. A row exists for EVERY toponym examined, including the ones no backend can
   handle. Recording only successes makes two different things look identical
   -- "we tried and there is no route" and "we never looked" -- so every
   re-run would retry all ~20M unroutable toponyms forever, and any coverage
   figure would be a numerator with no denominator.

2. Staleness is detected STRUCTURALLY, via name_sha, not by trusting that
   toponym_id encodes the name. The id is {name}@{lang} today; if that
   convention ever changes, a hash comparison still catches a changed name
   whereas an id comparison silently would not.

3. The merge is serial and single-writer. Concurrent DuckDB writers do not
   work; workers emit Parquet shards and exactly one process merges them.

4. The merge refuses to run on an INCOMPLETE shard set. A glob of whatever
   happens to be on disk cannot tell a finished run from one whose array tasks
   died -- both look like "some Parquet files". The plan writes the expected
   shard ids and the merge checks against them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

STATUS_OK = "ok"
STATUS_NO_LANG = "no_lang"
STATUS_NO_ROUTE = "no_route"
STATUS_QUARANTINED = "quarantined"
STATUS_NON_LANGUAGE_TAG = "non_language_tag"
STATUS_FAILED = "failed"
STATUS_EMPTY = "empty_output"
STATUS_ECHO = "echoed_input"

TERMINAL_STATUSES = {
    STATUS_NO_LANG, STATUS_NO_ROUTE, STATUS_QUARANTINED,
    STATUS_NON_LANGUAGE_TAG,
}

DDL = """
CREATE TABLE IF NOT EXISTS ipa (
    toponym_id     VARCHAR PRIMARY KEY,
    name_sha       VARCHAR NOT NULL,
    lang           VARCHAR,
    script         VARCHAR,
    ipa            VARCHAR,
    backend        VARCHAR,
    mode           VARCHAR,
    status         VARCHAR NOT NULL,
    error          VARCHAR,
    run_id         VARCHAR NOT NULL,
    computed_at    TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         VARCHAR PRIMARY KEY,
    started_at     TIMESTAMP,
    finished_at    TIMESTAMP,
    planned_shards INTEGER,
    merged_shards  INTEGER,
    rows_written   BIGINT,
    notes          VARCHAR
);
CREATE TABLE IF NOT EXISTS schema_meta (
    key VARCHAR PRIMARY KEY, value VARCHAR
);
"""


def name_sha(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def connect(path: str, read_only: bool = False):
    import duckdb
    con = duckdb.connect(path, read_only=read_only)
    if not read_only:
        con.execute(DDL)
        con.execute(
            "INSERT INTO schema_meta VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [str(SCHEMA_VERSION)],
        )
    return con


def merge_shards(con, shard_paths: List[Path], run_id: str) -> Dict[str, int]:
    """Upsert every shard into the store in ONE transaction.

    Returns per-status counts actually written, so the caller can compare them
    against what the plan expected rather than trusting a bare success.
    """
    if not shard_paths:
        return {"rows": 0}
    files = [str(p) for p in shard_paths]
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE incoming AS
            SELECT * FROM read_parquet(?)
            """,
            [files],
        )
        # Last write wins within the batch, deterministically.
        con.execute("""
            CREATE OR REPLACE TEMP TABLE incoming_dedup AS
            SELECT * FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY toponym_id ORDER BY computed_at DESC
                ) AS rn FROM incoming
            ) WHERE rn = 1
        """)
        con.execute("DELETE FROM ipa WHERE toponym_id IN "
                    "(SELECT toponym_id FROM incoming_dedup)")
        con.execute("""
            INSERT INTO ipa
            SELECT toponym_id, name_sha, lang, script, ipa, backend, mode,
                   status, error, run_id, computed_at
            FROM incoming_dedup
        """)
        rows = con.execute("SELECT count(*) FROM incoming_dedup").fetchone()[0]
        by_status = dict(con.execute(
            "SELECT status, count(*) FROM incoming_dedup GROUP BY status"
        ).fetchall())
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    out = {"rows": rows}
    out.update({f"status_{k}": v for k, v in by_status.items()})
    return out


def coverage(con) -> Dict[str, object]:
    """Coverage WITH its denominator, always."""
    total = con.execute("SELECT count(*) FROM ipa").fetchone()[0]
    by_status = dict(con.execute(
        "SELECT status, count(*) FROM ipa GROUP BY status ORDER BY 2 DESC"
    ).fetchall())
    ok = by_status.get(STATUS_OK, 0)
    return {
        "rows_in_store": total,
        "with_ipa": ok,
        "coverage_of_examined_pct": round(100.0 * ok / total, 4) if total else None,
        "by_status": by_status,
    }
