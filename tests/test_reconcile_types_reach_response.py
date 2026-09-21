"""Reconcile candidates must carry their source-vocabulary ``types`` (place#273).

16.6% of *confident* accepted matches were lakes, airfields and war memorials.
It is a RANKING failure, not a coverage one: in 5 of 6 probed rows the correctly
typed settlement was already in the candidate pool and lost to an identically
named non-settlement, because nothing downstream could tell them apart.

Two independent gaps had to line up for that, and each is silent on its own:

1. ``build_places_filter`` never selected ``types`` in ``_source`` unless the
   caller asked for clustering fuel — an opt-in defaulting to false — so the
   ordinary reconcile request did not fetch it.
2. ``CandidateHit`` did not declare ``types``, so pydantic would have dropped it
   on serialisation even had the query returned it. An undeclared field does not
   raise; it vanishes.

⚠ Fixing only one of the two leaves the symptom exactly as it was, and each
looks fixed in isolation — so both are asserted here, separately.

``aat_ids``/``aat_paths`` cannot stand in for this: the AAT projection is 0%
outside ``tgn``, whereas the discriminating value (``wd Q3947`` house,
``gn S.CSTL``, ``osm natural=water``) is on nearly every record already.
"""

from __future__ import annotations

import unittest

from gateway.es_helpers import build_places_filter

try:
    from gateway.reconcile import CandidateHit, _format_candidate
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")


TYPES = [
    {"identifier": "Q3947", "label": "wikidata", "sourceLabel": "P31=Q3947"},
    {"identifier": "S.CSTL", "label": "S", "sourceLabel": "S.CSTL"},
]


class TypesAreSelectedFromEs(unittest.TestCase):
    """Gap 1 — the query must actually ask ES for the field.

    ⚠ The first version of this class asserted only that
    ``build_places_filter(extra_source=["types"])`` works — i.e. that the
    MECHANISM exists. Removing the fix from ``reconcile.py`` left every test
    green, because nothing checked that the endpoint USES the mechanism. A test
    that passes on the defect is worse than no test: it certifies the bug.
    ``test_reconcile_call_site_requests_types`` below is the one that fails.
    """

    def _source_of(self, **kw):
        body = build_places_filter(["gn:1"], None, None, None, None, **kw)
        return body["_source"]

    def test_extra_source_puts_types_in_the_projection(self):
        self.assertIn("types", self._source_of(extra_source=["types"]))

    def test_reconcile_call_site_requests_types(self):
        """THE fix-site assertion. Via AST, not a string search.

        A substring check for ``extra_source`` would pass on a docstring, a
        comment, or a mention in a different call. This walks to the
        ``_build_places_filter`` call the endpoint actually makes and reads its
        keyword.
        """
        import ast
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "gateway" / "reconcile.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))

        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "_build_places_filter":
                continue
            for kw in node.keywords:
                if kw.arg == "extra_source":
                    found.append(ast.literal_eval(kw.value))

        self.assertTrue(
            found,
            "reconcile.py never passes extra_source to _build_places_filter, so "
            "`types` is not fetched and every candidate is typeless (place#273)")
        for value in found:
            self.assertIn(
                "types", value,
                f"extra_source={value!r} does not request `types`")

    def test_types_absent_by_default_is_the_documented_baseline(self):
        """Not an endorsement — it records WHY reconcile must pass it.

        If a later change adds ``types`` to the shared base list, this fails and
        whoever does it should delete this test deliberately, having decided that
        /api/search should carry types too. That is a real decision, not a detail.
        """
        self.assertNotIn("types", self._source_of())

    def test_clustering_fuel_is_not_a_substitute(self):
        """types arrived only via an opt-in that defaults to false."""
        self.assertIn("types", self._source_of(clustering_fields=True))
        self.assertNotIn("types", self._source_of(clustering_fields=False))


class TypesSurviveSerialisation(unittest.TestCase):
    """Gap 2 — the model must declare it, or it is dropped without error."""

    def test_candidate_hit_declares_types(self):
        hit = CandidateHit(place_id="gn:1", title="Test", types=TYPES)
        self.assertEqual(hit.types, TYPES)

    def test_types_survive_a_model_dump(self):
        """The failure mode was silent loss at serialisation, so dump it."""
        dumped = CandidateHit(place_id="gn:1", title="Test", types=TYPES).model_dump()
        self.assertIn("types", dumped)
        self.assertEqual(dumped["types"], TYPES)

    def test_default_is_empty_list_not_none(self):
        self.assertEqual(CandidateHit(place_id="gn:1", title="T").types, [])


class TypesAreMappedFromTheSource(unittest.TestCase):
    """Gap 2b — declaring the field changes nothing if nothing populates it."""

    def _candidate(self, src):
        return _format_candidate(src, 1.0, None, None, None)

    def test_types_are_carried_from_the_es_source(self):
        hit = self._candidate({"place_id": "gn:1", "title": "Test", "types": TYPES})
        self.assertEqual(hit.types, TYPES)

    def test_missing_types_yields_empty_list(self):
        self.assertEqual(self._candidate({"place_id": "gn:1", "title": "T"}).types, [])

    def test_non_dict_entries_are_dropped_not_crashed(self):
        """A malformed type must not 500 the whole response."""
        hit = self._candidate(
            {"place_id": "gn:1", "title": "T", "types": [TYPES[0], "junk", None]})
        self.assertEqual(hit.types, [TYPES[0]])


if __name__ == "__main__":
    unittest.main()
