"""A skipped ccode stage must still leave a regenerated ``final/`` (Fault 12).

``un`` supplies the country codes, so enriching it against itself is
meaningless and ``submit_ccode_slurm`` marks its ``ccode`` stage ``skipped``.
Until 31 August 2026 it marked ``ccode_merge`` skipped as well — and
``ccode_merge`` is the **only** stage that writes ``final/``, which is what the
indexer reads.

The cost, measured in the 31 Aug audit: ``un``'s improved ``h3_cover`` sat in
``h3_merged/`` for three days while the live index served the stale copy. The
freshness gate could not see it, because the stale ``final/`` was internally
self-consistent — nothing compares it against the stage it derives from. And
``un`` is the namespace that supplies ``contained_in`` regions, so this alone
nullified the place#174 fix until someone checked by hand.

So the assertion here is not "the stage ran" (the run that broke this reported
success throughout) but the independent one: **``final/`` is newer than the
``h3_merged/`` it derives from, and carries its content.**
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq

try:                            # package-qualified run (tests/__init__.py ran)
    from ._sandbox import assert_sandboxed
except ImportError:             # `discover -s tests` puts tests/ on sys.path
    from _sandbox import assert_sandboxed


def _h3_merged_doc(place_id: str, ccodes: list[str], h3: str) -> dict:
    return {
        "place_id": place_id,
        "namespace": "un",
        "title": place_id.split(":")[-1].upper(),
        "ccodes": ccodes,
        "geometries": [{"geometry_index": 0, "h3_cover": [h3],
                        "repr_point": {"type": "Point", "coordinates": [2.0, 48.0]}}],
    }


class SkippedCcodeStillRegeneratesFinal(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # This test writes a namespace tree under STAGED_BASE_DIR. Refuse to run
        # unless the sandbox took effect — `discover -s tests` skips
        # tests/__init__.py, and that is how the real staged trees were stubbed.
        assert_sandboxed()
        from processing.settings import STAGED_BASE_DIR
        cls.staged_base = Path(STAGED_BASE_DIR)

    def setUp(self):
        self.ns_dir = self.staged_base / "un"
        if self.ns_dir.exists():
            shutil.rmtree(self.ns_dir)
        (self.ns_dir / "h3_merged").mkdir(parents=True)
        # The FRESH extract — the one that must reach final/.
        self.rows = [
            _h3_merged_doc("un:fra", ["FR"], "871f9a1ffffffff"),
            _h3_merged_doc("un:esp", ["ES"], "8739a10ffffffff"),
        ]
        pq.write_table(pa.Table.from_pylist(self.rows),
                       str(self.ns_dir / "h3_merged" / "places.parquet"))

    def _write_stale_final(self) -> Path:
        """A final/ from a PREVIOUS run: self-consistent, and out of date."""
        final_dir = self.ns_dir / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        stale = [_h3_merged_doc("un:fra", ["FR"], "OLD-CELL")]
        pq.write_table(pa.Table.from_pylist(stale), str(final_dir / "places.parquet"))
        path = final_dir / "places.parquet"
        # Backdate it, so "newer than h3_merged" is a real test and not an
        # artefact of both files being written in the same second.
        import os
        src_mtime = (self.ns_dir / "h3_merged" / "places.parquet").stat().st_mtime
        os.utime(path, (src_mtime - 3600, src_mtime - 3600))
        return path

    # -- the stage itself ---------------------------------------------------

    def test_pass_through_merge_needs_no_patch(self):
        """With no ccode patch on disk the merge copies h3_merged → final."""
        from processing.ccode_merge import run_ccode_merge
        metrics = run_ccode_merge(run_id="test-fault12", namespace="un",
                                  allow_missing_patch=True)
        self.assertEqual(2, metrics["docs_written"])
        self.assertEqual(0, metrics["docs_updated"])
        self.assertTrue(metrics["passthrough"])

        written = [json.loads(l) for l in
                   (self.ns_dir / "final" / "places.jsonl").read_text().splitlines()]
        self.assertEqual({"un:fra", "un:esp"}, {d["place_id"] for d in written})
        # ccodes pass through untouched — the patch is absent, not empty-and-authoritative
        self.assertEqual(["FR"], [d for d in written if d["place_id"] == "un:fra"][0]["ccodes"])

    def test_missing_patch_is_still_an_error_for_everyone_else(self):
        """Only a deliberately-skipped namespace may merge without a patch.

        For any other namespace an absent patch means the enrichment produced
        nothing, and passing its documents through would silently publish a
        corpus with no country codes.
        """
        from processing.ccode_merge import run_ccode_merge
        with self.assertRaises(FileNotFoundError):
            run_ccode_merge(run_id="test-fault12", namespace="un")

    # -- the submitter, which is where the fault actually lived --------------

    def _mark_skipped(self, manifest: dict, statuses: dict):
        """Run _mark_un_skipped against an in-memory manifest, recording writes."""
        from processing import submit_ccode_slurm as mod

        def _record(_path, ns, stage, status, metrics=None):
            statuses[(ns, stage)] = status

        # A real (if empty) manifest file: run_ccode_merge only records a stage
        # status when the path it is handed exists.
        manifest_path = self.staged_base / "runs" / "test-fault12" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest))

        with mock.patch.object(mod, "update_namespace_stage_status", _record), \
             mock.patch("processing.ccode_merge.update_namespace_stage_status", _record):
            mod._mark_un_skipped(manifest, manifest_path, run_id="test-fault12")

    def test_skipping_ccode_leaves_final_newer_than_h3_merged(self):
        stale_final = self._write_stale_final()
        src = self.ns_dir / "h3_merged" / "places.parquet"
        self.assertLess(stale_final.stat().st_mtime, src.stat().st_mtime,
                        "precondition: final/ starts out stale")

        statuses: dict = {}
        self._mark_skipped(
            {"namespaces": {"un": {"stages": {"ccode": "pending",
                                              "ccode_merge": "pending"}}}},
            statuses)

        # THE ASSERTION THAT WOULD HAVE CAUGHT FAULT 12.
        final = self.ns_dir / "final" / "places.parquet"
        self.assertTrue(final.exists())
        self.assertGreaterEqual(final.stat().st_mtime, src.stat().st_mtime,
                                "final/ must be regenerated from h3_merged/, not "
                                "left behind by a skipped ccode_merge")

        # ...and it holds the fresh h3_cover, not the previous run's.
        got = pq.read_table(str(final)).to_pylist()
        self.assertEqual({"un:fra", "un:esp"}, {d["place_id"] for d in got})
        covers = {c for d in got for g in d["geometries"] for c in g["h3_cover"]}
        self.assertNotIn("OLD-CELL", covers)

        # ccode enrichment itself is still skipped — un IS the ccode source.
        self.assertEqual("skipped", statuses[("un", "ccode")])
        # ...but its merge genuinely ran, so it is reported as completed.
        self.assertEqual("completed", statuses[("un", "ccode_merge")])

    def test_up_to_date_final_is_not_rebuilt(self):
        """A resume re-submitting the array must not redo a merge that is current."""
        from processing.ccode_merge import run_ccode_merge
        run_ccode_merge(run_id="test-fault12", namespace="un", allow_missing_patch=True)
        final = self.ns_dir / "final" / "places.parquet"
        before = final.stat().st_mtime_ns

        statuses: dict = {}
        self._mark_skipped(
            {"namespaces": {"un": {"stages": {"ccode": "skipped",
                                              "ccode_merge": "completed"}}}},
            statuses)

        self.assertEqual(before, final.stat().st_mtime_ns)
        self.assertEqual({}, statuses)

    def test_no_h3_merged_falls_back_to_skipped(self):
        """Nothing to derive from ⇒ record `skipped` so the barrier still passes.

        The global barrier requires `completed` or `skipped` for both ccode
        stages; left `pending`, un blocks it for ever. But the fallback is
        announced, because a silent one is how the stale final/ went unnoticed.
        """
        shutil.rmtree(self.ns_dir / "h3_merged")
        statuses: dict = {}
        self._mark_skipped(
            {"namespaces": {"un": {"stages": {"ccode": "pending",
                                              "ccode_merge": "pending"}}}},
            statuses)
        self.assertEqual("skipped", statuses[("un", "ccode")])
        self.assertEqual("skipped", statuses[("un", "ccode_merge")])

    def test_absent_namespace_is_a_no_op(self):
        statuses: dict = {}
        self._mark_skipped({"namespaces": {"gn": {"stages": {}}}}, statuses)
        self.assertEqual({}, statuses)


if __name__ == "__main__":
    unittest.main()
