"""Tests for the offline calibration signal math + artefact writers
(``clustering/calibrate_params.py``). The empirical ``calibrate()`` fit needs
ES + numpy/sklearn and is not exercised here; the pure signal functions and the
defaults writer are.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from clustering import calibrate_params as cpar


class TestSignalMath(unittest.TestCase):
    def test_haversine_known_distance(self):
        # London → Paris ≈ 343 km.
        km = cpar.haversine_km(51.5, -0.13, 48.85, 2.35)
        self.assertAlmostEqual(km, 343.5, delta=2.0)

    def test_spatial_signal_monotone(self):
        self.assertEqual(cpar.spatial_signal(0.0), 1.0)
        self.assertAlmostEqual(cpar.spatial_signal(25.0, half_life_km=25.0), 0.5)
        self.assertEqual(cpar.spatial_signal(None), 0.0)
        self.assertGreater(cpar.spatial_signal(10), cpar.spatial_signal(100))

    def test_temporal_overlap(self):
        self.assertAlmostEqual(cpar.temporal_overlap([1500, 1700], [1600, 1800]), 1/3)
        self.assertEqual(cpar.temporal_overlap([1500, 1550], [1600, 1700]), 0.0)
        self.assertEqual(cpar.temporal_overlap([1600, 1600], [1600, 1600]), 1.0)
        self.assertEqual(cpar.temporal_overlap(None, [1, 2]), 0.0)

    def test_wu_palmer(self):
        self.assertEqual(cpar.wu_palmer("1.2.3", "1.2.3"), 1.0)
        self.assertAlmostEqual(cpar.wu_palmer("1.2.3", "1.4.5"), 1/3)   # share root only
        self.assertAlmostEqual(cpar.wu_palmer("1.2.3", "1.2.9"), 4/6)   # share 1.2
        self.assertEqual(cpar.wu_palmer("1.2", "9.8"), 0.0)             # no common prefix

    def test_type_signal_takes_best_pair(self):
        self.assertAlmostEqual(cpar.type_signal(["1.2.3"], ["1.2.9", "1.4"]), 4/6)
        self.assertEqual(cpar.type_signal([], ["1.2"]), 0.0)

    def test_cosine_byte(self):
        self.assertAlmostEqual(cpar.cosine_byte([1, 2, 3], [1, 2, 3]), 1.0)
        self.assertAlmostEqual(cpar.cosine_byte([1, 0], [0, 1]), 0.0)
        self.assertEqual(cpar.cosine_byte([0, 0], [1, 1]), 0.0)  # zero-norm guard

    def test_best_threshold_separates(self):
        scores = [0.1, 0.2, 0.15, 0.8, 0.9, 0.85]
        y = [0, 0, 0, 1, 1, 1]
        t = cpar._best_threshold(scores, y)
        self.assertTrue(0.15 < t <= 0.8)


class TestDefaultsWriter(unittest.TestCase):
    def test_defaults_shape_and_write(self):
        with TemporaryDirectory() as tmp:
            orig = cpar.PARAMS_FILE
            cpar.PARAMS_FILE = Path(tmp) / "clustering_params.json"
            try:
                params = cpar.write_defaults()
                on_disk = json.loads(cpar.PARAMS_FILE.read_text())
            finally:
                cpar.PARAMS_FILE = orig
        self.assertEqual(params, on_disk)
        self.assertFalse(params["calibrated"])
        # Weights sum to 1.0 and cover all five signals.
        w = params["weights"]
        self.assertEqual(set(w), {"name", "spatial", "temporal", "type", "link"})
        self.assertAlmostEqual(sum(w.values()), 1.0)
        # All thresholds present.
        self.assertEqual(set(params["thresholds"]),
                         {"theta_query", "theta_bridge", "theta_synth",
                          "theta_synth_structural", "tau_name", "tau_link"})


if __name__ == "__main__":
    unittest.main()
