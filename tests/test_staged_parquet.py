"""Unit tests for processing/staged_parquet.py.

Covers the shared parquet-conversion helpers that ``h3_merge`` and
``boundary_merge`` both rely on. The contract:

* ``normalize_for_parquet`` flips empty nested-list fields to ``None`` so
  ``pyarrow.json.read_json``'s schema inference doesn't oscillate between
  ``list<null>`` and ``list<struct>``.
* ``strip_hull_for_parquet`` drops ``geometries[].hull`` (whose nested
  ``coordinates`` legitimately varies between Polygon and MultiPolygon
  shapes across our authority sources, and pyarrow rejects the variance).
* ``write_parquet_from_jsonl`` round-trips a JSONL through hull-strip and
  pyarrow without mutating the canonical JSONL on disk and without
  leaving behind temp files.

This is the regression target for two production failures: the
OHM h3_merge crash on
``Column(/geometries/[]/hull/coordinates/[]/[]) changed from array to
number in row 2`` and the OHM boundary_merge crash on
``Column(/geometries/[]/hull/coordinates/[]) changed from number to
array in row 216``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq

from processing.staged_parquet import (
    normalize_for_parquet,
    strip_hull_for_parquet,
    write_parquet_from_jsonl,
)


class TestNormalizeForParquet(unittest.TestCase):
    def test_empty_lists_become_none(self):
        doc = {"place_id": "x:1", "geometries": [], "toponyms": [],
               "types": [], "relations": []}
        out = normalize_for_parquet(doc)
        for k in ("geometries", "toponyms", "types", "relations"):
            self.assertIsNone(out[k])

    def test_non_empty_lists_pass_through(self):
        doc = {"place_id": "x:1", "toponyms": [{"toponym_id": "Foo@en"}]}
        self.assertEqual(
            normalize_for_parquet(doc)["toponyms"], [{"toponym_id": "Foo@en"}]
        )

    def test_keeps_hull(self):
        # Caller is expected to apply normalize before writing the
        # canonical JSONL — hull MUST survive.
        doc = {"place_id": "x:1",
               "geometries": [{"geometry_index": 0, "hull": {"type": "Point"}}]}
        self.assertIn("hull", normalize_for_parquet(doc)["geometries"][0])


class TestStripHullForParquet(unittest.TestCase):
    def test_drops_hull_from_each_geometry(self):
        doc = {
            "place_id": "ohm:r1",
            "geometries": [
                {"geometry_index": 0, "geom": {"type": "Polygon"},
                 "hull": {"type": "Polygon"}, "h3_centroid": "8a283082a667fff"},
                {"geometry_index": 1, "geom": {"type": "Point", "coordinates": [0, 0]},
                 "hull": {"type": "MultiPolygon"}},
            ],
        }
        stripped = strip_hull_for_parquet(doc)
        for geom in stripped["geometries"]:
            self.assertNotIn("hull", geom)
        self.assertIn("geom", stripped["geometries"][0])
        self.assertIn("h3_centroid", stripped["geometries"][0])
        self.assertEqual(stripped["place_id"], "ohm:r1")

    def test_handles_geometries_without_hull(self):
        doc = {"place_id": "po:p0",
               "geometries": [{"geometry_index": 0, "geom": {"type": "Point"}}]}
        self.assertEqual(strip_hull_for_parquet(doc), doc)

    def test_handles_no_geometries(self):
        doc = {"place_id": "x:1", "title": "no-geom"}
        self.assertEqual(strip_hull_for_parquet(doc), doc)

    def test_does_not_mutate_input(self):
        doc = {"place_id": "x:1",
               "geometries": [{"geometry_index": 0, "hull": {"type": "Polygon"}}]}
        before = json.loads(json.dumps(doc))
        strip_hull_for_parquet(doc)
        self.assertEqual(doc, before)


class TestWriteParquetFromJsonl(unittest.TestCase):
    """Regression: the production crash was ``ArrowInvalid: Column(
    /geometries/[]/hull/coordinates/[]) changed from number to array``.
    Reproduce that exact schema variance in two adjacent rows and confirm
    ``write_parquet_from_jsonl`` succeeds where ``paj.read_json`` direct
    on the JSONL would fail.
    """

    def _polygon_hull(self):
        return {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}

    def _multipolygon_hull(self):
        return {"type": "MultiPolygon",
                "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]}

    def _row(self, place_id: str, hull: dict | None) -> str:
        geometry = {"geometry_index": 0,
                    "geom": {"type": "Point", "coordinates": [0, 0]}}
        if hull is not None:
            geometry["hull"] = hull
        doc = {"place_id": place_id, "geometries": [geometry]}
        return json.dumps(doc, ensure_ascii=True) + "\n"

    def test_handles_mixed_hull_nesting(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "places.jsonl"
            parquet = Path(tmp) / "places.parquet"
            with jsonl.open("w", encoding="utf-8") as fh:
                fh.write(self._row("ohm:r1", self._polygon_hull()))
                fh.write(self._row("ohm:r2", self._multipolygon_hull()))
                fh.write(self._row("ohm:r3", None))
            write_parquet_from_jsonl(jsonl, parquet)
            self.assertTrue(parquet.exists())

            # Parquet is hull-less (the whole point).
            table = pq.ParquetFile(parquet).read()
            geoms_column = table.column("geometries").to_pylist()
            for row in geoms_column:
                for g in row or []:
                    self.assertNotIn("hull", g)

    def test_canonical_jsonl_keeps_hull(self):
        """The hull-strip happens in a temp file, never on the canonical jsonl."""
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "places.jsonl"
            parquet = Path(tmp) / "places.parquet"
            with jsonl.open("w", encoding="utf-8") as fh:
                fh.write(self._row("ohm:r1", self._polygon_hull()))
                fh.write(self._row("ohm:r2", self._multipolygon_hull()))
            write_parquet_from_jsonl(jsonl, parquet)

            with jsonl.open("r", encoding="utf-8") as fh:
                docs = [json.loads(line) for line in fh if line.strip()]
            for doc in docs:
                self.assertIn("hull", doc["geometries"][0])

    def test_temp_input_file_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "places.jsonl"
            parquet = Path(tmp) / "places.parquet"
            with jsonl.open("w", encoding="utf-8") as fh:
                fh.write(self._row("ohm:r1", self._polygon_hull()))
            write_parquet_from_jsonl(jsonl, parquet)
            # No leftover *.parquet_input.jsonl in the directory.
            leftover = list(Path(tmp).glob("*parquet_input*"))
            self.assertEqual(leftover, [])

    def test_temp_cleaned_up_even_on_pyarrow_failure(self):
        """If parquet conversion fails, the temp file must still be removed."""
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "places.jsonl"
            parquet = Path(tmp) / "places.parquet"
            # Truly malformed JSON line — pyarrow read_json will raise.
            jsonl.write_text('{"place_id": "x:1", "broken":\n', encoding="utf-8")
            with self.assertRaises(Exception):
                write_parquet_from_jsonl(jsonl, parquet)
            leftover = list(Path(tmp).glob("*parquet_input*"))
            self.assertEqual(leftover, [])


if __name__ == "__main__":
    unittest.main()
