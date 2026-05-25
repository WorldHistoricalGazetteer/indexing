# gateway/spatial.py
"""
Spatial-containment engine for ``/api/search`` and ``/api/reconcile``.

Resolves a *containment region* from a set of place_ids (or raw GeoJSON) and
filters candidate place hits by **fuzzy** (H3, cheap, tolerant) or **exact**
(Shapely) containment — with **no Elasticsearch reindex**, using only fields
that are already indexed:

* ``geometries.repr_point`` — geo_point, queryable. Computed via Shapely
  ``representative_point()`` (``processing.helpers.enrich_geometry``), so it is
  **guaranteed to lie within the place geometry**. Hence ``repr_point ∈ R``
  implies the geometry intersects R — no Shapely needed for ``intersects``.
* ``geometries.h3_cover`` — compacted, mixed-resolution H3 cell set (keyword)
  covering the geometry's extent. Used both for the ES recall clause and the
  fuzzy areal test.
* ``geometries.geom`` — full GeoJSON, retrievable from ``_source`` (not
  geo_shape-queryable), parsed with Shapely for the exact refine.

The region itself is built from the *per-place* geometry of the supplied
place_ids — independent of the gazetteer-level ``h3_coverage`` "global"
sentinel — so polygons from any gazetteer (osm/ohm/wd/gn/po/tgn included) are
valid containment regions.

The two-pass algorithm (per the design): a cheap H3/centroid gate first, then
the expensive geometry test only on survivors.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .config import ES_BACKEND, PLACES_INDEX
from .es_helpers import ES_HEADERS, extract_place_geoms, extract_repr_point

logger = logging.getLogger("gateway.spatial")

try:
    import h3 as _h3
    _H3_AVAILABLE = True
except Exception:  # pragma: no cover
    _h3 = None
    _H3_AVAILABLE = False

try:
    from shapely.geometry import shape as _shape, Point as _Point, mapping as _mapping
    from shapely.ops import unary_union as _unary_union
    from shapely.prepared import prep as _prep
    _SHAPELY_AVAILABLE = True
except Exception:  # pragma: no cover
    _SHAPELY_AVAILABLE = False


# H3 polyfill controls — kept local (mirrors processing.helpers) so the gateway
# stays import-light and free of the pyproj/Slurm pulls in processing.*.
H3_POLYFILL_MAX_CELLS = 10_000
H3_CENTROID_RESOLUTION = 7
# Approx H3 cell area in deg² (equator-equivalent) for picking a start res that
# won't overflow the cap on continent-scale regions.
_H3_HEX_AREA_DEG2 = {
    0: 38000.0, 1: 5400.0, 2: 770.0, 3: 110.0,
    4: 16.0, 5: 2.2, 6: 0.31, 7: 0.045, 8: 0.0064,
}
# Cap on the size of the ES ``h3_cover`` terms clause (region cells + ancestors).
_ES_H3_TERMS_CAP = 4000
# Region cache size (resolved regions are reused across requests — the Atlas UI
# filters by the same country repeatedly).
_REGION_CACHE_MAX = 128


class RegionError(ValueError):
    """A containment region could not be built (e.g. no usable geometry)."""


@dataclass
class ResolvedRegion:
    """A resolved containment region, reusable across all candidates."""
    union: Any                       # shapely geometry (the region R)
    prepared: Any                    # prep(union) — reused per candidate
    cover_by_res: dict[int, set[str]]  # compacted region cover, grouped by res
    resolutions: tuple[int, ...]     # sorted resolutions present in cover
    bbox_geojson: dict               # envelope of R as GeoJSON (cheap ES gate)
    h3_terms: list[str]              # region cells + ancestors for ES recall

    @property
    def has_area(self) -> bool:
        return getattr(self.union, "area", 0.0) > 0.0


# ---------------------------------------------------------------------------
# H3 helpers (local, pure)
# ---------------------------------------------------------------------------

def _bbox_area_deg2(geom) -> float:
    try:
        minx, miny, maxx, maxy = geom.bounds
    except Exception:
        return 0.0
    return max(0.0, (maxx - minx) * (maxy - miny))


def _pick_polyfill_resolution(bbox_area_deg2: float) -> int:
    for res in (H3_CENTROID_RESOLUTION, 5, 3):
        if bbox_area_deg2 / max(_H3_HEX_AREA_DEG2.get(res, 0.045), 1e-12) <= H3_POLYFILL_MAX_CELLS:
            return res
    return 3


def _polyfill(geom) -> set[str]:
    """Polyfill a Shapely (multi)polygon → single-resolution H3 cell set."""
    if not _H3_AVAILABLE:
        return set()
    try:
        shape_obj = _h3.geo_to_h3shape(_mapping(geom))
    except Exception:
        return set()
    start = _pick_polyfill_resolution(_bbox_area_deg2(geom))
    tried: list[int] = []
    for res in (start, 5, 3):
        if res in tried:
            continue
        tried.append(res)
        try:
            cells = _h3.h3shape_to_cells(shape_obj, res)
            if len(cells) <= H3_POLYFILL_MAX_CELLS:
                return set(cells)
        except Exception:
            continue
    return set()


def _normalise_cells_to_resolution(cells: Iterable[str], target_res: int) -> set[str]:
    """Walk H3 cells to ``target_res`` (parent if finer, children if coarser).

    Ported from ``processing.ccode_enrichment._normalise_cells_to_resolution``.
    """
    if not _H3_AVAILABLE:
        return set()
    out: set[str] = set()
    for cell in cells:
        if not isinstance(cell, str) or not cell:
            continue
        try:
            res = _h3.get_resolution(cell)
        except Exception:
            continue
        if res == target_res:
            out.add(cell)
        elif res > target_res:
            try:
                out.add(_h3.cell_to_parent(cell, target_res))
            except Exception:
                continue
        else:
            try:
                out.update(_h3.cell_to_children(cell, target_res))
            except Exception:
                continue
    return out


def _build_cover_by_res(geom) -> dict[int, set[str]]:
    """Build a compacted, multi-resolution region cover grouped by resolution."""
    if not _H3_AVAILABLE:
        return {}
    # Degenerate (point/line) region → represent by representative-point cells.
    if getattr(geom, "area", 0.0) <= 0.0:
        cells: set[str] = set()
        parts = list(getattr(geom, "geoms", [geom]))
        for part in parts:
            try:
                rp = part.representative_point()
                cells.add(_h3.latlng_to_cell(rp.y, rp.x, H3_CENTROID_RESOLUTION))
            except Exception:
                continue
        return {H3_CENTROID_RESOLUTION: cells} if cells else {}

    raw = _polyfill(geom)
    if not raw:
        return {}
    try:
        compacted = list(_h3.compact_cells(list(raw)))
    except Exception:
        compacted = list(raw)
    by_res: dict[int, set[str]] = {}
    for cell in compacted:
        try:
            res = _h3.get_resolution(cell)
        except Exception:
            continue
        by_res.setdefault(res, set()).add(cell)
    return by_res


def _es_h3_terms(cover_by_res: dict[int, set[str]]) -> list[str]:
    """Region cover cells + all their ancestors, for the ES ``h3_cover`` terms
    recall clause. Matching a candidate's compacted cover against this set
    catches large candidates whose cover cell is an ancestor-or-equal of a
    region cell (i.e. they span the region) even when their repr_point lies
    outside R. Capped to keep the terms query bounded."""
    if not _H3_AVAILABLE:
        return []
    terms: set[str] = set()
    for res, cells in cover_by_res.items():
        for cell in cells:
            terms.add(cell)
            for parent_res in range(res - 1, -1, -1):
                try:
                    terms.add(_h3.cell_to_parent(cell, parent_res))
                except Exception:
                    break
            if len(terms) > _ES_H3_TERMS_CAP:
                # Too large — fall back to just the cover cells (no ancestors).
                flat: set[str] = set()
                for cs in cover_by_res.values():
                    flat.update(cs)
                return list(flat)[:_ES_H3_TERMS_CAP]
    return list(terms)


# ---------------------------------------------------------------------------
# Region construction
# ---------------------------------------------------------------------------

def _region_from_union(union) -> ResolvedRegion:
    cover = _build_cover_by_res(union)
    minx, miny, maxx, maxy = union.bounds
    bbox_geojson = {
        "type": "Polygon",
        "coordinates": [[
            [minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny],
        ]],
    }
    return ResolvedRegion(
        union=union,
        prepared=_prep(union),
        cover_by_res=cover,
        resolutions=tuple(sorted(cover.keys())),
        bbox_geojson=bbox_geojson,
        h3_terms=_es_h3_terms(cover),
    )


def _union_from_geoms(geojson_geoms: list[dict]):
    shapes = []
    for gj in geojson_geoms:
        try:
            shp = _shape(gj)
            if shp is None or shp.is_empty:
                continue
            if not shp.is_valid:
                shp = shp.buffer(0)
            if shp.is_empty:
                continue
            shapes.append(shp)
        except Exception:
            continue
    if not shapes:
        return None
    try:
        return _unary_union(shapes)
    except Exception:
        return shapes[0]


# --- region cache (in-process, size-capped) ---
_region_cache: "OrderedDict[str, ResolvedRegion]" = OrderedDict()


def _cache_get(key: str) -> Optional[ResolvedRegion]:
    region = _region_cache.get(key)
    if region is not None:
        _region_cache.move_to_end(key)
    return region


def _cache_put(key: str, region: ResolvedRegion) -> None:
    _region_cache[key] = region
    _region_cache.move_to_end(key)
    while len(_region_cache) > _REGION_CACHE_MAX:
        _region_cache.popitem(last=False)


def region_from_geojson(bounds: dict) -> Optional[ResolvedRegion]:
    """Build a region from a raw GeoJSON geometry (the ``bounds`` param), so
    raw-geometry callers get the same fuzzy/exact engine."""
    if not _SHAPELY_AVAILABLE or not isinstance(bounds, dict):
        return None
    key = "gj:" + hashlib.sha1(
        json.dumps(bounds, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        return cached

    geoms: list[dict] = []
    if bounds.get("type") == "GeometryCollection":
        geoms = [g for g in bounds.get("geometries", []) if isinstance(g, dict)]
    elif bounds.get("type") and bounds.get("coordinates"):
        geoms = [bounds]
    union = _union_from_geoms(geoms)
    if union is None or union.is_empty:
        return None
    region = _region_from_union(union)
    _cache_put(key, region)
    return region


async def resolve_region(place_ids: list[str], client, auth) -> ResolvedRegion:
    """Resolve a region from a set of place_ids by fetching their per-place
    geometry from ES ``_source`` and unioning it.

    Raises ``RegionError`` when none of the places carry usable geometry.
    """
    if not _SHAPELY_AVAILABLE:
        raise RegionError("shapely unavailable in the gateway")
    ids = sorted({p for p in (place_ids or []) if p})
    if not ids:
        raise RegionError("contained_in is empty")
    key = "ids:" + "|".join(ids)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    body = {
        "size": len(ids),
        "query": {"terms": {"place_id": ids}},
        "_source": [
            "place_id",
            "geometries.geom",
            "geometries.repr_point",
            "geometries.location",
            "geometries.h3_cover",
        ],
    }
    resp = await client.post(
        f"{ES_BACKEND}/{PLACES_INDEX}/_search",
        json=body, auth=auth, headers=ES_HEADERS,
    )
    resp.raise_for_status()
    hits = resp.json().get("hits", {}).get("hits", [])
    if not hits:
        raise RegionError(f"no places found for contained_in: {ids}")

    geoms: list[dict] = []
    for h in hits:
        geoms.extend(extract_place_geoms(h.get("_source", {})))
    # Only polygonal geometry makes a meaningful containment region; if every
    # contained_in place is point-only, the region is degenerate.
    union = _union_from_geoms(geoms)
    if union is None or union.is_empty:
        raise RegionError(
            f"contained_in places have no usable geometry: {ids}"
        )
    region = _region_from_union(union)
    _cache_put(key, region)
    return region


# ---------------------------------------------------------------------------
# Containment tests
# ---------------------------------------------------------------------------

def _collect_h3_cover(src: dict) -> list[str]:
    cover: list[str] = []
    for g in src.get("geometries", []) or []:
        if not isinstance(g, dict):
            continue
        cc = g.get("h3_cover")
        if isinstance(cc, list):
            cover.extend(c for c in cc if isinstance(c, str))
        elif isinstance(cc, str) and cc:
            cover.append(cc)
    return cover


def _point_in_region_h3(repr_point: list[float], region: ResolvedRegion) -> bool:
    """Cheap H3 test: is the representative point inside the region cover?"""
    if not _H3_AVAILABLE or not repr_point:
        return False
    lon, lat = repr_point[0], repr_point[1]
    for res in region.resolutions:
        try:
            if _h3.latlng_to_cell(lat, lon, res) in region.cover_by_res[res]:
                return True
        except Exception:
            continue
    return False


def _cover_overlaps_region(cover: list[str], region: ResolvedRegion) -> bool:
    """Fuzzy areal test: does the candidate's h3_cover intersect the region?"""
    if not cover:
        return False
    for res in region.resolutions:
        normalised = _normalise_cells_to_resolution(cover, res)
        if normalised & region.cover_by_res[res]:
            return True
    return False


def _cover_within_region(cover: list[str], region: ResolvedRegion) -> bool:
    """Fuzzy 'within' test: is every candidate cover cell inside the region?

    A candidate cell is inside R iff, at its own resolution, it (or an ancestor)
    is a region cover cell. We test each cell against the region cover walked to
    that cell's resolution.
    """
    if not cover or not _H3_AVAILABLE:
        return False
    for cell in cover:
        try:
            cres = _h3.get_resolution(cell)
        except Exception:
            return False
        inside = False
        for res in region.resolutions:
            if res > cres:
                continue  # region cell finer than candidate cell — can't contain it
            target = cell if res == cres else _safe_parent(cell, res)
            if target is not None and target in region.cover_by_res[res]:
                inside = True
                break
        if not inside:
            return False
    return True


def _safe_parent(cell: str, res: int) -> Optional[str]:
    try:
        return _h3.cell_to_parent(cell, res)
    except Exception:
        return None


def _exact_intersects(src: dict, region: ResolvedRegion) -> bool:
    """Exact Shapely intersects against all of the candidate's geometries."""
    for gj in extract_place_geoms(src):
        try:
            if region.prepared.intersects(_shape(gj)):
                return True
        except Exception:
            continue
    return False


def _exact_within(src: dict, region: ResolvedRegion) -> bool:
    """Exact Shapely 'within': the candidate's (unioned) geometry ⊆ region."""
    geoms = extract_place_geoms(src)
    union = _union_from_geoms(geoms)
    if union is None or union.is_empty:
        return False
    try:
        return region.prepared.contains(union)
    except Exception:
        return False


def hit_matches(src: dict, region: ResolvedRegion, mode: str, relation: str) -> bool:
    """Decide whether a single place ``_source`` matches the containment region.

    ``mode``     — "fuzzy" (H3 cells) | "exact" (Shapely).
    ``relation`` — "intersects" (default) | "within".
    """
    rp = extract_repr_point(src)

    if mode == "exact" and _SHAPELY_AVAILABLE:
        if relation == "within":
            return _exact_within(src, region)
        # intersects: repr_point is guaranteed within the place geometry, so if
        # it lies within R the geometry intersects R — skip the full-geom test.
        if rp:
            try:
                if region.prepared.intersects(_Point(rp[0], rp[1])):
                    return True
            except Exception:
                pass
        return _exact_intersects(src, region)

    # fuzzy (default / shapely-unavailable fallback)
    cover = _collect_h3_cover(src)
    if relation == "within":
        if not _point_in_region_h3(rp, region):
            return False
        if cover:
            return _cover_within_region(cover, region)
        return True
    # intersects
    if _point_in_region_h3(rp, region):
        return True
    return _cover_overlaps_region(cover, region)


def apply_containment(
    hits: list[dict], region: ResolvedRegion, mode: str = "fuzzy",
    relation: str = "intersects",
) -> list[dict]:
    """Filter ES place hits to those matching the containment region."""
    if region is None:
        return hits
    out: list[dict] = []
    for hit in hits:
        src = hit.get("_source", {})
        try:
            if hit_matches(src, region, mode, relation):
                out.append(hit)
        except Exception as exc:  # never let one bad geom drop the whole request
            logger.debug("containment test failed for a hit: %s", exc)
    return out
