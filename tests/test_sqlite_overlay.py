"""Tests for clustering/sqlite_overlay.insert_rows.

Focused on the ``inserted`` counter — the previous implementation read
``SELECT changes()`` after the COMMIT which always returned 0 (the COMMIT
itself is the most recent statement and it changes 0 rows), so the run
report severely understated successful inserts. The fix uses
``conn.total_changes`` diffing.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from clustering.sqlite_overlay import builder, insert_rows


def _row(place_a: str, place_b: str, source_id: str = "authority:test:1") -> dict:
    return {
        "place_a": place_a,
        "place_b": place_b,
        "relation_type": "closeMatch",
        "source_category": "authority",
        "source_id": source_id,
        "asserted_at": None,
        "justification": None,
    }


class TestInsertRowsCounter(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "overlay.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_inserted_count_matches_actual_inserts(self):
        rows = [_row(f"a:{i}", f"b:{i}") for i in range(50)]
        with builder(self.db_path) as conn:
            stats = insert_rows(conn, rows, batch_size=10)
            actual = conn.execute("SELECT COUNT(*) FROM hard_link_assertions").fetchone()[0]
        self.assertEqual(stats["attempted"], 50)
        self.assertEqual(stats["inserted"], 50)
        self.assertEqual(stats["rejected"], 0)
        self.assertEqual(actual, 50)

    def test_duplicates_collapse_correctly(self):
        # 30 unique rows + 20 exact duplicates → 30 inserted, 20 ignored.
        unique = [_row(f"a:{i}", f"b:{i}") for i in range(30)]
        rows = unique + unique[:20]
        with builder(self.db_path) as conn:
            stats = insert_rows(conn, rows, batch_size=15)
            actual = conn.execute("SELECT COUNT(*) FROM hard_link_assertions").fetchone()[0]
        self.assertEqual(stats["attempted"], 50)
        self.assertEqual(stats["inserted"], 30)
        self.assertEqual(actual, 30)

    def test_multibatch_counts_correctly(self):
        # Specifically guards against the previous bug: with the broken
        # changes() reader, only the LAST batch's count was visible (or 0).
        rows = [_row(f"a:{i}", f"b:{i}") for i in range(123)]
        with builder(self.db_path) as conn:
            stats = insert_rows(conn, rows, batch_size=20)  # ~7 batches
        self.assertEqual(stats["inserted"], 123)


if __name__ == "__main__":
    unittest.main()
