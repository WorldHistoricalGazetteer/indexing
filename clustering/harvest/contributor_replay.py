"""Batch 12 — Contributor attestation replay from DO PostgreSQL.

Reads ``contributor_attestations`` from the canonical DO PostgreSQL store
(SSH-tunnelled via ``clustering.pg_client.pg_connection``) and inserts each
``status = 'active'`` row into the Pitt-side SQLite hard-link overlay.

* ``source_category`` is always ``'contributor'``.
* ``source_id`` is ``'contributor:<user_id>'`` (Master Plan §10.2). When the
  row carries ``legacy_v3_2 = true`` (Batch 13b), the suffix
  ``:legacy_v3_2`` is appended so the gateway can filter without rejoining
  to DO PG.
* Rows with ``status != 'active'`` (i.e. ``pending`` / ``rejected`` /
  ``superseded``) are intentionally **excluded** from the publishable
  SQLite — pending assertions are visible only to in-scope users via a
  Django-side scope-filtering merge at request time (Master Plan §7.4 /
  §9.3), not by being in the public hard-link store.

The column names ``place_a`` / ``place_b`` / ``user_id`` / ``relation_type`` /
``status`` are the standard contract; ``legacy_v3_2`` is tolerated as
absent (older DB schema). Override the SELECT with
``WHG_CONTRIBUTOR_QUERY`` if the production schema diverges.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterator

from clustering.sqlite_overlay import builder, insert_rows


# Canonical Django-side table name (app label = ``api``,
# model = ``ContributorAttestation``). See
# ``../../../whg3/api/models.py`` and the ``api/migrations/0002_*`` migration.
_TABLE = "api_contributorattestation"

_DEFAULT_QUERY = f"""
    SELECT
        user_id,
        dataset_id,
        place_a,
        place_b,
        relation_type,
        asserted_at,
        justification,
        COALESCE(legacy_v3_2, FALSE) AS legacy_v3_2
    FROM {_TABLE}
    WHERE status = 'active'
    ORDER BY id
"""

# Fallback used when the ``legacy_v3_2`` column doesn't yet exist on the DO
# side — defensive, since the migration that adds it ships in the same
# rebuild. Also used when the older, unprefixed ``contributor_attestations``
# table name is in play (pre-Django integration test fixtures).
_FALLBACK_QUERY = f"""
    SELECT
        user_id,
        dataset_id,
        place_a,
        place_b,
        relation_type,
        asserted_at,
        justification,
        FALSE AS legacy_v3_2
    FROM {_TABLE}
    WHERE status = 'active'
    ORDER BY id
"""


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _build_source_id(user_id: Any, legacy_v3_2: bool) -> str:
    base = f"contributor:{user_id}"
    return f"{base}:legacy_v3_2" if legacy_v3_2 else base


def _to_hard_link_row(record: dict[str, Any]) -> dict[str, Any] | None:
    """Map one PG row → hard_link_assertions row, or ``None`` if invalid."""
    place_a = record.get("place_a")
    place_b = record.get("place_b")
    if not isinstance(place_a, str) or not isinstance(place_b, str):
        return None
    if place_a == place_b:
        return None
    user_id = record.get("user_id")
    if user_id is None:
        return None
    relation_type = record.get("relation_type")
    if not isinstance(relation_type, str):
        return None

    pa, pb = _canonical_pair(place_a, place_b)
    asserted_at = record.get("asserted_at")
    if hasattr(asserted_at, "isoformat"):
        asserted_at = asserted_at.isoformat()
    elif asserted_at is not None and not isinstance(asserted_at, str):
        asserted_at = str(asserted_at)

    justification = record.get("justification")
    if justification is not None and not isinstance(justification, str):
        justification = str(justification)

    return {
        "place_a": pa,
        "place_b": pb,
        "relation_type": relation_type,
        "source_category": "contributor",
        "source_id": _build_source_id(user_id, bool(record.get("legacy_v3_2"))),
        "asserted_at": asserted_at,
        "justification": justification,
    }


# ---------------------------------------------------------------------------
# Async fetch from DO PG via the existing SSH-tunnelled client
# ---------------------------------------------------------------------------


async def _fetch_active_rows(query: str) -> list[dict[str, Any]]:
    # Lazy import: pg_client pulls in asyncpg + sshtunnel which are heavy and
    # not needed for the LOC / staged harvesters.
    from clustering.pg_client import pg_connection  # noqa: WPS433
    async with pg_connection() as conn:
        try:
            rows = await conn.fetch(query)
        except Exception as exc:
            # If the legacy_v3_2 column is missing, retry once with the
            # fallback query that hard-codes FALSE for that field.
            if "legacy_v3_2" in str(exc) and query is _DEFAULT_QUERY:
                rows = await conn.fetch(_FALLBACK_QUERY)
            else:
                raise
        return [dict(r) for r in rows]


def fetch_active_rows(query: str | None = None) -> list[dict[str, Any]]:
    """Synchronous façade over the asyncpg fetch."""
    return asyncio.run(_fetch_active_rows(query or _DEFAULT_QUERY))


def iter_hard_link_rows(query: str | None = None) -> Iterator[dict[str, Any]]:
    """Yield hard_link_assertions-shaped rows from active contributor attestations."""
    for record in fetch_active_rows(query):
        row = _to_hard_link_row(record)
        if row is not None:
            yield row


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def replay(
    *,
    db_path: Path,
    query: str | None = None,
    batch_size: int = 5_000,
) -> dict[str, Any]:
    rows = list(iter_hard_link_rows(query))
    with builder(db_path) as conn:
        stats = insert_rows(conn, rows, batch_size=batch_size)
    stats["db_path"] = str(db_path)
    stats["fetched_rows"] = len(rows)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay active contributor attestations from DO PG into the SQLite overlay"
    )
    parser.add_argument("--db-path", required=True, help="SQLite output path")
    parser.add_argument("--query",
                        default=os.environ.get("WHG_CONTRIBUTOR_QUERY"),
                        help="Override the SELECT (default: standard contract; "
                             "auto-falls-back when legacy_v3_2 column is absent)")
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch rows but do not insert; print summary only")
    args = parser.parse_args()

    if args.dry_run:
        rows = list(iter_hard_link_rows(args.query))
        print(json.dumps({
            "fetched_rows": len(rows),
            "sample": rows[:5],
            "db_path": args.db_path,
        }, indent=2, sort_keys=True))
        return

    try:
        summary = replay(
            db_path=Path(args.db_path),
            query=args.query,
            batch_size=args.batch_size,
        )
    except ImportError as exc:
        print(
            f"ERROR: contributor replay needs asyncpg + sshtunnel installed: {exc}",
            file=sys.stderr,
        )
        sys.exit(2)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
