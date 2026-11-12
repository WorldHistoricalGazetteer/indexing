"""Trim-before-enrichment must not change what /api/reconcile returns.

The enrichment fetch used to cover every survivor of the places filter (up to 200)
although only ``size`` can reach the response. It now covers only those ``size``.
This is safe only if the survivors chosen BEFORE enrichment are exactly the ones
Step 4's sort-and-slice would have kept, in the same order — including ties.

Asserted three ways, because each alone is weak:

* the selector against a brute-force reference on random scores with many ties;
* the real endpoint on a stubbed ES: the enrichment request now names ONLY the
  top ``size`` places, and sizes the fetch from the UNTRIMMED count;
* end to end, the response equals the one produced with the trim disabled.
"""

from __future__ import annotations

import asyncio
import random
import unittest
from unittest import mock

try:
    from gateway import reconcile as rc
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")

from tests._es_stub import FakeES


def _hit(pid):
    return {"_source": {"place_id": pid}}


def _reference(raw_hits, place_scores, size):
    """Step 4 as written: score every hit, stable-sort descending, slice."""
    top = max(place_scores.values()) if place_scores else 1.0
    scored = [((place_scores.get(h["_source"]["place_id"], 0.0) / top * 100) if top > 0 else 0, h)
              for h in raw_hits]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [h for _, h in scored][:size]


class Selector(unittest.TestCase):
    def test_matches_the_reference_on_random_data_with_heavy_ties(self):
        rng = random.Random(7)
        for trial in range(300):
            n = rng.randint(0, 60)
            pids = [f"p{i}" for i in range(n)]
            # few distinct scores -> many ties; some places absent from the scores dict
            scores = {p: rng.choice([0.0, 0.3, 0.3, 0.7, 0.7, 0.7, 1.0, 1.0]) for p in pids if rng.random() < .85}
            hits = [_hit(p) for p in pids]
            for size in (1, 3, 10, 50, 100):
                self.assertEqual(rc._top_hits_by_score(hits, scores, size),
                                 _reference(hits, scores, size), f"trial {trial} size {size}")

    def test_pure_spatial_no_scores_keeps_the_first_size_in_order(self):
        hits = [_hit(f"p{i}") for i in range(20)]
        self.assertEqual(rc._top_hits_by_score(hits, {}, 5), hits[:5])

    def test_nonpositive_top_score_does_not_divide_by_zero(self):
        hits = [_hit("a"), _hit("b"), _hit("c")]
        self.assertEqual(rc._top_hits_by_score(hits, {"a": 0.0, "b": 0.0}, 2), hits[:2])

    def test_small_pool_is_still_ordered_as_step_4_would_order_it(self):
        hits = [_hit("a"), _hit("b"), _hit("c")]
        self.assertEqual(rc._top_hits_by_score(hits, {"a": 0.2, "b": 0.9, "c": 0.5}, 10),
                         [_hit("b"), _hit("c"), _hit("a")])


class Endpoint(unittest.TestCase):
    def setUp(self):
        from gateway.result_cache import TTLCache
        p = mock.patch.object(rc, "_result_cache", TTLCache(0, 0))   # cache off: assert on ES traffic
        p.start()
        self.addCleanup(p.stop)

    def _run(self, fake, trim=True, **kw):
        req = rc.ReconcileRequest(query="Sarum", mode="fuzzy", size=10, **kw)
        patches = [mock.patch.object(rc, "_build_phonetic_knn", lambda *a, **k: {"knn": {"stub": 1}, "size": 10})]
        if not trim:
            patches.append(mock.patch.object(rc, "_top_hits_by_score", lambda hits, scores, size: hits))
        with fake.installed():
            for p in patches:
                p.start()
            try:
                return asyncio.run(rc.reconcile_search(req))
            finally:
                for p in patches:
                    p.stop()

    def test_enrichment_names_only_the_places_that_can_be_returned(self):
        fake = FakeES(n_places=60)
        res = self._run(fake)
        (enrich,) = [c for c in fake.calls if c["kind"] == "enrich"]
        self.assertEqual(len(enrich["ids"]), 10, "enrichment should cover only the top `size` places")
        self.assertEqual(set(enrich["ids"]), {h.place_id for h in res.hits})

    def test_enrichment_size_still_comes_from_the_untrimmed_count(self):
        fake = FakeES(n_places=60)
        self._run(fake)
        (enrich,) = [c for c in fake.calls if c["kind"] == "enrich"]
        self.assertEqual(enrich["body"]["size"], max(60 * 30, 500))

    def test_response_is_identical_with_and_without_the_trim(self):
        a = self._run(FakeES(n_places=60), trim=True)
        b = self._run(FakeES(n_places=60), trim=False)
        self.assertEqual(a.model_dump(), b.model_dump())
        self.assertEqual(len(a.hits), 10)
        self.assertTrue(all(h.names for h in a.hits), "every returned candidate must still have its names")

    def test_identical_when_fewer_places_than_size(self):
        a = self._run(FakeES(n_places=4), trim=True)
        b = self._run(FakeES(n_places=4), trim=False)
        self.assertEqual(a.model_dump(), b.model_dump())


if __name__ == "__main__":
    unittest.main()
