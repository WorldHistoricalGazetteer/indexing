"""Batch 12 — Contributor attestation harvest from DO PostgreSQL.

Two flows feed the batch overlay from DO PG:

**A. Legacy v3.2 replay** (``place_link`` + ``close_matches``). The legacy v3
schema has **no** ``contributor_attestations`` table. Identity-linking
acceptance evidence is stored in two durable tables that the
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
v3 evidence from fresh dynamic-cluster attestations.

**B. Live contributor attestations** (``api_contributorattestation``). This is
the canonical store for links asserted through the platform since the new flow
shipped — the same rows the gateway's ``POST /api/links`` receiver lands in the
Pitt-side *live-delta* SQLite (``gateway/links.py``). Django's ``api/signals.py``
forwards every ``status='active'`` create/revoke to the gateway; this harvest
reads the same ``active`` rows straight from PG and folds them into the freshly
built batch overlay, so the overlay becomes a superset of the live-delta's
active rows (which is what makes the post-build live-delta prune safe — see
``clustering.sqlite_overlay.prune_live_delta`` and Ticket A in
``developer/handoff-hardlink-live-delta-followups.md``).

``source_id`` mirrors ``ContributorAttestation.source_id()`` exactly
(``contributor:<user_id>`` for fresh rows; ``:legacy_v3_2`` suffix for the rare
v3.2-inherited row that is *also* active) so a row present in both the batch
overlay and the live-delta dedups on the identical ``(place_a, place_b,
relation_type, source_id)`` UNIQUE key. No ``ds_status`` filter is applied — the
attestation's own ``status='active'`` is the publish gate, matching precisely
what Django forwards to the live-delta.

Selection rules (per user, 2026-05-02):

* **Sources**: ``place_link`` + ``close_matches`` only. The legacy ES
  ``whg`` index ``relation.parent`` field is intentionally NOT consulted
  — assumed redundant with ``close_matches``.
* **CloseMatch basis**: ``reviewed``, ``authid``, **and** ``imported`` are
  all included. The bulk ``imported`` rows in whgv3beta represent a
  pre-approved import from another table; the user confirmed they should
  be treated as previously contributor-approved.
* **Dataset status**: only places belonging to "late-curation" datasets
  contribute (``ds_status`` ∈ ``indexed`` / ``accessioning`` /
  ``wd-complete`` — there are zero ``accessioned`` rows in whgv3beta;
  the equivalent v3 lifecycle states are these three). Earlier states
  (``reconciling``, ``uploaded``, ``remote``, ``format_ok``, ``updated``)
  and ``NULL`` are excluded as not-yet-curated. Override at runtime via
  ``WHG_PUBLISHABLE_DS_STATUSES`` (comma-separated env var) if needed.

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
from processing.staging_contract import HARD_LINK_RELATION_TYPES


# ---------------------------------------------------------------------------
# Source queries
# ---------------------------------------------------------------------------


# The ds_status values that count as "publishable" for hard-link harvest.
# whgv3beta has no rows in the canonical ``accessioned`` state — the live
# late-curation states are ``indexed`` / ``accessioning`` / ``wd-complete``.
# Override at runtime with the ``WHG_PUBLISHABLE_DS_STATUSES`` env var
# (comma-separated, e.g. ``indexed,accessioned``).
_DEFAULT_PUBLISHABLE_DS_STATUSES = ("indexed", "accessioning", "wd-complete")


def _publishable_ds_statuses() -> tuple[str, ...]:
    import os
    raw = os.getenv("WHG_PUBLISHABLE_DS_STATUSES")
    if not raw:
        return _DEFAULT_PUBLISHABLE_DS_STATUSES
    statuses = tuple(s.strip() for s in raw.split(",") if s.strip())
    return statuses or _DEFAULT_PUBLISHABLE_DS_STATUSES


# Bound at query time as a single $1 array parameter — asyncpg expands
# Python tuples/lists into PG arrays for ANY()/= ANY().
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
      AND d.ds_status = ANY($1::text[])
"""


# ``imported`` is included alongside ``reviewed`` / ``authid`` because the
# bulk ``imported`` rows in whgv3beta come from a pre-approved import from
# another table (per user, 2026-05-02). All three count as
# contributor-approved equivalences for clustering purposes.
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
    WHERE da.ds_status = ANY($1::text[])
      AND db.ds_status = ANY($1::text[])
      AND cm.basis IN ('reviewed', 'authid', 'imported')
      AND cm.created_by_id IS NOT NULL
"""


# Live contributor attestations (the new flow). No ``ds_status`` join and no
# publishable-status filter: the row's own ``status='active'`` is the publish
# gate, which is *exactly* the subset Django forwards to the gateway live-delta
# (``api/signals.py``). Reading the same predicate here guarantees the batch
# overlay is a superset of the live-delta's active rows, so the post-build prune
# never drops a row that isn't already folded in. ``place_a``/``place_b`` are
# stored already-namespaced and canonical-ordered (model CHECK constraint), so
# no id reconstruction is needed. ``legacy_v3_2`` is selected so ``source_id``
# can carry the same suffix ``ContributorAttestation.source_id()`` would emit.
_ACTIVE_ATTESTATION_QUERY = """
    SELECT
        ca.place_a       AS place_a,
        ca.place_b       AS place_b,
        ca.relation_type AS relation_type,
        ca.user_id       AS user_id,
        ca.asserted_at   AS asserted_at,
        ca.justification AS justification,
        ca.legacy_v3_2   AS legacy_v3_2
    FROM api_contributorattestation ca
    WHERE ca.status = 'active'
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


_BOGUS_IDENTIFIER_TAILS = frozenset({"none", "null", "undefined", "nan", ""})


def _looks_namespaced(s: str) -> bool:
    """``identifier`` strings stored on PlaceLink should be namespaced
    (e.g. ``wd:Q90``, ``tgn:7000874``). Reject obviously broken values:

    * Missing colon, or empty either side of the colon.
    * Sentinel tails like ``wd:None`` / ``wd:null`` (curator tools wrote a
      Python ``None``/JS ``null`` literal as the identifier).
    * Wikidata-namespaced IDs without the ``Q``/``P``/``L``/``M`` prefix
      — Wikidata entity IDs are always letter-prefixed; bare digits are a
      legacy data-entry bug from earlier curation tools.
    """
    if ":" not in s:
        return False
    ns, _, rest = s.partition(":")
    if not ns or not rest:
        return False
    if rest.lower() in _BOGUS_IDENTIFIER_TAILS:
        return False
    if ns == "wd" and not rest[0].isalpha():
        return False
    return True


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


def _build_live_source_id(user_id: Any, *, legacy: bool = False) -> str:
    """``source_id`` for a live ``api_contributorattestation`` row.

    Mirrors ``whg3 api/models.py::ContributorAttestation.source_id()`` exactly:
    ``contributor:<user_id>`` for a fresh row, with the ``:legacy_v3_2`` suffix
    only when the row carries the inherited-from-v3.2 flag. Matching that method
    byte-for-byte is what lets a row present in both the batch overlay and the
    gateway live-delta dedup on the ``(place_a, place_b, relation_type,
    source_id)`` UNIQUE key."""
    base = f"contributor:{user_id}"
    return f"{base}:legacy_v3_2" if legacy else base


def _attestation_to_hard_link(record: dict[str, Any]) -> dict[str, Any] | None:
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
    # The model constrains relation_type to the four canonical values, but guard
    # anyway so a bad direct DB write can't fail the whole batch insert.
    if relation_type not in HARD_LINK_RELATION_TYPES:
        return None
    pa, pb = _canonical_pair(place_a, place_b)
    justification = record.get("justification")
    return {
        "place_a": pa,
        "place_b": pb,
        "relation_type": relation_type,
        "source_category": "contributor",
        "source_id": _build_live_source_id(
            user_id, legacy=bool(record.get("legacy_v3_2"))),
        "asserted_at": _coerce_iso(record.get("asserted_at")),
        # Preserve the contributor's free-text justification verbatim (may be
        # None) so the overlay row matches what the live-delta stores.
        "justification": justification if justification else None,
    }


# ---------------------------------------------------------------------------
# Async fetch from DO PG via the existing SSH-tunnelled client
# ---------------------------------------------------------------------------


async def _fetch_all_async() -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
]:
    # Lazy import: pg_client pulls in asyncpg + sshtunnel which are heavy and
    # not needed for the LOC / staged harvesters.
    from clustering.pg_client import pg_connection  # noqa: WPS433

    statuses = list(_publishable_ds_statuses())
    async with pg_connection() as conn:
        place_link_rows = await conn.fetch(_PLACE_LINK_QUERY, statuses)
        close_match_rows = await conn.fetch(_CLOSE_MATCH_QUERY, statuses)
        attestation_rows = await conn.fetch(_ACTIVE_ATTESTATION_QUERY)
    return (
        [dict(r) for r in place_link_rows],
        [dict(r) for r in close_match_rows],
        [dict(r) for r in attestation_rows],
    )


def fetch_all() -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
]:
    """Synchronous façade — returns
    ``(place_link_rows, close_match_rows, attestation_rows)``."""
    return asyncio.run(_fetch_all_async())


def iter_hard_link_rows(
    place_link_rows: Iterable[dict[str, Any]],
    close_match_rows: Iterable[dict[str, Any]],
    attestation_rows: Iterable[dict[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Map raw PG rows into hard_link_assertions-shaped rows.

    Returns ``(rows, per_source_counts)`` where ``per_source_counts`` is a
    breakdown of converted rows per source for diagnostics. ``attestation_rows``
    defaults to empty so callers only interested in the legacy replay keep
    working unchanged.
    """
    out: list[dict[str, Any]] = []
    counts = {
        "place_link_input": 0,
        "place_link_converted": 0,
        "close_match_input": 0,
        "close_match_converted": 0,
        "attestation_input": 0,
        "attestation_converted": 0,
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
    for record in attestation_rows:
        counts["attestation_input"] += 1
        row = _attestation_to_hard_link(record)
        if row is not None:
            counts["attestation_converted"] += 1
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
    place_link_rows, close_match_rows, attestation_rows = fetch_all()
    rows, counts = iter_hard_link_rows(
        place_link_rows, close_match_rows, attestation_rows)
    with builder(db_path) as conn:
        stats = insert_rows(conn, rows, batch_size=batch_size)
    stats["db_path"] = str(db_path)
    stats["fetched_rows"] = len(rows)
    stats.update(counts)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Harvest contributor hard links from DO PG into the SQLite overlay: "
            "legacy v3.2 replay (place_link + close_matches) plus active "
            "api_contributorattestation rows (the live-forwarding flow)"
        )
    )
    parser.add_argument("--db-path", required=True, help="SQLite output path")
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch rows but do not insert; print summary only")
    args = parser.parse_args()

    if args.dry_run:
        try:
            place_link_rows, close_match_rows, attestation_rows = fetch_all()
        except ImportError as exc:
            print(
                f"ERROR: contributor replay needs asyncpg + sshtunnel installed: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        rows, counts = iter_hard_link_rows(
            place_link_rows, close_match_rows, attestation_rows)
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
