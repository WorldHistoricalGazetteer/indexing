"""The discrimination gate must FIRE — on the degenerate model, and on easy negatives.

Two distinct mutations, because they are two distinct historical failures:

  1. A model that scores everything high. The old pairs test reported 100% and
     could not fail. AUC must land at chance and the constant-scorer must be
     visibly no better than a coin.
  2. A negative set that is not matched. This is the one that produces a
     FLATTERING number rather than a null one — an unmatched negative set lets a
     script detector score AUC ~1.0, and nothing in the output says so. The
     guard must refuse to score it at all.

Pure numpy/sklearn, no torch, no ES, no /vast.
"""
import unittest

from evaluation.discrimination import (
    NegativesNotMatched, Pair, check_negative_matching, evaluate_pairs)


def _pair(q, c, label, qs="LATIN", cs="CYRILLIC", stratum=""):
    return Pair(query=q, query_lang="en", query_script=qs,
                candidate=c, candidate_lang="ru", candidate_script=cs,
                label=label, stratum=stratum)


def _matched_set(n=200):
    """Positives and negatives identical in script pair and length band, so the
    only thing that can separate them is the scorer."""
    pairs = []
    for i in range(n):
        pairs.append(_pair(f"name{i:04d}", f"имя{i:04d}xx", 1))
        pairs.append(_pair(f"name{i:04d}", f"дру{i:04d}yy", 0))
    return pairs


class DiscriminationFiresTest(unittest.TestCase):

    def test_constant_scorer_lands_at_chance_not_at_100_percent(self):
        """MUTATION: the model that returns 1.0 for everything.

        This is precisely what "100% of pairs clear 0.65" was compatible with.
        AUC must be 0.5 — and crucially it must be REPORTED, not hidden behind a
        pass rate.
        """
        res = evaluate_pairs(_matched_set(), lambda p: 1.0, "always-1.0")
        self.assertAlmostEqual(res.auc, 0.5, places=6)
        self.assertAlmostEqual(res.positive_mean, res.negative_mean, places=6)

    def test_a_real_separation_is_detected(self):
        """The control. Without it, a gate that reports 0.5 for everything looks
        correct."""
        res = evaluate_pairs(_matched_set(),
                             lambda p: 0.9 if p.label == 1 else 0.1, "oracle")
        self.assertAlmostEqual(res.auc, 1.0, places=6)

    def test_unmatched_negatives_are_refused(self):
        """MUTATION: negatives drawn from a different script pair.

        A script detector scores ~1.0 on this set. The guard must refuse rather
        than let the number out.
        """
        pairs = ([_pair(f"name{i:04d}", f"имя{i:04d}xx", 1) for i in range(200)]
                 + [_pair(f"name{i:04d}", f"nombre{i:04d}", 0, cs="LATIN")
                    for i in range(200)])
        with self.assertRaises(NegativesNotMatched) as ctx:
            check_negative_matching(pairs)
        self.assertIn("script pair", str(ctx.exception))

    def test_a_script_detector_would_have_scored_near_perfect_on_them(self):
        """Proof that the previous test is guarding something real, not a rule
        for its own sake: with the guard off, a scorer that reads ONLY the
        candidate's script — and knows nothing about names — scores near 1.0."""
        pairs = ([_pair(f"name{i:04d}", f"имя{i:04d}xx", 1) for i in range(200)]
                 + [_pair(f"name{i:04d}", f"nombre{i:04d}", 0, cs="LATIN")
                    for i in range(200)])
        res = evaluate_pairs(pairs,
                             lambda p: 1.0 if p.candidate_script == "CYRILLIC" else 0.0,
                             "script-detector", require_matched=False)
        self.assertGreater(res.auc, 0.99)

    def test_unmatched_length_bands_are_refused(self):
        """MUTATION: negatives systematically shorter than positives.

        Length is the other free shortcut, and it is easy to introduce by
        accident when negatives are sampled without a band constraint.
        """
        pairs = ([_pair(f"name{i:04d}", "и" * 14, 1) for i in range(200)]
                 + [_pair(f"name{i:04d}", "д" * 3, 0) for i in range(200)])
        with self.assertRaises(NegativesNotMatched) as ctx:
            check_negative_matching(pairs)
        self.assertIn("length band", str(ctx.exception))

    def test_single_class_set_has_no_auc_and_says_so(self):
        """The old pairs test's actual shape: positives only. It must be an
        error, not an AUC."""
        with self.assertRaises(NegativesNotMatched):
            check_negative_matching([_pair("a", "б", 1) for _ in range(10)])

    def test_uncovered_pairs_are_excluded_not_zeroed(self):
        """A baseline that cannot score a pair must not be given 0 for it.

        Zeroing turns 'inapplicable' into 'confidently wrong' — and on a set
        where the uncovered pairs are mostly positives it would report the
        baseline as worse than chance for a reason that is not its performance.
        """
        class S:
            def __init__(self, score, covered):
                self.score, self.covered = score, covered

        pairs = _matched_set(100)
        # Cover only the first half; the rest are uncovered on BOTH classes.
        def scorer(p):
            i = int(p.query[4:])
            if i >= 50:
                return S(None, False)
            return S(0.9 if p.label == 1 else 0.1, True)

        res = evaluate_pairs(pairs, scorer, "half-covered")
        self.assertEqual(res.n_pairs, 200)
        self.assertEqual(res.n_scored, 100)
        self.assertAlmostEqual(res.coverage, 0.5)
        self.assertAlmostEqual(res.auc, 1.0, places=6)

    def test_scorer_that_covers_nothing_reports_no_auc(self):
        class S:
            score, covered = None, False
        res = evaluate_pairs(_matched_set(50), lambda p: S(), "covers-nothing")
        self.assertEqual(res.n_scored, 0)
        self.assertIsNone(res.auc)
        self.assertTrue(res.notes)


if __name__ == "__main__":
    unittest.main()
