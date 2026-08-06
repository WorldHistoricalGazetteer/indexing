"""Tier 2 must cover every country, not only those the primary lacks.

The bug, 6 August 2026: `split_by_tier` placed a country in tier 2 only when
*all* its geometries were BNDA-sourced — i.e. only the ~18 that geoBoundaries
does not carve out. So a place just outside geoBoundaries' finer coastline got
tier 1 = nothing, then tier 2 = a set that did not contain its country either,
and ended with **no country code at all**.

464 places on the 5 Aug 2026 corpus run: `VI` 33, `AS` 96, `GU` 27, `MP` 28,
`BQ` 280 — every one in a territory geoBoundaries *does* cover, so the fallback
could never fire for them.

Two distinct causes, both answered by the same widening:

* **Precision.** The uncoded `AS`/`VI` places are overwhelmingly coastal —
  capes, bays, coves, rocks, piers — whose representative point sits a few
  metres seaward. A 232-vertex outline swallowed them; a 73,663-vertex one
  correctly does not. Accuracy *creates* uncoded coastal places.
* **Omission.** geoBoundaries models `BQ` as ONE polygon spanning
  (-68.421, 12.025)..(-68.195, 12.312) — Bonaire only. Saba and Sint Eustatius
  are absent entirely, including their own administrative polygons. BNDA models
  `BQ` as three features, one per island.

Widening tier 2 does not reintroduce the mixed-resolution sliver problem: the
ordering is unchanged and absolute, so there is never a tier-1 answer for a
tier-2 answer to disagree with.
"""

from __future__ import annotations

import unittest

from shapely.geometry import LineString, Point, Polygon


class FullBndaTierMembership(unittest.TestCase):

    def test_tier_includes_countries_the_primary_also_covers(self):
        """The whole point: `US` and `NL` are in tier 1, and must ALSO be in
        tier 2, or a place outside their fine coastline has nowhere to go."""
        from processing.ccode_tiers import load_full_bnda_tier
        entries = load_full_bnda_tier()
        if not entries:
            self.skipTest("BNDA source file not available")
        codes = {cc for cc, _ in entries}
        for cc in ("US", "NL", "AU", "FI", "CN"):
            self.assertIn(cc, codes,
                          f"{cc} is in the primary tier and must still appear "
                          f"in the full BNDA fallback")

    def test_multi_island_territories_keep_every_part(self):
        """`BQ` is three islands ~800 km apart; collapsing them to one entry
        is how Saba and Sint Eustatius went missing."""
        from processing.ccode_tiers import load_full_bnda_tier
        entries = load_full_bnda_tier()
        if not entries:
            self.skipTest("BNDA source file not available")
        bq = [g for cc, g in entries if cc == "BQ"]
        self.assertGreaterEqual(len(bq), 3,
                                "BQ must retain a feature per island")

    def test_longitudes_are_normalised(self):
        """BNDA ships the US Aleutians unwrapped to lon 191."""
        from processing.ccode_tiers import load_full_bnda_tier
        entries = load_full_bnda_tier()
        if not entries:
            self.skipTest("BNDA source file not available")
        for cc, g in entries:
            minx, _, maxx, _ = g.bounds
            self.assertGreaterEqual(minx, -180.0001, f"{cc} lon < -180")
            self.assertLessEqual(maxx, 180.0001, f"{cc} lon > 180")


class FallbackIndexResolves(unittest.TestCase):

    def setUp(self):
        from processing.ccode_tiers import BndaFallbackIndex, load_full_bnda_tier
        entries = load_full_bnda_tier()
        if not entries:
            self.skipTest("BNDA source file not available")
        self.idx = BndaFallbackIndex(entries)

    def test_the_actual_uncoded_places_now_resolve(self):
        """Real coordinates taken from the uncoded set."""
        cases = [
            ("Fort Panga, Sint Eustatius", -62.990012, 17.495315, "BQ"),
            ("Vaisigano Point, Am. Samoa", -170.799487, -14.334066, "AS"),
            ("Cinnamon Bay, US VI", -64.759304, 18.354398, "VI"),
            ("Coral Bay, US VI", -64.690969, 18.330233, "VI"),
        ]
        for label, lon, lat, expected in cases:
            got = self.idx.ccodes_for(Point(lon, lat))
            self.assertIn(expected, got, f"{label}: got {got}")

    def test_all_three_bq_islands_resolve(self):
        for label, lon, lat in (("Bonaire", -68.26, 12.18),
                                ("Sint Eustatius", -62.99, 17.52),
                                ("Saba", -63.24, 17.63)):
            self.assertIn("BQ", self.idx.ccodes_for(Point(lon, lat)), label)

    def test_open_ocean_still_resolves_to_nothing(self):
        """The fallback must not become a catch-all that codes the mid-Pacific."""
        self.assertEqual(self.idx.ccodes_for(Point(-140.0, -30.0)), [])

    def test_an_areal_place_gets_its_country(self):
        poly = Polygon([(-63.00, 17.47), (-62.95, 17.47),
                        (-62.95, 17.52), (-63.00, 17.52)])
        self.assertIn("BQ", self.idx.ccodes_for(poly))

    def test_a_linear_place_is_measured_by_length_not_area(self):
        """A line has zero area; measuring by area discarded every way once
        already (see test_ccode_linear_features)."""
        line = LineString([(-62.99, 17.48), (-62.97, 17.50)])
        self.assertIn("BQ", self.idx.ccodes_for(line))

    def test_empty_geometry_is_safe(self):
        self.assertEqual(self.idx.ccodes_for(None), [])
        self.assertEqual(self.idx.ccodes_for(Polygon()), [])


if __name__ == "__main__":
    unittest.main()
