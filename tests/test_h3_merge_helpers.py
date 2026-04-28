"""Unit tests for processing/h3_merge.py helpers (findings 2 + 3 from the
OHM smoke run).

Covers:

* ``_strip_hull_for_parquet`` removes ``hull`` from every geometry while
  preserving every other geometry field and not mutating the input doc.
* ``_normalize_for_parquet`` keeps the rest of the doc intact and only
  flips empty nested-list fields to ``None``.
* ``_source_dir`` falls back to ``extract/`` for OSM/OHM when
  ``boundary_merged/`` is absent (mirrors h3_stage's behaviour, so a smoke
  run that skipped boundary completion can still exercise h3_merge).
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from processing.h3_merge import (
    _normalize_for_parquet,
    _source_dir,
    _strip_hull_for_parquet,
)


class TestStripHullForParquet(unittest.TestCase):
    def test_drops_hull_from_each_geometry(self):
        doc = {
            "place_id": "ohm:r1",
            "title": "Sample",
            "geometries": [
                {
                    "geometry_index": 0,
                    "geom": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
                    "hull": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
                    "h3_centroid": "8a283082a667fff",
                },
                {
                    "geometry_index": 1,
                    "geom": {"type": "Point", "coordinates": [0, 0]},
                    "hull": {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]},
                },
            ],
        }
        stripped = _strip_hull_for_parquet(doc)
        self.assertEqual(len(stripped["geometries"]), 2)
        for geom in stripped["geometries"]:
            self.assertNotIn("hull", geom)
        self.assertIn("geom", stripped["geometries"][0])
        self.assertIn("h3_centroid", stripped["geometries"][0])
        self.assertEqual(stripped["place_id"], "ohm:r1")

    def test_handles_geometries_without_hull(self):
        doc = {
            "place_id": "po:p0",
            "geometries": [{"geometry_index": 0, "geom": {"type": "Point", "coordinates": [0, 0]}}],
        }
        stripped = _strip_hull_for_parquet(doc)
        self.assertEqual(stripped["geometries"][0], {
            "geometry_index": 0,
            "geom": {"type": "Point", "coordinates": [0, 0]},
        })

    def test_handles_no_geometries(self):
        doc = {"place_id": "x:1", "title": "no-geom"}
        self.assertEqual(_strip_hull_for_parquet(doc), {"place_id": "x:1", "title": "no-geom"})

    def test_does_not_mutate_input(self):
        doc = {
            "place_id": "ohm:r1",
            "geometries": [{"geometry_index": 0, "hull": {"type": "Polygon"}}],
        }
        original = {
            "place_id": "ohm:r1",
            "geometries": [{"geometry_index": 0, "hull": {"type": "Polygon"}}],
        }
        _strip_hull_for_parquet(doc)
        self.assertEqual(doc, original)


class TestNormalizeForParquet(unittest.TestCase):
    def test_empty_lists_become_none(self):
        doc = {
            "place_id": "x:1",
            "geometries": [],
            "toponyms": [],
            "types": [],
            "relations": [],
        }
        out = _normalize_for_parquet(doc)
        for k in ("geometries", "toponyms", "types", "relations"):
            self.assertIsNone(out[k], f"{k} should be None when source is []")

    def test_non_empty_lists_pass_through(self):
        doc = {
            "place_id": "x:1",
            "toponyms": [{"toponym_id": "Foo@en"}],
        }
        out = _normalize_for_parquet(doc)
        self.assertEqual(out["toponyms"], [{"toponym_id": "Foo@en"}])

    def test_keeps_hull(self):
        doc = {
            "place_id": "x:1",
            "geometries": [{"geometry_index": 0, "hull": {"type": "Point"}}],
        }
        out = _normalize_for_parquet(doc)
        self.assertIn("hull", out["geometries"][0])


class TestSourceDirFallback(unittest.TestCase):
    def test_ohm_falls_through_when_boundary_merged_absent(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "ohm"
            (base / "extract").mkdir(parents=True)
            with mock.patch("processing.h3_merge.STAGED_BASE_DIR", str(Path(tmp))):
                resolved = _source_dir("ohm")
            self.assertEqual(resolved.name, "extract")
            self.assertEqual(resolved, base / "extract")

    def test_ohm_uses_boundary_merged_when_present(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "ohm"
            (base / "boundary_merged").mkdir(parents=True)
            with mock.patch("processing.h3_merge.STAGED_BASE_DIR", str(Path(tmp))):
                resolved = _source_dir("ohm")
            self.assertEqual(resolved, base / "boundary_merged")

    def test_non_boundary_namespace_uses_extract(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "po"
            (base / "extract").mkdir(parents=True)
            with mock.patch("processing.h3_merge.STAGED_BASE_DIR", str(Path(tmp))):
                resolved = _source_dir("po")
            self.assertEqual(resolved, base / "extract")


if __name__ == "__main__":
    unittest.main()
