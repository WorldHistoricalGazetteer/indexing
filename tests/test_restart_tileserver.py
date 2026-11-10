"""Tests for processing.generate_tiles.restart_tileserver (NYC1 contract, place#335).

The host has no sudo, no systemd units and no tiler: restart is one script,
``cd /srv && bash restart_services.sh``, whose exit status is the health check
(it blocks until /styles/whg-context/style.json serves layers). subprocess.run is
mocked so the constructed command can be inspected without touching the network.
"""
from __future__ import annotations

import unittest
from unittest import mock

from processing import generate_tiles


def _mock_run(stdout="", stderr="", returncode=0):
    m = mock.MagicMock()
    m.stdout, m.stderr, m.returncode = stdout, stderr, returncode
    return m


class TestRestartTileserverCommand(unittest.TestCase):
    def _capture(self, **kwargs):
        captured = []
        with mock.patch.object(generate_tiles.subprocess, "run",
                               side_effect=lambda cmd, **k: captured.append(cmd) or _mock_run()), \
             mock.patch.object(generate_tiles, "_running_on_proxy", return_value=False), \
             mock.patch("processing.settings.TILESERVER_SSH_KEY", ""):
            ok = generate_tiles.restart_tileserver(**kwargs)
        return ok, captured

    def test_single_ssh_session_runs_only_the_restart_script(self):
        ok, calls = self._capture()
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1, "no separate pgrep verify session any more")
        remote = calls[0][-1]
        self.assertIn("cd /srv && bash restart_services.sh", remote)
        for banned in ("sudo", "systemctl", "pkill", "pgrep", "tiler"):
            self.assertNotIn(banned, remote)

    def test_via_proxy_default_is_double_ssh(self):
        _, calls = self._capture()
        self.assertIn("ssh -o BatchMode=yes whgadmin@", calls[0][-1])

    def test_via_proxy_false_is_direct(self):
        _, calls = self._capture(via_proxy=False)
        self.assertIn("whgadmin@", calls[0][-2])
        self.assertNotIn("ssh -o BatchMode=yes whgadmin@", calls[0][-1])

    def test_targets_nyc1_not_london(self):
        _, calls = self._capture(via_proxy=False)
        joined = " ".join(calls[0])
        self.assertIn("168.144.13.214", joined)
        self.assertNotIn("134.209.177.234", joined)

    def test_nonzero_exit_is_failure(self):
        with mock.patch.object(generate_tiles.subprocess, "run",
                               return_value=_mock_run(stderr="style not serving", returncode=1)):
            self.assertFalse(generate_tiles.restart_tileserver())


if __name__ == "__main__":
    unittest.main()
