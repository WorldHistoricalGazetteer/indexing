"""Tests for processing.tilegen_bands.

Cover:
1. Band.matches: where=None matches everything; property lookup matches
   only listed values.
2. load_bands: reads metadata.whg:tilegen.buckets correctly; gracefully
   returns {} on missing file / missing block / bad JSON.
3. assign_band: returns first matching band, or None if no match.
4. The actual tileserver/whg-context.style.json shipped in the repo
   parses cleanly and yields the expected six buckets.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from processing.tilegen_bands import (
    Band, DEFAULT_STYLE_PATH, assign_band, load_bands,
)


class TestBandMatches(unittest.TestCase):
    def test_match_all_with_where_none(self):
        b = Band(name="all", minzoom=2, maxzoom=10, where=None)
        self.assertTrue(b.matches({"properties": {"boundary": "anything"}}))
        self.assertTrue(b.matches({"properties": {}}))
        self.assertTrue(b.matches({}))   # no properties at all → still matches

    def test_property_in_list(self):
        b = Band(name="continental", minzoom=0, maxzoom=4,
                 where={"property": "boundary", "in": ["0", "1"]})
        self.assertTrue(b.matches({"properties": {"boundary": "0"}}))
        self.assertTrue(b.matches({"properties": {"boundary": "1"}}))
        self.assertFalse(b.matches({"properties": {"boundary": "2"}}))
        self.assertFalse(b.matches({"properties": {"boundary": "anything"}}))
        self.assertFalse(b.matches({"properties": {}}))   # missing prop
        self.assertFalse(b.matches({}))                     # no properties

    def test_malformed_where_rejects_all(self):
        # Missing property
        self.assertFalse(Band("x", 2, 10, where={"in": ["a"]}).matches(
            {"properties": {"boundary": "a"}}))
        # Missing values
        self.assertFalse(Band("x", 2, 10, where={"property": "boundary"}).matches(
            {"properties": {"boundary": "a"}}))


class TestLoadBands(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.style = Path(self._tmp.name) / "style.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, payload):
        self.style.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_bands(self.style), {})

    def test_missing_metadata_returns_empty(self):
        self._write({"version": 8, "name": "X"})
        self.assertEqual(load_bands(self.style), {})

    def test_missing_tilegen_block_returns_empty(self):
        self._write({"version": 8, "metadata": {"whg:other": "stuff"}})
        self.assertEqual(load_bands(self.style), {})

    def test_bad_json_returns_empty(self):
        self.style.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_bands(self.style), {})

    def test_loads_bands_with_correct_shapes(self):
        self._write({
            "metadata": {
                "whg:tilegen": {
                    "buckets": {
                        "osm_admin": [
                            {"name": "continental", "minzoom": 0, "maxzoom": 4,
                             "where": {"property": "boundary", "in": ["0", "1"]}},
                            {"name": "country", "minzoom": 0, "maxzoom": 8,
                             "where": {"property": "boundary", "in": ["2"]}},
                        ],
                        "po": [
                            {"name": "all", "minzoom": 2, "maxzoom": 10, "where": None},
                        ],
                    }
                }
            }
        })
        bands = load_bands(self.style)
        self.assertEqual(set(bands), {"osm_admin", "po"})
        self.assertEqual(len(bands["osm_admin"]), 2)
        self.assertEqual(bands["osm_admin"][0].name, "continental")
        self.assertEqual(bands["osm_admin"][0].minzoom, 0)
        self.assertEqual(bands["osm_admin"][0].maxzoom, 4)
        self.assertEqual(bands["osm_admin"][0].where,
                         {"property": "boundary", "in": ["0", "1"]})
        self.assertIsNone(bands["po"][0].where)


class TestAssignBand(unittest.TestCase):
    def test_returns_first_matching_band(self):
        bands = [
            Band("continental", 0, 4, where={"property": "boundary", "in": ["1"]}),
            Band("country",     0, 8, where={"property": "boundary", "in": ["2"]}),
        ]
        self.assertEqual(
            assign_band({"properties": {"boundary": "1"}}, bands).name,
            "continental",
        )
        self.assertEqual(
            assign_band({"properties": {"boundary": "2"}}, bands).name,
            "country",
        )

    def test_returns_none_on_no_match(self):
        bands = [Band("x", 0, 4, where={"property": "boundary", "in": ["1"]})]
        self.assertIsNone(assign_band({"properties": {"boundary": "9"}}, bands))


class TestRepoStyleFile(unittest.TestCase):
    """The shipped style file must parse cleanly and have the expected
    six tilegen buckets — guards against accidental rebuild regressions."""

    def test_default_style_parses_and_has_expected_buckets(self):
        # Skip if the file isn't materialised yet (CI sandboxes might
        # not have it on disk).
        if not DEFAULT_STYLE_PATH.exists():
            self.skipTest(f"{DEFAULT_STYLE_PATH} not present")
        bands = load_bands(DEFAULT_STYLE_PATH)
        # Bucket names equal the authority namespace since 32a96fc
        # ("rename admin buckets to align with authority namespaces").
        # This assertion still named the pre-rename osm_admin/ohm_admin
        # until 2 Sep 2026 — it skips when the sibling ../tileboss clone
        # is absent (CI, CRC) and failed for anyone who had it, so it was
        # repeatedly filed as environmental noise rather than read.
        self.assertEqual(
            set(bands),
            {"osm", "ohm", "osm_misc", "po", "clio", "nl"},
        )
        # osm has 5 admin-level bands
        self.assertEqual(len(bands["osm"]), 5)
        names = [b.name for b in bands["osm"]]
        self.assertEqual(names,
                         ["continental", "country", "state", "district", "local"])
        # osm_misc has the 3 corrected bands (NOT water/forest/park)
        self.assertEqual(
            [b.name for b in bands["osm_misc"]],
            ["broad-regional", "historical", "parish-local"],
        )


if __name__ == "__main__":
    unittest.main()
