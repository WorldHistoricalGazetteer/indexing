"""Timespan reading in the /api/places response (place#164 encoding, place#169).

The response carries two different things and must not conflate them: the
*possible envelope* (start/end) and, where the record pins one, the *definite
core* it is attested alive throughout.
"""

from __future__ import annotations

import unittest

try:
    from gateway.places import _collapse_timespans, _normalise_timespans
except ModuleNotFoundError as exc:  # pragma: no cover - dev machines without the API deps
    # gateway.places pulls in fastapi/httpx for the routes; the timespan readers
    # under test are pure. Skip where the serving deps aren't installed (they are
    # on the gateway host, which is where this suite is expected to run).
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")


class TestNormaliseTimespans(unittest.TestCase):
    def test_legacy_exact_lifespan(self):
        out = _normalise_timespans([{"start": {"in": 1400}, "end": {"in": 1550}}])
        # An exact year is both the earliest and the latest that endpoint can be,
        # so it is simultaneously the envelope and the core.
        self.assertEqual(out, [{"start": 1400, "end": 1550, "definite": [1400, 1550]}])

    def test_attestation_has_a_core_but_no_envelope_bounds(self):
        # OSM's snapshot: attested alive at 2025, silent about before and after.
        out = _normalise_timespans([{"start": {"latest": 2025}, "end": {"earliest": 2025}}])
        self.assertEqual(out, [{"start": None, "end": None, "definite": [2025, 2025]}])

    def test_census_snapshot_carries_both(self):
        out = _normalise_timespans([{
            "start": {"earliest": 1841, "latest": 1851},
            "end": {"earliest": 1851, "latest": 1861},
        }])
        self.assertEqual(out, [{"start": 1841, "end": 1861, "definite": [1851, 1851]}])

    def test_open_start_has_no_core(self):
        out = _normalise_timespans([{"end": {"in": 1851}}])
        self.assertEqual(out, [{"start": None, "end": 1851}])
        self.assertNotIn("definite", out[0])

    def test_empty_and_junk_are_dropped(self):
        self.assertEqual(_normalise_timespans([]), [])
        self.assertEqual(_normalise_timespans([{}, "nonsense", {"start": {}}]), [])

    def test_non_integer_years_ignored(self):
        # Some legacy whg docs carry string years; they are not usable as bounds.
        self.assertEqual(_normalise_timespans([{"start": {"in": "2022"}}]), [])


class TestCollapseTimespans(unittest.TestCase):
    def test_envelope_across_all_three_parents(self):
        src = {
            "toponyms": [{"timespans": [{"start": {"in": 1500}, "end": {"in": 1700}}]}],
            "geometries": [{"timespans": [{"start": {"in": 1200}, "end": {"in": 1400}}]}],
            "relations": [{"timespans": [{"end": {"latest": 1900}}]}],
        }
        self.assertEqual(_collapse_timespans(src), [{"start": 1200, "end": 1900}])

    def test_attestation_only_record_is_not_undated(self):
        # Pre-place#169 this returned [] — the record read as undated because it
        # carries no `in` at all.
        src = {"toponyms": [{"timespans": [
            {"start": {"earliest": 1841, "latest": 1851}, "end": {"latest": 1861}},
        ]}]}
        self.assertEqual(_collapse_timespans(src), [{"start": 1841, "end": 1861}])

    def test_no_definite_summary(self):
        # Several cores are a set of intervals, not one: summarising them
        # min-to-max would assert continuous existence across the gaps.
        src = {"toponyms": [{"timespans": [
            {"start": {"in": 1200}, "end": {"in": 1250}},
            {"start": {"in": 1800}, "end": {"in": 1850}},
        ]}]}
        collapsed = _collapse_timespans(src)
        self.assertEqual(collapsed, [{"start": 1200, "end": 1850}])
        self.assertNotIn("definite", collapsed[0])

    def test_undated_source(self):
        self.assertEqual(_collapse_timespans({"toponyms": [{"names": []}]}), [])


if __name__ == "__main__":
    unittest.main()
