"""Gateway source-attribution echo (place#157).

Multi-record responses carry the set of authorities they draw on, so the Django
layer can resolve per-source licence terms in one registry lookup instead of
re-deriving the set by string-splitting every result id (which breaks if the
``{ns}:{id}`` format ever changes, and cannot express "searched, matched
nothing").
"""

from __future__ import annotations

import unittest

from gateway.es_helpers import collect_namespaces


class _Hit:
    """Stand-in for SearchHit / CandidateHit / PlaceDetail."""

    def __init__(self, place_id="", namespace=""):
        self.place_id = place_id
        self.namespace = namespace


class TestCollectNamespaces(unittest.TestCase):
    def test_empty(self):
        self.assertEqual([], collect_namespaces([]))

    def test_deduped_and_sorted(self):
        hits = [
            _Hit("gn:2643743", "gn"),
            _Hit("wd:Q84", "wd"),
            _Hit("gn:745044", "gn"),
            _Hit("osm:R65606", "osm"),
        ]
        self.assertEqual(["gn", "osm", "wd"], collect_namespaces(hits))

    def test_falls_back_to_id_prefix(self):
        """A doc indexed before the extract_namespace pipeline populated the
        field must still be attributed, not silently dropped from the terms."""
        self.assertEqual(["tgn"], collect_namespaces([_Hit("tgn:7011781", "")]))

    def test_multipart_ids_take_only_the_leading_segment(self):
        """Contributed-dataset ids are ``whg:<dataset>:<entity>``."""
        self.assertEqual(["whg"], collect_namespaces([_Hit("whg:1234:56", "")]))

    def test_accepts_raw_es_source_dicts(self):
        self.assertEqual(
            ["pl", "wd"],
            collect_namespaces([
                {"place_id": "pl:579885", "namespace": "pl"},
                {"place_id": "wd:Q220", "namespace": "wd"},
            ]),
        )

    def test_unattributable_records_are_skipped(self):
        """No namespace and no prefix ⇒ nothing to claim; never emit "".

        An empty string in the set would be resolved against the registry as a
        namespace and come back as unknown terms.
        """
        self.assertEqual([], collect_namespaces([_Hit("2643743", "")]))
        self.assertEqual([], collect_namespaces([{"place_id": None}]))


class TestResponseModels(unittest.TestCase):
    """The echo fields exist and default to empty (additive — an existing
    consumer sees byte-identical behaviour apart from the new keys)."""

    def test_search_response(self):
        from gateway.search import SearchResponse
        r = SearchResponse()
        self.assertEqual([], r.namespaces)
        self.assertEqual([], r.namespaces_searched)

    def test_reconcile_response(self):
        from gateway.reconcile import ReconcileResponse
        r = ReconcileResponse()
        self.assertEqual([], r.namespaces)
        self.assertEqual([], r.namespaces_searched)

    def test_places_response(self):
        from gateway.places import PlacesResponse
        self.assertEqual([], PlacesResponse().namespaces)


if __name__ == "__main__":
    unittest.main()
