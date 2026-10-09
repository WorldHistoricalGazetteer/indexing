"""The ofs extractor attests every name within the 1830-1849 register window (place#288).

The modern-name toponym used to carry a literal ``{in: 2000}..{in: 2025}``
lifespan, so the registry published ``temporal_extent [1830, 2025]`` for a
register that covers 1830-1849 — the same snapshot-year-as-coverage
conflation place#288 removed from kain_par, un, nl and iv, but here it is an
extractor defect rather than a registry one, so the fix is in the extractor
and the aggregate follows from a re-extract.

The modern name identifies the place the registers recorded; it is kept (it
is how a user searches for a place that existed in 1840) and dated like the
Ottoman and transliterated names, somewhere within the register window.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from processing.gazetteer_temporal_extent import doc_temporal_range
from processing.temporal import attested_window

# The module name is hyphenated (authorities/ottnfs-places.py) — load by path.
_spec = importlib.util.spec_from_file_location(
    "ottnfs_places",
    str(Path(__file__).resolve().parent.parent / "authorities" / "ottnfs-places.py"),
)
ofs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ofs)

# One normalised row, as _row_iter hands it to process_row (canonical keys).
ROW = {
    "place_id": "1234",
    "lon": "28.9784", "lat": "41.0082",
    "reg_date": 1256,                      # AH ≈ 1840 CE
    "ottoman": "قسطنطينيه",
    "translit": "Kostantiniyye",
    "modern": "İstanbul",
    "liva_1848": "İstanbul", "kaza_1848": "İstanbul", "nahiye": "", "divan": "",
}

WINDOW = attested_window(ofs.REGISTER_START, ofs.REGISTER_END)


class TestEveryNameIsAttestedWithinTheRegisterWindow(unittest.TestCase):
    def setUp(self):
        self.doc = ofs.process_row(dict(ROW))
        self.assertIsNotNone(self.doc)

    def test_the_modern_name_is_present_and_dated_like_the_others(self):
        by_lang = {t["toponym_id"].rpartition("@")[2]: t for t in self.doc["toponyms"]}
        # Presence first: an extractor that dropped the modern name would pass
        # a timespan-only check.
        self.assertEqual(set(by_lang), {"ota", "ota-Latn", "und"})
        self.assertEqual(by_lang["und"]["toponym_id"], "İstanbul@und")
        for lang, top in by_lang.items():
            with self.subTest(lang=lang):
                self.assertEqual(top["timespans"], WINDOW)

    def test_the_window_is_the_source_stated_one(self):
        self.assertEqual((ofs.REGISTER_START, ofs.REGISTER_END), (1830, 1849))
        self.assertEqual(WINDOW, [{"start": {"latest": 1849}, "end": {"earliest": 1830}}])

    def test_the_doc_extent_is_the_register_window(self):
        # What the registry aggregate pools from this doc: the published
        # temporal_extent for ofs is the min/max over these.
        self.assertEqual(doc_temporal_range(self.doc, "ofs"), (1830, 1849))

    def test_a_vanished_place_keeps_the_window_without_a_modern_name(self):
        doc = ofs.process_row({**ROW, "modern": "vanished"})
        langs = {t["toponym_id"].rpartition("@")[2] for t in doc["toponyms"]}
        self.assertEqual(langs, {"ota", "ota-Latn"})
        self.assertEqual(doc_temporal_range(doc, "ofs"), (1830, 1849))


if __name__ == "__main__":
    unittest.main()
