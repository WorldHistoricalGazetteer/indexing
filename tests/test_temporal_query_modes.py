"""Semantics of the two temporal query modes (place#164 encoding, place#169 consumers).

These tests do not merely assert the shape of the generated DSL: they extract the
per-timespan bool query and *evaluate* it against synthetic timespan documents, so
an inverted bound or a missing unbounded branch fails here rather than in prod.

    possibly   (start.earliest ?? -inf) <= Q <= (end.latest ?? +inf)
    definitely  start.latest <= Q <= end.earliest

``in`` stands in for whichever outer bound is asked for — the sources that still
use it (ohm, clio, hgis) mean an exact year.
"""

from __future__ import annotations

import json
import unittest

from gateway.es_helpers import build_places_filter

TS = "toponyms.timespans"


# --------------------------------------------------------------------------- #
# A minimal ES bool/range/exists evaluator, enough for the timespan sub-query.
# --------------------------------------------------------------------------- #
def _as_list(v):
    return v if isinstance(v, list) else [v]


def matches(query: dict, doc: dict) -> bool:
    """Evaluate a bool/range/exists query against a flat {field: value} doc."""
    if "bool" in query:
        b = query["bool"]
        if not all(matches(c, doc) for c in _as_list(b.get("must", []))):
            return False
        if any(matches(c, doc) for c in _as_list(b.get("must_not", []))):
            return False
        should = _as_list(b.get("should", []))
        if should:
            need = b.get("minimum_should_match", 1)
            if sum(1 for c in should if matches(c, doc)) < need:
                return False
        return True
    if "range" in query:
        (field, cond), = query["range"].items()
        v = doc.get(field)
        if v is None:
            return False
        if "lte" in cond and v > cond["lte"]:
            return False
        if "gte" in cond and v < cond["gte"]:
            return False
        return True
    if "exists" in query:
        return doc.get(query["exists"]["field"]) is not None
    raise AssertionError(f"evaluator does not handle {list(query)}")


def timespan_query(start_year, end_year, mode="possibly"):
    """The innermost per-timespan bool from a built places filter."""
    body = build_places_filter(
        ["gn:1"], None, None, start_year, end_year, temporal_mode=mode,
    )
    clause = [f for f in body["query"]["bool"]["filter"] if "timespans" in json.dumps(f)][0]
    return clause["nested"]["query"]["nested"]["query"]


def span(**kwargs) -> dict:
    """A timespan doc: span(start_latest=2025, end_earliest=2025)."""
    doc = {}
    for key, value in kwargs.items():
        side, qualifier = key.split("_", 1)
        doc[f"{TS}.{side}.{qualifier}"] = value
    return doc


# --------------------------------------------------------------------------- #
# Real encodings, from plan-temporal-model.md §3
# --------------------------------------------------------------------------- #
OSM_SNAPSHOT = span(start_latest=2025, end_earliest=2025)      # attested alive at 2025
LIFESPAN_1400_1450 = span(start_in=1400, end_in=1450)          # ohm/clio/hgis idiom
LIFESPAN_1400_1550 = span(start_in=1400, end_in=1550)
OPEN_START_TO_1851 = span(end_in=1851)                          # ukhc / kain_par
ONGOING_FROM_1707 = span(start_in=1707)                         # un boundaries
VOB_CENSUS_1851 = span(                                         # definite core at 1851,
    start_earliest=1841, start_latest=1851,                     # possible 1841-1861
    end_earliest=1851, end_latest=1861,
)


class TestPossiblyAlive(unittest.TestCase):
    """The default mode: could this place have been alive in the window?"""

    def test_snapshot_source_is_possibly_alive_in_the_past(self):
        # The whole point of place#164: OSM's 2025 stamp must stop excluding it
        # from historical windows, because it makes no claim about when the
        # feature began.
        self.assertTrue(matches(timespan_query(1500, 1600), OSM_SNAPSHOT))

    def test_lifespan_ending_before_the_window_is_excluded(self):
        self.assertFalse(matches(timespan_query(1500, 1600), LIFESPAN_1400_1450))

    def test_lifespan_overlapping_the_window_matches(self):
        self.assertTrue(matches(timespan_query(1500, 1600), LIFESPAN_1400_1550))

    def test_ongoing_feature_matches_a_later_window(self):
        self.assertTrue(matches(timespan_query(1900, 1950), ONGOING_FROM_1707))

    def test_ongoing_feature_excluded_before_it_began(self):
        self.assertFalse(matches(timespan_query(1500, 1600), ONGOING_FROM_1707))

    def test_open_start_survives_a_bounded_end_year(self):
        # place#138: a feature with an end but no start was dropped by any
        # bounded end_year. No start bound means it could have begun at any time.
        self.assertTrue(matches(timespan_query(1000, 1800), OPEN_START_TO_1851))

    def test_open_start_still_excluded_after_it_ended(self):
        self.assertFalse(matches(timespan_query(1900, 1950), OPEN_START_TO_1851))

    def test_census_snapshot_matches_between_neighbouring_censuses(self):
        self.assertTrue(matches(timespan_query(1845, 1848), VOB_CENSUS_1851))


class TestDefinitelyAlive(unittest.TestCase):
    """The strict mode: is the attested core inside the window?"""

    def test_snapshot_source_is_not_definitely_alive_in_the_past(self):
        self.assertFalse(matches(timespan_query(1500, 1600, "definitely"), OSM_SNAPSHOT))

    def test_snapshot_source_is_definitely_alive_at_its_own_year(self):
        self.assertTrue(matches(timespan_query(2020, 2026, "definitely"), OSM_SNAPSHOT))

    def test_lifespan_overlapping_the_window_matches(self):
        self.assertTrue(matches(timespan_query(1500, 1600, "definitely"), LIFESPAN_1400_1550))

    def test_open_start_has_no_definite_core(self):
        self.assertFalse(matches(timespan_query(1000, 1800, "definitely"), OPEN_START_TO_1851))

    def test_census_snapshot_only_at_the_census_year(self):
        q = timespan_query(1845, 1848, "definitely")
        self.assertFalse(matches(q, VOB_CENSUS_1851))
        self.assertTrue(matches(timespan_query(1850, 1852, "definitely"), VOB_CENSUS_1851))

    def test_definitely_is_stricter_than_possibly_everywhere(self):
        for doc in (OSM_SNAPSHOT, LIFESPAN_1400_1450, LIFESPAN_1400_1550,
                    OPEN_START_TO_1851, ONGOING_FROM_1707, VOB_CENSUS_1851):
            for window in ((1500, 1600), (1000, 1800), (1900, 1950), (2020, 2026)):
                if matches(timespan_query(*window, "definitely"), doc):
                    self.assertTrue(
                        matches(timespan_query(*window), doc),
                        f"definitely matched but possibly did not: {doc} {window}",
                    )


class TestModeHandling(unittest.TestCase):
    def test_default_is_possibly(self):
        self.assertEqual(
            json.dumps(timespan_query(1500, 1600)),
            json.dumps(timespan_query(1500, 1600, "possibly")),
        )

    def test_unknown_mode_falls_back_to_possibly(self):
        self.assertEqual(
            json.dumps(timespan_query(1500, 1600, "sometimes")),
            json.dumps(timespan_query(1500, 1600, "possibly")),
        )

    def test_one_sided_windows(self):
        self.assertTrue(matches(timespan_query(1500, None), LIFESPAN_1400_1550))
        self.assertFalse(matches(timespan_query(1600, None), LIFESPAN_1400_1550))
        self.assertTrue(matches(timespan_query(None, 1450), LIFESPAN_1400_1550))
        self.assertFalse(matches(timespan_query(None, 1300), LIFESPAN_1400_1550))


class TestUndatedProbe(unittest.TestCase):
    def test_undated_branch_names_all_six_subfields(self):
        # Probing `in` alone (the pre-place#169 behaviour) reads every
        # attestation-encoded record as undated, so "+undated" would admit dated
        # records the window excludes.
        body = build_places_filter(["gn:1"], None, None, 1500, 1600, undated=True)
        clause = [f for f in body["query"]["bool"]["filter"] if "timespans" in json.dumps(f)][0]
        blob = json.dumps(clause)
        for side in ("start", "end"):
            for qualifier in ("in", "earliest", "latest"):
                self.assertIn(f"{TS}.{side}.{qualifier}", blob)


if __name__ == "__main__":
    unittest.main()
