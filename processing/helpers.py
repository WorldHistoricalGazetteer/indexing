# processing/helpers.py

"""
Geospatial helper functions using GEOS (via Shapely) for accurate computations.

This module provides geodetically-correct operations that account for Earth's
spherical geometry, unlike simple arithmetic means which cause distortion at
high latitudes.
"""

from shapely.geometry import shape, Point, LineString, Polygon, MultiPoint, MultiLineString, MultiPolygon, \
    GeometryCollection
from shapely.ops import transform
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
        print(f"Error converting GeoJSON to Shapely: {e}")
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
            return {'lon': geom.x, 'lat': geom.y}

        if isinstance(geom, GeometryCollection):
            rp = _representative_from_collection(geom)
            if rp is None:
                return None
            return {'lon': rp.x, 'lat': rp.y}

        rep_point = geom.representative_point()
        return {'lon': rep_point.x, 'lat': rep_point.y}

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