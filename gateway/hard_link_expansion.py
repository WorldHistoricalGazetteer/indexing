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

"Best-effort" now includes the failure that has no exception (place#241)
--------------------------------------------------------------------------
The original guard covered the failures that *raise*. A hard-mounted NFS
filesystem that wedges does not raise: ``Path.exists()`` and ``sqlite3.connect``
block in uninterruptible sleep and never return, so ``try/except`` never fired
and the request hung — 45 s+ for ``include_hard_links``, and for any
``contained_in`` scope whose container had no polygon and took the
``linked-polygon`` co-referent path through ``gateway/spatial.py``. Every
filesystem touch below therefore happens inside an :class:`IoGuard` deadline
(``gateway/bounded_io.py``), and the two stores hold **separate** guards so a
wedged batch overlay never disables a healthy live-delta.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from .bounded_io import IoGuard, Outcome
from .config import IX3_BASE
from .links import LIVE_DB_PATH

logger = logging.getLogger("gateway.hard_link_expansion")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The batch overlay the clustering pipeline publishes. Matches
# ``processing.settings.PITT_HARDLINK_DIR`` / ``PITT_HARDLINK_FILENAME``.
# Override via ``HARD_LINK_BATCH_DB``.
#
# ⚠️ Defaults to ``IX3_BASE`` (/vast flash), NOT ``IX1_BASE``, since place#241:
# ``/ix1`` is a hard NFS mount whose wedge took the serving path down with it,
# and nothing in the query path should depend on it. The publisher default
# (``processing.settings.PITT_HARDLINK_DIR``) moved in the same commit — reader
# and writer must be changed together or the gateway reads a file nobody
# updates. ``publish_local`` copies into the target dir and renames within it,
# so the build may still land anywhere.
BATCH_DB_PATH = Path(os.getenv(
    "HARD_LINK_BATCH_DB", f"{IX3_BASE}/hardlinks/hard_links.sqlite"))

# Wall-clock budget for one store read (open + query + close) and how long a
# store is skipped after it overruns. Per-store guards: a wedged overlay must
# not disable the live-delta, which is on different storage.
_IO_TIMEOUT_S = float(os.getenv("HARD_LINK_IO_TIMEOUT_S", "3.0"))
_IO_COOLDOWN_S = float(os.getenv("HARD_LINK_IO_COOLDOWN_S", "60.0"))

BATCH_GUARD = IoGuard("hard-link batch overlay",
                      timeout=_IO_TIMEOUT_S, cooldown=_IO_COOLDOWN_S)
LIVE_GUARD = IoGuard("hard-link live-delta",
                     timeout=_IO_TIMEOUT_S, cooldown=_IO_COOLDOWN_S)

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
    is best-effort enrichment).

    ⚠️ Both the ``exists()`` probe and the ``connect()`` can block forever on a
    wedged hard mount, so this must only ever be called from inside an
    :class:`~gateway.bounded_io.IoGuard` — see :func:`_read_store`."""
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


def _read_store(path: Path, ids: list[str]) -> list[tuple]:
    """Open, query and close one store — the whole filesystem interaction.

    Every touch of ``path`` lives in here, and nothing else touches it, so that
    wrapping this one call in a deadline bounds all of it. Splitting the open
    from the query would leave the ``exists()``/``connect()`` — precisely the
    calls that hang on a wedged mount — outside the bound.
    """
    conn = _connect_ro(path)
    if conn is None:
        return []
    try:
        return _query_touching(conn, ids)
    finally:
        conn.close()


def _read_store_bounded(guard: IoGuard, path: Path, ids: list[str]) -> Outcome:
    """:func:`_read_store` under ``guard``'s deadline; ``[]`` when it can't run."""
    return guard.run(_read_store, path, ids, default=[])


def store_degraded() -> bool:
    """True while either hard-link store is being skipped for hanging.

    Exposed so a caller can *say so* instead of reporting a store it could not
    read as a store that held nothing — the distinction place#241 is about.
    ``gateway/spatial.py`` uses it to qualify a failed-closed scope message.
    """
    return BATCH_GUARD.degraded or LIVE_GUARD.degraded


def store_stats() -> list[dict]:
    """Per-store guard counters (calls, timeouts, skips, stuck threads)."""
    return [BATCH_GUARD.stats(), LIVE_GUARD.stats()]


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
    deterministic, sorted list. Never raises **and never blocks indefinitely** —
    a store that errors, is missing, or hangs (place#241) yields fewer (or zero)
    edges within :data:`_IO_TIMEOUT_S`. Ask :func:`store_degraded` whether the
    empty answer means "no links" or "could not read".
    """
    id_set = {pid for pid in place_ids if pid}
    if not id_set:
        return []

    ids = list(id_set)
    # UNIQUE-key → row, deduping the two stores. Batch overlay scanned first.
    seen: dict[tuple, tuple] = {}
    for guard, path in ((BATCH_GUARD, BATCH_DB_PATH), (LIVE_GUARD, LIVE_DB_PATH)):
        for row in _read_store_bounded(guard, path, ids).value:
            key = (row[0], row[1], row[2], row[4])  # a, b, relation_type, source_id
            seen.setdefault(key, row)

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
