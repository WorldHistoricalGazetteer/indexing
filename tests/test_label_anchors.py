"""One label per boundary, at a point actually inside it (place#159).

A polygon is cut at every tile edge, and MapLibre draws one symbol per feature
*per tile* — so Nebraska carried five "Nebraska" labels at z6.2 and Italy eight
"Italia" at z5. It reads as several different places sharing a name. Nothing
client-side can fix it: choosing the right fragment needs the label point,
which is exactly what the tileset did not carry.

The anchor goes in the **same source-layer** as the shapes, marked `label: 1`
(plan-atlas-data-architecture.md §3.1, which withdrew the earlier
separate-layer design).

Two properties are worth pinning, because getting either wrong is invisible
until it reaches a map:

* the anchor must be **inside** its polygon — a centroid is not, for a concave
  region, which is the whole reason the staged `repr_point` was rejected too;
* point features must yield **no** anchor, or `gn`'s ~12M features double.
"""

from __future__ import annotations

import unittest

from shapely.geometry import Polygon, shape


def _feature(geom, **props):
    return {"type": "Feature",
            "properties": {"name": "Test", "place_id": "x:1", **props},
            "geometry": geom.__geo_interface__ if hasattr(geom, "__geo_interface__")
            else geom}


class LabelAnchorGeometry(unittest.TestCase):

    def test_anchor_is_inside_a_convex_polygon(self):
        from processing.generate_tiles import _label_point_feature
        poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        f = _label_point_feature(_feature(poly))
        self.assertIsNotNone(f)
        pt = shape(f["geometry"])
        self.assertTrue(poly.contains(pt))

    def test_anchor_is_inside_a_CONCAVE_polygon(self):
        """The case that rules out the centroid — a C shape whose centroid
        falls in the notch, outside the polygon entirely."""
        from processing.generate_tiles import _label_point_feature
        c_shape = Polygon([(0, 0), (10, 0), (10, 3), (3, 3), (3, 7),
                           (10, 7), (10, 10), (0, 10)])
        self.assertFalse(c_shape.contains(c_shape.centroid),
                         "fixture is not concave enough to be a real test")
        f = _label_point_feature(_feature(c_shape))
        pt = shape(f["geometry"])
        self.assertTrue(c_shape.contains(pt),
                        "label anchor landed outside its own polygon")

    def test_multipolygon_anchors_on_the_largest_part(self):
        """Otherwise the country's label lands on an offshore islet."""
        from processing.generate_tiles import _label_point_feature
        mainland = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        islet = Polygon([(50, 50), (50.2, 50), (50.2, 50.2), (50, 50.2)])
        geom = {"type": "MultiPolygon",
                "coordinates": [list(mainland.__geo_interface__["coordinates"]),
                                list(islet.__geo_interface__["coordinates"])]}
        f = _label_point_feature(_feature(geom))
        pt = shape(f["geometry"])
        self.assertTrue(mainland.contains(pt), "anchored on the islet")

    def test_a_huge_ring_still_produces_an_anchor(self):
        """polylabel is iterative and unbounded; large rings are simplified
        first. Australia's real outline is 1,655,696 vertices."""
        import math
        from processing.generate_tiles import _label_point_feature
        n = 20_000
        ring = [(math.cos(2 * math.pi * i / n) * 10,
                 math.sin(2 * math.pi * i / n) * 10) for i in range(n)]
        poly = Polygon(ring)
        f = _label_point_feature(_feature(poly))
        self.assertIsNotNone(f)
        self.assertTrue(poly.contains(shape(f["geometry"])))


class LabelFeatureShape(unittest.TestCase):

    def test_marked_with_label_1_not_a_separate_layer(self):
        from processing.generate_tiles import _label_point_feature
        f = _label_point_feature(_feature(Polygon([(0, 0), (1, 0), (1, 1)])))
        self.assertEqual(f["properties"]["label"], 1)

    def test_carries_the_properties_the_style_filters_on(self):
        """The style's label layers filter on `boundary`; whg3's date filter
        reads start/end. Losing either silently unstyles or undates labels."""
        from processing.generate_tiles import _label_point_feature
        f = _label_point_feature(_feature(
            Polygon([(0, 0), (1, 0), (1, 1)]),
            boundary="administrative", start=1850, end=1974,
            name_en="Nebraska", name_fr="Nebraska"))
        p = f["properties"]
        for k in ("place_id", "boundary", "name", "start", "end", "name_en"):
            self.assertIn(k, p, f"{k} missing from label properties")

    def test_drops_properties_labels_do_not_need(self):
        from processing.generate_tiles import _label_point_feature
        f = _label_point_feature(_feature(
            Polygon([(0, 0), (1, 0), (1, 1)]),
            aat="300008347", population=1_900_000, fcode="ADM1"))
        for k in ("aat", "population", "fcode"):
            self.assertNotIn(k, f["properties"])

    def test_shares_the_place_id_so_feature_state_is_shared(self):
        from processing.generate_tiles import _label_point_feature
        f = _label_point_feature(_feature(
            Polygon([(0, 0), (1, 0), (1, 1)]), place_id="osm:r161648"))
        self.assertEqual(f["properties"]["place_id"], "osm:r161648")


class NonPolygonsGetNoAnchor(unittest.TestCase):

    def test_point_features_yield_nothing(self):
        """A point is its own anchor; anchoring points would double gn."""
        from processing.generate_tiles import _label_point_feature
        f = _label_point_feature(_feature(
            {"type": "Point", "coordinates": [1.0, 2.0]}))
        self.assertIsNone(f)

    def test_line_features_yield_nothing_here(self):
        from processing.generate_tiles import _label_point_feature
        f = _label_point_feature(_feature(
            {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}))
        self.assertIsNone(f)

    def test_a_nameless_polygon_yields_nothing(self):
        """No name, nothing to draw — an empty label is pure tile bytes."""
        from processing.generate_tiles import _label_point_feature
        f = _label_point_feature({
            "type": "Feature", "properties": {"place_id": "x:1"},
            "geometry": Polygon([(0, 0), (1, 0), (1, 1)]).__geo_interface__})
        self.assertIsNone(f)

    def test_malformed_geometry_is_survivable(self):
        from processing.generate_tiles import _label_point_feature
        self.assertIsNone(_label_point_feature({"type": "Feature"}))
        self.assertIsNone(_label_point_feature(
            {"type": "Feature", "properties": {"name": "x"},
             "geometry": {"type": "Polygon", "coordinates": []}}))


if __name__ == "__main__":
    unittest.main()
