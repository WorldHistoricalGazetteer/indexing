"""Unit tests for processing.fix_point_derived_fields._derive (place#145).

Fixtures are the real ``_source`` shapes taken from the live index on
2026-07-23 — the three gaps differ, and each must be filled from what that
geometry actually carries rather than from a single assumed source.
"""

import unittest

try:
    import h3  # noqa: F401
    import shapely  # noqa: F401
    _DEPS = True
except Exception:  # pragma: no cover
    _DEPS = False

if _DEPS:
    from processing.fix_point_derived_fields import _derive


@unittest.skipUnless(_DEPS, "h3 + shapely required")
class TestDerive(unittest.TestCase):
    def test_chgis_null_h3_fields_filled_from_repr_point(self):
        # chgis: bounds already correct, h3 fields explicitly null.
        g = {"h3_centroid": None, "h3_cover": None,
             "bounds": [101.97659, 31.85892, 101.97659, 31.85892],
             "has_geom": False, "repr_point": {"lon": 101.97659, "lat": 31.85892}}
        d = _derive(g)
        self.assertEqual(set(d), {"h3_centroid", "h3_cover"})   # bounds untouched
        self.assertEqual(d["h3_cover"], [d["h3_centroid"]])     # point convention
        self.assertEqual(h3.get_resolution(d["h3_centroid"]), 7)
        self.assertEqual(h3.latlng_to_cell(31.85892, 101.97659, 7), d["h3_centroid"])

    def test_og_point_only_gets_all_three(self):
        # og: a wd-derived centroid with nothing else at all.
        g = {"approximation": "centroid", "source": "wd", "has_geom": False,
             "repr_point": {"lon": 48.304167, "lat": 30.339167}}
        d = _derive(g)
        self.assertEqual(set(d), {"h3_centroid", "h3_cover", "bounds"})
        # a point's envelope is itself
        self.assertEqual(d["bounds"], [48.304167, 30.339167, 48.304167, 30.339167])

    def test_tm_bounds_from_inline_point_geom(self):
        # tm: h3 fields fine, bounds missing, inline Point geom present.
        g = {"h3_centroid": "873e62553ffffff", "h3_cover": ["873e62553ffffff"],
             "geom": {"type": "Point", "coordinates": [30.897603, 29.5164395]},
             "repr_point": {"lon": 30.897603, "lat": 29.5164395}}
        d = _derive(g)
        self.assertEqual(set(d), {"bounds"})
        self.assertEqual(d["bounds"], [30.897603, 29.51644, 30.897603, 29.51644])

    def test_inline_hull_beats_repr_point(self):
        # WHG-computed approximation polygons (ottgaz admin hulls) live inline
        # with has_geom=False — the cover must come from the POLYGON, not from a
        # point that would collapse it to one cell.
        hull = {"type": "Polygon", "coordinates": [[[0.5, 50.5], [1.5, 50.5],
                                                    [1.5, 51.5], [0.5, 51.5],
                                                    [0.5, 50.5]]]}
        g = {"has_geom": False, "repr_point": {"lon": 1.0, "lat": 51.0}, "hull": hull}
        d = _derive(g)
        self.assertGreater(len(d["h3_cover"]), 1)
        self.assertEqual(d["bounds"], [0.5, 50.5, 1.5, 51.5])

    def test_complete_geometry_is_a_noop(self):
        g = {"h3_centroid": "8740c1858ffffff", "h3_cover": ["8740c1858ffffff"],
             "bounds": [0, 0, 1, 1], "repr_point": {"lon": 0.5, "lat": 0.5}}
        self.assertEqual(_derive(g), {})

    def test_no_repr_point_cannot_be_derived(self):
        self.assertEqual(_derive({"has_geom": False, "timespans": []}), {})


if __name__ == "__main__":
    unittest.main()
