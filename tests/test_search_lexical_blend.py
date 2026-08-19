"""/api/search fuzzy discovery blends a lexical pass with the phonetic KNN.

The twin of the reconcile change (place#197 / place#188): KNN answers "what
sounds like this", which does not reliably include "the toponym spelled exactly
like this", so an exact match enters discovery on lexical evidence and outranks
any phonetic neighbour.

Search has no ``variants`` channel — only the primary query is looked up — and
it must keep its two invariants: a single normalised KNN pass changes no
ordering, and the candidate pool stays deterministic for stable pagination.

Pure-function tests: no live Elasticsearch.
"""

import unittest

from gateway.es_helpers import (
    LEXICAL_EXACT_BOOST,
    apply_lexical_boost,
    build_lexical_exact_query,
    collect_place_ids,
    rank_candidate_ids,
)


def _knn(pairs):
    return [{"_score": sc, "_source": {"name": n, "attestations": pids}}
            for n, sc, pids in pairs]


def _lex(pairs):
    return [{"_source": {"name": n, "attestations": pids}} for n, pids in pairs]


class TestSingleKnnPassNormalisation(unittest.TestCase):
    """Search runs ONE KNN pass, so normalising it must not re-order anything —
    it exists only to cap the phonetic band below the lexical boost."""

    def test_ordering_is_unchanged_by_normalisation(self):
        hits = _knn([("A", 0.97, ["gn:1"]), ("B", 0.93, ["gn:2"]), ("C", 0.88, ["gn:3"])])
        raw, norm = {}, {}
        collect_place_ids(hits, raw)
        collect_place_ids(hits, norm, normalise=True)
        self.assertEqual(rank_candidate_ids(raw, 10), rank_candidate_ids(norm, 10))

    def test_band_is_capped_at_one(self):
        scores = {}
        collect_place_ids(_knn([("A", 0.97, ["gn:1"])]), scores, normalise=True)
        self.assertLessEqual(max(scores.values()), 1.0)


class TestSearchLexicalBlend(unittest.TestCase):
    def test_exact_match_tops_a_pool_of_phonetic_neighbours(self):
        scores, names = {}, {}
        collect_place_ids(
            _knn([("Nizui", 0.99, ["gn:junk"]), ("Nishi-oguchi", 0.95, ["gn:junk2"])]),
            scores, match_names=names, normalise=True)
        apply_lexical_boost(_lex([("Newton with Scales", ["gn:good"])]), scores,
                            {"newton with scales": LEXICAL_EXACT_BOOST},
                            match_names=names)
        self.assertEqual(rank_candidate_ids(scores, 1), ["gn:good"])
        self.assertEqual(names["gn:good"], "Newton with Scales")

    def test_a_place_found_both_ways_beats_one_found_only_lexically(self):
        scores = {}
        collect_place_ids(_knn([("Paris", 1.0, ["gn:both"])]), scores, normalise=True)
        apply_lexical_boost(_lex([("Paris", ["gn:both", "gn:lex"])]), scores,
                            {"paris": LEXICAL_EXACT_BOOST})
        self.assertEqual(rank_candidate_ids(scores, 2), ["gn:both", "gn:lex"])

    def test_no_exact_match_leaves_the_phonetic_ranking_alone(self):
        # The honest case: nothing is spelled that way, so search behaves as before.
        scores = {}
        collect_place_ids(_knn([("A", 0.9, ["gn:1"]), ("B", 0.8, ["gn:2"])]),
                          scores, normalise=True)
        before = dict(scores)
        apply_lexical_boost([], scores, {"melford, long": LEXICAL_EXACT_BOOST})
        self.assertEqual(scores, before)

    def test_search_looks_up_only_the_primary_query(self):
        # No variants channel on /api/search.
        body = build_lexical_exact_query(["Newton with Scales"])
        self.assertEqual(body["query"]["terms"]["name.raw"], ["newton with scales"])

    def test_namespace_scope_reaches_the_lexical_pass(self):
        body = build_lexical_exact_query(["Sarum"], namespaces=["iv", "gn"])
        self.assertEqual(body["query"]["bool"]["filter"],
                         [{"terms": {"namespaces": ["iv", "gn"]}}])

    def test_pool_ordering_stays_deterministic_for_pagination(self):
        scores = {}
        collect_place_ids(_knn([("A", 0.9, [f"gn:{i}" for i in range(50)])]),
                          scores, normalise=True)
        apply_lexical_boost(_lex([("A", ["gn:7"])]), scores,
                            {"a": LEXICAL_EXACT_BOOST})
        page1 = rank_candidate_ids(scores, 10)
        self.assertEqual(page1[0], "gn:7")
        # A deeper fetch must be a consistent superset, or pages overlap/skip.
        self.assertEqual(rank_candidate_ids(scores, 30)[:10], page1)


if __name__ == "__main__":
    unittest.main()
