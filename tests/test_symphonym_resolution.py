"""Tests for bounded Symphonym model resolution (``gateway/symphonym.py``).

place#242: on the CRC deployment ``hf/final_model.pt`` and ``hf/vocab`` are
symlinks into a hard NFS mount, and ``Path.exists()`` follows a symlink. When
that mount wedged, ``_resolve_model_dir`` blocked in uninterruptible sleep
inside ``app.lifespan``'s "won't crash if unavailable" ``try/except`` — which
catches everything that RAISES, and so caught nothing. Both workers sat in `D`
state and the gateway never listened on 9200.

These tests drive a probe that never returns — a ``threading.Event`` that is
never set, which like a blocked syscall cannot be interrupted — and assert BOTH
that resolution came back within its budget AND that the blocked probe was still
blocked when it did. Without the second half the test would also pass if the
probe had simply finished quickly, which is a check that cannot fail.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gateway import symphonym as sym

_ENV_KEYS = ("SYMPHONYM_MODEL_DIR", "SYMPHONYM_DATA_VERSION", "IX1_BASE", "IX3_BASE")


def _self_contained(root: Path) -> Path:
    """A complete model dir: config + weights + vocab, as `hf/` is off-CRC."""
    (root / "vocab").mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"num_scripts": 20}))
    (root / "model.safetensors").write_bytes(b"\x00")
    (root / "vocab" / "char_vocab.json").write_text("{}")
    return root


def _crc_layout(base: Path, version: str = "7") -> None:
    """The CRC split layout: weights under checkpoints/, vocab under data/."""
    ckpt = base / "models" / "phonetic" / "checkpoints" / f"v{version}"
    vocab = base / "models" / "phonetic" / "data" / f"v{version}" / "vocab"
    ckpt.mkdir(parents=True, exist_ok=True)
    vocab.mkdir(parents=True, exist_ok=True)
    (ckpt / "final_model.pt").write_bytes(b"\x00")
    (vocab / "char_vocab.json").write_text("{}")


class ResolutionCase(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.hf = self.root / "hf"
        self.hf.mkdir()

        saved_exists = sym._exists
        self.addCleanup(lambda: setattr(sym, "_exists", saved_exists))
        self._real_exists = saved_exists

        # Guards are created per candidate on first use, from these module
        # globals. A SHORT timeout so the tests are quick, and a deliberately
        # LONG cooldown: a shared guard would then let one wedged candidate
        # suppress every probe after it, so this setting is what makes the
        # per-candidate separation testable instead of assumed.
        budget = (sym._PROBE_TIMEOUT_S, sym._IO_COOLDOWN_S)
        saved_guards = dict(sym._PROBE_GUARDS)
        sym._PROBE_TIMEOUT_S, sym._IO_COOLDOWN_S = 0.3, 30.0
        sym._PROBE_GUARDS.clear()

        def _restore():
            sym._PROBE_TIMEOUT_S, sym._IO_COOLDOWN_S = budget
            sym._PROBE_GUARDS.clear()
            sym._PROBE_GUARDS.update(saved_guards)
        self.addCleanup(_restore)

        saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}

        def _restore_env():
            for k, v in saved_env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(_restore_env)
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        # Point both CRC bases at empty dirs unless a test says otherwise, so
        # nothing reaches the machine's real /vast or /ix1.
        os.environ["IX3_BASE"] = str(self.root / "no-vast")
        os.environ["IX1_BASE"] = str(self.root / "no-ix1")

    def wedge(self, predicate):
        """Make ``_exists`` block forever for paths matching ``predicate``."""
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.addCleanup(self.release.set)
        real = self._real_exists

        def blocking(path):
            if predicate(Path(path)):
                self.entered.set()
                self.release.wait(30)   # bounded only so a failure can't hang CI
                self.finished.set()
                return True
            return real(path)

        sym._exists = blocking


class TestBoundedResolution(ResolutionCase):
    def test_wedged_hf_is_skipped_and_the_vast_layout_still_resolves(self):
        """The exact place#242 shape: hf/ traverses a wedge, /vast is healthy."""
        vast = self.root / "vast"
        _crc_layout(vast)
        os.environ["IX3_BASE"] = str(vast)
        (self.hf / "config.json").write_text("{}")
        self.wedge(lambda p: p.parent == self.hf and p.name != "config.json")

        started = time.monotonic()
        resolved = sym._resolve_model_dir(repo_hf=self.hf)
        elapsed = time.monotonic() - started

        self.assertTrue(self.entered.wait(1), "the wedged path was never probed")
        self.assertLess(elapsed, 5.0, "resolution was not bounded")
        # Positive: it did not merely return, it reached the healthy candidate.
        self.assertEqual(resolved, self.hf)
        self.assertTrue((vast / "models/phonetic/checkpoints/v7/final_model.pt").exists())
        # Negative: the wedged probe is STILL blocked, so resolution walked past
        # an unfinished call rather than one that had completed.
        self.assertFalse(self.finished.is_set())
        self.assertTrue(sym._unreachable, "the unreachable path was not recorded")

    def test_total_wedge_returns_instead_of_hanging(self):
        """Nothing reachable at all: resolve to hf/ so the loader raises."""
        self.wedge(lambda p: True)
        started = time.monotonic()
        resolved = sym._resolve_model_dir(repo_hf=self.hf)
        self.assertLess(time.monotonic() - started, 8.0)
        self.assertEqual(resolved, self.hf)
        self.assertFalse(self.finished.is_set())
        self.assertTrue(sym.status()["unreachable_paths"])
        self.assertFalse(sym.status()["loaded"])

    def test_one_wedged_candidate_does_not_suppress_the_others(self):
        """Each candidate needs its OWN breaker.

        A guard's breaker writes its resource off for a cooldown after a
        timeout. Share one guard across candidates and the first wedge silences
        every probe behind it — so `/ix1` being down would make a perfectly
        healthy `/vast` copy unreachable too, with the cooldown (30s here)
        guaranteeing it. This is the case that a `cooldown=0` test cannot see.
        """
        vast = self.root / "vast"
        _crc_layout(vast)
        os.environ["IX3_BASE"] = str(vast)
        (self.hf / "config.json").write_text("{}")
        self.wedge(lambda p: p.parent == self.hf and p.name != "config.json")

        self.assertEqual(sym._resolve_model_dir(repo_hf=self.hf), self.hf)
        stats = {g["label"]: g for g in sym.status()["probes"]}
        # The wedged candidate tripped; the healthy one was never even degraded.
        self.assertEqual(stats["symphonym probe [repo-hf]"]["timeouts"], 1)
        self.assertTrue(stats["symphonym probe [repo-hf]"]["degraded"])
        self.assertEqual(stats["symphonym probe [ix3]"]["timeouts"], 0)
        self.assertFalse(stats["symphonym probe [ix3]"]["degraded"])

    def test_unreachable_is_reported_distinctly_from_absent(self):
        """"Missing" and "unreachable" are different operational problems."""
        # Absent: nothing exists anywhere, nothing hangs.
        sym._resolve_model_dir(repo_hf=self.hf)
        self.assertEqual(sym.status()["unreachable_paths"], [])
        # Unreachable: same empty result, but now it is attributed.
        self.wedge(lambda p: True)
        sym._resolve_model_dir(repo_hf=self.hf)
        self.assertTrue(sym.status()["unreachable_paths"])


class TestResolutionOrder(ResolutionCase):
    def test_explicit_env_var_wins(self):
        explicit = _self_contained(self.root / "explicit")
        _self_contained(self.hf)
        os.environ["SYMPHONYM_MODEL_DIR"] = str(explicit)
        self.assertEqual(sym._resolve_model_dir(repo_hf=self.hf), explicit)

    def test_self_contained_hf_wins_over_crc_layout(self):
        _self_contained(self.hf)
        vast = self.root / "vast"
        _crc_layout(vast)
        os.environ["IX3_BASE"] = str(vast)
        self.assertEqual(sym._resolve_model_dir(repo_hf=self.hf), self.hf)
        # It never needed the CRC layout, so it planted no symlinks.
        self.assertFalse((self.hf / "final_model.pt").is_symlink())

    def test_ix3_is_preferred_over_ix1(self):
        """The serving path must not default to the hard mount (place#242)."""
        vast, ix1 = self.root / "vast", self.root / "ix1"
        _crc_layout(vast)
        _crc_layout(ix1)
        os.environ["IX3_BASE"], os.environ["IX1_BASE"] = str(vast), str(ix1)
        (self.hf / "config.json").write_text("{}")

        self.assertEqual(sym._resolve_model_dir(repo_hf=self.hf), self.hf)
        target = os.readlink(self.hf / "final_model.pt")
        # The symlink it plants is the mechanism that created the landmine, so
        # assert BOTH that it points at flash and that it does not point at /ix1.
        self.assertIn(str(vast), target)
        self.assertNotIn(str(ix1), target)

    def test_ix1_still_works_when_it_is_the_only_layout(self):
        ix1 = self.root / "ix1"
        _crc_layout(ix1)
        os.environ["IX1_BASE"] = str(ix1)
        (self.hf / "config.json").write_text("{}")
        self.assertEqual(sym._resolve_model_dir(repo_hf=self.hf), self.hf)
        self.assertIn(str(ix1), os.readlink(self.hf / "final_model.pt"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
