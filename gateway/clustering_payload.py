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

import logging

logger = logging.getLogger("gateway.clustering_payload")


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


def _temporal_range(geometries: list[dict]) -> list[int] | None:
    """``[min_start, max_end]`` across all geometries' ``timespans``; None if none.

    Reads ``timespans[].start.in`` / ``timespans[].end.in`` (integer years).
    A missing start/end simply doesn't contribute a bound; if neither bound is
    ever seen the range is None (undated)."""
    starts: list[int] = []
    ends: list[int] = []
    for g in geometries:
        if not isinstance(g, dict):
            continue
        for ts in g.get("timespans", []) or []:
            if not isinstance(ts, dict):
                continue
            start = (ts.get("start") or {}).get("in")
            end = (ts.get("end") or {}).get("in")
            if isinstance(start, int):
                starts.append(start)
            if isinstance(end, int):
                ends.append(end)
    if not starts and not ends:
        return None
    lo = min(starts) if starts else min(ends)
    hi = max(ends) if ends else max(starts)
    return [lo, hi]


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
        "aat_ids": sorted(aat_ids),
        "aat_paths": sorted(aat_paths),
    }
