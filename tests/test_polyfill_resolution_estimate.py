"""The polyfill resolution estimate must track real polyfills.

Until 3 September 2026 ``_H3_HEX_AREA_DEG2`` held areas that had been divided
by 111 (km per degree) instead of 111² (km² per degree²) — a units error,
uniform across all nine resolutions and therefore invisible to any check that
compared resolutions against each other. It made the estimate under-predict
cell counts by **~108×**, so ``_pick_polyfill_resolution`` started at r7 for
any polygon under 450 deg² when it should do so only under ~4.2 deg².

It was also latitude-blind: a degree of longitude is cos(latitude) shorter, so
a fixed-area H3 cell spans more degrees² near the poles and a bounding box of a
given degrees² covers less ground.

These tests compare the estimate against **actual h3 polyfills** rather than
against another formula, because the defect was in the numbers and a
self-consistent formula reproduces it exactly.
"""

from __future__ import annotations

import math
import unittest

from processing import helpers

try:
    import h3 as _h3
    _H3 = True
except ImportError:                                            # pragma: no cover
    _H3 = False

# The pre-fix table, kept ONLY so the tests can be shown to fail on it.
_BROKEN_TABLE = {
    0: 38000.0, 1: 5400.0, 2: 770.0, 3: 110.0,
    4: 16.0, 5: 2.2, 6: 0.31, 7: 0.045, 8: 0.0064,
}


def _square(lat: float, side: float = 4.0) -> dict:
    return {"type": "Polygon", "coordinates": [[
        [0.0, lat], [side, lat], [side, lat + side], [0.0, lat + side], [0.0, lat]]]}


def _actual_cells(lat: float, res: int, side: float = 4.0) -> int:
    return len(_h3.h3shape_to_cells(_h3.geo_to_h3shape(_square(lat, side)), res))


@unittest.skipUnless(_H3, "h3 not installed")
class EstimateTracksRealPolyfill(unittest.TestCase):
    """The estimate must be within a small factor of a real polyfill."""

    LATS = (0, 15, 30, 45, 60, 75, 80)

    def test_within_a_factor_of_two_across_latitudes(self) -> None:
        for lat in self.LATS:
            with self.subTest(lat=lat):
                actual = _actual_cells(lat, 5)
                est = helpers.estimate_polyfill_cells(16.0, lat + 2.0, 5)
                self.assertGreater(actual, 0)
                self.assertLess(max(est, actual) / min(est, actual), 2.0,
                                f"lat {lat}: estimate {est:.0f} vs actual {actual}")

    def test_the_broken_table_FAILS_that_bound(self) -> None:
        """The half everyone skips: show the test can fail on the old numbers."""
        worst = 0.0
        for lat in self.LATS:
            actual = _actual_cells(lat, 5)
            broken = 16.0 / _BROKEN_TABLE[5]          # latitude-blind, wrong units
            worst = max(worst, max(broken, actual) / min(broken, actual))
        self.assertGreater(worst, 50.0,
                           "the pre-fix table should be off by ~100x; if this "
                           "passes, the test is not measuring what it claims")

    def test_estimate_runs_low_not_high(self) -> None:
        """Under-estimating is the tolerable direction — the ladder recovers it.

        An over-estimate picks too coarse a resolution and nothing recovers the
        lost detail, so this pins the sign of the error, not just its size.
        """
        for lat in self.LATS:
            with self.subTest(lat=lat):
                actual = _actual_cells(lat, 5)
                est = helpers.estimate_polyfill_cells(16.0, lat + 2.0, 5)
                self.assertLessEqual(est, actual * 1.05,
                                     f"lat {lat}: estimate {est:.0f} exceeds actual {actual}")

    def test_scales_with_cos_latitude(self) -> None:
        eq = helpers.estimate_polyfill_cells(16.0, 0.0, 5)
        for lat in (30.0, 60.0, 80.0):
            with self.subTest(lat=lat):
                got = helpers.estimate_polyfill_cells(16.0, lat, 5)
                self.assertAlmostEqual(got, eq * math.cos(math.radians(lat)), delta=eq * 0.01)


@unittest.skipUnless(_H3, "h3 not installed")
class CellAreaDerivedFromH3(unittest.TestCase):

    def test_matches_h3_average_hexagon_area(self) -> None:
        for res in range(9):
            with self.subTest(res=res):
                expected = _h3.average_hexagon_area(res, unit="km^2") / (111.19492664455873 ** 2)
                self.assertAlmostEqual(helpers._h3_cell_area_deg2_equator(res), expected,
                                       delta=expected * 1e-9)

    def test_fallback_table_matches_the_derived_values(self) -> None:
        """The literal fallback must not drift from what h3 reports."""
        for res, literal in helpers._H3_HEX_AREA_DEG2_FALLBACK.items():
            with self.subTest(res=res):
                derived = helpers._h3_cell_area_deg2_equator(res)
                self.assertLess(abs(literal - derived) / derived, 0.01)

    def test_the_broken_table_would_NOT_match(self) -> None:
        for res, broken in _BROKEN_TABLE.items():
            with self.subTest(res=res):
                derived = helpers._h3_cell_area_deg2_equator(res)
                self.assertGreater(broken / derived, 50.0)


class ResolutionChoice(unittest.TestCase):

    def test_small_polygon_starts_fine(self) -> None:
        self.assertEqual(helpers._pick_polyfill_resolution(0.001, 0.0),
                         helpers.H3_CENTROID_RESOLUTION)

    def test_country_scale_polygon_steps_down(self) -> None:
        """100 deg² (a 10°x10° country) must not start at r7."""
        self.assertLess(helpers._pick_polyfill_resolution(100.0, 0.0),
                        helpers.H3_CENTROID_RESOLUTION)

    def test_the_broken_tables_defect_window(self) -> None:
        """Pins the ACTUAL defect, which is narrower than first assumed.

        The broken table stepped down above 450 deg² (0.045 x 10,000) and the
        correct one steps down above ~4.2 deg². So the window where the two
        disagree is 4.2-450 deg² — country-scale, not continent-scale. At
        1000 deg² even the broken table stepped down, which is why the
        docstring's own example never exposed it.
        """
        broken_threshold = _BROKEN_TABLE[helpers.H3_CENTROID_RESOLUTION] * helpers.H3_POLYFILL_MAX_CELLS
        self.assertAlmostEqual(broken_threshold, 450.0, delta=1.0)

        # inside the window: broken starts r7, fixed does not
        area = 100.0
        self.assertLessEqual(area / _BROKEN_TABLE[helpers.H3_CENTROID_RESOLUTION],
                             helpers.H3_POLYFILL_MAX_CELLS)
        self.assertLess(helpers._pick_polyfill_resolution(area, 0.0),
                        helpers.H3_CENTROID_RESOLUTION)

        # above the window both step down — so a test picked here proves nothing
        self.assertGreater(1000.0 / _BROKEN_TABLE[helpers.H3_CENTROID_RESOLUTION],
                           helpers.H3_POLYFILL_MAX_CELLS)

    def test_high_latitude_allows_a_finer_start_than_the_equator(self) -> None:
        """Same bbox degrees², less ground covered — so a finer start is correct."""
        area = 8.0
        eq = helpers._pick_polyfill_resolution(area, 0.0)
        polar = helpers._pick_polyfill_resolution(area, 75.0)
        self.assertGreaterEqual(polar, eq)

    def test_unknown_latitude_is_conservative(self) -> None:
        """Defaulting to 0.0 must give the coarsest (safest) of the choices."""
        for lat in (0.0, 30.0, 60.0, 85.0):
            self.assertLessEqual(helpers._pick_polyfill_resolution(50.0, 0.0),
                                 helpers._pick_polyfill_resolution(50.0, lat))

    def test_centre_lat_extracted_from_geometry(self) -> None:
        self.assertAlmostEqual(helpers._bbox_centre_lat(_square(40.0)), 42.0)
        self.assertEqual(helpers._bbox_centre_lat({}), 0.0)
        self.assertEqual(helpers._bbox_centre_lat({"coordinates": []}), 0.0)


if __name__ == "__main__":                                     # pragma: no cover
    unittest.main()
