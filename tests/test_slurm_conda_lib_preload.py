"""Slurm submitters must prefer the conda env's libstdc++ before importing.

Some `htc` nodes carry a `/lib64/libstdc++.so.6` (GLIBCXX to 3.4.29) older
than the conda env's `libicuuc.so.75` requires, and the loader prefers the
system copy. `import sqlite3` then dies with::

    ImportError: /lib64/libstdc++.so.6: version `GLIBCXX_3.4.30' not found
                 (required by .../envs/whg/.../libicuuc.so.75)

The env ships its own `libstdc++.so.6.0.34`, so exporting
``LD_LIBRARY_PATH="$CONDA_PREFIX/lib:..."`` resolves it. It is the node image
rather than the environment, so it strikes only *some* nodes and only some
array tasks — which is exactly why it reads as a mysterious code fault and
why it was diagnosed twice, independently, before anyone noticed the first
fix existed (`submit_hardlinks_slurm`, S3, htc-n77, 31 August 2026).

The failure is not confined to the harvest. `ccode_enrichment` imports
`geom_store`, which imports `sqlite3`, so an affected node loses the whole
array task at import — three frames deep, looking like a repository bug.

**Why the ordering assertions matter more than the presence one.** A test
that only checks the export string appears would still pass if someone moved
it above `conda activate`, where ``$CONDA_PREFIX`` is unset and the export
silently expands to a harmless no-op — reintroducing the fault while staying
green. Same trap if the probe drifts after the real work: the point of the
probe is to fail in one second rather than after an enrichment pass. So the
assertions here are about position, not just presence.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


EXPORT = 'export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH}"'


class _PreloadContract:
    """Assertions every submitter that imports sqlite3 must satisfy."""

    def _script(self) -> str:
        raise NotImplementedError

    def setUp(self):
        self.lines = self._script().splitlines()

    def _index_of(self, needle: str) -> int:
        for i, line in enumerate(self.lines):
            if needle in line:
                return i
        self.fail(f"not found in generated sbatch script: {needle!r}")

    def test_exports_the_conda_lib_path(self):
        self._index_of(EXPORT)

    def test_export_comes_after_conda_activate(self):
        """Above `conda activate`, $CONDA_PREFIX is unset and this is a no-op.

        The discriminating case: the export present but hoisted, which looks
        correct in a diff and does nothing at runtime.
        """
        self.assertGreater(
            self._index_of(EXPORT), self._index_of("conda activate"),
            "LD_LIBRARY_PATH export must follow `conda activate`, or "
            "$CONDA_PREFIX is empty and the export silently does nothing",
        )

    def test_probes_sqlite3_before_doing_any_work(self):
        """Fail in one second on an affected node, not after the real pass."""
        probe = self._index_of("import sqlite3")
        self.assertGreater(probe, self._index_of(EXPORT),
                           "the probe must come after the export or it tests "
                           "nothing but the broken loader path")
        first_work = min(
            i for i, line in enumerate(self.lines)
            if line.lstrip().startswith("python -") and "import sqlite3" not in line
        )
        self.assertLess(probe, first_work,
                        "the sqlite3 probe must precede the first real "
                        "invocation, so an affected node dies immediately")


class CcodeSubmitter(_PreloadContract, unittest.TestCase):
    """ccode_enrichment -> geom_store -> sqlite3, so this array needs it."""

    def _script(self) -> str:
        from processing import submit_ccode_slurm as mod

        with TemporaryDirectory() as tmp:
            # Patch _REPO so building the script cannot mkdir into the real
            # repo or, worse, onto /vast.
            with mock.patch.object(mod, "_REPO", Path(tmp)):
                return mod._build_sbatch_script(
                    run_id="test-run",
                    namespaces=["ukhc"],
                    manifest_path=Path(tmp) / "manifest.json",
                    array_map_path=Path(tmp) / "array_map.json",
                    wall_seconds_per_ns={"ukhc": 600},
                    depend_on=None,
                )


class HardlinksSubmitter(_PreloadContract, unittest.TestCase):
    """The original site of the fix — pinned so it cannot regress either."""

    def _script(self) -> str:
        from processing import submit_hardlinks_slurm as mod

        with TemporaryDirectory() as tmp:
            with mock.patch.object(mod, "_REPO", Path(tmp)):
                return mod._build_sbatch(
                    run_id="test-run",
                    manifest_path=Path(tmp) / "manifest.json",
                    db_path=Path(tmp) / "hardlinks.sqlite",
                    marker_path=Path(tmp) / "marker",
                    depend_on=None,
                    pitt_user=None,
                    pitt_host=None,
                    pitt_dir=None,
                    pitt_filename="hardlinks.sqlite",
                    pitt_live_db="live.sqlite",
                    skip_loc=True,
                    skip_contributors=True,
                    skip_prune=True,
                    loc_source=None,
                )


class EverySubmitterCarriesThePreamble(unittest.TestCase):
    """No submitter that activates conda may omit the preload.

    Deliberately exhaustive rather than naming submitters. The per-submitter
    tests above cover the two that HAD the fix; the defect was that seven
    others did not — including submit_h3_slurm, whose silent hull fallback
    killed the ccode tier-1 prefilter corpus-wide. A test naming only the
    known-good ones stays green through exactly that.
    """

    def test_no_submitter_activates_conda_without_the_preload(self):
        root = Path(__file__).resolve().parent.parent / "processing"
        missing = []
        for path in sorted(root.glob("submit_*.py")):
            src = path.read_text()
            if "conda activate" not in src:
                continue
            if "CONDA_LIB_PRELOAD" not in src and "LD_LIBRARY_PATH" not in src:
                missing.append(path.name)
        self.assertEqual(
            [], missing,
            "these submitters activate conda but never prefer its libstdc++, so "
            "`import sqlite3` dies on affected htc nodes: " + ", ".join(missing),
        )


# The contract is a mixin, not a test case in its own right.
del _PreloadContract


if __name__ == "__main__":
    unittest.main()
