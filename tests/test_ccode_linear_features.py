"""A river inside a country is in that country.

The bug, 5 August 2026: ``_filter_by_containment`` measured every non-point
intersection by AREA and discarded ``area <= 0`` as a shared-border touch. A
LineString intersected with a country polygon is a *line* — zero area, non-zero
length — so every linear feature was rejected.

Measured on the live index at cutover:

    osm  point   8,983,883   94.5% coded
    osm  line   10,078,925    0.0% coded   <-- every one
    osm  area      792,827   96.0% coded
    ohm  line      681,970    0.0% coded

10,760,895 ways — rivers, roads, waterways — with no country code at all, while
points and areas in the same namespaces were 92-97% coded. The stage reported
``completed`` and its own metrics recorded the loss as ``docs_no_match``:
9,993,573 for osm, i.e. "a candidate country was found and then rejected".
"""

from __future__ import annotations

import unittest

from shapely.geometry import LineString, Point, Polygon


class LinearFeaturesGetCodes(unittest.TestCase):

    def setUp(self):
        from processing.ccode_enrichment import _UnGeometryCache
        # A unit-square "country".
        self.country = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])

        class _Cache(_UnGeometryCache):
            def __init__(self, geoms):
                self._geoms = geoms

            def geoms_for(self, ccode):
                return self._geoms.get(ccode, [])

        self.cache = _Cache({"XX": [self.country]})

    def _filter(self, geom):
        from processing.ccode_enrichment import _filter_by_containment
        return _filter_by_containment(geom, ["XX"], self.cache)

    def test_line_wholly_inside_is_coded(self):
        """The actual failure: a river inside a country."""
        river = LineString([(2, 2), (8, 8)])
        self.assertEqual(self._filter(river), ["XX"])

    def test_line_crossing_the_border_is_coded(self):
        road = LineString([(-5, 5), (5, 5)])
        self.assertEqual(self._filter(road), ["XX"])

    def test_point_inside_is_coded(self):
        self.assertEqual(self._filter(Point(5, 5)), ["XX"])

    def test_area_inside_is_coded(self):
        lake = Polygon([(2, 2), (4, 2), (4, 4), (2, 4)])
        self.assertEqual(self._filter(lake), ["XX"])

    def test_line_entirely_outside_is_not_coded(self):
        self.assertEqual(self._filter(LineString([(20, 20), (30, 30)])), [])

    def test_border_touch_is_still_rejected(self):
        """The behaviour the area test was protecting must survive.

        A neighbour whose boundary merely touches — zero area AND zero length
        of overlap — must not be credited.
        """
        neighbour = Polygon([(10, 0), (20, 0), (20, 10), (10, 10)])
        self.assertEqual(self._filter(neighbour), [],
                         "a shared border is not containment")

    def test_multipoint_is_treated_as_a_point(self):
        """MultiPoint has zero area AND zero length; without the point branch
        it would be silently dropped like the lines were."""
        from shapely.geometry import MultiPoint
        self.assertEqual(self._filter(MultiPoint([(3, 3), (6, 6)])), ["XX"])


class MeasureIsDimensionAware(unittest.TestCase):

    def test_source_measures_area_or_length(self):
        from pathlib import Path
        src = Path("processing/ccode_enrichment.py").read_text()
        self.assertIn("inter.length if is_linear else inter.area", src,
                      "measure in the PLACE's dimension: 'area or length' also "
                      "credits two polygons that merely share a border")


if __name__ == "__main__":
    unittest.main()
