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


class TestOverlayComponents(unittest.TestCase):
    def _overlay(self, tmp, rows):
        import sqlite3
        from pathlib import Path
        from clustering.sqlite_overlay import initialise_schema, _INSERT_SQL, _row_tuple
        p = Path(tmp) / "hl.sqlite"
        conn = sqlite3.connect(str(p))
        initialise_schema(conn)
        conn.executemany(_INSERT_SQL, [_row_tuple(r) for r in rows])
        conn.commit(); conn.close()
        return p

    def _row(self, a, b, rel="sameAs"):
        return {"place_a": a, "place_b": b, "relation_type": rel,
                "source_category": "authority", "source_id": "wd",
                "asserted_at": None, "justification": None}

    def test_transitive_pair_is_dropped(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            # a≡c and c≡b, but (a,b) is NOT a direct edge.
            db = self._overlay(tmp, [self._row("gn:1", "wd:9"),
                                     self._row("tgn:5", "wd:9")])
            comps = sf._overlay_components(db)
            # gn:1 and tgn:5 are in the same component via wd:9.
            dropped = sf._coreferent_pairs([("gn:1", "tgn:5")], comps)
            self.assertEqual(dropped, {("gn:1", "tgn:5")})

    def test_distinct_pair_is_kept(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            db = self._overlay(tmp, [self._row("gn:1", "wd:9", rel="distinct")])
            comps = sf._overlay_components(db)
            # distinct is NOT unioned → not coreferent → usable as a negative.
            self.assertEqual(sf._coreferent_pairs([("gn:1", "wd:9")], comps), set())

    def test_unrelated_pair_is_kept(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            db = self._overlay(tmp, [self._row("gn:1", "wd:9")])
            comps = sf._overlay_components(db)
            self.assertEqual(sf._coreferent_pairs([("gn:2", "wd:8")], comps), set())


if __name__ == "__main__":
    unittest.main()
