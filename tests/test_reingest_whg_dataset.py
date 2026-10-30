"""``POST /api/registry/reingest`` with ``namespace = "whg:<id>"`` (place#341, item 5).

Since 30 Oct 2026 a ``whg:<id>`` request launches a VM-local job
(``gateway.dataset_jobs``) rather than an sbatch; here ``dataset_jobs.spawn``
is replaced by a recorder, and ``gateway.reingest._ssh`` by an async fake that
records every argv and plays the Slurm side (``squeue`` from a settable queue
state, ``sbatch`` returning a fixed id, ``sacct`` from a table). Every
assertion about "no ssh happened" / "nothing was spawned" is an assertion
about those lists being empty — which a request that reached them would break.

The plain-namespace path is exercised in the same file: by default it is 501
(this VM has no Slurm), and with ``REINGEST_PLAIN_VIA_SLURM=1`` the former
ssh → sbatch behaviour is shown to be intact. The ``sacct`` GET path still
serves Slurm-sized job ids (below ``10**12``); local ids are covered in
``tests/test_dataset_jobs_gateway.py``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import dataset_jobs
from gateway import reingest

TOKEN = "s3cret-token"


class _FakeSlurm:
    def __init__(self):
        self.argvs: list[list[str]] = []
        self.active: dict[str, str] = {}      # job name -> job id squeue reports
        self.next_job_id = "4242"
        # job id -> {JobName, State, Start, End, ExitCode, Reason} for sacct.
        self.accounted: dict[str, dict[str, str]] = {}

    async def ssh(self, argv, *, timeout):
        self.argvs.append(list(argv))
        if argv[0] == "squeue":
            name = argv[argv.index("-n") + 1]
            return 0, (self.active.get(name, "") + "\n"), ""
        if argv[0] == "sbatch":
            name = next(a for a in argv if a.startswith("--job-name=")).split("=", 1)[1]
            self.active[name] = self.next_job_id
            return 0, f"Submitted batch job {self.next_job_id}\n", ""
        if argv[0] == "sacct":
            job = argv[argv.index("-j") + 1]
            fmt = next(a for a in argv if a.startswith("--format=")).split("=", 1)[1]
            row = self.accounted.get(job)
            if row is None:
                return 0, "\n", ""
            # Emit the columns in the order ASKED FOR, as sacct does — so a
            # parser that assumes a different order gets the wrong field.
            cols = {"JobID": job, **row}
            return 0, "|".join(cols.get(f, "") for f in fmt.split(",")) + "\n", ""
        raise AssertionError(f"unexpected remote command {argv!r}")

    def account(self, job_id, *, name, state="COMPLETED", start="2026-10-30T10:00:00",
                end="2026-10-30T10:05:00", exit_code="0:0", reason="None"):
        self.accounted[job_id] = {"JobName": name, "State": state, "Start": start,
                                  "End": end, "ExitCode": exit_code, "Reason": reason}

    def sbatch_calls(self):
        return [a for a in self.argvs if a[0] == "sbatch"]


class _Base(unittest.TestCase):
    def setUp(self):
        self.slurm = _FakeSlurm()
        p = mock.patch.object(reingest, "_ssh", self.slurm.ssh)
        p.start()
        self.addCleanup(p.stop)
        # Local dataset jobs: per-test state/log dirs, and a spawn recorder.
        self.jobs_dir = Path(tempfile.mkdtemp(prefix="reingest-jobs-"))
        p = mock.patch.dict(os.environ, {
            dataset_jobs.STATE_DIR_ENV: str(self.jobs_dir / "state"),
            dataset_jobs.LOG_DIR_ENV: str(self.jobs_dir / "logs"),
        })
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(reingest.PLAIN_VIA_SLURM_ENV, None)
        self.spawned: list[tuple[str, str, str]] = []

        def fake_spawn(job_id, dataset_id, action):
            self.spawned.append((job_id, dataset_id, action))
            return {"pid": os.getpid(), "pgid": os.getpid(),
                    "log": str(dataset_jobs.log_path_for(job_id)), "argv": ["fake"]}

        p = mock.patch.object(dataset_jobs, "spawn", fake_spawn)
        p.start()
        self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(reingest.router)
        self.client = TestClient(app)

    def post(self, namespace, token=None, raw_header=None, action=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if raw_header is not None:
            headers["Authorization"] = raw_header
        body = {"namespace": namespace}
        if action is not None:
            body["action"] = action
        return self.client.post("/api/registry/reingest", json=body, headers=headers)


class TestDatasetTrigger(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {reingest.REINGEST_DATASET_TOKEN_ENV: TOKEN})
        p.start()
        self.addCleanup(p.stop)

    def test_whg_12_starts_a_local_job_once_then_409_with_the_live_job_id(self):
        r = self.post("whg:12", token=TOKEN)
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()
        self.assertEqual(set(body), {"job_id", "status"})
        self.assertEqual(body["status"], "queued")
        job_id = body["job_id"]
        self.assertTrue(dataset_jobs.is_local_job_id(job_id), job_id)
        self.assertEqual(self.spawned, [(job_id, "12", "index")])
        self.assertEqual(self.slurm.argvs, [], "a dataset job reached ssh/sbatch")
        self.assertEqual(dataset_jobs.read_state(job_id)["namespace"], "whg:12")

        # Second trigger while the job is queued: 409, Django adopts the id.
        r2 = self.post("whg:12", token=TOKEN)
        self.assertEqual(r2.status_code, 409, r2.text)
        detail = r2.json()["detail"]
        self.assertEqual(detail["job_id"], job_id)
        self.assertEqual(detail["namespace"], "whg:12")
        self.assertIn("error", detail)
        self.assertEqual(len(self.spawned), 1, "spawned twice")

    def test_the_dataset_lock_is_global_across_datasets(self):
        # One VM-local job at a time: whg:1 is refused while whg:12 runs, and
        # the 409 names the job that IS running, not the one asked for.
        first = self.post("whg:12", token=TOKEN).json()["job_id"]
        r = self.post("whg:1", token=TOKEN)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"], {"error": mock.ANY, "job_id": first,
                                              "namespace": "whg:12"})
        self.assertEqual(len(self.spawned), 1)

    def test_action_retract_is_accepted_and_anything_else_is_400(self):
        r = self.post("whg:12", token=TOKEN, action="retract")
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.spawned[-1][2], "retract")
        self.assertEqual(self.post("whg:12", token=TOKEN, action="purge").status_code, 400)
        self.assertEqual(len(self.spawned), 1)

    def test_bad_token_is_401_and_touches_nothing(self):
        r = self.post("whg:12", token="wrong")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.slurm.argvs, [])
        self.assertEqual(self.spawned, [])

    def test_missing_or_non_bearer_header_is_401(self):
        self.assertEqual(self.post("whg:12").status_code, 401)
        self.assertEqual(self.post("whg:12", raw_header=f"Basic {TOKEN}").status_code, 401)
        self.assertEqual(self.post("whg:12", raw_header="Bearer").status_code, 401)
        self.assertEqual(self.slurm.argvs, [])
        self.assertEqual(self.spawned, [])

    def test_unset_token_is_503_fail_closed(self):
        with mock.patch.dict(os.environ, {reingest.REINGEST_DATASET_TOKEN_ENV: ""}):
            r = self.post("whg:12", token=TOKEN)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.slurm.argvs, [])
        self.assertEqual(self.spawned, [])

    def test_malformed_dataset_id_is_400_before_auth_and_launch(self):
        for bad in ("whg:12;rm", "whg:12 rm", "whg:", "whg:-1", "whg:1.2", "whg:12/..", "whg:abc"):
            with self.subTest(bad=bad):
                r = self.post(bad, token=TOKEN)
                self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.slurm.argvs, [], "a malformed id reached ssh")
        self.assertEqual(self.spawned, [], "a malformed id was launched")
        self.assertEqual(dataset_jobs.list_job_ids(), [])

    def test_job_name(self):
        self.assertEqual(reingest._job_name_for("whg:12"), "reingest-whg-12")
        self.assertEqual(reingest._job_name_for("gn"), "reingest-gn")
        self.assertEqual(reingest.parse_dataset_namespace("whg:12"), "12")
        self.assertIsNone(reingest.parse_dataset_namespace("whg:12;rm"))
        self.assertIsNone(reingest.parse_dataset_namespace("whg"))


class TestPlainNamespaceDefault(_Base):
    """Without ``REINGEST_PLAIN_VIA_SLURM`` a plain namespace is 501: the
    gateway VM cannot reach Slurm, and saying so beats a 504 after a 30 s
    ssh timeout."""

    def test_plain_namespace_is_501_and_touches_nothing(self):
        for ns in ("gn", "osm", "un"):
            with self.subTest(ns=ns):
                r = self.post(ns)
                self.assertEqual(r.status_code, 501, r.text)
                self.assertEqual(r.json()["detail"], reingest.PLAIN_NOT_AVAILABLE_DETAIL)
        self.assertEqual(self.slurm.argvs, [])
        self.assertEqual(self.spawned, [])

    def test_plain_namespace_retract_is_400_before_501(self):
        self.assertEqual(self.post("gn", action="retract").status_code, 400)

    def test_unknown_namespace_still_400(self):
        r = self.post("nope")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.slurm.argvs, [])

    def test_bare_whg_is_not_a_valid_plain_namespace(self):
        # Was never in VALID_NAMESPACES; must not become valid via the dataset path.
        r = self.post("whg")
        self.assertEqual(r.status_code, 400)


class TestPlainNamespaceViaSlurm(_Base):
    """``REINGEST_PLAIN_VIA_SLURM=1``: the former ssh → sbatch path, unchanged."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {reingest.PLAIN_VIA_SLURM_ENV: "1"})
        p.start()
        self.addCleanup(p.stop)

    def test_plain_namespace_needs_no_token(self):
        # No REINGEST_DATASET_TOKEN in the environment at all.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(reingest.REINGEST_DATASET_TOKEN_ENV, None)
            r = self.post("gn")
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json(), {"job_id": "4242", "status": "queued"})
        (sbatch,) = self.slurm.sbatch_calls()
        self.assertIn("--job-name=reingest-gn", sbatch)
        self.assertEqual(sbatch[-1], "gn")
        self.assertEqual(sbatch[-2], reingest.CRC_SBATCH_SCRIPT)
        self.assertEqual(self.spawned, [])

    def test_plain_namespace_ignores_a_bogus_header(self):
        r = self.post("gn", raw_header="Bearer definitely-wrong")
        self.assertEqual(r.status_code, 202, r.text)

    def test_plain_namespace_409(self):
        self.post("gn")
        r = self.post("gn")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["job_id"], "4242")
        # Plain 409s are unchanged: no ``namespace`` key was added to them.
        self.assertNotIn("namespace", r.json()["detail"])
        squeues = [a for a in self.slurm.argvs if a[0] == "squeue"]
        self.assertTrue(all("reingest-gn" in a for a in squeues))

    def test_whg_dataset_still_runs_locally_with_the_switch_on(self):
        with mock.patch.dict(os.environ, {reingest.REINGEST_DATASET_TOKEN_ENV: TOKEN}):
            r = self.post("whg:12", token=TOKEN)
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.slurm.sbatch_calls(), [])
        self.assertEqual(len(self.spawned), 1)


# ---------------------------------------------------------------------------
# GET /api/registry/reingest/<job_id>: the ``indexed`` signal
# ---------------------------------------------------------------------------

class TestIndexedSignal(_Base):
    """The marker ``processing.index_namespace`` writes is read back here.
    Its only writer in these tests is the test itself, into a tempdir pointed
    at by ``STAGED_BASE_DIR`` — the same variable the gateway resolves."""

    def setUp(self):
        super().setUp()
        self.staged = Path(tempfile.mkdtemp(prefix="reingest-staged-"))
        p = mock.patch.dict(os.environ, {"STAGED_BASE_DIR": str(self.staged)})
        p.start()
        self.addCleanup(p.stop)

    def marker_path(self, dataset="12"):
        return self.staged / "whg" / "datasets" / dataset / "indexed.json"

    def write_marker(self, dataset="12", *, job_id="4242", count=5, schema=None, raw=None):
        p = self.marker_path(dataset)
        p.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            p.write_bytes(raw)
            return p
        p.write_text(json.dumps({
            "schema": schema or reingest.INDEXED_MARKER_SCHEMA, "dataset_id": dataset,
            "job_id": job_id, "indexed_count": count,
            "indexed_at": "2026-10-30T10:04:59Z"}))
        return p

    def get(self, job_id):
        return self.client.get(f"/api/registry/reingest/{job_id}")

    def test_completed_with_matching_marker_is_indexed_true_with_count(self):
        self.slurm.account("4242", name="reingest-whg-12")
        self.write_marker("12", job_id="4242", count=5)
        r = self.get("4242")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "completed")
        self.assertIs(body["indexed"], True)
        self.assertEqual(body["indexed_count"], 5)
        self.assertEqual(body["started_at"], "2026-10-30T10:00:00Z")
        self.assertIn("message", body)                 # unchanged: null, not omitted
        self.assertIsNone(body["message"])
        # A Slurm-run dataset job carries the ``reason`` key (dataset jobs
        # always do) with no code behind it.
        self.assertIn("reason", body)
        self.assertIsNone(body["reason"])
        (sacct,) = [a for a in self.slurm.argvs if a[0] == "sacct"]
        self.assertIn("JobName", next(a for a in sacct if a.startswith("--format=")))

    def test_completed_with_no_marker_is_indexed_false(self):
        self.slurm.account("4242", name="reingest-whg-12")
        r = self.get("4242")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "completed")   # the job DID exit 0
        self.assertIs(body["indexed"], False)
        self.assertIn("indexed_count", body)            # always present for a dataset job
        self.assertIsNone(body["indexed_count"])

    def test_marker_from_a_different_job_is_indexed_false(self):
        self.slurm.account("4242", name="reingest-whg-12")
        self.write_marker("12", job_id="1111", count=5)
        body = self.get("4242").json()
        self.assertIs(body["indexed"], False)
        self.assertIsNone(body["indexed_count"])
        # Control: the same marker IS a match for the job it names.
        self.slurm.account("1111", name="reingest-whg-12")
        self.assertIs(self.get("1111").json()["indexed"], True)

    def test_array_suffix_is_ignored_when_matching(self):
        self.slurm.account("4242_1", name="reingest-whg-12")
        self.write_marker("12", job_id="4242", count=5)
        self.assertIs(self.get("4242_1").json()["indexed"], True)
        self.slurm.account("4242", name="reingest-whg-12")
        self.write_marker("12", job_id="4242_1", count=5)
        self.assertIs(self.get("4242").json()["indexed"], True)

    def test_corrupt_or_wrong_marker_is_indexed_false_not_an_error(self):
        self.slurm.account("4242", name="reingest-whg-12")
        cases = {
            "truncated json": b'{"schema": "whg-dataset-indexed/1", "job_id": "42',
            "not an object": b"[1, 2, 3]",
            "wrong schema": None,
            "other dataset": None,
            "oversize": b"{" + b" " * (reingest.INDEXED_MARKER_MAX_BYTES + 10) + b"}",
            "count not int": None,
            "no job_id": None,
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                if label == "wrong schema":
                    self.write_marker("12", schema="whg-dataset-indexed/0")
                elif label == "other dataset":
                    self.write_marker("12")
                    data = json.loads(self.marker_path("12").read_text())
                    data["dataset_id"] = "13"
                    self.marker_path("12").write_text(json.dumps(data))
                elif label == "count not int":
                    self.write_marker("12", count="5")
                elif label == "no job_id":
                    self.write_marker("12", job_id=None)
                else:
                    self.write_marker("12", raw=raw)
                r = self.get("4242")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIs(r.json()["indexed"], False)
                self.assertIsNone(r.json()["indexed_count"])
        # Control for the whole table: a well-formed marker reads true.
        self.write_marker("12")
        self.assertIs(self.get("4242").json()["indexed"], True)

    def test_plain_namespace_job_carries_no_indexed_keys(self):
        self.slurm.account("4242", name="reingest-gn")
        r = self.get("4242")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "completed")
        self.assertNotIn("indexed", body)
        self.assertNotIn("indexed_count", body)
        self.assertNotIn("reason", body)
        self.assertEqual(set(body), {"job_id", "status", "started_at", "finished_at", "message"})
        # ...even when a dataset marker happens to name this job id.
        self.write_marker("12", job_id="4242")
        self.assertNotIn("indexed", self.get("4242").json())
        # And a near-miss name is not a dataset job either.
        for name in ("reingest-whg", "reingest-whg-12x", "xreingest-whg-12", "reingest-whg-1-2",
                     "reingest-gn", ""):
            with self.subTest(name=name):
                self.assertIsNone(reingest.dataset_id_from_job_name(name))
        self.assertEqual(reingest.dataset_id_from_job_name("reingest-whg-12"), "12")

    def test_failed_job_status_is_unchanged_and_indexed_follows_the_marker(self):
        self.slurm.account("4242", name="reingest-whg-12", state="FAILED", exit_code="1:0")
        body = self.get("4242").json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["message"], "exit=1:0")
        self.assertIs(body["indexed"], False)
        # A marker naming this job reads true even though the job failed —
        # the two signals are independent, and status is never derived from
        # the marker (the index step may have landed before a later step died).
        self.write_marker("12", job_id="4242", count=3)
        body = self.get("4242").json()
        self.assertEqual(body["status"], "failed")
        self.assertIs(body["indexed"], True)
        self.assertEqual(body["indexed_count"], 3)

    def test_running_dataset_job_reports_indexed_false(self):
        self.slurm.account("4242", name="reingest-whg-12", state="RUNNING", end="Unknown")
        body = self.get("4242").json()
        self.assertEqual(body["status"], "running")
        self.assertIsNone(body["finished_at"])          # "Unknown" → null, as before
        self.assertIs(body["indexed"], False)

    def test_unknown_job_is_still_404(self):
        self.assertEqual(self.get("9999").status_code, 404)
        self.assertEqual(self.get("abc").status_code, 400)

    def test_marker_path_refuses_a_non_digit_id(self):
        with self.assertRaises(ValueError):
            reingest.indexed_marker_path("../12")
        self.assertEqual(reingest.indexed_marker_path("12"), self.marker_path("12"))


if __name__ == "__main__":
    unittest.main()
