"""A staged merge must never publish a half-written snapshot (§2.8).

``update_merge``, ``boundary_merge``, ``h3_merge`` and ``ccode_merge`` write
``update_merged/``, ``boundary_merged/``, ``h3_merged/`` and ``final/`` —
**four of the five** directories in ``_STAGED_SOURCE_PRIORITY`` (``final``,
``h3_merged``, ``boundary_merged``, ``update_merged``, ``extract``), and every
one of them outranks the ``extract/`` it derives from. Every consumer resolves
through that chain testing
``.exists()`` **only**, with no size or completeness check, and prefers
``places.parquet`` over ``places.jsonl`` within a stage.

Until 1 September 2026 all four did ``jsonl_path.open("w")`` **in place**
and derived the parquet afterwards. ``open("w")`` creates the file at *zero
bytes on the first instant*, so from that instant a reader resolving the
chain preferred an empty — then partial — file over the complete earlier
stage it supersedes, and got no rows at all. Silently, for as long as the
merge ran: hours, for ``gn`` and ``wd``. That is worse than truncation,
because a partial JSONL is valid JSONL as far as it has reached, so nothing
downstream can tell it apart from a small namespace.

The property under test is therefore **not** "the merge finishes correctly"
— it always did — but that the target path is only ever observed in a
complete state. The tests below assert that directly, without any
concurrency: they make the merge raise partway through its document loop and
then look at what it left behind. On a first write the target must be
**absent**; on a re-write it must be **byte-identical to the snapshot that
was already there**.

All four writers are covered because the defect is the class, not the site:
``update_merge`` in particular runs FIRST in the 2.7 chain, on ``gn`` — fixing
only the two originally scoped would have left the hazard exactly where the
next step meets it.

The crash tests fail on the pre-change code (a truncated file is left in
place), which is the point — per this campaign's standing rule, a verification that
has never been run against a known-bad input isn't a verification.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq

try:                            # package-qualified run (tests/__init__.py ran)
    from ._sandbox import assert_sandboxed
except ImportError:             # `discover -s tests` puts tests/ on sys.path
    from _sandbox import assert_sandboxed


def _doc(ns: str, place_id: str, marker: str) -> dict:
    return {
        "place_id": place_id,
        "namespace": ns,
        "title": place_id.split(":")[-1].upper(),
        "ccodes": ["GR"],
        "geometries": [{
            "geometry_index": 0,
            "h3_centroid": marker,
            "h3_cover": [marker],
            "repr_point": {"type": "Point", "coordinates": [23.7, 37.9]},
        }],
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _StagedMergeAtomicityBase(unittest.TestCase):
    """Shared fixture: a staged tree under the sandboxed STAGED_BASE_DIR."""

    #: overridden per merge under test
    module_name: str = ""
    out_stage: str = ""
    src_stage: str = ""
    ns: str = ""
    #: name of the module-level source iterator to make raise mid-loop
    source_iter: str = "_iter_source_docs"

    @classmethod
    def setUpClass(cls):
        # These tests write a namespace tree under STAGED_BASE_DIR. Refuse to
        # run unless the sandbox took effect — `discover -s tests` skips
        # tests/__init__.py, and that is how the real staged trees were stubbed
        # on 7 August. See tests/_sandbox.py.
        assert_sandboxed()
        from processing.settings import STAGED_BASE_DIR
        cls.staged_base = Path(STAGED_BASE_DIR)

    def setUp(self):
        self.ns_dir = self.staged_base / self.ns
        if self.ns_dir.exists():
            shutil.rmtree(self.ns_dir)
        (self.ns_dir / self.src_stage).mkdir(parents=True)
        self.rows = [_doc(self.ns, f"{self.ns}:{n}", "NEW-CELL") for n in range(6)]
        pq.write_table(pa.Table.from_pylist(self.rows),
                       str(self.ns_dir / self.src_stage / "places.parquet"))
        self.out_dir = self.ns_dir / self.out_stage
        self.jsonl = self.out_dir / "places.jsonl"
        self.parquet = self.out_dir / "places.parquet"

    # -- helpers ------------------------------------------------------------

    def _run(self):
        raise NotImplementedError

    def _write_previous_snapshot(self) -> tuple[str, str]:
        """A complete snapshot from an earlier run, as a reader would find it.

        Returns the (jsonl, parquet) digests that a crashed re-run must
        leave untouched.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        previous = [_doc(self.ns, f"{self.ns}:{n}", "OLD-CELL") for n in range(3)]
        with self.jsonl.open("w", encoding="utf-8") as fh:
            for row in previous:
                fh.write(json.dumps(row, ensure_ascii=True) + "\n")
        pq.write_table(pa.Table.from_pylist(previous), str(self.parquet))
        return _digest(self.jsonl), _digest(self.parquet)

    def _crash_partway(self):
        """Patch the merge's source iterator to die mid-loop.

        Deliberately *not* a concurrency test: the failure is made
        deterministic so the assertion is about the state left on disk, not
        about winning a race.
        """
        module = __import__(self.module_name, fromlist=[self.source_iter])
        rows = self.rows

        def _explode(namespace):
            for row in rows[:3]:
                yield row
            raise RuntimeError("simulated mid-merge failure")

        return mock.patch.object(module, self.source_iter, _explode)

    def _stray_temp_files(self) -> list[str]:
        if not self.out_dir.exists():
            return []
        return sorted(
            p.name for p in self.out_dir.iterdir()
            if p.name not in {"places.jsonl", "places.parquet"}
        )

    # -- the property -------------------------------------------------------

    def test_crash_on_first_write_leaves_no_snapshot(self):
        """A merge that dies mid-loop must publish nothing at all.

        Pre-change this leaves a partial ``places.jsonl``, which every
        resolver prefers over the complete upstream stage — the defect.
        """
        with self._crash_partway():
            with self.assertRaises(RuntimeError):
                self._run()

        self.assertFalse(
            self.jsonl.exists(),
            f"a crashed merge published a partial {self.out_stage}/places.jsonl "
            f"({self.jsonl.stat().st_size if self.jsonl.exists() else 0} bytes); "
            "every consumer prefers it over the complete upstream stage",
        )
        self.assertFalse(self.parquet.exists(),
                         f"a crashed merge published {self.out_stage}/places.parquet")
        self.assertEqual([], self._stray_temp_files(),
                         "crashed merge left temp files behind")

    def test_crash_on_rewrite_preserves_previous_snapshot(self):
        """A merge that dies mid-loop must not damage the snapshot in place.

        Pre-change ``open("w")`` truncates it to zero bytes immediately, so a
        re-run that fails destroys a good snapshot as its first act.
        """
        jsonl_before, parquet_before = self._write_previous_snapshot()

        with self._crash_partway():
            with self.assertRaises(RuntimeError):
                self._run()

        self.assertTrue(self.jsonl.exists(),
                        "a crashed re-run deleted the previous snapshot's JSONL")
        self.assertEqual(
            jsonl_before, _digest(self.jsonl),
            f"a crashed re-run modified {self.out_stage}/places.jsonl in place "
            "— readers resolving during the merge see a partial snapshot",
        )
        self.assertEqual(parquet_before, _digest(self.parquet),
                         f"a crashed re-run modified {self.out_stage}/places.parquet")
        self.assertEqual([], self._stray_temp_files(),
                         "crashed merge left temp files behind")

    # -- the fix must not break the merge itself ----------------------------

    def test_successful_merge_publishes_complete_snapshot(self):
        """Atomicity is worthless if the merge stops producing the goods.

        ``final/`` is what the indexer and the tile submitter consume, so a
        defect introduced here yields an empty stage that reports success —
        this campaign's signature failure. Assert the row count independently.
        """
        metrics = self._run()

        self.assertEqual(len(self.rows), metrics["docs_written"])
        self.assertTrue(self.jsonl.exists())
        self.assertTrue(self.parquet.exists())

        with self.jsonl.open("r", encoding="utf-8") as fh:
            lines = [json.loads(ln) for ln in fh if ln.strip()]
        self.assertEqual(len(self.rows), len(lines))
        self.assertEqual({r["place_id"] for r in self.rows},
                         {ln["place_id"] for ln in lines})

        table = pq.read_table(str(self.parquet))
        self.assertEqual(len(self.rows), table.num_rows)
        self.assertEqual([], self._stray_temp_files(),
                         "a successful merge left temp files behind")

    def test_successful_rewrite_replaces_previous_snapshot(self):
        """The happy-path re-run still supersedes what was there."""
        self._write_previous_snapshot()

        metrics = self._run()

        self.assertEqual(len(self.rows), metrics["docs_written"])
        table = pq.read_table(str(self.parquet))
        self.assertEqual(len(self.rows), table.num_rows)
        with self.jsonl.open("r", encoding="utf-8") as fh:
            lines = [json.loads(ln) for ln in fh if ln.strip()]
        self.assertEqual(len(self.rows), len(lines))
        self.assertEqual([], self._stray_temp_files())


class UpdateMergeAtomicity(_StagedMergeAtomicityBase):
    """First writer in the 2.7 chain, and the one 2.8 originally missed."""

    module_name = "processing.update_merge"
    out_stage = "update_merged"
    src_stage = "extract"
    source_iter = "_iter_extract_docs"
    ns = "wd"                   # must be in UPDATE_PATCH_NAMESPACES

    def setUp(self):
        super().setUp()
        # An empty patch set is enough: the property under test is the write,
        # not the merge. _load_patches tolerates an absent patch file.
        patch_dir = self.ns_dir / "update"
        patch_dir.mkdir(parents=True, exist_ok=True)

    def _run(self):
        from processing.update_merge import run_update_merge
        return run_update_merge(run_id="test-2.8-atomicity", namespace=self.ns)


class BoundaryMergeAtomicity(_StagedMergeAtomicityBase):
    module_name = "processing.boundary_merge"
    out_stage = "boundary_merged"
    src_stage = "extract"
    source_iter = "_iter_extract_docs"
    ns = "ohm"                  # boundary_merge accepts only osm/ohm

    def setUp(self):
        super().setUp()
        # boundary_merge raises unless a patch file exists; empty is enough.
        patch_dir = self.ns_dir / "boundary"
        patch_dir.mkdir(parents=True, exist_ok=True)
        (patch_dir / "places.boundary.jsonl").write_text("", encoding="utf-8")

    def _run(self):
        from processing.boundary_merge import run_boundary_merge
        return run_boundary_merge(run_id="test-2.8-atomicity", namespace=self.ns)


class H3MergeAtomicity(_StagedMergeAtomicityBase):
    module_name = "processing.h3_merge"
    out_stage = "h3_merged"
    src_stage = "extract"
    ns = "pl"                   # neither boundary-required nor update-patched,
                                # so h3_merge reads plain ``extract/``

    def setUp(self):
        super().setUp()
        # h3_merge raises unless a patch file exists; an empty patch set is
        # enough — the atomicity property is about the write, not the merge.
        h3_dir = self.ns_dir / "h3"
        h3_dir.mkdir(parents=True, exist_ok=True)
        (h3_dir / "places.h3.jsonl").write_text("", encoding="utf-8")

    def _run(self):
        from processing.h3_merge import run_h3_merge
        return run_h3_merge(run_id="test-2.8-atomicity", namespace=self.ns)


class CcodeMergeAtomicity(_StagedMergeAtomicityBase):
    module_name = "processing.ccode_merge"
    out_stage = "final"
    src_stage = "h3_merged"
    ns = "pl"

    def _run(self):
        from processing.ccode_merge import run_ccode_merge
        return run_ccode_merge(run_id="test-2.8-atomicity", namespace=self.ns,
                               allow_missing_patch=True)


class CleanupNeverMasksTheRealFailure(unittest.TestCase):
    """A failing cleanup must not replace the exception that caused it.

    ``_unlink_quietly`` swallows only ``FileNotFoundError``, deliberately: the
    failed-conversion branch relies on a stale sidecar actually being removed.
    But that means a ``PermissionError`` — or an NFS ``EIO``, which this
    cluster has produced — while removing the temps would propagate *instead
    of* the real error, losing the cause exactly when it is most needed.
    """

    def test_original_exception_survives_a_failing_cleanup(self):
        from processing import staged_parquet

        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            jsonl = out_dir / "places.jsonl"
            parquet = out_dir / "places.parquet"

            # The helper also pre-cleans temps on entry; only the cleanup
            # inside the exception handler is under test here.
            calls = {"n": 0}

            def _explode_on_unlink(*_args, **_kwargs):
                calls["n"] += 1
                if calls["n"] > 1:
                    raise PermissionError("simulated NFS failure during cleanup")

            with mock.patch.object(staged_parquet, "_unlink_quietly",
                                   _explode_on_unlink):
                with self.assertRaises(RuntimeError) as caught:
                    with staged_parquet.atomic_staged_snapshot(
                            jsonl, parquet, label="test"):
                        raise RuntimeError("the real failure")

            self.assertEqual("the real failure", str(caught.exception))


# The base class is a fixture, not a test case.
del _StagedMergeAtomicityBase


if __name__ == "__main__":
    unittest.main()
