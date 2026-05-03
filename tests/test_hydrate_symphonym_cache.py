"""Unit tests for processing/hydrate_symphonym_cache.

The full hydration round-trip (ES → cache) needs a live ES; here we cover
the pure conversion piece — ES returns ``element_type=byte`` arrays as
signed-int lists in JSON, and they must round-trip through bytes back to
identical int8 values matching how the compute path stores them.
"""

from __future__ import annotations

import unittest

import numpy as np

from processing.hydrate_symphonym_cache import _embedding_to_bytes


class TestEmbeddingToBytes(unittest.TestCase):
    def test_roundtrip_signed_values(self):
        # Realistic mix of signs — what ES returns for a dense_vector
        # element_type=byte field.
        original = [0, 1, -1, 127, -128, 64, -64]
        emb_bytes = _embedding_to_bytes(original)
        self.assertEqual(len(emb_bytes), len(original))
        # Round-trip: bytes back to int8 list matches.
        recovered = np.frombuffer(emb_bytes, dtype=np.int8).tolist()
        self.assertEqual(recovered, original)

    def test_matches_compute_path_quantisation(self):
        # The compute path does:
        #   quantised = np.round(emb * 127.0).astype(np.int8)
        #   emb_bytes = quantised.tobytes()
        # Hydration must produce IDENTICAL bytes for an embedding ES has
        # already stored, so the cache key+value match what compute would
        # write.
        from phonetics.inference.update_es import quantize_embeddings_to_bytes

        emb = np.array([[0.5, -0.5, 0.0, 1.0, -1.0]], dtype=np.float32)
        compute_bytes = quantize_embeddings_to_bytes(emb)[0].tobytes()

        # ES would expose those quantised values as a list of signed ints.
        as_list = np.frombuffer(compute_bytes, dtype=np.int8).tolist()
        hydrated_bytes = _embedding_to_bytes(as_list)

        self.assertEqual(hydrated_bytes, compute_bytes)

    def test_full_128d_vector(self):
        # Production embeddings are 128-d. Make sure the conversion holds
        # at the real shape with values across the int8 range.
        rng = np.random.default_rng(seed=42)
        original = rng.integers(-128, 128, size=128, dtype=np.int8).tolist()
        emb_bytes = _embedding_to_bytes(original)
        self.assertEqual(len(emb_bytes), 128)
        recovered = np.frombuffer(emb_bytes, dtype=np.int8).tolist()
        self.assertEqual(recovered, original)


if __name__ == "__main__":
    unittest.main()
