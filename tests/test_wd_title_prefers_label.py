"""The wd extractor stores a name as ``title``, not the QID (place#290).

2,721,066 ``wd`` places were ingested with a bare ``Q…`` as their title
because the extractor fell back to the QID whenever the entity had no ``en``
(or ``mul``) label — Czech, Indonesian, Bengali, Javanese, Madurese labels
were all skipped. The gateway has a read-time repair (``gateway/titles.py``,
``qid_title``); this suite pins the ingest-side rule and its parity with
that repair, so the title a record shows does not change when the index is
re-ingested.

The rule lives once, in ``processing/titles.py``: first ``en``, else the
first ``en-*``, else ``mul``, else the first usable name in document order
(labels, then aliases); the QID only when the entity has no name at all.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import orjson

from gateway.titles import display_title

# The module name is hyphenated (authorities/wikidata-places.py) — load by path.
_spec = importlib.util.spec_from_file_location(
    "wikidata_places",
    str(Path(__file__).resolve().parent.parent / "authorities" / "wikidata-places.py"),
)
wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wd)


def _entity(qid: str, labels: dict[str, str], aliases: dict[str, list[str]] | None = None) -> dict:
    """A minimal geographic entity: coordinates, and labels keyed by language
    in the order given (the dump's own order is what 'first' means)."""
    return {
        "id": qid,
        "labels": {lang: {"language": lang, "value": v} for lang, v in labels.items()},
        "aliases": {lang: [{"language": lang, "value": v} for v in vs]
                    for lang, vs in (aliases or {}).items()},
        "claims": {"P625": [{"mainsnak": {"datavalue": {"value": {"longitude": 112.75, "latitude": -7.23}}}}]},
    }


def _doc(entity: dict) -> dict:
    doc, _ = wd.create_place_doc_fast(entity, orjson.dumps(entity))
    assert doc is not None, "the fixture must be staged, or nothing below is tested"
    return doc


def _names(doc: dict) -> list[dict]:
    """The toponyms as the gateway sees them: ``{label, lang}`` in stored order."""
    out = []
    for t in doc["toponyms"]:
        label, _, lang = t["toponym_id"].rpartition("@")
        out.append({"label": label, "lang": lang})
    return out


class TestTitleIsANameNotTheQid(unittest.TestCase):
    def test_the_measured_case_a_single_non_english_label(self):
        # wd:Q100002611 on the live index: title 'Q100002611', toponyms ['Pasar Krempyeng Sidotopo@id']
        doc = _doc(_entity("Q100002611", {"id": "Pasar Krempyeng Sidotopo"}))
        self.assertEqual(doc["title"], "Pasar Krempyeng Sidotopo")
        self.assertEqual(doc["place_id"], "wd:Q100002611", "the QID stays the identifier")

    def test_english_label_still_wins(self):
        doc = _doc(_entity("Q84", {"fr": "Londres", "en": "London", "mul": "London (mul)"}))
        self.assertEqual(doc["title"], "London")

    def test_regional_english_before_mul_and_the_rest(self):
        doc = _doc(_entity("Q1", {"fr": "Rive", "mul": "Riva", "en-gb": "River"}))
        self.assertEqual(doc["title"], "River")

    def test_mul_before_an_arbitrary_language(self):
        doc = _doc(_entity("Q2", {"cs": "Praha (cs)", "mul": "Praha"}))
        self.assertEqual(doc["title"], "Praha")

    def test_first_label_in_dump_order_when_nothing_is_preferred(self):
        doc = _doc(_entity("Q100002649", {"id": "Pasar Kedung Klinter", "bn": "কেডং ক্লিন্টার মার্কেট",
                                          "jv": "Pasar Kedung Klinter"}))
        self.assertEqual(doc["title"], "Pasar Kedung Klinter")

    def test_an_alias_serves_when_there_is_no_label_at_all(self):
        doc = _doc(_entity("Q3", {}, aliases={"de": ["Altstadt"]}))
        self.assertEqual(doc["title"], "Altstadt")

    def test_a_label_that_is_itself_a_qid_is_skipped(self):
        doc = _doc(_entity("Q4", {"xx": "Q4", "tr": "Köy"}))
        self.assertEqual(doc["title"], "Köy")

    def test_the_qid_remains_the_fallback_for_a_nameless_entity(self):
        # Nothing to show: the identifier is still better than an empty title,
        # and the record is still staged (its coordinates are real data).
        doc = _doc(_entity("Q5", {}))
        self.assertEqual(doc["title"], "Q5")
        self.assertEqual(doc["toponyms"], [])


class TestParityWithTheGatewayRepair(unittest.TestCase):
    """What ingest now stores is what the gateway already shows for the old
    records, so the re-ingest changes no displayed title; and once the stored
    title is a name, the repair leaves it alone and reports no ``qid_title``."""

    CASES = [
        _entity("Q100002611", {"id": "Pasar Krempyeng Sidotopo"}),
        _entity("Q84", {"fr": "Londres", "en": "London"}),
        _entity("Q1", {"fr": "Rive", "mul": "Riva", "en-gb": "River"}),
        _entity("Q2", {"cs": "Praha (cs)", "mul": "Praha"}),
        _entity("Q100002649", {"id": "Pasar Kedung Klinter", "bn": "কেডং ক্লিন্টার মার্কেট"}),
        _entity("Q3", {}, aliases={"de": ["Altstadt"]}),
        _entity("Q4", {"xx": "Q4", "tr": "Köy"}),
    ]

    def test_old_record_repaired_at_read_time_equals_new_record_as_stored(self):
        for entity in self.CASES:
            with self.subTest(qid=entity["id"]):
                doc = _doc(entity)
                names = _names(doc)
                self.assertEqual(display_title(entity["id"], names), (doc["title"], entity["id"]))

    def test_repair_leaves_the_new_title_alone(self):
        for entity in self.CASES:
            with self.subTest(qid=entity["id"]):
                doc = _doc(entity)
                self.assertEqual(display_title(doc["title"], _names(doc)), (doc["title"], None))

    def test_nameless_entity_is_the_same_both_ways(self):
        doc = _doc(_entity("Q5", {}))
        self.assertEqual(display_title(doc["title"], _names(doc)), ("Q5", None))


if __name__ == "__main__":
    unittest.main()
