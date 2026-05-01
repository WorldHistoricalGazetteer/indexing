"""Unit tests for processing/aat_enrich.py and processing/aat_data_lookup.py.

Covers:

* File-based loaders parse each vocab's nested shape into the canonical
  ``{native_key: [aat_ids]}`` dict.
* The hierarchy loader fails fast when ``aat_hierarchy.json`` is absent.
* ``vocab_for_type_entry`` and ``lookup_key_for_type_entry`` agree with
  the doc shapes the authority scripts emit (verified against the actual
  authority code in the design doc).
* ``augment_doc`` produces parallel ``aat_ids`` / ``aat_paths`` arrays,
  preserves doc identity when nothing changed, and gracefully handles
  ids without a hierarchy entry.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from processing.aat_data_lookup import (
    load_aat_hierarchy,
    load_all_aat_mappings,
    lookup_key_for_type_entry,
    vocab_for_type_entry,
)
from processing.aat_enrich import augment_doc


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestAATDataLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        # osm.json — keyed by tag key
        _write_json(self.data_dir / "osm.json", {
            "place": {"values": [
                {"value": "city", "aat_mapping": {"aat_id": 300008389}},
                {"value": "village", "aat_mapping": {"aat_id": 300008372}},
                {"value": "made-up", "aat_mapping": None},
            ]},
            "natural": {"values": [
                {"value": "peak", "aat_mapping": {"aat_id": 300008795}},
            ]},
        })
        # ohm.json — same shape as osm
        _write_json(self.data_dir / "ohm.json", {
            "place": {"values": [
                {"value": "village", "aat_mapping": {"aat_id": 300008372}},
            ]},
        })
        # geonames.json — keyed by feature class with class+code lookup keys
        _write_json(self.data_dir / "geonames.json", {
            "P": {"values": [
                {"value": "PPL", "aat_mapping": {"aat_id": 300008347}},
                {"value": "PPLC", "aat_mapping": {"aat_id": 300008389}},
            ]},
            "H": {"values": [
                {"value": "STM", "aat_mapping": {"aat_id": 300008707}},
            ]},
        })
        # wikidata.json — flat values list
        _write_json(self.data_dir / "wikidata.json", {
            "values": [
                {"value": "Q515", "aat_mapping": {"aat_id": 300008389}},
                {"value": "Q8502", "aat_mapping": {"aat_id": 300008795}},
                {"value": "Q-broken", "aat_mapping": {"aat_id": "not-an-int"}},
            ],
        })
        # pleiades.json — flat values list
        _write_json(self.data_dir / "pleiades.json", {
            "values": [
                {"value": "settlement", "aat_mapping": {"aat_id": 300008347}},
            ],
        })
        # aat_hierarchy.json — string keys per the file format
        _write_json(self.data_dir / "aat_hierarchy.json", {
            "300008389": {"path": "/300000000/300006300/300006520/300008389",
                          "label_en": "city"},
            "300008372": {"path": "/300000000/300006300/300006520/300008372"},
            "300008795": {"path": "/300008100/300008700/300008795",
                          "label_en": "mountain"},
            # Note: 300008347 is intentionally absent from hierarchy to test
            # the "id without path" branch.
        })

    def test_load_all_aat_mappings_keys_and_shapes(self):
        m = load_all_aat_mappings(self.data_dir)
        # osm/ohm: lookup key is tag_key=value
        self.assertEqual(m["osm"]["place=city"], [300008389])
        self.assertEqual(m["osm"]["natural=peak"], [300008795])
        self.assertEqual(m["ohm"]["place=village"], [300008372])
        # geonames: lookup key is fclass.code
        self.assertEqual(m["gn"]["P.PPL"], [300008347])
        self.assertEqual(m["gn"]["P.PPLC"], [300008389])
        self.assertEqual(m["gn"]["H.STM"], [300008707])
        # wd: lookup key is the Q-id
        self.assertEqual(m["wd"]["Q515"], [300008389])
        # pleiades: lookup key is the type identifier
        self.assertEqual(m["pleiades"]["settlement"], [300008347])
        # broken aat_id values are dropped (not raised)
        self.assertNotIn("Q-broken", m["wd"])
        # entries with aat_mapping=None are dropped silently
        self.assertNotIn("place=made-up", m["osm"])

    def test_load_all_aat_mappings_missing_file(self):
        # Deleting a vocab file should produce an empty dict for that vocab,
        # not raise — partial refreshes still produce useful augmentation.
        (self.data_dir / "ohm.json").unlink()
        m = load_all_aat_mappings(self.data_dir)
        self.assertEqual(m["ohm"], {})
        self.assertNotEqual(m["osm"], {})

    def test_load_aat_hierarchy(self):
        h = load_aat_hierarchy(self.data_dir)
        self.assertEqual(h[300008389]["path"],
                         "/300000000/300006300/300006520/300008389")
        self.assertEqual(h[300008389]["label_en"], "city")
        # Entry without label_en is preserved without the field
        self.assertEqual(h[300008372]["path"],
                         "/300000000/300006300/300006520/300008372")
        self.assertNotIn("label_en", h[300008372])

    def test_load_aat_hierarchy_missing_file_raises(self):
        (self.data_dir / "aat_hierarchy.json").unlink()
        with self.assertRaises(FileNotFoundError):
            load_aat_hierarchy(self.data_dir)


class TestVocabResolution(unittest.TestCase):
    def test_vocab_for_type_entry(self):
        self.assertEqual(vocab_for_type_entry("osm"), "osm")
        self.assertEqual(vocab_for_type_entry("ohm"), "ohm")
        self.assertEqual(vocab_for_type_entry("wikidata"), "wd")
        self.assertEqual(vocab_for_type_entry("pleiades"), "pleiades")
        self.assertEqual(vocab_for_type_entry("P"), "gn")
        self.assertEqual(vocab_for_type_entry("H"), "gn")
        self.assertIsNone(vocab_for_type_entry("aat"))   # un-geoscheme uses 'aat'
        self.assertIsNone(vocab_for_type_entry("chgis"))
        self.assertIsNone(vocab_for_type_entry(None))

    def test_lookup_key_uses_sourceLabel_for_tagged_vocabs(self):
        for vocab in ("osm", "ohm", "gn"):
            self.assertEqual(
                lookup_key_for_type_entry(vocab, {"sourceLabel": "X"}), "X"
            )
            self.assertIsNone(
                lookup_key_for_type_entry(vocab, {"identifier": "X"})
            )

    def test_lookup_key_uses_identifier_for_id_vocabs(self):
        for vocab in ("wd", "pleiades"):
            self.assertEqual(
                lookup_key_for_type_entry(vocab, {"identifier": "X"}), "X"
            )
            self.assertIsNone(
                lookup_key_for_type_entry(vocab, {"sourceLabel": "X"})
            )


class TestAugmentDoc(unittest.TestCase):
    def setUp(self):
        # Reuse the synthetic mappings from the loader test
        self.mappings = {
            "osm": {"place=city": [300008389]},
            "ohm": {},
            "gn": {"P.PPL": [300008347]},
            "wd": {"Q515": [300008389]},
            "pleiades": {"settlement": [300008347]},
        }
        self.hierarchy = {
            300008389: {"path": "/300000000/.../300008389", "label_en": "city"},
            # 300008347 intentionally absent
        }

    def test_osm_doc_gets_id_and_path(self):
        doc = {"place_id": "osm:n1",
               "types": [{"identifier": "city", "label": "osm",
                          "sourceLabel": "place=city"}]}
        new_doc, seen, aug = augment_doc(doc, self.mappings, self.hierarchy)
        self.assertEqual(seen, 1)
        self.assertEqual(aug, 1)
        t = new_doc["types"][0]
        self.assertEqual(t["aat_ids"], [300008389])
        self.assertEqual(t["aat_paths"], ["/300000000/.../300008389"])
        # Original doc unchanged (we shallow-copy on augment)
        self.assertNotIn("aat_ids", doc["types"][0])

    def test_id_without_path_only_emits_aat_ids(self):
        doc = {"place_id": "gn:1",
               "types": [{"identifier": "PPL", "label": "P",
                          "sourceLabel": "P.PPL"}]}
        new_doc, seen, aug = augment_doc(doc, self.mappings, self.hierarchy)
        self.assertEqual(aug, 1)
        t = new_doc["types"][0]
        self.assertEqual(t["aat_ids"], [300008347])
        # Hierarchy doesn't have 300008347 → no aat_paths key at all
        self.assertNotIn("aat_paths", t)

    def test_unmapped_native_type_passes_through(self):
        doc = {"place_id": "osm:n2",
               "types": [{"identifier": "nope", "label": "osm",
                          "sourceLabel": "place=nope"}]}
        new_doc, seen, aug = augment_doc(doc, self.mappings, self.hierarchy)
        self.assertEqual(aug, 0)
        self.assertEqual(new_doc, doc)   # identity preserved
        self.assertNotIn("aat_ids", new_doc["types"][0])

    def test_unknown_label_passes_through(self):
        doc = {"place_id": "chgis:1",
               "types": [{"identifier": "city", "label": "chgis",
                          "sourceLabel": "chgis"}]}
        new_doc, seen, aug = augment_doc(doc, self.mappings, self.hierarchy)
        self.assertEqual(aug, 0)
        self.assertEqual(new_doc, doc)

    def test_no_types_array_is_a_no_op(self):
        doc = {"place_id": "x:1"}
        new_doc, seen, aug = augment_doc(doc, self.mappings, self.hierarchy)
        self.assertEqual(seen, 0)
        self.assertEqual(aug, 0)
        self.assertIs(new_doc, doc)

    def test_mixed_types_partial_augment(self):
        # Two type entries, one mapped, one not. Doc must be copied with
        # both entries' relative order preserved and only the mapped one
        # carrying aat_ids.
        doc = {"place_id": "osm:n3", "types": [
            {"identifier": "fortress", "label": "osm",
             "sourceLabel": "historic=fortress"},          # not mapped
            {"identifier": "city", "label": "osm",
             "sourceLabel": "place=city"},                 # mapped
        ]}
        new_doc, seen, aug = augment_doc(doc, self.mappings, self.hierarchy)
        self.assertEqual(seen, 2)
        self.assertEqual(aug, 1)
        self.assertNotIn("aat_ids", new_doc["types"][0])
        self.assertEqual(new_doc["types"][1]["aat_ids"], [300008389])


if __name__ == "__main__":
    unittest.main()
