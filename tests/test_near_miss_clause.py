"""The near-miss tier no longer runs fuzzy expansion over the edge-n-gram field.

``name.prefix`` indexes every prefix of every name, so ``fuzziness: AUTO`` over it
walked an enormous term dictionary per segment per shard and dominated the near-miss
search (median 75 ms of Elasticsearch time vs 11 ms without, on the live index).
Retrieval of truncated forms needs no fuzziness (the query term is itself an indexed
gram), and typo tolerance stays on the whole-name fields.

What is pinned here, and why:

* the new clause has fuzziness on ``name`` / ``name_romanized`` and NONE on
  ``name.prefix``, which is matched exactly, and keeps the keyword boost;
* the clause is used by the near-miss search ONLY: ``/api/search``'s fuzzy clause is
  byte-for-byte what it was;
* the rollback switch restores the old clause;
* the real endpoint sends the new body (stubbed ES), so a refactor that stops using it
  fails here rather than silently reverting the 7x saving.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from gateway import es_helpers as eh

try:
    from gateway import reconcile as rc
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")

from tests._es_stub import FakeES


def _walk(obj):
    """Every dict in a JSON-ish structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


class Clause(unittest.TestCase):
    def test_prefix_length_and_boost_defaults_and_overrides(self):
        """The tuned defaults, and that the rollback env vars really change them."""
        import importlib, os
        self.assertEqual(eh.LEXICAL_FUZZY_BOOST, 1.15)
        self.assertLess(eh.LEXICAL_FUZZY_BOOST + 1.0, eh.LEXICAL_EXACT_BOOST * eh.VARIANT_SCORE_WEIGHT,
                        "the tier order: an exact variant match must still beat near-miss + best phonetic")
        old = {k: os.environ.get(k) for k in ("GATEWAY_NEAR_MISS_PREFIX_LENGTH", "GATEWAY_LEXICAL_FUZZY_BOOST")}
        try:
            os.environ["GATEWAY_NEAR_MISS_PREFIX_LENGTH"] = "2"
            os.environ["GATEWAY_LEXICAL_FUZZY_BOOST"] = "0.75"
            importlib.reload(eh)
            self.assertEqual((eh.NEAR_MISS_PREFIX_LENGTH, eh.LEXICAL_FUZZY_BOOST), (2, 0.75))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(eh)
        self.assertEqual((eh.NEAR_MISS_PREFIX_LENGTH, eh.LEXICAL_FUZZY_BOOST), (1, 1.15))

    def test_fuzziness_stays_on_the_whole_name_fields_and_leaves_the_ngram_field_exact(self):
        c = eh.build_near_miss_clause("Broxbourn")
        should = c["bool"]["should"]
        (mm,) = [s["multi_match"] for s in should if "multi_match" in s]
        self.assertEqual(mm["fields"], ["name^3", "name_romanized^2"])
        self.assertEqual((mm["fuzziness"], mm["prefix_length"]), ("AUTO", eh.NEAR_MISS_PREFIX_LENGTH))
        self.assertEqual(eh.NEAR_MISS_PREFIX_LENGTH, 1, "tuned 2026-11-13: first-letter slips must be findable")
        self.assertNotIn("name.prefix", json.dumps(mm), "no fuzzy expansion over the n-gram field")
        (prefix,) = [s["match"] for s in should if "match" in s]
        self.assertEqual(prefix, {"name.prefix": {"query": "Broxbourn"}})
        self.assertNotIn("fuzziness", json.dumps(prefix))
        (term,) = [s["term"] for s in should if "term" in s]
        self.assertEqual(term, {"name.raw": {"value": "broxbourn", "boost": 5}})

    def test_the_shared_fuzzy_clause_used_elsewhere_is_unchanged(self):
        std = eh.build_toponym_text_clause("Broxbourn", "fuzzy")
        (mm,) = [s["multi_match"] for s in std["bool"]["should"] if "multi_match" in s]
        self.assertEqual(mm["fields"], ["name^3", "name_romanized^2", "name.prefix"])
        self.assertEqual(mm["fuzziness"], "AUTO")

    def test_other_modes_pass_straight_through(self):
        for mode in ("exact", "starts", "in"):
            self.assertEqual(eh.build_near_miss_clause("Sarum", mode),
                             eh.build_toponym_text_clause("Sarum", mode))

    def test_rollback_switch_restores_the_old_clause(self):
        with mock.patch.object(eh, "NEAR_MISS_FUZZY_NGRAM", True):
            self.assertEqual(eh.build_near_miss_clause("Sarum"),
                             eh.build_toponym_text_clause("Sarum", "fuzzy"))

    def test_the_near_miss_query_uses_it_for_the_primary_and_every_variant(self):
        body = eh.build_lexical_fuzzy_query(["Sarum", "Sarem"], variant_weight=0.9)
        clauses = body["query"]["dis_max"]["queries"]
        self.assertEqual(len(clauses), 2)
        primary, variant = clauses[0], clauses[1]["bool"]["must"][0]
        for c in (primary, variant):
            mms = [d["multi_match"] for d in _walk(c) if "multi_match" in d]
            self.assertTrue(mms and all("name.prefix" not in m["fields"] for m in mms))
        self.assertEqual(clauses[1]["bool"]["boost"], 0.9)

    def test_a_single_form_is_not_wrapped_and_size_is_kept(self):
        body = eh.build_lexical_fuzzy_query(["Sarum"], size=123)
        self.assertEqual(body["size"], 123)
        self.assertNotIn("dis_max", body["query"])

    def test_other_callers_of_build_toponym_query_are_unaffected(self):
        std = eh.build_toponym_query("Sarum", "fuzzy")
        self.assertIn("name.prefix", json.dumps(std))


class EndpointUsesIt(unittest.TestCase):
    def test_the_real_endpoint_sends_the_new_near_miss_body(self):
        from gateway.result_cache import TTLCache
        fake = FakeES(n_places=20)
        req = rc.ReconcileRequest(query="Broxbourn", mode="fuzzy", size=10)
        with fake.installed(), mock.patch.object(rc, "_result_cache", TTLCache(0, 0)), \
                mock.patch.object(rc, "_build_phonetic_knn", lambda *a, **k: {"knn": {"stub": 1}, "size": 10}):
            asyncio.run(rc.reconcile_search(req))
        near = [c for c in fake.calls if c["kind"] == "discovery_other"
                and any("multi_match" in d for d in _walk(c["body"]))]
        self.assertEqual(len(near), 1, "exactly one near-miss search per request")
        for mm in (d["multi_match"] for d in _walk(near[0]["body"]) if "multi_match" in d):
            self.assertNotIn("name.prefix", mm["fields"])


if __name__ == "__main__":
    unittest.main()
