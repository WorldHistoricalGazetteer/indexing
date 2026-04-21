#!/usr/bin/env python3
"""Regression checks for hull-based H3 cover geometry selection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.helpers import select_h3_cover_geometry


POLYGON = {
    "type": "Polygon",
    "coordinates": [[
        [0.0, 0.0],
        [2.0, 0.0],
        [2.0, 2.0],
        [1.0, 1.0],
        [0.0, 2.0],
        [0.0, 0.0],
    ]],
}

HULL = {
    "type": "Polygon",
    "coordinates": [[
        [0.0, 0.0],
        [2.0, 0.0],
        [2.0, 2.0],
        [0.0, 2.0],
        [0.0, 0.0],
    ]],
}

LINE = {
    "type": "LineString",
    "coordinates": [[0.0, 0.0], [1.0, 0.5], [2.0, 0.0]],
}

POINT = {
    "type": "Point",
    "coordinates": [0.5, 0.5],
}


def run_tests():
    geom_entry = {"hull": HULL}
    assert select_h3_cover_geometry(geom_entry, POLYGON) == HULL

    # Non-area geometries keep their original semantics.
    assert select_h3_cover_geometry(geom_entry, LINE) == LINE
    assert select_h3_cover_geometry(geom_entry, POINT) == POINT

    # Missing or unusable hull falls back to the raw geometry.
    assert select_h3_cover_geometry({}, POLYGON) == POLYGON
    assert select_h3_cover_geometry({"hull": POINT}, POLYGON) == POLYGON
    assert select_h3_cover_geometry(geom_entry, None) is None

    print("Hull-based H3 cover geometry selection tests passed.")


if __name__ == "__main__":
    run_tests()

