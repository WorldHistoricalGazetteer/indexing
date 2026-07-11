"""Tests for clustering/sqlite_overlay.insert_rows.

Focused on the ``inserted`` counter — the previous implementation read
``SELECT changes()`` after the COMMIT which always returned 0 (the COMMIT
itself is the most recent statement and it changes 0 rows), so the run
report severely understated successful inserts. The fix uses
``conn.total_changes`` diffing.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from clustering.sqlite_overlay import _PRUNE_REMOTE_PY, builder, insert_rows


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


class TestLiveDeltaPruneSnippet(unittest.TestCase):
    """Exercise the exact ``_PRUNE_REMOTE_PY`` snippet that ``prune_live_delta``
    ships over SSH — run it locally with ``python3 -c`` so the cutoff / NULL
    semantics are validated without a real Pitt host."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "hard_links_live.sqlite"
        # asserted_at values straddling the cutoff, plus a NULL. place_a/place_b
        # use a<b lettered suffixes so canonical ordering always holds.
        rows = [
            _contrib("p:before", "2026-07-10T00:00:00+00:00"),   # before → pruned
            _contrib("p:atcut",  "2026-07-11T12:00:00+00:00"),   # == cutoff → pruned
            _contrib("p:after1", "2026-07-11T12:00:00.5+00:00"),  # after → kept
            _contrib("p:after2", "2026-07-12T00:00:00+00:00"),   # after → kept
            _contrib("p:nullts", None),                           # NULL → kept
        ]
        with builder(self.db_path) as conn:
            insert_rows(conn, rows)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_prune(self, cutoff: str) -> int:
        result = subprocess.run(
            [sys.executable, "-c", _PRUNE_REMOTE_PY, str(self.db_path), cutoff],
            capture_output=True, text=True, check=True,
        )
        import json
        return json.loads(result.stdout.strip().splitlines()[-1])["deleted"]

    def test_prunes_at_or_before_cutoff_keeps_inflight_and_null(self):
        deleted = self._run_prune("2026-07-11T12:00:00+00:00")
        self.assertEqual(deleted, 2)  # before + exactly-at cutoff
        with builder(self.db_path) as conn:
            survivors = {r[0] for r in conn.execute(
                "SELECT place_a FROM hard_link_assertions").fetchall()}
        # in-flight (after cutoff) + NULL-asserted rows survive
        self.assertEqual(survivors, {"p:after1", "p:after2", "p:nullts"})

    def test_null_asserted_never_pruned(self):
        # Even a far-future cutoff leaves the NULL row (no insertion timestamp
        # to prove it predates the build).
        self._run_prune("2999-01-01T00:00:00+00:00")
        with builder(self.db_path) as conn:
            survivors = {r[0] for r in conn.execute(
                "SELECT place_a FROM hard_link_assertions").fetchall()}
        self.assertEqual(survivors, {"p:nullts"})


def _contrib(place_a: str, asserted_at: str | None) -> dict:
    # place_b sorts after place_a ('~' is high-ASCII) → canonical order holds.
    return {
        "place_a": place_a,
        "place_b": place_a + "~",
        "relation_type": "sameAs",
        "source_category": "contributor",
        "source_id": "contributor:1",
        "asserted_at": asserted_at,
        "justification": None,
    }


if __name__ == "__main__":
    unittest.main()
