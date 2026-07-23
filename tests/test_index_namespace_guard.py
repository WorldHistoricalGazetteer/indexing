"""The pre-index enrichment gate in processing.index_namespace (place#145).

The gate used to check only ``has_geom`` geometries for a missing ``h3_cover``,
so a point-only namespace staged without the h3 stage passed it silently — how
chgis/og/tm reached prod with no h3 fields (and, for tm, no bounds) at all.
"""

import unittest

from processing.index_namespace import _has_uncovered_geometry as uncovered


def _doc(*geoms):
    return {"place_id": "x:1", "geometries": list(geoms)}


class TestUncoveredGeometryGate(unittest.TestCase):
    def test_polygon_without_cover_still_caught(self):
        # the original check — h3 stage not merged for an area feature
        self.assertTrue(uncovered(_doc({"has_geom": True, "repr_point": {"lon": 1, "lat": 2}})))

    def test_point_without_h3_is_caught(self):
        # chgis shape: enriched (bounds present) but the h3 stage never ran
        self.assertTrue(uncovered(_doc(
            {"repr_point": {"lon": 1, "lat": 2}, "bounds": [1, 2, 1, 2]})))

    def test_point_without_bounds_is_caught(self):
        # tm shape: h3 fields present, but the entry was hand-built so
        # enrich_geometry never wrote bounds
        self.assertTrue(uncovered(_doc(
            {"repr_point": {"lon": 1, "lat": 2},
             "h3_centroid": "87…", "h3_cover": ["87…"]})))

    def test_fully_enriched_point_passes(self):
        self.assertFalse(uncovered(_doc(
            {"repr_point": {"lon": 1, "lat": 2}, "h3_centroid": "87…",
             "h3_cover": ["87…"], "bounds": [1, 2, 1, 2]})))

    def test_fully_enriched_polygon_passes(self):
        self.assertFalse(uncovered(_doc(
            {"has_geom": True, "repr_point": {"lon": 1, "lat": 2},
             "h3_centroid": "87…", "h3_cover": ["87…", "87‥"],
             "bounds": [0, 0, 2, 2]})))

    def test_doc_without_geometry_is_not_flagged(self):
        # a place with no location at all is legitimate — not an enrichment gap
        self.assertFalse(uncovered(_doc()))
        self.assertFalse(uncovered({"place_id": "x:2"}))

    def test_one_bad_geometry_among_several_is_caught(self):
        self.assertTrue(uncovered(_doc(
            {"repr_point": {"lon": 1, "lat": 2}, "h3_centroid": "87…",
             "h3_cover": ["87…"], "bounds": [1, 2, 1, 2]},
            {"repr_point": {"lon": 3, "lat": 4}})))


if __name__ == "__main__":
    unittest.main()
