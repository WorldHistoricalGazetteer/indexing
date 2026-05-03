"""Tests for the multi-GPU sharding additions to update_es.py compute and
the companion shard-merge utility.

The compute path itself needs a real model checkpoint + vocab to invoke
end-to-end, so the tests here cover the *deterministic* pieces:

1. Shard partitioning over a synthetic DuckDB matches what the compute
   query would produce. Across all shards every row appears exactly
   once.
2. The merge utility round-trips multi-shard parquets back into a single
   file with the union of rows and identical schema.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from phonetics.inference.merge_shards import merge


class TestShardPartitioning(unittest.TestCase):
    """The DuckDB hash predicate ``(hash(toponym_id) %% N) = i`` must
    partition the input into exactly N disjoint sets whose union is the
    whole input."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "tops.duckdb"
        self.conn = duckdb.connect(str(self.db))
        self.conn.execute(
            "CREATE TABLE toponyms ("
            "  toponym_id VARCHAR PRIMARY KEY, name VARCHAR, "
            "  lang VARCHAR, script VARCHAR)"
        )
        # 5,000 synthetic toponyms across many languages — enough sample
        # for the hash distribution to be roughly uniform.
        rows = [
            (f"name{i:05d}@{lang}", f"name{i:05d}", lang, "LATIN")
            for i in range(5000)
            for lang in (i % 7,)  # arbitrary lang variation
        ]
        # rows is a list of tuples; one toponym per i
        self.conn.executemany(
            "INSERT INTO toponyms VALUES (?, ?, ?, ?)",
            [(f"name{i:05d}@{i % 7}", f"name{i:05d}", str(i % 7), "LATIN")
             for i in range(5000)],
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _shard(self, n: int, i: int) -> set[str]:
        rows = self.conn.execute(
            f"SELECT toponym_id FROM toponyms "
            f"WHERE (hash(toponym_id) % {n}) = {i}"
        ).fetchall()
        return {r[0] for r in rows}

    def test_partitions_are_disjoint_and_complete(self):
        all_ids = {
            r[0] for r in self.conn.execute("SELECT toponym_id FROM toponyms").fetchall()
        }
        for num_shards in (2, 4, 8):
            shards = [self._shard(num_shards, i) for i in range(num_shards)]
            # Disjoint
            for i in range(num_shards):
                for j in range(i + 1, num_shards):
                    self.assertEqual(
                        shards[i] & shards[j], set(),
                        f"shards {i} and {j} overlap (n={num_shards})",
                    )
            # Complete
            self.assertEqual(
                set().union(*shards), all_ids,
                f"shards don't cover all toponyms (n={num_shards})",
            )

    def test_distribution_is_roughly_uniform(self):
        # 5000 / 4 = 1250 per shard; allow ±15% slack for hash variance.
        sizes = [len(self._shard(4, i)) for i in range(4)]
        for s in sizes:
            self.assertGreater(s, 1062)
            self.assertLess(s, 1438)
        self.assertEqual(sum(sizes), 5000)


class TestMergeShards(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.canonical = Path(self.tmp.name) / "embeddings.parquet"
        self.schema = pa.schema([
            ('doc_id', pa.string()),
            ('embedding', pa.list_(pa.int8())),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def _write_shard(self, shard_id: int, doc_ids: list[str]) -> None:
        path = self.canonical.with_suffix(f".shard_{shard_id}{self.canonical.suffix}")
        rows = [{"doc_id": d, "embedding": [0, 1, -1] * 42 + [0, 1]}  # 128-d
                for d in doc_ids]
        pq.write_table(pa.Table.from_pylist(rows, schema=self.schema), path)

    def test_merge_three_shards(self):
        self._write_shard(0, [f"a{i}@en" for i in range(10)])
        self._write_shard(1, [f"b{i}@en" for i in range(10)])
        self._write_shard(2, [f"c{i}@en" for i in range(10)])

        summary = merge(output_file=self.canonical, num_shards=3)
        self.assertEqual(summary["total_rows"], 30)
        self.assertTrue(self.canonical.exists())

        merged = pq.read_table(self.canonical)
        self.assertEqual(merged.num_rows, 30)
        self.assertEqual(merged.schema, self.schema)
        ids = set(merged.column("doc_id").to_pylist())
        for prefix in ("a", "b", "c"):
            self.assertTrue(any(i.startswith(prefix) for i in ids))

    def test_missing_shard_raises(self):
        self._write_shard(0, ["x@en"])
        # shard 1 not written
        with self.assertRaises(FileNotFoundError):
            merge(output_file=self.canonical, num_shards=2)

    def test_refuses_to_overwrite(self):
        self._write_shard(0, ["x@en"])
        self._write_shard(1, ["y@en"])
        merge(output_file=self.canonical, num_shards=2)
        with self.assertRaises(FileExistsError):
            merge(output_file=self.canonical, num_shards=2)

    def test_delete_shards(self):
        self._write_shard(0, ["x@en"])
        self._write_shard(1, ["y@en"])
        merge(output_file=self.canonical, num_shards=2, delete_shards=True)
        for i in range(2):
            shard_path = self.canonical.with_suffix(f".shard_{i}{self.canonical.suffix}")
            self.assertFalse(shard_path.exists())


if __name__ == "__main__":
    unittest.main()
