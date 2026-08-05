"""A stale runtime median must not be allowed to kill a ccode run.

The bug, 5 August 2026: ``estimate_wall_time_seconds`` medians the last five
completed runs. That is only predictive while the *inputs* are unchanged, and
the BNDA→geoBoundaries move (232 → 73,663 vertices per country) invalidated
every stored ccode runtime at a stroke.

The array was given ``01:20:00`` on that stale history. Both tasks were killed
at the wall with the work unfinished:

    clio   9,407 of  15,690 documents written
    ohm  580,085 of ~905,000 documents written

Cost per document tracks geometry complexity rather than document count —
``clio`` is only 15,690 places but ran at 198 docs/min, while ``osm`` points
ran at 460,000 — so a doc-count-derived floor would not have saved it either.
Slurm wall time is a ceiling, not a reservation, so the floor simply buys the
whole shortest QOS tier.
"""

from __future__ import annotations

import unittest
from unittest import mock


class WallTimeFloor(unittest.TestCase):

    def _wall_for(self, estimate: int, namespace: str = "clio") -> int:
        """Run submit()'s wall-time selection with a stubbed estimator."""
        from processing import submit_ccode_slurm as mod

        captured: dict[str, dict] = {}

        def _fake_build(*, wall_seconds_per_ns, **kw):
            captured["walls"] = wall_seconds_per_ns
            return "#!/bin/bash\n"

        manifest = {"namespaces": {namespace: {"stages": {
            "h3_merge": "completed", "ccode": "pending"}}}}

        with mock.patch.object(mod, "estimate_wall_time_seconds",
                               return_value=estimate), \
             mock.patch.object(mod, "load_run_manifest",
                               return_value=manifest), \
             mock.patch.object(mod, "_build_sbatch_script", _fake_build), \
             mock.patch.object(mod, "_mark_un_skipped"), \
             mock.patch.object(mod, "_write_array_map",
                               return_value=mock.MagicMock()), \
             mock.patch.object(mod, "array_memory_gb", return_value=64), \
             mock.patch("pathlib.Path.mkdir"), \
             mock.patch("pathlib.Path.write_text"):
            mod.submit(run_id="r", manifest_path=mock.MagicMock(),
                       dry_run=True, only_namespaces=[namespace])

        return captured["walls"][namespace]

    def test_stale_short_median_is_raised_to_the_floor(self):
        """The actual failure: 01:20:00 from a pre-geoBoundaries median."""
        from processing.submit_ccode_slurm import _MIN_CCODE_WALL_SECONDS
        self.assertEqual(self._wall_for(4_760), _MIN_CCODE_WALL_SECONDS)

    def test_floor_sits_inside_the_shortest_qos_tier(self):
        """Over-asking must not push the job into a slower-scheduling tier."""
        from processing.submit_ccode_slurm import (
            _MIN_CCODE_WALL_SECONDS, _select_qos,
        )
        qos, capped = _select_qos(_MIN_CCODE_WALL_SECONDS)
        self.assertEqual(qos, "htc-htc-s")
        self.assertEqual(capped, _MIN_CCODE_WALL_SECONDS)

    def test_a_generous_estimate_is_left_alone(self):
        """The floor is a floor, not an override."""
        self.assertEqual(self._wall_for(2 * 86_400), 2 * 86_400)

    def test_explicit_wall_hours_wins_over_both(self):
        """An operator who names a wall time gets exactly that."""
        from processing import submit_ccode_slurm as mod

        captured: dict[str, dict] = {}

        def _fake_build(*, wall_seconds_per_ns, **kw):
            captured["walls"] = wall_seconds_per_ns
            return "#!/bin/bash\n"

        manifest = {"namespaces": {"osm": {"stages": {
            "h3_merge": "completed", "ccode": "pending"}}}}

        with mock.patch.object(mod, "estimate_wall_time_seconds",
                               return_value=4_760), \
             mock.patch.object(mod, "load_run_manifest",
                               return_value=manifest), \
             mock.patch.object(mod, "_build_sbatch_script", _fake_build), \
             mock.patch.object(mod, "_mark_un_skipped"), \
             mock.patch.object(mod, "_write_array_map",
                               return_value=mock.MagicMock()), \
             mock.patch.object(mod, "array_memory_gb", return_value=64), \
             mock.patch("pathlib.Path.mkdir"), \
             mock.patch("pathlib.Path.write_text"):
            mod.submit(run_id="r", manifest_path=mock.MagicMock(),
                       dry_run=True, only_namespaces=["osm"], wall_hours=20)

        self.assertEqual(captured["walls"]["osm"], 20 * 3600)


if __name__ == "__main__":
    unittest.main()
