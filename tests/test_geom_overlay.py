"""Geom-store OVERLAYS and live reload (place#341): ``processing.geom_overlay``
+ the overlay/generation seam in ``processing.geom_store.GeomStoreReader``.

Run package-qualified, never with ``discover -s tests`` (tests/_sandbox.py):

    .venv/bin/python -m unittest tests.test_geom_overlay

Every store here is a real one in a tempdir — shards, ``index.sqlite`` written
by the production builder, overlays written by ``write_overlay`` through the
production ``consolidate_geom_store`` — and every "absent" assertion sits next
to a "present" one in the same test, so a reader that never consulted an
overlay at all cannot pass (``~/.claude/memory/a-check-that-cannot-fail.md``).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from shapely import wkb as shapely_wkb
from shapely.geometry import shape

from processing import geom_overlay as go
from processing.geom_store import (
    INDEX_SQLITE_NAME,
    Generation,
    GeomStoreReader,
    GeomStoreWriter,
    main_generation_stamp,
    shard_filename,
    write_sqlite_index,
)
from processing.helpers import compute_h3_fields
from processing.h3_stage import cover_geometry_for

LOCK_TOKEN_NAME = ".lock"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _square(x0, y0, x1, y1) -> dict:
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def _wkb_of(gj: dict) -> bytes:
    """What the writer stores for *gj* (byte-exact: the same Shapely dump)."""
    return shapely_wkb.dumps(shape(gj))


def _main_store(store: Path, payloads: dict[str, bytes]) -> None:
    """One shard of synthetic payloads + an ``index.sqlite``."""
    store.mkdir(parents=True, exist_ok=True)
    entries = []
    offset = 0
    with open(store / shard_filename(1), "wb") as fh:
        for key, blob in payloads.items():
            fh.write(blob)
            entries.append((key, shard_filename(1), offset, len(blob)))
            offset += len(blob)
    write_sqlite_index(entries, store)


def _stage(staging: Path, items: dict[str, dict]) -> Path:
    with GeomStoreWriter(staging, namespace="whg") as w:
        for key, gj in items.items():
            assert w.write(key, "8a2a1072b59ffff", gj), key
    return staging


def _write_overlay(tmp: Path, store: Path, ds: str, items: dict[str, dict],
                   job_id: str = "1700000000000", **kw) -> dict:
    staging = tmp / f"staging-{ds}-{job_id}-{len(items)}"
    if staging.exists():
        shutil.rmtree(staging)
    _stage(staging, items)
    token = tmp / LOCK_TOKEN_NAME
    token.write_text("{}")
    return go.write_overlay(store, ds, staging, job_id, lock_token=token, **kw)


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class OverlayBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "geom"
        self.main_payloads = {"osm:r1_0": b"<osm:r1_0>" * 3, "wd:Q90_0": b"<wd:Q90_0>" * 5}
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()
        _main_store(self.store, self.main_payloads)
        self.clock = _Clock()

    def tearDown(self):
        self._quiet.__exit__(None, None, None)
        self._tmp.cleanup()

    def reader(self, **kw) -> GeomStoreReader:
        kw.setdefault("clock", self.clock)
        kw.setdefault("rescan_s", 2.0)
        kw.setdefault("grace_s", 60.0)
        r = GeomStoreReader(self.store, **kw)
        self.addCleanup(r.close)
        return r


# ---------------------------------------------------------------------------
# Serving, replacing, removing
# ---------------------------------------------------------------------------

class TestOverlayLifecycle(OverlayBase):
    def test_presence_then_replacement_then_removal(self):
        key = "whg:12:a_0"
        r = self.reader()
        # Presence FIRST — a reader that never looks at overlays fails here.
        v1 = _square(0, 0, 1, 1)
        _write_overlay(self.tmp, self.store, "12", {key: v1}, job_id="1700000000001")
        self.clock.advance(3)
        self.assertEqual(r.get_wkb(key), _wkb_of(v1))
        self.assertEqual(r.get(key)["type"], "Polygon")
        self.assertEqual(set(r.overlay_generations), {"12"})
        epoch_after_add = r.overlay_epoch

        # A new generation: different bytes after the rescan, not before.
        v2 = _square(5, 5, 7, 7)
        _write_overlay(self.tmp, self.store, "12", {key: v2}, job_id="1700000000002")
        self.assertEqual(r.get_wkb(key), _wkb_of(v1), "swap visible before the throttle expired")
        self.clock.advance(3)
        self.assertEqual(r.get_wkb(key), _wkb_of(v2))
        self.assertGreater(r.overlay_epoch, epoch_after_add)
        self.assertEqual(len(r), len(self.main_payloads), "__len__ must not count overlays")

        # Removed: None, and the main store is unaffected throughout.
        self.assertTrue(go.remove_overlay(self.store, "12"))
        self.clock.advance(3)
        self.assertIsNone(r.get_wkb(key))
        self.assertIsNone(r.get(key))
        self.assertEqual(r.overlay_generations, {})
        self.assertEqual(r.get_wkb("osm:r1_0"), self.main_payloads["osm:r1_0"])
        self.assertFalse(go.remove_overlay(self.store, "12"), "second removal reports nothing")

    def test_overlay_precedes_main_for_the_same_key_and_falls_back_after(self):
        key = "whg:7:dup_0"
        _main_store(self.store, {**self.main_payloads, key: b"MAIN-COPY"})
        r = self.reader()
        self.assertEqual(r.get_wkb(key), b"MAIN-COPY")
        ov = _square(1, 1, 2, 2)
        _write_overlay(self.tmp, self.store, "7", {key: ov})
        self.clock.advance(3)
        self.assertEqual(r.get_wkb(key), _wkb_of(ov), "overlay must win over main")
        go.remove_overlay(self.store, "7")
        self.clock.advance(3)
        self.assertEqual(r.get_wkb(key), b"MAIN-COPY", "main is the fallback once removed")

    def test_miss_then_arrival_is_not_pinned_by_the_lru(self):
        key = "whg:3:late_0"
        r = self.reader()
        self.assertIsNone(r.get_wkb(key))          # caches None
        self.assertIsNone(r.get_wkb(key))
        gj = _square(0, 0, 0.5, 0.5)
        _write_overlay(self.tmp, self.store, "3", {key: gj})
        self.clock.advance(3)
        self.assertEqual(r.get_wkb(key), _wkb_of(gj))
        self.assertIn(key, r)

    def test_two_readers_pick_up_independently(self):
        key = "whg:9:x_0"
        c1, c2 = _Clock(), _Clock()
        r1 = self.reader(clock=c1)
        r2 = self.reader(clock=c2)
        gj = _square(0, 0, 1, 1)
        _write_overlay(self.tmp, self.store, "9", {key: gj})
        c1.advance(3)
        self.assertEqual(r1.get_wkb(key), _wkb_of(gj))
        self.assertIsNone(r2.get_wkb(key), "r2 has not ticked yet")
        c2.advance(3)
        self.assertEqual(r2.get_wkb(key), _wkb_of(gj))
        go.remove_overlay(self.store, "9")
        c2.advance(3)
        self.assertIsNone(r2.get_wkb(key))
        self.assertEqual(r1.get_wkb(key), _wkb_of(gj), "r1 still on its own schedule")

    def test_non_whg_keys_never_consult_the_registry(self):
        r = self.reader()
        _write_overlay(self.tmp, self.store, "12", {"whg:12:a_0": _square(0, 0, 1, 1)})
        self.clock.advance(3)
        asked: list[str] = []
        real_get = r.overlays.get

        def counting_get(ds):
            asked.append(ds)
            return real_get(ds)
        r.overlays.get = counting_get
        r.get_wkb("osm:r1_0", cached=False)
        r.get_wkb("wd:Q90_0", cached=False)
        r.get_wkb("whg:abc_0", cached=False)        # whg: but not whg:<digits>:
        r.get_wkb("whg:12x:y_0", cached=False)
        self.assertEqual(asked, [])
        r.get_wkb("whg:12:a_0", cached=False)
        self.assertEqual(asked, ["12"])

    def test_non_digit_dataset_id_rejected(self):
        staging = _stage(self.tmp / "s", {"whg:1:a_0": _square(0, 0, 1, 1)})
        token = self.tmp / LOCK_TOKEN_NAME
        token.write_text("{}")
        for bad in ("12a", "-1", "", "1 2", "whg:12"):
            with self.assertRaises(ValueError, msg=bad):
                go.write_overlay(self.store, bad, staging, "1700000000000", lock_token=token)
            with self.assertRaises(ValueError, msg=bad):
                go.validate_dataset_id(bad)
        self.assertEqual(go.validate_dataset_id(" 42 "), "42")
        self.assertFalse(go.overlays_dir(self.store).exists(), "a refusal must write nothing")

    def test_lock_token_and_empty_staging_are_refused(self):
        staging = _stage(self.tmp / "s", {"whg:1:a_0": _square(0, 0, 1, 1)})
        with self.assertRaises(go.OverlayLockMissing):
            go.write_overlay(self.store, "1", staging, "1700000000000",
                             lock_token=self.tmp / "no-such-lock")
        token = self.tmp / LOCK_TOKEN_NAME
        token.write_text("{}")
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(go.OverlayEmpty):
            go.write_overlay(self.store, "1", empty, "1700000000000", lock_token=token)
        self.assertIsNone(go.current_generation(self.store, "1"))
        m = go.write_overlay(self.store, "1", empty, "1700000000000", lock_token=token,
                             allow_empty=True)
        self.assertEqual(m["count"], 0)
        self.assertIsNotNone(go.current_generation(self.store, "1"))
        r = self.reader()
        self.assertIsNone(r.get_wkb("whg:1:a_0"))
        with self.assertRaises(ValueError):
            go.write_overlay(self.store, "1", staging, "job-x", lock_token=token)


# ---------------------------------------------------------------------------
# Unusable overlays are skipped, logged once, retried
# ---------------------------------------------------------------------------

class TestBrokenOverlays(OverlayBase):
    def test_half_written_tmp_dir_is_ignored(self):
        key = "whg:5:a_0"
        odir = go.overlay_dir(self.store, "5")
        half = odir / f"{go.TMP_PREFIX}20260101T000000Z-j1"
        _main_store(half, {key: b"HALF"})           # a complete-looking mini store
        (half / go.MANIFEST_NAME).write_text(json.dumps(
            {"schema": go.MANIFEST_SCHEMA, "dataset_id": "5", "generation": half.name, "count": 1}))
        r = self.reader()
        self.assertIsNone(r.get_wkb(key), "no CURRENT: not live")
        self.assertEqual(r.overlay_generations, {})
        # Even a CURRENT that points INTO a .tmp- directory is not live.
        os.symlink(half.name, odir / go.CURRENT_LINK)
        self.clock.advance(3)
        self.assertIsNone(r.get_wkb(key))
        self.assertEqual(r.overlay_generations, {})
        # Positive control: the same bytes under a real generation ARE served.
        os.unlink(odir / go.CURRENT_LINK)
        real = odir / "20260101T000000Z-j1"
        os.rename(half, real)
        go._swap_current(odir, real.name)
        self.clock.advance(3)
        self.assertEqual(r.get_wkb(key), b"HALF")

    def test_corrupt_sqlite_is_skipped_logged_once_and_retried(self):
        good, bad = "whg:1:g_0", "whg:2:b_0"
        g_gj, b_gj = _square(0, 0, 1, 1), _square(2, 2, 3, 3)
        _write_overlay(self.tmp, self.store, "1", {good: g_gj})
        _write_overlay(self.tmp, self.store, "2", {bad: b_gj})
        gen2 = go.overlay_dir(self.store, "2") / go.current_generation(self.store, "2")
        intact = (gen2 / INDEX_SQLITE_NAME).read_bytes()
        (gen2 / INDEX_SQLITE_NAME).write_bytes(b"not a database at all" * 50)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = self.reader()
            self.assertEqual(r.get_wkb(good), _wkb_of(g_gj), "the other overlay is still served")
            self.assertIsNone(r.get_wkb(bad))
            self.assertEqual(set(r.overlay_generations), {"1"})
            for _ in range(3):                     # three more rescans …
                self.clock.advance(3)
                r.get_wkb(bad, cached=False)
        lines = [l for l in out.getvalue().splitlines() if "whg-2" in l and "unusable" in l]
        self.assertEqual(len(lines), 1, f"logged once per overlay+reason, got:\n{out.getvalue()}")

        # Never cached as failed forever: repaired on disk → served on the next rescan.
        (gen2 / INDEX_SQLITE_NAME).write_bytes(intact)
        # The link text is unchanged, so force the registry to re-open by
        # re-pointing CURRENT at a fresh generation (what a re-run job does).
        _write_overlay(self.tmp, self.store, "2", {bad: b_gj}, job_id="1700000000009")
        self.clock.advance(3)
        self.assertEqual(r.get_wkb(bad), _wkb_of(b_gj))
        self.assertEqual(set(r.overlay_generations), {"1", "2"})

    def test_manifest_naming_another_dataset_is_skipped(self):
        key = "whg:4:a_0"
        _write_overlay(self.tmp, self.store, "4", {key: _square(0, 0, 1, 1)})
        gen = go.overlay_dir(self.store, "4") / go.current_generation(self.store, "4")
        m = json.loads((gen / go.MANIFEST_NAME).read_text())
        m["dataset_id"] = "44"
        (gen / go.MANIFEST_NAME).write_text(json.dumps(m))
        r = self.reader()
        self.assertIsNone(r.get_wkb(key))
        self.assertEqual(r.overlay_generations, {})

    def test_missing_overlays_dir_is_not_an_error(self):
        self.assertFalse(go.overlays_dir(self.store).exists())
        r = self.reader()
        self.assertEqual(r.get_wkb("osm:r1_0"), self.main_payloads["osm:r1_0"])
        self.assertIsNone(r.get_wkb("whg:1:a_0"))
        self.assertEqual(go.list_overlays(self.store), {})


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

class TestCaps(OverlayBase):
    def test_writer_refuses_over_budget(self):
        items = {f"whg:1:k{i}_0": _square(i, 0, i + 1, 1) for i in range(3)}
        with self.assertRaises(go.OverlayBudgetExceeded):
            _write_overlay(self.tmp, self.store, "1", items, max_geometries_=2)
        with self.assertRaises(go.OverlayBudgetExceeded):
            _write_overlay(self.tmp, self.store, "1", items, max_bytes_=100)
        self.assertIsNone(go.current_generation(self.store, "1"))
        odir = go.overlay_dir(self.store, "1")
        self.assertEqual([p for p in odir.iterdir()] if odir.exists() else [], [],
                         "a refused build leaves no .tmp- directory behind")
        # Positive control at the cap exactly.
        m = _write_overlay(self.tmp, self.store, "1", items, max_geometries_=3)
        self.assertEqual(m["count"], 3)

    def test_registry_skips_over_budget_and_raises_in_strict_mode(self):
        items = {f"whg:1:k{i}_0": _square(i, 0, i + 1, 1) for i in range(3)}
        _write_overlay(self.tmp, self.store, "1", items)
        _write_overlay(self.tmp, self.store, "2", {"whg:2:a_0": _square(0, 0, 1, 1)})
        reg = go.OverlayRegistry(self.store, clock=self.clock, max_geometries_=2)
        self.assertTrue(reg.rescan(self.clock()))
        self.assertEqual(set(reg.generations()), {"2"}, "the over-budget overlay is skipped")
        with self.assertRaises(go.OverlayBudgetExceeded):
            go.OverlayRegistry(self.store, clock=self.clock, max_geometries_=2).rescan(
                self.clock(), strict=True)
        reg2 = go.OverlayRegistry(self.store, clock=self.clock, max_overlays_=1)
        reg2.rescan(self.clock())
        self.assertEqual(set(reg2.generations()), {"1"}, "first by dataset id within the cap")
        with self.assertRaises(go.OverlayBudgetExceeded):
            go.OverlayRegistry(self.store, clock=self.clock, max_overlays_=1).rescan(
                self.clock(), strict=True)
        reg3 = go.OverlayRegistry(self.store, clock=self.clock, max_bytes_=10)
        reg3.rescan(self.clock())
        self.assertEqual(reg3.generations(), {})
        reg.close(); reg2.close(); reg3.close()

    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {go.MAX_GEOMETRIES_ENV: "7",
                                                   go.MAX_MB_ENV: "0.5",
                                                   go.MAX_OVERLAYS_ENV: "3"}):
            self.assertEqual(go.max_geometries(), 7)
            self.assertEqual(go.max_bytes(), 512 * 1024)
            self.assertEqual(go.max_overlays(), 3)


# ---------------------------------------------------------------------------
# Concurrency: a swap under threaded reads returns old-or-new bytes only
# ---------------------------------------------------------------------------

class TestConcurrentReplace(OverlayBase):
    def test_threaded_reads_during_replacement_see_whole_generations_only(self):
        key = "whg:21:race_0"
        gens = [_square(i, i, i + 1.5, i + 1.5) for i in range(6)]
        allowed = {_wkb_of(g) for g in gens}
        _write_overlay(self.tmp, self.store, "21", {key: gens[0]}, job_id="1700000000000")
        r = self.reader(clock=None, rescan_s=0.0, grace_s=0.5, lru_maxsize=8)
        stop = threading.Event()
        bad: list = []
        reads = [0]
        lock = threading.Lock()

        def worker(cached: bool):
            n = 0
            while not stop.is_set():
                got = r.get_wkb(key, cached=cached)
                if got not in allowed:
                    bad.append(got if got is None else got[:16])
                    return
                n += 1
            with lock:
                reads[0] += n

        threads = [threading.Thread(target=worker, args=(i % 2 == 0,)) for i in range(6)]
        for t in threads:
            t.start()
        try:
            for i, g in enumerate(gens[1:], start=1):
                _write_overlay(self.tmp, self.store, "21", {key: g}, job_id=f"170000000000{i}")
                time.sleep(0.05)
            time.sleep(0.2)
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=10)
        self.assertEqual(bad, [], f"garbage or None observed: {bad[:3]}")
        self.assertGreater(reads[0], 50, "the stress loop barely ran")
        self.assertEqual(r.get_wkb(key, cached=False), _wkb_of(gens[-1]))


# ---------------------------------------------------------------------------
# Main-store generation reload
# ---------------------------------------------------------------------------

class TestMainStoreReload(OverlayBase):
    def _compaction_like_replace(self, added: dict[str, bytes]) -> None:
        """Copy index.sqlite to a temp, append a NEW shard, insert rows,
        os.replace — the shape ``processing.compact_overlays`` performs."""
        live = self.store / INDEX_SQLITE_NAME
        tmp = self.store / (INDEX_SQLITE_NAME + ".tmp")
        shutil.copyfile(live, tmp)
        with open(self.store / shard_filename(2), "wb") as fh:
            rows, off = [], 0
            for k, blob in added.items():
                fh.write(blob)
                rows.append((k, 2, off, len(blob)))
                off += len(blob)
        conn = sqlite3.connect(str(tmp))
        conn.execute("PRAGMA journal_mode=OFF")
        conn.executemany("INSERT OR REPLACE INTO geom VALUES(?,?,?,?)", rows)
        n = conn.execute("SELECT count(*) FROM geom").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO meta VALUES('count', ?)", (str(n),))
        conn.commit()
        conn.close()
        os.replace(tmp, live)

    def test_reload_sees_new_keys_and_keeps_old_fds_through_the_grace(self):
        r = self.reader()
        self.assertEqual(r.get_wkb("osm:r1_0"), self.main_payloads["osm:r1_0"])
        gen_before = r.main_generation
        old = r._main
        self.assertIsNone(r.get_wkb("gn:new_0"))
        self._compaction_like_replace({"gn:new_0": b"NEWSHARD-BYTES"})
        self.assertNotEqual(main_generation_stamp(self.store), gen_before)

        self.assertIsNone(r.get_wkb("gn:new_0"), "not before the throttle expires")
        self.clock.advance(3)
        epoch = r.overlay_epoch
        self.assertEqual(r.get_wkb("gn:new_0"), b"NEWSHARD-BYTES")
        self.assertEqual(r.main_generation, main_generation_stamp(self.store))
        self.assertNotEqual(r.main_generation, gen_before)
        self.assertGreater(r.overlay_epoch, epoch - 1)
        self.assertEqual(len(r), len(self.main_payloads) + 1)
        self.assertEqual(r.get_wkb("osm:r1_0"), self.main_payloads["osm:r1_0"])

        # The superseded generation is still open inside the grace …
        self.assertIsNot(r._main, old)
        self.assertFalse(old.closed)
        self.assertEqual(old.read_wkb("osm:r1_0"), self.main_payloads["osm:r1_0"])
        self.assertIsNone(old.read_wkb("gn:new_0"), "the old snapshot stays consistent")
        # … and closed once it has passed.
        self.clock.advance(61)
        r.tick(force=True)
        self.assertTrue(old.closed)
        self.assertEqual(r.get_wkb("osm:r1_0", cached=False), self.main_payloads["osm:r1_0"])

    def test_unopenable_new_index_keeps_the_previous_generation(self):
        r = self.reader()
        before = r.main_generation
        live = self.store / INDEX_SQLITE_NAME
        tmp = self.store / "junk"
        tmp.write_bytes(b"garbage" * 100)
        os.replace(tmp, live)
        self.clock.advance(3)
        self.assertEqual(r.get_wkb("osm:r1_0"), self.main_payloads["osm:r1_0"])
        self.assertEqual(r.main_generation, before)

    def test_json_backend_is_never_reloaded(self):
        (self.store / INDEX_SQLITE_NAME).unlink()
        index = {k: {"file": shard_filename(1), "offset": 0, "length": len(v)}
                 for k, v in self.main_payloads.items()}
        (self.store / "index.json").write_text(json.dumps(index))
        r = self.reader()
        self.assertEqual(r.backend, "json")
        self.assertEqual(r.main_generation, "json")
        self.clock.advance(3)
        self.assertFalse(r.tick(force=True))


# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------

class TestHeartbeat(OverlayBase):
    def test_written_throttled_removed_and_stale_ignored(self):
        r = self.reader(heartbeat=True)
        path = go.heartbeat_path(self.store)
        self.assertTrue(path.exists(), "the constructor's first tick writes it")
        hb = json.loads(path.read_text())
        self.assertEqual(hb["pid"], os.getpid())
        self.assertEqual(hb["main_generation"], r.main_generation)
        self.assertEqual(hb["overlays"], {})
        first_mtime = path.stat().st_mtime_ns

        _write_overlay(self.tmp, self.store, "12", {"whg:12:a_0": _square(0, 0, 1, 1)})
        self.clock.advance(3)
        r.tick(force=True)
        self.assertEqual(path.stat().st_mtime_ns, first_mtime, "at most every 30 s")
        self.clock.advance(31)
        r.tick(force=True)
        hb = json.loads(path.read_text())
        self.assertEqual(set(hb["overlays"]), {"12"})

        live = go.read_live_heartbeats(self.store)
        self.assertEqual([h["pid"] for h in live], [os.getpid()])

        # Stale by age: ignored. Dead pid: ignored (a really-dead one, and an
        # injected liveness oracle). Another host: judged on its timestamp only.
        go.write_json_atomic(go.heartbeat_dir(self.store) / "99999991.json",
                             {"pid": 99999991, "host": hb["host"], "main_generation": "x",
                              "overlays": {}, "ts": time.time() - 700})
        proc = subprocess.Popen(["true"])
        proc.wait()
        go.write_json_atomic(go.heartbeat_dir(self.store) / f"{proc.pid}.json",
                             {"pid": proc.pid, "host": hb["host"], "main_generation": "x",
                              "overlays": {}, "ts": time.time()})
        go.write_json_atomic(go.heartbeat_dir(self.store) / "424242.json",
                             {"pid": 424242, "host": "elsewhere", "main_generation": "x",
                              "overlays": {}, "ts": time.time()})
        (go.heartbeat_dir(self.store) / "garbage.json").write_text("{not json")
        live = go.read_live_heartbeats(self.store)
        self.assertEqual(sorted(h["pid"] for h in live), sorted([os.getpid(), 424242]))
        live = go.read_live_heartbeats(self.store, pid_alive_fn=lambda pid: False)
        self.assertEqual([h["pid"] for h in live], [424242])

        r.close()
        self.assertFalse(path.exists(), "a closing reader removes its own file")

    def test_heartbeat_off_by_default(self):
        self.reader()
        self.assertFalse(go.heartbeat_dir(self.store).exists())


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

class TestPrune(OverlayBase):
    def test_keeps_current_and_newest_then_removes_empty_overlay(self):
        key = "whg:8:a_0"
        for i in range(4):
            _write_overlay(self.tmp, self.store, "8", {key: _square(i, 0, i + 1, 1)},
                           job_id=f"170000000000{i}")
        odir = go.overlay_dir(self.store, "8")
        current = go.current_generation(self.store, "8")
        half = odir / f"{go.TMP_PREFIX}stale"
        half.mkdir()
        def _gen_dirs():
            return sorted(p.name for p in odir.iterdir()
                          if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
        gens = _gen_dirs()
        self.assertEqual(len(gens), 4)

        removed = go.prune_overlay_generations(self.store, "8", keep=1, min_age_s=10_000)
        self.assertEqual(removed, [], "nothing younger than min_age_s is touched")
        removed = go.prune_overlay_generations(self.store, "8", keep=1, min_age_s=0)
        left = _gen_dirs()
        self.assertEqual(left, sorted([current, gens[-2]]))
        self.assertNotIn(half, odir.iterdir())
        self.assertIn(half, removed)

        r = self.reader()
        self.assertEqual(r.get_wkb(key), _wkb_of(_square(3, 0, 4, 1)))
        go.remove_overlay(self.store, "8")
        go.prune_overlay_generations(self.store, keep=0, min_age_s=0)
        self.assertFalse(odir.exists())
        self.clock.advance(3)
        self.assertIsNone(r.get_wkb(key))


# ---------------------------------------------------------------------------
# THE HARNESS-CAN-FAIL TEST
# ---------------------------------------------------------------------------

class TestRealisticPolygonsRoundTrip(OverlayBase):
    """One test, two halves: the overlay serves a realistic polygon dataset
    through the SAME calls the pipeline makes (reader.get → compute_h3_fields,
    and h3_stage.cover_geometry_for), and after the overlay is removed those
    same calls return None / raise. A harness in which either half is vacuous
    (a reader that never reads, a cover that never covers) fails here."""

    def test_overlay_serves_then_removal_fails_closed(self):
        # A MultiPolygon with holes (two islands, each with a lake) …
        multi = {"type": "MultiPolygon", "coordinates": [
            [[[10.0, 50.0], [10.6, 50.0], [10.6, 50.5], [10.0, 50.5], [10.0, 50.0]],
             [[10.2, 50.2], [10.3, 50.2], [10.3, 50.3], [10.2, 50.3], [10.2, 50.2]]],
            [[[11.0, 50.0], [11.4, 50.0], [11.4, 50.3], [11.0, 50.3], [11.0, 50.0]],
             [[11.1, 50.1], [11.2, 50.1], [11.2, 50.2], [11.1, 50.2], [11.1, 50.1]]],
        ]}
        # … and a Polygon of ~0.5° extent.
        poly = {"type": "Polygon", "coordinates": [[
            [-1.0, 51.0], [-0.5, 51.05], [-0.45, 51.5], [-1.05, 51.45], [-1.0, 51.0]]]}
        expected_area = {
            "whg:77:islands_0": shape(multi).area,   # (0.6*0.5 - 0.01) + (0.4*0.3 - 0.01)
            "whg:77:shire_0": shape(poly).area,
        }
        self.assertAlmostEqual(expected_area["whg:77:islands_0"], 0.30 - 0.01 + 0.12 - 0.01)
        self.assertGreater(expected_area["whg:77:shire_0"], 0.2)

        m = _write_overlay(self.tmp, self.store, "77",
                           {"whg:77:islands_0": multi, "whg:77:shire_0": poly})
        self.assertEqual(m["count"], 2)
        r = self.reader()
        self.clock.advance(3)

        for key, gj in (("whg:77:islands_0", multi), ("whg:77:shire_0", poly)):
            got = r.get(key)
            self.assertIsNotNone(got, key)
            self.assertEqual(got["type"], gj["type"])
            self.assertAlmostEqual(shape(got).area, expected_area[key], places=9)
            if key == "whg:77:islands_0":
                self.assertEqual(len(got["coordinates"]), 2)
                self.assertEqual(len(got["coordinates"][0]), 2, "the hole survived")
            rp = shape(got).representative_point()
            centroid, cover = compute_h3_fields(rp.x, rp.y, got)
            self.assertIsNotNone(centroid)
            self.assertGreater(len(cover), 1, f"{key}: a {gj['type']} must cover > 1 cell")
            entry = {"has_geom": True, "geom_ref": key, "geometry_index": 0}
            self.assertEqual(cover_geometry_for(entry, key.rsplit("_", 1)[0], 0, r), got)

        # Now take the overlay away: the SAME retrieval returns None, and the
        # has_geom promise can no longer be honoured — so h3_stage must raise
        # rather than fall back to a hull.
        self.assertTrue(go.remove_overlay(self.store, "77"))
        self.clock.advance(3)
        for key in expected_area:
            self.assertIsNone(r.get(key), key)
            entry = {"has_geom": True, "geom_ref": key, "geometry_index": 0}
            with self.assertRaises(RuntimeError):
                cover_geometry_for(entry, key.rsplit("_", 1)[0], 0, r)
        # And the main store was never involved in any of it.
        self.assertEqual(len(r), len(self.main_payloads))
        self.assertEqual(r.get_wkb("osm:r1_0"), self.main_payloads["osm:r1_0"])


# ---------------------------------------------------------------------------
# Generation primitives
# ---------------------------------------------------------------------------

class TestGeneration(OverlayBase):
    def test_iter_rows_and_locate(self):
        g = Generation(self.store, label="t")
        self.addCleanup(g.close)
        rows = list(g.iter_rows(batch=1))
        self.assertEqual(sorted(k for k, *_ in rows), sorted(self.main_payloads))
        for k, shard, off, ln in rows:
            self.assertEqual(g.locate(k), (shard, off, ln))
            self.assertEqual(g.read_wkb(k), self.main_payloads[k])
        self.assertIsNone(g.locate("nope"))
        self.assertTrue(g.contains("osm:r1_0"))
        self.assertEqual(g.n_rows(), 2)
        self.assertEqual(g.stamp, main_generation_stamp(self.store))
        g.close()
        self.assertTrue(g.closed)
        with self.assertRaises(RuntimeError):
            g.locate("osm:r1_0")


if __name__ == "__main__":
    unittest.main()
