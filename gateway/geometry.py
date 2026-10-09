# gateway/geometry.py
"""
``GET /api/geometry/{place_id}`` — one place's authoritative geometry, read
from the geom store rather than reassembled from vector-tile fragments.

Why (developer/plan-atlas-data-architecture.md §5.3). The Atlas turns a
selected boundary into a search constraint by collecting that feature's tile
fragments and unioning them client-side (place#156). That finds only the
fragments in *loaded* tiles — a region larger than the viewport is silently
truncated — and the geometry is whatever simplification the current zoom
happens to carry. The full polygon has been readable from the gateway host
since place#165 put the store index into SQLite (gateway RSS +3.8 MB, not
+5.4 GB), so it can simply be served.

What this returns. The union of every stored geometry of the place (a place
may carry several — ``geom_ref`` keys are ``"<place_id>_<geometry_index>"``),
as one GeoJSON geometry, with its bounds and the vertex count. Point-only
places have nothing in the store and are a 404 with ``error: "no geometry"``,
which is a different answer from ``error: "not found"`` (no such place).

Size bound. Some stored polygons run to hundreds of thousands of vertices.
The response is capped at ``max_bytes`` (default ``GEOMETRY_MAX_BYTES``,
1 MB; ceiling 5 MB): a geometry over the cap is simplified with
Douglas–Peucker (topology-preserving), starting at 1/65536 of its own extent
(sub-metre for anything smaller than a county) and doubling until it fits,
so the result is the least-simplified geometry under the cap rather than
the first one that happened to fit; the response says so (``simplified``,
``tolerance``). Measured locally on a synthetic 200k-vertex, 7.7 MB
polygon: 0.9 s to a first-step fit (a smooth shape loses nearly everything
at any tolerance; a real coastline will take more doublings and more
time). A caller may also ask for a tolerance directly.
Coordinates are rounded to 6 decimals (~0.1 m) in either case. A geometry
that cannot be brought under the cap in 20 doublings is a 413 carrying its
bounds, which has not been observed and would mean the cap is set absurdly
low.

Licence. Geometry is source content, so this is a redistribution surface in
exactly the sense of place#269: an authority whose terms do not let WHG
re-serve its records (``redistributable: False`` in
``processing.settings.AUTHORITIES`` — kain_par, nl, chgis as of 2026-07-22)
gets a 451 naming the source and rights holder, before Elasticsearch or the
store is touched. Indexing, search and reconciliation remain permitted for
every authority; handing a complete, full-precision polygon to a browser as
JSON is not any of those. The whg3 proxy applies the same determination from
its registry copy, so the two cannot disagree without one of them being
stale — and if they do, the refusal wins.

Fields are DECLARED on ``GeometryResponse``: FastAPI drops anything the
response model does not name (reference_gateway_response_models).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from functools import lru_cache
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import spatial
from .config import ES_BACKEND, PLACES_INDEX
from .es_helpers import ES_HEADERS, es_auth

try:
    from shapely import wkb as _wkb
    from shapely.geometry import mapping as _mapping
    from shapely.ops import unary_union as _unary_union
    _SHAPELY_AVAILABLE = True
except Exception:  # pragma: no cover
    _SHAPELY_AVAILABLE = False

logger = logging.getLogger("gateway.geometry")

router = APIRouter(prefix="/api", tags=["Geometry"])

# Size bound on the serialised geometry (bytes of compact JSON). The default
# is an environment setting so the gateway host can tune it without a deploy;
# the ceiling is a hard limit on what a caller may ask for.
GEOMETRY_MAX_BYTES_DEFAULT = int(os.getenv("GEOMETRY_MAX_BYTES", "1000000"))
GEOMETRY_MAX_BYTES_CEILING = 5_000_000
GEOMETRY_MAX_BYTES_FLOOR = 10_000
# Automatic simplification starts at this fraction of the geometry's own
# extent and doubles the tolerance up to this many times.
_AUTO_TOLERANCE_DIVISOR = 65536
_AUTO_TOLERANCE_STEPS = 20
COORD_DECIMALS = 6

_ENTITY_PREFIX = "place:"

_ES_SOURCE = [
    "place_id",
    "namespace",
    "geometries.geometry_index",
    "geometries.geom_class",
    "geometries.has_geom",
]


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

class GeometryResponse(BaseModel):
    place_id: str
    namespace: str = ""
    geometry: dict = Field(description="GeoJSON geometry: the union of the place's stored geometries")
    geometry_count: int = Field(description="How many stored geometries were merged")
    bounds: Optional[list[float]] = Field(None, description="[west, south, east, north] of the returned geometry")
    vertex_count: int = Field(description="Coordinate pairs in the returned geometry")
    bytes: int = Field(description="Size of the compact-JSON geometry")
    max_bytes: int = Field(description="The cap this response was bounded to")
    simplified: bool = Field(description="Whether the geometry was simplified to fit, or at the caller's request")
    tolerance: Optional[float] = Field(None, description="Douglas–Peucker tolerance applied (degrees), if any")
    source: str = Field("geom-store", description="Where the geometry came from")


# ---------------------------------------------------------------------------
# Licence determination (processing.settings is the source of truth)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _authorities_by_namespace() -> dict[str, dict]:
    """``{namespace: authority dict}`` from ``processing.settings.AUTHORITIES``.

    Lazy and cached: the gateway only imports ``processing.settings`` on the
    paths that need it (``spatial.get_geom_reader`` does the same). A failure
    to import is raised, not swallowed — without the determination the
    endpoint must not serve, and the store would be unreachable through the
    same import anyway.
    """
    from processing.settings import AUTHORITIES
    return {a["namespace"]: a for a in AUTHORITIES if a.get("namespace")}


def non_redistributable_namespaces() -> frozenset[str]:
    """Namespaces whose authority says ``redistributable: False``.

    An unset flag means True (the convention ``processing.settings`` states
    and the inventory push applies), so only an explicit False withholds.
    """
    return frozenset(
        ns for ns, a in _authorities_by_namespace().items()
        if a.get("redistributable", True) is False
    )


def _withheld_detail(namespace: str, place_id: str) -> dict:
    a = _authorities_by_namespace().get(namespace, {})
    name = a.get("dataset_name") or namespace
    return {
        "error": "source not redistributable",
        "detail": (f"{name} is indexed and searchable through WHG, but its terms do not "
                   f"permit WHG to redistribute its records, and geometry is source "
                   f"content. Obtain the data from the source under its own terms."),
        "id": place_id,
        "namespace": namespace,
        "source": {
            "name": a.get("dataset_name"),
            "rights_holder": a.get("rights_holder"),
            "source_url": a.get("source_url"),
            "license": a.get("license_spdx"),
        },
    }


# ---------------------------------------------------------------------------
# Geometry assembly (runs in a worker thread — SQLite, pread, Shapely, json)
# ---------------------------------------------------------------------------

def _round_coords(coords: Any, nd: int = COORD_DECIMALS) -> Any:
    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (int, float)):
            return [round(float(c), nd) for c in coords]
        return [_round_coords(c, nd) for c in coords]
    return coords


def _vertex_count(coords: Any) -> int:
    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (int, float)):
            return 1
        return sum(_vertex_count(c) for c in coords)
    return 0


def vertex_count(gj: dict) -> int:
    """Coordinate pairs in a GeoJSON geometry, collections included."""
    if gj.get("type") == "GeometryCollection":
        return sum(vertex_count(g) for g in gj.get("geometries", []))
    return _vertex_count(gj.get("coordinates", []))


def _geojson(geom) -> dict:
    gj = _mapping(geom)
    out: dict = {"type": gj["type"]}
    if gj["type"] == "GeometryCollection":
        out["geometries"] = [_geojson(g) for g in geom.geoms]
    else:
        out["coordinates"] = _round_coords(gj["coordinates"])
    return out


def _size(gj: dict) -> int:
    return len(json.dumps(gj, separators=(",", ":")))


def bound_geometry(geom, tolerance: Optional[float], max_bytes: int) -> tuple[dict, bool, Optional[float], int]:
    """Serialise *geom* within *max_bytes*, simplifying if it must.

    Returns ``(geojson, simplified, tolerance, bytes)``. Raises
    ``HTTPException(413)`` if the geometry cannot be brought under the cap.
    """
    simplified = False
    tol = tolerance if tolerance else None
    if tol:
        geom = geom.simplify(tol, preserve_topology=True)
        simplified = True
    gj = _geojson(geom)
    size = _size(gj)
    if size <= max_bytes:
        return gj, simplified, tol, size

    minx, miny, maxx, maxy = geom.bounds
    span = max(maxx - minx, maxy - miny) or 1e-6
    tol = tol or span / _AUTO_TOLERANCE_DIVISOR
    for _ in range(_AUTO_TOLERANCE_STEPS):
        cand = geom.simplify(tol, preserve_topology=True)
        if cand.is_empty:
            break
        gj = _geojson(cand)
        size = _size(gj)
        if size <= max_bytes:
            return gj, True, tol, size
        tol *= 2
    raise HTTPException(status_code=413, detail={
        "error": "geometry too large",
        "detail": (f"The geometry could not be simplified under {max_bytes} bytes "
                   f"(last attempt {size} bytes at tolerance {tol:g})."),
        "bounds": [minx, miny, maxx, maxy],
        "max_bytes": max_bytes,
    })


def _assemble(reader, place_id: str, keys: list[str],
              tolerance: Optional[float], max_bytes: int) -> Optional[dict]:
    """Read, union and bound. Returns the response payload, or None when the
    store holds nothing for any key (a located-but-point-only place)."""
    shapes = []
    for key in keys:
        try:
            raw = reader.get_wkb(key)
        except Exception as exc:  # noqa: BLE001 — one bad key must not hide the rest
            logger.warning("geom-store read failed for %s: %s", key, exc)
            raw = None
        if not raw:
            continue
        try:
            shp = _wkb.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("geom-store WKB unreadable for %s: %s", key, exc)
            continue
        if shp is not None and not shp.is_empty:
            shapes.append(shp)
    if not shapes:
        return None
    geom = _unary_union(shapes) if len(shapes) > 1 else shapes[0]
    gj, simplified, tol, size = bound_geometry(geom, tolerance, max_bytes)
    minx, miny, maxx, maxy = geom.bounds
    return {
        "geometry": gj,
        "geometry_count": len(shapes),
        "bounds": [round(minx, COORD_DECIMALS), round(miny, COORD_DECIMALS),
                   round(maxx, COORD_DECIMALS), round(maxy, COORD_DECIMALS)],
        "vertex_count": vertex_count(gj),
        "bytes": size,
        "max_bytes": max_bytes,
        "simplified": simplified,
        "tolerance": tol,
    }


def geom_keys_for(src: dict) -> list[str]:
    """The store keys a place ``_source`` can be expected to have.

    ``has_geom: False`` entries are points carried inline as ``repr_point``;
    the store holds nothing for them, so they are not asked for.
    """
    pid = src.get("place_id")
    keys: list[str] = []
    if not pid:
        return keys
    for idx, g in enumerate(src.get("geometries", []) or []):
        if not isinstance(g, dict):
            continue
        if g.get("has_geom") is False:
            continue
        keys.append(f"{pid}_{g.get('geometry_index', idx)}")
    return keys


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.get("/geometry/{place_id}", response_model=GeometryResponse)
async def place_geometry(
    place_id: str,
    tolerance: Optional[float] = Query(
        None, ge=0.0, le=10.0,
        description="Douglas–Peucker tolerance in degrees; omit to receive the full "
                    "geometry when it fits under max_bytes",
    ),
    max_bytes: int = Query(
        GEOMETRY_MAX_BYTES_DEFAULT, ge=GEOMETRY_MAX_BYTES_FLOOR, le=GEOMETRY_MAX_BYTES_CEILING,
        description="Response size cap on the serialised geometry",
    ),
):
    """One place's stored geometry as GeoJSON (see the module docstring).

    Examples:
      /api/geometry/osm:r62149
      /api/geometry/ohm:r2660219?max_bytes=200000
      /api/geometry/clio:1234?tolerance=0.01

    404 ``error: "not found"`` for an unknown place; 404 ``error: "no geometry"``
    for a place the store has nothing for (point-only); 451 for an authority
    WHG may not redistribute; 503 when the geom store is unavailable.
    """
    if not _SHAPELY_AVAILABLE:  # pragma: no cover
        raise HTTPException(status_code=503, detail={"error": "shapely unavailable"})
    pid = place_id[len(_ENTITY_PREFIX):] if place_id.startswith(_ENTITY_PREFIX) else place_id
    pid = pid.strip()
    if not pid or ":" not in pid:
        raise HTTPException(status_code=422, detail={"error": "place_id must be namespaced, e.g. osm:r62149"})
    namespace = pid.split(":", 1)[0]

    # Licence first: cheaper than ES, and a withheld source leaks nothing — not
    # even whether the id exists.
    try:
        withheld = non_redistributable_namespaces()
    except Exception as exc:  # noqa: BLE001
        logger.error("licence determination unavailable: %s", exc)
        raise HTTPException(status_code=503, detail={"error": "licence determination unavailable"})
    if namespace in withheld:
        raise HTTPException(status_code=451, detail=_withheld_detail(namespace, pid))

    reader = spatial.get_geom_reader()
    if reader is None:
        raise HTTPException(status_code=503, detail={"error": "geometry store unavailable"})

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{ES_BACKEND}/{PLACES_INDEX}/_search",
                json={"size": 1, "query": {"term": {"place_id": pid}}, "_source": _ES_SOURCE},
                auth=es_auth(), headers=ES_HEADERS,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
    except httpx.HTTPError as exc:
        logger.warning("ES lookup failed for %s: %s", pid, exc)
        raise HTTPException(status_code=502, detail={"error": "index unavailable"})
    if not hits:
        raise HTTPException(status_code=404, detail={"error": "not found", "id": pid})
    src = hits[0].get("_source", {})
    keys = geom_keys_for(src)
    payload = None
    if keys:
        payload = await asyncio.to_thread(_assemble, reader, pid, keys, tolerance, max_bytes)
    if payload is None:
        raise HTTPException(status_code=404, detail={
            "error": "no geometry", "id": pid,
            "detail": "The index locates this place by a point only; no polygon or line is stored for it.",
        })
    return GeometryResponse(place_id=pid, namespace=src.get("namespace") or namespace, **payload)
