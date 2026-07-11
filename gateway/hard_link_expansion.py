# gateway/hard_link_expansion.py
"""
Result-set hard-link expansion for the WHG API gateway.

This is the *read* counterpart of ``gateway/links.py`` (the ``/api/links``
receiver). Per search/reconcile query it looks up the co-reference
(``sameAs`` / ``exactMatch`` / ``closeMatch`` / ``distinct``) assertions that
touch the result-set place_ids and ships them to the client as edges. The
browser (whg3 ``clustering.js``) consumes these as the ``s.l`` hard-link signal
of its client-side scorer + Union-Find — see
``developer/plan-outstanding-2026-07.md`` §1 ("Hard-link expansion + ship").

Union of two stores
--------------------
Hard-link assertions live in **two** SQLite files with the *same* schema
(``processing.staging_contract.HARD_LINK_SQLITE_SCHEMA``):

* the **batch overlay** — ``clustering.sqlite_overlay.ship_to_pitt`` rsyncs a
  freshly built DB then atomically ``mv``s it over the live file. Read-only from
  the gateway's side. Holds authority + LOC-transitive + contributor-replay
  links folded in at build time.
* the **live-delta** — ``gateway/links.py`` writes each freshly-forwarded
  contributor assertion here (a *second* file the batch swap never touches).

Reading the **union** (deduped by the overlay UNIQUE key
``(place_a, place_b, relation_type, source_id)``) is exactly what gives
``POST /api/links`` a real-time reconcile effect: a contributor link asserted a
moment ago is visible here before the next batch re-cluster folds it into the
overlay (and the live-delta is pruned — Ticket A). This is the consumption side
that ``developer/handoff-hardlink-live-delta-followups.md`` "Ticket B" folded
into plan §1.

Both files are opened **read-only, best-effort**: a missing / mid-swap / locked
file is skipped, never fatal — hard-link edges are additive enrichment, exactly
like the (soon-retired) cluster lookup.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from .config import IX1_BASE
from .links import LIVE_DB_PATH

logger = logging.getLogger("gateway.hard_link_expansion")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The batch overlay the clustering pipeline ships to Pitt. Matches
# ``processing.settings.PITT_HARDLINK_DIR`` / ``PITT_HARDLINK_FILENAME``
# (``/ix1/ishi/hardlinks/hard_links.sqlite``). Override via ``HARD_LINK_BATCH_DB``.
BATCH_DB_PATH = Path(os.getenv(
    "HARD_LINK_BATCH_DB", f"{IX1_BASE}/hardlinks/hard_links.sqlite"))

# SQLite bind-variable safety: keep each IN() list well under the 999-variable
# floor of older SQLite builds. A chunk binds 2×_ID_CHUNK vars (place_a IN … OR
# place_b IN …), so 400 → 800 vars/chunk.
_ID_CHUNK = 400

# Cap on 1-hop expansion edges (exactly one endpoint in the result set). In-set
# edges (both endpoints in the result) are always returned in full; only the
# outward expansion is bounded — and truncation is logged, never silent.
_DEFAULT_MAX_ONE_HOP = 2000

# Column order matches the overlay schema; index 4 is source_id (the UNIQUE-key
# tiebreaker), index 3 is source_category (unused in the emitted edge).
_SELECT_COLS = "place_a, place_b, relation_type, source_category, source_id"


class HardLinkEdge(BaseModel):
    """One hard-link assertion emitted to the client.

    ``a`` / ``b`` are canonically ordered (``a < b``, enforced by the overlay
    CHECK constraint). ``source`` is the assertion's ``source_id`` (e.g.
    ``"wikidata"``, ``"loc"``, ``"contributor:42"``). ``via_hard_link`` marks
    the provenance so the browser scorer treats it as an authoritative link
    signal rather than an inferred one.
    """
    a: str
    b: str
    relation_type: str
    source: str
    via_hard_link: bool = True


# ---------------------------------------------------------------------------
# SQLite access (read-only, WAL-safe, best-effort)
# ---------------------------------------------------------------------------


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    """Open a hard-link SQLite read-only. Returns None if absent/unreadable.

    Uses a ``file:…?mode=ro`` URI so a WAL reader coexists with the live-delta's
    writer (``gateway/links.py``) and never creates the file. Any error — missing
    file, mid-atomic-swap, permissions — is logged and skipped (the edge payload
    is best-effort enrichment)."""
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn
    except sqlite3.Error as exc:
        logger.warning("hard-link overlay open failed (%s): %s", path, exc)
        return None


def _query_touching(conn: sqlite3.Connection, ids: list[str]) -> list[tuple]:
    """All assertions with either endpoint in ``ids`` (chunked for the var limit)."""
    rows: list[tuple] = []
    for i in range(0, len(ids), _ID_CHUNK):
        chunk = ids[i:i + _ID_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        sql = (
            f"SELECT {_SELECT_COLS} FROM hard_link_assertions "
            f"WHERE place_a IN ({placeholders}) OR place_b IN ({placeholders})"
        )
        try:
            rows.extend(conn.execute(sql, chunk + chunk).fetchall())
        except sqlite3.Error as exc:  # e.g. table absent on a half-built file
            logger.warning("hard-link query failed (%s): %s", conn, exc)
            break
    return rows


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def expand_hard_links(
    place_ids: list[str],
    *,
    one_hop: bool = True,
    max_one_hop: int = _DEFAULT_MAX_ONE_HOP,
) -> list[HardLinkEdge]:
    """Return hard-link edges for a result set (union batch overlay + live-delta).

    Args:
        place_ids: the result-set place_ids (the hits being shipped).
        one_hop: also include edges with exactly one endpoint in the result set
            (a bounded outward expansion — the neighbour place is *not* in the
            result but the browser may still want the assertion). ``False``
            returns only edges wholly within the result set.
        max_one_hop: cap on the number of 1-hop edges (in-set edges are never
            capped). Truncation is logged.

    Edges are deduped across both stores by the overlay UNIQUE key
    ``(place_a, place_b, relation_type, source_id)``; the batch overlay wins ties
    (it is scanned first, ``setdefault`` keeps the first-seen row). Returns a
    deterministic, sorted list. Never raises — any store error yields fewer (or
    zero) edges.
    """
    id_set = {pid for pid in place_ids if pid}
    if not id_set:
        return []

    ids = list(id_set)
    # UNIQUE-key → row, deduping the two stores. Batch overlay scanned first.
    seen: dict[tuple, tuple] = {}
    for path in (BATCH_DB_PATH, LIVE_DB_PATH):
        conn = _connect_ro(path)
        if conn is None:
            continue
        try:
            for row in _query_touching(conn, ids):
                key = (row[0], row[1], row[2], row[4])  # a, b, relation_type, source_id
                seen.setdefault(key, row)
        finally:
            conn.close()

    in_set: list[HardLinkEdge] = []
    one_hop_edges: list[HardLinkEdge] = []
    for place_a, place_b, relation_type, _source_category, source_id in seen.values():
        edge = HardLinkEdge(
            a=place_a, b=place_b, relation_type=relation_type, source=source_id)
        if place_a in id_set and place_b in id_set:
            in_set.append(edge)
        elif one_hop:
            one_hop_edges.append(edge)

    _sort_key = lambda e: (e.a, e.b, e.relation_type, e.source)
    in_set.sort(key=_sort_key)
    one_hop_edges.sort(key=_sort_key)

    if len(one_hop_edges) > max_one_hop:
        logger.info(
            "hard-link 1-hop expansion truncated: %d -> %d edges (result set %d)",
            len(one_hop_edges), max_one_hop, len(id_set))
        one_hop_edges = one_hop_edges[:max_one_hop]

    return in_set + one_hop_edges
