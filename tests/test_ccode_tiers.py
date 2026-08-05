"""Tier-2 fallback and the disputed-claims overlay (place#173).

The tiers must be consulted **in order and kept separate**, never merged.
Merging geoBoundaries (73,663 vertices/country) with BNDA (232) would put two
different outlines along the same border, and every disagreement becomes a
sliver where a place is claimed by both countries or neither. Fallback-on-empty
makes that impossible: a place is either inside a primary polygon and answered
there, or it is in a hole the primary does not cover at all.

The overlay is separate from the fallback because the failure it addresses is
different: for Western Sahara the primary DOES return an answer (`MAR`), so no
fallback would ever fire, and 4,387 places would silently become Moroccan.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path


def _square(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[
        [x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


class TieredResolution(unittest.TestCase):

    def test_primary_answer_stops_the_fallback(self):
        """Tier 2 must not run when tier 1 answered — that is the whole point."""
        from processing.ccode_tiers import resolve_tiered
        called = []

        def fallback():
            called.append(True)
            return ["XX"]

        codes, tier = resolve_tiered(lambda: ["CN"], fallback)
        self.assertEqual(codes, ["CN"])
        self.assertEqual(tier, "primary")
        self.assertEqual(called, [],
                         "consulting tier 2 after a tier-1 hit would reintroduce "
                         "exactly the border conflicts the tiers exist to avoid")

    def test_fallback_fires_only_on_empty(self):
        from processing.ccode_tiers import resolve_tiered
        codes, tier = resolve_tiered(lambda: [], lambda: ["HK"])
        self.assertEqual(codes, ["HK"])
        self.assertEqual(tier, "fallback")

    def test_neither_tier_answers(self):
        from processing.ccode_tiers import resolve_tiered
        codes, tier = resolve_tiered(lambda: [], lambda: [])
        self.assertEqual(codes, [])
        self.assertEqual(tier, "none")

    def test_tier_is_reported(self):
        """Fallback usage must be measurable; a rising rate means the primary
        source has developed holes."""
        from processing.ccode_tiers import resolve_tiered
        self.assertEqual(resolve_tiered(lambda: ["FR"], lambda: [])[1], "primary")
        self.assertEqual(resolve_tiered(lambda: [], lambda: ["JE"])[1], "fallback")


class DisputedClaimsOverlay(unittest.TestCase):

    def setUp(self):
        self.territories = [{
            "name": "Test disputed zone",
            "claimants": ["MA", "EH"],
            "bbox": [-17.0, 20.0, -8.0, 28.0],
            "geometry": _square(-17.0, 20.0, -8.0, 28.0),
        }]

    def test_overlay_adds_claimants_to_a_primary_answer(self):
        """The Western Sahara case: the primary answered, so no fallback fires."""
        from processing.ccode_tiers import resolve_tiered
        codes, tier = resolve_tiered(
            lambda: ["MA"], lambda: [],
            lon=-13.203, lat=27.154, territories=self.territories)
        self.assertEqual(codes, ["EH", "MA"])
        self.assertEqual(tier, "primary",
                         "the overlay must not disguise which tier answered")

    def test_overlay_is_additive_never_subtractive(self):
        """Claiming a source is WRONG about administration is a larger claim
        than claiming a territory is contested. We only make the latter."""
        from processing.ccode_tiers import apply_overlay
        out = apply_overlay(["MA"], -13.203, 27.154, self.territories)
        self.assertIn("MA", out, "the source's own answer must survive")
        self.assertIn("EH", out)

    def test_points_outside_are_untouched(self):
        from processing.ccode_tiers import apply_overlay
        self.assertEqual(apply_overlay(["FR"], 2.35, 48.85, self.territories),
                         ["FR"])

    def test_missing_coordinates_are_safe(self):
        from processing.ccode_tiers import apply_overlay
        self.assertEqual(apply_overlay(["FR"], None, None, self.territories),
                         ["FR"])

    def test_inert_entry_without_geometry_does_nothing(self):
        """Entries are listed before a polygon is sourced so the outstanding
        decision stays visible; they must not throw."""
        from processing.ccode_tiers import apply_overlay
        inert = [{"name": "no polygon yet", "claimants": ["XX"],
                  "bbox": [-180, -90, 180, 90], "geometry": None}]
        self.assertEqual(apply_overlay(["FR"], 2.35, 48.85, inert), ["FR"])


class ShippedOverlayData(unittest.TestCase):

    def setUp(self):
        self.path = Path("processing/data/disputed_claims.json")
        self.data = json.loads(self.path.read_text())

    def test_file_parses_and_loads(self):
        from processing.ccode_tiers import load_disputed_claims
        self.assertIsInstance(load_disputed_claims(self.path), list)

    def test_western_sahara_is_recorded_with_its_decision(self):
        entry = next(t for t in self.data["territories"]
                     if t["name"] == "Western Sahara")
        self.assertEqual(sorted(entry["claimants"]), ["EH", "MA"])
        self.assertIn("decided", entry)
        self.assertTrue(entry["evidence"],
                        "a sovereignty decision must carry its reasoning")

    def test_undecided_disputes_are_listed_not_silently_dropped(self):
        """Eight disputes are political judgements left for explicit decision.
        Recording them is what stops them being forgotten."""
        names = {t["name"] for t in self.data["undecided"]}
        for expected in ("Golan Heights", "Kuril Islands", "Crimea"):
            self.assertTrue(any(expected in n for n in names),
                            f"{expected} must remain visible as undecided")

    def test_each_undecided_records_the_behaviour_change(self):
        for t in self.data["undecided"]:
            self.assertIn("today_bnda", t)
            self.assertIn("after_switch", t)


if __name__ == "__main__":
    unittest.main()
