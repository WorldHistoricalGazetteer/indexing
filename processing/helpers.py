# processing/helpers.py

"""
Geospatial helper functions using GEOS (via Shapely) for accurate computations.

This module provides geodetically-correct operations that account for Earth's
spherical geometry, unlike simple arithmetic means which cause distortion at
high latitudes.

**Coordinate precision convention:** All coordinates are rounded to 6 decimal
places (~0.11 m) at ingestion time per RFC 7946, via ``round_coordinates()``
and ``enrich_geometry()``.  This mitigates storage bloat from the excessive
pseudo-precision common in upstream data sources (Wikidata often has 10+ digits,
OSM PBF stores 7).  The constant ``COORDINATE_PRECISION`` controls the number
of decimal places.
"""

from shapely.geometry import shape, Point, LineString, Polygon, MultiPoint, MultiLineString, MultiPolygon, \
    GeometryCollection
from shapely.ops import transform
from shapely.validation import make_valid as shapely_make_valid
from pyproj import Transformer, CRS
from typing import Iterable
import json
import logging
import math
from collections import Counter
from functools import lru_cache

try:
    import h3 as _h3
    _H3_AVAILABLE = True
except ImportError:
    _H3_AVAILABLE = False

# H3 reference resolution for h3_centroid (≈1.2 km hexagon edge)
H3_CENTROID_RESOLUTION = 7
# Maximum cells produced by polyfill before dropping to a coarser resolution
H3_POLYFILL_MAX_CELLS = 10_000

# ``h3shape_to_cells`` runtime scales with the polygon's *vertex* count as well
# as its area, so a hugely detailed boundary (full-resolution OSM coastline with
# 10⁵–10⁶ vertices) can take minutes even when the cell count is bounded. Above
# this many vertices we first simplify the polygon to a tolerance comparable to
# the target resolution's edge length — the h3_cover is a coarse fuzzy prefilter,
# so dropping sub-cell detail does not change the resulting cell set materially.
H3_SIMPLIFY_VERTEX_THRESHOLD = 5_000
# Douglas–Peucker tolerance (degrees) ≈ half the H3 edge length at each res.
_H3_SIMPLIFY_TOL_DEG = {3: 0.25, 4: 0.10, 5: 0.04, 6: 0.015, 7: 0.005}


def _count_vertices(geojson_geom: dict) -> int:
    """Cheaply count coordinate pairs in a GeoJSON geometry (no Shapely)."""
    n = 0
    stack = [geojson_geom.get("coordinates")]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            if node and isinstance(node[0], (int, float)):
                n += 1
            else:
                stack.extend(node)
    return n


# A feature whose bounding box is smaller than ~one r7 cell (≈1.2 km / 0.01°)
# cannot produce a multi-cell h3_cover — its cover is just the centroid cell.
# Callers can therefore skip loading the full polygon and polyfilling it, and
# emit the centroid-only cover directly (output-identical, far cheaper). This is
# what spares ingestion / remediation from reading the millions of buildings,
# POIs and small ways whose covers were never wrong.
H3_SUBCELL_BBOX_DEG = 0.01


def bbox_maxdim_deg(bounds) -> float | None:
    """Largest bounding-box side in degrees from a stored ``[w, s, e, n]``.

    Returns ``None`` when bounds are absent/malformed or antimeridian-spanning
    (``w > e``) — i.e. "unknown / treat as large" — so the caller does not skip.
    """
    if not (isinstance(bounds, (list, tuple)) and len(bounds) == 4):
        return None
    try:
        w, s, e, n = (float(x) for x in bounds)
    except (TypeError, ValueError):
        return None
    dx = e - w
    if dx < 0:  # crosses the antimeridian → not sub-cell
        return None
    return max(dx, n - s)


def geojson_to_shapely(geojson_geom):
    """
    Convert GeoJSON geometry dict to Shapely geometry object.

    Args:
        geojson_geom: Dict with GeoJSON geometry (type, coordinates)

    Returns:
        Shapely geometry object or None
    """
    if not geojson_geom or not isinstance(geojson_geom, dict):
        return None

    try:
        return shape(geojson_geom)
    except Exception as e:
        # Try stripping Z-coordinates if present
        try:
            geom_2d = strip_z_coordinates(geojson_geom)
            if geom_2d:
                return shape(geom_2d)
        except:
            pass
        print(f"Error converting GeoJSON to Shapely: {e}")
        return None


def strip_z_coordinates(geojson_geom):
    """
    Remove Z-coordinates from a GeoJSON geometry.

    Args:
        geojson_geom: Dict with GeoJSON geometry

    Returns:
        New geometry dict with only lon,lat coordinates
    """
    if not geojson_geom:
        return None

    geom_type = geojson_geom.get('type')
    coords = geojson_geom.get('coordinates')

    if not coords:
        return None

    def strip_coord(c):
        """Strip a single coordinate to 2D."""
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            return [c[0], c[1]]
        return c

    def strip_coords_recursive(coords, depth):
        """Recursively strip coordinates based on geometry type."""
        if depth == 0:
            return strip_coord(coords)
        return [strip_coords_recursive(c, depth - 1) for c in coords]

    # Determine nesting depth for each geometry type
    depth_map = {
        'Point': 0,
        'LineString': 1,
        'Polygon': 2,
        'MultiPoint': 1,
        'MultiLineString': 2,
        'MultiPolygon': 3
    }

    if geom_type in depth_map:
        new_coords = strip_coords_recursive(coords, depth_map[geom_type])
        return {
            'type': geom_type,
            'coordinates': new_coords
        }
    elif geom_type == 'GeometryCollection':
        geometries = geojson_geom.get('geometries', [])
        new_geometries = [strip_z_coordinates(g) for g in geometries]
        return {
            'type': 'GeometryCollection',
            'geometries': [g for g in new_geometries if g]
        }

    return None


# Default coordinate precision (decimal places).
# 6 dp ≈ 0.11 m — matches RFC 7946 recommendation and exceeds the native
# accuracy of every source authority.  Applied to all coordinates at ingestion
# time to mitigate storage bloat from pseudo-precision.
COORDINATE_PRECISION = 6


def wrap_longitude(lon):
    """Fold a longitude into [-180, 180], the only range Elasticsearch accepts.

    Sources do not agree on convention. Wikidata carries some coordinates in
    [0, 360] (``351.83`` for -8.17), some already-shifted past the antimeridian
    (``-236.4`` for 123.6), and a Native Land treaty polygon straddling the
    dateline yields a representative point at ``-186.05``. ES rejects the
    **whole document** on any of them — ``illegal longitude value [351.83] for
    repr_point`` — which cost 3,636 ``wd`` docs and one ``nl`` doc on the
    place#164 rebuild.

    Applied to the *representative point* only. A ring's vertices must NOT be
    wrapped individually: doing so tears a polygon that crosses the dateline
    into a globe-spanning artefact. That case has its own machinery
    (``split_at_antimeridian``), and the full geometry lives in the geom store
    rather than in ES anyway — ``repr_point`` and ``bounds`` are what ES sees.
    """
    if not isinstance(lon, (int, float)):
        return lon
    # ((lon + 180) mod 360) - 180, with the +180 boundary preserved rather than
    # folded to -180, since a point exactly on the dateline is legitimate.
    wrapped = (lon + 180.0) % 360.0 - 180.0
    if wrapped == -180.0 and lon > 0:
        return 180.0
    return wrapped


def round_coordinates(geojson_geom, precision=COORDINATE_PRECISION):
    """
    Round all coordinates in a GeoJSON geometry to *precision* decimal places.

    This is pure coordinate truncation (not topological simplification) and is
    safe for any geometry type.  At 6 dp the maximum positional shift is
    ~0.06 m — well below the accuracy of any source authority.

    Optimised for the two dominant geometry types in the index:
    - Point (~80% of 47M records): direct round, no recursion
    - Polygon/MultiPolygon: tight loops, no per-coord function call overhead

    Args:
        geojson_geom: Dict with GeoJSON geometry (type, coordinates)
        precision:    Number of decimal places (default: 6)

    Returns:
        New geometry dict with rounded coordinates, or None on bad input
    """
    if not geojson_geom:
        return None

    geom_type = geojson_geom.get('type')
    coords = geojson_geom.get('coordinates')

    if not geom_type:
        return None

    _round = round  # local binding avoids global lookup in tight loops

    # ── Fast path: Point (vast majority of records) ─────────────────
    if geom_type == 'Point':
        if coords and len(coords) >= 2:
            return {'type': 'Point',
                    'coordinates': [_round(coords[0], precision),
                                    _round(coords[1], precision)]}
        return geojson_geom

    # ── Fast path: Polygon ──────────────────────────────────────────
    if geom_type == 'Polygon':
        if not coords:
            return geojson_geom
        return {'type': 'Polygon',
                'coordinates': [
                    [[_round(c[0], precision), _round(c[1], precision)]
                     for c in ring]
                    for ring in coords
                ]}

    # ── Fast path: MultiPolygon ─────────────────────────────────────
    if geom_type == 'MultiPolygon':
        if not coords:
            return geojson_geom
        return {'type': 'MultiPolygon',
                'coordinates': [
                    [[
                        [_round(c[0], precision), _round(c[1], precision)]
                        for c in ring
                    ] for ring in poly]
                    for poly in coords
                ]}

    # ── Generic recursive fallback for remaining types ──────────────
    def round_recursive(coords, depth):
        if depth == 0:
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                return [_round(coords[0], precision),
                        _round(coords[1], precision)]
            return coords
        return [round_recursive(c, depth - 1) for c in coords]

    depth_map = {
        'LineString': 1,
        'MultiPoint': 1,
        'MultiLineString': 2,
    }

    if geom_type in depth_map:
        if coords is None:
            return None
        return {
            'type': geom_type,
            'coordinates': round_recursive(coords, depth_map[geom_type]),
        }
    elif geom_type == 'GeometryCollection':
        geometries = geojson_geom.get('geometries', [])
        new_geometries = [round_coordinates(g, precision) for g in geometries]
        return {
            'type': 'GeometryCollection',
            'geometries': [g for g in new_geometries if g],
        }

    return None


def compute_geodetic_centroid(geojson_geom):
    """
    Compute geodetically-correct centroid of a GeoJSON geometry.

    This function:
    1. Converts GeoJSON to Shapely geometry
    2. Projects from WGS84 (EPSG:4326) to Equal Area projection (EPSG:6933)
    3. Computes centroid in projected coordinates
    4. Projects back to WGS84

    The Equal Area projection (Cylindrical Equal Area) minimizes distortion
    for area-based operations globally, making it suitable for computing
    centroids of features at any latitude.

    Args:
        geojson_geom: Dict with GeoJSON geometry

    Returns:
        Dict with {'lon': float, 'lat': float} or None
    """
    if not geojson_geom:
        return None

    try:
        # Convert to Shapely
        geom = geojson_to_shapely(geojson_geom)
        if not geom or geom.is_empty:
            return None

        # Define transformers
        # WGS84 (lon/lat) to World Cylindrical Equal Area (meters)
        wgs84_to_equal_area = Transformer.from_crs(
            CRS.from_epsg(4326),  # WGS84
            CRS.from_epsg(6933),  # World Cylindrical Equal Area
            always_xy=True
        )

        # Equal Area back to WGS84
        equal_area_to_wgs84 = Transformer.from_crs(
            CRS.from_epsg(6933),
            CRS.from_epsg(4326),
            always_xy=True
        )

        # Transform to Equal Area projection
        geom_projected = transform(wgs84_to_equal_area.transform, geom)

        # Compute centroid in projected coordinates
        centroid_projected = geom_projected.centroid

        # Transform centroid back to WGS84
        centroid_wgs84 = transform(equal_area_to_wgs84.transform, centroid_projected)

        return {
            'lon': centroid_wgs84.x,
            'lat': centroid_wgs84.y
        }

    except Exception as e:
        print(f"Error computing geodetic centroid: {e}")
        return None


def _representative_from_collection(gc: GeometryCollection):
    """
    Select a meaningful geometry from a GeometryCollection
    and return its representative point.
    """
    if gc.is_empty:
        return None

    # Prefer polygons (by area)
    polygons = []
    lines = []
    points = []

    for g in gc.geoms:
        if g.is_empty:
            continue
        if isinstance(g, (Polygon, MultiPolygon)):
            polygons.append(g)
        elif isinstance(g, (LineString, MultiLineString)):
            lines.append(g)
        elif isinstance(g, (Point, MultiPoint)):
            points.append(g)

    if polygons:
        geom = max(polygons, key=lambda g: g.area)
        return geom.representative_point()

    if lines:
        geom = max(lines, key=lambda g: g.length)
        return geom.representative_point()

    if points:
        # Deterministic: first point
        geom = points[0]
        return geom.representative_point()

    return None


def compute_representative_point(geojson_geom):
    """
    Compute a representative point guaranteed to be within the geometry.

    For complex or concave polygons, the centroid may fall outside the shape.
    This function returns a point guaranteed to be inside.

    Args:
        geojson_geom: Dict with GeoJSON geometry

    Returns:
        Dict with {'lon': float, 'lat': float} or None
    """
    if not geojson_geom:
        return None

    try:
        geom = geojson_to_shapely(geojson_geom)
        if not geom or geom.is_empty:
            return None

        if isinstance(geom, Point):
            return {'lon': round(geom.x, COORDINATE_PRECISION),
                    'lat': round(geom.y, COORDINATE_PRECISION)}

        if isinstance(geom, GeometryCollection):
            rp = _representative_from_collection(geom)
            if rp is None:
                return None
            return {'lon': round(rp.x, COORDINATE_PRECISION),
                    'lat': round(rp.y, COORDINATE_PRECISION)}

        rep_point = geom.representative_point()
        return {'lon': round(rep_point.x, COORDINATE_PRECISION),
                'lat': round(rep_point.y, COORDINATE_PRECISION)}

    except Exception as e:
        print(f"Error computing representative point: {e}")
        return None


def compute_bbox(geojson_geom):
    """
    Compute bounding box of a geometry.

    Args:
        geojson_geom: Dict with GeoJSON geometry

    Returns:
        Dict with {'minlon': float, 'minlat': float, 'maxlon': float, 'maxlat': float} or None
    """
    if not geojson_geom:
        return None

    try:
        geom = geojson_to_shapely(geojson_geom)
        if not geom or geom.is_empty:
            return None

        bounds = geom.bounds  # (minx, miny, maxx, maxy)

        return {
            'minlon': bounds[0],
            'minlat': bounds[1],
            'maxlon': bounds[2],
            'maxlat': bounds[3]
        }

    except Exception as e:
        print(f"Error computing bounding box: {e}")
        return None


def compute_area_km2(geojson_geom):
    """
    Compute geodetic area of a polygon in square kilometers.

    Uses Equal Area projection for accurate area calculation.

    Args:
        geojson_geom: Dict with GeoJSON geometry (Polygon or MultiPolygon)

    Returns:
        Float area in km² or None
    """
    if not geojson_geom:
        return None

    try:
        geom = geojson_to_shapely(geojson_geom)
        if not geom or geom.is_empty:
            return None

        # Only compute area for Polygons
        if not isinstance(geom, (Polygon, MultiPolygon)):
            return None

        # Transform to Equal Area projection
        wgs84_to_equal_area = Transformer.from_crs(
            CRS.from_epsg(4326),
            CRS.from_epsg(6933),
            always_xy=True
        )

        geom_projected = transform(wgs84_to_equal_area.transform, geom)

        # Area in square meters, convert to km²
        area_m2 = geom_projected.area
        area_km2 = area_m2 / 1_000_000

        return area_km2

    except Exception as e:
        print(f"Error computing area: {e}")
        return None


def compute_length_km(geojson_geom):
    """
    Compute geodetic length of a line in kilometers.

    Uses Equal Area projection for accurate length calculation.

    Args:
        geojson_geom: Dict with GeoJSON geometry (LineString or MultiLineString)

    Returns:
        Float length in km or None
    """
    if not geojson_geom:
        return None

    try:
        geom = geojson_to_shapely(geojson_geom)
        if not geom or geom.is_empty:
            return None

        # Only compute length for Lines
        if not isinstance(geom, (LineString, MultiLineString)):
            return None

        # Transform to Equal Area projection
        wgs84_to_equal_area = Transformer.from_crs(
            CRS.from_epsg(4326),
            CRS.from_epsg(6933),
            always_xy=True
        )

        geom_projected = transform(wgs84_to_equal_area.transform, geom)

        # Length in meters, convert to km
        length_m = geom_projected.length
        length_km = length_m / 1_000

        return length_km

    except Exception as e:
        print(f"Error computing length: {e}")
        return None


def simplify_geometry(geojson_geom, tolerance_km=1.0):
    """
    Simplify a geometry for faster storage/rendering.

    Uses Douglas-Peucker algorithm in Equal Area projection.

    Args:
        geojson_geom: Dict with GeoJSON geometry
        tolerance_km: Simplification tolerance in kilometers (default 1km)

    Returns:
        Simplified GeoJSON geometry dict or None
    """
    if not geojson_geom:
        return None

    try:
        geom = geojson_to_shapely(geojson_geom)
        if not geom or geom.is_empty:
            return None

        # Don't simplify points
        if isinstance(geom, Point):
            return geojson_geom

        # Transform to Equal Area projection
        wgs84_to_equal_area = Transformer.from_crs(
            CRS.from_epsg(4326),
            CRS.from_epsg(6933),
            always_xy=True
        )

        equal_area_to_wgs84 = Transformer.from_crs(
            CRS.from_epsg(6933),
            CRS.from_epsg(4326),
            always_xy=True
        )

        # Project, simplify, project back
        geom_projected = transform(wgs84_to_equal_area.transform, geom)
        tolerance_m = tolerance_km * 1000
        geom_simplified = geom_projected.simplify(tolerance_m, preserve_topology=True)
        geom_wgs84 = transform(equal_area_to_wgs84.transform, geom_simplified)

        # Convert back to GeoJSON dict
        return json.loads(json.dumps(geom_wgs84.__geo_interface__))

    except Exception as e:
        print(f"Error simplifying geometry: {e}")
        return None


def validate_geometry(geojson_geom):
    """
    Validate and attempt to fix a geometry.

    Args:
        geojson_geom: Dict with GeoJSON geometry

    Returns:
        Tuple of (is_valid: bool, fixed_geometry: dict or None, error_message: str or None)
    """
    if not geojson_geom:
        return (False, None, "No geometry provided")

    try:
        geom = geojson_to_shapely(geojson_geom)
        if not geom:
            return (False, None, "Could not parse geometry")

        if geom.is_valid:
            return (True, geojson_geom, None)

        # Attempt to fix with buffer(0) trick
        fixed_geom = geom.buffer(0)

        if fixed_geom.is_valid:
            fixed_geojson = json.loads(json.dumps(fixed_geom.__geo_interface__))
            return (True, fixed_geojson, "Geometry was invalid but fixed")
        else:
            return (False, None, f"Invalid geometry: {geom.is_valid_reason}")

    except Exception as e:
        return (False, None, f"Error validating geometry: {str(e)}")


def select_h3_cover_geometry(geom_entry: dict | None, fallback_geom: dict | None = None):
    """
    Choose the geometry used for ``h3_cover`` computation.

    Prefer the already-computed convex hull from ``enrich_geometry()`` for
    area geometries. This keeps ``h3_centroid`` unchanged while making H3
    polyfill substantially cheaper for complex polygons. Falls back to the raw
    geometry whenever no suitable hull is available.
    """
    if not isinstance(fallback_geom, dict):
        return fallback_geom

    fallback_type = fallback_geom.get("type")
    if fallback_type not in {"Polygon", "MultiPolygon", "GeometryCollection"}:
        return fallback_geom

    if isinstance(geom_entry, dict):
        hull = geom_entry.get("hull")
        if isinstance(hull, dict) and hull.get("type") in {"Polygon", "MultiPolygon"}:
            return hull

    return fallback_geom


#: Why ``compute_h3_fields`` fell back to a centroid-only cover, and how often.
#: Keyed ``"{reason}:{geom_type}"``. **Print this at the end of any pipeline
#: run that computes covers** — a non-zero count means geometries are being
#: indexed with an ``h3_cover`` that does not describe them, and `h3_cover`
#: drives ``containment=fuzzy``, so those places are unfindable by scope.
#:
#: This exists because every path to that fallback used to be silent, three of
#: them behind bare ``except Exception: pass``. Both defects found by audit in
#: Sep 2026 — MultiPoint flattening (779 features) and the GeometryCollection
#: branch dropping non-polygon members — would have announced themselves here
#: on the first run, with no audit at all.
H3_COVER_FALLBACKS: Counter = Counter()

#: Log the first N occurrences of each distinct reason, then count silently.
#: The corpus is ~51 M places; an unbounded log would be its own outage.
_H3_FALLBACK_LOG_LIMIT = 5

_h3_log = logging.getLogger(__name__)


def _h3_fallback(reason: str, geom_type: str | None,
                 exc: BaseException | None = None) -> None:
    """Record — and, for the first few, announce — a centroid-only fallback."""
    key = f"{reason}:{geom_type or 'None'}"
    H3_COVER_FALLBACKS[key] += 1
    n = H3_COVER_FALLBACKS[key]
    if n > _H3_FALLBACK_LOG_LIMIT:
        return
    detail = f" ({type(exc).__name__}: {exc})" if exc is not None else ""
    tail = "  [further occurrences counted silently]" if n == _H3_FALLBACK_LOG_LIMIT else ""
    _h3_log.warning(
        "h3_cover fell back to centroid-only: %s geometry, reason=%s%s%s",
        geom_type or "unknown", reason, detail, tail,
    )


def _compact_mixed(cells) -> set:
    """``compact_cells`` over a possibly MIXED-RESOLUTION cell set.

    ``h3.compact_cells`` raises ``H3ResMismatchError`` when handed cells of
    differing resolutions, and ``_polyfill_adaptive`` legitimately produces
    such a set: for a dateline-crossing MultiPolygon it fills each member (and
    each antimeridian-split part) **independently**, and
    ``_polyfill_one_polygon`` picks its resolution from *that part's* bounding
    box. A multipolygon whose parts differ greatly in size — a colonial empire,
    a country with distant islands — therefore yields a union spanning
    resolutions, and compaction blew up on it.

    Compacting **within** each resolution and unioning is the right fix rather
    than normalising to one resolution: a mixed-resolution cover is the
    intended output (``schemas/field-notes.md`` documents ``h3_cover`` as a
    "compacted, multi-resolution set"), and flattening would either explode the
    cell count going finer or lose precision going coarser.

    Measured 3 Sep 2026: 5 live instances, all MultiPolygon, out of 145,764
    geometries — but it is a latent CLASS, not five one-offs. It silently
    degrades any large multi-resolution polygon, and the population grows with
    the corpus. It was invisible to a `computed != stored` census because the
    exception left `computed == stored`; it surfaced only because the fallback
    counter added in this module records the exception rather than swallowing it.
    """
    if not cells:
        return set()
    by_res: dict[int, list] = {}
    for c in cells:
        by_res.setdefault(_h3.get_resolution(c), []).append(c)
    if len(by_res) == 1:
        return set(_h3.compact_cells(cells))
    out: set = set()
    for group in by_res.values():
        out |= set(_h3.compact_cells(group))
    return out


def _cover_cells(geojson_geom: dict) -> set | None:
    """Cells covering ``geojson_geom``, or ``None`` when it cannot be covered.

    Deliberately never falls back to a centroid: that decision belongs to
    ``compute_h3_fields``, and keeping it there is what lets the fallback be
    counted instead of silently taken here.

    ``GeometryCollection`` **recurses**, so a collection whose members are
    themselves collections is handled, and covers every member type rather
    than only polygonal ones. The previous implementation filtered members to
    ``("Polygon","MultiPolygon")`` and did not recurse, so
    ``wd:Q11512408`` "Meiji Dori" — a collection containing one collection —
    got **zero** cells and no fuzzy containment at all, while
    ``wd:Q1289367`` "Lemboulas" (MultiLineString + Point, ~50 km) collapsed
    to a single cell.
    """
    gt = geojson_geom.get("type")

    if gt in ("Point", "MultiPoint"):
        # A Point is handled here as well as short-circuited by the caller,
        # because as a COLLECTION MEMBER it must contribute its own cell.
        # Returning None for it would both drop that cell and log a spurious
        # fallback warning — `wd:Q1289367` "Lemboulas" is exactly
        # GeometryCollection[MultiLineString, Point], so a check that warned
        # on it would be crying wolf on correct data.
        return _member_point_cells(geojson_geom) or None

    if gt in ("Polygon", "MultiPolygon"):
        cells = _polyfill_adaptive(geojson_geom)
        return _compact_mixed(cells) or None

    if gt in ("LineString", "MultiLineString"):
        # ~500 m buffer in degrees (≈ 0.005°) so a line has an area to fill.
        buffered = shape(geojson_geom).buffer(0.005)
        buf_geojson = json.loads(json.dumps(buffered.__geo_interface__))
        cells = _polyfill_adaptive(buf_geojson)
        return _compact_mixed(cells) or None

    if gt == "GeometryCollection":
        out: set = set()
        for member in geojson_geom.get("geometries") or []:
            if not isinstance(member, dict):
                continue
            try:
                sub = _cover_cells(member)          # recurses
            except Exception as exc:                # noqa: BLE001
                _h3_fallback("member_exception", member.get("type"), exc)
                continue
            if sub:
                out.update(sub)
            else:
                _h3_fallback("member_uncoverable", member.get("type"))
        return out or None

    return None


def _member_point_cells(geojson_geom: dict) -> set:
    """Return the set of H3 cells containing the geometry's member points.

    One cell per member point at ``H3_CENTROID_RESOLUTION``, deduplicated —
    members sharing a cell contribute it once. Walks arbitrary coordinate
    nesting so it is correct for MultiPoint regardless of how the source
    nested it.

    Verified against the live corpus before use (dry run, 3 Sep 2026, 779
    extent-bearing MultiPoints, 0 geom-store misses): 340 carry more members
    than their stored single-cell cover and 439 genuinely occupy one cell.
    Notably `wd` contributed only 45 of those 340 — 188 wd MultiPoints span
    a non-zero distance but sit inside one r7 cell (~2.4 km), so their
    one-cell cover was already correct and rewriting them would be a no-op.
    """
    cells, stack = set(), [geojson_geom.get("coordinates")]
    while stack:
        c = stack.pop()
        if c is None or isinstance(c, (int, float)):
            continue
        if c and isinstance(c[0], (int, float)):
            cells.add(_h3.latlng_to_cell(c[1], c[0], H3_CENTROID_RESOLUTION))
            continue
        stack.extend(c)
    return cells


def compute_h3_fields(lon: float, lat: float, geojson_geom=None) -> tuple[str | None, list[str]]:
    """
    Compute ``h3_centroid`` and ``h3_cover`` for a place.

    These are **per-geometry** fields, stored on each entry inside
    ``geometries[]`` — NOT on the place document root. ``schemas/places.json``
    defines ``geometries.h3_centroid`` / ``geometries.h3_cover`` and has no
    top-level equivalents. Call this for a geometry after ``enrich_geometry()``
    has produced its ``repr_point``, then set them on that entry::

        geom_entry["h3_centroid"] = h3_centroid
        geom_entry["h3_cover"] = h3_cover

    This docstring previously said "**top-level** fields on the place document
    (not nested inside ``geometries[]``)" and showed ``doc[...] = ...``. That was
    wrong, and it was load-bearing: on 2026-08-31 a session checked ``has_geom``
    and ``geom_class`` at the document root on the strength of it, read 4,363
    uniform ``None``s, and concluded a namespace was exempt from a defect it
    actually had — clearing its own output on the false premise. Documentation
    that agrees with a wrong reading is worse than none, so the correction is
    recorded here rather than silently applied.

    Args:
        lon:          Longitude of the representative point (from repr_point).
        lat:          Latitude of the representative point.
        geojson_geom: Optional GeoJSON geometry dict. When supplied and
                      non-Point, ``h3_cover`` is the compacted H3 cell set
                      covering that geometry. Callers may pass the precomputed
                      convex hull from ``enrich_geometry()`` as a faster,
                      coarser approximation. For point geometries (or when
                      omitted) ``h3_cover`` equals ``[h3_centroid]``.

    Returns:
        ``(h3_centroid, h3_cover)`` — both may be empty/None if h3 is
        unavailable or the geometry is malformed.
    """
    if not _H3_AVAILABLE:
        return None, []

    try:
        centroid_cell = _h3.latlng_to_cell(lat, lon, H3_CENTROID_RESOLUTION)
    except Exception:
        return None, []

    # ── Cover for point / no geometry supplied ─────────────────────────
    if geojson_geom is None:
        return centroid_cell, [centroid_cell]

    geom_type = geojson_geom.get("type", "")
    if geom_type == "Point":
        return centroid_cell, [centroid_cell]

    # ── MultiPoint: ONE CELL PER MEMBER POINT ──────────────────────────
    # Without this branch a MultiPoint fell through to the centroid-only
    # fallback at the end, so every one carried a SINGLE-cell cover however
    # far apart its members were — the Silk Roads corridor (32 points across
    # 46° of Asia) was represented by one cell. Since ``h3_cover`` drives
    # ``containment=fuzzy``, such a place was unfindable from a scope over
    # anywhere but that one cell.
    #
    # Deliberately NOT the convex hull. The hull of scattered points is a
    # solid swathe the place never occupies — hull-covering the Silk Roads
    # claims 46° of Asia the corridor never touches, which is the
    # over-coverage 2.11 correctly removed. Member cells cover where the
    # place IS and nowhere else.
    #
    # Deliberately NOT compacted: compaction only merges complete sets of
    # seven sibling cells, which scattered members never form, so it would
    # be a no-op that could only ever broaden the cover.
    #
    # ``geom_class`` stays "point" — these must remain findable WITHIN a
    # scope while never being usable to DEFINE one, since the hull of
    # scattered contributed points is not a boundary. Cover size and
    # geom_class are independent fields and that independence is the design.
    #
    # ⚠️ This makes the representation faithful; it does not make a sparse
    # MultiPoint dense. `whg:1361:9` "Danube" holds exactly two members —
    # the Black Forest source and the Black Sea mouth — so it gains a
    # 2-cell cover and is still NOT returned by a scope over Austria.
    # That is a data limitation, not a cover one; fixing it would mean
    # synthesising intermediate points, which this pipeline never does.
    if geom_type == "MultiPoint":
        try:
            cells = _member_point_cells(geojson_geom)
            if cells:
                return centroid_cell, sorted(cells)
        except Exception:
            pass

    # ── Cover every other geometry type via one audited path ───────────
    # Polygon/MultiPolygon polyfill+compact, Line/MultiLine buffer+polyfill,
    # MultiPoint member cells, GeometryCollection recursion. `_cover_cells`
    # returns None rather than a centroid, so every fallback below is counted.
    try:
        cells = _cover_cells(geojson_geom)
    except Exception as exc:                        # noqa: BLE001
        # Was `except Exception: pass` in three separate branches. A polyfill
        # that raised produced a one-cell cover indistinguishable from a place
        # that genuinely occupies one cell.
        _h3_fallback("exception", geom_type, exc)
    else:
        if cells:
            # Sorted, not `list(set)`: `compact_cells` returns a set, so the
            # previous ordering varied between runs on identical input.
            return centroid_cell, sorted(cells)
        _h3_fallback("uncoverable", geom_type)

    # ── Fallback: centroid only ─────────────────────────────────────────
    # Reaching here means the cover does NOT describe the geometry. It is a
    # correct answer only for a Point (returned earlier); for anything else
    # it is the defect this counter exists to surface.
    return centroid_cell, [centroid_cell]


# Mean great-circle km per degree of latitude (WGS84). One degree of LONGITUDE
# is this times cos(latitude), which is the whole reason the estimate below
# needs a latitude.
_KM_PER_DEGREE = 111.19492664455873

# ⚠️ There used to be a hard-coded ``_H3_HEX_AREA_DEG2`` table here. Every entry
# was **~108× too large**, because the areas had been divided by 111 (km per
# degree) rather than 111² (km² per degree²) — a units error, uniform across all
# nine resolutions. Measured 3 Sep 2026: for r5 the table said 2.2 where the true
# equator value is 0.0205, and 252.904 km² ÷ 111.19 = 2.274, which is where 2.2
# came from.
#
# Effect: the estimate under-predicted cell counts by ~108×, so
# ``_pick_polyfill_resolution`` left r7 only above **450 deg²** (a 21°×21°
# polygon) when it should do so above **~4.2 deg²** (2°×2°). Everything in
# between started at r7, overflowed the cap, and relied on the ladder in
# ``compute_h3_fields`` to recover — which is why this cost wasted work rather
# than wrong answers.
#
# The table is now DERIVED FROM ``h3`` ITSELF rather than transcribed, so the
# same class of error cannot recur. The literal fallback below is used only when
# h3 is unavailable, in which case polyfill returns nothing anyway.
_H3_HEX_AREA_DEG2_FALLBACK = {
    0: 352.4, 1: 49.32, 2: 7.020, 3: 1.002,
    4: 0.1432, 5: 0.02045, 6: 0.002922, 7: 0.0004174, 8: 5.963e-05,
}


@lru_cache(maxsize=32)
def _h3_cell_area_deg2_equator(res: int) -> float:
    """Mean H3 cell area at ``res`` in degrees², at the equator.

    Derived from ``h3.average_hexagon_area`` rather than transcribed — see the
    note above. Falls back to a literal only when h3 is missing.
    """
    if not _H3_AVAILABLE:
        return _H3_HEX_AREA_DEG2_FALLBACK.get(res, 0.0004174)
    try:
        return _h3.average_hexagon_area(res, unit="km^2") / (_KM_PER_DEGREE ** 2)
    except Exception:                                          # noqa: BLE001
        return _H3_HEX_AREA_DEG2_FALLBACK.get(res, 0.0004174)


def estimate_polyfill_cells(bbox_area_deg2: float, centre_lat_deg: float,
                            res: int) -> float:
    """Estimated cell count for a bbox of ``bbox_area_deg2`` centred at
    ``centre_lat_deg``, filled at ``res``.

    A degree of longitude is ``cos(latitude)`` shorter than a degree of latitude,
    so a fixed-area H3 cell spans MORE degrees² near the poles — and a bounding
    box of a given degrees² covers correspondingly LESS ground. The cell count
    therefore scales with ``cos(latitude)``, and omitting it over-predicts at
    high latitude.

    ⚠️ Verified against real polyfills of a 4°×4° box at r5 (3 Sep 2026):
    1,025 cells at the equator falling to 114 at 80°N, against this estimate's
    782 → 109. It runs slightly LOW, which is the tolerable direction: an
    under-estimate picks too fine a resolution and the ladder in
    ``compute_h3_fields`` recovers, whereas an over-estimate picks too coarse
    and **nothing recovers the lost detail**.
    """
    # Clamp so a polar bbox cannot drive the estimate to zero and pick r7 for
    # something enormous.
    cos_lat = max(math.cos(math.radians(max(-89.9, min(89.9, centre_lat_deg)))), 0.02)
    area = _h3_cell_area_deg2_equator(res)
    return bbox_area_deg2 * cos_lat / max(area, 1e-12)


def _bbox_area_deg2(geojson_geom: dict) -> float:
    """Approximate polygon bounding-box area in degrees²."""
    if not isinstance(geojson_geom, dict):
        return 0.0
    coords: list[list[float]] = []

    def _walk(node):
        if isinstance(node, list):
            if (
                node
                and isinstance(node[0], (int, float))
                and len(node) >= 2
                and isinstance(node[1], (int, float))
            ):
                coords.append([float(node[0]), float(node[1])])
            else:
                for child in node:
                    _walk(child)

    _walk(geojson_geom.get("coordinates"))
    if not coords:
        return 0.0
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return max(0.0, (max(lons) - min(lons)) * (max(lats) - min(lats)))


def _bbox_centre_lat(geojson_geom: dict) -> float:
    """Latitude of the geometry's bounding-box centre, or 0.0 if unknown.

    0.0 is the *conservative* default here: cos(0) = 1 maximises the estimated
    cell count, so an unknown latitude picks a coarser resolution rather than
    silently assuming a polar geometry is tiny.
    """
    if not isinstance(geojson_geom, dict):
        return 0.0
    lats: list[float] = []

    def _walk(node):
        if isinstance(node, list):
            if (node and isinstance(node[0], (int, float)) and len(node) >= 2
                    and isinstance(node[1], (int, float))):
                lats.append(float(node[1]))
            else:
                for child in node:
                    _walk(child)

    _walk(geojson_geom.get("coordinates"))
    if not lats:
        return 0.0
    return (max(lats) + min(lats)) / 2.0


def _pick_polyfill_resolution(bbox_area_deg2: float,
                              centre_lat_deg: float = 0.0) -> int:
    """Return the highest H3 resolution whose estimated cell count for a
    polygon of ``bbox_area_deg2`` centred at ``centre_lat_deg`` won't exceed
    ``H3_POLYFILL_MAX_CELLS``.

    ``centre_lat_deg`` defaults to 0.0 (equator) so existing single-argument
    callers keep working and get the *conservative* answer — see
    :func:`_bbox_centre_lat`.
    """
    for res in (H3_CENTROID_RESOLUTION, 5, 3):
        if estimate_polyfill_cells(bbox_area_deg2, centre_lat_deg, res) <= H3_POLYFILL_MAX_CELLS:
            return res
    return 3  # always at least try r3 as a last resort


def _iter_rings(geojson_geom: dict) -> Iterable[list]:
    """Yield every linear ring (exterior + holes) of a Polygon/MultiPolygon."""
    gtype = geojson_geom.get("type")
    coords = geojson_geom.get("coordinates") or []
    if gtype == "Polygon":
        for ring in coords:
            yield ring
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                yield ring


def _crosses_antimeridian(geojson_geom: dict) -> bool:
    """True when any ring edge jumps > 180° in longitude — the signature of a
    polygon straddling ±180° (e.g. US via the Aleutians, RU via Chukotka). Such
    geometries get a degenerate ~360° bbox, so the standard whole-geometry fill
    picks the coarsest resolution and mis-fills, dropping the interior."""
    for ring in _iter_rings(geojson_geom):
        prev = None
        for pt in ring:
            try:
                lon = float(pt[0])
            except (TypeError, ValueError, IndexError):
                prev = None
                continue
            if prev is not None and abs(lon - prev) > 180.0:
                return True
            prev = lon
    return False


def _split_polygon_at_antimeridian(poly_geojson: dict) -> list[dict]:
    """Split one dateline-crossing Polygon into pieces valid in [-180, 180].

    Shifts negative longitudes by +360 so the ring is contiguous in [0, 360],
    clips into the [0,180] and [180,360] half-planes, then translates the
    eastern clip back by −360. Returns a list of non-crossing GeoJSON Polygons.
    """
    from shapely.geometry import box, shape
    from shapely.affinity import translate

    try:
        def _shift(ring):
            out = []
            for pt in ring:
                lon, lat = float(pt[0]), float(pt[1])
                out.append([lon + 360.0 if lon < 0 else lon, lat])
            return out

        shifted = {
            "type": "Polygon",
            "coordinates": [_shift(r) for r in poly_geojson.get("coordinates") or []],
        }
        poly = shape(shifted)
        if not poly.is_valid:
            poly = poly.buffer(0)
        parts: list[dict] = []
        west = poly.intersection(box(0.0, -90.0, 180.0, 90.0))
        east = translate(poly.intersection(box(180.0, -90.0, 360.0, 90.0)), xoff=-360.0)
        for piece in (west, east):
            if piece.is_empty:
                continue
            gj = json.loads(json.dumps(piece.__geo_interface__))
            if gj.get("type") == "Polygon":
                parts.append(gj)
            elif gj.get("type") == "MultiPolygon":
                for pc in gj.get("coordinates") or []:
                    parts.append({"type": "Polygon", "coordinates": pc})
        return parts or [poly_geojson]
    except Exception:
        return [poly_geojson]


def _cells_for_shape(h3_poly, res: int) -> list:
    """Cells the shape OVERLAPS, not merely those whose centre it contains.

    ``h3shape_to_cells`` emits a cell only when the cell's **centre** falls
    inside the polygon. At the resolutions used for large geometries — res 3
    hexagons are ~120 km across — a coastal city routinely sits in a cell whose
    centre is offshore, so the cell is never emitted even though the polygon
    plainly covers the city. Measured on Australia (6,627 parts, 1,655,696
    vertices): Sydney had no covering cell at any resolution, while Cronulla
    20 km away did. Rio, Buenos Aires, Honolulu and the Fujian coast behaved the
    same way.

    That matters twice over. ``h3_cover`` is the candidate prefilter for ccode
    resolution, and it is also what ``gateway/spatial.py`` uses for **fuzzy
    containment — the default search mode** — on both the region and each hit
    (place#174). A missing cell is a silent false negative in both.

    h3 4.x exposes the correct semantics directly. ``contain='overlap'`` returns
    every cell the shape intersects. Guarded because the API is marked
    experimental: if it is absent or changes, fall back to centre containment
    rather than failing, since a narrow cover still beats none.
    """
    fn = getattr(_h3, "h3shape_to_cells_experimental", None)
    if fn is not None:
        try:
            return fn(h3_poly, res, contain="overlap")
        except Exception:
            pass
    return _h3.h3shape_to_cells(h3_poly, res)


def _polyfill_adaptive(geojson_geom: dict) -> set[str]:
    """Polyfill a GeoJSON polygon with H3 cells at an adaptive resolution.

    Dispatcher that keeps the standard whole-geometry fill for the common
    (non-crossing) case, but for **dateline-crossing** geometries decomposes to
    individual polygons — splitting any part that itself crosses ±180 — and
    fills each piece independently before unioning. Without this, a polygon
    straddling the antimeridian silently loses its interior (e.g. California /
    Siberia were absent from the US / RU covers). See :func:`_crosses_antimeridian`.
    """
    if not _H3_AVAILABLE:
        return set()

    gtype = geojson_geom.get("type")
    if gtype in ("Polygon", "MultiPolygon") and _crosses_antimeridian(geojson_geom):
        cells: set[str] = set()
        # Decompose MultiPolygon → its member Polygons (most members do NOT
        # cross; only the seam-straddling one needs splitting).
        if gtype == "MultiPolygon":
            members = [
                {"type": "Polygon", "coordinates": pc}
                for pc in geojson_geom.get("coordinates") or []
            ]
        else:
            members = [geojson_geom]
        for poly in members:
            if _crosses_antimeridian(poly):
                for part in _split_polygon_at_antimeridian(poly):
                    cells |= _polyfill_one_polygon(part)
            else:
                cells |= _polyfill_one_polygon(poly)
        return cells

    return _polyfill_one_polygon(geojson_geom)


def _polyfill_one_polygon(geojson_geom: dict) -> set[str]:
    """Fill a single non-crossing Polygon/MultiPolygon at an adaptive resolution.

    Starts at the highest resolution whose estimated cell count fits in
    ``H3_POLYFILL_MAX_CELLS``; drops to the next coarser resolution on the
    rare cases where the estimate undershoots the actual count.

    The previous implementation always started at ``H3_CENTROID_RESOLUTION``
    (r7) and waited for the polyfill to exceed the cap before falling back —
    which burned O(seconds) per continent-scale polygon. The area-based
    pre-selection avoids that wasted work entirely.
    """
    if not _H3_AVAILABLE:
        return set()

    bbox_area = _bbox_area_deg2(geojson_geom)
    start_res = _pick_polyfill_resolution(bbox_area, _bbox_centre_lat(geojson_geom))

    # Bound ``h3shape_to_cells`` cost on hyper-detailed boundaries by simplifying
    # away sub-resolution vertices first. Gated on vertex count so small geoms
    # (which must keep their shape) are never simplified.
    if _count_vertices(geojson_geom) > H3_SIMPLIFY_VERTEX_THRESHOLD:
        try:
            tol = _H3_SIMPLIFY_TOL_DEG.get(start_res, 0.04)
            simplified = shape(geojson_geom).simplify(tol, preserve_topology=True)
            # Douglas–Peucker moves the boundary INWARD by up to ``tol`` — at
            # res 4 that is 0.10° ≈ 11 km, at res 3 0.25° ≈ 28 km. For a
            # country that silently deletes coastal cells, and an h3_cover is
            # a PREFILTER: a cell that is missing loses its candidate
            # permanently, because the precise Shapely refine never sees the
            # place. A cell that is spurious costs only that refine, which
            # rejects it.
            #
            # So the error must point outward. Dilating the simplified shape by
            # the same tolerance guarantees it contains the original, hence
            # cover(simplified ⊕ tol) ⊇ cover(original).
            #
            # Measured cost of not doing this: the UN prefilter held 77,279
            # res-4 cells against ~84,000 needed for global land, and 外高村
            # (118.690158, 24.67126) sat *inside* China's polygon yet was never
            # offered China as a candidate — ~615 k osm places lost this way.
            if not simplified.is_empty:
                simplified = simplified.buffer(tol)
            if not simplified.is_empty:
                geojson_geom = json.loads(json.dumps(simplified.__geo_interface__))
        except Exception:
            pass  # fall back to the original geometry

    try:
        h3_poly = _h3.geo_to_h3shape(geojson_geom)
    except Exception:
        return set()

    # Try the chosen res; on overflow drop to the next coarser one. The
    # ordered candidate list keeps the original res 5 / res 3 fallback
    # for shapes whose bbox underestimates the actual polyfill cost.
    #
    # ⚠️ The ladder MUST run down to r0. It used to stop at r3, and a shape
    # that overflowed there had no escape: the loop returned an empty set,
    # `if cells:` failed in the caller, and the geometry got a centroid-only
    # fallback with an EMPTY ``h3_cover`` — no fuzzy containment at all.
    # Measured on `po:p0s2rwkrjbs`, a valid near-global MultiPolygon
    # (359.99° × 116.53°, 33% fill, reaching 81.7°N): r3 gave 10,381 cells
    # against a 10,000 cap and r5 gave 508,243, so both were rejected —
    # while r2 gives 1,483 and would have worked. r0 has 122 cells globally
    # and can never exceed the cap, so with the ladder complete THIS FUNCTION
    # CAN NO LONGER RETURN AN EMPTY COVER FOR A VALID POLYGON.
    #
    # ⚠️ ``_pick_polyfill_resolution``'s estimate is a STARTING GUESS, not a
    # guarantee: do not rely on it to keep the first candidate inside the cap.
    # It was badly wrong until 3 Sep 2026 (a units error made it under-predict
    # by ~108×, so it began at r7 for anything under 450 deg²); it is now
    # derived from h3 and latitude-aware, and verified against real polyfills
    # across 0-80° latitude. It still runs slightly low by design — an
    # under-estimate is recovered by this ladder, an over-estimate would pick
    # too coarse a resolution and nothing would recover the lost detail.
    candidates: list[int] = []
    for res in (start_res, 5, 3, 2, 1, 0):
        if res not in candidates:
            candidates.append(res)

    for res in candidates:
        try:
            cells = _cells_for_shape(h3_poly, res)
            if len(cells) <= H3_POLYFILL_MAX_CELLS:
                return set(cells)
        except Exception:
            continue
    return set()


def geom_class_of(geojson):
    """Coarse geometry class ∈ {'point','line','area'} for a GeoJSON dict — the
    *shape* discriminator stored as ``geometries[].geom_class`` (see
    ``schemas/field-notes.md``).

    Multi- variants collapse to their base (``MultiPolygon`` → ``area``); a
    ``GeometryCollection`` is resolved once, here, by its members (any polygon →
    ``area``, else any line → ``line``, else ``point``) so no consumer re-opens
    the geometry to classify it. Distinct from ``has_geom`` (retrievability):
    downstream "is it areal / usable as a ``contained_in`` region" keys on this,
    not ``has_geom`` (a LineString is ``has_geom`` true but not areal). Returns
    None for an unknown/empty type.
    """
    t = (geojson or {}).get("type")
    if t in ("Polygon", "MultiPolygon"):
        return "area"
    if t in ("LineString", "MultiLineString"):
        return "line"
    if t in ("Point", "MultiPoint"):
        return "point"
    if t == "GeometryCollection":
        classes = {geom_class_of(g) for g in geojson.get("geometries", [])}
        if "area" in classes:
            return "area"
        if "line" in classes:
            return "line"
        return "point"
    return None


def has_valid_latitudes(geojson_geom):
    """False when any latitude is outside [-90, 90] — an unfixable geometry.

    Longitude can be folded (:func:`wrap_longitude`); latitude cannot. A value
    past the pole is upstream corruption, and every candidate "fix" invents
    data: transposing lat/lon guesses, clamping to +/-90 relocates the place to
    a pole. Wikidata supplied five on the place#164 rebuild —

        wd:Q134355453  lat=123.045403 lon=13.51768   (Bikol Wiktionary event,
                                                      coordinates transposed)
        wd:Q130748798  lat=123.045403 lon=13.51768   (same, same event venue)
        wd:Q134355589  lat=123.16244  lon=13.45592   (likewise)
        wd:Q64027103   lat=135.872891                (Kashiwa is 35.87N)
        wd:Q113370244  lat=99.999999  lon=0.0        (placeholder sentinel)

    — and Elasticsearch rejects the whole document on each, so the place would
    be absent entirely rather than merely unlocated. Dropping the geometry
    keeps the record, its names, types and links; only the coordinate we never
    had is lost.
    """
    if not isinstance(geojson_geom, dict):
        return True
    coords = geojson_geom.get("coordinates")
    if coords is None:
        return True
    stack = [coords]
    while stack:
        cur = stack.pop()
        if isinstance(cur, (list, tuple)):
            if (len(cur) >= 2 and isinstance(cur[0], (int, float))
                    and isinstance(cur[1], (int, float))):
                if not (-90.0 <= float(cur[1]) <= 90.0):
                    return False
            else:
                stack.extend(cur)
    return True


def enrich_geometry(geojson_geom, timespans=None, geom_key: str | None = None):
    """
    Compute a geometry entry for the ``places`` index from a GeoJSON geometry.

    Full geometries are **no longer stored in Elasticsearch**.  Instead, when a
    ``geom_key`` is supplied and the module-level ``GeomStoreWriter`` is
    configured (via ``processing.geom_store.configure_module_writer()``), the
    validated WKB is written to the VAST geometry store and ``has_geom`` is
    set to ``True`` in the returned entry.

    Returns a dict suitable for the ``geometries[]`` nested array::

        {
          "has_geom":   bool,          # True iff full geom written to VAST
          "repr_point": {"lon", "lat"},
          "hull":       <GeoJSON convex hull>,
          "bounds":     [west, south, east, north],
          "timespans":  [...],         # passed through if provided
        }

    Additionally, derive ``h3_centroid`` / ``h3_cover`` by calling
    ``compute_h3_fields()`` with the ``repr_point`` from the returned entry, and
    set them **ON THAT ENTRY** — they are per-geometry fields inside
    ``geometries[]``, never on the place document root::

        geom_entry = enrich_geometry(geom, geom_key=f"{place_id}_0")
        if geom_entry and geom_entry.get("repr_point"):
            rp = geom_entry["repr_point"]
            h3_geom = select_h3_cover_geometry(geom_entry, geom)
            h3c, h3cover = compute_h3_fields(rp["lon"], rp["lat"], h3_geom)
            geom_entry["h3_centroid"] = h3c
            geom_entry["h3_cover"] = h3cover

    .. warning::

        This docstring previously told you to write ``doc["h3_centroid"]`` and
        ``doc["h3_cover"]`` on the document ROOT. Do not. Seven authority
        scripts plus the update-patch path followed it, so **1,310,192 live
        documents carry a top-level ``h3_cover``** that ``schemas/places.json``
        does not declare (it is dynamically mapped as ``text``, so H3 cell ids
        are analysed as prose), that **no reader anywhere consumes** — every
        reader in ``gateway/``, ``processing/`` and ``clustering/`` takes the
        nested field — and that has diverged from the nested truth in **3,999
        of 4,000** sampled ``clio`` docs, because the root was written once at
        ingest while the nested one has been recomputed by every H3 stage
        since. See ``compute_h3_fields`` for the same correction.
        **``geometries[].h3_cover`` is the real one.**

    Coordinates are rounded **before** validation so that any self-intersections
    introduced by rounding are caught and repaired.

    Args:
        geojson_geom: Dict with GeoJSON geometry (type, coordinates)
        timespans:    Optional list of timespan dicts to attach
        geom_key:     Key for the VAST geometry store entry
                      (``"{place_id}_{geom_idx}"``).  Required for VAST write.

    Returns:
        Dict suitable for the ``geometries[]`` nested array, or None on failure
    """
    if not geojson_geom:
        return None

    try:
        # ── 1. Round coordinates first ──────────────────────────────────
        rounded_geojson = round_coordinates(geojson_geom)
        if not rounded_geojson:
            return None

        # An impossible latitude makes the geometry unusable and, left alone,
        # makes ES reject the entire document. Drop the geometry, keep the place.
        if not has_valid_latitudes(rounded_geojson):
            return None

        # ── 2. Convert to Shapely (after rounding) ─────────────────────
        geom = geojson_to_shapely(rounded_geojson)
        if not geom or geom.is_empty:
            return None

        # ── 3. Validate / fix (catches rounding-induced intersections) ─
        if not geom.is_valid:
            geom = shapely_make_valid(geom)
            if not geom.is_valid:
                geom = geom.buffer(0)
            if not geom.is_valid or geom.is_empty:
                return None

        # ── 4. Serialise validated geometry to GeoJSON ──────────────────
        full_geom = geom.__geo_interface__

        # ── 5. Representative point (reuse the Shapely object) ──────────
        P = COORDINATE_PRECISION
        rep_point = None
        if isinstance(geom, Point):
            rep_point = {'lon': round(wrap_longitude(geom.x), P),
                         'lat': round(geom.y, P)}
        elif isinstance(geom, GeometryCollection):
            rp = _representative_from_collection(geom)
            if rp is not None:
                rep_point = {'lon': round(wrap_longitude(rp.x), P),
                             'lat': round(rp.y, P)}
        else:
            try:
                rp = geom.representative_point()
                rep_point = {'lon': round(wrap_longitude(rp.x), P),
                             'lat': round(rp.y, P)}
            except Exception:
                pass

        # ── 6. Convex hull + bounds ──────────────────────────────────────
        hull_geojson = None
        bounds_arr = None
        try:
            hull = geom.convex_hull
            if hull.is_empty or not hull.is_valid:
                hull = geom.envelope
            if hull and not hull.is_empty:
                if not hull.is_valid:
                    hull = hull.buffer(0)
                if hull.is_valid and not hull.is_empty:
                    hull_geojson_raw = hull.__geo_interface__
                    hull_geojson = round_coordinates(hull_geojson_raw, precision=P)
                    if hull_geojson:
                        hull_rounded = geojson_to_shapely(hull_geojson)
                        if hull_rounded and not hull_rounded.is_valid:
                            hull_rounded = shapely_make_valid(hull_rounded)
                            if hull_rounded and hull_rounded.is_valid and not hull_rounded.is_empty:
                                hull_geojson = round_coordinates(
                                    hull_rounded.__geo_interface__, precision=P
                                )
                            else:
                                hull_geojson = None
                    if hull_geojson:
                        hb = hull.bounds
                        bounds_arr = [
                            round(hb[0], P), round(hb[1], P),
                            round(hb[2], P), round(hb[3], P),
                        ]
        except Exception:
            try:
                env = geom.envelope
                if env and not env.is_empty and env.is_valid:
                    env_geojson = round_coordinates(env.__geo_interface__, precision=P)
                    if env_geojson:
                        hull_geojson = env_geojson
                        eb = env.bounds
                        bounds_arr = [
                            round(eb[0], P), round(eb[1], P),
                            round(eb[2], P), round(eb[3], P),
                        ]
            except Exception:
                pass

        # ── 7. Write full geometry to VAST store (if configured) ─────────
        has_geom = False
        is_non_trivial = not isinstance(geom, Point)
        if is_non_trivial and geom_key:
            from processing.geom_store import get_module_writer
            writer = get_module_writer()
            if writer is not None:
                h3c = None
                if rep_point and _H3_AVAILABLE:
                    try:
                        h3c = _h3.latlng_to_cell(rep_point["lat"], rep_point["lon"],
                                                  H3_CENTROID_RESOLUTION)
                    except Exception:
                        pass
                has_geom = writer.write(geom_key, h3c or "", full_geom)

        # ── 8. Assemble result (no 'geom' field — stored externally) ─────
        entry: dict = {'has_geom': has_geom}
        # geom_class = shape discriminator {point,line,area}; see field-notes.
        # Distinct from has_geom (retrievability): downstream "is it areal / a
        # containment region" logic keys on geom_class, not has_geom.
        gc = geom_class_of(full_geom)
        if gc:
            entry['geom_class'] = gc
        if rep_point:
            entry['repr_point'] = rep_point
        if hull_geojson:
            entry['hull'] = hull_geojson
        if bounds_arr:
            entry['bounds'] = bounds_arr
        if timespans:
            entry['timespans'] = timespans

        return entry

    except Exception as e:
        print(f"Error in enrich_geometry: {e}")
        return None


# ============================================================================
# Staged Extraction Shim: Authority Script Integration
# ============================================================================

_VALID_DATASET_STATUSES = ("published", "pending")


def _geometry_signature(geom: dict) -> str:
    """Content identity of a geometry, ignoring fields added by later stages.

    ``geometry_index`` is positional and ``h3_centroid`` / ``h3_cover`` are
    filled in by ``h3_stage``, which enriches only one copy when an extract has
    emitted several. Including them would make identical geometries look
    distinct — and it was precisely their divergence that made 127 chgis
    documents end up with a null ``h3_cover``.
    """
    import json
    ignore = {"geometry_index", "h3_centroid", "h3_cover"}
    return json.dumps({k: v for k, v in sorted(geom.items()) if k not in ignore},
                      sort_keys=True, default=str)


def merge_place_docs(docs: list[dict]) -> dict:
    """Merge staged place documents that share a ``place_id`` into one.

    Bulk indexing keys on ``place_id`` as ``_id``, so several staged rows for
    one place are several successful writes and a single surviving document —
    whichever was written last. Nothing errors, and ``docs_indexed`` counts the
    writes, so the loss is invisible from the indexer's own report.

    Two real cases, both observed:

    * **Duplicate source rows.** CHGIS `placename` has up to 11 byte-identical
      rows per ``sys_id`` (differing only in a surrogate key), and the extract
      keys documents on ``sys_id``. Merging collapses them to one.
    * **Genuinely distinct geometries for one place.** HGIS ships *lugares*
      (points) and *territorios* (polygons) as separate LPF files, and 47
      ``src_id``s appear in both. The point and the polygon belong on the same
      document, as two entries in ``geometries``. Keeping the last-written row
      discards one of them.

    Scalars take the first non-empty value; lists are unioned preserving first
    -seen order. ``geometries`` are deduplicated on content and renumbered, so
    a place with genuinely different geometries — including ones attested over
    different timespans — keeps all of them.
    """
    if not docs:
        raise ValueError("merge_place_docs() called with no documents")
    if len(docs) == 1:
        return docs[0]

    merged: dict = {}
    for doc in docs:
        for key, value in doc.items():
            if key in ("toponyms", "geometries", "types", "relations",
                       "links", "ccodes"):
                continue
            if merged.get(key) in (None, "", [], {}):
                merged[key] = value

    def _union(key, identity):
        seen, out = set(), []
        for doc in docs:
            for item in (doc.get(key) or []):
                ident = identity(item)
                if ident in seen:
                    continue
                seen.add(ident)
                out.append(item)
        return out

    import json as _json

    merged["toponyms"] = _union(
        "toponyms", lambda t: t.get("toponym_id") or _json.dumps(t, sort_keys=True, default=str))
    merged["types"] = _union(
        "types", lambda t: (t.get("identifier"), t.get("label")))
    merged["relations"] = _union(
        "relations", lambda r: (r.get("relation_type"), r.get("related_place_id")))
    merged["links"] = _union(
        "links", lambda l: (l.get("type"), l.get("identifier") or l.get("url")))
    merged["ccodes"] = _union("ccodes", lambda c: c)

    # Deduplicate geometries on content, but salvage the enrichment fields:
    # h3_stage populates only one copy when an extract emitted several, so the
    # copy that survives a first-seen-wins dedup may be the one with a null
    # h3_cover. That is the exact shape of the chgis loss, so it is repaired
    # here rather than depending on which row happened to come first.
    geometries: list[dict] = []
    by_signature: dict[str, dict] = {}
    for doc in docs:
        for geom in (doc.get("geometries") or []):
            sig = _geometry_signature(geom)
            kept = by_signature.get(sig)
            if kept is None:
                kept = dict(geom)
                by_signature[sig] = kept
                geometries.append(kept)
                continue
            for field in ("h3_centroid", "h3_cover"):
                if kept.get(field) in (None, [], "") and geom.get(field):
                    kept[field] = geom[field]
    for idx, geom in enumerate(geometries):
        geom["geometry_index"] = idx
    merged["geometries"] = geometries

    # Drop keys that were absent everywhere rather than inventing empty lists.
    for key in ("toponyms", "geometries", "types", "relations", "links",
                "ccodes"):
        if not merged[key] and not any(d.get(key) for d in docs):
            merged.pop(key, None)
    return merged


def write_staged_place_doc(namespace: str, doc: dict) -> None:
    """Write a standardised place document to the staged extract for a namespace.

    Used by authority scripts running in ``WHG_STAGING_MODE=1`` to write place
    documents to a staged JSONL file instead of indexing to Elasticsearch.

    This function appends to ``{STAGED_BASE_DIR}/{namespace}/extract/places.jsonl``
    and is the lightweight, standardised way for authority scripts to emit place
    documents without ES access. The appended documents can later be consolidated
    to Parquet by batch processes that expect bulk performance.

    Per Master Plan Appendix E.2 item 3, every emitted doc must carry
    ``dataset_status`` (``'published'`` | ``'pending'``) and ``dataset_id``. If
    absent, defaults are filled in: ``dataset_status='published'`` and
    ``dataset_id=<namespace>`` for ordinary authorities. ``whg``-namespace docs
    must set ``dataset_id`` themselves (sub-namespaced per Dataset/Collection,
    e.g. ``'whg:1234'``); the helper raises if a ``whg`` doc is missing it.

    Args:
        namespace (str): Authority namespace (e.g. ``gn``, ``wd``, ``osm``, ``nl``).
        doc (dict): A place document conforming to the `places` ES schema (minus _index/_id).

    Raises:
        OSError: If the staged directory cannot be created or the file cannot be written.
        TypeError: If ``doc`` is not a dict.
        ValueError: If ``dataset_status`` is not one of ``'published'`` / ``'pending'``,
            or if a ``whg``-namespace doc lacks an explicit ``dataset_id``.
    """
    import os
    import json
    from pathlib import Path

    if not isinstance(doc, dict):
        raise TypeError(f"Expected dict, got {type(doc)}")

    status = doc.setdefault("dataset_status", "published")
    if status not in _VALID_DATASET_STATUSES:
        raise ValueError(
            f"dataset_status must be one of {_VALID_DATASET_STATUSES}, got {status!r}"
        )

    if "dataset_id" not in doc:
        if namespace == "whg":
            raise ValueError(
                "whg-namespace records must set dataset_id explicitly "
                "(e.g. 'whg:<dataset_id>')"
            )
        doc["dataset_id"] = namespace

    # Augment the doc so each ``geometries[]`` entry carries ``geometry_index``
    # and ``geom_ref`` — required by tile generation and ccode_enrichment to
    # look up full polygons from the geom store. Done at write time so the
    # canonical JSONL matches the parquet sidecar produced later by
    # ``_consolidate_extracts`` (which calls the same augmenter).
    from processing.stage_writers import _augment_doc_for_stage
    doc = _augment_doc_for_stage(doc)

    staged_base = os.environ.get(
        "STAGED_BASE_DIR",
        os.path.join(os.environ.get("IX3_BASE", "/vast/ishi"), "staged"),
    )
    extract_dir = Path(staged_base) / namespace / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)

    out_file = extract_dir / "places.jsonl"

    with out_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=True) + "\n")


def is_staging_mode() -> bool:
    """Check if the current process is running in ``WHG_STAGING_MODE``."""
    import os
    return os.environ.get("WHG_STAGING_MODE", "").lower() in ("1", "true", "yes")


# Example usage and testing
if __name__ == "__main__":
    # Test with various geometries

    # Simple point
    point_geom = {
        "type": "Point",
        "coordinates": [0.0, 51.5]
    }
    print("Point centroid:", compute_geodetic_centroid(point_geom))

    # Polygon (roughly UK-shaped)
    polygon_geom = {
        "type": "Polygon",
        "coordinates": [[
            [-5.0, 50.0],
            [2.0, 50.0],
            [2.0, 59.0],
            [-5.0, 59.0],
            [-5.0, 50.0]
        ]]
    }
    print("Polygon centroid:", compute_geodetic_centroid(polygon_geom))
    print("Polygon area (km²):", compute_area_km2(polygon_geom))
    print("Polygon bbox:", compute_bbox(polygon_geom))

    # LineString (sample river)
    line_geom = {
        "type": "LineString",
        "coordinates": [
            [0.0, 51.5],
            [0.1, 51.6],
            [0.2, 51.7],
            [0.3, 51.8]
        ]
    }
    print("Line centroid:", compute_geodetic_centroid(line_geom))
    print("Line length (km):", compute_length_km(line_geom))

    # Test at high latitude (near North Pole)
    arctic_polygon = {
        "type": "Polygon",
        "coordinates": [[
            [-10.0, 80.0],
            [10.0, 80.0],
            [10.0, 85.0],
            [-10.0, 85.0],
            [-10.0, 80.0]
        ]]
    }
    print("\nArctic polygon (high latitude test):")
    print("  Geodetic centroid:", compute_geodetic_centroid(arctic_polygon))
    # Compare with naive arithmetic mean:
    coords = arctic_polygon['coordinates'][0]
    naive_lon = sum(c[0] for c in coords) / len(coords)
    naive_lat = sum(c[1] for c in coords) / len(coords)
    print(f"  Naive centroid: {{'lon': {naive_lon}, 'lat': {naive_lat}}}")
    print(f"  Difference matters at high latitudes!")