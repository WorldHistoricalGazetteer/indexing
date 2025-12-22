"""
Robust GeometryCollection processing for Wikidata geoshapes.

Properly handles merging mixed geometry types following GeoJSON hierarchy:
1. Polygon/MultiPolygon → MultiPolygon (highest priority)
2. LineString/MultiLineString → MultiLineString (if no polygons)
3. Point/MultiPoint → MultiPoint (if no polygons or lines)
"""

import json


def process_geometry_collection(geojson):
    """
    Process a GeometryCollection by extracting and merging geometries.

    Follows type hierarchy:
    - If any Polygon/MultiPolygon exists, return merged MultiPolygon
    - Else if any LineString/MultiLineString exists, return merged MultiLineString
    - Else if any Point/MultiPoint exists, return merged MultiPoint
    - Else return None

    Args:
        geojson: Dict with type="GeometryCollection"

    Returns:
        Merged geometry dict, or None if empty
    """
    geometries = geojson.get("geometries", [])
    if not geometries:
        return None

    # Collect by type
    polygons = []
    lines = []
    points = []

    for g in geometries:
        gtype = g.get("type")
        coords = g.get("coordinates")

        if not coords:
            continue

        if gtype == "Polygon":
            polygons.append(coords)
        elif gtype == "MultiPolygon":
            polygons.extend(coords)
        elif gtype == "LineString":
            lines.append(coords)
        elif gtype == "MultiLineString":
            lines.extend(coords)
        elif gtype == "Point":
            points.append(coords)
        elif gtype == "MultiPoint":
            points.extend(coords)

    # Return highest priority non-empty type
    if polygons:
        return create_multipolygon(polygons)
    elif lines:
        return create_multilinestring(lines)
    elif points:
        return create_multipoint(points)

    return None


def create_multipolygon(polygon_arrays):
    """
    Create a valid MultiPolygon from a list of polygon coordinate arrays.

    Args:
        polygon_arrays: List of polygon coordinate arrays
                       Each is [[exterior_ring], [hole1], [hole2], ...]

    Returns:
        Dict with type="MultiPolygon" and valid coordinates
    """
    if not polygon_arrays:
        return None

    # Validate and clean each polygon
    valid_polygons = []

    for poly_coords in polygon_arrays:
        if not isinstance(poly_coords, list) or not poly_coords:
            continue

        # Validate rings
        valid_rings = []
        for ring in poly_coords:
            if not isinstance(ring, list) or len(ring) < 4:
                continue

            # Check if ring has valid coordinates
            if not all(is_valid_coordinate(pt) for pt in ring):
                continue

            # Ensure ring is closed
            if ring[0] != ring[-1]:
                ring = ring + [ring[0]]

            valid_rings.append(ring)

        if valid_rings:
            valid_polygons.append(valid_rings)

    if not valid_polygons:
        return None

    return {
        "type": "MultiPolygon",
        "coordinates": valid_polygons
    }


def create_multilinestring(line_arrays):
    """
    Create a valid MultiLineString from a list of line coordinate arrays.

    Args:
        line_arrays: List of linestring coordinate arrays
                    Each is [[x,y], [x,y], ...]

    Returns:
        Dict with type="MultiLineString" and valid coordinates
    """
    if not line_arrays:
        return None

    valid_lines = []

    for line_coords in line_arrays:
        if not isinstance(line_coords, list) or len(line_coords) < 2:
            continue

        # Filter to valid coordinates
        valid_coords = [pt for pt in line_coords if is_valid_coordinate(pt)]

        if len(valid_coords) >= 2:
            valid_lines.append(valid_coords)

    if not valid_lines:
        return None

    return {
        "type": "MultiLineString",
        "coordinates": valid_lines
    }


def create_multipoint(point_arrays):
    """
    Create a valid MultiPoint from a list of point coordinates.

    Args:
        point_arrays: List of point coordinates [[x,y], [x,y], ...]

    Returns:
        Dict with type="MultiPoint" and valid coordinates
    """
    if not point_arrays:
        return None

    # Filter to valid coordinates
    valid_points = [pt for pt in point_arrays if is_valid_coordinate(pt)]

    if not valid_points:
        return None

    # Remove duplicates while preserving order
    seen = set()
    unique_points = []
    for pt in valid_points:
        pt_tuple = tuple(pt[:2])  # Use only lon,lat for deduplication
        if pt_tuple not in seen:
            seen.add(pt_tuple)
            unique_points.append(pt)

    if not unique_points:
        return None

    return {
        "type": "MultiPoint",
        "coordinates": unique_points
    }


def is_valid_coordinate(coord):
    """
    Check if a coordinate is valid [lon, lat] or [lon, lat, z].

    Validates:
    - Is list/tuple
    - Has at least 2 elements
    - First two elements are valid numbers
    - Longitude in range [-180, 180]
    - Latitude in range [-90, 90]
    """
    if not isinstance(coord, (list, tuple)) or len(coord) < 2:
        return False

    try:
        lon = float(coord[0])
        lat = float(coord[1])

        # Check valid ranges
        if lon < -180 or lon > 180:
            return False
        if lat < -90 or lat > 90:
            return False

        return True
    except (ValueError, TypeError):
        return False


def validate_geometry(geom):
    """
    Validate and clean a GeoJSON geometry.

    Handles:
    - Empty coordinate arrays
    - Invalid coordinate values
    - Unclosed rings
    - Single-point arrays (Shapely error)
    - GeometryCollections (validates each sub-geometry)

    Returns:
        Cleaned geometry dict, or None if invalid
    """
    if not geom or not isinstance(geom, dict):
        return None

    geom_type = geom.get("type")

    # Handle GeometryCollection
    if geom_type == "GeometryCollection":
        geometries = geom.get("geometries", [])
        if not geometries:
            return None

        # Validate each sub-geometry
        valid_geometries = []
        for sub_geom in geometries:
            validated = validate_geometry(sub_geom)
            if validated:
                valid_geometries.append(validated)

        if not valid_geometries:
            return None

        return {
            "type": "GeometryCollection",
            "geometries": valid_geometries
        }

    coords = geom.get("coordinates")

    if not coords:
        return None

    if geom_type == "Point":
        if is_valid_coordinate(coords):
            return geom
        return None

    elif geom_type == "LineString":
        valid_coords = [c for c in coords if is_valid_coordinate(c)]
        if len(valid_coords) >= 2:
            return {"type": "LineString", "coordinates": valid_coords}
        return None

    elif geom_type == "Polygon":
        valid_rings = []
        for ring in coords:
            if not isinstance(ring, list) or len(ring) < 4:
                continue
            valid_coords = [c for c in ring if is_valid_coordinate(c)]
            if len(valid_coords) >= 4:
                # Ensure closed
                if valid_coords[0] != valid_coords[-1]:
                    valid_coords.append(valid_coords[0])
                valid_rings.append(valid_coords)

        if valid_rings:
            return {"type": "Polygon", "coordinates": valid_rings}
        return None

    elif geom_type == "MultiPoint":
        valid_coords = [c for c in coords if is_valid_coordinate(c)]
        if valid_coords:
            return {"type": "MultiPoint", "coordinates": valid_coords}
        return None

    elif geom_type == "MultiLineString":
        valid_lines = []
        for line in coords:
            if not isinstance(line, list) or len(line) < 2:
                continue
            valid_coords = [c for c in line if is_valid_coordinate(c)]
            if len(valid_coords) >= 2:
                valid_lines.append(valid_coords)

        if valid_lines:
            return {"type": "MultiLineString", "coordinates": valid_lines}
        return None

    elif geom_type == "MultiPolygon":
        valid_polygons = []
        for poly in coords:
            if not isinstance(poly, list) or not poly:
                continue

            valid_rings = []
            for ring in poly:
                if not isinstance(ring, list) or len(ring) < 4:
                    continue
                valid_coords = [c for c in ring if is_valid_coordinate(c)]
                if len(valid_coords) >= 4:
                    # Ensure closed
                    if valid_coords[0] != valid_coords[-1]:
                        valid_coords.append(valid_coords[0])
                    valid_rings.append(valid_coords)

            if valid_rings:
                valid_polygons.append(valid_rings)

        if valid_polygons:
            return {"type": "MultiPolygon", "coordinates": valid_polygons}
        return None

    return None


# Test cases
if __name__ == '__main__':
    # Test GeometryCollection with mixed types
    gc = {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
            {"type": "Point", "coordinates": [0.5, 0.5]},
            {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}
        ]
    }

    result = process_geometry_collection(gc)
    print("Mixed GeometryCollection → MultiPolygon:")
    print(json.dumps(result, indent=2))

    # Test with only lines
    gc_lines = {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            {"type": "MultiLineString", "coordinates": [[[2, 2], [3, 3]], [[4, 4], [5, 5]]]}
        ]
    }

    result2 = process_geometry_collection(gc_lines)
    print("\nLines-only GeometryCollection → MultiLineString:")
    print(json.dumps(result2, indent=2))