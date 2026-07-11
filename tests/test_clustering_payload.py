"""Tests for the per-hit clustering-fuel assembler
(``gateway/clustering_payload.py``) and the ``query_match`` capture in
``gateway.es_helpers.collect_place_ids``.
"""

from __future__ import annotations

import unittest

from gateway import clustering_payload as cp
from gateway.es_helpers import collect_place_ids


def _geom(centroid=None, cover=None, spans=None) -> dict:
    g = {}
    if centroid is not None:
        g["h3_centroid"] = centroid
    if cover is not None:
        g["h3_cover"] = cover
    if spans is not None:
        g["timespans"] = spans
    return g


class TestAssembleClusteringFields(unittest.TestCase):
    def test_empty_source(self):
        out = cp.assemble_clustering_fields({})
        self.assertEqual(out, {
            "h3": None, "h3_cover": [], "temporal_range": None,
            "aat_ids": [], "aat_paths": [],
        })

    def test_h3_centroid_is_first_available(self):
        src = {"geometries": [_geom(cover=["a"]), _geom(centroid="87xyz")]}
        self.assertEqual(cp.assemble_clustering_fields(src)["h3"], "87xyz")

    def test_h3_cover_union_sorted_deduped(self):
        src = {"geometries": [_geom(cover=["c", "a"]), _geom(cover=["b", "a"])]}
        self.assertEqual(cp.assemble_clustering_fields(src)["h3_cover"], ["a", "b", "c"])

    def test_h3_cover_bounded(self):
        cells = [f"cell{i:04d}" for i in range(cp._MAX_H3_COVER + 50)]
        src = {"geometries": [_geom(cover=cells)]}
        out = cp.assemble_clustering_fields(src)
        self.assertEqual(len(out["h3_cover"]), cp._MAX_H3_COVER)

    def test_temporal_range_spans_all_geometries(self):
        src = {"geometries": [
            _geom(spans=[{"start": {"in": 1500}, "end": {"in": 1700}}]),
            _geom(spans=[{"start": {"in": 1200}, "end": {"in": 1900}}]),
        ]}
        self.assertEqual(cp.assemble_clustering_fields(src)["temporal_range"], [1200, 1900])

    def test_temporal_range_none_when_undated(self):
        src = {"geometries": [_geom(cover=["a"])]}
        self.assertIsNone(cp.assemble_clustering_fields(src)["temporal_range"])

    def test_temporal_range_partial_bounds(self):
        # Only an end year present — range collapses to [end, end].
        src = {"geometries": [_geom(spans=[{"end": {"in": 1850}}])]}
        self.assertEqual(cp.assemble_clustering_fields(src)["temporal_range"], [1850, 1850])

    def test_aat_ids_and_paths_union_sorted(self):
        src = {"types": [
            {"aat_ids": [300132315], "aat_paths": ["300264550.300132315"]},
            {"aat_ids": [300008347, 300132315], "aat_paths": ["300264550.300008347"]},
        ]}
        out = cp.assemble_clustering_fields(src)
        self.assertEqual(out["aat_ids"], [300008347, 300132315])
        self.assertEqual(out["aat_paths"],
                         ["300264550.300008347", "300264550.300132315"])

    def test_malformed_subdocs_are_skipped(self):
        src = {"geometries": ["bad", None, _geom(centroid="87ok")],
               "types": ["bad", None, {"aat_ids": [1]}]}
        out = cp.assemble_clustering_fields(src)
        self.assertEqual(out["h3"], "87ok")
        self.assertEqual(out["aat_ids"], [1])


class TestQueryMatchCapture(unittest.TestCase):
    def _hit(self, name, score, pid):
        return {"_score": score, "_source": {"name": name, "attestations": [pid]}}

    def test_match_name_tracks_best_score(self):
        scores: dict = {}
        names: dict = {}
        collect_place_ids(
            [self._hit("Londinium", 5.0, "gn:1"),
             self._hit("London", 9.0, "gn:1"),
             self._hit("Lundun", 3.0, "gn:1")],
            scores, match_names=names)
        self.assertEqual(scores["gn:1"], 9.0)
        self.assertEqual(names["gn:1"], "London")  # the name of the best-scoring hit

    def test_match_names_optional(self):
        # No match_names dict passed → behaves exactly as before, no error.
        scores: dict = {}
        collect_place_ids([self._hit("X", 1.0, "gn:1")], scores)
        self.assertEqual(scores, {"gn:1": 1.0})


if __name__ == "__main__":
    unittest.main()
