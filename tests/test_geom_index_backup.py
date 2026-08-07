"""The geom-store index is backed up to separate storage after consolidation.

The index is the only key → (shard, offset, length) map for ~61 GB of keyless
WKB. Losing it strands every shard, and neither ES nor the staged parquet holds
coordinates to rebuild from — only a ``geom_ref``. So consolidation takes a
timestamped copy once all namespaces are packed. These tests pin the properties
that make that copy worth having.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from processing import geom_store


def _make_index_sqlite(store: Path, keys=("ns:a_0", "ns:b_0")) -> None:
    """Write a minimal but real index.sqlite into *store*."""
    store.mkdir(parents=True, exist_ok=True)
    entries = ((k, "geom_shard_0001.bin", i * 10, 10) for i, k in enumerate(keys))
    geom_store.write_sqlite_index(entries, store)


class TestGeomIndexBackup(unittest.TestCase):
    def test_backup_is_a_faithful_copy(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            store, backups = tmp / "geom", tmp / "backups"
            _make_index_sqlite(store)

            dest = geom_store.backup_sqlite_index(store, backup_dir=backups)

            self.assertIsNotNone(dest)
            self.assertTrue(dest.exists())
            self.assertEqual(dest.read_bytes(),
                             (store / geom_store.INDEX_SQLITE_NAME).read_bytes())
            # And it is a usable index, not just matching bytes.
            con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM geom").fetchone()[0], 2)
            con.close()

    def test_backups_are_timestamped_not_overwritten(self):
        # A bad consolidation must not be able to clobber the last good copy.
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            store, backups = tmp / "geom", tmp / "backups"
            _make_index_sqlite(store, keys=("ns:a_0", "ns:b_0"))
            first = geom_store.backup_sqlite_index(store, backup_dir=backups)
            _make_index_sqlite(store, keys=("ns:c_0",))
            second = geom_store.backup_sqlite_index(store, backup_dir=backups)

            self.assertNotEqual(first, second)
            self.assertTrue(first.exists(), "earlier backup was overwritten")
            con = sqlite3.connect(f"file:{first}?mode=ro", uri=True)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM geom").fetchone()[0], 2)
            con.close()

    def test_retention_prunes_oldest_first(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            store, backups = tmp / "geom", tmp / "backups"
            _make_index_sqlite(store)
            made = []
            for i in range(5):
                # Names carry a 1-second-resolution stamp; write directly so the
                # test doesn't depend on wall-clock spacing.
                dest = backups / f"index-2026010{i}T000000Z.sqlite"
                backups.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"x")
                made.append(dest)
            geom_store.backup_sqlite_index(store, backup_dir=backups, keep=3)

            remaining = sorted(p.name for p in backups.glob("index-*.sqlite"))
            self.assertEqual(len(remaining), 3)
            # The two oldest hand-made ones went; the newest survivors remain.
            self.assertNotIn(made[0].name, remaining)
            self.assertNotIn(made[1].name, remaining)

    def test_missing_index_is_reported_not_raised(self):
        # A consolidation that failed to write the index must not also die here.
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            store, backups = tmp / "geom", tmp / "backups"
            store.mkdir()
            self.assertIsNone(geom_store.backup_sqlite_index(store, backup_dir=backups))

    def test_unwritable_destination_is_non_fatal(self):
        # Losing the backup is bad; losing hours of packing to an exception is
        # worse. Failure is loud (printed) but returns None.
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            store = tmp / "geom"
            _make_index_sqlite(store)
            blocked = tmp / "blocked"
            blocked.write_text("i am a file, not a directory")
            self.assertIsNone(
                geom_store.backup_sqlite_index(store, backup_dir=blocked / "sub"))

    def test_consolidation_writes_a_backup(self):
        # End-to-end: the backup happens as part of consolidate_geom_store,
        # after all staging namespaces have been packed.
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            staging, store, backups = tmp / "staging", tmp / "geom", tmp / "backups"
            staging.mkdir()
            with geom_store.GeomStoreWriter(staging, namespace="ns") as w:
                w.write("ns:a_0", "8a", {"type": "Point", "coordinates": [1.0, 2.0]})
                w.write("ns:b_0", "8b", {"type": "Point", "coordinates": [3.0, 4.0]})

            import os
            os.environ["GEOM_STORE_BACKUP_DIR"] = str(backups)
            try:
                import importlib

                from processing import settings
                importlib.reload(settings)
                written = geom_store.consolidate_geom_store(
                    staging_dir=staging, output_dir=store)
            finally:
                os.environ.pop("GEOM_STORE_BACKUP_DIR", None)

            self.assertEqual(written, 2)
            copies = list(backups.glob("index-*.sqlite"))
            self.assertEqual(len(copies), 1, f"expected one backup, got {copies}")
            con = sqlite3.connect(f"file:{copies[0]}?mode=ro", uri=True)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM geom").fetchone()[0], 2)
            con.close()


if __name__ == "__main__":
    unittest.main()
