"""``update_merge`` must not be silently skippable.

It folds a namespace's Phase 3 update patch into its snapshot. Until 2026-08-03
nothing ran it, nothing checked it, and ``h3_stage`` fell back to ``extract/``
when ``update_merged/`` was absent — so the omission produced no error anywhere.

``gn`` and ``wd`` therefore passed the global barrier with their patches
unmerged in the 2026-05-02 rebuild *and* in the place#164 rebuild, costing
production ~26.7M GeoNames alternate names (every non-primary name, so no
Japanese / Cyrillic / Arabic recall for those places) and 58,658 Wikidata
Commons geoshapes. The patches were sitting on disk the whole time.

Three things now have to hold together, and each is cheap to break:
"""

from __future__ import annotations

import unittest

from processing.staging_contract import UPDATE_PATCH_NAMESPACES
from processing.staging_orchestrator import (
    GLOBAL_BARRIER_REQUIRED_STAGES,
    check_global_barrier,
)
from processing.submit_h3_slurm import _pending_namespaces


def _ns(**stages):
    base = {
        "extract": "completed", "update_merge": "completed",
        "boundary_merge": "skipped", "h3": "completed",
        "h3_merge": "completed", "h3_coverage": "completed",
        "ccode": "completed", "ccode_merge": "completed",
    }
    base.update(stages)
    return {"stages": base}


class UpdateMergeIsRequiredTests(unittest.TestCase):
    def test_it_is_a_barrier_stage(self):
        """Without this the corpus can pass the barrier with a patch unmerged."""
        self.assertIn("update_merge", GLOBAL_BARRIER_REQUIRED_STAGES)

    def test_barrier_fails_when_a_patch_namespace_has_not_merged(self):
        manifest = {
            "selected_namespaces": ["gn", "osm"],
            "namespaces": {"gn": _ns(update_merge="pending"), "osm": _ns()},
        }
        ok, report = check_global_barrier(manifest, staged_base_dir="/nonexistent")
        self.assertFalse(ok, "gn has an unmerged update patch; the barrier must hold")
        self.assertIn("update_merge", report["gn"].get("__missing__", ""))

    def test_barrier_passes_when_it_is_skipped_for_a_namespace_with_no_patch(self):
        manifest = {
            "selected_namespaces": ["osm"],
            "namespaces": {"osm": _ns(update_merge="skipped")},
        }
        ok, _ = check_global_barrier(manifest, staged_base_dir="/nonexistent")
        self.assertTrue(ok)


class H3DefersUntilMergedTests(unittest.TestCase):
    """The barrier alone is not enough — H3 runs before it."""

    def test_h3_defers_a_patch_namespace_until_merged(self):
        manifest = {"namespaces": {
            "gn": {"stages": {"extract": "completed", "update_merge": "pending",
                              "boundary_merge": "skipped", "h3": "pending"}},
        }}
        self.assertEqual(_pending_namespaces(manifest), [],
                         "H3 would read extract/ and drop the patch")

    def test_h3_proceeds_once_merged(self):
        manifest = {"namespaces": {
            "gn": {"stages": {"extract": "completed", "update_merge": "completed",
                              "boundary_merge": "skipped", "h3": "pending"}},
        }}
        self.assertEqual(_pending_namespaces(manifest), ["gn"])

    def test_namespaces_without_a_patch_are_unaffected(self):
        manifest = {"namespaces": {
            "iv": {"stages": {"extract": "completed", "update_merge": "skipped",
                              "boundary_merge": "skipped", "h3": "pending"}},
        }}
        self.assertEqual(_pending_namespaces(manifest), ["iv"])


class PatchNamespacesAreDeclaredTests(unittest.TestCase):
    def test_the_registry_matches_the_scripts_that_emit_patches(self):
        """gn and wd are the namespaces with a second, patch-emitting script."""
        from processing.ingest_all_authorities import INGESTION_ORDER
        multi = {ns for ns, *_ in INGESTION_ORDER
                 if sum(1 for n, *_ in INGESTION_ORDER if n == ns) > 1}
        self.assertEqual(
            set(UPDATE_PATCH_NAMESPACES), multi,
            "a namespace with a follow-up update script must be declared in "
            "UPDATE_PATCH_NAMESPACES, or its patch is silently dropped",
        )


if __name__ == "__main__":
    unittest.main()
