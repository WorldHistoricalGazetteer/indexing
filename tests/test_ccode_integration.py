"""The tier logic and overlay must be REACHED by the resolver, not merely exist.

`processing/ccode_tiers.py` and `split_by_tier` were written, tested and
committed while `run_ccode_enrichment` still called the original single-tier
path. Nothing imported them; the design was described as implemented while the
resolver ignored it. It would have produced plausible output — with both
sources merged in one candidate pool, Hong Kong resolves via BNDA's polygon and
Laayoune returns both MA and EH — but by the merged-set mechanism the tiering
exists to avoid, and with the overlay never exercised.

These tests assert integration, not just presence.
"""

from __future__ import annotations

import unittest
from pathlib import Path

SRC = Path("processing/ccode_enrichment.py").read_text()


class TieringIsReached(unittest.TestCase):

    def test_resolver_splits_the_records(self):
        self.assertIn("split_by_tier(un_records)", SRC)

    def test_primary_prefilter_is_built_from_the_primary_records(self):
        self.assertIn("build_un_prefilter(primary_records)", SRC)

    def test_fallback_index_is_constructed_and_used(self):
        """Tier 2 is now the FULL BNDA set behind an STRtree, not a second H3
        prefilter over the countries geoBoundaries happens to lack.

        Keying tier 2 on "geoBoundaries lacks this country" left 464 places in
        VI, AS, GU, MP and BQ with no code at all: tier 1 said nothing, and
        their own country was in tier 1 and so absent from tier 2.
        """
        self.assertIn("BndaFallbackIndex", SRC)
        self.assertIn("fb_index = BndaFallbackIndex()", SRC)
        self.assertIn("fb_index.ccodes_for(place_geom)", SRC)

    def test_fallback_runs_only_when_primary_is_empty(self):
        i = SRC.index("tier = \"primary\" if ccodes else \"none\"")
        window = SRC[i:i + 400]
        self.assertIn("if not ccodes and fb_index", window,
                      "tier 2 must be gated on tier 1 returning nothing — "
                      "consulting it unconditionally is the merged set again")

    def test_no_tier1_candidates_no_longer_skips_the_document(self):
        """A place with no tier-1 candidate must still reach tier 2.

        Previously `not candidates and not fb_candidates` skipped the document
        as `docs_no_candidate`, which is exactly the population that ended
        uncoded — tier 2 has no H3 prefilter now, so there is nothing to be
        absent from.
        """
        self.assertNotIn("if not candidates and not fb_candidates", SRC)
        self.assertIn("if not candidates and not fb_index", SRC)

    def test_fallback_usage_is_counted(self):
        self.assertIn("docs_from_fallback", SRC)
        self.assertIn('"docs_from_fallback": docs_from_fallback', SRC)


class OverlayIsReached(unittest.TestCase):

    def test_overlay_is_imported_and_called(self):
        self.assertIn("from processing.ccode_tiers import", SRC)
        self.assertIn("apply_overlay(ccodes,", SRC)

    def test_overlay_runs_after_both_tiers(self):
        """It must apply even when tier 1 answered — that is the Western Sahara
        case, where the primary returns MAR and no fallback fires."""
        overlay = SRC.index("apply_overlay(ccodes,")
        fallback = SRC.index("docs_from_fallback += 1")
        self.assertLess(fallback, overlay)

    def test_overlay_usage_is_counted(self):
        self.assertIn('"docs_overlay_applied": docs_overlay_applied', SRC)


class NoOrphanModules(unittest.TestCase):
    """A module nothing imports is a design that is not running."""

    def test_ccode_tiers_is_imported_by_the_pipeline(self):
        import subprocess
        out = subprocess.run(
            ["grep", "-rln", "ccode_tiers", "--include=*.py", "processing/"],
            capture_output=True, text=True).stdout.split()
        importers = [f for f in out if not f.endswith("ccode_tiers.py")]
        self.assertTrue(importers,
                        "ccode_tiers is imported by nothing — it is not wired in")


if __name__ == "__main__":
    unittest.main()
