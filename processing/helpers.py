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


def enrich_geometry(geojson_geom, timespans=None):
    """
    Compute a full geometry entry for the places index from a GeoJSON geometry.

    Accepts a GeoJSON geometry dict and returns a dict with:
      - geom:       validated GeoJSON geometry (coordinates rounded to 6 dp)
      - repr_point: {lon, lat} guaranteed inside the geometry (rounded to 6 dp)
      - hull:       GeoJSON convex hull (rounded to 6 dp)
      - bounds:     [west, south, east, north] bounding box (rounded to 6 dp)
      - timespans:  passed through if provided

    Coordinates are rounded **before** validation so that any self-intersections
    introduced by rounding are caught and repaired.

    Args:
        geojson_geom: Dict with GeoJSON geometry (type, coordinates)
        timespans:    Optional list of timespan dicts to attach

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

        # ── 4. Serialise to GeoJSON dict (already rounded) ─────────────
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

        # ── 6. Convex hull + bounds (from the validated Shapely geom) ───
        # IMPORTANT: coordinates must be rounded to COORDINATE_PRECISION here,
        # just like the main geometry was rounded in step 1.  Leaving hull
        # coordinates at float64 precision (17 dp) produces near-collinear
        # vertices that Shapely accepts but ES's stricter JTS parser rejects
        # with "Polygon self-intersection".
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
                    # Round hull coordinates to match main geometry precision
                    hull_geojson_raw = hull.__geo_interface__
                    hull_geojson = round_coordinates(hull_geojson_raw, precision=P)
                    # Re-validate after rounding (rounding can introduce micro-intersections)
                    if hull_geojson:
                        hull_rounded = geojson_to_shapely(hull_geojson)
                        if hull_rounded and not hull_rounded.is_valid:
                            hull_rounded = shapely_make_valid(hull_rounded)
                            if hull_rounded and hull_rounded.is_valid and not hull_rounded.is_empty:
                                hull_geojson = round_coordinates(
                                    hull_rounded.__geo_interface__, precision=P
                                )
                            else:
                                hull_geojson = None  # Cannot repair; omit hull
                    if hull_geojson:
                        hb = hull.bounds  # (minx, miny, maxx, maxy)
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

        # ── 7. Assemble result ──────────────────────────────────────────
        entry = {'geom': full_geom}
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