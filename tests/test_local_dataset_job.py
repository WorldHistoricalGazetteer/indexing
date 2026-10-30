"""``processing.local_dataset_job`` — the VM-local runner (place#341).

Everything that would touch the network, Slurm or a real process is injected
through :class:`local_dataset_job.Steps`:

* **discovery** answers from a dict of records;
* **staging** is the real LPF → staged-doc mapping (``whg_places.
  lpf_feature_to_staged_doc`` + ``write_staged_place_doc``) over hand-built
  features, with a real ``GeomStoreWriter`` on the private per-run directory
  the runner hands it — so ``has_geom`` / ``bounds`` come from
  ``enrich_geometry`` exactly as production stages them;
* **index_namespace** runs in-process against the evaluating ``FakeES`` from
  ``tests.test_index_dataset`` (dry run and execute alike), so the marker the
  gateway reads is written by the real code path and never by the test;
* **killpg / setrlimit / alarm / _exit** are recorders.

Paths are per-test tempdirs through the same environment variables the code
reads; ``assert_sandboxed`` refuses to run against real storage.
"""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

try:
    from ._sandbox import assert_sandboxed
except ImportError:  # pragma: no cover
    from _sandbox import assert_sandboxed

from gateway import dataset_jobs as dj
from processing import index_namespace as ixn
from processing import local_dataset_job as ldj
from tests.test_index_dataset import FakeES, _bulk_with_errors, _fake_bulk

JOB = "1700000000000"
DS = "12"


def _lpf(fid, src_id, title, geometry, *, ccodes=None, names=("Alpha",)):
    props = {"src_id": src_id, "title": title,
             "names": [{"toponym": n} for n in names]}
    if ccodes is not None:
        props["ccodes"] = ccodes
    return {"id": fid, "type": "Feature", "properties": props, "geometry": geometry}


def _point(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _multipoint(*coords):
    return {"type": "MultiPoint", "coordinates": [list(c) for c in coords]}


def _polygon(w, s, e, n):
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


PUBLISHED = {"id": 12, "title": "Twelve", "place_count": 3, "dataset_status": "published",
             "lifecycle_state": "published", "public": True, "authority": False,
             "ds_status": "indexed", "owner_id": 7, "description": "d"}


class _FakeCountryIndex:
    """Every point is in GB; the real ``resolve_ccodes_for_doc_exact`` calls this."""

    def __init__(self):
        self.calls = 0

    def ccodes_for(self, geom, snap_tol_deg=0.0):
        self.calls += 1
        return ["GB"], False


class _RunnerSandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_sandboxed()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="local-job-"))
        self.staged = self.tmp / "staged"
        self.geom_staging = self.tmp / "geom-staging"
        env = {
            "STAGED_BASE_DIR": str(self.staged),
            "GEOM_STORE_STAGING_DIR": str(self.geom_staging),
            dj.STATE_DIR_ENV: str(self.tmp / "state"),
            dj.LOG_DIR_ENV: str(self.tmp / "logs"),
        }
        for target in (mock.patch.dict(os.environ, env),
                       mock.patch.object(ixn, "STAGED_BASE_DIR", str(self.staged)),
                       mock.patch.object(ixn.es_helpers, "bulk", _fake_bulk),
                       mock.patch.object(ixn, "_refresh_registry_aggregates", lambda ns: None)):
            target.start()
            self.addCleanup(target.stop)
        os.environ.pop(ldj.RLIMIT_ENV, None)
        quiet = logging.NullHandler()
        ldj.logger.addHandler(quiet)
        self.addCleanup(ldj.logger.removeHandler, quiet)
        # Runner.run installs real SIGALRM/SIGTERM handlers; put the originals back.
        originals = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGTERM)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in originals.items()])

        self.es = FakeES()
        self.es.docs["places_x"] = {
            "whg:12:old": {"place_id": "whg:12:old", "toponyms": [{"toponym_id": "Old@en"}]},
            "whg:1:a": {"place_id": "whg:1:a"},
        }
        self.es.docs["toponyms_x"] = {
            "Old@en": {"name": "Old", "attestations": ["whg:12:old"], "namespaces": ["whg"],
                       "primary_namespace": "whg"},
        }
        self.records: dict[str, dict] = {}
        self.features: dict[str, list] = {}
        self.calls: list[tuple] = []
        self.country_index = _FakeCountryIndex()
        self.killpg = mock.Mock()
        self.setrlimit = mock.Mock()
        self.alarm = mock.Mock()
        self.exit = mock.Mock()
        self.steps = ldj.Steps(
            discover=self._discover, stage=self._stage, run_index=self._run_index,
            country_index=lambda: self.country_index, killpg=self.killpg,
            setrlimit=self.setrlimit, alarm=self.alarm, exit=self.exit,
        )
        dj.create_state(JOB, DS, "index")

    # -- injected steps --------------------------------------------------------
    def _discover(self, dataset_id):
        self.calls.append(("discover", dataset_id))
        return self.records.get(dataset_id)

    def _stage(self, dataset_id, geom_dir):
        from processing.geom_store import GeomStoreWriter, configure_module_writer
        wp = ldj.load_whg_places()
        geom_dir = Path(geom_dir)
        self.calls.append(("stage", dataset_id, geom_dir, geom_dir.is_dir()))
        wp._truncate_per_dataset_extract(int(dataset_id))
        feats = self.features.get(dataset_id, [])
        written = 0
        seen: set[str] = set()
        with GeomStoreWriter(geom_dir, "whg") as gsw:
            configure_module_writer(gsw)
            try:
                for f in feats:
                    doc = wp.lpf_feature_to_staged_doc(f, dataset_sub_id=int(dataset_id),
                                                       seen_src_ids=seen)
                    wp.write_staged_place_doc(wp.per_dataset_staged_namespace(dataset_id), doc)
                    written += 1
            finally:
                configure_module_writer(None)
        return {"per_dataset": {dataset_id: {"docs_written": written, "features_seen": len(feats),
                                             "docs_skipped": 0, "title": "Twelve"}},
                "geom_store_entries": gsw.count}

    def _run_index(self, argv):
        state = dj.read_state(JOB)
        self.calls.append(("index", list(argv), self.marker().exists(), state["status"]))
        out, err = io.StringIO(), io.StringIO()
        rc = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                ixn.main(list(argv), es_factory=lambda host: self.es)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, out.getvalue() + err.getvalue()

    # -- helpers ----------------------------------------------------------------
    def marker(self):
        return self.staged / "whg" / "datasets" / DS / "indexed.json"

    def final_docs(self):
        p = self.staged / "whg" / "datasets" / DS / "final" / "places.jsonl"
        if not p.exists():
            return None
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def run_job(self, action="index", **kw):
        runner = ldj.Runner(JOB, DS, action, es_host="http://fake:1", steps=self.steps, **kw)
        # The runner logs through ``ldj.logger`` (a NullHandler keeps the
        # last-resort stderr handler quiet) and the snapshot writer prints;
        # both are kept on ``self.stdout`` for a failing test to show.
        self.stdout = io.StringIO()
        with redirect_stdout(self.stdout):
            return runner, runner.run()

    def index_calls(self):
        return [c for c in self.calls if c[0] == "index"]

    def assert_failed(self, reason, message_part=None):
        st = dj.read_state(JOB)
        self.assertEqual(st["status"], "failed", st)
        self.assertEqual(st["reason"], reason, st)
        if message_part:
            self.assertIn(message_part, st["message"])
        self.assertIsNotNone(st["finished_at"])
        self.assertIsNone(dj.read_lock(), "lock not released")
        return st


# ---------------------------------------------------------------------------
# Index: happy path and each fail-closed exit
# ---------------------------------------------------------------------------

class TestIndex(_RunnerSandbox):
    def test_point_dataset_happy_path(self):
        self.records[DS] = dict(PUBLISHED)
        self.features[DS] = [
            _lpf(1, "a", "A", _point(-0.1, 51.5)),                        # no ccodes → BNDA fill
            _lpf(2, "b", "B", _point(2.35, 48.85), ccodes=["FR"]),         # contributor ccodes kept
            _lpf(3, "c", "C", _multipoint((-3.0, 55.0), (-3.001, 55.001))),  # sub-cell, has_geom
        ]
        es_before = json.dumps(self.es.docs, sort_keys=True)
        runner, rc = self.run_job()
        self.assertEqual(rc, 0, dj.read_state(JOB))

        st = dj.read_state(JOB)
        self.assertEqual(st["status"], "completed")
        self.assertIsNone(st["reason"])
        self.assertIn("indexed 3 docs", st["message"])
        self.assertIsNotNone(st["started_at"])
        self.assertIsNotNone(st["finished_at"])
        self.assertEqual((st["pid"], st["pgid"]), (os.getpid(), os.getpgid(0)))
        self.assertIsNone(dj.read_lock())

        # final/places.jsonl: every geometry carries h3_centroid + h3_cover.
        docs = self.final_docs()
        self.assertEqual([d["place_id"] for d in docs], ["whg:12:a", "whg:12:b", "whg:12:c"])
        for d in docs:
            for g in d["geometries"]:
                self.assertTrue(g.get("h3_centroid"), d)
                self.assertTrue(g.get("h3_cover"), d)
                self.assertTrue(g.get("bounds"), d)
                self.assertIn(g["h3_centroid"], g["h3_cover"])
                self.assertIs(g["has_geom"], False)
                self.assertIsNone(g.get("geom_ref"))
        by_id = {d["place_id"]: d for d in docs}
        self.assertEqual(by_id["whg:12:a"]["ccodes"], ["GB"])
        self.assertEqual(by_id["whg:12:b"]["ccodes"], ["FR"])
        self.assertEqual(self.country_index.calls, 2)          # a and c, not b
        self.assertEqual(by_id["whg:12:c"]["geometries"][0]["geom_class"], "point")
        self.assertEqual(len(by_id["whg:12:c"]["geometries"][0]["h3_cover"]), 1)

        # index_namespace: dry run first, then execute, and only then.
        calls = self.index_calls()
        self.assertEqual(len(calls), 2)
        dry, execute = calls
        self.assertNotIn("--execute", dry[1])
        self.assertIn("--replace", dry[1])
        self.assertIn("final", dry[1])
        self.assertNotIn("--allow-missing-h3", dry[1] + execute[1])
        self.assertEqual(execute[1][-4:], ["--replace", "--execute", "--job-id", JOB])
        # Marker absent at both calls (written LAST by index_namespace), state
        # still running while the execute ran.
        self.assertEqual((dry[2], dry[3]), (False, "running"))
        self.assertEqual((execute[2], execute[3]), (False, "running"))
        m = json.loads(self.marker().read_text())
        self.assertEqual((m["job_id"], m["indexed_count"], m["dataset_id"]), (JOB, 3, DS))

        # Live index: whg:12:old replaced by a/b/c; whg:1 untouched.
        self.assertEqual(sorted(self.es.docs["places_x"]),
                         ["whg:12:a", "whg:12:b", "whg:12:c", "whg:1:a"])
        self.assertNotEqual(json.dumps(self.es.docs, sort_keys=True), es_before)
        # LPF names without a lang are staged as ``@und`` and canonicalised to
        # ``name@`` by index_namespace.parse_toponym_id.
        self.assertEqual(self.es.docs["toponyms_x"]["Alpha@"]["attestations"],
                         ["whg:12:a", "whg:12:b", "whg:12:c"])

        # Sidecar written as published with the selection inputs; geom staging gone.
        side = json.loads((self.staged / "whg" / "datasets" / DS / "dataset.json").read_text())
        self.assertEqual(side["dataset_status"], "published")
        self.assertEqual(side["lifecycle_state"], "published")
        self.assertIs(side["public"], True)
        self.assertEqual(side["record_count"], 3)
        self.assertEqual(side["job_id"], JOB)
        (stage_call,) = [c for c in self.calls if c[0] == "stage"]
        self.assertTrue(stage_call[3], "private geom dir did not exist at stage time")
        self.assertEqual(stage_call[2].parent, self.geom_staging)
        self.assertFalse(stage_call[2].exists(), "private geom staging survived")

        # Limits were applied and the alarm armed then cleared.
        self.setrlimit.assert_called_once_with(4 * (1 << 30))
        self.assertEqual([c.args[0] for c in self.alarm.call_args_list], [dj.DEFAULT_TIMEOUT_S, 0])
        self.killpg.assert_not_called()

    def test_areal_geometry_fails_closed_before_any_es_call(self):
        self.records[DS] = dict(PUBLISHED)
        self.features[DS] = [
            _lpf(1, "a", "A", _point(-0.1, 51.5)),
            _lpf(2, "b", "B", _polygon(-1.0, 51.0, -0.9, 51.1)),   # 0.1° — needs the store
        ]
        es_before = json.dumps(self.es.docs, sort_keys=True)
        _, rc = self.run_job()
        self.assertEqual(rc, 1)
        self.assert_failed("areal_geometry", ldj.AREAL_MESSAGE)
        self.assertEqual(self.index_calls(), [], "index_namespace ran")
        self.assertEqual(json.dumps(self.es.docs, sort_keys=True), es_before)
        self.assertIsNone(self.final_docs(), "a half final/ was published")
        self.assertFalse(self.marker().exists())
        # Boundary: a MultiPoint spanning exactly 0.01° is areal too.
        self.features[DS] = [_lpf(3, "c", "C", _multipoint((0.0, 0.0), (0.01, 0.0)))]
        dj.update_state(JOB, status="queued")
        self.run_job()
        self.assert_failed("areal_geometry")

    def test_not_indexable_and_absent_fail_closed_before_staging(self):
        self.records[DS] = dict(PUBLISHED, lifecycle_state="in_review")
        _, rc = self.run_job()
        self.assertEqual(rc, 1)
        self.assert_failed("not_indexable", "not_published")
        self.assertEqual([c for c in self.calls if c[0] == "stage"], [])
        # Django says pending even though the record passes is_indexable.
        self.records[DS] = dict(PUBLISHED, dataset_status="pending")
        dj.update_state(JOB, status="queued")
        self.run_job()
        self.assert_failed("not_indexable", "django_dataset_status")
        # Absent from discovery.
        self.records.clear()
        dj.update_state(JOB, status="queued")
        self.run_job()
        self.assert_failed("not_indexable", "not returned")
        self.assertEqual(self.index_calls(), [])

    def test_discovery_unreachable(self):
        def boom(dataset_id):
            raise ConnectionError("whgazetteer.org: connection refused")
        self.steps.discover = boom
        self.run_job()
        self.assert_failed("discovery_unreachable", "connection refused")

    def test_no_docs_staged(self):
        self.records[DS] = dict(PUBLISHED, place_count=5)
        self.features[DS] = []
        self.run_job()
        self.assert_failed("no_docs_staged", "place_count=5")
        self.assertEqual(self.index_calls(), [])

    def test_stage_errors_map_to_their_codes(self):
        self.records[DS] = dict(PUBLISHED)

        def not_indexable(dataset_id, geom_dir):
            wp = ldj.load_whg_places()
            raise wp.DatasetNotIndexable(["whg:12 not indexable (x)"])
        self.steps.stage = not_indexable
        self.run_job()
        self.assert_failed("not_indexable", "whg:12 not indexable")

        def broken(dataset_id, geom_dir):
            raise RuntimeError("LPF stream truncated")
        self.steps.stage = broken
        dj.update_state(JOB, status="queued")
        self.run_job()
        self.assert_failed("stage_error", "LPF stream truncated")
        self.assertFalse(any(self.geom_staging.glob("reingest-*")), "private dir left behind")

    def test_unclean_dry_run_means_no_execute(self):
        self.records[DS] = dict(PUBLISHED)
        self.features[DS] = [_lpf(1, "a", "A", _point(-0.1, 51.5))]
        real = self._run_index

        def dry_run_reports_uncovered(argv):
            rc, out = real(argv)
            if "--execute" not in argv:
                out = out.replace("1 staged docs, 0 with uncovered geometry",
                                  "1 staged docs, 1 with uncovered geometry")
            return rc, out
        self.steps.run_index = dry_run_reports_uncovered
        es_before = json.dumps(self.es.docs, sort_keys=True)
        self.run_job()
        self.assert_failed("dry_run_unclean", "uncovered")
        self.assertEqual(len(self.index_calls()), 1)
        self.assertEqual(json.dumps(self.es.docs, sort_keys=True), es_before)
        self.assertFalse(self.marker().exists())

    def test_execute_failure_or_missing_marker_is_index_error(self):
        self.records[DS] = dict(PUBLISHED)
        self.features[DS] = [_lpf(1, "a", "A", _point(-0.1, 51.5))]
        # The bulk lands but reports an error: index_namespace exits 0 and
        # writes no marker, which this runner must not read as success.
        with mock.patch.object(ixn.es_helpers, "bulk", _bulk_with_errors):
            self.run_job()
        self.assert_failed("index_error", "no indexed.json")
        self.assertEqual(len(self.index_calls()), 2)
        # A non-zero execute exit.
        real = self._run_index

        def execute_dies(argv):
            rc, out = real(argv)
            return (3, out) if "--execute" in argv else (rc, out)
        self.steps.run_index = execute_dies
        dj.update_state(JOB, status="queued")
        self.run_job()
        self.assert_failed("index_error", "exited 3")

    def test_rlimit_env_override_and_real_apply_rlimit(self):
        self.records[DS] = dict(PUBLISHED, lifecycle_state="draft")   # fails fast after limits
        with mock.patch.dict(os.environ, {ldj.RLIMIT_ENV: "2"}):
            self.run_job()
        self.setrlimit.assert_called_once_with(2 * (1 << 30))
        import resource
        with mock.patch.object(resource, "getrlimit",
                               return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY)), \
                mock.patch.object(resource, "setrlimit") as setr:
            ldj.apply_rlimit(3 * (1 << 30))
        setr.assert_called_once_with(resource.RLIMIT_AS, (3 * (1 << 30), 3 * (1 << 30)))
        # Never above the current hard limit.
        with mock.patch.object(resource, "getrlimit", return_value=(1 << 30, 1 << 30)), \
                mock.patch.object(resource, "setrlimit") as setr:
            ldj.apply_rlimit(3 * (1 << 30))
        setr.assert_called_once_with(resource.RLIMIT_AS, (1 << 30, 1 << 30))

    def test_lock_held_by_another_live_job_is_refused(self):
        dj.create_state("1700000000001", "1", "index")
        dj.update_state("1700000000001", status="running", pid=os.getpid(), pgid=os.getpid())
        dj.acquire_lock("1700000000001", "whg:1", os.getpid())
        self.records[DS] = dict(PUBLISHED)
        self.run_job()
        st = dj.read_state(JOB)
        self.assertEqual((st["status"], st["reason"]), ("failed", "lock_conflict"))
        self.assertEqual(dj.read_lock()["job_id"], "1700000000001", "stole another job's lock")
        self.assertEqual(self.calls, [])


# ---------------------------------------------------------------------------
# Retract
# ---------------------------------------------------------------------------

class TestRetract(_RunnerSandbox):
    def setUp(self):
        super().setUp()
        dj.update_state(JOB, action="retract")
        self.marker().parent.mkdir(parents=True, exist_ok=True)
        self.marker().write_text(json.dumps({
            "schema": ixn.INDEXED_MARKER_SCHEMA, "dataset_id": DS, "job_id": "1600000000000",
            "indexed_count": 1, "indexed_at": "2026-01-01T00:00:00Z"}))

    def test_allowed_when_absent_then_refused_while_published(self):
        # POSITIVE CONTROL FIRST: the dataset is gone from discovery.
        self.records.clear()
        _, rc = self.run_job("retract")
        self.assertEqual(rc, 0, dj.read_state(JOB))
        st = dj.read_state(JOB)
        self.assertEqual((st["status"], st["reason"]), ("completed", None))
        self.assertIn("retracted whg:12", st["message"])
        (call,) = self.index_calls()
        self.assertIn("--retract", call[1])
        self.assertIn("--execute", call[1])
        self.assertNotIn("--replace", call[1])
        self.assertEqual(sorted(self.es.docs["places_x"]), ["whg:1:a"])
        self.assertEqual(self.es.docs["toponyms_x"]["Old@en"]["attestations"], [])
        self.assertFalse(self.marker().exists(), "retract left the marker")
        side = json.loads((self.staged / "whg" / "datasets" / DS / "dataset.json").read_text())
        self.assertEqual(side["dataset_status"], ldj.RETRACTED_STATUS)
        self.assertNotEqual(side["dataset_status"], "published")
        self.assertEqual([c for c in self.calls if c[0] == "stage"], [])

        # NOW the refusal: present and indexable.
        self.records[DS] = dict(PUBLISHED)
        dj.update_state(JOB, status="queued")
        self.es.docs["places_x"]["whg:12:old"] = {"place_id": "whg:12:old"}
        es_before = json.dumps(self.es.docs, sort_keys=True)
        _, rc = self.run_job("retract")
        self.assertEqual(rc, 1)
        self.assert_failed("still_published", "still published")
        self.assertEqual(len(self.index_calls()), 1, "index_namespace ran for the refusal")
        self.assertEqual(json.dumps(self.es.docs, sort_keys=True), es_before)

    def test_present_but_withdrawn_is_retractable(self):
        self.records[DS] = dict(PUBLISHED, lifecycle_state="withdrawn")
        _, rc = self.run_job("retract")
        self.assertEqual(rc, 0, dj.read_state(JOB))
        self.assertNotIn("whg:12:old", self.es.docs["places_x"])
        side = json.loads((self.staged / "whg" / "datasets" / DS / "dataset.json").read_text())
        self.assertEqual(side["lifecycle_state"], "withdrawn")
        self.assertIn("not_published", side["retract_reason"])

    def test_discovery_unreachable_refuses_to_retract(self):
        def boom(dataset_id):
            raise TimeoutError("read timed out")
        self.steps.discover = boom
        self.run_job("retract")
        self.assert_failed("discovery_unreachable")
        self.assertEqual(self.index_calls(), [])
        self.assertIn("whg:12:old", self.es.docs["places_x"])


# ---------------------------------------------------------------------------
# Timeout, SIGTERM, --kill
# ---------------------------------------------------------------------------

class TestSignals(_RunnerSandbox):
    def test_alarm_marks_timeout_releases_lock_and_kills_the_group(self):
        dj.update_state(JOB, status="running", pid=os.getpid(), pgid=os.getpgid(0))
        dj.acquire_lock(JOB, "whg:12", os.getpid())
        runner = ldj.Runner(JOB, DS, "index", es_host="http://fake:1", steps=self.steps,
                            timeout_s=7)
        runner.phase = "index-execute"
        runner.on_alarm(signal.SIGALRM, None)
        st = self.assert_failed("timeout", "timeout after 7s")
        self.assertIn("index-execute", st["message"])
        self.killpg.assert_called_once_with(os.getpgid(0), signal.SIGTERM)
        self.exit.assert_called_once_with(124)
        # Re-entry is a no-op (the group kill re-signals us).
        runner.on_alarm(signal.SIGALRM, None)
        self.killpg.assert_called_once()

    def test_sigterm_marks_killed_unless_already_terminal(self):
        dj.update_state(JOB, status="running", pid=os.getpid(), pgid=os.getpgid(0))
        runner = ldj.Runner(JOB, DS, "index", es_host="http://fake:1", steps=self.steps)
        runner.on_term(signal.SIGTERM, None)
        self.assert_failed("killed")
        self.exit.assert_called_once_with(143)
        # A terminal state already recorded (by --kill) is not overwritten.
        dj.update_state(JOB, status="failed", reason="timeout", message="timeout after 1s")
        runner2 = ldj.Runner(JOB, DS, "index", es_host="http://fake:1", steps=self.steps)
        runner2.on_term(signal.SIGTERM, None)
        self.assertEqual(dj.read_state(JOB)["reason"], "timeout")

    def test_kill_cli_with_a_fake_pgid(self):
        dj.update_state(JOB, status="running", pid=424242, pgid=424242)
        dj.acquire_lock(JOB, "whg:12", 424242)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ldj.main(["--kill", JOB], steps=self.steps)
        self.assertEqual(rc, 0, out.getvalue())
        self.killpg.assert_called_once_with(424242, signal.SIGTERM)
        st = self.assert_failed("killed", "pgid 424242")
        self.assertIsNotNone(st["finished_at"])
        # Already terminal → 1; unknown → 2; neither signals anything.
        self.killpg.reset_mock()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ldj.main(["--kill", JOB], steps=self.steps), 1)
            self.assertEqual(ldj.main(["--kill", "1700000000009"], steps=self.steps), 2)
        self.killpg.assert_not_called()
        # A gone process group is tolerated.
        dj.create_state("1700000000002", DS, "index")
        dj.update_state("1700000000002", status="running", pid=424243, pgid=424243)
        self.steps.killpg = mock.Mock(side_effect=ProcessLookupError)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ldj.main(["--kill", "1700000000002"], steps=self.steps), 0)
        self.assertEqual(dj.read_state("1700000000002")["reason"], "killed")

    def test_main_refuses_without_a_state_file(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = ldj.main(["--job-id", "1700000000009", "--dataset", DS], steps=self.steps)
        self.assertEqual(rc, 2)
        self.assertIn("no state file", err.getvalue())
        self.assertEqual(self.calls, [])


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    CLEAN = ("ES 9 @ x  mode=DRY-RUN  REPLACE  dataset=whg:12 prefix='whg:12:'\n"
             "[gate] whg:12 indexable=True (dataset_status='published')\n"
             "[places] scanned 1,234 staged docs, 0 with uncovered geometry\n"
             "[places] docs to index: 1,234\n")

    def test_dry_run_verdict(self):
        self.assertEqual(ldj.dry_run_verdict(0, self.CLEAN), (True, "1234 docs, 0 uncovered", 1234))
        cases = {
            "nonzero exit": (2, self.CLEAN, "exited 2"),
            "no scan line": (0, "[gate] whg:12 indexable=True (x)\n", "no scan line"),
            "refusal announced": (0, self.CLEAN + "[gate] (dry-run) --execute would REFUSE: x\n",
                                  "would refuse"),
            "gate not ok": (0, self.CLEAN.replace("indexable=True", "indexable=False"),
                            "not report the dataset indexable"),
            "zero docs": (0, self.CLEAN.replace("1,234", "0"), "0 docs"),
            "uncovered": (0, self.CLEAN.replace("0 with uncovered", "3 with uncovered"),
                          "3 of 1234 docs have uncovered"),
        }
        for label, (rc, out, detail) in cases.items():
            with self.subTest(label=label):
                clean, why, _ = ldj.dry_run_verdict(rc, out)
                self.assertFalse(clean)
                self.assertIn(detail, why)

    def test_areal_geometries(self):
        doc = {"geometries": [
            {"has_geom": True, "bounds": [0, 0, 0.005, 0.005]},            # sub-cell
            {"has_geom": True, "bounds": [0, 0, 0.01, 0.0], "geometry_index": 7},
            {"has_geom": False, "bounds": [0, 0, 5, 5]},                   # a point-class hull
            {"has_geom": True, "bounds": [179.9, 0, -179.9, 1]},           # antimeridian → unknown
            {"has_geom": True},                                           # no bounds → unknown
        ]}
        self.assertEqual(ldj.areal_geometries(doc), [(7, 0.01), (3, None), (4, None)])
        self.assertEqual(ldj.areal_geometries({"geometries": None}), [])

    def test_index_argv_never_allows_missing_h3(self):
        dry = ldj.index_argv("12", "http://es", execute=False)
        ex = ldj.index_argv("12", "http://es", execute=True, job_id=JOB)
        rt = ldj.index_argv("12", "http://es", execute=True, job_id=JOB, retract=True)
        for argv in (dry, ex, rt):
            self.assertNotIn("--allow-missing-h3", argv)
            self.assertEqual(argv[:4], ["--namespace", "whg", "--dataset", "12"])
        self.assertNotIn("--execute", dry)
        self.assertEqual(ex[-3:], ["--execute", "--job-id", JOB])
        self.assertIn("--retract", rt)
        self.assertNotIn("--replace", rt)
        with self.assertRaises(ValueError):
            ldj.index_argv("12;rm", "http://es", execute=False)

    def test_job_failed_rejects_unknown_reason(self):
        with self.assertRaises(ValueError):
            ldj.JobFailed("nope", "x")
        self.assertEqual(ldj.JobFailed("timeout", "x").reason, "timeout")


if __name__ == "__main__":
    unittest.main()
