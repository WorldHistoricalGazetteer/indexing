"""``generate_tiles.atomic_swap_tileset`` — push under a temporary name, rename, check.

place#166 §4.4: ``rsync --inplace`` tore ``wd`` for 2,387 s on the 10 Oct 2026
swap, so large tilesets are pushed to ``<b>.mbtiles.new`` and swapped in by
rename. Every ssh/rsync here is faked by a recorder on ``subprocess.run`` that
classifies each argv and scripts the host's answers, so the tests assert the
exact SEQUENCE the host sees:

    df → rsync to .new → (mv live .prev && mv .new live; stat) → check → rm .prev

and the two ways it must stop: a failed check (rename .prev back, check again,
discard the rejected file, never remove .prev first) and too little disk
(nothing written at all). Run package-qualified:

    python -m unittest tests.test_tiles_atomic_push
"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from processing import generate_tiles as gt

KEY = "/secrets/tileserver_key"
DIR = "/srv/tileserver/tiles"


def _done(stdout: str = "", returncode: int = 0, stderr: str = ""):
    m = mock.MagicMock()
    m.stdout, m.stderr, m.returncode = stdout, stderr, returncode
    return m


class FakeHost:
    """Scripted tileserver: records every remote step in order."""

    def __init__(self, *, free: int, live_size_after_rename: int | None,
                 rename_rc: int = 0, rsync_rc: int = 0):
        self.free = free
        self.live_size_after_rename = live_size_after_rename
        self.rename_rc = rename_rc
        self.rsync_rc = rsync_rc
        self.steps: list[str] = []
        self.argv: list[list[str]] = []

    def run(self, cmd, **kwargs):
        self.argv.append(list(cmd))
        if cmd[0] == "rsync":
            self.steps.append(f"rsync→{cmd[-1]}")
            return _done("sent 1 bytes", self.rsync_rc)
        assert cmd[0] == "ssh", cmd
        assert cmd[1:3] == ["-i", KEY], "every host call must use the configured key"
        remote = cmd[-1]
        if remote.startswith("df -B1"):
            self.steps.append("df")
            return _done(f"{self.free}\n")
        if ".new" in remote and "mv -f" in remote and "stat -c %s" in remote:
            self.steps.append("rename")
            s = self.live_size_after_rename
            return _done("" if s is None else f"{s}\n", self.rename_rc)
        if ".rejected" in remote and "mv -f" in remote:
            self.steps.append("rollback-rename")
            return _done(f"{self.live_size_after_rename}\n")
        if remote.startswith("rm -f") and remote.endswith(".prev"):
            self.steps.append("rm .prev")
            return _done()
        if remote.startswith("rm -f") and remote.endswith(".rejected"):
            self.steps.append("rm .rejected")
            return _done()
        if "mv -f" in remote and ".prev" in remote:
            self.steps.append("restore-after-failed-rename")
            return _done()
        raise AssertionError(f"unexpected remote command: {remote!r}")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "gn.mbtiles"
        self.path.write_bytes(b"x" * 4096)
        self.size = 4096
        self.margin = 1000

    def swap(self, host: FakeHost, check_results: list[bool], **kw):
        checks: list[int] = []

        def check():
            host.steps.append("check")
            checks.append(1)
            return check_results[len(checks) - 1]

        out = io.StringIO()
        with mock.patch("processing.settings.TILESERVER_SSH_KEY", KEY), \
             mock.patch("processing.settings.TILESERVER_TILES_DIR", DIR), \
             mock.patch.object(gt.shutil, "which", return_value="/usr/bin/rsync"), \
             mock.patch.object(gt.subprocess, "run", side_effect=host.run), \
             redirect_stdout(out):
            ok = gt.atomic_swap_tileset(self.path, check=check,
                                        free_margin=self.margin, **kw)
        return ok, out.getvalue()


class TestHappyPath(_Base):
    def test_sequence_push_new_rename_check_cleanup(self):
        host = FakeHost(free=self.size + self.margin, live_size_after_rename=self.size)
        ok, out = self.swap(host, [True])
        self.assertTrue(ok)
        self.assertEqual(host.steps, ["df", f"rsync→whgadmin@134.209.177.234:{DIR}/gn.mbtiles.new",
                                      "rename", "check", "rm .prev"])
        # the rsync wrote the temporary name, in place, never the live file
        rsync = next(a for a in host.argv if a[0] == "rsync")
        self.assertIn("--inplace", rsync)
        self.assertTrue(rsync[-1].endswith("/gn.mbtiles.new"))
        # the rename is ONE shell: live → .prev before .new → live, then stat
        rename = next(a[-1] for a in host.argv if a[0] == "ssh" and "stat -c %s" in a[-1] and ".rejected" not in a[-1])
        i_prev = rename.index("mv -f gn.mbtiles gn.mbtiles.prev")
        i_new = rename.index("mv -f gn.mbtiles.new gn.mbtiles")
        self.assertLess(i_prev, i_new)
        self.assertIn(f"cd {DIR}", rename)
        self.assertIn("test -f gn.mbtiles.new", rename)
        self.assertIn("4,096 B (new file)", out)

    def test_cleanup_only_after_check_passes(self):
        host = FakeHost(free=10 ** 9, live_size_after_rename=self.size)
        self.swap(host, [True])
        self.assertLess(host.steps.index("check"), host.steps.index("rm .prev"))


class TestRollback(_Base):
    def test_failed_check_renames_prev_back_and_rechecks(self):
        host = FakeHost(free=10 ** 9, live_size_after_rename=self.size)
        ok, out = self.swap(host, [False, True])
        self.assertFalse(ok)
        self.assertEqual(host.steps, ["df", f"rsync→whgadmin@134.209.177.234:{DIR}/gn.mbtiles.new",
                                      "rename", "check", "rollback-rename", "check", "rm .rejected"])
        self.assertNotIn("rm .prev", host.steps)
        rb = next(a[-1] for a in host.argv if a[0] == "ssh" and ".rejected" in a[-1] and "mv -f" in a[-1])
        self.assertLess(rb.index("mv -f gn.mbtiles gn.mbtiles.rejected"),
                        rb.index("mv -f gn.mbtiles.prev gn.mbtiles"))
        self.assertIn("test -f gn.mbtiles.prev", rb)
        self.assertIn("check after rollback: OK", out)

    def test_failed_check_then_failed_recheck_is_reported(self):
        host = FakeHost(free=10 ** 9, live_size_after_rename=self.size)
        ok, out = self.swap(host, [False, False])
        self.assertFalse(ok)
        self.assertIn("restore from the /ix1 backup", out)

    def test_rename_size_mismatch_is_a_failure_before_any_restart(self):
        host = FakeHost(free=10 ** 9, live_size_after_rename=self.size - 1)
        ok, out = self.swap(host, [True])
        self.assertFalse(ok)
        self.assertNotIn("check", host.steps)
        self.assertNotIn("rm .prev", host.steps)
        self.assertEqual(host.steps[-1], "restore-after-failed-rename")
        self.assertIn("not the pushed one", out)

    def test_failed_push_stops_before_rename(self):
        host = FakeHost(free=10 ** 9, live_size_after_rename=self.size, rsync_rc=23)
        ok, _ = self.swap(host, [True])
        self.assertFalse(ok)
        self.assertEqual(host.steps, ["df", f"rsync→whgadmin@134.209.177.234:{DIR}/gn.mbtiles.new"])


class TestDiskRefusal(_Base):
    def test_refuses_when_free_is_below_size_plus_margin(self):
        host = FakeHost(free=self.size + self.margin - 1, live_size_after_rename=self.size)
        ok, out = self.swap(host, [True])
        self.assertFalse(ok)
        self.assertEqual(host.steps, ["df"], "nothing may be written after a refusal")
        self.assertIn("REFUSED", out)
        self.assertIn(f"free {self.size + self.margin - 1:,} B", out)
        self.assertIn(f"= {self.size + self.margin:,} B", out)
        self.assertIn("1 B short", out)

    def test_exactly_enough_is_allowed(self):
        host = FakeHost(free=self.size + self.margin, live_size_after_rename=self.size)
        ok, _ = self.swap(host, [True])
        self.assertTrue(ok)

    def test_unmeasurable_df_is_a_refusal_not_a_pass(self):
        host = FakeHost(free=10 ** 9, live_size_after_rename=self.size)
        host.free = "not a number"  # df answered with something unparseable
        ok, out = self.swap(host, [True])
        self.assertFalse(ok)
        self.assertEqual(host.steps, ["df"])
        self.assertIn("could not be measured", out)

    def test_no_key_refuses_without_touching_the_host(self):
        host = FakeHost(free=10 ** 9, live_size_after_rename=self.size)
        out = io.StringIO()
        with mock.patch("processing.settings.TILESERVER_SSH_KEY", ""), \
             mock.patch.object(gt.subprocess, "run", side_effect=host.run), \
             redirect_stdout(out):
            ok = gt.atomic_swap_tileset(self.path, check=lambda: True)
        self.assertFalse(ok)
        self.assertEqual(host.steps, [])
        self.assertIn("TILESERVER_SSH_KEY", out.getvalue())


class TestModeSelection(_Base):
    def test_auto_picks_by_named_threshold(self):
        with mock.patch.object(gt, "ATOMIC_PUSH_MIN_BYTES", 4096):
            self.assertEqual(gt.push_mode_for(self.path), "rename")
        with mock.patch.object(gt, "ATOMIC_PUSH_MIN_BYTES", 4097):
            self.assertEqual(gt.push_mode_for(self.path), "inplace")
        self.assertEqual(gt.push_mode_for(self.path, "rename"), "rename")
        self.assertEqual(gt.push_mode_for(self.path, "inplace"), "inplace")
        with self.assertRaises(ValueError):
            gt.push_mode_for(self.path, "sideways")

    def test_threshold_is_about_200_mb(self):
        self.assertEqual(gt.ATOMIC_PUSH_MIN_BYTES, 200 * 1024 * 1024)

    def test_redeploy_dispatches_rename_or_inplace(self):
        small = Path(self.tmp.name) / "og.mbtiles"
        small.write_bytes(b"y" * 10)
        calls: list[tuple[str, str]] = []
        with mock.patch.object(gt, "ATOMIC_PUSH_MIN_BYTES", 4096), \
             mock.patch.object(gt, "atomic_swap_tileset",
                               side_effect=lambda p, **k: calls.append(("rename", p.name)) or True), \
             mock.patch.object(gt, "push_mbtiles_to_tileserver",
                               side_effect=lambda p, **k: calls.append(("inplace", p.name)) or True), \
             redirect_stdout(io.StringIO()):
            res = gt.redeploy_tilesets([self.path, small])
            self.assertEqual(calls, [("rename", "gn.mbtiles"), ("inplace", "og.mbtiles")])
            calls.clear()
            gt.redeploy_tilesets([self.path, small], mode="inplace")
            self.assertEqual(calls, [("inplace", "gn.mbtiles"), ("inplace", "og.mbtiles")])
        self.assertEqual(res, {"gn.mbtiles": True, "og.mbtiles": True})

    def test_cli_passes_push_mode_through(self):
        seen = {}

        def fake_redeploy(paths, *, mode):
            seen["paths"], seen["mode"] = [p.name for p in paths], mode
            return {p.name: True for p in paths}

        argv = ["generate_tiles", "--redeploy-only", "--bucket", "gn",
                "--output-dir", self.tmp.name, "--push-mode", "rename"]
        with mock.patch.object(gt, "redeploy_tilesets", side_effect=fake_redeploy), \
             mock.patch.object(gt.sys, "argv", argv), redirect_stdout(io.StringIO()):
            gt.main()
        self.assertEqual(seen, {"paths": ["gn.mbtiles"], "mode": "rename"})


class TestPushDestName(unittest.TestCase):
    def test_dest_name_targets_that_file_and_default_targets_the_directory(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "wd.mbtiles"
        p.write_bytes(b"z")
        targets = []

        def run(cmd, **kw):
            targets.append(cmd[-1])
            return _done("sent 1 bytes")

        with mock.patch("processing.settings.TILESERVER_SSH_KEY", KEY), \
             mock.patch("processing.settings.TILESERVER_TILES_DIR", DIR), \
             mock.patch.object(gt.shutil, "which", return_value="/usr/bin/rsync"), \
             mock.patch.object(gt.subprocess, "run", side_effect=run), \
             redirect_stdout(io.StringIO()):
            self.assertTrue(gt.push_mbtiles_to_tileserver(p, dest_name="wd.mbtiles.new"))
            self.assertTrue(gt.push_mbtiles_to_tileserver(p))
        self.assertEqual(targets, [f"whgadmin@134.209.177.234:{DIR}/wd.mbtiles.new",
                                   f"whgadmin@134.209.177.234:{DIR}/"])


if __name__ == "__main__":
    unittest.main()
