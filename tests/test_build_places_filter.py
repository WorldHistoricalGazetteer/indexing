"""Tests for the temporal-filter `undated` handling in
``gateway.es_helpers.build_places_filter``.
"""

from __future__ import annotations

import json
import unittest

from gateway.es_helpers import build_places_filter


def _filters(body: dict) -> list:
    return body["query"]["bool"]["filter"]


class TestUndatedTemporalFilter(unittest.TestCase):
    def test_no_temporal_filter_when_no_years(self):
        body = build_places_filter(["gn:1"], None, None, None, None)
        blob = json.dumps(_filters(body))
        self.assertNotIn("timespans", blob)

    def test_dated_only_is_a_plain_nested_match(self):
        body = build_places_filter(["gn:1"], None, None, 1500, 1600, undated=False)
        # Exactly one clause references timespans, and it is NOT a should-wrapper.
        temporal = [f for f in _filters(body) if "timespans" in json.dumps(f)]
        self.assertEqual(len(temporal), 1)
        self.assertIn("nested", temporal[0])
        self.assertNotIn("should", json.dumps(temporal[0])[:60])

    def test_undated_wraps_range_or_no_timespans(self):
        body = build_places_filter(["gn:1"], None, None, 1500, 1600, undated=True)
        temporal = [f for f in _filters(body) if "timespans" in json.dumps(f)][0]
        self.assertIn("bool", temporal)
        should = temporal["bool"]["should"]
        self.assertEqual(temporal["bool"]["minimum_should_match"], 1)
        self.assertEqual(len(should), 2)
        # One branch is the range match; the other is must_not exists (undated).
        blob = json.dumps(should)
        self.assertIn("must_not", blob)
        self.assertIn("exists", blob)
        self.assertIn("toponyms.timespans.start.in", blob)

    def test_undated_noop_without_temporal_range(self):
        # undated only matters when a date filter is active.
        body = build_places_filter(["gn:1"], None, None, None, None, undated=True)
        self.assertNotIn("timespans", json.dumps(_filters(body)))


if __name__ == "__main__":
    unittest.main()
