"""Longitudes must land in [-180, 180] before Elasticsearch sees them.

ES rejects the **whole document** on an out-of-range ``geo_point``:

    illegal longitude value [351.83] for repr_point

That cost 3,636 ``wd`` docs and one ``nl`` doc on the place#164 rebuild. Sources
disagree on convention — Wikidata carries some coordinates in [0, 360] and some
already shifted past the dateline — so the fold belongs in the shared
enrichment layer rather than in whichever authority script trips over it next.
"""

from __future__ import annotations

import unittest

from processing.helpers import wrap_longitude


class WrapLongitudeTests(unittest.TestCase):
    def test_the_values_that_actually_failed(self):
        """Every case is a real document from the rebuild."""
        for lon, expected, who in (
            (351.83, -8.17, "wd:Q24227"),
            (336.0, -24.0, "wd:Q45326"),
            (-236.4, 123.6, "wd:Q219269"),
            (-186.052087, 173.947913, "nl:treaty:he-whakaputanga-moana"),
        ):
            self.assertAlmostEqual(wrap_longitude(lon), expected, places=6, msg=who)

    def test_in_range_values_are_untouched(self):
        for lon in (0.0, 1.5, -1.5, 179.999999, -179.999999, 45.0, -122.4194):
            self.assertEqual(wrap_longitude(lon), lon)

    def test_dateline_endpoints_are_preserved(self):
        # +180 and -180 are both legitimate and distinct; folding +180 to -180
        # would silently move a point across the dateline.
        self.assertEqual(wrap_longitude(180.0), 180.0)
        self.assertEqual(wrap_longitude(-180.0), -180.0)

    def test_multiple_wraps(self):
        self.assertAlmostEqual(wrap_longitude(720.0 + 10.0), 10.0, places=6)
        self.assertAlmostEqual(wrap_longitude(-720.0 - 10.0), -10.0, places=6)

    def test_result_is_always_in_range(self):
        for lon in range(-1000, 1001, 7):
            self.assertTrue(-180.0 <= wrap_longitude(float(lon)) <= 180.0, lon)

    def test_non_numeric_passes_through(self):
        for value in (None, "", "abc", {}, []):
            self.assertEqual(wrap_longitude(value), value)


if __name__ == "__main__":
    unittest.main()
