"""Tests for the pure (non-ES) logic in ``clustering/signal_features.py`` —
canonical pair ordering and the hard/random negative assembler. The ES-dependent
generators are exercised by the live ``--calibrate`` run, not here.
"""

from __future__ import annotations

import random
import unittest

from clustering import signal_features as sf


class TestCanon(unittest.TestCase):
    def test_orders_and_filters(self):
        self.assertEqual(sf._canon("wd:Q90", "gn:1"), ("gn:1", "wd:Q90"))  # reordered
        self.assertEqual(sf._canon("gn:1", "wd:Q90"), ("gn:1", "wd:Q90"))
        self.assertIsNone(sf._canon("gn:1", "gn:2"))   # same namespace
        self.assertIsNone(sf._canon("gn:1", "gn:1"))   # same place
        self.assertIsNone(sf._canon("gn:1", ""))       # empty


class TestAssembleNegatives(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(0)

    def test_excludes_positives_and_overlay(self):
        rand = [("a:1", "b:1"), ("a:2", "b:2")]
        out = sf.assemble_negatives(rand, [], [], target=10,
                                    exclude={("a:1", "b:1")}, rng=self.rng)
        self.assertIn(("a:2", "b:2"), out)
        self.assertNotIn(("a:1", "b:1"), out)  # excluded

    def test_prefers_hard_then_fills_from_random(self):
        same = [(f"s:{i}", f"z:{i}") for i in range(100)]
        near = [(f"n:{i}", f"z:{i}") for i in range(100)]
        rand = [(f"r:{i}", f"z:{i}") for i in range(100)]
        out = sf.assemble_negatives(rand, same, near, target=30,
                                    exclude=set(), rng=self.rng)
        self.assertEqual(len(out), 30)
        kinds = {p[0].split(":")[0] for p in out}
        # With a 33/33 hard split of 30, both hard sources contribute.
        self.assertIn("s", kinds)
        self.assertIn("n", kinds)

    def test_fills_target_when_hard_short(self):
        # Hard sources nearly empty → random fills to target.
        rand = [(f"r:{i}", f"z:{i}") for i in range(50)]
        out = sf.assemble_negatives(rand, [("s:1", "z:1")], [], target=20,
                                    exclude=set(), rng=self.rng)
        self.assertEqual(len(out), 20)

    def test_no_duplicates(self):
        dup = [("a:1", "b:1")] * 10
        out = sf.assemble_negatives(dup, dup, dup, target=10,
                                    exclude=set(), rng=self.rng)
        self.assertEqual(len(out), len(set(out)))


if __name__ == "__main__":
    unittest.main()
