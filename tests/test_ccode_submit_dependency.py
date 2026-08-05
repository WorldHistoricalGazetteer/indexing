"""`--depend-on` must not freeze the namespace list against a part-run H3 array.

Eligibility is evaluated when the array is SUBMITTED, not when it runs. Chaining
the ccode array behind the H3 array therefore selected against whatever H3 had
finished at that instant: on 5 August 2026 it picked 11 of 27 namespaces — the
small ones whose H3 tasks completed in the few minutes since submission — and
silently omitted osm, gn, wd, tgn and ohm, i.e. every namespace that mattered.

The dependency is the assertion that H3 will have run, so the H3 gate must not
be applied at selection time when one is given.
"""

from __future__ import annotations

import unittest


def _manifest(h3_state: dict) -> dict:
    return {"namespaces": {
        ns: {"stages": {"h3_merge": st, "ccode": "pending"}}
        for ns, st in h3_state.items()}}


class DependencyAwareSelection(unittest.TestCase):

    def setUp(self):
        # osm/gn still running; the small ones already done — the exact shape
        # of the 5 Aug mis-selection.
        self.m = _manifest({"osm": "pending", "gn": "pending", "wd": "running",
                            "alc": "completed", "tm": "completed"})

    def test_without_dependency_only_completed_h3_is_selected(self):
        from processing.submit_ccode_slurm import _pending_namespaces
        self.assertEqual(sorted(_pending_namespaces(self.m)), ["alc", "tm"])

    def test_with_dependency_all_pending_namespaces_are_selected(self):
        from processing.submit_ccode_slurm import _pending_namespaces
        got = sorted(_pending_namespaces(self.m, h3_pending_ok=True))
        self.assertEqual(got, ["alc", "gn", "osm", "tm", "wd"],
                         "a dependency asserts H3 will have run; omitting osm "
                         "and gn is the failure this guards against")

    def test_completed_ccode_is_still_skipped_for_idempotent_resume(self):
        from processing.submit_ccode_slurm import _pending_namespaces
        m = _manifest({"osm": "pending"})
        m["namespaces"]["osm"]["stages"]["ccode"] = "completed"
        self.assertEqual(_pending_namespaces(m, h3_pending_ok=True), [])

    def test_call_site_passes_the_flag(self):
        from pathlib import Path
        src = Path("processing/submit_ccode_slurm.py").read_text()
        self.assertIn("h3_pending_ok=bool(depend_on)", src)


if __name__ == "__main__":
    unittest.main()
