"""Unit tests for the per-feature temporal + AAT tile properties (place#131).

Covers:

* ``gazetteer_temporal_extent.doc_temporal_range`` — the shared per-doc span
  helper (dated / ongoing / open-start / undated / outlier-clamp / namespace
  override).
* ``generate_tiles._temporal_props`` — sentinel/omit conventions matching the
  gateway's temporal-overlap + ``undated`` semantics.
* ``generate_tiles._aat_prop`` — ``;``-bracketed deduped union of AAT path
  segment ids.
* The tippecanoe ``--postfilter`` script dedupes clustered ``aat`` and leaves
  non-``aat`` features untouched.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from processing import generate_tiles as gt
from processing.gazetteer_temporal_extent import doc_temporal_range


class TestDocTemporalRange(unittest.TestCase):
    def test_fully_dated(self):
        doc = {"geometries": [{"timespans": [{"start": {"in": 1500},
                                              "end": {"in": 1800}}]}]}
        self.assertEqual(doc_temporal_range(doc, "iv"), (1500, 1800))

    def test_ongoing_has_start_no_end(self):
        doc = {"geometries": [{"timespans": [{"start": {"in": 1707}}]}]}
        self.assertEqual(doc_temporal_range(doc, "un"), (1707, None))

    def test_open_start_has_end_no_start(self):
        doc = {"toponyms": [{"timespans": [{"end": {"in": 1974}}]}]}
        self.assertEqual(doc_temporal_range(doc, "ukhc"), (None, 1974))

    def test_undated(self):
        doc = {"geometries": [{"timespans": []}], "toponyms": [{}]}
        self.assertEqual(doc_temporal_range(doc, "iv"), (None, None))

    def test_scans_all_three_timespan_locations(self):
        doc = {
            "geometries": [{"timespans": [{"start": {"in": 1600}}]}],
            "toponyms": [{"timespans": [{"end": {"in": 1900}}]}],
            "relations": [{"timespans": [{"start": {"in": 1550},
                                          "end": {"in": 1950}}]}],
        }
        # widest min-start / max-end across all locations
        self.assertEqual(doc_temporal_range(doc, "iv"), (1550, 1950))

    def test_outlier_year_clamped_to_undated(self):
        # OHM-style typo end_date=20222 is out of range -> rejected -> undated.
        doc = {"geometries": [{"timespans": [{"end": {"in": 20222}}]}]}
        self.assertEqual(doc_temporal_range(doc, "ohm"), (None, None))

    def test_namespace_clamp_override_preserves_deep_time(self):
        # PeriodO geological epochs legitimately predate the default -10000 min.
        doc = {"geometries": [{"timespans": [{"start": {"in": -4_000_000_000},
                                              "end": {"in": -541_000_000}}]}]}
        self.assertEqual(doc_temporal_range(doc, "po"),
                         (-4_000_000_000, -541_000_000))


class TestTemporalProps(unittest.TestCase):
    def test_dated_emits_both_bounds(self):
        doc = {"geometries": [{"timespans": [{"start": {"in": 1500},
                                              "end": {"in": 1800}}]}]}
        self.assertEqual(gt._temporal_props(doc, "iv"),
                         {"start": 1500, "end": 1800})

    def test_ongoing_fills_end_sentinel(self):
        doc = {"geometries": [{"timespans": [{"start": {"in": 1707}}]}]}
        self.assertEqual(gt._temporal_props(doc, "un"),
                         {"start": 1707, "end": gt.TILE_OPEN_END_YEAR})

    def test_open_start_fills_start_sentinel(self):
        doc = {"toponyms": [{"timespans": [{"end": {"in": 1974}}]}]}
        self.assertEqual(gt._temporal_props(doc, "ukhc"),
                         {"start": gt.TILE_OPEN_START_YEAR, "end": 1974})

    def test_undated_omits_both(self):
        doc = {"geometries": [{"timespans": []}]}
        self.assertEqual(gt._temporal_props(doc, "iv"), {})

    def test_sentinels_fall_outside_any_real_window(self):
        # A dated feature's bounds must never collide with the sentinels.
        self.assertLess(gt.TILE_OPEN_START_YEAR, -5000)
        self.assertGreater(gt.TILE_OPEN_END_YEAR, 2500)


class TestAatProp(unittest.TestCase):
    def test_bracketed_deduped_sorted_union_dot_delimited(self):
        # Real AAT hierarchy paths are dot-delimited.
        doc = {"types": [
            {"aat_paths": ["300000000.300264092.300008347",
                           "300000000.300387179"]},
            {"aat_paths": ["300000000.300008347"]},  # dup 300008347 + 300000000
        ]}
        self.assertEqual(
            gt._aat_prop(doc),
            ";300000000;300008347;300264092;300387179;",
        )

    def test_slash_delimited_also_supported(self):
        doc = {"types": [{"aat_paths": ["/300000000/300008347"]}]}
        self.assertEqual(gt._aat_prop(doc), ";300000000;300008347;")

    def test_none_when_no_types(self):
        self.assertIsNone(gt._aat_prop({"types": []}))

    def test_none_when_types_lack_aat_paths(self):
        self.assertIsNone(gt._aat_prop({"types": [{"identifier": "PPL"}]}))

    def test_every_id_is_bracket_delimited_for_substring_filter(self):
        # The client filter is ['in', ';<id>;', ['get','aat']]; verify each id
        # is individually addressable and no false substring boundary exists.
        doc = {"types": [{"aat_paths": ["/300008347/300008073"]}]}
        aat = gt._aat_prop(doc)
        self.assertIn(";300008347;", aat)
        self.assertIn(";300008073;", aat)
        # 3000083 (a prefix of 300008347) must NOT match as ';3000083;'
        self.assertNotIn(";3000083;", aat)


class TestAatPostfilterScript(unittest.TestCase):
    SCRIPT = Path(gt.__file__).with_name("tilegen_aat_postfilter.sh")

    def _run(self, *lines: str) -> list[dict]:
        proc = subprocess.run(
            [str(self.SCRIPT), "layer", "5", "10", "12"],
            input="\n".join(lines) + "\n",
            capture_output=True, text=True, check=True,
        )
        return [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]

    def test_script_exists_and_executable(self):
        self.assertTrue(self.SCRIPT.exists())

    def test_dedupes_and_sorts_cluster_aat(self):
        feat = ('{"type":"Feature","properties":'
                '{"aat":";300008347;300387179;300008347;300000810;"},'
                '"geometry":null}')
        out = self._run(feat)
        self.assertEqual(out[0]["properties"]["aat"],
                         ";300000810;300008347;300387179;")

    def test_passes_through_features_without_aat(self):
        feat = ('{"type":"Feature","properties":{"name":"x"},"geometry":null}')
        out = self._run(feat)
        self.assertEqual(out[0]["properties"], {"name": "x"})

    def test_collapses_empty_segments_from_concat(self):
        # concat of two ';'-bracketed strings yields a ';;' seam.
        feat = ('{"type":"Feature","properties":'
                '{"aat":";300387179;;300008347;300387179;"},"geometry":null}')
        out = self._run(feat)
        self.assertEqual(out[0]["properties"]["aat"],
                         ";300008347;300387179;")


if __name__ == "__main__":
    unittest.main()
