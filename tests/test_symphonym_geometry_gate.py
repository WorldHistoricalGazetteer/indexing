"""The geometry gate must FIRE. These are the mutations it claims to catch.

A gate nobody has watched fail is a decoration, and this repository has shipped
several: a pairs test that asserted known matches score high and never asked
what non-matches score; an equivalence corpus containing none of the characters
whose handling it was checking. So every threshold in `geometry.DEFAULT_THRESHOLDS`
gets a synthetic input built to violate it, and one healthy input that must pass
all of them — because a gate that fails everything is as useless as one that
fails nothing, and only the pair of tests together distinguishes the two.

Pure numpy: no torch, no model, no ES, no /vast. It can run anywhere and cannot
touch production.
"""
import unittest

import numpy as np

from evaluation.geometry import DEFAULT_THRESHOLDS, measure_geometry

N, D = 4000, 128


def _unit(v):
    return v / np.linalg.norm(v, axis=1, keepdims=True)


class GeometryGateFiresTest(unittest.TestCase):

    def test_healthy_isotropic_space_passes(self):
        """The control. Without it, a gate that fails everything looks correct."""
        rng = np.random.default_rng(0)
        V = _unit(rng.standard_normal((N, D)))
        rep = measure_geometry(V)
        self.assertTrue(rep.passed, f"healthy space rejected:\n{rep.summary()}")
        self.assertGreater(rep.effective_rank, 120,
                           "an isotropic Gaussian should use nearly every dimension")

    def test_dimensional_collapse_fails(self):
        """MUTATION: the space spans 8 directions of 128 — Symphonym v7's defect.

        v7 measures effective rank 10.83, so a gate that cannot fail here cannot
        have caught v7.
        """
        rng = np.random.default_rng(1)
        basis = rng.standard_normal((8, D))
        V = _unit(rng.standard_normal((N, 8)) @ basis)
        rep = measure_geometry(V)
        self.assertFalse(rep.passed)
        self.assertLess(rep.effective_rank, DEFAULT_THRESHOLDS["effective_rank_min"])
        self.assertTrue(any("effective rank" in f for f in rep.failures), rep.failures)

    def test_off_centre_cloud_fails(self):
        """MUTATION: a large shared component, so every cosine is high.

        This is the 'random pairs reach 0.93' shape: the pairs test passes at
        100% and means nothing.
        """
        rng = np.random.default_rng(2)
        shared = rng.standard_normal(D)
        shared /= np.linalg.norm(shared)
        V = _unit(shared + 0.15 * rng.standard_normal((N, D)))
        rep = measure_geometry(V)
        self.assertFalse(rep.passed)
        self.assertGreater(rep.mean_norm, DEFAULT_THRESHOLDS["mean_norm_max"])
        self.assertTrue(any("mean vector" in f for f in rep.failures), rep.failures)

    def test_model_that_scores_everything_one_fails(self):
        """MUTATION: the degenerate 'always 1.0' model the pairs test cannot fail.

        Identical vectors plus float noise: every pair scores ~1.0, so a
        pass-rate-based check reports 100%.
        """
        rng = np.random.default_rng(3)
        v = rng.standard_normal(D)
        V = _unit(np.tile(v, (N, 1)) + 1e-6 * rng.standard_normal((N, D)))
        rep = measure_geometry(V)
        self.assertFalse(rep.passed)
        self.assertGreater(rep.cosine_p50, DEFAULT_THRESHOLDS["p50_cosine_max"])
        self.assertLess(rep.nn_gap, DEFAULT_THRESHOLDS["nn_gap_min"])

    def test_spectral_cliff_fails(self):
        """MUTATION: full-width support, but 99.9% of the scale in 12 components.

        Separated from the collapse case on purpose: a spectrum can have
        non-zero mass everywhere — so no dimension is literally dead — and still
        be unusable past the shelf. sigma20/sigma1 sees this; a rank count alone
        can miss it.
        """
        rng = np.random.default_rng(4)
        scale = np.concatenate([np.ones(12), np.full(D - 12, 1e-4)])
        V = _unit(rng.standard_normal((N, D)) * scale)
        rep = measure_geometry(V)
        self.assertFalse(rep.passed)
        self.assertLess(rep.sigma20_over_1, DEFAULT_THRESHOLDS["sigma20_over_1_min"])

    def test_refuses_a_sample_too_small_to_measure_saturation(self):
        """A gap over fewer vectors than k is a different quantity, not a noisier
        one. It must raise rather than return a comparable-looking number."""
        rng = np.random.default_rng(5)
        with self.assertRaises(ValueError):
            measure_geometry(_unit(rng.standard_normal((50, D))), knn_k=200)

    def test_the_sampled_path_agrees_with_the_exact_one(self):
        """The scale path is a DIFFERENT computation and must be shown to give
        the same answer, or a 1M-vector report is not comparable with a 6k one.

        Same 12,000 vectors measured both ways: exact, and forced through the
        sampled path by lowering the threshold. The neighbourhood statistics are
        over random probes so they will not match to the last decimal; they must
        match to within a tolerance far tighter than any threshold in the gate.
        """
        from evaluation import geometry as g
        rng = np.random.default_rng(11)
        V = _unit(rng.standard_normal((12_000, D)))
        exact = measure_geometry(V, knn_k=200)
        self.assertEqual(exact.method, "exact")
        original = g.EXACT_MAX_VECTORS
        try:
            g.EXACT_MAX_VECTORS = 1_000
            sampled = measure_geometry(V, knn_k=200, probe_rows=3_000,
                                       pair_samples=2_000_000)
        finally:
            g.EXACT_MAX_VECTORS = original
        self.assertTrue(sampled.method.startswith("sampled"))
        self.assertEqual(sampled.effective_rank, exact.effective_rank)
        self.assertEqual(sampled.mean_norm, exact.mean_norm)
        for a, b, tol in ((exact.cosine_p50, sampled.cosine_p50, 0.01),
                          (exact.nn1_cosine, sampled.nn1_cosine, 0.01),
                          (exact.nn200_cosine, sampled.nn200_cosine, 0.01)):
            self.assertAlmostEqual(a, b, delta=tol)
        self.assertEqual(sampled.passed, exact.passed)

    def test_the_sampled_path_still_fails_a_collapsed_space(self):
        """MUTATION through the scale path. A guard that fires only on the
        code path used by small corpora would pass every real 1M run."""
        from evaluation import geometry as g
        rng = np.random.default_rng(12)
        basis = rng.standard_normal((8, D))
        V = _unit(rng.standard_normal((30_000, 8)) @ basis)
        rep = measure_geometry(V, probe_rows=2_000, pair_samples=1_000_000)
        self.assertTrue(rep.method.startswith("sampled"))
        self.assertFalse(rep.passed)
        self.assertLess(rep.effective_rank, DEFAULT_THRESHOLDS["effective_rank_min"])

    def test_every_default_threshold_has_a_mutation_that_trips_it(self):
        """The column that exposed the hole in the last fixture: for each
        threshold, name the test above that violates it. A threshold with no
        such test is decorative and this fails until one exists."""
        covered = {
            "effective_rank_min": "test_dimensional_collapse_fails",
            "mean_norm_max": "test_off_centre_cloud_fails",
            "sigma20_over_1_min": "test_spectral_cliff_fails",
            "p50_cosine_max": "test_model_that_scores_everything_one_fails",
            "nn_gap_min": "test_model_that_scores_everything_one_fails",
        }
        self.assertEqual(set(DEFAULT_THRESHOLDS), set(covered),
                         "a threshold was added or removed without a mutation "
                         "test that trips it")


if __name__ == "__main__":
    unittest.main()
