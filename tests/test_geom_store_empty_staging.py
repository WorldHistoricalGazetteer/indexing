"""A consolidation that reads nothing must not look like a successful merge.

31 August 2026: a mis-quoted `--staging-dir "\$STAGING"` in an sbatch heredoc
expanded to the literal string `$STAGING`. `consolidate_geom_store` glob'd a
directory that did not exist, found no entries, printed a note and returned 0 —
and the job carried on to the next stage having merged none of 9,849 staged whg
geometries. That is the same shape as the failure CLAUDE.md opens with: a job
reporting success is not evidence it read any geometry.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from processing import geom_store


class TestMissingStagingDirectory(unittest.TestCase):
    def test_nonexistent_staging_dir_raises(self):
        # Never a legitimate request — a typo or an unexpanded shell variable.
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError) as ctx:
                geom_store.consolidate_geom_store(
                    staging_dir=Path(td) / "$STAGING",
                    output_dir=Path(td) / "out",
                    merge_with_existing=True,
                )
            self.assertIn("does not exist", str(ctx.exception))

    def test_a_file_is_not_a_staging_dir(self):
        with tempfile.TemporaryDirectory() as td:
            bogus = Path(td) / "staging"
            bogus.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                geom_store.consolidate_geom_store(
                    staging_dir=bogus, output_dir=Path(td) / "out",
                    merge_with_existing=True,
                )

    def test_existing_but_empty_dir_still_returns_zero(self):
        # The empty case stays a return value rather than an exception: the
        # library is used by tests and orchestrators that may legitimately
        # consolidate nothing. It is the CLI that refuses to exit 0 on it.
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            written = geom_store.consolidate_geom_store(
                staging_dir=staging, output_dir=Path(td) / "out",
                merge_with_existing=True,
            )
            self.assertEqual(written, 0)

    def test_a_real_staging_dir_still_consolidates(self):
        # Guard against the guard: the happy path must be untouched.
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            with geom_store.GeomStoreWriter(staging, "ns") as w:
                w.write("ns:1_0", "", {"type": "LineString",
                                        "coordinates": [[0, 0], [1, 1]]})
            self.assertTrue((staging / "ns.index.json").exists())
            self.assertEqual(len(json.loads((staging / "ns.index.json").read_text())), 1)
            written = geom_store.consolidate_geom_store(
                staging_dir=staging, output_dir=Path(td) / "out",
                merge_with_existing=True, delete_staging=False,
            )
            self.assertEqual(written, 1)


if __name__ == "__main__":
    unittest.main()
