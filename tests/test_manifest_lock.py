"""Concurrency tests for staging_orchestrator manifest write locking.

Spawns N child processes, each calling ``update_namespace_stage_status``
on the same manifest at the same time, and asserts that every update
landed (no lost updates from race-on-tmp-file or race-on-read-modify-write).

This is the regression for the aat_enrich backfill incident on 2026-05-01:
15 concurrent Slurm tasks each tried to flip their own namespace's
stage status in the same shared manifest; without flock, _atomic_write_json
collided on ``<path>.tmp`` and most tasks crashed with
``FileNotFoundError: <path>.tmp -> <path>``.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from processing.staging_orchestrator import (
    create_run_manifest,
    update_namespace_stage_status,
)

NAMESPACES = [f"ns{i:02d}" for i in range(15)]


def _writer(args):
    """Module-level worker — multiprocessing requires a top-level callable."""
    manifest_path_str, ns = args
    update_namespace_stage_status(
        Path(manifest_path_str), ns, "aat_enrich", "completed",
        metrics={"docs_seen": 100, "ns": ns},
    )
    return ns


class TestManifestLock(unittest.TestCase):
    def test_concurrent_stage_status_writes_all_persist(self):
        with TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "run.json"
            create_run_manifest(manifest_path, "test-run", NAMESPACES)

            # 15 writers, each flipping a different namespace concurrently.
            # If the lock is missing, most processes will crash on
            # ``FileNotFoundError`` from the tmp-file race; some that don't
            # crash will silently overwrite each other's updates.
            with mp.get_context("fork").Pool(processes=len(NAMESPACES)) as pool:
                results = pool.map(
                    _writer,
                    [(str(manifest_path), ns) for ns in NAMESPACES],
                )

            # Every writer reports success.
            self.assertEqual(sorted(results), sorted(NAMESPACES))

            # Every namespace's stage now reads as completed in the final
            # manifest — proves no lost updates.
            final = json.loads(manifest_path.read_text())
            for ns in NAMESPACES:
                stage = final["namespaces"][ns]["stages"].get("aat_enrich")
                self.assertEqual(
                    stage, "completed",
                    f"namespace {ns!r} lost its aat_enrich update — "
                    f"stages: {final['namespaces'][ns].get('stages')}",
                )
                metrics = final["namespaces"][ns].get("stage_metrics", {}).get("aat_enrich")
                self.assertIsNotNone(
                    metrics,
                    f"namespace {ns!r} lost its stage_metrics",
                )
                self.assertEqual(metrics.get("ns"), ns)


if __name__ == "__main__":
    unittest.main()
