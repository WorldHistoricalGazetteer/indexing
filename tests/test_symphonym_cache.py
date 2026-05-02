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

    def test_int8_quantised_embedding_roundtrip(self):
        # Regression for the 2026-05-02 ValueError: bytes must be in
        # range(0, 256) — the compute path was doing
        # ``bytes(int8_arr.tolist())`` which rejects negative values. The
        # fix is ``int8_arr.tobytes()``. This test guards the cache
        # contract: insert raw int8 bytes, get them back unchanged.
        import numpy as np
        from phonetics.inference.update_es import (
            quantize_embeddings_to_bytes,
            dequantize_embeddings_from_bytes,
        )
        # Realistic L2-normalised float32 with both signs.
        emb = np.array([[0.5, -0.5, 0.0, 1.0, -1.0]], dtype=np.float32)
        quantised = quantize_embeddings_to_bytes(emb)        # int8
        # The buggy idiom raises; verify it's still buggy so we know our
        # test catches it if someone reverts the fix.
        with self.assertRaises(ValueError):
            bytes(quantised[0].tolist())
        # The correct idiom (what the fix uses) preserves bytes verbatim.
        emb_bytes = quantised[0].tobytes()
        self.assertEqual(len(emb_bytes), 5)

        with cache_connection(self.db) as conn:
            insert_many(
                conn, [("Negativo@xx", emb_bytes)],
                model_version=7, checkpoint_hash="cafebabe",
            )
            hits = load_hits(conn, model_version=7, checkpoint_hash="cafebabe")
            self.assertEqual(hits["Negativo@xx"], emb_bytes)
            # Round-trip through dequantise still produces the original.
            recovered = np.frombuffer(hits["Negativo@xx"], dtype=np.int8)
            float_back = dequantize_embeddings_from_bytes(recovered)
            np.testing.assert_allclose(float_back, emb[0], atol=1.0 / 127)

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
