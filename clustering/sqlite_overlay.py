"""Batch 12 — SQLite hard-link overlay (build, atomic ship-to-Pitt).

Centralises:

* Schema initialisation using the constants in
  ``processing.staging_contract`` (``HARD_LINK_SQLITE_SCHEMA``,
  ``HARD_LINK_RELATION_TYPES``, ``HARD_LINK_SOURCE_CATEGORIES``).
* A ``HardLinkBuilder`` context manager that opens the file with the
  pragmas required for fast bulk insert (WAL, large cache, deferred sync)
  and flushes/commits on exit.
* ``insert_rows`` — bulk ``INSERT OR IGNORE`` driven by validated rows.
* ``ship_to_pitt`` — rsync to a Pitt staging path then a remote
  ``mv`` over the live file. The atomic-rename keeps the gateway's open
  descriptor valid; the next ``open()`` picks up the new file.

This module deliberately has no Slurm or harvester knowledge — it is just
the database surface used by ``clustering/harvest/*.py`` and the Slurm
submitter (``processing/submit_hardlinks_slurm.py``).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from processing.staging_contract import (
    HARD_LINK_REQUIRED_FIELDS,
    HARD_LINK_SQLITE_SCHEMA,
    validate_hard_link_row,
)


# Pragmas applied at open time. WAL + large cache + relaxed sync gives the
# fastest bulk insert. The shipping step performs a checkpoint before
# closing so the file is consistent on disk.
_BULK_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA cache_size=-200000;",   # ~200 MB page cache
    "PRAGMA temp_store=MEMORY;",
    "PRAGMA mmap_size=268435456;",  # 256 MB mmap window
)

_INSERT_SQL = (
    "INSERT OR IGNORE INTO hard_link_assertions "
    "(place_a, place_b, relation_type, source_category, source_id, "
    " asserted_at, justification) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


# ---------------------------------------------------------------------------
# Schema + open helpers
# ---------------------------------------------------------------------------


def initialise_schema(conn: sqlite3.Connection) -> None:
    """Apply ``HARD_LINK_SQLITE_SCHEMA`` (idempotent — ``CREATE … IF NOT EXISTS``
    isn't used in the canonical DDL, so we wrap each statement)."""
    for stmt in _split_statements(HARD_LINK_SQLITE_SCHEMA):
        # Tolerate "table already exists" when the schema has been applied
        # to this file before (resumes / partial runs).
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "already exists" not in str(exc):
                raise


def _split_statements(ddl: str) -> Iterator[str]:
    for stmt in ddl.split(";"):
        s = stmt.strip()
        if s:
            yield s


def open_for_bulk(db_path: Path) -> sqlite3.Connection:
    """Open the SQLite at ``db_path`` for bulk insert.

    Creates the file (and parent directory) if missing, applies the bulk
    pragmas, and ensures the schema exists. Caller owns the connection.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    for pragma in _BULK_PRAGMAS:
        conn.execute(pragma)
    initialise_schema(conn)
    return conn


@contextmanager
def builder(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Context manager wrapping ``open_for_bulk`` with a final WAL checkpoint."""
    conn = open_for_bulk(db_path)
    try:
        yield conn
    finally:
        try:
            # Flush the WAL into the main database file so a single mv at
            # ship time captures the full state.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
        conn.close()


# ---------------------------------------------------------------------------
# Bulk insert
# ---------------------------------------------------------------------------


def _row_tuple(row: dict) -> tuple:
    return (
        row["place_a"],
        row["place_b"],
        row["relation_type"],
        row["source_category"],
        row["source_id"],
        row.get("asserted_at"),
        row.get("justification"),
    )


def insert_rows(
    conn: sqlite3.Connection,
    rows: Iterable[dict],
    *,
    batch_size: int = 5_000,
    validate: bool = True,
) -> dict[str, int]:
    """Insert validated rows in batched transactions.

    Returns ``{"attempted": N, "inserted": M, "rejected": R}``. ``inserted``
    counts only rows that were actually written (de-duped via the unique
    constraint); ``rejected`` counts validation failures.
    """
    attempted = 0
    inserted = 0
    rejected = 0

    batch: list[tuple] = []
    cursor = conn.cursor()

    def _flush():
        nonlocal inserted
        if not batch:
            return
        conn.execute("BEGIN")
        try:
            cursor.executemany(_INSERT_SQL, batch)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        # ``rowcount`` after executemany on an OR IGNORE reflects the number
        # of rows actually written, modulo SQLite version quirks. We use
        # changes() as a more reliable counter.
        inserted += int(conn.execute("SELECT changes()").fetchone()[0])
        batch.clear()

    for row in rows:
        attempted += 1
        if validate:
            try:
                validate_hard_link_row(row)
            except Exception:
                rejected += 1
                continue
        batch.append(_row_tuple(row))
        if len(batch) >= batch_size:
            _flush()
    _flush()

    return {"attempted": attempted, "inserted": inserted, "rejected": rejected}


# ---------------------------------------------------------------------------
# Ship-to-Pitt (atomic swap)
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise RuntimeError(
            f"Command failed (rc={result.returncode}): {' '.join(cmd)}"
        )


def ship_to_pitt(
    *,
    local_db: Path,
    remote_user: str,
    remote_host: str,
    remote_dir: str,
    remote_filename: str = "hard_links.sqlite",
    ssh_options: str | None = None,
) -> dict[str, str]:
    """Rsync ``local_db`` to ``<remote_dir>/.<filename>.incoming`` then ``mv``
    it over the live ``<remote_dir>/<filename>`` atomically.

    The remote ``mv`` is a single rename within the same filesystem, so the
    gateway's open descriptors against the previous file remain valid (the
    inode is preserved until the last fd closes). Subsequent ``open()`` calls
    pick up the new file. The gateway is responsible for periodic re-open
    (tracked separately).

    Returns ``{"remote_path": ..., "incoming_path": ...}``.
    """
    if not local_db.exists():
        raise FileNotFoundError(f"local_db not found: {local_db}")

    remote_target = f"{remote_user}@{remote_host}:{remote_dir.rstrip('/')}/{remote_filename}"
    remote_incoming = f"{remote_user}@{remote_host}:{remote_dir.rstrip('/')}/.{remote_filename}.incoming"

    rsync_ssh = ["-e", f"ssh {ssh_options}"] if ssh_options else []

    # 1. rsync to a hidden incoming path so partial transfers never replace
    #    the live file.
    _run([
        "rsync", "-az", "--inplace", "--partial",
        *rsync_ssh,
        str(local_db),
        remote_incoming,
    ])

    # 2. Single atomic rename within the remote filesystem.
    ssh_cmd = ["ssh"]
    if ssh_options:
        ssh_cmd.extend(ssh_options.split())
    ssh_cmd.extend([
        f"{remote_user}@{remote_host}",
        f"mv {remote_dir.rstrip('/')}/.{remote_filename}.incoming "
        f"{remote_dir.rstrip('/')}/{remote_filename}",
    ])
    _run(ssh_cmd)

    return {
        "remote_path": f"{remote_dir.rstrip('/')}/{remote_filename}",
        "incoming_path": f"{remote_dir.rstrip('/')}/.{remote_filename}.incoming",
    }


# ---------------------------------------------------------------------------
# Local-only finalisation (for runs that build a file-on-CRC then publish via
# a separate mechanism).
# ---------------------------------------------------------------------------


def finalise_local(local_db: Path, *, optimize: bool = True) -> int:
    """Close out a build by checkpointing and (optionally) running OPTIMIZE.

    Returns the row count for sanity-check logging.
    """
    conn = sqlite3.connect(str(local_db))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        if optimize:
            conn.execute("PRAGMA optimize;")
        count = conn.execute("SELECT COUNT(*) FROM hard_link_assertions").fetchone()[0]
        return int(count)
    finally:
        conn.close()
