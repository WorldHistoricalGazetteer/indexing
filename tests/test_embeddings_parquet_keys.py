"""The embeddings Parquet must carry a doc_id on EVERY row.

The bug, 4 August 2026: ``update_es compute`` writes cache hits and cache
misses through the same Parquet writer and the same schema, but the hit path
built its dicts with the key ``toponym_id`` while the schema's field — and the
column the index stage joins on — is ``doc_id``.

``pa.Table.from_pylist(rows, schema=...)`` does **not** reject unknown keys. It
silently writes null for any field the dict does not supply. So all 67,878,740
cache hits landed with ``doc_id: NULL``, the index stage's join matched none of
them, and the toponyms index came out with 4,824,812 embeddings instead of
72,703,552 — exactly the cache-*miss* count.

Every stage reported success: compute logged "72,703,552 embeddings saved",
the bulk load logged "Success: 72,703,552, Errors: 0". Only counting documents
that actually carry an ``embedding`` field revealed it. That is the same shape
as the May rebuild's "embeddings missing for ~25% of toponyms".
"""

from __future__ import annotations

import unittest


class PyarrowSilentlyNullsUnknownKeys(unittest.TestCase):
    """Pin the library behaviour the bug depended on, so it stays visible."""

    def test_from_pylist_does_not_raise_on_a_wrong_key(self):
        import pyarrow as pa
        schema = pa.schema([("doc_id", pa.string()),
                            ("embedding", pa.list_(pa.int8()))])
        table = pa.Table.from_pylist(
            [{"toponym_id": "London@en", "embedding": [1, 2, 3]}], schema=schema)
        self.assertIsNone(table.to_pylist()[0]["doc_id"],
                          "if this ever raises instead, the guard below can go")


class BothWritePathsUseDocId(unittest.TestCase):

    def setUp(self):
        from pathlib import Path
        self.src = Path("phonetics/inference/update_es.py").read_text()

    def test_hit_path_writes_doc_id(self):
        # The cache-hit branch appends to hit_batch; it must use 'doc_id'.
        i = self.src.index("hit_batch.append({")
        window = self.src[i:i + 200]
        self.assertIn("'doc_id':", window,
                      "the cache-hit path must write the schema's field name")
        self.assertNotIn("'toponym_id':", window)

    def test_miss_path_writes_doc_id(self):
        i = self.src.index("out_batch.append({")
        window = self.src[i:i + 200]
        self.assertIn("'doc_id':", window)

    def test_schema_field_is_doc_id(self):
        self.assertIn("('doc_id', pa.string())", self.src)


class RoundTripThroughTheRealSchema(unittest.TestCase):
    """A behavioural check: both paths' dict shapes must survive the schema."""

    def _schema(self):
        import pyarrow as pa
        return pa.schema([("doc_id", pa.string()),
                          ("embedding", pa.list_(pa.int8()))])

    def test_hit_shaped_row_round_trips(self):
        import numpy as np
        import pyarrow as pa
        emb = np.array([1, -2, 3], dtype=np.int8)
        # Exactly what the cache-hit path builds, post-fix.
        row = {"doc_id": "London@en",
               "embedding": np.frombuffer(emb.tobytes(), dtype=np.int8).tolist()}
        out = pa.Table.from_pylist([row], schema=self._schema()).to_pylist()[0]
        self.assertEqual(out["doc_id"], "London@en")
        self.assertEqual(out["embedding"], [1, -2, 3],
                         "int8 sign must survive the tobytes/frombuffer trip")

    def test_null_doc_id_would_be_detectable(self):
        """A regression must be visible as null doc_ids, not silent success."""
        import pyarrow as pa
        t = pa.Table.from_pylist(
            [{"doc_id": "a@en", "embedding": [1]},
             {"toponym_id": "b@en", "embedding": [2]}], schema=self._schema())
        nulls = sum(1 for r in t.to_pylist() if r["doc_id"] is None)
        self.assertEqual(nulls, 1)


if __name__ == "__main__":
    unittest.main()
