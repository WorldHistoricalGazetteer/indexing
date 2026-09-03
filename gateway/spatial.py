# gateway/spatial.py
"""
Spatial-containment engine for ``/api/search`` and ``/api/reconcile``.

Resolves a *containment region* from a set of place_ids (or raw GeoJSON) and
filters candidate place hits by **fuzzy** (H3, cheap, tolerant) or **exact**
(Shapely) containment. **No Elasticsearch reindex.**

What's available where (postbarrier index, verified 2026-05-25):

* ES ``_source`` per geometry carries ``repr_point`` (guaranteed *within* the
  geometry — see ``processing.helpers.enrich_geometry`` → ``representative_point``),
  ``h3_centroid`` (r7), ``h3_cover`` (compacted, mixed-resolution cells covering
  the geometry), ``bounds`` (bbox) and ``geometry_index``. **The full polygon is
  NOT in ``_source``.**
* Full polygons live in the ``/vast`` geom-store (``processing.geom_store``),
  keyed ``"{place_id}_{geometry_index}"``.

So **fuzzy** containment runs entirely off ``_source`` (h3_cover) — no ``/vast``
dependency — while **exact** containment reads real polygons from the geom-store
via a cached ``GeomStoreReader`` and tests them with Shapely.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
from functools import lru_cache
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from pydantic import BaseModel

from .config import ES_BACKEND, PLACES_INDEX
from .es_helpers import (
    ES_HEADERS, _has_geometries, extract_repr_point, extract_place_geoms,
)

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
    from shapely import wkb as _wkb
    _SHAPELY_AVAILABLE = True
except Exception:  # pragma: no cover
    _SHAPELY_AVAILABLE = False


# H3 polyfill controls (used only by region_from_geojson, where we have a real
# geometry). Mirrors processing.helpers to keep the gateway import-light.
H3_POLYFILL_MAX_CELLS = 10_000
H3_CENTROID_RESOLUTION = 7
# ⚠️ A hard-coded ``_H3_HEX_AREA_DEG2`` table stood here and in
# ``processing.helpers``. Every entry was **~108× too large** — the areas had
# been divided by 111 (km per degree) instead of 111² (km² per degree²). Both
# copies now DERIVE the value from ``h3`` itself, so the two cannot drift and
# the transcription error cannot recur. This file keeps its own copy of the
# logic deliberately (see the note above about staying import-light); what it
# no longer keeps is its own copy of the *numbers*.
_KM_PER_DEGREE = 111.19492664455873
_H3_HEX_AREA_DEG2_FALLBACK = {
    0: 352.4, 1: 49.32, 2: 7.020, 3: 1.002,
    4: 0.1432, 5: 0.02045, 6: 0.002922, 7: 0.0004174, 8: 5.963e-05,
}
_ES_H3_TERMS_CAP = 4000
_REGION_CACHE_MAX = 128

class RegionError(ValueError):
    """A containment region could not be built (e.g. no usable geometry)."""


# ---------------------------------------------------------------------------
# Geom-store reader (lazy singleton; only the exact path needs it / /vast)
# ---------------------------------------------------------------------------
_reader: Any = None
_reader_init = False


def get_geom_reader():
    """Return a cached ``GeomStoreReader`` for the exact path, or None if the
    ``/vast`` geom-store is unavailable (exact then degrades to repr_point)."""
    global _reader, _reader_init
    if _reader_init:
        return _reader
    _reader_init = True
    try:
        from processing.geom_store import GeomStoreReader
        from processing.settings import GEOM_STORE_DIR
        _reader = GeomStoreReader(GEOM_STORE_DIR)
    except Exception as exc:  # pragma: no cover
        logger.warning("geom-store unavailable — exact containment degraded: %s", exc)
        _reader = None
    return _reader


# ---------------------------------------------------------------------------
# H3 helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=32)
def _h3_cell_area_deg2_equator(res: int) -> float:
    """Mean H3 cell area at ``res`` in degrees², at the equator, from h3."""
    if not _H3_AVAILABLE:
        return _H3_HEX_AREA_DEG2_FALLBACK.get(res, 0.0004174)
    try:
        return _h3.average_hexagon_area(res, unit="km^2") / (_KM_PER_DEGREE ** 2)
    except Exception:                                          # noqa: BLE001
        return _H3_HEX_AREA_DEG2_FALLBACK.get(res, 0.0004174)


def _estimate_polyfill_cells(bbox_area_deg2: float, centre_lat_deg: float,
                             res: int) -> float:
    """Estimated cell count — scales with cos(latitude); see processing.helpers."""
    cos_lat = max(math.cos(math.radians(max(-89.9, min(89.9, centre_lat_deg)))), 0.02)
    return bbox_area_deg2 * cos_lat / max(_h3_cell_area_deg2_equator(res), 1e-12)


def _pick_polyfill_resolution(bbox_area_deg2: float,
                              centre_lat_deg: float = 0.0) -> int:
    for res in (H3_CENTROID_RESOLUTION, 5, 3):
        if _estimate_polyfill_cells(bbox_area_deg2, centre_lat_deg, res) <= H3_POLYFILL_MAX_CELLS:
            return res
    return 3


def _polyfill(geom) -> set[str]:
    if not _H3_AVAILABLE:
        return set()
    try:
        shape_obj = _h3.geo_to_h3shape(_mapping(geom))
    except Exception:
        return set()
    minx, miny, maxx, maxy = geom.bounds
    start = _pick_polyfill_resolution(
        max(0.0, (maxx - minx) * (maxy - miny)), (miny + maxy) / 2.0)
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


def _group_by_res(cells: Iterable[str]) -> dict[int, set[str]]:
    by_res: dict[int, set[str]] = {}
    for cell in cells:
        if not isinstance(cell, str) or not cell:
            continue
        try:
            res = _h3.get_resolution(cell)
        except Exception:
            continue
        by_res.setdefault(res, set()).add(cell)
    return by_res


def _safe_parent(cell: str, res: int) -> Optional[str]:
    try:
        return _h3.cell_to_parent(cell, res)
    except Exception:
        return None


def _es_h3_terms(cover_by_res: dict[int, set[str]]) -> list[str]:
    """Region cover cells + their ancestors, for the ES ``h3_cover`` terms
    recall clause (catches large candidates spanning the region). Capped."""
    if not _H3_AVAILABLE:
        return []
    terms: set[str] = set()
    for res, cells in cover_by_res.items():
        for cell in cells:
            terms.add(cell)
            for parent_res in range(res - 1, -1, -1):
                p = _safe_parent(cell, parent_res)
                if p is None:
                    break
                terms.add(p)
        if len(terms) > _ES_H3_TERMS_CAP:
            flat: set[str] = set()
            for cs in cover_by_res.values():
                flat.update(cs)
            return list(flat)[:_ES_H3_TERMS_CAP]
    return list(terms)


# ---------------------------------------------------------------------------
# Resolved region
# ---------------------------------------------------------------------------

@dataclass
class ResolvedRegion:
    cover_by_res: dict[int, set[str]]      # fuzzy: region H3 cover, by resolution
    resolutions: tuple[int, ...]
    bbox_geojson: dict                     # envelope for the cheap ES gate
    h3_terms: list[str]                    # region cells + ancestors (ES recall)
    geom_keys: tuple[str, ...] = ()        # "{pid}_{idx}" keys for lazy exact load
    union: Any = None                      # shapely region (exact; lazy)
    prepared: Any = None                   # prep(union) (exact; lazy)
    # --- provenance (place#144): lets the endpoint tell the client HOW the
    #     scope was applied instead of silently widening or dropping it ---
    source: str = "polygon"                # polygon | linked-polygon | geojson
    area_ids: tuple[str, ...] = ()         # containers that contributed a polygon
    linked_ids: tuple[str, ...] = ()       # co-referents whose polygon was borrowed
    point_ids: tuple[str, ...] = ()        # containers that contributed only a point
    unresolved_ids: tuple[str, ...] = ()   # requested ids with no usable geometry
    _geom_loaded: bool = False
    # Region cells lifted to a COARSER resolution, memoised per resolution. Built
    # on demand by _region_ancestors_at so an overlap test against a candidate
    # whose cover is coarser than the region's costs a hash lookup instead of a
    # child expansion. Idempotent, so the benign race between threads is fine.
    _ancestor_cache: dict = field(
        default_factory=dict, repr=False, compare=False,
    )
    # A ResolvedRegion is shared via _region_cache and load_geometry mutates
    # it, so the load must be serialised now that it runs off the event loop
    # (see apply_containment_async). Without this, a second thread could
    # observe _geom_loaded=True while `prepared` was still None and silently
    # degrade an exact request to fuzzy — the same class of invisible wrong
    # answer place#165 exists to remove.
    _lock: Any = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )
    # `prepared` is built once and cached on this SHARED region — but a
    # PreparedGeometry is not safe to USE from several threads. GEOS builds its
    # spatial index lazily inside the prepared geometry on the first predicate call
    # and mutates it as it goes, so concurrent intersects()/contains() on one
    # instance corrupts that index. It does not raise: it segfaults the worker.
    #
    # Observed 2026-08-18: a batch of 25 reconcile queries against one container,
    # each running its exact test in a worker thread, killed gateway workers four
    # times in three minutes — `segfault at 8 ... in libgeos.so.3.13.1` — and every
    # request in flight on the dying worker came back empty, so rows silently
    # recorded "no match". The load path was already serialised; the USE path was
    # the hole.
    #
    # Each thread therefore gets its own PreparedGeometry over the shared (immutable)
    # union. Preparing is cheap next to the geom-store reads, and this keeps the
    # concurrency the thread hand-off exists to buy.
    _tls: Any = field(
        default_factory=threading.local, repr=False, compare=False,
    )
    # WKB snapshot of `union`: plain bytes are safe to share between threads, whereas
    # the GEOSGeometry they describe is not.
    _union_wkb: Any = field(default=None, repr=False, compare=False)

    def prepared_local(self):
        """This thread's own PreparedGeometry, or None when no geometry is loaded.

        `self.prepared` remains the marker that an exact geometry exists (the load
        path sets it); it must never be used for predicates from a worker thread.

        The thread gets its own GEOMETRY as well as its own prepared wrapper, rebuilt
        from a WKB snapshot. Giving each thread only its own wrapper was not enough —
        the workers still died in libgeos, because every wrapper still pointed at the
        one shared GEOSGeometry, and GEOS mutates lazily-cached state inside a
        geometry as predicates run. Sharing only the WKB (plain immutable bytes)
        leaves no GEOS object crossing a thread boundary at all.

        The geometry is cached alongside the prepared object deliberately: the
        prepared wrapper holds a bare pointer to it, so letting it be collected would
        leave the wrapper pointing at freed memory — the same crash by another route.
        """
        if self.union is None:
            return None
        p = getattr(self._tls, "prepared", None)
        if p is None:
            # A region built straight from GeoJSON (region_from_geojson, and the
            # bounds path) never runs load_geometry, so it carries a union with no
            # snapshot yet. Take one now, under the lock — serialising the shared
            # geometry is itself a read of it, and two threads doing that at once is
            # the very thing this method exists to avoid.
            if self._union_wkb is None:
                with self._lock:
                    if self._union_wkb is None:
                        try:
                            self._union_wkb = self.union.wkb
                        except Exception:
                            return None
            try:
                geom = _wkb.loads(self._union_wkb)
                p = _prep(geom)
            except Exception:
                return None
            self._tls.geom = geom      # keep alive: `p` only holds a pointer to it
            self._tls.prepared = p
        return p

    @property
    def has_cover(self) -> bool:
        return bool(self.cover_by_res)

    def load_geometry(self, reader) -> bool:
        """Load the region's real polygons from the geom-store for exact tests.
        Returns True if a usable geometry is now available.

        Thread-safe and idempotent: concurrent callers for the same cached
        region do the work once and all observe the completed result.
        """
        if self.prepared is not None:
            return True
        with self._lock:
            return self._load_geometry_locked(reader)

    def _load_geometry_locked(self, reader) -> bool:
        if self.prepared is not None:
            return True
        if self._geom_loaded:
            return self.prepared is not None
        if reader is None or not _SHAPELY_AVAILABLE:
            self._geom_loaded = True
            return False
        shapes = []
        for key in self.geom_keys:
            try:
                gj = reader.get(key)
            except Exception:
                gj = None
            if gj:
                shp = _safe_shape(gj)
                if shp is not None:
                    shapes.append(shp)
        # Set only now that the reads are done: an early flag would let a
        # waiting thread see "loaded" with nothing behind it.
        self._geom_loaded = True
        if not shapes:
            return False
        try:
            self.union = _unary_union(shapes) if len(shapes) > 1 else shapes[0]
            self.prepared = _prep(self.union)
            self._union_wkb = self.union.wkb   # per-thread rebuilds work from this
        except Exception:
            self.union = None
            self.prepared = None
            return False
        return True


def _safe_shape(gj: dict):
    try:
        shp = _shape(gj)
        if shp is None or shp.is_empty:
            return None
        if not shp.is_valid:
            shp = shp.buffer(0)
        return None if shp.is_empty else shp
    except Exception:
        return None


def _bbox_geojson_from_bounds(bounds_list: list[list[float]]) -> Optional[dict]:
    boxes = [b for b in bounds_list if isinstance(b, (list, tuple)) and len(b) == 4]
    if not boxes:
        return None
    w = min(b[0] for b in boxes); s = min(b[1] for b in boxes)
    e = max(b[2] for b in boxes); n = max(b[3] for b in boxes)
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


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
    """Build a region from a raw GeoJSON geometry (the ``bounds`` param). The
    geometry is supplied, so both fuzzy (polyfill → cover) and exact (prepared
    union) are available without the geom-store."""
    if not _SHAPELY_AVAILABLE or not isinstance(bounds, dict):
        return None
    key = "gj:" + hashlib.sha1(json.dumps(bounds, sort_keys=True).encode()).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        return cached

    parts = []
    if bounds.get("type") == "GeometryCollection":
        parts = [g for g in bounds.get("geometries", []) if isinstance(g, dict)]
    elif bounds.get("type") and bounds.get("coordinates"):
        parts = [bounds]
    shapes = [s for s in (_safe_shape(p) for p in parts) if s is not None]
    if not shapes:
        return None
    union = _unary_union(shapes) if len(shapes) > 1 else shapes[0]
    if union.is_empty:
        return None

    cover = _group_by_res(_polyfill(union)) if union.area > 0 else {}
    if not cover:
        try:
            rp = union.representative_point()
            cover = {H3_CENTROID_RESOLUTION: {_h3.latlng_to_cell(rp.y, rp.x, H3_CENTROID_RESOLUTION)}}
        except Exception:
            cover = {}
    minx, miny, maxx, maxy = union.bounds
    region = ResolvedRegion(
        cover_by_res=cover,
        resolutions=tuple(sorted(cover.keys())),
        bbox_geojson={"type": "Polygon", "coordinates": [[
            [minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]]},
        h3_terms=_es_h3_terms(cover),
        union=union,
        prepared=_prep(union),
        source="geojson",
        _geom_loaded=True,
    )
    _cache_put(key, region)
    return region


# Approximate H3 edge length in km per resolution — for choosing a resolution and
# a ring count from a radius. Indicative averages; cells are not equal-area, which
# is why the disc is grown by one ring below rather than trusted to the millimetre.
_H3_EDGE_KM = {4: 22.6, 5: 8.54, 6: 3.23, 7: 1.22, 8: 0.46}
_DISC_MAX_CELLS = 2000


def region_from_circle(lat: float, lng: float, radius_km: float) -> Optional[ResolvedRegion]:
    """Build a containment region for "within `radius_km` of this point", as an H3
    disc rather than a polygon.

    A radial filter used to be expressed as a 32-point circle handed to
    ``region_from_geojson``, which unions it in Shapely, polyfills it, and prepares
    a geometry for exact tests. None of that is needed to answer "near this point":
    the index already stores an H3 cover per geometry, so the question is a terms
    match on precomputed cells. This path therefore touches no GEOS at all — which
    also keeps radial queries clear of the polygon machinery that has twice been
    implicated in a wedged worker (WHG place#184).

    The resolution is chosen from the radius so the disc stays a few hundred cells:
    fine enough to be a real filter, coarse enough that the ES terms clause stays
    small. Cells are not equal-area, so the ring count is rounded up and grown by
    one — a radial filter that is slightly generous is a filter; one that clips
    inside the radius silently loses valid matches.
    """
    if not _H3_AVAILABLE or lat is None or lng is None or not radius_km or radius_km <= 0:
        return None
    key = f"circle:{round(float(lat), 5)}:{round(float(lng), 5)}:{round(float(radius_km), 3)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    # Finest resolution whose disc stays under the cell budget.
    res, rings = None, 0
    for r in sorted(_H3_EDGE_KM):                        # coarse → fine
        k = max(1, int(math.ceil(float(radius_km) / _H3_EDGE_KM[r])) + 1)
        n_cells = 1 + 3 * k * (k + 1)                     # cells in a k-ring disc
        if n_cells <= _DISC_MAX_CELLS:
            res, rings = r, k
        else:
            break
    if res is None:
        return None

    try:
        centre = _h3.latlng_to_cell(float(lat), float(lng), res)
        cells = set(_h3.grid_disk(centre, rings))
    except Exception:
        return None
    if not cells:
        return None

    # Envelope for the cheap ES gate: a degree box around the point, latitude-corrected.
    dlat = float(radius_km) / 110.574
    dlng = float(radius_km) / max(0.1, 111.320 * math.cos(math.radians(float(lat))))
    minx, maxx = float(lng) - dlng, float(lng) + dlng
    miny, maxy = float(lat) - dlat, float(lat) + dlat

    cover = {res: cells}
    region = ResolvedRegion(
        cover_by_res=cover,
        resolutions=(res,),
        bbox_geojson={"type": "Polygon", "coordinates": [[
            [minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]]},
        h3_terms=_es_h3_terms(cover),
        source="h3-disc",
        # No `union`/`prepared`: an exact test would need a real polygon, and the
        # point of this path is not to build one. `hit_matches` falls back to the
        # fuzzy H3 test, which is what a radial filter wants anyway.
        _geom_loaded=True,
    )
    _cache_put(key, region)
    return region


def _is_located(g: dict) -> bool:
    """True if a non-area geometry is at least locatable, from indexed fields
    only — ``repr_point``, ``h3_centroid`` (r7) or ``bounds``. No geom-store
    read. Distinguishes "container resolved but has only a point" from
    "container carries no geometry at all"."""
    rp = g.get("repr_point")
    if isinstance(rp, dict) and rp.get("lon") is not None and rp.get("lat") is not None:
        return True
    if isinstance(rp, (list, tuple)) and len(rp) == 2:
        return True
    if isinstance(g.get("h3_centroid"), str) and g["h3_centroid"]:
        return True
    b = g.get("bounds")
    return isinstance(b, list) and len(b) == 4 and all(
        isinstance(v, (int, float)) for v in b)


# ---------------------------------------------------------------------------
# Hard-link container upgrade — borrow a co-referent's polygon
# ---------------------------------------------------------------------------

# Only *identity* assertions may lend their geometry. closeMatch is explicitly
# weaker than identity (it is what the legacy contributor replay emits), and
# `distinct` is a negative assertion.
_GEOM_LENDING_RELATIONS = ("sameAs", "exactMatch")
_MAX_LINKED_CONTAINERS = 50


async def _linked_container_ids(ids: list[str]) -> list[str]:
    """Co-referent place_ids of ``ids`` per the hard-link stores.

    Polygon coverage is namespace-shaped, not place-shaped: GeoNames and (mostly)
    Wikidata carry a point for exactly the admin units users pick as a scope
    region, while the co-referent record in another gazetteer has the boundary —
    e.g. ``gn:3017382`` (France, point) ``sameAs`` ``wd:Q142`` (France, polygon).
    Following the identity edge gives the user the boundary they meant, without
    inventing one. Best-effort: a missing/locked store just yields no upgrade.
    """
    import asyncio

    try:
        from .hard_link_expansion import expand_hard_links
        edges = await asyncio.to_thread(expand_hard_links, ids, one_hop=True)
    except Exception as exc:  # pragma: no cover — enrichment, never fatal
        logger.debug("hard-link container upgrade unavailable: %s", exc)
        return []

    wanted = set(ids)
    linked: list[str] = []
    for edge in edges:
        if edge.relation_type not in _GEOM_LENDING_RELATIONS:
            continue
        for near, far in ((edge.a, edge.b), (edge.b, edge.a)):
            if near in wanted and far not in wanted and far not in linked:
                linked.append(far)
    return sorted(linked)[:_MAX_LINKED_CONTAINERS]


def _strip_place_prefix(pid: str) -> str:
    """Normalise a ``contained_in`` id: strip a leading ``place:`` so a client
    passing the canonical candidate id form (``place:ukhc:CMB``) resolves to the
    bare namespaced id (``ukhc:CMB``) rather than silently hitting no-resolve."""
    if isinstance(pid, str) and pid.startswith("place:"):
        return pid[len("place:"):]
    return pid


def is_areal(g: dict) -> bool:
    """True iff a geometry can serve as a **containment region** — i.e. it is
    areal (a polygon), the shape distinction that ``geom_class`` records.

    Keys on ``geom_class == "area"``. For legacy docs that do not yet carry
    ``geom_class`` (the corpus-wide backfill is a separate step), it falls back
    to ``has_geom`` — a stored non-point geometry, which for those docs is a
    polygon. Crucially, a restored **LineString** carries ``geom_class="line"``
    and is therefore correctly **excluded** here even though ``has_geom`` is true
    (place#145 — the reason we stopped keying containment off ``has_geom``: a
    line is retrievable but not a container).
    """
    gc = g.get("geom_class")
    if gc is not None:
        return gc == "area"
    return bool(g.get("has_geom"))


_CONTAINER_SOURCE = [
    "place_id",
    "geometries.h3_cover",
    "geometries.h3_centroid",
    "geometries.geometry_index",
    "geometries.bounds",
    "geometries.repr_point",
    "geometries.has_geom",
    "geometries.geom_class",
]


async def _fetch_containers(ids: list[str], client, auth) -> list[dict]:
    """Fetch the container places' geometry attributes from ES ``_source``."""
    resp = await client.post(
        f"{ES_BACKEND}/{PLACES_INDEX}/_search",
        json={"size": len(ids), "query": {"terms": {"place_id": ids}},
              "_source": _CONTAINER_SOURCE},
        auth=auth, headers=ES_HEADERS,
    )
    resp.raise_for_status()
    return resp.json().get("hits", {}).get("hits", [])


@dataclass
class _ContainerGeoms:
    """What a set of container hits offers: exact area cover, or seed points."""
    cells: set[str] = field(default_factory=set)
    bounds: list[list[float]] = field(default_factory=list)
    geom_keys: list[str] = field(default_factory=list)
    area_ids: list[str] = field(default_factory=list)
    point_ids: list[str] = field(default_factory=list)


def _collect_containers(hits: list[dict]) -> _ContainerGeoms:
    out = _ContainerGeoms()
    for h in hits:
        src = h.get("_source", {})
        pid = src.get("place_id")
        for idx, g in enumerate(src.get("geometries", []) or []):
            if not isinstance(g, dict):
                continue
            cover = g.get("h3_cover")
            cells: set[str] = set()
            if isinstance(cover, list):
                cells = {c for c in cover if isinstance(c, str)}
            elif isinstance(cover, str) and cover:
                cells = {cover}
            # Only AREAL geometries (polygons) define the region EXACTLY. Keyed
            # on geom_class now, not has_geom: a restored LineString is
            # has_geom=true but geom_class="line", and a line must not become a
            # containment region (place#145). Point-only / line geometries are
            # kept aside as seeds for the fallbacks / reporting instead.
            if is_areal(g) and cells:
                out.cells.update(cells)
                b = g.get("bounds")
                if isinstance(b, list) and len(b) == 4:
                    out.bounds.append(b)
                if pid is not None:
                    out.geom_keys.append(f"{pid}_{g.get('geometry_index', idx)}")
                    if pid not in out.area_ids:
                        out.area_ids.append(pid)
                continue
            # Located, but not an area — it cannot define a region itself. It is
            # still recorded so the endpoint can report which containers were
            # only partly honoured, and it seeds the co-referent lookup.
            if _is_located(g) and pid is not None and pid not in out.point_ids:
                out.point_ids.append(pid)
    return out


def _region_from_area(found: _ContainerGeoms, **provenance) -> Optional[ResolvedRegion]:
    cover_by_res = _group_by_res(found.cells)
    if not cover_by_res:
        return None
    bbox = _bbox_geojson_from_bounds(found.bounds) or _bbox_from_cells(found.cells)
    return ResolvedRegion(
        cover_by_res=cover_by_res,
        resolutions=tuple(sorted(cover_by_res.keys())),
        bbox_geojson=bbox,
        h3_terms=_es_h3_terms(cover_by_res),
        geom_keys=tuple(found.geom_keys),
        **provenance,
    )


async def resolve_region(
    place_ids: list[str],
    client,
    auth,
    link_fallback: bool = True,
) -> Optional[ResolvedRegion]:
    """Resolve a region from place_ids using only ES ``_source``: the region's
    H3 cover (fuzzy) + bbox + the geom-store keys for lazy exact loading.

    Geometries carrying a usable **area** cover (``has_geom = true`` with an
    ``h3_cover``) define the region exactly, as before.

    When **no** container yields an area cover — every id is point-only or
    unresolvable — ``link_fallback`` borrows the boundary from a
    ``sameAs``/``exactMatch`` **co-referent** of the container
    (``source="linked-polygon"``). Polygon coverage is namespace-shaped: the
    GeoNames record for a country is a point while its Wikidata twin carries the
    boundary. That is still an exact region, just sourced from a different
    gazetteer's record of the same place — no geometry is invented here.

    Returns ``None`` when no real boundary can be found. Callers **must** treat
    that as "the requested scope could not be applied" — NOT as "no scope
    requested"; running the query unconstrained is the place#144 bug. Raises
    ``RegionError`` only for a gateway misconfiguration (H3 unavailable).
    """
    if not _H3_AVAILABLE:
        raise RegionError("h3 unavailable in the gateway")
    ids = sorted({_strip_place_prefix(p) for p in (place_ids or []) if p})
    ids = [i for i in ids if i]
    if not ids:
        return None
    key = f"ids:{'|'.join(ids)}#lf={int(bool(link_fallback))}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    hits = await _fetch_containers(ids, client, auth)
    if not hits:
        # No id resolved to a place at all → nothing to build a region from.
        return None
    found = _collect_containers(hits)

    region = _region_from_area(
        found,
        area_ids=tuple(found.area_ids),
        point_ids=tuple(p for p in found.point_ids if p not in found.area_ids),
        unresolved_ids=tuple(
            i for i in ids
            if i not in found.area_ids and i not in found.point_ids),
    )

    if region is None and link_fallback:
        # Every container is point-only — try their co-referents' boundaries.
        linked = await _linked_container_ids(ids)
        if linked:
            linked_found = _collect_containers(
                await _fetch_containers(linked, client, auth))
            region = _region_from_area(
                linked_found,
                source="linked-polygon",
                linked_ids=tuple(linked_found.area_ids),
                point_ids=tuple(found.point_ids),
                unresolved_ids=tuple(i for i in ids if i not in found.point_ids),
            )

    if region is None:
        return None
    _cache_put(key, region)
    return region


def _bbox_from_cells(cells: Iterable[str]) -> dict:
    lons: list[float] = []
    lats: list[float] = []
    for cell in cells:
        try:
            lat, lon = _h3.cell_to_latlng(cell)
        except Exception:
            continue
        lons.append(lon); lats.append(lat)
    if not lons:
        return {"type": "Polygon", "coordinates": [[[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]]]}
    # pad by ~one r4 cell (~0.3°) so boundary candidates are gathered
    pad = 0.3
    w, e = min(lons) - pad, max(lons) + pad
    s, n = min(lats) - pad, max(lats) + pad
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


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


def _point_in_region_h3(repr_point, region: ResolvedRegion) -> bool:
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


def _region_ancestors_at(region: ResolvedRegion, res: int) -> set[str]:
    """The region's cells lifted to ``res`` — a resolution COARSER than they are.

    Memoised on the region. Cost is one parent walk per region cell, once per
    resolution asked for, against a set that only ever SHRINKS as it coarsens.
    """
    cached = region._ancestor_cache.get(res)
    if cached is not None:
        return cached
    out: set[str] = set()
    for r, cells in region.cover_by_res.items():
        if r < res:
            continue
        for cell in cells:
            parent = cell if r == res else _safe_parent(cell, res)
            if parent is not None:
                out.add(parent)
    region._ancestor_cache[res] = out
    return out


def _cover_overlaps_region(cover: list[str], region: ResolvedRegion) -> bool:
    """Does a candidate's H3 cover touch the region's?

    Two H3 cells overlap exactly when one is the other's ancestor, so the test
    only ever needs to walk the FINER cell UP to the coarser resolution. Which
    side is finer varies, so both directions are handled — but neither expands.

    It used to normalise the candidate's cover to each region resolution via
    ``cell_to_children``, which multiplies by 7 per resolution step and builds
    every child as a Python string. Against a county-sized container that is
    hundreds of cover cells expanded 49-fold, per candidate, per region
    resolution: enough to pin both gateway workers at 100% CPU until the
    watchdog killed them (observed in prod 2026-08-19, stack dump parked in
    ``h3/api/basic_str/_convert.py`` under this function). ``_cover_within_region``
    already walked parents only; this is the same idiom, applied to the twin it
    was missing from.
    """
    if not cover or not _H3_AVAILABLE:
        return False
    for cell in cover:
        try:
            cres = _h3.get_resolution(cell)
        except Exception:
            continue
        for res in region.resolutions:
            if cres >= res:
                # Candidate cell is finer (or equal): lift IT to the region's res.
                target = cell if cres == res else _safe_parent(cell, res)
                if target is not None and target in region.cover_by_res[res]:
                    return True
            elif cell in _region_ancestors_at(region, cres):
                # Region cells are finer: lift THEM, once, and look the cell up.
                return True
    return False


def _cover_within_region(cover: list[str], region: ResolvedRegion) -> bool:
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
                continue
            target = cell if res == cres else _safe_parent(cell, res)
            if target is not None and target in region.cover_by_res[res]:
                inside = True
                break
        if not inside:
            return False
    return True


def _candidate_geometry(src: dict, reader):
    """Build the candidate's Shapely geometry from the geom-store (exact mode),
    falling back to its repr_point if the polygon is unavailable."""
    pid = src.get("place_id")
    shapes = []
    if reader is not None and pid:
        for idx, g in enumerate(src.get("geometries", []) or []):
            if not isinstance(g, dict):
                continue
            try:
                gj = reader.get(f"{pid}_{g.get('geometry_index', idx)}")
            except Exception:
                gj = None
            if gj:
                shp = _safe_shape(gj)
                if shp is not None:
                    shapes.append(shp)
    if not shapes:
        # Fallback: any inline _source geom (rare in prod — usually only a
        # repr_point Point is recoverable here, which is still a valid point
        # test) — also the path unit tests exercise.
        for gj in extract_place_geoms(src):
            shp = _safe_shape(gj)
            if shp is not None:
                shapes.append(shp)
    if shapes:
        try:
            return _unary_union(shapes) if len(shapes) > 1 else shapes[0]
        except Exception:
            return shapes[0]
    return None


def hit_matches(src: dict, region: ResolvedRegion, mode: str, relation: str, reader=None) -> bool:
    rp = extract_repr_point(src)

    # Predicates go through THIS THREAD's prepared geometry — see ResolvedRegion._tls.
    # `region.prepared` still answers "is an exact geometry available?", but using it
    # for the tests themselves segfaults GEOS under concurrency.
    prepared = region.prepared_local() if mode == "exact" and _SHAPELY_AVAILABLE else None

    if mode == "exact" and _SHAPELY_AVAILABLE and prepared is not None:
        if relation == "within":
            # Cheap, exact fast-reject: repr_point is guaranteed within the
            # candidate's geometry, so geometry ⊆ R requires repr_point ∈ R.
            # A point-in-polygon test (~µs) lets us skip the expensive
            # geom-store polygon load for every candidate that overlaps R but
            # spills outside it (e.g. cross-border polygons).
            if rp:
                try:
                    if not prepared.intersects(_Point(rp[0], rp[1])):
                        return False
                except Exception:
                    pass
            g = _candidate_geometry(src, reader)
            if g is None:
                return False
            try:
                return prepared.contains(g)
            except Exception:
                return False
        # intersects: repr_point is guaranteed within the place geometry, so if
        # it lies within R the geometry intersects R — skip the geom-store fetch.
        if rp:
            try:
                if prepared.intersects(_Point(rp[0], rp[1])):
                    return True
            except Exception:
                pass
        g = _candidate_geometry(src, reader)
        if g is None:
            return False
        try:
            return prepared.intersects(g)
        except Exception:
            return False

    # fuzzy (default; also the fallback when exact geometry is unavailable)
    cover = _collect_h3_cover(src)
    if relation == "within":
        if not _point_in_region_h3(rp, region):
            return False
        return _cover_within_region(cover, region) if cover else True
    if _point_in_region_h3(rp, region):
        return True
    return _cover_overlaps_region(cover, region)


def apply_containment(
    hits: list[dict], region: ResolvedRegion, mode: str = "fuzzy",
    relation: str = "intersects", reader=None,
) -> list[dict]:
    """Filter ES place hits to those matching the containment region.

    For ``mode='exact'`` the caller should pass ``reader`` (a GeomStoreReader)
    and have called ``region.load_geometry(reader)``; if the region geometry
    could not be loaded, exact silently degrades to the fuzzy test.
    """
    if region is None:
        return hits
    out: list[dict] = []
    for hit in hits:
        src = hit.get("_source", {})
        try:
            if hit_matches(src, region, mode, relation, reader=reader):
                out.append(hit)
        except Exception as exc:
            logger.debug("containment test failed for a hit: %s", exc)
    return out


async def apply_containment_async(
    hits: list[dict], region: ResolvedRegion, mode: str = "fuzzy",
    relation: str = "intersects", reader=None,
) -> list[dict]:
    """``apply_containment``, with the exact path moved off the event loop.

    Until place#165 the geom-store never loaded, so ``mode='exact'`` did no
    real work and running it inline was free. Now that it genuinely reads
    polygons and runs Shapely, doing so on the event loop would stall *every*
    concurrent request, not just this one — so the exact path (geom-store
    reads + unary_union + prep + per-hit tests) runs in a worker thread.

    This is only safe because ``GeomStoreReader`` is thread-safe: shard reads
    use ``os.pread`` rather than ``seek()``-then-``read()`` on a shared handle,
    and SQLite connections are per-thread. ``load_geometry`` is called inside
    the thread under the region's lock.

    The fuzzy path stays inline — it is set arithmetic over H3 cells, and the
    thread hand-off would cost more than the work.
    """
    if region is None:
        return hits
    if mode != "exact" or reader is None:
        return apply_containment(hits, region, mode, relation, reader=reader)

    def _work() -> list[dict]:
        region.load_geometry(reader)  # no-op for bounds-built regions
        return apply_containment(hits, region, mode, relation, reader=reader)

    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Scope reporting (place#144) — shared by /api/search and /api/reconcile
# ---------------------------------------------------------------------------
#
# Both endpoints must answer the same two questions the same way: *was* the
# requested geographic scope applied, and *how*. Until 2026-08-31 only
# /api/reconcile could — /api/search had no `scope` field at all and answered
# an unresolvable `contained_in` GLOBALLY, so a client with a typo'd or stale
# place id got a confident unscoped answer that looked scoped
# (HANDOVER-2026-08-31 §2b). The builder lives here, next to `resolve_region`
# whose outcome it describes, so the two endpoints cannot drift apart again.

class ScopeInfo(BaseModel):
    """How the requested geographic scope was actually applied.

    Present whenever ``contained_in`` / ``bounds`` (or a radial ``lat``/``lng``/
    ``radius``) was sent, so the client can warn the user instead of trusting a
    scope the gateway could not honour verbatim (place#144). ``applied=False``
    means **no** spatial constraint could be built — the request is failed
    closed (no hits) rather than answered with an unscoped result set.
    """
    requested: bool = False
    applied: bool = False
    mode: str = "none"                     # polygon | linked-polygon | geojson
                                           # | bbox | none
    approximate: bool = False              # True when the constraint is coarser
                                           # than the geometry that was asked for
    containers_polygon: list[str] = []     # containers that contributed a real polygon
    containers_linked: list[str] = []      # co-referent (sameAs) places whose polygon was
                                           # borrowed because the container had none
    containers_approximated: list[str] = []  # point-only containers (buffered, or ignored
                                             # when a polygon container was also given)
    containers_unresolved: list[str] = []  # requested ids with no usable geometry at all
    message: Optional[str] = None


def scope_message(region: ResolvedRegion) -> Optional[str]:
    """Human-readable note on any way the region departs from what was asked."""
    if region.source == "linked-polygon":
        return (
            "No container has its own polygon; scope taken from the boundary of "
            f"{', '.join(region.linked_ids) or 'a co-referent place'} "
            "(sameAs/exactMatch)."
        )
    if region.point_ids:
        return (f"{len(region.point_ids)} point-only container(s) ignored — the "
                f"scope is the union of the polygon containers.")
    return None


#: Wording for a scope that was applied, but not at the precision asked for.
_DEGRADED_MESSAGE = (
    "`containment=exact` was requested but no polygon geometry was available "
    "for the region, so the H3 (fuzzy) cell test was used instead — results are "
    "cell-accurate, not boundary-accurate."
)


def exact_degraded(region: Optional[ResolvedRegion], containment: str) -> bool:
    """True when an ``exact`` request was in fact answered by the fuzzy test.

    ``hit_matches`` falls through to the H3 branch whenever no prepared geometry
    is available — a geom-store miss, an unavailable reader, no Shapely, or a
    region (``h3-disc``) that deliberately never builds a polygon. That
    fallback is the right behaviour; being silent about it is not. Before the
    ``un`` polygons were merged on 31 Aug 2026, ``contained_in: ["un:fra"]``
    with ``containment=exact`` returned the *fuzzy* answer — and said
    ``applied: true, mode: "polygon"``, which was true and still misleading,
    because a polygon region had been built and only its geometry was missing.

    Must be called AFTER the containment pass: geometry loads lazily, so before
    it there is nothing to have failed.
    """
    return (containment == "exact"
            and region is not None
            and region.union is None)


def mark_scope_degraded(scope: Optional[ScopeInfo]) -> None:
    """Record on ``scope`` that the constraint applied was coarser than asked.

    ``approximate`` already means exactly this, and it is the one thing a client
    cannot detect for itself: the hit count looks plausible either way.
    """
    if scope is None or not scope.applied:
        return
    scope.approximate = True
    scope.message = (f"{scope.message} {_DEGRADED_MESSAGE}".strip()
                     if scope.message else _DEGRADED_MESSAGE)


def build_scope_info(
    *,
    region: Optional[ResolvedRegion],
    contained_in: Optional[list[str]] = None,
    bounds: Optional[dict] = None,
    radial: bool = False,
) -> Optional[ScopeInfo]:
    """Describe how the requested geographic scope was applied.

    Returns ``None`` when no scope was requested (the response is then
    byte-identical to the pre-place#144 shape). Otherwise the caller MUST
    honour ``applied=False`` by refusing to answer with unscoped results.
    """
    if not contained_in and not bounds and not radial:
        return None

    if region is not None:
        return ScopeInfo(
            requested=True,
            applied=True,
            mode=region.source,
            containers_polygon=list(region.area_ids),
            containers_linked=list(region.linked_ids),
            containers_approximated=list(region.point_ids),
            containers_unresolved=list(region.unresolved_ids),
            message=scope_message(region),
        )

    if bounds and _has_geometries(bounds):
        # region_from_geojson failed (Shapely unavailable / unsupported shape) but
        # build_places_filter still applies its degenerate `repr_point ∈ bounds`
        # gate, so the query IS constrained — just coarsely and without a refine.
        return ScopeInfo(
            requested=True, applied=True, mode="bbox", approximate=True,
            message="Bounds could not be resolved into a containment region; "
                    "applied a coarse repr_point-in-bounds filter instead.",
        )

    return ScopeInfo(
        requested=True,
        applied=False,
        mode="none",
        containers_unresolved=[
            _strip_place_prefix(p) for p in (contained_in or []) if p],
        message=(
            "None of the containment place_ids resolved to a usable geometry "
            "(not even a representative point), so the requested scope could not "
            "be applied. No results are returned rather than unscoped ones."
            if contained_in else
            "The supplied bounds contained no usable geometry, so the requested "
            "scope could not be applied."
        ),
    )
