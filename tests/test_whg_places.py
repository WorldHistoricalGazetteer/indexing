"""Unit tests for authorities/whg-places.py LPF→staged-doc mapping."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load_whg_places():
    """``whg-places`` has a hyphen in its filename, so it isn't importable
    as a module via ``import``. Load it explicitly via importlib."""
    path = Path(__file__).resolve().parent.parent / "authorities" / "whg-places.py"
    spec = importlib.util.spec_from_file_location("whg_places", str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestLpfFeatureToStagedDoc(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.module = _load_whg_places()

    def test_basic_feature_round_trip(self):
        feature = {
            "type": "Feature",
            "id": 4242,
            "geometry": {"type": "Point", "coordinates": [2.5, 46.5]},
            "properties": {
                "title": "Lyon",
                "ccodes": ["FR"],
                "names": [
                    {"toponym": "Lyon"},
                    {"toponym": "Lugdunum",
                     "when": {"timespans": [{"start": {"in": -50}}]}},
                ],
                "types": [
                    {"identifier": "city", "label": "city",
                     "sourceLabel": "place=city"},
                ],
                "links": [{"type": "wikidata", "identifier": "wd:Q456"}],
                "related": [
                    {"relation_type": "sameAs", "relation_to": "wd:Q456",
                     "label": "Wikidata"},
                ],
                "whens": [{"timespans": [{"start": {"in": 1500},
                                          "end": {"in": 2000}}]}],
            },
        }
        doc = self.module.lpf_feature_to_staged_doc(
            feature, dataset_sub_id=42, dataset_status="published",
        )
        self.assertEqual(doc["place_id"], "whg:42:4242")
        self.assertEqual(doc["dataset_id"], "whg:42")
        self.assertEqual(doc["dataset_status"], "published")
        self.assertEqual(doc["namespace"], "whg")
        self.assertEqual(doc["title"], "Lyon")
        self.assertEqual(doc["ccodes"], ["FR"])

        toponym_ids = [t["toponym_id"] for t in doc["toponyms"]]
        self.assertIn("Lyon@und", toponym_ids)
        self.assertIn("Lugdunum@und", toponym_ids)

        rels = doc["relations"]
        self.assertEqual(rels[0]["relation_type"], "sameAs")
        self.assertEqual(rels[0]["related_place_id"], "wd:Q456")

        # Whens were lifted into the geometry's timespans.
        self.assertIn("geometries", doc)
        self.assertEqual(len(doc["geometries"]), 1)
        ts = doc["geometries"][0].get("timespans")
        self.assertIsNotNone(ts)
        self.assertEqual(ts[0]["start"]["in"], 1500)

    def test_feature_without_geometry_drops_geometries(self):
        feature = {
            "type": "Feature",
            "id": 1,
            "geometry": None,
            "properties": {"title": "T"},
        }
        doc = self.module.lpf_feature_to_staged_doc(
            feature, dataset_sub_id=7,
        )
        self.assertEqual(doc["place_id"], "whg:7:1")
        self.assertNotIn("geometries", doc)

    def test_geometry_collection_unwraps_to_first(self):
        feature = {
            "type": "Feature",
            "id": 2,
            "geometry": {
                "type": "GeometryCollection",
                "geometries": [
                    {"type": "Point", "coordinates": [0.0, 0.0]},
                    {"type": "Point", "coordinates": [1.0, 1.0]},
                ],
            },
            "properties": {"title": "Multi"},
        }
        doc = self.module.lpf_feature_to_staged_doc(feature, dataset_sub_id=7)
        self.assertEqual(doc["place_id"], "whg:7:2")
        self.assertIn("geometries", doc)
        self.assertEqual(doc["geometries"][0]["repr_point"], {"lon": 0.0, "lat": 0.0})

    def test_missing_id_returns_none(self):
        doc = self.module.lpf_feature_to_staged_doc(
            {"type": "Feature", "properties": {}}, dataset_sub_id=7,
        )
        self.assertIsNone(doc)

    def test_lang_lifted_from_citation(self):
        feature = {
            "type": "Feature", "id": 3,
            "geometry": None,
            "properties": {"names": [
                {"toponym": "Lyon", "citations": [{"lang": "fr"}]},
            ]},
        }
        doc = self.module.lpf_feature_to_staged_doc(feature, dataset_sub_id=42)
        self.assertEqual(doc["toponyms"][0]["toponym_id"], "Lyon@fr")

    def test_relation_with_missing_field_skipped(self):
        feature = {
            "type": "Feature", "id": 4,
            "geometry": None,
            "properties": {"related": [
                {"relation_type": "sameAs"},   # missing relation_to → drop
                {"relation_to": "wd:Q1"},      # missing relation_type → drop
                {"relation_type": "sameAs", "relation_to": "wd:Q1"},  # ok
            ]},
        }
        doc = self.module.lpf_feature_to_staged_doc(feature, dataset_sub_id=42)
        self.assertEqual(len(doc["relations"]), 1)


if __name__ == "__main__":
    unittest.main()
