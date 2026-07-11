# gateway/links.py
"""
Contributor hard-link receiver for the WHG API gateway.

Implements ``POST`` / ``DELETE /api/links`` — the CRC-side counterpart of the
WHG website's ``whg3/api/crc_client.py`` (``crc_post_link`` / ``crc_delete_link``,
wired via ``whg3/api/signals.py``). The website forwards every **active**
``ContributorAttestation`` create/revoke here; this receiver lands it in a
Pitt-side "live-delta" SQLite that mirrors the batch hard-link overlay's schema.

Why a *separate* live-delta DB (not the batch overlay)
------------------------------------------------------
``clustering.sqlite_overlay.ship_to_pitt`` atomically ``mv``s a freshly built
batch DB over the live file, and the gateway opens the batch overlay **read-only**
(``processing.staging_contract``: "the gateway opens it read-only"). A live writer
cannot share that file — its rows would be clobbered on the next batch swap, and
read-write against an atomically-replaced inode is unsafe. So live writes go to a
**second** file with the *same schema* that the batch swap never touches
(handoff option A). Consumers read the UNION of the batch overlay + the live-delta;
on each batch run the "new flow" folds active attestations into the fresh batch
build and the live-delta is pruned (a separate follow-up ticket — see the handoff
and ``clustering/harvest/contributor_replay.py``).

Consumption / semantics (decided 2026-07-11)
--------------------------------------------
Nothing at *reconcile time* currently reads ``hard_link_assertions`` — the batch
clustering phase is today's only consumer. So a live ``/api/links`` write gives
**durability + faster next-rebuild inclusion**, not yet real-time reconcile effect.
A live reconcile-time hard-link lookup (union of batch + live-delta) is a separate
feature to build if real-time is required. The Django side already treats these
calls as best-effort, so this matches the existing contract.

Auth (decided 2026-07-11)
-------------------------
None — matches ``gateway/reingest.py``: the Pitt firewall whitelists the DO
app-server IP. Django may send a bearer (``CRC_GATEWAY_API_KEY``); we ignore it,
exactly like reingest.

The insert contract (column order, ``INSERT OR IGNORE``, the schema) is reused
verbatim from ``clustering.sqlite_overlay`` / ``processing.staging_contract`` so it
cannot drift from the batch builder.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# Reuse the canonical contract — do NOT re-derive it here (handoff §"target schema").
from processing.staging_contract import validate_hard_link_row
from clustering.sqlite_overlay import _INSERT_SQL, _row_tuple, initialise_schema

from .config import IX3_BASE

logger = logging.getLogger("gateway.links")

router = APIRouter(prefix="/api", tags=["Links"])


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The live-delta DB — a SECOND SQLite the batch ``ship_to_pitt`` swap never
# touches. Defaults to fast /vast flash (``IX3_BASE``); the batch overlay lives
# on /ix1, so this is a distinct file the batch swap never rewrites. The gateway
# runs as ``gazetteer`` (group ``ishi``) and ``/vast/ishi`` is group-writable,
# so ``_connect`` can create the dir + file on first use. Override per-deployment
# via ``HARD_LINK_LIVE_DB``.
LIVE_DB_PATH = Path(os.getenv(
    "HARD_LINK_LIVE_DB", f"{IX3_BASE}/hardlinks/hard_links_live.sqlite"))

# SQLite busy timeout for the (low-volume, one-per-contributor-action) writes.
_BUSY_TIMEOUT_MS = int(os.getenv("HARD_LINK_LIVE_BUSY_TIMEOUT_MS", "5000"))

# These rows are always contributor-sourced (the receiver only handles the
# contributor forward path); authority links are harvested in batch, not here.
_SOURCE_CATEGORY = "contributor"

_DELETE_SQL = (
    "DELETE FROM hard_link_assertions "
    "WHERE place_a = ? AND place_b = ? AND relation_type = ? AND source_id = ?"
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LinkRow(BaseModel):
    place_a: str = Field(..., min_length=1, max_length=128,
                         description="Canonical-ordered: place_a < place_b")
    place_b: str = Field(..., min_length=1, max_length=128)
    relation_type: str = Field(..., description="sameAs | exactMatch | closeMatch | distinct")
    # Django sends "contributor"; we force it below regardless.
    source_category: Optional[str] = Field("contributor")
    source_id: str = Field(..., min_length=1, max_length=128,
                           description="contributor:<user_id>")
    asserted_at: Optional[str] = None
    justification: Optional[str] = None


class LinkKey(BaseModel):
    """Identifying fields for a revoke — the overlay's UNIQUE key."""
    place_a: str = Field(..., min_length=1, max_length=128)
    place_b: str = Field(..., min_length=1, max_length=128)
    relation_type: str = Field(..., min_length=1, max_length=16)
    source_id: str = Field(..., min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# DB access (live-delta; read-write, WAL)
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    """Open the live-delta DB read-write (WAL + busy timeout), creating the file,
    parent dir, and schema on first use. Caller closes. Opened, used, and closed
    within a single worker thread, so ``check_same_thread`` is satisfied.

    The DB (and its ``-wal`` / ``-shm`` sidecars) are created **group-writable**
    so the batch job — which runs as a different ``ishi``-group user (e.g.
    ``stg135``) — can prune the live-delta after folding its active rows into a
    fresh overlay (``clustering.sqlite_overlay.prune_live_delta``; Ticket A in
    ``developer/handoff-hardlink-live-delta-followups.md``). Without this the
    prune can read but not DELETE, and the live-delta grows unbounded."""
    # Force a group-writable creation mask for the parent dir + DB + WAL/SHM
    # sidecars, all of which may be created below. The group-writable dir lets a
    # different ishi-group user (the batch prune) recreate the WAL sidecars if
    # they're ever absent. Restored immediately.
    prev_umask = os.umask(0o002)
    try:
        LIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(LIVE_DB_PATH), isolation_level=None,  # autocommit — one row per call
            timeout=_BUSY_TIMEOUT_MS / 1000.0,
        )
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS};")
        initialise_schema(conn)  # idempotent CREATE (tolerates "already exists")
    finally:
        os.umask(prev_umask)
    _ensure_group_writable()
    return conn


def _ensure_group_writable() -> None:
    """Best-effort ``g+w`` on the live-delta + its WAL/SHM sidecars.

    ``umask`` only governs files created *while it is set*; a file that already
    existed (created before this fix shipped, or under a stricter mask) keeps
    its old mode. Re-assert the mode each connect so an existing ``-rw-r--r--``
    live-delta becomes group-writable on the next gateway write. Silently skips
    files we don't own (only the creator can ``chmod``)."""
    targets = [LIVE_DB_PATH.parent] + [
        Path(f"{LIVE_DB_PATH}{suffix}") for suffix in ("", "-wal", "-shm")
    ]
    for p in targets:
        try:
            if p.exists():
                mode = p.stat().st_mode & 0o777
                if not mode & 0o020:  # group-write bit missing
                    os.chmod(p, mode | 0o020)
        except OSError:
            pass


def _insert_row(row: dict) -> bool:
    """INSERT OR IGNORE one validated row. Returns True if a new row was inserted,
    False if it already existed (idempotent duplicate)."""
    conn = _connect()
    try:
        cur = conn.execute(_INSERT_SQL, _row_tuple(row))
        return cur.rowcount == 1
    finally:
        conn.close()


def _delete_row(place_a: str, place_b: str, relation_type: str, source_id: str) -> int:
    """DELETE by the UNIQUE key. Returns the number of rows removed (0 or 1)."""
    conn = _connect()
    try:
        cur = conn.execute(_DELETE_SQL, (place_a, place_b, relation_type, source_id))
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/links", status_code=201,
             responses={422: {"description": "Invalid hard-link row"}})
async def post_link(body: LinkRow) -> dict:
    """Create/publish one contributor hard-link assertion (idempotent).

    Validates against the canonical contract (``validate_hard_link_row``:
    required fields, ``relation_type`` enum, canonical ``place_a < place_b``
    ordering), then ``INSERT OR IGNORE`` into the live-delta. Returns
    ``201`` with ``{"inserted": bool}`` — ``false`` on a duplicate.
    """
    row = {
        "place_a": body.place_a,
        "place_b": body.place_b,
        "relation_type": body.relation_type,
        "source_category": _SOURCE_CATEGORY,  # always contributor here
        "source_id": body.source_id,
        "asserted_at": body.asserted_at,
        "justification": body.justification,
    }
    try:
        validate_hard_link_row(row)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    inserted = await asyncio.to_thread(_insert_row, row)
    logger.info("link %s %s %s..%s inserted=%s",
                row["relation_type"], row["source_id"],
                row["place_a"], row["place_b"], inserted)
    return {"inserted": inserted}


@router.delete("/links", status_code=200)
async def delete_link(body: LinkKey) -> dict:
    """Revoke one contributor hard-link assertion by its UNIQUE key.

    Idempotent — deleting a row that is absent (or that only exists in the
    read-only batch overlay, which cannot be honoured live) returns
    ``{"deleted": 0}`` with a 2xx, matching Django's best-effort contract.
    """
    deleted = await asyncio.to_thread(
        _delete_row, body.place_a, body.place_b, body.relation_type, body.source_id)
    logger.info("link revoke %s %s %s..%s deleted=%s",
                body.relation_type, body.source_id,
                body.place_a, body.place_b, deleted)
    return {"deleted": deleted}
