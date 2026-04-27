"""Unit tests for phonetics/inference/symphonym_cache.py (Batch 9 cache)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from phonetics.inference.symphonym_cache import (
    cache_connection,
    cache_size,
    cache_size_for,
    compute_checkpoint_hash,
    insert_many,
    load_hits,
    open_cache,
)


class TestCheckpointHash(unittest.TestCase):

    def test_hash_is_deterministic_and_changes_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ckpt.pt"
            p.write_bytes(b"version-A bytes")
            h1 = compute_checkpoint_hash(p)
            h2 = compute_checkpoint_hash(p)
            self.assertEqual(h1, h2)

            p.write_bytes(b"version-B bytes")
            h3 = compute_checkpoint_hash(p)
            self.assertNotEqual(h1, h3)


class TestCacheRoundTrip(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "cache.duckdb"

    def tearDown(self):
        self._tmp.cleanup()

    def test_insert_then_lookup(self):
        with cache_connection(self.db) as conn:
            n = insert_many(
                conn,
                [
                    ("London@en", b"\x01" * 128),
                    ("Paris@fr", b"\x02" * 128),
                ],
                model_version=7,
                checkpoint_hash="aaaaaaaa",
            )
            self.assertEqual(n, 2)
            self.assertEqual(cache_size(conn), 2)
            hits = load_hits(conn, model_version=7, checkpoint_hash="aaaaaaaa")
            self.assertEqual(set(hits), {"London@en", "Paris@fr"})
            self.assertEqual(hits["London@en"], b"\x01" * 128)

    def test_idempotent_reinsert(self):
        with cache_connection(self.db) as conn:
            insert_many(
                conn, [("Lyon@fr", b"\x03" * 128)],
                model_version=7, checkpoint_hash="aaaaaaaa",
            )
            n = insert_many(
                conn, [("Lyon@fr", b"\x99" * 128)],   # same key, different bytes
                model_version=7, checkpoint_hash="aaaaaaaa",
            )
            self.assertEqual(n, 0)  # PK conflict → IGNORE
            hits = load_hits(conn, model_version=7, checkpoint_hash="aaaaaaaa")
            # The first write wins.
            self.assertEqual(hits["Lyon@fr"], b"\x03" * 128)

    def test_version_bump_invalidates(self):
        with cache_connection(self.db) as conn:
            insert_many(
                conn, [("X@en", b"\x04" * 128)],
                model_version=7, checkpoint_hash="aaaaaaaa",
            )
            # Bump model_version → no hits.
            self.assertEqual(
                load_hits(conn, model_version=8, checkpoint_hash="aaaaaaaa"),
                {},
            )
            # Bump checkpoint_hash → no hits.
            self.assertEqual(
                load_hits(conn, model_version=7, checkpoint_hash="bbbbbbbb"),
                {},
            )
            # Original key still works.
            self.assertEqual(
                set(load_hits(conn, model_version=7, checkpoint_hash="aaaaaaaa")),
                {"X@en"},
            )

    def test_cache_size_for_filters_by_version_hash(self):
        with cache_connection(self.db) as conn:
            insert_many(conn, [("a@en", b"\x01" * 4)],
                        model_version=7, checkpoint_hash="aaaa")
            insert_many(conn, [("b@en", b"\x02" * 4)],
                        model_version=7, checkpoint_hash="bbbb")
            insert_many(conn, [("c@en", b"\x03" * 4)],
                        model_version=8, checkpoint_hash="aaaa")
            self.assertEqual(cache_size(conn), 3)
            self.assertEqual(
                cache_size_for(conn, model_version=7, checkpoint_hash="aaaa"), 1,
            )
            self.assertEqual(
                cache_size_for(conn, model_version=7, checkpoint_hash="bbbb"), 1,
            )
            self.assertEqual(
                cache_size_for(conn, model_version=8, checkpoint_hash="aaaa"), 1,
            )


if __name__ == "__main__":
    unittest.main()
