"""Unit tests for the SQLite geom-store index (place#165).

``GeomStoreReader`` used to ``json.load()`` a 1.02 GB ``index.json`` into
~5.4 GB of RSS, which is why ``containment=exact`` never actually switched
itself on in production. These tests pin the replacement:

* the reader prefers ``index.sqlite`` and falls back to ``index.json``;
* both backends resolve identical keys to **byte-identical WKB** — the
  property the one-off backfill's ``verify`` mode asserts at scale;
* ``__contains__`` / ``__len__`` work on both backends (the original issue
  spec listed only ``__init__`` and ``_read_wkb``, and ``__len__`` on a
  ``WITHOUT ROWID`` table is a full scan unless the count is cached);
* reads are thread-safe — ``os.pread`` replaced a ``seek()``-then-``read()``
  on a shared handle that would return another thread's bytes;
* a connection is not reused across ``fork()``;
* a non-shard filename is rejected loudly rather than silently mangled.
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from processing.geom_store import (
    INDEX_JSON_NAME,
    INDEX_SQLITE_NAME,
    GeomStoreReader,
    shard_filename,
    shard_num_from_filename,
    write_sqlite_index,
)


def _build_store(store_dir: Path, n: int = 200) -> dict[str, bytes]:
    """Write two shards of synthetic payloads + an ``index.json``.

    Payloads are distinct per key and of varying length, so an off-by-one in
    offset or length shows up as a byte mismatch rather than parsing anyway.
    Returns ``{key: payload}``.
    """
    payloads: dict[str, bytes] = {}
    index: dict[str, dict] = {}
    for shard in (1, 2):
        name = shard_filename(shard)
        offset = 0
        with open(store_dir / name, "wb") as fh:
            for i in range(n // 2):
                key = f"osm:r{shard}{i:04d}_0"
                blob = (f"<{key}>".encode() * (1 + i % 7))
                fh.write(blob)
                index[key] = {"file": name, "offset": offset, "length": len(blob)}
                payloads[key] = blob
                offset += len(blob)
    with open(store_dir / INDEX_JSON_NAME, "w") as f:
        json.dump(index, f)
    return payloads


def _entries(store_dir: Path):
    with open(store_dir / INDEX_JSON_NAME) as f:
        index = json.load(f)
    for key, e in index.items():
        yield key, e["file"], e["offset"], e["length"]


class ShardFilenameTests(unittest.TestCase):
    def test_roundtrip(self):
        for n in (1, 42, 1234, 99999):
            self.assertEqual(shard_num_from_filename(shard_filename(n)), n)

    def test_zero_padding_matches_consolidate(self):
        self.assertEqual(shard_filename(7), "geom_shard_0007.bin")

    def test_non_shard_filename_rejected_loudly(self):
        # A silently-coerced filename would produce an index that resolves to
        # the wrong bytes, so this must raise rather than guess.
        for bad in ("osm.bin", "geom_shard_.bin", "geom_shard_0001.bin.tmp", ""):
            with self.assertRaises(ValueError):
                shard_num_from_filename(bad)


class BackendParityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = Path(self._tmp.name)
        self.payloads = _build_store(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_json_backend_used_when_no_sqlite(self):
        reader = GeomStoreReader(self.store)
        self.addCleanup(reader.close)
        self.assertEqual(reader.backend, "json")
        self.assertEqual(len(reader), len(self.payloads))

    def test_sqlite_preferred_when_present(self):
        write_sqlite_index(_entries(self.store), self.store)
        reader = GeomStoreReader(self.store)
        self.addCleanup(reader.close)
        self.assertEqual(reader.backend, "sqlite")

    def test_prefer_sqlite_false_forces_json(self):
        write_sqlite_index(_entries(self.store), self.store)
        reader = GeomStoreReader(self.store, prefer_sqlite=False)
        self.addCleanup(reader.close)
        self.assertEqual(reader.backend, "json")

    def test_both_backends_return_byte_identical_wkb(self):
        write_sqlite_index(_entries(self.store), self.store)
        sq = GeomStoreReader(self.store, prefer_sqlite=True)
        js = GeomStoreReader(self.store, prefer_sqlite=False)
        self.addCleanup(sq.close)
        self.addCleanup(js.close)
        for key, expected in self.payloads.items():
            self.assertEqual(sq._cached_wkb(key), expected, key)
            self.assertEqual(js._cached_wkb(key), expected, key)

    def test_missing_key_returns_none_on_both_backends(self):
        write_sqlite_index(_entries(self.store), self.store)
        sq = GeomStoreReader(self.store, prefer_sqlite=True)
        js = GeomStoreReader(self.store, prefer_sqlite=False)
        self.addCleanup(sq.close)
        self.addCleanup(js.close)
        self.assertIsNone(sq._cached_wkb("osm:nope_0"))
        self.assertIsNone(js._cached_wkb("osm:nope_0"))
        self.assertIsNone(sq.get("osm:nope_0"))

    def test_contains_and_len_on_sqlite(self):
        write_sqlite_index(_entries(self.store), self.store)
        reader = GeomStoreReader(self.store)
        self.addCleanup(reader.close)
        some_key = next(iter(self.payloads))
        self.assertIn(some_key, reader)
        self.assertNotIn("osm:nope_0", reader)
        self.assertEqual(len(reader), len(self.payloads))

    def test_len_falls_back_to_count_without_meta_row(self):
        write_sqlite_index(_entries(self.store), self.store)
        conn = sqlite3.connect(str(self.store / INDEX_SQLITE_NAME))
        conn.execute("DELETE FROM meta WHERE k='count'")
        conn.commit()
        conn.close()
        reader = GeomStoreReader(self.store)
        self.addCleanup(reader.close)
        self.assertEqual(len(reader), len(self.payloads))

    def test_missing_both_indexes_raises(self):
        (self.store / INDEX_JSON_NAME).unlink()
        with self.assertRaises(FileNotFoundError):
            GeomStoreReader(self.store)

    def test_write_is_atomic_no_tmp_or_journal_left(self):
        write_sqlite_index(_entries(self.store), self.store)
        leftovers = [
            p.name for p in self.store.iterdir()
            if p.name.startswith(INDEX_SQLITE_NAME) and p.name != INDEX_SQLITE_NAME
        ]
        # A lingering -wal/-shm would also break the reader's immutable=1 open.
        self.assertEqual(leftovers, [], f"unexpected leftovers: {leftovers}")

    def test_rebuild_over_existing_index_replaces_not_appends(self):
        write_sqlite_index(_entries(self.store), self.store)
        first = len(GeomStoreReader(self.store))
        write_sqlite_index(_entries(self.store), self.store)
        self.assertEqual(len(GeomStoreReader(self.store)), first)


class ConcurrencyTests(unittest.TestCase):
    """The reader is shared across threads and inherited across fork()."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = Path(self._tmp.name)
        self.payloads = _build_store(self.store, n=200)
        write_sqlite_index(_entries(self.store), self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_concurrent_reads_return_correct_bytes(self):
        # With the old seek()+read() on a shared handle, interleaved threads
        # could read from another thread's offset. lru_maxsize=0 defeats the
        # cache so every call actually hits the shard file.
        reader = GeomStoreReader(self.store, lru_maxsize=0)
        self.addCleanup(reader.close)
        keys = list(self.payloads) * 20

        def _check(key):
            return key, reader._read_wkb(key)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for key, got in pool.map(_check, keys):
                self.assertEqual(got, self.payloads[key], key)

    @unittest.skipUnless(hasattr(os, "fork"), "fork() unavailable")
    def test_connection_not_reused_across_fork(self):
        reader = GeomStoreReader(self.store)
        self.addCleanup(reader.close)
        key = next(iter(self.payloads))
        parent_conn = reader._conn()  # establish a pre-fork connection

        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            try:
                os.close(r)
                ok = (
                    reader._read_wkb(key) == self.payloads[key]
                    and reader._conn() is not parent_conn
                )
                os.write(w, b"1" if ok else b"0")
                os.close(w)
            finally:
                os._exit(0)
        os.close(w)
        result = os.read(r, 1)
        os.close(r)
        os.waitpid(pid, 0)
        self.assertEqual(result, b"1", "child reused the parent's connection")


class ConsolidateWritesSqliteTests(unittest.TestCase):
    """consolidate_geom_store must emit index.sqlite alongside index.json."""

    def test_consolidation_emits_both_indexes(self):
        from processing.geom_store import GeomStoreWriter, consolidate_geom_store

        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            staging, out = tmp / "staging", tmp / "out"
            staging.mkdir()
            square = {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            }
            with GeomStoreWriter(staging, namespace="un") as w:
                if not w.write("un:fra_0", "8a", square):
                    self.skipTest("shapely unavailable")
                w.write("un:esp_0", "8b", square)
            consolidate_geom_store(
                staging_dir=staging, output_dir=out, delete_staging=False,
                merge_with_existing=False,
            )
            self.assertTrue((out / INDEX_JSON_NAME).exists())
            self.assertTrue((out / INDEX_SQLITE_NAME).exists())

            reader = GeomStoreReader(out)
            self.addCleanup(reader.close)
            self.assertEqual(reader.backend, "sqlite")
            self.assertEqual(len(reader), 2)
            self.assertIsNotNone(reader.get("un:fra_0"))


if __name__ == "__main__":
    unittest.main()
