"""A ``namespaces`` scope may name ONE contributed dataset: ``whg:<dataset_id>``.

Website place#332: the Atlas Place List browses a gazetteer by posting
``{"browse": true, "namespaces": ["<gazetteer id>"]}``. For an authority the id
is its namespace code (``clio``) and the list filled; for a contributed dataset
the id is ``whg:<dataset_id>`` (the registry row's id), but a contributed place
doc carries ``namespace: "whg"`` and ``dataset_id: "whg:<dataset_id>"``
(authorities/whg-places.py), so ``terms: {namespace: ["whg:1203"]}`` matched
nothing and the list said "No places to browse in this gazetteer". A typed
search in the same list was empty for the same reason.

The fix maps such an entry to ``dataset_id`` on the places index and to a
``prefix`` on ``attestations`` on the toponyms index, leaves a plain namespace
scope byte-identical, and echoes the dataset entries as ``datasets_searched``.

How these fail without the fix: the places-filter assertions find
``{"terms": {"namespace": ["whg:1203"]}}`` instead of a ``dataset_id`` clause;
``datasets_searched`` does not exist on the response. The unchanged-shape
assertions are paired with the changed ones in the same test.

Pure-function / stubbed tests: no live Elasticsearch.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from gateway import symphonym
from gateway.es_helpers import (
    build_lexical_exact_query,
    build_phonetic_knn,
    build_places_filter,
    build_toponym_query,
    places_scope_filter,
    split_namespace_scope,
    toponym_scope_filter,
)
from gateway.search import SearchRequest, SearchResponse, search


def _filters(body: dict) -> list:
    return body["query"]["bool"]["filter"]


class TestSplit(unittest.TestCase):
    def test_codes_and_dataset_ids(self):
        self.assertEqual((["clio", "gn"], ["whg:1203"]),
                         split_namespace_scope(["clio", "whg:1203", "gn", " "]))
        self.assertEqual(([], []), split_namespace_scope(None))


class TestPlacesFilter(unittest.TestCase):
    def test_dataset_id_filters_dataset_id_not_namespace(self):
        body = build_places_filter(None, None, None, None, None, namespaces=["whg:1203"])
        self.assertIn({"terms": {"dataset_id": ["whg:1203"]}}, _filters(body))
        self.assertNotIn('"namespace"', json.dumps(_filters(body)))

    def test_plain_scope_is_unchanged(self):
        body = build_places_filter(None, None, None, None, None, namespaces=["clio", "gn"])
        self.assertIn({"terms": {"namespace": ["clio", "gn"]}}, _filters(body))
        self.assertNotIn("dataset_id", json.dumps(_filters(body)))

    def test_mixed_scope_is_an_or(self):
        self.assertEqual(
            places_scope_filter(["clio", "whg:12"]),
            {"bool": {"should": [{"terms": {"namespace": ["clio"]}},
                                 {"terms": {"dataset_id": ["whg:12"]}}],
                      "minimum_should_match": 1}})
        self.assertIsNone(places_scope_filter([]))


class TestToponymFilters(unittest.TestCase):
    def test_dataset_is_an_attestation_prefix_with_its_colon(self):
        self.assertEqual(toponym_scope_filter(["whg:12"]),
                         {"prefix": {"attestations": "whg:12:"}})   # never whg:123
        body = build_toponym_query("Hu", "in", namespaces=["whg:12"])
        self.assertEqual(_filters(body), [{"prefix": {"attestations": "whg:12:"}}])
        lex = build_lexical_exact_query(["Hubei"], namespaces=["whg:12"])
        self.assertEqual(_filters(lex), [{"prefix": {"attestations": "whg:12:"}}])

    def test_plain_scope_is_unchanged(self):
        body = build_toponym_query("Hu", "in", namespaces=["clio"])
        self.assertEqual(_filters(body), [{"terms": {"namespaces": ["clio"]}}])

    def test_knn_prefilter(self):
        with mock.patch.object(symphonym, "model_version", return_value="m"):
            def knn(ns):
                return build_phonetic_knn("Hubei", query_vector=[0] * 128,
                                          query_vector_model="m", namespaces=ns)
            self.assertEqual(knn(["whg:12"])["knn"]["filter"],
                             {"prefix": {"attestations": "whg:12:"}})
            self.assertEqual(knn(["iv"])["knn"]["filter"], {"terms": {"namespaces": ["iv"]}})


class TestResponseEcho(unittest.TestCase):
    def test_datasets_searched_is_derived_and_serialised(self):
        r = SearchResponse(namespaces_searched=["clio", "whg:1203"])
        self.assertEqual(r.datasets_searched, ["whg:1203"])
        self.assertEqual(r.model_dump(mode="json")["datasets_searched"], ["whg:1203"])
        self.assertEqual(SearchResponse().model_dump(mode="json")["datasets_searched"], [])


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    """httpx.AsyncClient stand-in: records every (url, body); answers a places
    query with one contributed place and a toponyms query with its name."""

    def __init__(self, *_a, **_k):
        self.posts: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, json=None, auth=None, headers=None):
        self.posts.append((url, json))
        if "/places" in url:
            return _Resp({"hits": {"total": {"value": 1}, "hits": [
                {"_source": {"place_id": "whg:1203:195002", "title": "Hubei",
                             "namespace": "whg", "dataset_id": "whg:1203",
                             "ccodes": ["CN"], "types": [], "geometries": []}}]},
                "aggregations": {}})
        return _Resp({"hits": {"hits": [
            {"_score": 3.0, "_source": {"name": "Hubei", "lang": "en",
                                        "attestations": ["whg:1203:195002"]}}]}})


class TestEndpoint(unittest.IsolatedAsyncioTestCase):
    async def _run(self, **kw):
        client = _Client()
        with mock.patch("gateway.search.httpx.AsyncClient", side_effect=lambda *a, **k: client):
            resp = await search(SearchRequest(**kw))
        places = [b for u, b in client.posts if "/places" in u]
        return resp, client, places

    async def test_browse_one_contributed_dataset(self):
        resp, _, places = await self._run(browse=True, namespaces=["whg:1203"], size=50)
        self.assertEqual(len(places), 1)
        self.assertIn({"terms": {"dataset_id": ["whg:1203"]}}, _filters(places[0]))
        self.assertEqual(["whg:1203:195002"], [h.place_id for h in resp.hits])
        self.assertEqual(resp.datasets_searched, ["whg:1203"])
        self.assertEqual(resp.namespaces_searched, ["whg:1203"])

    async def test_browse_an_authority_is_unchanged(self):
        _, _, places = await self._run(browse=True, namespaces=["clio"], size=50)
        self.assertIn({"terms": {"namespace": ["clio"]}}, _filters(places[0]))

    async def test_typed_search_in_one_dataset(self):
        resp, client, places = await self._run(query="Hub", mode="in", namespaces=["whg:1203"])
        discovery = client.posts[0][1]
        self.assertIn({"prefix": {"attestations": "whg:1203:"}}, _filters(discovery))
        self.assertIn({"terms": {"dataset_id": ["whg:1203"]}}, _filters(places[0]))
        self.assertEqual(["whg:1203:195002"], [h.place_id for h in resp.hits])


if __name__ == "__main__":
    unittest.main()
