"""Tests for processing.generate_tiles.restart_tileserver.

The function is SSH-and-subprocess heavy; we mock subprocess.run so we can
inspect the constructed shell command without hitting the network. The key
invariants we lock in are:

1. pkill -9 -f <pattern> runs BEFORE the systemctl restart for each service,
   because forever-service init scripts on whg-tileboss lose PID tracking
   and ``systemctl restart`` alone is a no-op for the long-lived node child
   (see feedback_tileserver_restart.md memory for the back-story).
2. There's a sleep between pkill and restart so sockets/pids release.
3. The verification step uses ``pgrep -cf <pattern>`` so the parsed output
   tells us how many processes survived per service.
4. The function returns False when any service shows 0 processes after
   restart — a no-process count means the restart didn't take.
"""

from __future__ import annotations

import unittest
from unittest import mock

from processing import generate_tiles


def _mock_run(stdout="", stderr="", returncode=0):
    """Build a mock subprocess.CompletedProcess result."""
    m = mock.MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


class TestRestartTileserverCommand(unittest.TestCase):
    def _capture_cmd(self, **kwargs):
        """Run restart_tileserver with subprocess.run mocked, return the
        cmd argv list that was actually executed."""
        captured = {}

        def fake_run(cmd, **rkwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = rkwargs
            # default healthy: 2 procs per service
            stdout = "\n".join(
                f"'{svc}': 2" for svc in (
                    kwargs.get("services") or ["tiler.service", "tileserver-gl-light.service"]
                )
            )
            return _mock_run(stdout=stdout)

        with mock.patch.object(generate_tiles.subprocess, "run", side_effect=fake_run):
            ok = generate_tiles.restart_tileserver(**kwargs)
        return ok, captured["cmd"]

    def test_via_proxy_is_default_and_double_ssh(self):
        ok, cmd = self._capture_cmd()
        self.assertTrue(ok)
        # outer ssh to the proxy, then inner ssh to the tileserver
        self.assertEqual(cmd[0], "ssh")
        # Last arg is the remote command — it should embed an inner ssh
        self.assertIn("ssh -o BatchMode=yes whgadmin@", cmd[-1])

    def test_pkill_runs_before_systemctl_restart(self):
        _, cmd = self._capture_cmd(
            services=["tileserver-gl-light.service"],
        )
        remote = cmd[-1]
        pkill_pos = remote.find("pkill")
        restart_pos = remote.find("systemctl restart")
        self.assertGreater(pkill_pos, -1, f"pkill not in remote cmd: {remote}")
        self.assertGreater(restart_pos, -1, f"restart not in remote cmd: {remote}")
        self.assertLess(pkill_pos, restart_pos,
                        "pkill must precede systemctl restart")

    def test_sleep_between_pkill_and_restart(self):
        _, cmd = self._capture_cmd(settle_seconds=7)
        remote = cmd[-1]
        # sleep with the requested duration appears between pkill and restart
        pkill_pos = remote.find("pkill")
        sleep_pos = remote.find("sleep 7")
        restart_pos = remote.find("systemctl restart")
        self.assertLess(pkill_pos, sleep_pos)
        self.assertLess(sleep_pos, restart_pos)

    def test_uses_pkill_pattern_matching_node_child_for_known_services(self):
        # tileserver-gl-light.service should pkill on the worker pattern,
        # not on the .service suffix (that wouldn't match the node procs).
        _, cmd = self._capture_cmd(services=["tileserver-gl-light.service"])
        self.assertIn("pkill -9 -f tileserver-gl-light", cmd[-1])
        # tiler.service uses /srv/tiler/tiler.js as the pattern.
        _, cmd = self._capture_cmd(services=["tiler.service"])
        self.assertIn("pkill -9 -f /srv/tiler/tiler.js", cmd[-1])

    def test_verify_step_uses_pgrep_count(self):
        _, cmd = self._capture_cmd(services=["tileserver-gl-light.service"])
        self.assertIn("pgrep -cf tileserver-gl-light", cmd[-1])

    def test_returns_false_when_service_has_zero_processes(self):
        # Mock subprocess.run to return a healthy-looking rc=0 but with
        # one service showing 0 processes alive — restart silently
        # didn't take.
        with mock.patch.object(generate_tiles.subprocess, "run") as run:
            run.return_value = _mock_run(
                stdout="'tiler.service': 2\n'tileserver-gl-light.service': 0",
                returncode=0,
            )
            ok = generate_tiles.restart_tileserver()
        self.assertFalse(ok)

    def test_returns_false_on_subprocess_failure(self):
        with mock.patch.object(generate_tiles.subprocess, "run") as run:
            run.return_value = _mock_run(stderr="permission denied", returncode=1)
            ok = generate_tiles.restart_tileserver()
        self.assertFalse(ok)

    def test_via_proxy_false_skips_outer_ssh(self):
        _, cmd = self._capture_cmd(via_proxy=False)
        # First ssh is direct to the tileserver, no proxy hop
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("whgadmin@", cmd[-2])
        # Remote command does NOT contain a nested ssh
        self.assertNotIn("ssh -o BatchMode=yes whgadmin@", cmd[-1])


if __name__ == "__main__":
    unittest.main()
