"""Batch 12 — Contributor attestation replay from legacy v3.2 DO PostgreSQL.

The legacy v3 schema has **no** ``contributor_attestations`` table. Identity-
linking acceptance evidence is stored in two durable tables that the
reconciliation UI writes when a curator accepts a hit:

* **``place_link``** (``places.PlaceLink``) — written by
  ``datasets.services.write_wd_pass0`` and the manual review path. One row
  per accepted authority concordance:
    ``place`` (FK → ``places.id``), ``reviewer`` (FK → ``users.id``),
    ``task_id``, ``create_date``, ``jsonb = {"type": "closeMatch",
    "identifier": "wd:Q90"|"tgn:7000874"|...}``.
* **``close_matches``** (``places.CloseMatch``) — written for cross-dataset
  WHG-place equivalences:
    ``place_a``, ``place_b`` (both FK → ``places.id``), ``created_by`` (FK
    → ``users.id``), ``created_at``, ``basis ∈ {authid, reviewed, imported}``.

The ``hits`` table is the **workflow staging area**, not the canonical
store: ``Hit.matched`` is dead code (never set to True anywhere in whg3),
and ``Hit.reviewed`` only signals "a curator clicked something". The
acceptance is recorded by *what gets written* to ``place_link`` /
``close_matches``, not by a flag on Hit.

This script harvests both tables, maps each row into the canonical
``hard_link_assertions`` shape, and inserts into the Pitt-side SQLite
overlay. Every row from this legacy harvest carries the
``:legacy_v3_2`` suffix on ``source_id`` so the gateway can distinguish
v3 evidence from fresh dynamic-cluster attestations once the new flow
ships.

Selection rules (per user, 2026-05-02):

* **Sources**: ``place_link`` + ``close_matches`` only. The legacy ES
  ``whg`` index ``relation.parent`` field is intentionally NOT consulted
  — assumed redundant with ``close_matches``.
* **CloseMatch basis**: ``reviewed`` or ``authid`` only. ``imported`` is
  excluded as evidence of intent without human review.
* **Dataset status**: only places belonging to ``ds_status = 'accessioned'``
  datasets contribute. Pending / draft / rejected datasets are skipped
  entirely.

``place_id`` namespacing: the new contract is
``f"whg:{datasets.id}:{places.id}"``. Note ``places.dataset`` is a
``ForeignKey(to_field='label')`` so the join must be ``places.dataset =
datasets.label``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from clustering.sqlite_overlay import builder, insert_rows


# ---------------------------------------------------------------------------
# Source queries
# ---------------------------------------------------------------------------


_PLACE_LINK_QUERY = """
    SELECT
        ('whg:' || d.id::text || ':' || pl.place_id::text) AS place_a,
        pl.jsonb->>'identifier'                            AS place_b,
        pl.reviewer_id                                     AS user_id,
        pl.create_date                                     AS asserted_at
    FROM place_link pl
    JOIN places   p ON p.id    = pl.place_id
    JOIN datasets d ON d.label = p.dataset
    WHERE pl.jsonb ? 'identifier'
      AND pl.jsonb->>'identifier' IS NOT NULL
      AND pl.jsonb->>'identifier' <> ''
      AND pl.reviewer_id IS NOT NULL
      AND d.ds_status = 'accessioned'
"""


_CLOSE_MATCH_QUERY = """
    SELECT
        ('whg:' || da.id::text || ':' || cm.place_a_id::text) AS place_a,
        ('whg:' || db.id::text || ':' || cm.place_b_id::text) AS place_b,
        cm.created_by_id                                       AS user_id,
        cm.created_at                                          AS asserted_at,
        cm.basis                                               AS basis
    FROM close_matches cm
    JOIN places   pa ON pa.id    = cm.place_a_id
    JOIN places   pb ON pb.id    = cm.place_b_id
    JOIN datasets da ON da.label = pa.dataset
    JOIN datasets db ON db.label = pb.dataset
    WHERE da.ds_status = 'accessioned'
      AND db.ds_status = 'accessioned'
      AND cm.basis IN ('reviewed', 'authid')
      AND cm.created_by_id IS NOT NULL
"""


# ---------------------------------------------------------------------------
# Row → hard-link mapping
# ---------------------------------------------------------------------------


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _build_source_id(user_id: Any) -> str:
    """Every legacy v3 row carries the ``:legacy_v3_2`` suffix so the gateway
    can filter or down-weight it once dynamic-cluster attestations land."""
    return f"contributor:{user_id}:legacy_v3_2"


def _coerce_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def _looks_namespaced(s: str) -> bool:
    """``identifier`` strings stored on PlaceLink should be namespaced
    (e.g. ``wd:Q90``, ``tgn:7000874``). Reject bare strings that lack the
    ``<ns>:<id>`` shape — they would corrupt the cluster store."""
    if ":" not in s:
        return False
    ns, _, rest = s.partition(":")
    return bool(ns) and bool(rest)


def _place_link_to_hard_link(record: dict[str, Any]) -> dict[str, Any] | None:
    place_a = record.get("place_a")
    place_b = record.get("place_b")
    if not isinstance(place_a, str) or not isinstance(place_b, str):
        return None
    if not _looks_namespaced(place_b):
        return None
    if place_a == place_b:
        return None
    user_id = record.get("user_id")
    if user_id is None:
        return None
    pa, pb = _canonical_pair(place_a, place_b)
    return {
        "place_a": pa,
        "place_b": pb,
        # PlaceLink.jsonb.type is always 'closeMatch' in the legacy code path
        # (see datasets.services.write_wd_pass0). Don't bother re-reading it.
        "relation_type": "closeMatch",
        "source_category": "contributor",
        "source_id": _build_source_id(user_id),
        "asserted_at": _coerce_iso(record.get("asserted_at")),
        "justification": "place_link",
    }


def _close_match_to_hard_link(record: dict[str, Any]) -> dict[str, Any] | None:
    place_a = record.get("place_a")
    place_b = record.get("place_b")
    if not isinstance(place_a, str) or not isinstance(place_b, str):
        return None
    if place_a == place_b:
        return None
    user_id = record.get("user_id")
    if user_id is None:
        return None
    pa, pb = _canonical_pair(place_a, place_b)
    basis = record.get("basis") or ""
    # Both PlaceLink and CloseMatch represent contributor-asserted equivalence.
    # The legacy CloseMatch.basis is preserved in justification so
    # auditors can distinguish curator-reviewed vs authid-derived links.
    return {
        "place_a": pa,
        "place_b": pb,
        "relation_type": "closeMatch",
        "source_category": "contributor",
        "source_id": _build_source_id(user_id),
        "asserted_at": _coerce_iso(record.get("asserted_at")),
        "justification": f"close_match:{basis}",
    }


# ---------------------------------------------------------------------------
# Async fetch from DO PG via the existing SSH-tunnelled client
# ---------------------------------------------------------------------------


async def _fetch_all_async() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Lazy import: pg_client pulls in asyncpg + sshtunnel which are heavy and
    # not needed for the LOC / staged harvesters.
    from clustering.pg_client import pg_connection  # noqa: WPS433

    async with pg_connection() as conn:
        place_link_rows = await conn.fetch(_PLACE_LINK_QUERY)
        close_match_rows = await conn.fetch(_CLOSE_MATCH_QUERY)
    return [dict(r) for r in place_link_rows], [dict(r) for r in close_match_rows]


def fetch_all() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Synchronous façade — returns ``(place_link_rows, close_match_rows)``."""
    return asyncio.run(_fetch_all_async())


def iter_hard_link_rows(
    place_link_rows: Iterable[dict[str, Any]],
    close_match_rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Map raw PG rows into hard_link_assertions-shaped rows.

    Returns ``(rows, per_source_counts)`` where ``per_source_counts`` is a
    breakdown of converted rows per source for diagnostics.
    """
    out: list[dict[str, Any]] = []
    counts = {
        "place_link_input": 0,
        "place_link_converted": 0,
        "close_match_input": 0,
        "close_match_converted": 0,
    }
    for record in place_link_rows:
        counts["place_link_input"] += 1
        row = _place_link_to_hard_link(record)
        if row is not None:
            counts["place_link_converted"] += 1
            out.append(row)
    for record in close_match_rows:
        counts["close_match_input"] += 1
        row = _close_match_to_hard_link(record)
        if row is not None:
            counts["close_match_converted"] += 1
            out.append(row)
    return out, counts


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def replay(
    *,
    db_path: Path,
    batch_size: int = 5_000,
) -> dict[str, Any]:
    place_link_rows, close_match_rows = fetch_all()
    rows, counts = iter_hard_link_rows(place_link_rows, close_match_rows)
    with builder(db_path) as conn:
        stats = insert_rows(conn, rows, batch_size=batch_size)
    stats["db_path"] = str(db_path)
    stats["fetched_rows"] = len(rows)
    stats.update(counts)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replay legacy v3.2 contributor attestations from DO PG (place_link "
            "+ close_matches) into the SQLite hard-link overlay"
        )
    )
    parser.add_argument("--db-path", required=True, help="SQLite output path")
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch rows but do not insert; print summary only")
    args = parser.parse_args()

    if args.dry_run:
        try:
            place_link_rows, close_match_rows = fetch_all()
        except ImportError as exc:
            print(
                f"ERROR: contributor replay needs asyncpg + sshtunnel installed: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        rows, counts = iter_hard_link_rows(place_link_rows, close_match_rows)
        print(json.dumps({
            "fetched_rows": len(rows),
            "counts": counts,
            "sample": rows[:5],
            "db_path": args.db_path,
        }, indent=2, sort_keys=True))
        return

    try:
        summary = replay(
            db_path=Path(args.db_path),
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
