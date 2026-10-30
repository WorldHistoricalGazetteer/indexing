"""``POST /api/registry/reingest`` with ``namespace = "whg:<id>"`` (place#341, item 5).

``gateway.reingest._ssh`` is replaced by an async fake that records every argv
and plays the Slurm side: ``squeue`` answers from a settable queue state and
``sbatch`` returns a fixed job id. Every assertion about "no ssh happened" is
therefore an assertion about that list being empty — which a request that
reached squeue or sbatch would break.

The plain-namespace path is exercised in the same file so a change to the
dataset path that leaked into it (a token demand, a renamed job) fails here.
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
        app = FastAPI()
        app.include_router(reingest.router)
        self.client = TestClient(app)

    def post(self, namespace, token=None, raw_header=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if raw_header is not None:
            headers["Authorization"] = raw_header
        return self.client.post("/api/registry/reingest",
                                json={"namespace": namespace}, headers=headers)


class TestDatasetTrigger(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {reingest.REINGEST_DATASET_TOKEN_ENV: TOKEN})
        p.start()
        self.addCleanup(p.stop)

    def test_whg_12_submits_once_then_409_with_the_live_job_id(self):
        r = self.post("whg:12", token=TOKEN)
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json(), {"job_id": "4242", "status": "queued"})
        (sbatch,) = self.slurm.sbatch_calls()
        self.assertIn("--job-name=reingest-whg-12", sbatch)
        self.assertEqual(sbatch[-1], "whg:12")          # the sbatch script's $1
        self.assertEqual(sbatch[-2], reingest.CRC_SBATCH_SCRIPT)

        # Second trigger while the job is queued: 409, Django adopts the id.
        r2 = self.post("whg:12", token=TOKEN)
        self.assertEqual(r2.status_code, 409, r2.text)
        self.assertEqual(r2.json()["detail"]["job_id"], "4242")
        self.assertEqual(len(self.slurm.sbatch_calls()), 1, "submitted twice")
        # The guard asked squeue for THIS dataset's job name.
        squeues = [a for a in self.slurm.argvs if a[0] == "squeue"]
        self.assertTrue(all("reingest-whg-12" in a for a in squeues))

    def test_whg_1_and_whg_12_are_separate_guards(self):
        self.post("whg:12", token=TOKEN)
        r = self.post("whg:1", token=TOKEN)
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(len(self.slurm.sbatch_calls()), 2)

    def test_bad_token_is_401_and_touches_no_ssh(self):
        r = self.post("whg:12", token="wrong")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.slurm.argvs, [])

    def test_missing_or_non_bearer_header_is_401(self):
        self.assertEqual(self.post("whg:12").status_code, 401)
        self.assertEqual(self.post("whg:12", raw_header=f"Basic {TOKEN}").status_code, 401)
        self.assertEqual(self.post("whg:12", raw_header="Bearer").status_code, 401)
        self.assertEqual(self.slurm.argvs, [])

    def test_unset_token_is_503_fail_closed(self):
        with mock.patch.dict(os.environ, {reingest.REINGEST_DATASET_TOKEN_ENV: ""}):
            r = self.post("whg:12", token=TOKEN)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.slurm.argvs, [])

    def test_malformed_dataset_id_is_400_before_auth_and_ssh(self):
        for bad in ("whg:12;rm", "whg:12 rm", "whg:", "whg:-1", "whg:1.2", "whg:12/..", "whg:abc"):
            with self.subTest(bad=bad):
                r = self.post(bad, token=TOKEN)
                self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.slurm.argvs, [], "a malformed id reached ssh")

    def test_job_name(self):
        self.assertEqual(reingest._job_name_for("whg:12"), "reingest-whg-12")
        self.assertEqual(reingest._job_name_for("gn"), "reingest-gn")
        self.assertEqual(reingest.parse_dataset_namespace("whg:12"), "12")
        self.assertIsNone(reingest.parse_dataset_namespace("whg:12;rm"))
        self.assertIsNone(reingest.parse_dataset_namespace("whg"))


class TestPlainNamespaceUnchanged(_Base):
    def test_plain_namespace_needs_no_token(self):
        # No REINGEST_DATASET_TOKEN in the environment at all.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(reingest.REINGEST_DATASET_TOKEN_ENV, None)
            r = self.post("gn")
        self.assertEqual(r.status_code, 202, r.text)
        (sbatch,) = self.slurm.sbatch_calls()
        self.assertIn("--job-name=reingest-gn", sbatch)
        self.assertEqual(sbatch[-1], "gn")

    def test_plain_namespace_ignores_a_bogus_header(self):
        r = self.post("gn", raw_header="Bearer definitely-wrong")
        self.assertEqual(r.status_code, 202, r.text)

    def test_plain_namespace_409(self):
        self.post("gn")
        r = self.post("gn")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["job_id"], "4242")

    def test_unknown_namespace_still_400(self):
        r = self.post("nope")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.slurm.argvs, [])

    def test_bare_whg_is_not_a_valid_plain_namespace(self):
        # Was never in VALID_NAMESPACES; must not become valid via the dataset path.
        r = self.post("whg")
        self.assertEqual(r.status_code, 400)


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
