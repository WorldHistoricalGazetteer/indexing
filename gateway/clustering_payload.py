# gateway/clustering_payload.py
"""
Per-hit clustering *fuel* assembly for the WHG API gateway.

The browser-side scorer/clustering (whg3 `clustering.js`; see
`developer/plan-outstanding-2026-07.md` §1) computes every pair signal itself —
the gateway only *ships* the raw per-hit fields it needs. This module derives
those fields from a `places` index `_source`:

* ``h3`` — a single representative H3 cell (``geometries[].h3_centroid``) for the
  spatial-proximity / spatial synthetic-edge passes (``s.sp`` support).
* ``h3_cover`` — the union of the place's H3 cover cells (bounded), for coarse
  spatial-overlap tests.
* ``temporal_core`` — ``[latest_start, earliest_end]``, the attested span
* ``temporal_range`` — ``[min_start, max_end]`` derived from the geometries'
  ``timespans`` (``s.t`` interval-overlap support). Gateway-derived; no schema
  change.
* ``aat_ids`` — the leaf AAT concept ids (union of ``types[].aat_ids``).
* ``aat_paths`` — the materialised root→leaf AAT paths (union of
  ``types[].aat_paths``). These carry the **ancestor chain *and* depth**, which
  is exactly what a client-side Wu-Palmer similarity (``s.ty``) needs — so they
  subsume a flat "aat_ancestors" set (a flat set would lose depth/LCA).

``query_match{name, score}`` (which toponym produced the hit + its normalised
score) is assembled in the endpoints, not here — it comes from the discovery
step, not the place ``_source``.

All output is deterministic (sorted) so responses are stable across calls.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("gateway.clustering_payload")


# ---------------------------------------------------------------------------
# Offline calibration artefacts — shipped once per clustering query so the
# browser scorer has the composite weights + θ/τ thresholds + toponym stoplist.
# Produced by ``clustering.calibrate_params``; committed defaults live at
# ``clustering/data/`` (repo root = this file's grandparent). A gateway restart
# picks up a freshly calibrated file (paths overridable per-deployment).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
CLUSTERING_PARAMS_FILE = Path(os.getenv(
    "CLUSTERING_PARAMS_FILE", _REPO_ROOT / "clustering" / "data" / "clustering_params.json"))
TOPONYM_STOPLIST_FILE = Path(os.getenv(
    "TOPONYM_STOPLIST_FILE", _REPO_ROOT / "clustering" / "data" / "toponym_stoplist.json"))


@lru_cache(maxsize=1)
def load_clustering_params() -> dict | None:
    """The calibration params (weights + θ/τ thresholds), or None if absent.

    Cached for the process lifetime — restart the gateway to pick up a
    recalibrated file (matches the deploy model)."""
    try:
        return json.loads(CLUSTERING_PARAMS_FILE.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("clustering_params unavailable (%s): %s",
                       CLUSTERING_PARAMS_FILE, exc)
        return None


@lru_cache(maxsize=1)
def load_toponym_stoplist() -> list[str]:
    """The high-frequency toponym stoplist (empty if the file isn't built yet).

    The stoplist file is produced by ``calibrate_params --stoplist`` (needs an ES
    run) and is *not* committed, so this is empty until that run ships it."""
    try:
        data = json.loads(TOPONYM_STOPLIST_FILE.read_text())
        return data.get("stoplist", []) if isinstance(data, dict) else list(data)
    except (OSError, ValueError):
        return []


# Cap on the per-hit ``h3_cover`` union. A large polygon can cover many cells;
# 200 hits × unbounded cover would bloat the payload. The representative ``h3``
# centroid is the primary spatial signal — ``h3_cover`` is a secondary
# overlap aid — so a modest cap is acceptable. Truncation is logged, not silent.
_MAX_H3_COVER = 128

# The extra ``_source`` fields the assembler needs, beyond the default hit set.
# Endpoints add these to ``build_places_filter`` when clustering fuel is
# requested (``types`` is top-level; the rest are nested under ``geometries``).
CLUSTERING_SOURCE_FIELDS = (
    "types",
    "geometries.h3_centroid",
    "geometries.h3_cover",
    "geometries.timespans",
)


def _timespan_bound(endpoint: dict, qualifiers: tuple[str, ...]) -> int | None:
    """First integer year present among ``qualifiers``, in preference order.

    Each endpoint carries up to three sub-fields — ``in`` (exact), ``earliest``
    and ``latest``. Reading only ``in``, as this did until place#169, empties
    the temporal support of every source re-encoded under place#164, which
    records attestations with the outer bounds and no ``in`` at all.
    """
    if not isinstance(endpoint, dict):
        return None
    for q in qualifiers:
        v = endpoint.get(q)
        if isinstance(v, int):
            return v
    return None


def _temporal_core(geometries: list[dict]) -> list[int] | None:
    """``[latest_start, earliest_end]`` — the span the record is *attested* alive
    throughout — or None where no timespan pins both bounds.

    The counterpart to ``_temporal_range``'s envelope. The Atlas needs both to
    mirror the gateway's two query modes client-side (place#169): filtering a
    loaded result set on the envelope while the server filters on the core would
    show hits the next server response then removes.

    Where several timespans each pin a core, this takes the widest they span.
    That is deliberately the permissive choice for a *client-side preview*: the
    server remains authoritative, and a preview that keeps a borderline hit reads
    better than one that flickers it away and back.
    """
    starts: list[int] = []
    ends: list[int] = []
    for g in geometries:
        if not isinstance(g, dict):
            continue
        for ts in g.get("timespans", []) or []:
            if not isinstance(ts, dict):
                continue
            start = _timespan_bound(ts.get("start") or {}, ("latest", "in"))
            end = _timespan_bound(ts.get("end") or {}, ("earliest", "in"))
            if start is not None and end is not None:
                starts.append(start)
                ends.append(end)
    if not starts:
        return None
    return [min(starts), max(ends)]


def _temporal_range(geometries: list[dict]) -> list[int | None] | None:
    """``[earliest_start, latest_end]`` across all geometries' ``timespans``, with
    **None for a side no timespan bounds at all**; None when nothing is dated.

    The widest span the bounds allow. Interval-overlap support (``s.t``) is a
    *could these be the same place* test, so the permissive reading is right — a
    narrower core would refuse to cluster an attestation with the lifespan it
    belongs to.

    An absent bound is genuinely unbounded and must stay None rather than
    borrowing the other end (place#169). Collapsing an open-start county to
    ``[1974, 1974]`` claims it began in 1974, which excluded it from any earlier
    window in the Atlas's client-side preview — the exact over-claim place#164
    removed from storage. Consumers treat None as unbounded: see
    ``clustering.temporal_overlap`` and its JS twin in ``whg3 clustering.js``.
    """
    starts: list[int] = []
    ends: list[int] = []
    for g in geometries:
        if not isinstance(g, dict):
            continue
        for ts in g.get("timespans", []) or []:
            if not isinstance(ts, dict):
                continue
            start = _timespan_bound(ts.get("start") or {}, ("earliest", "in", "latest"))
            end = _timespan_bound(ts.get("end") or {}, ("latest", "in", "earliest"))
            if start is not None:
                starts.append(start)
            if end is not None:
                ends.append(end)
    if not starts and not ends:
        return None
    return [min(starts) if starts else None, max(ends) if ends else None]


def assemble_clustering_fields(src: dict) -> dict:
    """Derive the per-hit clustering fuel from a place ``_source``.

    Returns a dict with ``h3``, ``h3_cover``, ``temporal_range``, ``aat_ids``,
    ``aat_paths`` (see module docstring). Never raises — malformed sub-docs are
    skipped."""
    geometries = [g for g in (src.get("geometries") or []) if isinstance(g, dict)]

    # h3 centroid — first geometry that has one is the representative cell.
    h3 = None
    for g in geometries:
        cell = g.get("h3_centroid")
        if cell:
            h3 = cell
            break

    # h3_cover — dedup + sorted union across geometries, bounded.
    cover: set[str] = set()
    for g in geometries:
        cells = g.get("h3_cover")
        if isinstance(cells, list):
            cover.update(c for c in cells if c)
        elif cells:
            cover.add(cells)
    h3_cover = sorted(cover)
    if len(h3_cover) > _MAX_H3_COVER:
        logger.info("h3_cover truncated for %s: %d -> %d cells",
                    src.get("place_id", "?"), len(h3_cover), _MAX_H3_COVER)
        h3_cover = h3_cover[:_MAX_H3_COVER]

    # AAT leaves + materialised paths, union across the nested types.
    aat_ids: set[int] = set()
    aat_paths: set[str] = set()
    for t in src.get("types") or []:
        if not isinstance(t, dict):
            continue
        for aid in t.get("aat_ids") or []:
            if isinstance(aid, int):
                aat_ids.add(aid)
        for path in t.get("aat_paths") or []:
            if path:
                aat_paths.add(path)

    return {
        "h3": h3,
        "h3_cover": h3_cover,
        "temporal_range": _temporal_range(geometries),
        "temporal_core": _temporal_core(geometries),
        "aat_ids": sorted(aat_ids),
        "aat_paths": sorted(aat_paths),
    }
