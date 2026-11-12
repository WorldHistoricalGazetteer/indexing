"""The reconcile result cache is correct, bounded, and never stores a degraded answer.

A cache that is occasionally wrong is worse than none: it turns a transient fault
into a fault that lasts for the TTL, and it hides index changes. So the interesting
tests are the negative ones.

* TTLCache: hit/miss, expiry, LRU eviction, disabled by size 0 or ttl 0.
* Endpoint on a stubbed ES: a repeat makes NO Elasticsearch call and returns an equal
  response; a different request misses; embeddings/clustering requests bypass.
* Degraded answers are not stored: enrichment failure, missing KNN pass, unapplied
  scope. After a degraded answer the next identical request recomputes (and, once
  healthy, caches).
* Entries expire after the TTL (fake clock).
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

try:
    from gateway import reconcile as rc
    from gateway.result_cache import TTLCache
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")

from tests._es_stub import FakeES


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TTLCacheUnit(unittest.TestCase):
    def test_hit_miss_and_stats(self):
        c = TTLCache(4, 60)
        self.assertIsNone(c.get("a"))
        c.put("a", 1)
        self.assertEqual(c.get("a"), 1)
        s = c.stats()
        self.assertEqual((s["hits"], s["misses"], s["entries"]), (1, 1, 1))

    def test_entries_expire_after_the_ttl(self):
        clock = Clock()
        c = TTLCache(4, 60, clock)
        c.put("a", 1)
        clock.t += 59
        self.assertEqual(c.get("a"), 1)
        clock.t += 2
        self.assertIsNone(c.get("a"))
        self.assertEqual(c.stats()["expired"], 1)
        self.assertEqual(len(c), 0)

    def test_least_recently_used_is_evicted_and_a_read_refreshes(self):
        c = TTLCache(2, 60)
        c.put("a", 1); c.put("b", 2)
        c.get("a")                      # a is now most recent
        c.put("c", 3)                   # evicts b
        self.assertIsNone(c.get("b"))
        self.assertEqual(c.get("a"), 1)
        self.assertEqual(c.get("c"), 3)
        self.assertEqual(c.stats()["evictions"], 1)

    def test_size_zero_or_ttl_zero_disables_it(self):
        for c in (TTLCache(0, 60), TTLCache(10, 0)):
            self.assertFalse(c.enabled)
            c.put("a", 1)
            self.assertIsNone(c.get("a"))
            self.assertEqual(len(c), 0)


class EndpointCache(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cache = TTLCache(16, 120, self.clock)
        self._p = [mock.patch.object(rc, "_result_cache", self.cache),
                   mock.patch.object(rc, "_build_phonetic_knn",
                                     lambda *a, **k: {"knn": {"stub": 1}, "size": 10})]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])

    def _call(self, fake, **kw):
        kw.setdefault("size", 10)
        req = rc.ReconcileRequest(query=kw.pop("query", "Sarum"), mode="fuzzy", **kw)
        with fake.installed():
            return asyncio.run(rc.reconcile_search(req))

    def test_a_repeat_makes_no_elasticsearch_call_and_returns_an_equal_response(self):
        fake = FakeES(n_places=40)
        first = self._call(fake)
        calls_after_first = len(fake.calls)
        self.assertGreater(calls_after_first, 0)
        second = self._call(fake)
        self.assertEqual(len(fake.calls), calls_after_first, "cache hit must not touch ES")
        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertEqual(self.cache.stats()["hits"], 1)

    def test_any_difference_in_the_request_is_a_miss(self):
        fake = FakeES(n_places=40)
        self._call(fake)
        n = len(fake.calls)
        self._call(fake, ccodes=["GB"])
        self.assertGreater(len(fake.calls), n)
        n = len(fake.calls)
        self._call(fake, query="Other")
        self.assertGreater(len(fake.calls), n)
        n = len(fake.calls)
        self._call(fake, size=7)
        self.assertGreater(len(fake.calls), n)

    def test_entries_expire(self):
        fake = FakeES(n_places=40)
        self._call(fake)
        n = len(fake.calls)
        self.clock.t += 121
        self._call(fake)
        self.assertGreater(len(fake.calls), n, "an expired entry must be recomputed")

    def test_embedding_and_clustering_requests_bypass_the_cache(self):
        fake = FakeES(n_places=40)
        for kw in ({"include_embeddings": True}, {"include_clustering_fields": True}):
            self._call(fake, **kw)
            n = len(fake.calls)
            self._call(fake, **kw)
            self.assertGreater(len(fake.calls), n, f"{kw} must not be cached")
        self.assertEqual(len(self.cache), 0)

    def test_an_enrichment_failure_is_not_cached_and_recovers_when_healthy(self):
        sick = FakeES(n_places=40, fail={"enrich"})
        res = self._call(sick)
        self.assertEqual(len(res.hits), 10, "degraded but still answers")
        self.assertEqual(len(self.cache), 0, "a degraded answer must not be stored")
        n = len(sick.calls)
        self._call(sick)
        self.assertGreater(len(sick.calls), n, "the repeat must recompute, not serve the fault")
        healthy = FakeES(n_places=40)
        self._call(healthy)
        self.assertEqual(len(self.cache), 1, "a healthy answer is stored")

    def test_a_missing_knn_pass_is_not_cached(self):
        fake = FakeES(n_places=40)
        with mock.patch.object(rc, "_build_phonetic_knn", lambda *a, **k: None):
            self._call(fake)
        self.assertEqual(len(self.cache), 0)

    def test_an_unapplied_scope_is_not_cacheable(self):
        class S: is_degraded = False
        scope = mock.Mock(applied=False)
        self.assertFalse(rc._cacheable(mock.Mock(scope=scope), S()))
        self.assertTrue(rc._cacheable(mock.Mock(scope=mock.Mock(applied=True)), S()))
        self.assertTrue(rc._cacheable(mock.Mock(scope=None), S()))

    def test_the_degraded_flag_does_not_leak_between_requests(self):
        a, b = rc._NullStages(), rc._NullStages()
        a.degraded()
        self.assertTrue(a.is_degraded)
        self.assertFalse(b.is_degraded)

    def test_the_cache_key_covers_every_field_of_the_request(self):
        base = rc._cache_key(rc.ReconcileRequest(query="Sarum"))
        self.assertEqual(base, rc._cache_key(rc.ReconcileRequest(query="Sarum")))
        for kw in ({"ccodes": ["GB"]}, {"size": 3}, {"mode": "exact"}, {"lang": "fr"},
                   {"namespaces": ["gn"]}, {"variants": ["Sarem"]}, {"start_year": 1500}):
            self.assertNotEqual(base, rc._cache_key(rc.ReconcileRequest(query="Sarum", **kw)), kw)


if __name__ == "__main__":
    unittest.main()
