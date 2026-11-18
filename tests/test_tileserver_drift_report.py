"""snapshot_config_to_repo's git-drift report: skip cleanly when /srv is not a clone (NYC1)."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from processing import update_tileserver_config as u


def _r(stdout="", rc=0, stderr=""):
    m = mock.MagicMock()
    m.stdout, m.returncode, m.stderr = stdout, rc, stderr
    return m


def _run(drift_stdout):
    calls = []

    def fake_ssh(cmd, **kw):
        calls.append(cmd)
        return _r() if cmd.startswith("cat >") else _r(drift_stdout)

    buf = io.StringIO()
    with mock.patch.object(u, "_ssh", side_effect=fake_ssh), redirect_stdout(buf):
        ok = u.snapshot_config_to_repo({"data": {}})
    return ok, buf.getvalue(), calls


class TestDriftReport(unittest.TestCase):
    def test_not_a_clone_skips_with_info_line_only(self):
        ok, out, calls = _run(u.NOT_A_CLONE + "\n")
        self.assertTrue(ok)
        self.assertIn("drift report skipped", out)
        self.assertNotIn("⚠", out)
        self.assertIn("-e /srv/.git", calls[1])  # guarded on the host side

    def test_clone_behind_and_dirty_still_warns(self):
        ok, out, _ = _run(" M configs/config.snapshot.json\n---\n3\n")
        self.assertTrue(ok)
        self.assertIn("3 commit(s) BEHIND", out)
        self.assertIn("1 uncommitted change", out)
        self.assertNotIn("skipped", out)

    def test_clean_clone_is_silent(self):
        ok, out, _ = _run("---\n0\n")
        self.assertTrue(ok)
        self.assertNotIn("⚠", out)
        self.assertNotIn("skipped", out)


if __name__ == "__main__":
    unittest.main()
