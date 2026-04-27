"""Persistent Symphonym embedding cache (Batch 9).

The cache is a single DuckDB file shared across all compute runs:

    CREATE TABLE embeddings (
        toponym_id      TEXT NOT NULL,
        model_version   INT  NOT NULL,
        checkpoint_hash TEXT NOT NULL,
        embedding       BLOB NOT NULL,
        computed_at     TEXT NOT NULL,
        PRIMARY KEY (toponym_id, model_version, checkpoint_hash)
    );

Why DuckDB and not Parquet?

* Append-friendly with idempotent ``INSERT OR IGNORE``.
* Indexed lookup by composite key — version-bump invalidation is just a
  query that misses (no rewrite/expiry needed).
* The file lives outside the per-run staged tree because it spans runs.

Version preflight
-----------------
``compute_checkpoint_hash`` reads the model checkpoint file and returns a
SHA-256 hex digest. Any change to the checkpoint bytes (re-train, recompile,
weight surgery) flips the digest and forces a full recompute. The
``model_version`` axis is the user-facing version tag (the
``--embedding-version`` argument); it bumps independently when the
embedding schema changes.
"""

from __future__ import annotations

import hashlib
import sqlite3  # noqa: F401  # kept for import compatibility / dialect notes
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import duckdb


_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    toponym_id      TEXT NOT NULL,
    model_version   INT  NOT NULL,
    checkpoint_hash TEXT NOT NULL,
    embedding       BLOB NOT NULL,
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (toponym_id, model_version, checkpoint_hash)
);
"""

_INSERT_SQL = (
    "INSERT OR IGNORE INTO embeddings "
    "(toponym_id, model_version, checkpoint_hash, embedding, computed_at) "
    "VALUES (?, ?, ?, ?, ?)"
)

_LOOKUP_SQL = (
    "SELECT toponym_id, embedding FROM embeddings "
    "WHERE model_version = ? AND checkpoint_hash = ?"
)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def compute_checkpoint_hash(checkpoint_path: str | Path,
                            *, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 hex digest of the checkpoint file.

    Streamed read so a multi-GB ``.pt`` doesn't have to fit in RAM.
    """
    h = hashlib.sha256()
    with open(checkpoint_path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Cache surface
# ---------------------------------------------------------------------------


def open_cache(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open (or create) the cache file and ensure the schema is present.

    Caller owns the connection. ``DuckDB`` handles cross-process locking so
    concurrent compute runs are safe; conflicting inserts are dropped by
    the ``PRIMARY KEY`` (``INSERT OR IGNORE`` semantics via the SQL).
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    conn.execute(_SCHEMA)
    return conn


@contextmanager
def cache_connection(db_path: str | Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = open_cache(db_path)
    try:
        yield conn
    finally:
        conn.close()


def load_hits(
    conn: duckdb.DuckDBPyConnection,
    *,
    model_version: int,
    checkpoint_hash: str,
) -> dict[str, bytes]:
    """Return ``{toponym_id: embedding_bytes}`` for the (version, hash) key.

    The full result is materialised in memory; for the WHG corpus
    (~67M toponyms × 128 bytes ≈ 8.5 GB) this should be sized accordingly.
    On first run after a version bump the result is empty and the GPU does
    the full corpus.
    """
    rows = conn.execute(
        _LOOKUP_SQL, [int(model_version), str(checkpoint_hash)],
    ).fetchall()
    return {tid: bytes(emb) for tid, emb in rows}


def insert_many(
    conn: duckdb.DuckDBPyConnection,
    rows: Iterable[tuple[str, bytes]],
    *,
    model_version: int,
    checkpoint_hash: str,
) -> int:
    """Bulk-insert ``(toponym_id, embedding_bytes)`` pairs. Returns the
    number of rows newly written (existing rows are skipped via
    ``INSERT OR IGNORE``)."""
    now = datetime.now(timezone.utc).isoformat()
    payload = [
        (tid, int(model_version), str(checkpoint_hash), emb, now)
        for tid, emb in rows
    ]
    if not payload:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    conn.executemany(_INSERT_SQL, payload)
    after = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    return int(after - before)


def cache_size(conn: duckdb.DuckDBPyConnection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])


def cache_size_for(
    conn: duckdb.DuckDBPyConnection,
    *,
    model_version: int,
    checkpoint_hash: str,
) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM embeddings "
        "WHERE model_version = ? AND checkpoint_hash = ?",
        [int(model_version), str(checkpoint_hash)],
    ).fetchone()[0])
