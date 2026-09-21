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


class TestFclassesCaseFolding(unittest.TestCase):
    """place#267 — ``types.label`` is an analysed ``text`` field, so the
    standard analyser indexes the feature class ``P`` as the term ``p``.
    ``terms`` does not analyse its input, so the documented uppercase form
    matched nothing at all: 0 hits corpus-wide, with or without a spatial
    constraint, for as long as the parameter existed.

    These assert the case-folding, not the ES behaviour behind it — measured
    live on 21 Sep 2026: uppercase ``["P"]`` 0 hits, lowercase 5,220,641.
    """

    def _fclass_terms(self, fclasses):
        body = build_places_filter(["gn:1"], None, None, None, None,
                                   fclasses=fclasses)
        nested = [f for f in _filters(body) if "types.label" in json.dumps(f)]
        self.assertEqual(len(nested), 1, "expected exactly one fclass clause")
        return nested[0]["nested"]["query"]["terms"]["types.label"]

    def test_documented_uppercase_form_is_lowercased(self):
        # The pre-fix build emitted ["P"] here, and ES matched zero documents.
        self.assertEqual(self._fclass_terms(["P"]), ["p"])

    def test_every_geonames_feature_class_folds(self):
        classes = ["A", "H", "L", "P", "R", "S", "T", "U", "V"]
        self.assertEqual(self._fclass_terms(classes),
                         [c.lower() for c in classes])

    def test_case_is_irrelevant_to_the_emitted_query(self):
        # The real invariant: how the caller cased it cannot change the result.
        self.assertEqual(self._fclass_terms(["P", "s"]),
                         self._fclass_terms(["p", "S"]))

    def test_unknown_fclass_still_yields_a_constraining_clause(self):
        # Negative control: the fix must not turn the filter into a no-op.
        # A bogus class must still emit its (unmatchable) term rather than
        # being dropped — measured live: ["ZZ"] -> 0 hits, as it should.
        self.assertEqual(self._fclass_terms(["ZZ"]), ["zz"])

    def test_no_fclass_clause_when_unset(self):
        body = build_places_filter(["gn:1"], None, None, None, None)
        self.assertNotIn("types.label", json.dumps(_filters(body)))


if __name__ == "__main__":
    unittest.main()
