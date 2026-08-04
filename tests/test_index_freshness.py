"""An index built from a superseded staged artefact must not reach the alias.

The fault, 4 August 2026: ``update_merge`` re-ran for ``gn``, ``wd`` and ``nl``
*after* those namespaces were indexed. Their ``final/places.parquet`` was
rewritten; nothing re-ran the index stage. The manifest read
``index: completed`` throughout.

Doc counts are structurally blind to it — ``update_merge`` adds names to
*existing* places, so the place count does not move. A staging-vs-production
comparison matched on all 27 namespaces while ``gn`` carried one toponym per
place instead of its full inventory. These tests therefore assert on the
artefact comparison, and specifically that a count-preserving change is caught.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path


class FreshnessDetection(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def _make(self, ns, *, final_body=b"x" * 100, indexed_after=True):
        """Create staged final/ and index/ dirs with controlled mtimes."""
        final = self.base / ns / "final"
        index = self.base / ns / "index"
        final.mkdir(parents=True)
        index.mkdir(parents=True)
        f = final / "places.parquet"
        f.write_bytes(final_body)
        now = time.time()
        if indexed_after:
            os.utime(f, (now - 3600, now - 3600))     # source older
            os.utime(index, (now, now))               # indexed later
        else:
            os.utime(f, (now, now))                   # source newer
            os.utime(index, (now - 3600, now - 3600))
        return f, index

    def test_fresh_namespace_is_ok(self):
        from processing.index_freshness import check_namespace
        self._make("ok_ns", indexed_after=True)
        r = check_namespace("ok_ns", None, self.base)
        self.assertFalse(r["stale"], r)
        self.assertFalse(r["unknown"], r)

    def test_source_rewritten_after_indexing_is_stale(self):
        from processing.index_freshness import check_namespace
        self._make("gn", indexed_after=False)
        r = check_namespace("gn", None, self.base)
        self.assertTrue(r["stale"], r)
        self.assertEqual(r["basis"], "mtime")

    def test_fingerprint_beats_mtime_when_recorded(self):
        """A recorded fingerprint is authoritative over directory mtimes."""
        from processing.index_freshness import check_namespace, source_fingerprint
        f, _ = self._make("wd", indexed_after=False)  # mtimes say stale
        # The real manifest shape: status is a bare string, metrics live in a
        # sibling `stage_metrics` map (update_namespace_stage_status).
        manifest = {"namespaces": {"wd": {
            "stages": {"index": "completed"},
            "stage_metrics": {"index": {
                "source_fingerprint": source_fingerprint(f)}},
        }}}
        r = check_namespace("wd", manifest, self.base)
        self.assertFalse(r["stale"], "fingerprint matches, so not stale")
        self.assertEqual(r["basis"], "fingerprint")

    def test_count_preserving_rewrite_is_still_caught(self):
        """The actual failure mode: content changes, doc count does not.

        update_merge appends names to existing places. A same-length rewrite is
        the hardest case for anything comparing sizes alone, so the check must
        not rest on size.
        """
        from processing.index_freshness import check_namespace, source_fingerprint
        f, _ = self._make("gn", final_body=b"a" * 100, indexed_after=True)
        recorded = source_fingerprint(f)
        manifest = {"namespaces": {"gn": {
            "stages": {"index": "completed"},
            "stage_metrics": {"index": {"source_fingerprint": recorded}},
        }}}
        self.assertFalse(check_namespace("gn", manifest, self.base)["stale"])

        # Rewrite with identical byte length but a later mtime.
        f.write_bytes(b"b" * 100)
        os.utime(f, (time.time() + 10, time.time() + 10))
        r = check_namespace("gn", manifest, self.base)
        self.assertTrue(r["stale"],
                        "a same-size rewrite must still register as stale")

    def test_missing_artefact_is_unknown_not_fresh(self):
        """Absence of evidence must not read as evidence of freshness."""
        from processing.index_freshness import check_namespace
        (self.base / "empty_ns").mkdir()
        r = check_namespace("empty_ns", None, self.base)
        self.assertTrue(r["unknown"], r)
        self.assertFalse(r["stale"], r)

    def test_stale_namespaces_collects_only_stale(self):
        from processing.index_freshness import stale_namespaces
        self._make("fresh1", indexed_after=True)
        self._make("stale1", indexed_after=False)
        self._make("stale2", indexed_after=False)
        got = stale_namespaces(["fresh1", "stale1", "stale2"], None, self.base)
        self.assertEqual(sorted(got), ["stale1", "stale2"])


class PublicationIsGated(unittest.TestCase):
    """Both points where a stale index could become visible must refuse."""

    def test_index_from_stage_blocks_alias_swap(self):
        src = Path("processing/index_from_stage.py").read_text()
        self.assertIn("stale_namespaces(eligible, manifest)", src)
        self.assertIn("and not stale:", src,
                      "the alias swap condition must include the stale check")

    def test_promote_to_production_fails_verification(self):
        src = Path("processing/promote_to_production.py").read_text()
        self.assertIn("stale_namespaces(", src)
        self.assertIn("--allow-stale", src,
                      "override must be explicit, not the default")
        # The stale branch must clear `ok`, which is what gates the swap.
        idx = src.index("stale_namespaces(names, manifest_data)")
        window = src[idx:idx + 700]
        self.assertIn("ok = False", window)


class StaleCompletionIsReEligible(unittest.TestCase):
    """A stale `index: completed` must not make the namespace skip on resume.

    gn, wd and nl sat at index:completed for two days holding pre-merge data.
    A resume that honours that status walks straight back into the fault, and
    reports "No namespaces eligible for indexing" while doing so.
    """

    def test_eligibility_reruns_stale_namespaces(self):
        src = Path("processing/index_from_stage.py").read_text()
        i = src.index('if stage_status_with_fallback(manifest, ns, "index") == "completed":')
        window = src[i:i + 900]
        self.assertIn("check_namespace(ns, manifest)", window,
                      "a completed index must be freshness-checked before it "
                      "is allowed to skip")
        self.assertIn('["stale"]', window)
        # The `continue` must be conditional on NOT stale.
        self.assertIn("if not check_namespace(ns, manifest)[\"stale\"]:", window)

class CollapsedPlaceIdsAreReported(unittest.TestCase):
    """Duplicate place_ids produce two successful writes and one document.

    chgis stages 82,117 rows with 127 place_ids duplicated across 825 rows —
    all differing in `geometries` — and ends up with 81,292 documents. Because
    every bulk write succeeded, docs_indexed read 82,117 and the load looked
    clean. Only comparing staged rows against resulting DOCUMENTS shows it.
    """

    def test_helper_exists_and_compares_documents_not_writes(self):
        src = Path("processing/index_from_stage.py").read_text()
        self.assertIn("def _report_collapsed_ids(", src)
        i = src.index("def _report_collapsed_ids(")
        body = src[i:i + 2200]
        self.assertIn('"term": {"namespace": ns}', body,
                      "must count documents in the index per namespace")
        self.assertIn("docs_in_source", body,
                      "must compare against the staged row count")
        self.assertIn("if docs < staged:", body)

    def test_it_is_called_after_finalize(self):
        """Counting before the refresh would read a stale number."""
        src = Path("processing/index_from_stage.py").read_text()
        fin = src.index("final_count = finalize_index(")
        call = src.index("collapses = _report_collapsed_ids(")
        self.assertLess(fin, call)

    def test_it_does_not_block_the_load(self):
        """A duplicate place_id is a source defect, not an indexing failure."""
        src = Path("processing/index_from_stage.py").read_text()
        i = src.index("def _report_collapsed_ids(")
        body = src[i:i + 2200]
        self.assertNotIn("sys.exit", body)
        self.assertNotIn("raise SystemExit", body)

if __name__ == "__main__":
    unittest.main()
