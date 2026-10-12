"""Reconcile ``undated`` and the namespace pre-filter on phonetic KNN (2026-10-12).

``undated``: a year window used to drop every place with no timespan on
/api/reconcile whatever the caller asked, because the request model had no
field for it and ``build_places_filter`` was called with its default (False).

KNN pre-filter: the phonetic passes took the 200 nearest toponyms from EVERY
source and only then cut them to ``namespaces``, so a query scoped to a small
source could lose all its neighbours to bigger ones. The scope now travels into
ES's ``knn.filter``, the same ``terms`` clause the lexical passes use.

The endpoint tests drive ``reconcile_search`` against a fake ES and assert on
what was POSTED, so a call site that stops forwarding either fails them.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from gateway.es_helpers import _temporal_filter, build_phonetic_knn

try:
    from gateway import reconcile as rec
    from gateway import symphonym
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")


class _Resp:
    def __init__(self, hits): self._h = hits
    def raise_for_status(self): pass
    def json(self): return {"hits": {"hits": self._h, "total": {"value": len(self._h)}}}


class FakeClient:
    posts: list = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, **kw):
        FakeClient.posts.append((url, json))
        if rec.TOPONYMS_INDEX in url:
            return _Resp([{"_score": 1.0, "_source": {"name": "Gayhurst", "lang": "en",
                          "attestations": ["kain_par:1"]}}])
        return _Resp([{"_source": {"place_id": "kain_par:1", "namespace": "kain_par",
                                   "title": "Gayhurst", "geometries": []}}])


def run(**kw):
    FakeClient.posts = []
    req = rec.ReconcileRequest(query="Gateherst", mode="phonetic", **kw)
    with mock.patch("httpx.AsyncClient", FakeClient), \
         mock.patch.object(rec, "_build_phonetic_knn",
                           side_effect=lambda form, **k: {"knn": {}}) as knn:
        asyncio.run(rec.reconcile_search(req))
    places = [j for u, j in FakeClient.posts if rec.PLACES_INDEX in u]
    return knn, places


class KnnBodyCarriesTheFilter(unittest.TestCase):
    def _body(self, **kw):
        # A matching client vector skips the server-side embed, so no model is needed.
        with mock.patch.object(symphonym, "model_version", return_value="m"):
            return build_phonetic_knn("Gateherst", query_vector=[0] * 128,
                                      query_vector_model="m", **kw)

    def test_namespaces_become_a_knn_prefilter(self):
        body = self._body(namespaces=["kain_par", "iv"])
        self.assertEqual(body["knn"]["filter"], {"terms": {"namespaces": ["kain_par", "iv"]}})

    def test_no_namespaces_means_no_filter(self):
        self.assertIsNotNone(self._body())                       # positive control: a body was built
        self.assertNotIn("filter", self._body()["knn"])
        self.assertNotIn("filter", self._body(namespaces=[])["knn"])


class ReconcileForwardsNamespacesToKnn(unittest.TestCase):
    def test_every_phonetic_pass_is_scoped(self):
        knn, _ = run(namespaces=["kain_par"], variants=["Gaherst"])
        self.assertGreaterEqual(len(knn.call_args_list), 2)     # primary + variant
        self.assertEqual({tuple(c.kwargs["namespaces"]) for c in knn.call_args_list},
                         {("kain_par",)})

    def test_unscoped_query_is_unfiltered(self):
        knn, _ = run()
        self.assertTrue(knn.call_args_list)
        self.assertEqual({c.kwargs["namespaces"] for c in knn.call_args_list}, {None})


class ReconcileUndated(unittest.TestCase):
    def _filters(self, **kw):
        _, places = run(start_year=1000, end_year=1100, **kw)
        self.assertTrue(places)
        return places[0]["query"]["bool"]["filter"]

    def test_default_drops_undated(self):
        f = self._filters()
        self.assertIn(_temporal_filter(1000, 1100, "possibly", False), f)
        self.assertNotIn(_temporal_filter(1000, 1100, "possibly", True), f)

    def test_undated_true_keeps_undated(self):
        f = self._filters(undated=True)
        self.assertIn(_temporal_filter(1000, 1100, "possibly", True), f)
        self.assertNotIn(_temporal_filter(1000, 1100, "possibly", False), f)

    def test_temporal_mode_still_honoured(self):
        f = self._filters(undated=True, temporal_mode="definitely")
        self.assertIn(_temporal_filter(1000, 1100, "definitely", True), f)


if __name__ == "__main__":
    unittest.main()
