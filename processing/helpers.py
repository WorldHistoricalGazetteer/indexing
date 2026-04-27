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
import json

try:
    import h3 as _h3
    _H3_AVAILABLE = True
except ImportError:
    _H3_AVAILABLE = False

# H3 reference resolution for h3_centroid (≈1.2 km hexagon edge)
H3_CENTROID_RESOLUTION = 7
# Maximum cells produced by polyfill before dropping to a coarser resolution
H3_POLYFILL_MAX_CELLS = 10_000


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


def compute_h3_fields(lon: float, lat: float, geojson_geom=None) -> tuple[str | None, list[str]]:
    """
    Compute ``h3_centroid`` and ``h3_cover`` for a place.

    These are **top-level** fields on the place document (not nested inside
    ``geometries[]``).  Call this for the primary geometry after
    ``enrich_geometry()`` has produced a ``repr_point``, then set::

        doc["h3_centroid"] = h3_centroid
        doc["h3_cover"] = h3_cover

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

    # ── Cover for polygon/multipolygon: polyfill + compact ─────────────
    if geom_type in ("Polygon", "MultiPolygon"):
        try:
            cells = _polyfill_adaptive(geojson_geom)
            if cells:
                cover = list(_h3.compact_cells(cells))
                return centroid_cell, cover if cover else [centroid_cell]
        except Exception:
            pass

    # ── LineString: buffer to a small polygon, then polyfill ───────────
    if geom_type in ("LineString", "MultiLineString"):
        try:
            geom_shp = shape(geojson_geom)
            # ~500 m buffer in degrees (≈ 0.005°)
            buffered = geom_shp.buffer(0.005)
            buf_geojson = json.loads(json.dumps(buffered.__geo_interface__))
            cells = _polyfill_adaptive(buf_geojson)
            if cells:
                cover = list(_h3.compact_cells(cells))
                return centroid_cell, cover if cover else [centroid_cell]
        except Exception:
            pass

    # ── GeometryCollection: use centroid only ──────────────────────────
    return centroid_cell, [centroid_cell]


# Average H3 cell area in degrees² at each resolution (equator-equivalent).
# Used by ``_polyfill_adaptive`` to pick a starting resolution that won't
# overflow ``H3_POLYFILL_MAX_CELLS``: a continent-scale polygon (~1000 deg²)
# would generate ~22 000 cells at r7 (over cap → wasted work) but only ~450
# at r5 — so we start at r5 directly instead of trying r7 first.
_H3_HEX_AREA_DEG2 = {
    0: 38000.0, 1: 5400.0, 2: 770.0, 3: 110.0,
    4: 16.0, 5: 2.2, 6: 0.31, 7: 0.045, 8: 0.0064,
}


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


def _pick_polyfill_resolution(bbox_area_deg2: float) -> int:
    """Return the highest H3 resolution whose estimated cell count for a
    polygon of ``bbox_area_deg2`` won't exceed ``H3_POLYFILL_MAX_CELLS``."""
    for res in (H3_CENTROID_RESOLUTION, 5, 3):
        hex_area = _H3_HEX_AREA_DEG2.get(res, 0.045)
        if bbox_area_deg2 / max(hex_area, 1e-12) <= H3_POLYFILL_MAX_CELLS:
            return res
    return 3  # always at least try r3 as a last resort


def _polyfill_adaptive(geojson_geom: dict) -> set[str]:
    """Polyfill a GeoJSON polygon with H3 cells at an adaptive resolution.

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

    try:
        h3_poly = _h3.geo_to_h3shape(geojson_geom)
    except Exception:
        return set()

    bbox_area = _bbox_area_deg2(geojson_geom)
    start_res = _pick_polyfill_resolution(bbox_area)

    # Try the chosen res; on overflow drop to the next coarser one. The
    # ordered candidate list keeps the original res 5 / res 3 fallback
    # for shapes whose bbox underestimates the actual polyfill cost.
    candidates: list[int] = []
    for res in (start_res, 5, 3):
        if res not in candidates:
            candidates.append(res)

    for res in candidates:
        try:
            cells = _h3.h3shape_to_cells(h3_poly, res)
            if len(cells) <= H3_POLYFILL_MAX_CELLS:
                return set(cells)
        except Exception:
            continue
    return set()


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

    Additionally, to derive the **top-level** ``h3_centroid`` / ``h3_cover``
    place fields, call ``compute_h3_fields()`` using the ``repr_point`` from
    the returned entry::

        geom_entry = enrich_geometry(geom, geom_key=f"{place_id}_0")
        if geom_entry and geom_entry.get("repr_point"):
            rp = geom_entry["repr_point"]
            h3_geom = select_h3_cover_geometry(geom_entry, geom)
            h3c, h3cover = compute_h3_fields(rp["lon"], rp["lat"], h3_geom)
            doc["h3_centroid"] = h3c
            doc["h3_cover"] = h3cover

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
            rep_point = {'lon': round(geom.x, P), 'lat': round(geom.y, P)}
        elif isinstance(geom, GeometryCollection):
            rp = _representative_from_collection(geom)
            if rp is not None:
                rep_point = {'lon': round(rp.x, P), 'lat': round(rp.y, P)}
        else:
            try:
                rp = geom.representative_point()
                rep_point = {'lon': round(rp.x, P), 'lat': round(rp.y, P)}
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