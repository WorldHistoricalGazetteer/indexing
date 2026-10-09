"""Gateway read-time fixes shipped 2026-10-09 for the Atlas beta push.

* place#290 — a stored title that is a bare Wikidata QID is replaced by the
  preferred toponym (en, else first) on /api/search, /api/places and
  /api/reconcile, with the QID kept as ``qid_title``. Tested through the
  response MODELS (an undeclared field is silently dropped on serialisation —
  reference_gateway_response_models) and through the real /api/search handler
  against canned ES responses, so the assembly site itself is exercised.
* AAT facet labels come from a per-worker cache of the ``types`` index; a failed
  read and a missing concept are distinguished in the log.
* place#294 — ``namespaces_excluded`` echoes the exclusion actually applied.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

try:
    from gateway import aat_labels
    from gateway.titles import display_title, is_bare_qid
    from gateway.search import SearchHit, SearchRequest, SearchResponse, search
    from gateway.reconcile import (
        CandidateHit, ReconcileRequest, ReconcileResponse, _format_candidate,
        reconcile_search,
    )
    from gateway.places import PlaceDetail, _format_place_detail
except ModuleNotFoundError as exc:  # pragma: no cover - dev machines without the API deps
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")


def run(coro):
    return asyncio.run(coro)


class TestDisplayTitle(unittest.TestCase):
    def test_bare_qid_detection(self):
        self.assertTrue(is_bare_qid("Q42"))
        self.assertTrue(is_bare_qid(" Q42 "))
        for t in ("", None, "Q", "q42", "Q42a", "Quebec", "Q 42"):
            with self.subTest(t=t):
                self.assertFalse(is_bare_qid(t))

    def test_english_name_preferred(self):
        names = [{"label": "Londres", "lang": "fr"}, {"label": "London", "lang": "en"}]
        self.assertEqual(display_title("Q84", names), ("London", "Q84"))

    def test_regional_english_counts_as_english(self):
        names = [{"label": "Paris", "lang": "fr"}, {"label": "Parys", "lang": "en-GB"}]
        self.assertEqual(display_title("Q90", names), ("Parys", "Q90"))

    def test_first_name_when_no_english(self):
        names = [{"label": "Londres", "lang": "fr"}, {"label": "Londra", "lang": "it"}]
        self.assertEqual(display_title("Q84", names), ("Londres", "Q84"))

    def test_names_that_are_themselves_qids_are_skipped(self):
        names = [{"label": "Q84", "lang": None}, {"label": "Londra", "lang": "it"}]
        self.assertEqual(display_title("Q84", names), ("Londra", "Q84"))

    def test_no_usable_name_keeps_the_qid_and_reports_nothing_moved(self):
        self.assertEqual(display_title("Q84", []), ("Q84", None))
        self.assertEqual(display_title("Q84", [{"label": "", "lang": "en"}]), ("Q84", None))

    def test_ordinary_title_untouched(self):
        names = [{"label": "Londinium", "lang": "la"}]
        self.assertEqual(display_title("London", names), ("London", None))
        self.assertEqual(display_title(None, names), ("", None))

    def test_objects_with_label_attributes(self):
        from gateway.places import CandidateName
        names = [CandidateName(label="Roma", lang="it"), CandidateName(label="Rome", lang="en")]
        self.assertEqual(display_title("Q220", names), ("Rome", "Q220"))


class TestQidTitleSurvivesTheModels(unittest.TestCase):
    """The field must be DECLARED or it vanishes on the way out."""

    def test_models_declare_qid_title(self):
        for model in (SearchHit, CandidateHit, PlaceDetail):
            with self.subTest(model=model.__name__):
                self.assertIn("qid_title", model.model_fields)

    def test_dump_carries_it(self):
        dumped = SearchHit(place_id="wd:Q84", title="London", qid_title="Q84").model_dump()
        self.assertEqual((dumped["title"], dumped["qid_title"]), ("London", "Q84"))

    def test_reconcile_formatter(self):
        src = {"place_id": "wd:Q84", "namespace": "wd", "title": "Q84"}
        hit = _format_candidate(src, 1.0, [{"label": "Londres", "lang": "fr"},
                                           {"label": "London", "lang": "en"}], None, None)
        dumped = hit.model_dump()
        self.assertEqual((dumped["title"], dumped["qid_title"]), ("London", "Q84"))

    def test_reconcile_formatter_presence_and_absence_together(self):
        # Absence asserted alongside a presence, so a formatter that never ran
        # cannot pass this by returning defaults.
        ok = _format_candidate({"place_id": "gn:1", "title": "Paris"}, 1.0,
                               [{"label": "Paris", "lang": "en"}], None, None)
        self.assertEqual(ok.title, "Paris")
        self.assertIsNone(ok.qid_title)

    def test_places_formatter_falls_back_to_nested_toponyms(self):
        src = {"place_id": "wd:Q220", "namespace": "wd", "title": "Q220",
               "toponyms": [{"label": "Roma", "lang": "it"}, {"label": "Rome", "lang": "en"}]}
        detail = _format_place_detail(src, None)
        self.assertEqual((detail.title, detail.qid_title), ("Rome", "Q220"))
        self.assertEqual(detail.model_dump()["qid_title"], "Q220")

    def test_places_formatter_still_repairs_title_when_names_not_requested(self):
        src = {"place_id": "wd:Q220", "title": "Q220",
               "toponyms": [{"label": "Rome", "lang": "en"}]}
        detail = _format_place_detail(src, None, requested_fields={"ccodes"})
        self.assertEqual((detail.title, detail.qid_title, detail.names), ("Rome", "Q220", []))


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeES:
    """Stand-in for httpx.AsyncClient: answers by index name in the URL."""

    def __init__(self, toponym_hits, place_hits, aggs=None):
        self.toponym_hits = toponym_hits
        self.place_hits = place_hits
        self.aggs = aggs or {}
        self.calls = []

    def __call__(self, *a, **kw):  # constructor
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kw):
        self.calls.append(url)
        if "/places/" in url:
            return _Resp({"hits": {"hits": self.place_hits,
                                   "total": {"value": len(self.place_hits)}},
                          "aggregations": self.aggs})
        return _Resp({"hits": {"hits": self.toponym_hits}})


class TestSearchHandlerAssemblesTheTitle(unittest.TestCase):
    """Drive the real /api/search handler (exact mode) against canned ES."""

    def _search(self, req, fake):
        with mock.patch("gateway.search.httpx.AsyncClient", fake), \
                mock.patch("gateway.search.es_auth", return_value=None):
            return run(search(req))

    def test_qid_title_replaced_in_a_live_handler_path(self):
        topo = [
            {"_score": 5.0, "_source": {"name": "Ouagadougou", "lang": "en",
                                        "attestations": ["wd:Q3777", "gn:2357048"]}},
            {"_score": 4.0, "_source": {"name": "Wagadugu", "lang": "ha",
                                        "attestations": ["wd:Q3777"]}},
        ]
        places = [
            {"_source": {"place_id": "wd:Q3777", "namespace": "wd", "title": "Q3777"}},
            {"_source": {"place_id": "gn:2357048", "namespace": "gn", "title": "Ouagadougou"}},
        ]
        fake = _FakeES(topo, places)
        resp = self._search(SearchRequest(query="Ouagadougou", mode="exact"), fake)
        self.assertTrue(any("/places/" in c for c in fake.calls), "handler never reached places")
        by_id = {h.place_id: h for h in resp.hits}
        self.assertEqual(set(by_id), {"wd:Q3777", "gn:2357048"})
        self.assertEqual((by_id["wd:Q3777"].title, by_id["wd:Q3777"].qid_title),
                         ("Ouagadougou", "Q3777"))
        self.assertEqual((by_id["gn:2357048"].title, by_id["gn:2357048"].qid_title),
                         ("Ouagadougou", None))
        dumped = SearchResponse.model_validate(resp.model_dump()).model_dump()
        self.assertEqual(dumped["namespaces_excluded"], ["gb"])
        self.assertIn("Q3777", [h["qid_title"] for h in dumped["hits"]])

    def test_aat_facet_labels_come_from_the_cache(self):
        topo = [{"_score": 1.0, "_source": {"name": "X", "lang": "en", "attestations": ["gn:1"]}}]
        places = [{"_source": {"place_id": "gn:1", "namespace": "gn", "title": "X"}}]
        aggs = {"type_facets": {"by_aat": {"buckets": [
            {"key": 300008347, "doc_count": 3}, {"key": 300391431, "doc_count": 1}]}}}
        fake = _FakeES(topo, places, aggs)
        aat_labels.reset_cache()
        self.addCleanup(aat_labels.reset_cache)
        lookup = mock.AsyncMock(return_value={300008347: "inhabited places"})
        with mock.patch.object(aat_labels, "_lookup", lookup):
            resp = self._search(SearchRequest(query="X", mode="exact"), fake)
            again = self._search(SearchRequest(query="X", mode="exact"), fake)
        # Second search served from the cache: no new lookup for either id
        # (the labelled one is cached, the missing one negatively cached).
        self.assertEqual(lookup.await_count, 1)
        self.assertEqual([f["label"] for f in again.facets.aat_types],
                         ["inhabited places", "300391431"])
        labels = {f["aat_id"]: f["label"] for f in resp.facets.aat_types}
        self.assertEqual(labels, {300008347: "inhabited places", 300391431: "300391431"})


class TestNamespacesExcluded(unittest.TestCase):
    def test_declared_on_both_response_models(self):
        for model in (SearchResponse, ReconcileResponse):
            with self.subTest(model=model.__name__):
                self.assertIn("namespaces_excluded", model.model_fields)

    def test_search_default_exclusion_is_echoed_on_an_early_return(self):
        resp = run(search(SearchRequest()))  # no query, no scope: returns before ES
        self.assertEqual(resp.namespaces_excluded, ["gb"])

    def test_search_explicit_scope_overrides_exclusion(self):
        resp = run(search(SearchRequest(namespaces=["gb"])))
        self.assertEqual((resp.namespaces_searched, resp.namespaces_excluded), (["gb"], []))

    def test_search_caller_can_clear_it(self):
        resp = run(search(SearchRequest(exclude_namespaces=[])))
        self.assertEqual(resp.namespaces_excluded, [])

    def test_reconcile_echoes_it(self):
        resp = run(reconcile_search(ReconcileRequest()))
        self.assertEqual(resp.namespaces_excluded, ["gb"])
        resp = run(reconcile_search(ReconcileRequest(namespaces=["gn"])))
        self.assertEqual((resp.namespaces_searched, resp.namespaces_excluded), (["gn"], []))


class TestAatLabelCache(unittest.TestCase):
    def setUp(self):
        aat_labels.reset_cache()
        self.addCleanup(aat_labels.reset_cache)

    def test_memoised_after_one_lookup(self):
        lookup = mock.AsyncMock(return_value={1: "one", 2: "two"})
        with mock.patch.object(aat_labels, "_lookup", lookup):
            self.assertEqual(run(aat_labels.resolve_labels([1, 2], None)), {1: "one", 2: "two"})
            self.assertEqual(run(aat_labels.resolve_labels([2, 1], None)), {1: "one", 2: "two"})
        self.assertEqual(lookup.await_count, 1)

    def test_only_unseen_ids_are_looked_up(self):
        lookup = mock.AsyncMock(side_effect=[{1: "one"}, {3: "three"}])
        with mock.patch.object(aat_labels, "_lookup", lookup):
            run(aat_labels.resolve_labels([1], None))
            self.assertEqual(run(aat_labels.resolve_labels([1, 3], None)), {1: "one", 3: "three"})
        self.assertEqual(lookup.await_args_list[1].args[0], [3])

    def test_one_retry_then_success(self):
        lookup = mock.AsyncMock(side_effect=[httpx_timeout(), {7: "seven"}])
        with mock.patch.object(aat_labels, "_lookup", lookup), \
                self.assertLogs("gateway.aat_labels", "WARNING") as logs:
            self.assertEqual(run(aat_labels.resolve_labels([7], None)), {7: "seven"})
        self.assertEqual(lookup.await_count, 2)
        self.assertTrue(any("lookup FAILED" in m for m in logs.output))

    def test_failure_and_missing_label_log_differently(self):
        # Failure: both attempts fail -> {} and FAILED lines, no "no label" line,
        # and the id is NOT negatively cached (it was never shown to be missing).
        with mock.patch.object(aat_labels, "_lookup",
                               mock.AsyncMock(side_effect=RuntimeError("down"))), \
                self.assertLogs("gateway.aat_labels", "INFO") as logs:
            self.assertEqual(run(aat_labels.resolve_labels([9], None)), {})
        self.assertEqual(sum("FAILED" in m for m in logs.output), 2)
        self.assertFalse(any("no label" in m for m in logs.output))
        self.assertNotIn(9, aat_labels._missing)

        # Missing: the read works, the concept is absent -> "no label", no FAILED.
        with mock.patch.object(aat_labels, "_lookup", mock.AsyncMock(return_value={1: "one"})), \
                self.assertLogs("gateway.aat_labels", "INFO") as logs:
            self.assertEqual(run(aat_labels.resolve_labels([1, 9], None)), {1: "one"})
        self.assertTrue(any("no label" in m and "9" in m for m in logs.output))
        self.assertFalse(any("FAILED" in m for m in logs.output))
        self.assertIn(9, aat_labels._missing)

    def test_missing_ids_are_rechecked_after_the_window(self):
        lookup = mock.AsyncMock(side_effect=[{}, {9: "nine"}])
        with mock.patch.object(aat_labels, "_lookup", lookup):
            run(aat_labels.resolve_labels([9], None))
            run(aat_labels.resolve_labels([9], None))      # inside window: no lookup
            self.assertEqual(lookup.await_count, 1)
            aat_labels._missing[9] -= aat_labels.MISSING_RECHECK_SECONDS + 1
            self.assertEqual(run(aat_labels.resolve_labels([9], None)), {9: "nine"})

    def test_warm_fills_the_cache_so_requests_need_no_lookup(self):
        with mock.patch.object(aat_labels, "_fetch_all", mock.AsyncMock(return_value={5: "five"})):
            self.assertTrue(run(aat_labels.warm(None)))
        lookup = mock.AsyncMock()
        with mock.patch.object(aat_labels, "_lookup", lookup):
            self.assertEqual(run(aat_labels.resolve_labels([5], None)), {5: "five"})
        lookup.assert_not_awaited()

    def test_warm_retries_once_then_gives_up_quietly(self):
        fetch = mock.AsyncMock(side_effect=RuntimeError("busy"))
        with mock.patch.object(aat_labels, "_fetch_all", fetch), \
                mock.patch.object(aat_labels.asyncio, "sleep", mock.AsyncMock()), \
                self.assertLogs("gateway.aat_labels", "WARNING") as logs:
            self.assertFalse(run(aat_labels.warm(None)))
        self.assertEqual(fetch.await_count, 2)
        self.assertTrue(any("lookup FAILED" in m for m in logs.output))
        self.assertEqual(aat_labels.cache_size(), 0)

    def test_empty_types_read_is_a_failure_not_an_empty_cache(self):
        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *e): return False
            async def post(self, *a, **k): return _Resp({"hits": {"hits": []}})
        with mock.patch.object(aat_labels.httpx, "AsyncClient", _Client):
            with self.assertRaises(RuntimeError):
                run(aat_labels._fetch_all(None))

    def test_fetch_all_pages_with_search_after(self):
        pages = [
            {"hits": {"hits": [{"_source": {"aat_id": i, "term": f"t{i}"}, "sort": [i]}
                               for i in range(aat_labels._PAGE)]}},
            {"hits": {"hits": [{"_source": {"aat_id": 10**6, "term": "last"}, "sort": [10**6]},
                               {"_source": {"aat_id": 10**6 + 1, "term": ""}, "sort": [10**6 + 1]}]}},
        ]
        bodies = []

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *e): return False
            async def post(self, url, json=None, **k):
                bodies.append(json)
                return _Resp(pages[len(bodies) - 1])
        with mock.patch.object(aat_labels.httpx, "AsyncClient", _Client):
            labels = run(aat_labels._fetch_all(None))
        self.assertEqual(len(labels), aat_labels._PAGE + 1)  # blank term dropped
        self.assertNotIn("search_after", bodies[0])
        self.assertEqual(bodies[1]["search_after"], [aat_labels._PAGE - 1])


def httpx_timeout():
    import httpx
    return httpx.ReadTimeout("timed out")


if __name__ == "__main__":
    unittest.main()
