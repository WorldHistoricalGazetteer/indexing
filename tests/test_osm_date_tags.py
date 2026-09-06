"""OSM's start_date/end_date must reach the document — and mean the right thing.

226,468 ingested OSM features (1.10%; 132,841 of them `historic=*`) carry a
`start_date` the ingest discarded, so each asserted "attested 2026" while the
source stated a real start year. The discard happened in `process_tags`, which
never listed the two tags, so `create_doc` could not have used them however it
was written — the fix has two halves and a test of either alone passes while the
feature stays broken.

⚠ THE SEMANTIC TRAP. `ohm-places.py` uses `lifespan(start, end)` and is right to:
OpenHistoricalMap is a map OF THE PAST, so an undated end is genuinely unknown.
OSM is a map of the PRESENT — a feature tagged `start_date=1650` and still in the
2026 dump demonstrably exists now. Encoding its end as unknown would discard what
the dump itself asserts, and would make the feature fail a `definitely alive in
2026` query it should pass. Copying OHM's builder would have looked correct and
been wrong in exactly one branch.
"""
import unittest

from processing.temporal import attested_at, dated_or_attested, parse_osm_year


class ParseOsmYearTest(unittest.TestCase):
    def test_the_shapes_osm_actually_uses(self):
        for raw, want in (("1650", 1650), ("1650-01-01", 1650), ("~1900", 1900),
                          ("C19", 1800), ("-0500", -500), ("circa 1720", 1720),
                          ("1888-06-01T00:00:00Z", 1888)):
            self.assertEqual(parse_osm_year(raw), want, raw)

    def test_unparseable_is_none_not_zero(self):
        """None means 'no date'; 0 would mean 1 BCE and silently date the world."""
        for raw in ("", None, "rubbish", "unknown", "?"):
            self.assertIsNone(parse_osm_year(raw), repr(raw))


class DatedOrAttestedTest(unittest.TestCase):
    YEAR = 2026

    def test_start_only_keeps_the_snapshot_attestation(self):
        """THE BRANCH THAT DIFFERS FROM OHM. End must be bounded by the dump
        year, not left unknown."""
        ts = dated_or_attested(1650, None, self.YEAR)[0]
        self.assertEqual(ts["start"], {"in": 1650})
        self.assertEqual(ts["end"], {"earliest": self.YEAR},
                         "an OSM feature present in the dump has an attested "
                         "end bound; leaving it unknown discards that")

    def test_both_dates_are_a_genuine_lifespan(self):
        ts = dated_or_attested(1650, 1720, self.YEAR)[0]
        self.assertEqual(ts, {"start": {"in": 1650}, "end": {"in": 1720}})

    def test_end_only_gets_the_closure_rule(self):
        """Without `start.latest`, a feature tagged only `end_date` tests as
        definitely alive at NO year despite demonstrably existing before it."""
        ts = dated_or_attested(None, 1932, self.YEAR)[0]
        self.assertEqual(ts["end"], {"in": 1932})
        self.assertEqual(ts["start"], {"latest": 1932})

    def test_neither_is_unchanged_from_today(self):
        """The 98.9% of features with no dates must be byte-identical, or the
        patch's diff stops being attributable to the dated ones."""
        self.assertEqual(dated_or_attested(None, None, self.YEAR),
                         attested_at(self.YEAR))

    def test_reversed_dates_are_tolerated_not_emitted_as_nonsense(self):
        ts = dated_or_attested(1720, 1650, self.YEAR)[0]
        self.assertEqual(ts, {"start": {"in": 1650}, "end": {"in": 1720}})


class IngestSiteTest(unittest.TestCase):
    """Both halves of the fix. Either alone leaves the feature broken."""

    def _src(self):
        from pathlib import Path
        return (Path(__file__).resolve().parents[1]
                / "authorities" / "osm-places.py").read_text(encoding="utf-8")

    def test_process_tags_keeps_the_date_tags(self):
        src = self._src()
        block = src[src.index("def process_tags"):src.index("# ---------------- HANDLER")]
        self.assertIn("'start_date'", block,
                      "process_tags strips start_date, so create_doc cannot see it "
                      "however it is written")
        self.assertIn("'end_date'", block)

    def test_create_doc_derives_timespans_from_them(self):
        src = self._src()
        block = src[src.index("def create_doc"):src.index("def process_tags")]
        self.assertIn("_feature_timespans(tags)", block)
        self.assertNotIn("'timespans': _attestation_timespans()", block,
                         "create_doc still hard-codes the attestation year")

    def test_osm_does_not_use_ohms_lifespan_builder(self):
        src = self._src()
        self.assertIn("dated_or_attested", src)
        self.assertNotIn("build_timespans", src,
                         "osm must not adopt OHM's lifespan(start, end) — see "
                         "the module docstring for the branch that differs")


if __name__ == "__main__":
    unittest.main()
