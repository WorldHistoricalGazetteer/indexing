"""Unit tests for processing/boundary_shard_planner.py.

Focuses on the deterministic, PBF-independent parts:

* ``plan_shards`` Longest-Processing-Time bin-packing — the largest single
  relation always lands on the lightest-loaded shard, max load is
  minimised, and every relation appears exactly once.
* ``_is_boundary_candidate`` accepts the same boundary types the worker
  would assemble (admin levels 0..11, continent, country_border, curated
  misc) and rejects everything else.
* ``write_shard_map`` round-trips through json with the expected schema.
* ``_load_shard_relation_ids`` returns the correct subset and is the same
  set the worker uses for its leakage filter (regression test for the
  cross-shard duplication observed during the first OHM benchmark, where
  ``osmium getid -r`` followed relation→relation member refs and pulled
  in ~4× extra relations per shard).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from processing.boundary_shard_planner import (
    _is_boundary_candidate,
    plan_shards,
    write_shard_map,
)
from processing.boundary_stage import _load_shard_relation_ids


class TestPlanShards(unittest.TestCase):
    def test_lpt_balances_load(self):
        items = [(i, cost) for i, cost in enumerate([10, 9, 8, 7, 6, 5, 4, 3, 2, 1])]
        shards = plan_shards(items, 3)

        all_assigned = sorted(
            rid for shard in shards for rid in shard["relation_ids"]
        )
        self.assertEqual(all_assigned, sorted(i for i, _ in items))

        loads = [shard["estimated_cost"] for shard in shards]
        # LPT on this input gives loads [19, 18, 18] (max 19 ≤ ceil(55/3)=19).
        self.assertLessEqual(max(loads), 20)
        self.assertGreaterEqual(min(loads), 17)

    def test_largest_lands_alone_when_dominant(self):
        items = [(0, 10000), (1, 5), (2, 4), (3, 3), (4, 2)]
        shards = plan_shards(items, 4)
        # The 10000-cost relation must land on its own shard;
        # the heuristic puts the four small ones on the remaining shards.
        big_shard = next(s for s in shards if 0 in s["relation_ids"])
        self.assertEqual(big_shard["relation_ids"], [0])
        self.assertEqual(big_shard["estimated_cost"], 10000)

    def test_more_shards_than_items_yields_empty_shards(self):
        shards = plan_shards([(0, 5), (1, 3)], 5)
        self.assertEqual(len(shards), 5)
        non_empty = [s for s in shards if s["relation_ids"]]
        self.assertEqual(len(non_empty), 2)

    def test_invalid_shard_count_raises(self):
        with self.assertRaises(ValueError):
            plan_shards([(0, 1)], 0)

    def test_each_relation_assigned_exactly_once(self):
        items = [(i, (i * 31) % 17 + 1) for i in range(200)]
        shards = plan_shards(items, 8)
        flat = [rid for shard in shards for rid in shard["relation_ids"]]
        self.assertEqual(sorted(flat), sorted(i for i, _ in items))


class TestIsBoundaryCandidate(unittest.TestCase):
    def test_admin_level_in_range(self):
        for lvl in (0, 4, 8, 11):
            self.assertTrue(_is_boundary_candidate(
                {"boundary": "administrative", "admin_level": str(lvl)}
            ))

    def test_admin_level_out_of_range_rejected(self):
        for lvl in (12, 13, 99, -1):
            self.assertFalse(_is_boundary_candidate(
                {"boundary": "administrative", "admin_level": str(lvl)}
            ))

    def test_admin_level_missing_rejected(self):
        self.assertFalse(_is_boundary_candidate({"boundary": "administrative"}))

    def test_continent_and_country_border_accepted(self):
        self.assertTrue(_is_boundary_candidate({"boundary": "continent"}))
        self.assertTrue(_is_boundary_candidate({"boundary": "country_border"}))

    def test_misc_boundary_accepted(self):
        self.assertTrue(_is_boundary_candidate({"boundary": "civil_parish"}))
        self.assertTrue(_is_boundary_candidate({"boundary": "historic_county"}))

    def test_unknown_boundary_rejected(self):
        self.assertFalse(_is_boundary_candidate({"boundary": "lifeguard_zone"}))
        self.assertFalse(_is_boundary_candidate({"boundary": ""}))

    def test_no_boundary_tag_rejected(self):
        self.assertFalse(_is_boundary_candidate({"name": "Foo"}))


class TestWriteShardMap(unittest.TestCase):
    def test_round_trip(self):
        shards = plan_shards([(1, 5), (2, 3), (3, 2)], 2)
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "shard_map.json"
            write_shard_map(out_path, namespace="ohm", shards=shards, total_relations=3)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["namespace"], "ohm")
        self.assertEqual(payload["shard_count"], 2)
        self.assertEqual(payload["total_relations"], 3)
        self.assertEqual(len(payload["shards"]), 2)
        flat = sorted(rid for s in payload["shards"] for rid in s["relation_ids"])
        self.assertEqual(flat, [1, 2, 3])


class TestLoadShardRelationIds(unittest.TestCase):
    """Regression: the worker's leakage filter relies on this returning the
    EXACT set of relation IDs the planner assigned to a given shard.

    A mismatch (off-by-one shard_id, str vs int IDs, mis-keyed lookup) would
    silently re-introduce the cross-shard duplication that motivated the
    filter in the first place.
    """

    def _make_map(self, tmp: str) -> Path:
        shards = plan_shards(
            [(100, 5), (200, 4), (300, 3), (400, 2), (500, 1)], 3,
        )
        out_path = Path(tmp) / "shard_map.json"
        write_shard_map(
            out_path, namespace="ohm", shards=shards, total_relations=5,
        )
        return out_path

    def test_returns_set_of_ints(self):
        with tempfile.TemporaryDirectory() as tmp:
            map_path = self._make_map(tmp)
            assigned = {rid for s in json.loads(map_path.read_text())["shards"]
                        if s["shard_id"] == 0 for rid in s["relation_ids"]}
            ids = _load_shard_relation_ids(map_path, 0)
        self.assertEqual(ids, assigned)
        self.assertTrue(all(isinstance(x, int) for x in ids))

    def test_unknown_shard_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            map_path = self._make_map(tmp)
            with self.assertRaises(KeyError):
                _load_shard_relation_ids(map_path, 99)

    def test_disjoint_across_shards(self):
        """Every relation appears in exactly one shard's id set — guarantees
        the worker filter cannot accidentally accept the same relation in
        more than one shard."""
        with tempfile.TemporaryDirectory() as tmp:
            map_path = self._make_map(tmp)
            payload = json.loads(map_path.read_text())
            shard_ids = [s["shard_id"] for s in payload["shards"]]
            id_sets = [_load_shard_relation_ids(map_path, sid) for sid in shard_ids]
        union = set().union(*id_sets)
        # Sum of sizes equals union size iff disjoint.
        self.assertEqual(sum(len(s) for s in id_sets), len(union))


if __name__ == "__main__":
    unittest.main()
