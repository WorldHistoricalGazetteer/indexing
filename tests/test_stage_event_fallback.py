"""Tests for the per-namespace events.jsonl fallback used when run-scoped
manifests fall stale across retries (different ``run_id``).

Background: a Slurm task always appends to
``<staged>/<namespace>/<stage>/events.jsonl``, but only updates
``runs/<run_id>.json`` if that manifest file already exists. A retry under a
fresh ``run_id`` therefore writes events but no manifest, leaving the
original manifest stuck on ``status="running"``. Eligibility checks must
fall back to the event log so they self-heal across runs.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from processing import stage_writers
from processing import submit_tiles_slurm


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


class TestReadLastStageStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.staged = Path(self.tmp.name)
        # Patch STAGED_BASE_DIR for the templated _stage_dir resolution.
        self._patch_settings = mock.patch.object(
            stage_writers, "STAGED_BASE_DIR", str(self.staged)
        )
        self._patch_settings.start()

    def tearDown(self):
        self._patch_settings.stop()
        self.tmp.cleanup()

    def _events_path(self, namespace: str, stage: str) -> Path:
        return self.staged / namespace / stage / "events.jsonl"

    def test_no_log_returns_none(self):
        self.assertIsNone(stage_writers.read_last_stage_status("gn", "aat_enrich"))

    def test_returns_last_completed(self):
        _write_events(self._events_path("gn", "aat_enrich"), [
            {"stage": "aat_enrich", "status": "running",   "run_id": "first"},
            {"stage": "aat_enrich", "status": "completed", "run_id": "first"},
        ])
        self.assertEqual(
            stage_writers.read_last_stage_status("gn", "aat_enrich"),
            "completed",
        )

    def test_retry_overrides_earlier_failed(self):
        # Original run failed; retry under a different run_id completed.
        _write_events(self._events_path("gn", "aat_enrich"), [
            {"stage": "aat_enrich", "status": "running",   "run_id": "first"},
            {"stage": "aat_enrich", "status": "failed",    "run_id": "first"},
            {"stage": "aat_enrich", "status": "running",   "run_id": "retry"},
            {"stage": "aat_enrich", "status": "completed", "run_id": "retry"},
        ])
        self.assertEqual(
            stage_writers.read_last_stage_status("gn", "aat_enrich"),
            "completed",
        )

    def test_ignores_other_stages(self):
        _write_events(self._events_path("gn", "aat_enrich"), [
            {"stage": "extract",    "status": "completed"},
            {"stage": "aat_enrich", "status": "running"},
        ])
        self.assertEqual(
            stage_writers.read_last_stage_status("gn", "aat_enrich"),
            "running",
        )

    def test_skips_malformed_lines(self):
        path = self._events_path("gn", "aat_enrich")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            f.write("not json\n")
            f.write(json.dumps({"stage": "aat_enrich", "status": "completed"}) + "\n")
            f.write("\n")
        self.assertEqual(
            stage_writers.read_last_stage_status("gn", "aat_enrich"),
            "completed",
        )


class TestStageCompletedFallback(unittest.TestCase):
    """The submit_tiles_slurm gate uses the event-log fallback when the
    manifest disagrees with the per-namespace event log."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.staged = Path(self.tmp.name)
        self._patch_settings = mock.patch.object(
            stage_writers, "STAGED_BASE_DIR", str(self.staged)
        )
        self._patch_settings.start()

    def tearDown(self):
        self._patch_settings.stop()
        self.tmp.cleanup()

    def test_manifest_completed_short_circuits(self):
        manifest = {"namespaces": {"gn": {"stages": {"aat_enrich": "completed"}}}}
        # No event log on disk — manifest hit alone is sufficient.
        self.assertTrue(submit_tiles_slurm._stage_completed(manifest, "gn", "aat_enrich"))

    def test_manifest_stale_event_log_completed(self):
        # Manifest stuck on running (cross-run drift); event log says done.
        events_path = self.staged / "gn" / "aat_enrich" / "events.jsonl"
        _write_events(events_path, [
            {"stage": "aat_enrich", "status": "running"},
            {"stage": "aat_enrich", "status": "completed"},
        ])
        manifest = {"namespaces": {"gn": {"stages": {"aat_enrich": "running"}}}}
        self.assertTrue(submit_tiles_slurm._stage_completed(manifest, "gn", "aat_enrich"))

    def test_event_log_failed_still_blocks(self):
        events_path = self.staged / "gn" / "aat_enrich" / "events.jsonl"
        _write_events(events_path, [
            {"stage": "aat_enrich", "status": "failed"},
        ])
        manifest = {"namespaces": {"gn": {"stages": {"aat_enrich": "pending"}}}}
        self.assertFalse(submit_tiles_slurm._stage_completed(manifest, "gn", "aat_enrich"))

    def test_no_manifest_no_log(self):
        self.assertFalse(
            submit_tiles_slurm._stage_completed({"namespaces": {}}, "missing", "aat_enrich")
        )


class TestBarrierFallback(unittest.TestCase):
    """check_global_barrier should pass when the manifest is stale but the
    per-namespace event log shows completion."""

    def setUp(self):
        from processing import staging_orchestrator
        self.staging_orchestrator = staging_orchestrator
        self.tmp = TemporaryDirectory()
        self.staged = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _events(self, ns: str, stage: str, statuses: list[str]) -> None:
        path = self.staged / ns / stage / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for s in statuses:
                f.write(json.dumps({"stage": stage, "status": s}) + "\n")

    def test_stale_manifest_passes_via_event_log(self):
        # Manifest stuck on running for ccode_merge; event log says completed.
        manifest = {
            "selected_namespaces": ["gn"],
            "namespaces": {
                "gn": {
                    "stages": {
                        "extract": "completed",
                        "boundary_merge": "skipped",
                        "h3": "completed",
                        "h3_merge": "completed",
                        "h3_coverage": "completed",
                        "ccode": "completed",
                        "ccode_merge": "running",   # stale
                        "aat_enrich": "running",    # stale
                    }
                }
            },
        }
        self._events("gn", "ccode_merge", ["running", "completed"])
        self._events("gn", "aat_enrich",  ["running", "completed"])
        is_complete, report = self.staging_orchestrator.check_global_barrier(
            manifest, staged_base_dir=str(self.staged),
        )
        self.assertTrue(is_complete, report)

    def test_genuinely_running_stage_blocks(self):
        # ccode_merge is a barrier-required stage (aat_enrich is NOT — it is a
        # post-barrier Batch 9 stage, gated at indexing instead).
        manifest = {
            "selected_namespaces": ["osm"],
            "namespaces": {
                "osm": {
                    "stages": {s: "completed" for s in [
                        "extract", "boundary_merge", "h3", "h3_merge",
                        "h3_coverage", "ccode",
                    ]} | {"ccode_merge": "running"}
                }
            },
        }
        # Event log also shows running — barrier should NOT pass.
        self._events("osm", "ccode_merge", ["running"])
        is_complete, report = self.staging_orchestrator.check_global_barrier(
            manifest, staged_base_dir=str(self.staged),
        )
        self.assertFalse(is_complete)
        self.assertIn("ccode_merge", report["osm"]["__missing__"])


if __name__ == "__main__":
    unittest.main()
