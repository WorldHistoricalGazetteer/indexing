"""``processing.local_dataset_job`` indexing POLYGON datasets through a real
per-dataset geom OVERLAY (place#341, step 2).

Run package-qualified, never with ``discover -s tests`` (tests/_sandbox.py):

    .venv/bin/python -m unittest tests.test_local_dataset_job_overlay

Same harness as ``tests.test_local_dataset_job`` (real LPF → staged-doc
mapping with a real ``GeomStoreWriter``, ``index_namespace`` in-process against
the evaluating ``FakeES``), plus a REAL main geom store in ``GEOM_STORE_DIR``
so the runner's ``GeomStoreReader`` can open, and nothing patched between the
staging directory and the overlay except where a test says so. Every
"absent" assertion sits beside a "present" one on the same fixture, and the
harness is shown to be able to fail: a writer that lies about having written
an overlay, and one that writes an EMPTY one, both stop the job before any ES
write.
"""

from __future__ import annotations

import json
import logging
import os
import unittest
from unittest import mock

from shapely.geometry import Point, shape

from gateway import dataset_jobs as dj
from processing import geom_overlay as go
from processing import local_dataset_job as ldj
from processing.geom_store import GeomStoreReader
from tests.test_geom_overlay import _main_store
from tests.test_local_dataset_job import (
    DS,
    JOB,
    PUBLISHED,
    _RunnerSandbox,
    _lpf,
    _multipoint,
    _point,
)

JOB2 = "1700000000001"
JOB3 = "1700000000002"

# Two islands, each with a lake, ~0.6° × 0.5° and 0.4° × 0.3° …
ISLANDS = {"type": "MultiPolygon", "coordinates": [
    [[[10.0, 50.0], [10.6, 50.0], [10.6, 50.5], [10.0, 50.5], [10.0, 50.0]],
     [[10.2, 50.2], [10.3, 50.2], [10.3, 50.3], [10.2, 50.3], [10.2, 50.2]]],
    [[[11.0, 50.0], [11.4, 50.0], [11.4, 50.3], [11.0, 50.3], [11.0, 50.0]],
     [[11.1, 50.1], [11.2, 50.1], [11.2, 50.2], [11.1, 50.2], [11.1, 50.1]]],
]}
# … and a Polygon of ~0.5° extent.
SHIRE = {"type": "Polygon", "coordinates": [[
    [-1.0, 51.0], [-0.5, 51.05], [-0.45, 51.5], [-1.05, 51.45], [-1.0, 51.0]]]}
AREA = {"islands": shape(ISLANDS).area, "shire": shape(SHIRE).area}


def _polygon_features():
    return [
        _lpf(1, "islands", "Islands", ISLANDS, ccodes=["DE"]),
        _lpf(2, "shire", "Shire", SHIRE),
        _lpf(3, "town", "Town", _point(-0.1, 51.5)),
    ]


class _OverlaySandbox(_RunnerSandbox):
    """The runner sandbox plus a real main store the reader can open."""

    MAIN = {"osm:r1_0": b"<osm:r1_0>" * 3}

    def setUp(self):
        super().setUp()
        _main_store(self.geom_store, self.MAIN)
        self.records[DS] = dict(PUBLISHED)
        # The runner's INFO log, captured (the base sandbox only silences it).
        self.log_records: list[str] = []
        sandbox = self

        class _Capture(logging.Handler):
            def emit(self, record):
                sandbox.log_records.append(record.getMessage())
        handler = _Capture()
        ldj.logger.addHandler(handler)
        self.addCleanup(ldj.logger.removeHandler, handler)
        level = ldj.logger.level
        ldj.logger.setLevel(logging.INFO)
        self.addCleanup(ldj.logger.setLevel, level)

    # -- helpers ----------------------------------------------------------------
    def reader(self) -> GeomStoreReader:
        r = GeomStoreReader(self.geom_store)
        self.addCleanup(r.close)
        return r

    def current(self):
        return go.current_generation(self.geom_store, DS)

    def gen_dirs(self):
        odir = go.overlay_dir(self.geom_store, DS)
        if not odir.exists():
            return []
        return sorted(p.name for p in odir.iterdir() if p.is_dir() and not p.is_symlink())

    def es_snapshot(self):
        return json.dumps(self.es.docs, sort_keys=True)

    def recording_overlay_writer(self):
        """Wrap the REAL ``write_overlay`` so the call log shows when it ran
        relative to every ES write, and what ES looked like at that moment."""
        real = go.write_overlay
        before = self.es_snapshot()

        def wrapped(store, ds, staging, job, **kw):
            self.calls.append(("overlay", str(store), ds, str(staging), job,
                               self.es_snapshot() == before, len(self.es.calls)))
            return real(store, ds, staging, job, **kw)
        p = mock.patch.object(go, "write_overlay", side_effect=wrapped)
        p.start()
        self.addCleanup(p.stop)

    def recording_overlay_remover(self):
        real = go.remove_overlay

        def wrapped(store, ds):
            self.calls.append(("remove_overlay", ds, sorted(self.es.docs["places_x"])))
            return real(store, ds)
        p = mock.patch.object(go, "remove_overlay", side_effect=wrapped)
        p.start()
        self.addCleanup(p.stop)

    def new_job(self, job):
        dj.create_state(job, DS, "index")
        return job

    def assert_es_untouched_since(self, snapshot, n_calls=0):
        self.assertEqual(self.es_snapshot(), snapshot, "ES changed")
        self.assertEqual(len(self.es.calls), n_calls, self.es.calls)


# ---------------------------------------------------------------------------
# The happy path, with the ordering proven from the call log
# ---------------------------------------------------------------------------

class TestPolygonDataset(_OverlaySandbox):
    def test_polygon_dataset_is_indexed_through_a_real_overlay(self):
        self.features[DS] = _polygon_features()
        self.recording_overlay_writer()
        self.assertIsNone(self.current(), "fixture: no overlay before the run")
        _, rc = self.run_job()
        self.assertEqual(rc, 0, (dj.read_state(JOB), self.stdout.getvalue()))

        # PRESENCE FIRST: the overlay is CURRENT, and its manifest is the job's.
        gen = self.current()
        self.assertIsNotNone(gen, "no overlay CURRENT after the run")
        manifest = go.read_manifest(go.overlay_dir(self.geom_store, DS) / gen)
        self.assertEqual((manifest["dataset_id"], manifest["job_id"], manifest["count"]),
                         (DS, JOB, 2))                     # islands + shire; the point is not stored
        self.assertEqual(self.gen_dirs(), [gen])

        # The reader gets the polygons back, bytes-faithful (areas, hole survived).
        r = self.reader()
        islands = r.get("whg:12:islands_0")
        self.assertIsNotNone(islands)
        self.assertEqual(islands["type"], "MultiPolygon")
        self.assertEqual(len(islands["coordinates"][0]), 2, "the hole did not survive")
        self.assertAlmostEqual(shape(islands).area, AREA["islands"], places=9)
        shire = r.get("whg:12:shire_0")
        self.assertEqual(shire["type"], "Polygon")
        self.assertAlmostEqual(shape(shire).area, AREA["shire"], places=9)
        self.assertIsNone(r.get("whg:12:town_0"), "a bare Point must not be stored")
        self.assertEqual(r.overlay_generations, {DS: gen})
        # … and the main store was never involved.
        self.assertEqual(len(r), len(self.MAIN))
        self.assertEqual(r.get_wkb("osm:r1_0"), self.MAIN["osm:r1_0"])

        # The ES docs: has_geom kept, geom_ref intact, multi-cell cover from
        # the stored polygon, repr_point inside it.
        for src, gj in (("islands", ISLANDS), ("shire", SHIRE)):
            doc = self.es.docs["places_x"][f"whg:12:{src}"]
            (g,) = doc["geometries"]
            self.assertIs(g["has_geom"], True, doc)
            self.assertEqual(g["geom_ref"], f"whg:12:{src}_0")
            self.assertEqual(g["geom_class"], "area")
            self.assertGreater(len(g["h3_cover"]), 1, f"{src}: cover is not multi-cell")
            self.assertTrue(g["h3_centroid"])
            rp = g["repr_point"]
            self.assertTrue(shape(gj).contains(Point(rp["lon"], rp["lat"])),
                            f"{src}: repr_point {rp} outside the polygon")
        town = self.es.docs["places_x"]["whg:12:town"]["geometries"][0]
        self.assertIs(town["has_geom"], False)
        self.assertIsNone(town.get("geom_ref"))        # null in final/, dropped by the indexer
        self.assertEqual(len(town["h3_cover"]), 1)
        self.assertEqual(sorted(self.es.docs["places_x"]),
                         ["whg:12:islands", "whg:12:shire", "whg:12:town", "whg:1:a"])
        self.assertEqual(self.es.docs["places_x"]["whg:12:islands"]["ccodes"], ["DE"])
        self.assertEqual(self.es.docs["places_x"]["whg:12:shire"]["ccodes"], ["GB"])

        # ORDER: overlay written strictly before the first ES write, with ES
        # untouched at that moment; dry run, execute, marker last.
        kinds = [c[0] for c in self.calls]
        self.assertEqual(kinds, ["discover", "stage", "overlay", "index", "index"])
        (ov,) = [c for c in self.calls if c[0] == "overlay"]
        self.assertEqual((ov[1], ov[2], ov[4]), (str(self.geom_store), DS, JOB))
        self.assertTrue(ov[5], "ES had already changed when the overlay was written")
        self.assertEqual(ov[6], 0, "ES calls had been made before the overlay was written")
        (stage_call,) = [c for c in self.calls if c[0] == "stage"]
        self.assertEqual(ov[3], str(stage_call[2]), "overlay packed a different staging dir")
        self.assertFalse(stage_call[2].exists(), "private geom staging survived")
        dry, execute = self.index_calls()
        self.assertNotIn("--execute", dry[1])
        self.assertIn("--execute", execute[1])
        self.assertEqual((dry[2], execute[2]), (False, False), "marker not written last")
        m = json.loads(self.marker().read_text())
        self.assertEqual((m["job_id"], m["indexed_count"]), (JOB, 3))

        # The state file records the generation; the message names it.
        st = dj.read_state(JOB)
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["overlay"]["generation"], gen)
        self.assertEqual(st["overlay"]["count"], 2)
        self.assertIsNone(st["overlay"]["previous"])
        self.assertEqual(st["overlay"]["store"], str(self.geom_store))
        self.assertIn(f"overlay {gen} (2 geometries)", st["message"])
        self.assertTrue(any(f"overlay whg-{DS} generation {gen} is CURRENT" in m
                            for m in self.log_records), self.log_records)
        self.assertIn(f"geom-overlay: wrote whg-{DS}/{gen}", self.stdout.getvalue())
        self.assertIsNone(dj.read_lock())

    def test_sub_cell_multipoint_keeps_has_geom_with_a_one_cell_cover(self):
        # Formerly DEMOTED to has_geom=false because its store entry was
        # discarded; now the entry is served from the overlay, so the promise
        # is kept and the cover stays centroid-only (it fits in one cell).
        self.features[DS] = [_lpf(3, "c", "C", _multipoint((-3.0, 55.0), (-3.001, 55.001)))]
        _, rc = self.run_job()
        self.assertEqual(rc, 0, dj.read_state(JOB))
        (g,) = self.es.docs["places_x"]["whg:12:c"]["geometries"]
        self.assertIs(g["has_geom"], True)
        self.assertEqual(g["geom_ref"], "whg:12:c_0")
        self.assertEqual(g["geom_class"], "point")
        self.assertEqual(len(g["h3_cover"]), 1)
        self.assertIn(g["h3_centroid"], g["h3_cover"])
        r = self.reader()
        self.assertEqual(r.get("whg:12:c_0")["type"], "MultiPoint")
        self.assertEqual(dj.read_state(JOB)["overlay"]["count"], 1)


# ---------------------------------------------------------------------------
# HARNESS-CAN-FAIL: an overlay that was not really written stops the job
# ---------------------------------------------------------------------------

class TestPostCondition(_OverlaySandbox):
    def test_a_writer_that_lies_or_writes_nothing_is_caught_before_any_es_write(self):
        self.features[DS] = _polygon_features()
        es_before = self.es_snapshot()

        # (a) write_overlay is a no-op that RETURNS a plausible manifest.
        fake = {"schema": go.MANIFEST_SCHEMA, "dataset_id": DS, "generation": "20260101T000000Z-j1",
                "job_id": JOB, "count": 2, "bytes": 0}
        with mock.patch.object(go, "write_overlay", return_value=dict(fake)) as w:
            _, rc = self.run_job()
        self.assertEqual(rc, 1)
        w.assert_called_once()
        st = self.assert_failed("stage_error", "was written but the reader serves None")
        self.assertIsNone(self.current())
        self.assertEqual(self.index_calls(), [], "index_namespace ran")
        self.assert_es_untouched_since(es_before)
        self.assertIsNone(self.final_docs(), "a half final/ was published")
        self.assertFalse(self.marker().exists())
        self.assertEqual(st["overlay"]["generation"], fake["generation"])

        # (b) write_overlay writes a real but EMPTY generation: the reader
        # serves it, and the per-key post-condition names the missing keys.
        real = go.write_overlay
        empty = self.tmp / "empty-staging"
        empty.mkdir()

        def writes_nothing(store, ds, staging, job, **kw):
            return real(store, ds, empty, job, allow_empty=True, **kw)
        dj.update_state(JOB, status="queued")
        with mock.patch.object(go, "write_overlay", side_effect=writes_nothing):
            _, rc = self.run_job()
        self.assertEqual(rc, 1)
        st = self.assert_failed("stage_error", "2 of 2 has_geom geometries")
        self.assertIn("whg:12:islands_0", st["message"])
        self.assertIn("whg:12:shire_0", st["message"])
        self.assertIsNotNone(self.current(), "the empty generation is CURRENT (served, empty)")
        self.assertEqual(self.index_calls(), [])
        self.assert_es_untouched_since(es_before)
        self.assertIsNone(self.final_docs())
        self.assertFalse(self.marker().exists())

        # POSITIVE CONTROL on the same fixture: the real writer gets it indexed.
        dj.update_state(JOB, status="queued")
        _, rc = self.run_job()
        self.assertEqual(rc, 0, dj.read_state(JOB))
        self.assertNotEqual(self.es_snapshot(), es_before)
        self.assertIn("whg:12:islands", self.es.docs["places_x"])

    def test_missing_lock_token_is_a_stage_error_with_es_untouched(self):
        self.features[DS] = _polygon_features()
        es_before = self.es_snapshot()
        real_stage = self._stage

        def stage_then_drop_lock(dataset_id, geom_dir):
            out = real_stage(dataset_id, geom_dir)
            dj.lock_path().unlink()
            return out
        self.steps.stage = stage_then_drop_lock
        _, rc = self.run_job()
        self.assertEqual(rc, 1)
        self.assert_failed("stage_error", "OverlayLockMissing")
        self.assertIsNone(self.current())
        self.assertEqual(self.gen_dirs(), [], "a refused write left a generation dir")
        self.assertEqual(self.index_calls(), [])
        self.assert_es_untouched_since(es_before)


# ---------------------------------------------------------------------------
# Budget: nothing displaced, nothing written to ES
# ---------------------------------------------------------------------------

class TestBudget(_OverlaySandbox):
    def test_over_budget_leaves_es_and_the_previous_generation_untouched(self):
        # Generation 1: one polygon.
        self.features[DS] = [_lpf(2, "shire", "Shire", SHIRE)]
        _, rc = self.run_job()
        self.assertEqual(rc, 0, dj.read_state(JOB))
        gen1 = self.current()
        es_after_1 = self.es_snapshot()
        n_calls_1 = len(self.es.calls)
        self.calls.clear()

        # Job 2 stages two geometries under a one-row cap.
        self.features[DS] = _polygon_features()
        self.new_job(JOB2)
        with mock.patch.dict(os.environ, {go.MAX_GEOMETRIES_ENV: "1"}):
            _, rc = self.run_job(job=JOB2)
        self.assertEqual(rc, 1)
        self.assert_failed("overlay_budget", "cap 1", job=JOB2)
        self.assertEqual(self.current(), gen1, "CURRENT moved off the previous generation")
        self.assertEqual(self.gen_dirs(), [gen1], "a refused build left a directory")
        r = self.reader()
        self.assertAlmostEqual(shape(r.get("whg:12:shire_0")).area, AREA["shire"], places=9)
        self.assertIsNone(r.get("whg:12:islands_0"))
        self.assertEqual(self.index_calls(), [], "index_namespace ran after a budget refusal")
        self.assert_es_untouched_since(es_after_1, n_calls_1)
        self.assertIn("whg:12:shire", self.es.docs["places_x"])
        self.assertNotIn("whg:12:islands", self.es.docs["places_x"])
        st = dj.read_state(JOB2)
        self.assertIsNone(st.get("overlay"), "a refused overlay was recorded as written")

        # The overlay-COUNT cap is checked before anything is built: with the
        # dataset not among the served overlays and the cap already full,
        # the job refuses without creating a generation.
        go.remove_overlay(self.geom_store, DS)
        token = self.tmp / "token"
        token.write_text("{}")
        from tests.test_geom_overlay import _stage as stage_items
        other = stage_items(self.tmp / "other", {"whg:99:a_0": SHIRE})
        go.write_overlay(self.geom_store, "99", other, "1600000000000", lock_token=token)
        self.assertEqual(set(go.list_overlays(self.geom_store)), {"99"})
        self.new_job(JOB3)
        self.calls.clear()
        with mock.patch.dict(os.environ, {go.MAX_OVERLAYS_ENV: "1"}):
            _, rc = self.run_job(job=JOB3)
        self.assertEqual(rc, 1)
        self.assert_failed("overlay_budget", "overlays already present, cap 1", job=JOB3)
        self.assertIsNone(self.current())
        self.assertEqual(self.gen_dirs(), [gen1], "a generation was built despite the cap")
        self.assertEqual(self.index_calls(), [])
        self.assert_es_untouched_since(es_after_1, n_calls_1)

        # POSITIVE CONTROL: the same fixture under the default caps indexes.
        dj.update_state(JOB3, status="queued")
        _, rc = self.run_job(job=JOB3)
        self.assertEqual(rc, 0, dj.read_state(JOB3))
        self.assertIn("whg:12:islands", self.es.docs["places_x"])
        self.assertEqual(set(go.list_overlays(self.geom_store)), {DS, "99"})


# ---------------------------------------------------------------------------
# Replace: a new generation, or a stale overlay removed after the ES write
# ---------------------------------------------------------------------------

class TestReplace(_OverlaySandbox):
    def test_reindex_writes_a_new_generation_that_serves_the_new_bytes(self):
        self.features[DS] = [_lpf(2, "shire", "Shire", SHIRE)]
        _, rc = self.run_job()
        self.assertEqual(rc, 0, dj.read_state(JOB))
        gen1 = self.current()
        r = self.reader()
        self.assertAlmostEqual(shape(r.get("whg:12:shire_0")).area, AREA["shire"], places=9)
        cover1 = self.es.docs["places_x"]["whg:12:shire"]["geometries"][0]["h3_cover"]

        # Same src_id, a different polygon: a new generation, new bytes, new cover.
        self.features[DS] = [_lpf(2, "shire", "Shire", ISLANDS)]
        self.new_job(JOB2)
        _, rc = self.run_job(job=JOB2)
        self.assertEqual(rc, 0, dj.read_state(JOB2))
        gen2 = self.current()
        self.assertNotEqual(gen2, gen1)
        self.assertEqual(self.gen_dirs(), sorted([gen1, gen2]), "the previous generation "
                         "must survive the prune (keep=2)")
        fresh = self.reader()
        got = fresh.get("whg:12:shire_0")
        self.assertEqual(got["type"], "MultiPolygon")
        self.assertAlmostEqual(shape(got).area, AREA["islands"], places=9)
        # The long-lived reader follows the swap on its next rescan too.
        r.tick(force=True)
        self.assertEqual(r.get("whg:12:shire_0")["type"], "MultiPolygon")
        cover2 = self.es.docs["places_x"]["whg:12:shire"]["geometries"][0]["h3_cover"]
        self.assertNotEqual(sorted(cover1), sorted(cover2))
        st = dj.read_state(JOB2)
        self.assertEqual((st["overlay"]["generation"], st["overlay"]["previous"]), (gen2, gen1))
        self.assertEqual(json.loads(self.marker().read_text())["job_id"], JOB2)

    def test_reindex_without_geometry_removes_the_stale_overlay_after_the_replace(self):
        self.features[DS] = [_lpf(2, "shire", "Shire", SHIRE)]
        _, rc = self.run_job()
        self.assertEqual(rc, 0, dj.read_state(JOB))
        gen1 = self.current()
        self.assertIsNotNone(gen1)

        self.features[DS] = [_lpf(2, "shire", "Shire", _point(-0.7, 51.2))]
        self.recording_overlay_remover()
        self.new_job(JOB2)
        _, rc = self.run_job(job=JOB2)
        self.assertEqual(rc, 0, dj.read_state(JOB2))
        self.assertIsNone(self.current(), "the stale overlay is still CURRENT")
        self.assertIsNone(self.reader().get("whg:12:shire_0"))
        (g,) = self.es.docs["places_x"]["whg:12:shire"]["geometries"]
        self.assertIs(g["has_geom"], False)
        self.assertIsNone(g.get("geom_ref"))
        st = dj.read_state(JOB2)
        self.assertIsNone(st["overlay"])
        self.assertIn("stale overlay removed", st["message"])
        # Removed strictly AFTER the execute run, with the new doc already live.
        kinds = [c[0] for c in self.calls if c[0] in ("index", "remove_overlay")]
        self.assertEqual(kinds, ["index", "index", "index", "index", "remove_overlay"])
        (rm,) = [c for c in self.calls if c[0] == "remove_overlay"]
        self.assertIn("whg:12:shire", rm[2])

        # POSITIVE CONTROL: a failing replace leaves the overlay serving.
        self.features[DS] = [_lpf(2, "shire", "Shire", SHIRE)]
        self.new_job(JOB3)
        _, rc = self.run_job(job=JOB3)
        self.assertEqual(rc, 0, dj.read_state(JOB3))
        gen3 = self.current()
        self.assertIsNotNone(gen3)
        self.features[DS] = [_lpf(2, "shire", "Shire", _point(-0.7, 51.2))]
        real = self._run_index

        def execute_dies(argv):
            rc, out = real(argv)
            return (3, out) if "--execute" in argv else (rc, out)
        self.steps.run_index = execute_dies
        self.new_job("1700000000003")
        _, rc = self.run_job(job="1700000000003")
        self.assertEqual(rc, 1)
        self.assert_failed("index_error", "exited 3")
        self.assertEqual(self.current(), gen3, "a failed replace removed the overlay")


# ---------------------------------------------------------------------------
# Retract
# ---------------------------------------------------------------------------

class TestRetract(_OverlaySandbox):
    def test_overlay_removed_only_after_the_es_delete_succeeds(self):
        self.features[DS] = _polygon_features()
        _, rc = self.run_job()
        self.assertEqual(rc, 0, dj.read_state(JOB))
        gen = self.current()
        self.assertIsNotNone(gen)
        self.records.clear()                         # gone from discovery → retractable
        self.recording_overlay_remover()

        # The delete fails: the overlay must still be serving.
        real = self._run_index

        def retract_dies(argv):
            rc, out = real(argv)
            return (3, out) if "--retract" in argv else (rc, out)
        self.steps.run_index = retract_dies
        self.new_job(JOB2)
        _, rc = self.run_job("retract", job=JOB2)
        self.assertEqual(rc, 1)
        self.assert_failed("index_error", "--retract exited 3", job=JOB2)
        self.assertEqual(self.current(), gen, "a failed retract removed the overlay")
        self.assertEqual([c for c in self.calls if c[0] == "remove_overlay"], [])
        r = self.reader()
        self.assertAlmostEqual(shape(r.get("whg:12:shire_0")).area, AREA["shire"], places=9)
        # (The fake really did delete — the rc is what the runner sees.)
        self.assertNotIn("whg:12:shire", self.es.docs["places_x"])

        # The delete succeeds: overlay CURRENT gone, nothing served, pruned.
        self.steps.run_index = real
        self.es.docs["places_x"]["whg:12:shire"] = {"place_id": "whg:12:shire"}
        self.new_job(JOB3)
        _, rc = self.run_job("retract", job=JOB3)
        self.assertEqual(rc, 0, dj.read_state(JOB3))
        st = dj.read_state(JOB3)
        self.assertIn("overlay removed", st["message"])
        self.assertIsNone(self.current())
        self.assertNotIn("whg:12:shire", self.es.docs["places_x"])
        self.assertFalse(self.marker().exists())
        kinds = [c[0] for c in self.calls if c[0] in ("index", "remove_overlay")]
        self.assertEqual(kinds[-2:], ["index", "remove_overlay"], kinds)
        (rm,) = [c for c in self.calls if c[0] == "remove_overlay"]
        self.assertNotIn("whg:12:shire", rm[2], "removed before the ES delete")
        fresh = self.reader()
        self.assertIsNone(fresh.get("whg:12:shire_0"))
        self.assertEqual(fresh.overlay_generations, {})
        self.assertEqual(fresh.get_wkb("osm:r1_0"), self.MAIN["osm:r1_0"])

    def test_retract_of_a_point_dataset_has_no_overlay_to_remove(self):
        self.features[DS] = [_lpf(1, "a", "A", _point(-0.1, 51.5))]
        _, rc = self.run_job()
        self.assertEqual(rc, 0, dj.read_state(JOB))
        self.assertIsNone(self.current())
        self.records.clear()
        self.new_job(JOB2)
        _, rc = self.run_job("retract", job=JOB2)
        self.assertEqual(rc, 0, dj.read_state(JOB2))
        self.assertNotIn("whg:12:a", self.es.docs["places_x"])
        self.assertFalse(go.overlay_dir(self.geom_store, DS).exists())


if __name__ == "__main__":
    unittest.main()
