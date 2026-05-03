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
        # Two-part regression for the 2026-05-02 quantisation bugs:
        #   1) WRITE side: ``bytes(int8_arr.tolist())`` rejects negative
        #      values → fixed with ``int8_arr.tobytes()``.
        #   2) READ side: ``list(emb_bytes)`` yields unsigned 0..255 which
        #      the int8 Parquet schema rejects (e.g. 245 → "Value 245 too
        #      large to fit in C integer type") → fixed with
        #      ``np.frombuffer(emb_bytes, dtype=np.int8).tolist()``.
        import numpy as np
        from phonetics.inference.update_es import (
            quantize_embeddings_to_bytes,
            dequantize_embeddings_from_bytes,
        )
        # Realistic L2-normalised float32 with both signs.
        emb = np.array([[0.5, -0.5, 0.0, 1.0, -1.0]], dtype=np.float32)
        quantised = quantize_embeddings_to_bytes(emb)        # int8
        # WRITE: the buggy idiom raises; verify so we catch any revert.
        with self.assertRaises(ValueError):
            bytes(quantised[0].tolist())
        # WRITE: the correct idiom preserves bytes verbatim.
        emb_bytes = quantised[0].tobytes()
        self.assertEqual(len(emb_bytes), 5)

        with cache_connection(self.db) as conn:
            insert_many(
                conn, [("Negativo@xx", emb_bytes)],
                model_version=7, checkpoint_hash="cafebabe",
            )
            hits = load_hits(conn, model_version=7, checkpoint_hash="cafebabe")
            self.assertEqual(hits["Negativo@xx"], emb_bytes)

            # READ: list(bytes) gives unsigned ints that misalign for int8.
            unsigned = list(hits["Negativo@xx"])
            self.assertTrue(any(v > 127 for v in unsigned),
                            "test sample should include a negative→245-ish value")
            # READ (correct): frombuffer with int8 dtype gives signed ints
            # within [-128, 127] suitable for the Parquet int8 column.
            signed = np.frombuffer(hits["Negativo@xx"], dtype=np.int8).tolist()
            self.assertTrue(all(-128 <= v <= 127 for v in signed))
            self.assertEqual(len(signed), len(unsigned))

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


class TestReadOnlyCache(unittest.TestCase):
    """Regression for the 2026-05-03 sharded GPU array failure: 4 shards
    racing to ``open_cache`` in default RW mode all tried to acquire the
    same exclusive DuckDB file lock; only one won, three failed with
    ``Conflicting lock is held in PID -50``. The fix is opening the cache
    ``read_only=True`` in sharded mode."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "cache.duckdb"

    def tearDown(self):
        self._tmp.cleanup()

    def test_readonly_on_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            open_cache(self.db, read_only=True)

    def test_concurrent_readers_dont_lock_contend(self):
        # Materialise the file with a single writer first.
        w = open_cache(self.db)
        insert_many(w, [("x@en", b"\x01" * 128)],
                    model_version=7, checkpoint_hash="aaaa")
        w.close()
        # Now three concurrent readers must all succeed.
        readers = [open_cache(self.db, read_only=True) for _ in range(3)]
        try:
            counts = [r.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
                      for r in readers]
            self.assertEqual(counts, [1, 1, 1])
        finally:
            for r in readers:
                r.close()


if __name__ == "__main__":
    unittest.main()
