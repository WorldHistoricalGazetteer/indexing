"""Unit tests for processing/update_merge.py (Batch 4c Phase 3)."""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from processing import update_merge

try:                            # package-qualified run (tests/__init__.py ran)
    from ._sandbox import assert_sandboxed
except ImportError:             # `discover -s tests` puts tests/ on sys.path
    from _sandbox import assert_sandboxed


def _square_geom():
    return {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }


def _write_parquet(rows, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), str(out_path))


def _write_jsonl(rows, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestUpdateMergeGeoNamesShape(unittest.TestCase):
    """Toponyms + relations + title patch (gn-toponyms shape)."""

    @classmethod
    def setUpClass(cls):
        # This class rmtree's and rewrites a real namespace directory under
        # STAGED_BASE_DIR, so refuse to run unless the sandbox took effect:
        # `discover -s tests` skips tests/__init__.py, and without this check
        # the test silently replaces the live gn / wd staged snapshots.
        assert_sandboxed()
        from processing.settings import STAGED_BASE_DIR
        cls.staged_base = Path(STAGED_BASE_DIR)
        cls.namespace = "gn"
        cls.run_id = "test-update-merge-gn"

        ns_dir = cls.staged_base / cls.namespace
        if ns_dir.exists():
            shutil.rmtree(ns_dir)

        # Two place docs with one existing toponym each, plus a no-patch row.
        extract_rows = [
            {"place_id": "gn:1", "namespace": "gn", "title": "Old Title",
             "dataset_status": "published", "dataset_id": "gn",
             "toponyms": [{"toponym_id": "Original@en"}],
             "relations": None, "geometries": None, "ccodes": None,
             "types": None, "links": None},
            {"place_id": "gn:2", "namespace": "gn", "title": "Two",
             "dataset_status": "published", "dataset_id": "gn",
             "toponyms": [{"toponym_id": "Two@en"}],
             "relations": [{"relation_type": "sameAs",
                            "related_place_id": "wd:Q1"}],
             "geometries": None, "ccodes": None,
             "types": None, "links": None},
            {"place_id": "gn:3", "namespace": "gn", "title": "Untouched",
             "dataset_status": "published", "dataset_id": "gn",
             "toponyms": [{"toponym_id": "Untouched@en"}],
             "relations": None, "geometries": None, "ccodes": None,
             "types": None, "links": None},
        ]
        _write_parquet(extract_rows, ns_dir / "extract" / "places.parquet")

        # Patch rows:
        # - gn:1: add a toponym + change title.
        # - gn:2: add the same relation (no-op) + a different relation.
        # - gn:3: no patch.
        # - gn:99: unmatched patch (should be reported as unmatched).
        # - Plus a malformed row (no place_id) to verify it's silently dropped.
        patch_rows = [
            {"place_id": "gn:1", "title": "New Title",
             "toponyms_to_add": [
                 {"toponym_id": "London@en", "timespans": [
                     {"start": {"in": 1900}, "end": {"in": 2000}}]},
                 {"toponym_id": "Original@en"},  # already present → dropped
             ]},
            {"place_id": "gn:2",
             "relations_to_add": [
                 {"relation_type": "sameAs", "related_place_id": "wd:Q1"},  # dup
                 {"relation_type": "closeMatch", "related_place_id": "wd:Q2"},
             ]},
            {"place_id": "gn:99",
             "toponyms_to_add": [{"toponym_id": "Phantom@en"}]},
            {"toponyms_to_add": [{"toponym_id": "NoID@en"}]},  # malformed
        ]
        _write_jsonl(patch_rows, ns_dir / "update_patch" / "places.update.jsonl")

    def test_merge_produces_expected_outputs(self):
        metrics = update_merge.run_update_merge(
            run_id=self.run_id, namespace=self.namespace,
        )
        self.assertEqual(metrics["docs_seen"], 3)
        self.assertEqual(metrics["docs_changed"], 2)  # gn:1 + gn:2
        self.assertEqual(metrics["docs_written"], 3)
        # gn:99 is unmatched (gn:99 not in extract); the malformed no-place_id
        # row is silently dropped at parse time and never enters the patch dict.
        self.assertEqual(metrics["patches_unmatched"], 1)

        merged = pq.ParquetFile(
            self.staged_base / self.namespace / "update_merged" / "places.parquet"
        ).read().to_pylist()
        by_id = {r["place_id"]: r for r in merged}

        # gn:1: title overwritten + new toponym appended; original kept.
        self.assertEqual(by_id["gn:1"]["title"], "New Title")
        gn1_top = [t["toponym_id"] for t in by_id["gn:1"]["toponyms"]]
        self.assertIn("Original@en", gn1_top)
        self.assertIn("London@en", gn1_top)
        self.assertEqual(len(gn1_top), 2)

        # gn:2: relation deduplicated by (relation_type, related_place_id);
        # the closeMatch is added.
        rels = by_id["gn:2"]["relations"]
        rel_keys = {(r["relation_type"], r["related_place_id"]) for r in rels}
        self.assertEqual(rel_keys, {("sameAs", "wd:Q1"), ("closeMatch", "wd:Q2")})

        # gn:3: untouched (title preserved, toponyms preserved).
        self.assertEqual(by_id["gn:3"]["title"], "Untouched")
        self.assertEqual(
            [t["toponym_id"] for t in by_id["gn:3"]["toponyms"]],
            ["Untouched@en"],
        )

    def test_idempotent_rerun(self):
        # Re-running over the same input + patch produces the same output.
        first = update_merge.run_update_merge(
            run_id=self.run_id + "-rerun", namespace=self.namespace,
        )
        second = update_merge.run_update_merge(
            run_id=self.run_id + "-rerun", namespace=self.namespace,
        )
        self.assertEqual(first["docs_seen"], second["docs_seen"])
        self.assertEqual(first["docs_changed"], second["docs_changed"])
        self.assertEqual(first["docs_written"], second["docs_written"])

        first_rows = pq.ParquetFile(
            self.staged_base / self.namespace / "update_merged" / "places.parquet"
        ).read().to_pylist()
        # Compare normalised JSON strings (Parquet round-trip preserves shape).
        first_json = sorted(json.dumps(r, sort_keys=True) for r in first_rows)
        # Rerun is deterministic.
        second_rows = pq.ParquetFile(
            self.staged_base / self.namespace / "update_merged" / "places.parquet"
        ).read().to_pylist()
        second_json = sorted(json.dumps(r, sort_keys=True) for r in second_rows)
        self.assertEqual(first_json, second_json)


class TestUpdateMergeWikidataShape(unittest.TestCase):
    """Geometry-replacement patch (wd-geoshapes shape)."""

    @classmethod
    def setUpClass(cls):
        # This class rmtree's and rewrites a real namespace directory under
        # STAGED_BASE_DIR, so refuse to run unless the sandbox took effect:
        # `discover -s tests` skips tests/__init__.py, and without this check
        # the test silently replaces the live gn / wd staged snapshots.
        assert_sandboxed()
        from processing.settings import STAGED_BASE_DIR
        cls.staged_base = Path(STAGED_BASE_DIR)
        cls.namespace = "wd"
        cls.run_id = "test-update-merge-wd"

        ns_dir = cls.staged_base / cls.namespace
        if ns_dir.exists():
            shutil.rmtree(ns_dir)

        # One Wikidata place with a stub Point geometry.
        extract_rows = [
            {"place_id": "wd:Q1", "namespace": "wd", "title": "Q1",
             "dataset_status": "published", "dataset_id": "wd",
             "toponyms": None, "relations": None, "ccodes": None,
             "types": None, "links": None,
             "geometries": [{
                 "geometry_index": 0,
                 "has_geom": False,
                 "geom_ref": None,
                 "repr_point": {"lon": 0.0, "lat": 0.0},
                 "hull": None,
                 "bounds": [0.0, 0.0, 0.0, 0.0],
                 "timespans": None,
             }]},
        ]
        _write_parquet(extract_rows, ns_dir / "extract" / "places.parquet")

        # Patch replaces the geometry array entirely + sets H3 fields.
        patch_rows = [{
            "place_id": "wd:Q1",
            "geometries_to_replace": [{
                "geometry_index": 0,
                "has_geom": True,
                "geom_ref": "wd:Q1_0",
                "repr_point": {"lon": 1.5, "lat": 2.5},
                "hull": _square_geom(),
                "bounds": [0.0, 0.0, 1.0, 1.0],
                "timespans": [{"start": {"in": 2025}}],
            }],
            "h3_centroid": "8a2a107b59cffff",
            "h3_cover": ["8a2a107b59cffff", "8a2a107b59c7fff"],
        }]
        _write_jsonl(patch_rows, ns_dir / "update_patch" / "places.update.jsonl")

    def test_geometry_replacement_overwrites_array_and_sets_h3(self):
        metrics = update_merge.run_update_merge(
            run_id=self.run_id, namespace=self.namespace,
        )
        self.assertEqual(metrics["docs_changed"], 1)

        merged = pq.ParquetFile(
            self.staged_base / self.namespace / "update_merged" / "places.parquet"
        ).read().to_pylist()
        doc = merged[0]
        self.assertEqual(doc["geometries"][0]["geom_ref"], "wd:Q1_0")
        self.assertEqual(doc["geometries"][0]["repr_point"], {"lon": 1.5, "lat": 2.5})
        self.assertEqual(doc["h3_centroid"], "8a2a107b59cffff")
        self.assertEqual(
            doc["h3_cover"], ["8a2a107b59cffff", "8a2a107b59c7fff"]
        )


class TestUpdateMergeNoPatch(unittest.TestCase):
    """Namespace with no patch file should fail loudly (FileNotFoundError on
    extract-missing) — but a present extract + missing patch is treated as
    a no-op merge that copies extract → update_merged."""

    @classmethod
    def setUpClass(cls):
        # This class rmtree's and rewrites a real namespace directory under
        # STAGED_BASE_DIR, so refuse to run unless the sandbox took effect:
        # `discover -s tests` skips tests/__init__.py, and without this check
        # the test silently replaces the live gn / wd staged snapshots.
        assert_sandboxed()
        from processing.settings import STAGED_BASE_DIR
        cls.staged_base = Path(STAGED_BASE_DIR)
        cls.namespace = "gn"
        cls.run_id = "test-update-merge-nopatch"

        ns_dir = cls.staged_base / cls.namespace
        if ns_dir.exists():
            shutil.rmtree(ns_dir)
        _write_parquet(
            [{"place_id": "gn:1", "namespace": "gn", "title": "Solo",
              "dataset_status": "published", "dataset_id": "gn",
              "toponyms": None, "relations": None, "geometries": None,
              "ccodes": None, "types": None, "links": None}],
            ns_dir / "extract" / "places.parquet",
        )

    def test_no_patch_is_passthrough(self):
        metrics = update_merge.run_update_merge(
            run_id=self.run_id, namespace=self.namespace,
        )
        self.assertEqual(metrics["docs_seen"], 1)
        self.assertEqual(metrics["docs_changed"], 0)
        self.assertEqual(metrics["docs_written"], 1)

    def test_unsupported_namespace_skipped(self):
        result = update_merge.run_update_merge(
            run_id=self.run_id, namespace="osm",  # not in UPDATE_PATCH_NAMESPACES
        )
        self.assertTrue(result.get("skipped"))


if __name__ == "__main__":
    unittest.main()
