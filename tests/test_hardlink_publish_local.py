"""Publishing the hard-link overlay must be atomic, and must not need ssh.

`ship_to_pitt` rsyncs over ssh to the Pitt VM. That cannot work from a CRC
compute node: the VM is firewalled from them on **both** 9200 and 22 (verified
6 Aug 2026 — `curl` exit 28, `ssh` connect timeout). It is also unnecessary:
the shared volume is mounted on the compute nodes *and* on the VM, and
`PITT_HARDLINK_DIR` (`/vast/ishi/hardlinks` since place#241) is where the gateway reads its
batch overlay. The "ship" is a rename within one filesystem — which is exactly
what the remote `mv` did.

What matters is that the atomicity guarantee survives the change: a partial
write must never be visible as the live overlay, because the gateway serves
clustering edges from it.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


class PublishLocalIsAtomic(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "build" / "hard_links_run.sqlite"
        self.src.parent.mkdir(parents=True)
        self.src.write_bytes(b"NEW-OVERLAY-CONTENT")
        self.target_dir = self.root / "hardlinks"
        self.target_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_publishes_to_the_gateway_filename(self):
        from clustering.sqlite_overlay import publish_local
        res = publish_local(local_db=self.src, target_dir=self.target_dir)
        target = self.target_dir / "hard_links.sqlite"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"NEW-OVERLAY-CONTENT")
        self.assertEqual(res["published_path"], str(target))

    def test_previous_overlay_is_kept_as_a_backup(self):
        """A bad publish must be reversible without re-harvesting."""
        from clustering.sqlite_overlay import publish_local
        target = self.target_dir / "hard_links.sqlite"
        target.write_bytes(b"OLD-LIVE-CONTENT")
        res = publish_local(local_db=self.src, target_dir=self.target_dir)
        backup = self.target_dir / "hard_links.sqlite.previous"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), b"OLD-LIVE-CONTENT")
        self.assertEqual(res["backup_path"], str(backup))

    def test_no_incoming_file_is_left_behind(self):
        """The hidden staging path must not survive a successful publish."""
        from clustering.sqlite_overlay import publish_local
        publish_local(local_db=self.src, target_dir=self.target_dir)
        leftovers = [p.name for p in self.target_dir.iterdir()
                     if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_source_build_survives_publication(self):
        """The run-scoped build is kept so a republish needs no re-harvest."""
        from clustering.sqlite_overlay import publish_local
        publish_local(local_db=self.src, target_dir=self.target_dir)
        self.assertTrue(self.src.exists())

    def test_missing_source_raises_rather_than_publishing_nothing(self):
        from clustering.sqlite_overlay import publish_local
        with self.assertRaises(FileNotFoundError):
            publish_local(local_db=self.root / "absent.sqlite",
                          target_dir=self.target_dir)
        self.assertFalse((self.target_dir / "hard_links.sqlite").exists())


class PruneLiveDeltaLocal(unittest.TestCase):

    def _make_db(self, path: Path, rows):
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE hard_link_assertions ("
                     "place_a TEXT, place_b TEXT, asserted_at TEXT)")
        conn.executemany("INSERT INTO hard_link_assertions VALUES (?,?,?)",
                         rows)
        conn.commit()
        conn.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "hard_links_live.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_at_or_before_the_cutoff_are_deleted(self):
        from clustering.sqlite_overlay import prune_live_delta_local
        self._make_db(self.db, [
            ("a:1", "b:1", "2026-08-06T00:00:00+00:00"),   # before
            ("a:2", "b:2", "2026-08-06T10:00:00+00:00"),   # at cutoff
        ])
        res = prune_live_delta_local(
            live_db_path=self.db, cutoff_iso="2026-08-06T10:00:00+00:00")
        self.assertEqual(res["deleted"], 2)

    def test_rows_asserted_during_the_build_survive(self):
        """The whole point of the cutoff: a row created mid-build is not
        guaranteed to be in the freshly published overlay, so pruning it
        would be a lost write."""
        from clustering.sqlite_overlay import prune_live_delta_local
        self._make_db(self.db, [
            ("a:1", "b:1", "2026-08-06T00:00:00+00:00"),
            ("a:2", "b:2", "2026-08-06T23:59:00+00:00"),   # during
            ("a:3", "b:3", None),                          # unknown
        ])
        prune_live_delta_local(live_db_path=self.db,
                               cutoff_iso="2026-08-06T10:00:00+00:00")
        conn = sqlite3.connect(str(self.db))
        left = {r[0] for r in conn.execute(
            "SELECT place_a FROM hard_link_assertions")}
        conn.close()
        self.assertEqual(left, {"a:2", "a:3"})

    def test_absent_live_delta_is_not_an_error(self):
        """Best-effort: a prune failure must never block the ship marker."""
        from clustering.sqlite_overlay import prune_live_delta_local
        res = prune_live_delta_local(
            live_db_path=Path(self.tmp.name) / "nope.sqlite",
            cutoff_iso="2026-08-06T10:00:00+00:00")
        self.assertEqual(res["deleted"], 0)
        self.assertIn("skipped", res)


if __name__ == "__main__":
    unittest.main()
