"""``gateway.dataset_jobs`` + the ``whg:<id>`` route on top of it (place#341).

The launcher is a plain function the route calls through ``asyncio.to_thread``,
so the tests patch ``dataset_jobs.spawn`` (never ``Popen`` itself) and record
what it was asked to start; one test exercises the real ``spawn`` against a
fake ``Popen`` to pin the argv, the session flag, the cwd and the log handle.
State, lock and log directories are per-test tempdirs set through the same
environment variables the gateway reads.

Every "the job is still active" assertion has its positive control: a lock
with a LIVE pid blocks before the dead-pid one is shown not to.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import dataset_jobs as dj
from gateway import reingest

TOKEN = "s3cret-token"
# The unpatched launcher, for the one test that drives it against a fake Popen
# (``_Base.setUp`` replaces ``dj.spawn`` for everything else).
REAL_SPAWN = dj.spawn


def _dead_pid() -> int:
    """A pid that has exited and been reaped, so ``os.kill(pid, 0)`` raises."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dataset-jobs-"))
        self.state_dir = self.tmp / "state"
        self.log_dir = self.tmp / "logs"
        self.staged = self.tmp / "staged"
        env = {
            dj.STATE_DIR_ENV: str(self.state_dir),
            dj.LOG_DIR_ENV: str(self.log_dir),
            "STAGED_BASE_DIR": str(self.staged),
            reingest.REINGEST_DATASET_TOKEN_ENV: TOKEN,
        }
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(reingest.PLAIN_VIA_SLURM_ENV, None)
        self.spawned: list[tuple[str, str, str]] = []
        self.spawn_pid = os.getpid()            # alive for as long as the test runs

        def fake_spawn(job_id, dataset_id, action):
            self.spawned.append((job_id, dataset_id, action))
            return {"pid": self.spawn_pid, "pgid": self.spawn_pid,
                    "log": str(dj.log_path_for(job_id)), "argv": ["fake"]}

        p = mock.patch.object(dj, "spawn", fake_spawn)
        p.start()
        self.addCleanup(p.stop)
        self.ssh_calls: list[list[str]] = []

        async def no_ssh(argv, *, timeout):
            self.ssh_calls.append(list(argv))
            if argv[0] == "sacct":
                return 0, "\n", ""
            raise AssertionError(f"ssh reached for {argv!r}")

        p = mock.patch.object(reingest, "_ssh", no_ssh)
        p.start()
        self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(reingest.router)
        self.client = TestClient(app)

    def post(self, namespace, token=TOKEN, action=None, raw_header=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if raw_header is not None:
            headers["Authorization"] = raw_header
        body = {"namespace": namespace}
        if action is not None:
            body["action"] = action
        return self.client.post("/api/registry/reingest", json=body, headers=headers)

    def get(self, job_id):
        return self.client.get(f"/api/registry/reingest/{job_id}")

    def write_state(self, job_id, dataset_id="12", **over):
        st = dj.create_state(job_id, dataset_id, over.pop("action", "index"))
        if over:
            st = dj.update_state(job_id, **over)
        return st

    def write_marker(self, dataset="12", *, job_id, count=5):
        p = self.staged / "whg" / "datasets" / dataset / "indexed.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "schema": reingest.INDEXED_MARKER_SCHEMA, "dataset_id": dataset,
            "job_id": job_id, "indexed_count": count, "indexed_at": "2026-10-30T10:04:59Z"}))


# ---------------------------------------------------------------------------
# POST: start, 409, validation
# ---------------------------------------------------------------------------

class TestStart(_Base):
    def test_202_creates_state_lock_and_spawns_once(self):
        r = self.post("whg:12")
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()
        self.assertEqual(body["status"], "queued")
        job_id = body["job_id"]
        self.assertRegex(job_id, r"^\d{13,}$")
        self.assertTrue(dj.is_local_job_id(job_id))
        self.assertGreaterEqual(int(job_id), dj.LOCAL_JOB_ID_MIN)
        self.assertEqual(self.spawned, [(job_id, "12", "index")])
        st = dj.read_state(job_id)
        self.assertEqual((st["namespace"], st["dataset_id"], st["action"], st["status"]),
                         ("whg:12", "12", "index", "queued"))
        self.assertEqual((st["pid"], st["pgid"]), (self.spawn_pid, self.spawn_pid))
        self.assertIsNone(st["reason"])
        lock = dj.read_lock()
        self.assertEqual((lock["job_id"], lock["pid"], lock["namespace"]),
                         (job_id, self.spawn_pid, "whg:12"))
        self.assertEqual(self.ssh_calls, [], "a dataset job touched ssh")

    def test_409_while_active_carries_job_id_and_namespace_for_any_dataset(self):
        first = self.post("whg:12").json()["job_id"]
        for ns in ("whg:12", "whg:1"):
            with self.subTest(ns=ns):
                r = self.post(ns)
                self.assertEqual(r.status_code, 409, r.text)
                detail = r.json()["detail"]
                self.assertEqual(detail["job_id"], first)
                self.assertEqual(detail["namespace"], "whg:12")   # the LIVE job's, not the request's
                self.assertIn("error", detail)
        self.assertEqual(len(self.spawned), 1, "spawned while locked")

    def test_live_lock_blocks_then_dead_lock_is_broken(self):
        # Presence first: a lock + running state with a LIVE pid blocks.
        live = os.getpid()
        self.write_state("1700000000000", status="running", pid=live, pgid=live)
        self.assertTrue(dj.acquire_lock("1700000000000", "whg:12", live))
        r = self.post("whg:12")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["job_id"], "1700000000000")
        self.assertEqual(self.spawned, [])

        # Now the same shape with a DEAD pid: broken, and the old job is
        # reported dead rather than left running.
        dead = _dead_pid()
        dj.update_state("1700000000000", pid=dead, pgid=dead)
        dj.lock_path().unlink()
        dj.acquire_lock("1700000000000", "whg:12", dead)
        r = self.post("whg:12")
        self.assertEqual(r.status_code, 202, r.text)
        new = r.json()["job_id"]
        self.assertNotEqual(new, "1700000000000")
        self.assertEqual(dj.read_lock()["job_id"], new)
        old = dj.read_state("1700000000000")
        self.assertEqual((old["status"], old["reason"], old["message"]),
                         ("failed", "runner_died", "runner died"))

    def test_lock_without_state_is_broken_only_when_stale(self):
        dj.state_dir().mkdir(parents=True)
        # Live holder pid, no state file: held.
        dj.acquire_lock("1700000000000", "whg:9", os.getpid())
        self.assertFalse(dj.lock_is_stale(dj.read_lock()))
        r = self.post("whg:12")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"], {
            "error": mock.ANY, "job_id": "1700000000000", "namespace": "whg:9"})
        # Dead holder pid, no state file: stale.
        dj.lock_path().unlink()
        dj.acquire_lock("1700000000000", "whg:9", _dead_pid())
        self.assertTrue(dj.lock_is_stale(dj.read_lock()))
        self.assertEqual(self.post("whg:12").status_code, 202)

    def test_handover_window_is_not_stale(self):
        # Lock pid dead (the gateway that took it), but the job's state names
        # a live runner pid: NOT stale, so a second POST must not break it.
        live = os.getpid()
        self.write_state("1700000000000", status="running", pid=live, pgid=live)
        dj.acquire_lock("1700000000000", "whg:12", _dead_pid())
        self.assertFalse(dj.lock_is_stale(dj.read_lock()))
        self.assertEqual(self.post("whg:12").status_code, 409)

    def test_action_index_default_retract_allowed_else_400_before_token(self):
        r = self.post("whg:12", action="retract")
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.spawned[-1], (r.json()["job_id"], "12", "retract"))
        self.assertEqual(dj.read_state(r.json()["job_id"])["action"], "retract")
        # An unknown action is a 400 before the token is even looked at...
        for bad in ("purge", "delete", "retract;rm"):
            with self.subTest(bad=bad):
                self.assertEqual(self.post("whg:12", action=bad, token="wrong").status_code, 400)
        # ...while a well-formed one (case/space-insensitive, or blank = index)
        # proceeds to the token check, which is what fails here.
        for ok in ("INDEX ", "Retract", "", " "):
            with self.subTest(ok=ok):
                self.assertEqual(self.post("whg:12", action=ok, token="wrong").status_code, 401)
        self.assertEqual(len(self.spawned), 1)

    def test_malformed_id_is_400_before_token_and_spawn(self):
        for bad in ("whg:12;rm", "whg:", "whg:-1", "whg:abc", "whg:12/.."):
            with self.subTest(bad=bad):
                self.assertEqual(self.post(bad, token="wrong").status_code, 400)
        self.assertEqual(self.spawned, [])
        self.assertEqual(dj.list_job_ids(), [])

    def test_token_checks_happen_before_any_state_is_written(self):
        self.assertEqual(self.post("whg:12", token="wrong").status_code, 401)
        self.assertEqual(self.post("whg:12", token=None).status_code, 401)
        with mock.patch.dict(os.environ, {reingest.REINGEST_DATASET_TOKEN_ENV: ""}):
            self.assertEqual(self.post("whg:12").status_code, 503)
        self.assertEqual(self.spawned, [])
        self.assertFalse(dj.state_dir().exists())

    def test_spawn_failure_marks_state_failed_and_releases_lock(self):
        quiet = logging.NullHandler()      # the route logs the traceback; keep it out of the run
        reingest.logger.addHandler(quiet)
        reingest.logger.propagate = False
        self.addCleanup(reingest.logger.removeHandler, quiet)
        self.addCleanup(setattr, reingest.logger, "propagate", True)
        with mock.patch.object(dj, "spawn", side_effect=OSError("no python")):
            r = self.post("whg:12")
        self.assertEqual(r.status_code, 502, r.text)
        (jid,) = dj.list_job_ids()
        st = dj.read_state(jid)
        self.assertEqual((st["status"], st["reason"]), ("failed", "runner_died"))
        self.assertIn("spawn failed", st["message"])
        self.assertIsNone(dj.read_lock())
        self.assertEqual(self.post("whg:12").status_code, 202)   # lock really released


# ---------------------------------------------------------------------------
# The real spawn against a fake Popen
# ---------------------------------------------------------------------------

class TestSpawnArgv(_Base):
    def test_argv_session_cwd_env_and_log(self):
        popen_calls = []

        class FakeProc:
            pid = 4321

            def wait(self):
                return 0

        def fake_popen(argv, **kw):
            popen_calls.append((argv, kw))
            return FakeProc()

        dj.create_state("1700000000000", "12", "index")
        with mock.patch.dict(os.environ, {dj.JOB_PYTHON_ENV: "/opt/py/bin/python"}):
            info = REAL_SPAWN("1700000000000", "12", "retract", popen=fake_popen,
                              which=lambda name: f"/usr/bin/{name}")
        self.assertEqual((info["pid"], info["pgid"]), (4321, 4321))
        ((argv, kw),) = popen_calls
        self.assertEqual(argv[:6], ["nice", "-n", "19", "ionice", "-c", "3"])
        self.assertEqual(argv[6:], ["/opt/py/bin/python", "-m", "processing.local_dataset_job",
                                    "--job-id", "1700000000000", "--dataset", "12",
                                    "--action", "retract"])
        self.assertIs(kw["start_new_session"], True)
        self.assertEqual(kw["cwd"], str(dj.REPO_ROOT))
        self.assertEqual(kw["stderr"], subprocess.STDOUT)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(Path(kw["stdout"].name), self.log_dir / "dataset-job-1700000000000.log")
        self.assertEqual(info["log"], str(self.log_dir / "dataset-job-1700000000000.log"))
        self.assertTrue(kw["env"]["PYTHONPATH"].startswith(str(dj.REPO_ROOT)))
        self.assertIn("ELASTIC_PASS_FILE", kw["env"])

    def test_missing_ionice_is_dropped_and_default_python_is_sys_executable(self):
        os.environ.pop(dj.JOB_PYTHON_ENV, None)
        argv = dj.build_argv("1700000000000", "12", "index",
                             which=lambda name: "/usr/bin/nice" if name == "nice" else None)
        self.assertEqual(argv[:3], ["nice", "-n", "19"])
        self.assertNotIn("ionice", argv)
        self.assertEqual(argv[3], sys.executable)


# ---------------------------------------------------------------------------
# GET: local state, runner-died, marker, reason
# ---------------------------------------------------------------------------

class TestLocalStatus(_Base):
    def test_running_state_reads_as_is_with_reason_null(self):
        self.write_state("1700000000000", status="running", pid=os.getpid(), pgid=os.getpid(),
                         started_at="2026-10-30T10:00:00Z")
        r = self.get("1700000000000")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["started_at"], "2026-10-30T10:00:00Z")
        self.assertIsNone(body["finished_at"])
        self.assertIsNone(body["reason"])
        self.assertIs(body["indexed"], False)
        self.assertIsNone(body["indexed_count"])
        self.assertEqual(set(body), {"job_id", "status", "started_at", "finished_at",
                                     "message", "indexed", "indexed_count", "reason"})
        self.assertEqual([a for a in self.ssh_calls if a[0] == "sacct"], [],
                         "a local id was looked up in sacct")

    def test_runner_died_reads_as_failed(self):
        dead = _dead_pid()
        self.write_state("1700000000000", status="running", pid=dead, pgid=dead)
        dj.acquire_lock("1700000000000", "whg:12", dead)
        body = self.get("1700000000000").json()
        self.assertEqual((body["status"], body["reason"], body["message"]),
                         ("failed", "runner_died", "runner died"))
        self.assertIsNotNone(body["finished_at"])
        self.assertIsNone(dj.read_lock(), "a dead runner's lock survived")
        # Control: a live pid in the same state is still running.
        self.write_state("1700000000001", status="running", pid=os.getpid(), pgid=os.getpid())
        self.assertEqual(self.get("1700000000001").json()["status"], "running")

    def test_queued_with_no_pid_dies_after_the_spawn_grace(self):
        self.write_state("1700000000000")   # queued, pid None, created now
        self.assertEqual(self.get("1700000000000").json()["status"], "queued")
        dj.update_state("1700000000000", created_at="2026-01-01T00:00:00Z")
        self.assertEqual(self.get("1700000000000").json()["reason"], "runner_died")

    def test_failed_reason_passes_through_and_completed_has_null_reason(self):
        self.write_state("1700000000000", status="failed", reason="areal_geometry",
                         message="areal geometry needs the geom-store merge; index off-VM",
                         finished_at="2026-10-30T10:05:00Z")
        body = self.get("1700000000000").json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["reason"], "areal_geometry")
        self.assertIn("off-VM", body["message"])
        self.write_state("1700000000001", status="completed", message="indexed 3 docs")
        body = self.get("1700000000001").json()
        self.assertEqual((body["status"], body["reason"]), ("completed", None))

    def test_every_reason_code_is_writable_and_an_unknown_one_is_not(self):
        self.write_state("1700000000000")
        for code in sorted(dj.REASONS):
            dj.update_state("1700000000000", status="failed", reason=code)
            self.assertEqual(self.get("1700000000000").json()["reason"], code)
        with self.assertRaises(ValueError):
            dj.update_state("1700000000000", status="failed", reason="made_up")
        # A non-failed status clears the reason.
        dj.update_state("1700000000000", status="completed")
        self.assertIsNone(dj.read_state("1700000000000")["reason"])

    def test_indexed_follows_the_marker_matched_on_this_job(self):
        self.write_state("1700000000000", status="completed")
        self.write_marker("12", job_id="1700000000000", count=7)
        body = self.get("1700000000000").json()
        self.assertIs(body["indexed"], True)
        self.assertEqual(body["indexed_count"], 7)
        # Another job's marker is not this job's.
        self.write_marker("12", job_id="1700000000001", count=7)
        self.assertIs(self.get("1700000000000").json()["indexed"], False)
        # Failed status + matching marker: independent signals, as before.
        self.write_marker("12", job_id="1700000000000", count=7)
        dj.update_state("1700000000000", status="failed", reason="index_error")
        body = self.get("1700000000000").json()
        self.assertEqual(body["status"], "failed")
        self.assertIs(body["indexed"], True)

    def test_unknown_local_id_is_404_and_small_ids_use_sacct(self):
        self.assertEqual(self.get("1700000000000").status_code, 404)
        self.assertEqual(self.get("4242").status_code, 404)       # sacct answered nothing
        self.assertTrue(any(a[0] == "sacct" for a in self.ssh_calls))
        self.assertEqual(self.get("abc").status_code, 400)

    def test_state_survives_a_fresh_module_import(self):
        job_id = self.post("whg:12").json()["job_id"]
        dj.update_state(job_id, status="running", started_at="2026-10-30T10:00:00Z")
        importlib.reload(dj)           # the gateway restarted: nothing in memory
        self.assertEqual(dj.read_state(job_id)["status"], "running")
        self.assertEqual(dj.active_job()["job_id"], job_id)
        r = self.get(job_id)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "running")
        # The reloaded module still honours the lock: a second POST is 409.
        self.assertEqual(self.post("whg:1").status_code, 409)


class TestPlainNamespace(_Base):
    def test_plain_namespace_is_501_without_slurm_switch(self):
        r = self.post("gn", token=None)
        self.assertEqual(r.status_code, 501, r.text)
        self.assertEqual(r.json()["detail"], reingest.PLAIN_NOT_AVAILABLE_DETAIL)
        self.assertEqual(self.ssh_calls, [])
        self.assertEqual(self.spawned, [])
        self.assertEqual(dj.list_job_ids(), [])

    def test_plain_namespace_rejects_retract_before_501(self):
        self.assertEqual(self.post("gn", token=None, action="retract").status_code, 400)
        self.assertEqual(self.post("gn", token=None, action="index").status_code, 501)

    def test_unknown_namespace_is_still_400(self):
        self.assertEqual(self.post("nope", token=None).status_code, 400)
        self.assertEqual(self.post("whg", token=None).status_code, 400)


if __name__ == "__main__":
    unittest.main()
