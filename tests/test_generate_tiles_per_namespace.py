"""Unit tests for the per-namespace and per-WHG-dataset tile bucket logic in
``processing/generate_tiles.py``.

Covers:

* ``_doc_belongs_to_bucket`` matches on namespace for per-namespace buckets,
  and on the ``whg:<sub_id>:`` ``place_id`` prefix for per-WHG-dataset
  buckets — without requiring a ``boundary`` field.
* The fixed buckets (``osm_admin`` / ``ohm_admin`` / ``osm_misc``) keep their
  boundary gate.
* ``_has_renderable_geometry`` accepts both polygon (geom_ref / has_geom) and
  point (repr_point) entries.
* ``_build_staged_feature`` emits a Point feature when only ``repr_point`` is
  present and ``require_boundary=False``; returns ``None`` for the same doc
  when ``require_boundary=True``.
* ``resolve_buckets`` enumerates fixed + per-namespace + per-WHG-dataset
  buckets in the expected order.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from processing import generate_tiles


class _FakeReader:
    """Stand-in for ``GeomStoreReader`` in unit tests."""

    def __init__(self, store: dict[str, dict] | None = None):
        self._store = store or {}

    def get(self, key: str):
        return self._store.get(key)

    def close(self):
        pass


class TestHasRenderableGeometry(unittest.TestCase):
    def test_geom_ref(self):
        self.assertTrue(generate_tiles._has_renderable_geometry({"geom_ref": "k"}))

    def test_has_geom(self):
        self.assertTrue(
            generate_tiles._has_renderable_geometry({"has_geom": True, "geometry_index": 0})
        )

    def test_repr_point(self):
        self.assertTrue(
            generate_tiles._has_renderable_geometry({"repr_point": {"lon": 1, "lat": 2}})
        )

    def test_empty(self):
        self.assertFalse(generate_tiles._has_renderable_geometry({}))
        self.assertFalse(generate_tiles._has_renderable_geometry(None))
        self.assertFalse(generate_tiles._has_renderable_geometry({"repr_point": {"lon": 1}}))


class TestDocBelongsToBucket(unittest.TestCase):
    def test_per_namespace_point_only_matches(self):
        doc = {
            "place_id": "gn:12345",
            "geometries": [{"repr_point": {"lon": 0.1, "lat": 0.2}}],
        }
        matches, is_misc = generate_tiles._doc_belongs_to_bucket(doc, "gn", "gn")
        self.assertTrue(matches)
        self.assertFalse(is_misc)

    def test_per_namespace_no_geometry_skipped(self):
        doc = {"place_id": "gn:12345", "geometries": []}
        matches, _ = generate_tiles._doc_belongs_to_bucket(doc, "gn", "gn")
        self.assertFalse(matches)

    def test_per_namespace_wrong_namespace(self):
        doc = {
            "place_id": "wd:Q90",
            "geometries": [{"repr_point": {"lon": 0, "lat": 0}}],
        }
        matches, _ = generate_tiles._doc_belongs_to_bucket(doc, "gn", "wd")
        self.assertFalse(matches)

    def test_per_namespace_does_not_require_boundary(self):
        # No boundary field — still qualifies because it has geometry.
        doc = {
            "place_id": "pl:42",
            "geometries": [{"geom_ref": "pl:42_0"}],
        }
        matches, _ = generate_tiles._doc_belongs_to_bucket(doc, "pl", "pl")
        self.assertTrue(matches)

    def test_whg_dataset_prefix_match(self):
        doc = {
            "place_id": "whg:1234:abc",
            "geometries": [{"repr_point": {"lon": 0, "lat": 0}}],
        }
        matches, is_misc = generate_tiles._doc_belongs_to_bucket(doc, "whg-1234", "whg")
        self.assertTrue(matches)
        self.assertFalse(is_misc)

    def test_whg_dataset_prefix_mismatch(self):
        doc = {
            "place_id": "whg:9999:abc",
            "geometries": [{"repr_point": {"lon": 0, "lat": 0}}],
        }
        matches, _ = generate_tiles._doc_belongs_to_bucket(doc, "whg-1234", "whg")
        self.assertFalse(matches)

    def test_whg_dataset_wrong_namespace(self):
        doc = {
            "place_id": "gn:1234:abc",
            "geometries": [{"repr_point": {"lon": 0, "lat": 0}}],
        }
        matches, _ = generate_tiles._doc_belongs_to_bucket(doc, "whg-1234", "gn")
        self.assertFalse(matches)

    def test_fixed_admin_keeps_boundary_gate(self):
        # boundary missing → not eligible for fixed bucket even with geometry.
        doc = {
            "place_id": "osm:r123",
            "geometries": [{"geom_ref": "osm:r123_0"}],
        }
        matches, _ = generate_tiles._doc_belongs_to_bucket(doc, "osm_admin", "osm")
        self.assertFalse(matches)

    def test_fixed_admin_admin_level(self):
        doc = {
            "place_id": "osm:r123",
            "boundary": "4",
            "geometries": [{"geom_ref": "osm:r123_0"}],
        }
        matches, _ = generate_tiles._doc_belongs_to_bucket(doc, "osm_admin", "osm")
        self.assertTrue(matches)


class TestBuildStagedFeature(unittest.TestCase):
    def test_point_fallback_when_not_requiring_boundary(self):
        doc = {
            "place_id": "gn:12345",
            "title": "Foo",
            "geometries": [{"repr_point": {"lon": 1.5, "lat": 2.5}}],
            "toponyms": [{"toponym_id": "Foo@en"}],
        }
        feature = generate_tiles._build_staged_feature(
            doc, "gn", _FakeReader(), require_boundary=False
        )
        self.assertIsNotNone(feature)
        self.assertEqual(feature["geometry"], {"type": "Point", "coordinates": [1.5, 2.5]})
        self.assertEqual(feature["properties"]["place_id"], "gn:12345")
        self.assertEqual(feature["properties"]["namespace"], "gn")
        self.assertEqual(feature["properties"]["name"], "Foo")
        self.assertNotIn("boundary", feature["properties"])

    def test_point_doc_rejected_when_requiring_boundary(self):
        doc = {
            "place_id": "gn:12345",
            "geometries": [{"repr_point": {"lon": 1.5, "lat": 2.5}}],
        }
        feature = generate_tiles._build_staged_feature(
            doc, "gn", _FakeReader(), require_boundary=True
        )
        self.assertIsNone(feature)

    def test_polygon_from_store_preferred_over_point(self):
        polygon = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
        doc = {
            "place_id": "pl:42",
            "geometries": [
                {"geom_ref": "pl:42_0", "repr_point": {"lon": 9, "lat": 9}}
            ],
            "toponyms": [],
        }
        reader = _FakeReader({"pl:42_0": polygon})
        feature = generate_tiles._build_staged_feature(
            doc, "pl", reader, require_boundary=False
        )
        self.assertEqual(feature["geometry"], polygon)


class TestResolveBuckets(unittest.TestCase):
    def test_fixed_first_then_per_namespace_then_whg(self):
        with TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "_aggregates" / "whg.datasets.json"
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(
                json.dumps({
                    "namespace": "whg",
                    "datasets": [
                        {"id": "whg:1234", "name": "X", "record_count": 10},
                        {"id": "whg:5678", "name": "Y", "record_count": 20},
                    ],
                }),
                encoding="utf-8",
            )
            with mock.patch.object(generate_tiles, "STAGED_BASE_DIR", tmp):
                buckets = generate_tiles.resolve_buckets()
        # Fixed first
        self.assertEqual(buckets[:3], ["osm_admin", "ohm_admin", "osm_misc"])
        # Per-namespace next
        for ns in generate_tiles._PER_NAMESPACE_BUCKETS:
            self.assertIn(ns, buckets)
        # WHG last
        self.assertIn("whg-1234", buckets)
        self.assertIn("whg-5678", buckets)
        self.assertGreater(buckets.index("whg-1234"), buckets.index("gn"))

    def test_no_sidecar_yields_no_whg_buckets(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.object(generate_tiles, "STAGED_BASE_DIR", tmp):
                buckets = generate_tiles.resolve_buckets()
        for b in buckets:
            self.assertFalse(b.startswith("whg-"))


class TestBucketContributors(unittest.TestCase):
    def test_fixed(self):
        self.assertEqual(generate_tiles._bucket_contributors("osm_misc"), ("osm", "ohm"))

    def test_per_namespace(self):
        self.assertEqual(generate_tiles._bucket_contributors("gn"), ("gn",))

    def test_whg(self):
        self.assertEqual(generate_tiles._bucket_contributors("whg-1234"), ("whg",))

    def test_unknown(self):
        self.assertEqual(generate_tiles._bucket_contributors("nope"), ())


if __name__ == "__main__":
    unittest.main()
