"""Unit tests for processing/ccode_enrichment.py (Batch 7 validation gates).

Covers:

* Point-in-polygon for points (single matching country, no border ambiguity).
* Point exactly on a shared border yields all matching countries.
* Polygon overlap returns every intersecting country.
* Majority-overlap tie-break orders ccodes by descending intersection area.
* Touch-only intersections (zero-area) are dropped.
* H3 prefilter widens the candidate set conservatively (parent walk).
"""

from __future__ import annotations

import unittest

from shapely.geometry import Point, Polygon

from processing.ccode_enrichment import (
    PREFILTER_RESOLUTION,
    _UnGeometryCache,
    _filter_by_containment,
    _normalise_cells_to_resolution,
    build_un_prefilter,
    candidate_ccodes_for_cells,
)


def _square(lon0: float, lat0: float, side: float = 1.0) -> Polygon:
    return Polygon([
        (lon0, lat0),
        (lon0 + side, lat0),
        (lon0 + side, lat0 + side),
        (lon0, lat0 + side),
        (lon0, lat0),
    ])


def _un_doc(ccode: str, place_id: str, polygon: Polygon, h3_cells: list[str]) -> dict:
    """Build a synthetic UN doc shaped like a staged record."""
    return {
        "place_id": place_id,
        "ccodes": [ccode],
        "geometries": [{
            "geometry_index": 0,
            "geom_ref": place_id + "_0",
            "h3_cover": h3_cells,
            "hull": polygon.__geo_interface__,
            "repr_point": {"lon": polygon.centroid.x, "lat": polygon.centroid.y},
        }],
    }


class _StubCache(_UnGeometryCache):
    """UN geometry cache that serves Shapely polygons directly from a dict.

    Bypasses the geom_store reader so tests don't need an on-disk store.
    """

    def __init__(self, ccode_to_polygons: dict[str, list[Polygon]]):
        super().__init__(ccode_to_geoms={})  # parent init is harmless
        self._ccode_to_polygons = ccode_to_polygons

    def geoms_for(self, ccode: str):  # type: ignore[override]
        return list(self._ccode_to_polygons.get(ccode, []))


class TestPrefilter(unittest.TestCase):
    """build_un_prefilter + candidate_ccodes_for_cells."""

    def test_normalisation_walks_parents_and_children(self):
        try:
            import h3 as _h3
        except ImportError:
            self.skipTest("h3 library not installed")

        # A res-7 cell over central France.
        fine_cell = _h3.latlng_to_cell(46.6, 2.6, 7)
        coarse_cell = _h3.latlng_to_cell(46.6, 2.6, 4)

        # res-7 → res-4 by walking up.
        out = _normalise_cells_to_resolution([fine_cell], target_res=4)
        self.assertEqual(out, {coarse_cell})

        # res-4 → res-7 by descent.
        out = _normalise_cells_to_resolution([coarse_cell], target_res=7)
        self.assertIn(fine_cell, out)
        self.assertGreater(len(out), 1)

    def test_candidate_lookup_finds_overlapping_ccodes(self):
        try:
            import h3 as _h3
        except ImportError:
            self.skipTest("h3 library not installed")

        # Two synthetic UN coverages at adjacent res-4 cells.
        fr_cell = _h3.latlng_to_cell(46.6, 2.6, PREFILTER_RESOLUTION)
        es_cell = _h3.latlng_to_cell(40.4, -3.7, PREFILTER_RESOLUTION)

        un_docs = [
            _un_doc("FR", "un:fra",
                    _square(2.0, 46.0, 1.0), [fr_cell]),
            _un_doc("ES", "un:esp",
                    _square(-4.0, 40.0, 1.0), [es_cell]),
        ]
        cell_to_ccodes, _ = build_un_prefilter(un_docs)

        # A place at the same res-4 cell as France should be a candidate.
        place_cell = _h3.latlng_to_cell(46.6, 2.6, 7)
        candidates = candidate_ccodes_for_cells([place_cell], cell_to_ccodes)
        self.assertEqual(candidates, {"FR"})

        # A place outside both coverages → no candidate.
        far_cell = _h3.latlng_to_cell(0.0, 0.0, 7)
        self.assertEqual(
            candidate_ccodes_for_cells([far_cell], cell_to_ccodes), set()
        )


class TestFilterByContainment(unittest.TestCase):
    """_filter_by_containment — point-in-polygon + polygon-overlap."""

    def setUp(self):
        # Two unit squares that share an edge along longitude 1.0.
        self.fr = _square(0.0, 0.0, 1.0)
        self.de = _square(1.0, 0.0, 1.0)
        self.cache = _StubCache({"FR": [self.fr], "DE": [self.de]})

    def test_point_inside_one_country(self):
        place_geom = Point(0.5, 0.5)
        result = _filter_by_containment(place_geom, ["FR", "DE"], self.cache)
        self.assertEqual(result, ["FR"])

    def test_point_on_shared_border_matches_both(self):
        # A point exactly on the shared edge intersects both polygons.
        place_geom = Point(1.0, 0.5)
        result = _filter_by_containment(place_geom, ["FR", "DE"], self.cache)
        self.assertCountEqual(result, ["FR", "DE"])

    def test_polygon_intersecting_two_countries_returns_both(self):
        place_geom = _square(0.5, 0.25, 1.0)  # straddles FR (0..1) and DE (1..2)
        result = _filter_by_containment(place_geom, ["FR", "DE"], self.cache)
        self.assertCountEqual(result, ["FR", "DE"])

    def test_majority_overlap_orders_largest_first(self):
        # 90% in FR, 10% in DE.
        place_geom = _square(0.1, 0.25, 1.0)  # spans 0.1..1.1 longitude
        result = _filter_by_containment(place_geom, ["FR", "DE"], self.cache)
        self.assertEqual(result, ["FR", "DE"])

        # Now 10% in FR, 90% in DE.
        place_geom = _square(0.9, 0.25, 1.0)  # spans 0.9..1.9 longitude
        result = _filter_by_containment(place_geom, ["FR", "DE"], self.cache)
        self.assertEqual(result, ["DE", "FR"])

    def test_touch_only_intersection_drops_zero_area_match(self):
        # An area that only TOUCHES France along its eastern edge — zero
        # intersection area; should be dropped (in the area branch only).
        place_geom = _square(1.0, 0.25, 0.5)  # fully inside DE, touches FR
        result = _filter_by_containment(place_geom, ["FR", "DE"], self.cache)
        # Touch-only FR is dropped; DE remains.
        self.assertEqual(result, ["DE"])

    def test_no_candidate_match_returns_empty(self):
        place_geom = Point(50.0, 50.0)
        result = _filter_by_containment(place_geom, ["FR", "DE"], self.cache)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
