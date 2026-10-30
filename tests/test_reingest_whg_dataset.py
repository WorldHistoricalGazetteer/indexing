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

import os
import unittest
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

    async def ssh(self, argv, *, timeout):
        self.argvs.append(list(argv))
        if argv[0] == "squeue":
            name = argv[argv.index("-n") + 1]
            return 0, (self.active.get(name, "") + "\n"), ""
        if argv[0] == "sbatch":
            name = next(a for a in argv if a.startswith("--job-name=")).split("=", 1)[1]
            self.active[name] = self.next_job_id
            return 0, f"Submitted batch job {self.next_job_id}\n", ""
        raise AssertionError(f"unexpected remote command {argv!r}")

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


if __name__ == "__main__":
    unittest.main()
