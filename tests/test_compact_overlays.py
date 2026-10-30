"""``processing.compact_overlays``: folding overlays into the main geom store
and deleting them later, with the live index byte-identical on every failure.

Run package-qualified, never with ``discover -s tests`` (tests/_sandbox.py):

    .venv/bin/python -m unittest tests.test_compact_overlays

Every negative case (a stage that fails, a lock that is held, a deletion rule
that is not met) is paired with a positive control on the same fixture, so a
compactor that never folds, or never deletes, fails here too.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from shapely import wkb as shapely_wkb
from shapely.geometry import shape

from gateway import dataset_jobs as dj
from processing import compact_overlays as co
from processing import geom_overlay as go
from processing.geom_store import (
    INDEX_JSON_NAME,
    INDEX_SQLITE_NAME,
    GeomStoreReader,
    GeomStoreWriter,
    main_generation_stamp,
    shard_filename,
    write_sqlite_index,
)

BIG = 100 << 30   # "plenty" for the injected memory/disk probes


def _square(x0, y0, x1, y1) -> dict:
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def _wkb_of(gj: dict) -> bytes:
    return shapely_wkb.dumps(shape(gj))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _main_store(store: Path, n: int = 50) -> dict[str, bytes]:
    """Two shards of synthetic payloads, ``index.sqlite`` AND an ``index.json``
    (so the test can prove the JSON is left alone)."""
    store.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, bytes] = {}
    entries, index = [], {}
    for shard in (1, 2):
        off = 0
        with open(store / shard_filename(shard), "wb") as fh:
            for i in range(n // 2):
                key = f"osm:r{shard}{i:03d}_0"
                blob = f"<{key}>".encode() * (1 + i % 5)
                fh.write(blob)
                entries.append((key, shard_filename(shard), off, len(blob)))
                index[key] = {"file": shard_filename(shard), "offset": off, "length": len(blob)}
                payloads[key] = blob
                off += len(blob)
    write_sqlite_index(entries, store)
    (store / INDEX_JSON_NAME).write_text(json.dumps(index))
    return payloads


def _overlay(tmp: Path, store: Path, ds: str, items: dict[str, dict], job_id: str) -> dict:
    staging = tmp / f"staging-{ds}-{job_id}"
    if staging.exists():
        shutil.rmtree(staging)
    with GeomStoreWriter(staging, namespace="whg") as w:
        for key, gj in items.items():
            assert w.write(key, "8a2a1072b59ffff", gj), key
    token = tmp / ".token"
    token.write_text("{}")
    return go.write_overlay(store, ds, staging, job_id, lock_token=token)


def _live_heartbeat(store: Path) -> list[dict]:
    """A reader on the CURRENT main generation — the deletion-permitting state."""
    return [{"pid": os.getpid(), "main_generation": main_generation_stamp(store), "ts": 0}]


class CompactBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "geom"
        self.backups = self.tmp / "backups"
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()
        self.payloads = _main_store(self.store)
        self.json_sha = _sha(self.store / INDEX_JSON_NAME)
        self.a = {f"whg:12:a{i}_0": _square(i, 0, i + 1, 1) for i in range(3)}
        self.b = {f"whg:34:b{i}_0": _square(0, i, 1, i + 1) for i in range(4)}
        _overlay(self.tmp, self.store, "12", self.a, "1700000000001")
        _overlay(self.tmp, self.store, "34", self.b, "1700000000002")
        self.gen_a = go.current_generation(self.store, "12")
        self.gen_b = go.current_generation(self.store, "34")
        # Lock state dir for main()-level tests.
        self.state_dir = self.tmp / "jobs"
        self._env = mock.patch.dict(os.environ, {dj.STATE_DIR_ENV: str(self.state_dir)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._quiet.__exit__(None, None, None)
        self._tmp.cleanup()

    def compactor(self, **kw) -> co.Compactor:
        kw.setdefault("meminfo", lambda: BIG)
        kw.setdefault("free_space", lambda p: BIG)
        kw.setdefault("heartbeats", lambda s: [])
        kw.setdefault("backup", False)
        return co.Compactor(self.store, **kw)

    def main_reader(self) -> GeomStoreReader:
        r = GeomStoreReader(self.store, lru_maxsize=0, overlays=False)
        self.addCleanup(r.close)
        return r

    def assert_store_untouched(self, sha_before: str):
        self.assertEqual(_sha(self.store / INDEX_SQLITE_NAME), sha_before)
        self.assertEqual(_sha(self.store / INDEX_JSON_NAME), self.json_sha)
        leftovers = [p.name for p in self.store.iterdir()
                     if ".compact" in p.name or p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        shards = sorted(p.name for p in self.store.glob("geom_shard_*.bin"))
        self.assertEqual(shards, [shard_filename(1), shard_filename(2)])
        self.assertEqual(go.current_generation(self.store, "12"), self.gen_a)
        self.assertEqual(go.current_generation(self.store, "34"), self.gen_b)
        self.assertEqual(co.read_compacted(self.store), [])


# ---------------------------------------------------------------------------
# Folding
# ---------------------------------------------------------------------------

class TestFold(CompactBase):
    def test_folds_two_overlays_bytes_and_counts(self):
        before = main_generation_stamp(self.store)
        result = self.compactor(backup=True, backup_dir=self.backups).run()
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(sorted(r["dataset_id"] for r in result["folded"]), ["12", "34"])
        self.assertEqual(result["deleted"], [])
        self.assertNotEqual(result["main_generation_after"], before)

        r = self.main_reader()
        self.assertEqual(len(r), len(self.payloads) + len(self.a) + len(self.b))
        for key, gj in {**self.a, **self.b}.items():
            self.assertEqual(r.get_wkb(key, cached=False), _wkb_of(gj), key)
        for key, blob in self.payloads.items():
            self.assertEqual(r.get_wkb(key, cached=False), blob, key)
        self.assertEqual(_sha(self.store / INDEX_JSON_NAME), self.json_sha, "index.json untouched")
        self.assertTrue((self.store / shard_filename(3)).exists(), "new shard numbered after the last")
        self.assertFalse((self.store / shard_filename(4)).exists())
        records = co.read_compacted(self.store)
        self.assertEqual({(x["dataset_id"], x["generation"]) for x in records},
                         {("12", self.gen_a), ("34", self.gen_b)})
        self.assertTrue(all(x["main_generation_after"] == result["main_generation_after"]
                            for x in records))
        self.assertTrue(list(self.backups.glob("index-*.sqlite")), "backup refreshed")
        # Overlays stay in place until a later run deletes them.
        self.assertEqual(go.current_generation(self.store, "12"), self.gen_a)
        self.assertEqual(go.current_generation(self.store, "34"), self.gen_b)

    def test_refold_of_a_new_generation_replaces_keys(self):
        self.compactor().run()
        n_after_first = len(self.main_reader())
        newer = {"whg:12:a0_0": _square(9, 9, 10, 10), "whg:12:a1_0": _square(8, 8, 9, 9),
                 "whg:12:extra_0": _square(7, 7, 8, 8)}
        _overlay(self.tmp, self.store, "12", newer, "1700000000003")
        gen2 = go.current_generation(self.store, "12")
        self.assertNotEqual(gen2, self.gen_a)
        result = self.compactor(heartbeats=_live_heartbeat).run()
        self.assertEqual([x["dataset_id"] for x in result["folded"]], ["12"])
        self.assertEqual(result["folded"][0]["replaced"], 2)
        self.assertEqual(result["folded"][0]["count"], 3)
        r = self.main_reader()
        self.assertEqual(len(r), n_after_first + 1, "old + new − replaced")
        for key, gj in newer.items():
            self.assertEqual(r.get_wkb(key, cached=False), _wkb_of(gj))
        self.assertEqual(r.get_wkb("whg:12:a2_0", cached=False), _wkb_of(self.a["whg:12:a2_0"]),
                         "a key the new generation did not carry keeps its folded bytes")
        self.assertEqual(sorted(x["generation"] for x in co.read_compacted(self.store)
                                if x["dataset_id"] == "12"), [gen2],
                         "one record per dataset: the newest fold")
        # Deletion runs BEFORE folding: 34 (folded on run 1, same generation on
        # disk, a reader on the then-current main generation) is deleted in this
        # run, and 12's newer generation is folded afterwards.
        self.assertEqual([d["dataset_id"] for d in result["deleted"]], ["34"])
        self.assertIsNone(go.current_generation(self.store, "34"))

    def test_nothing_to_fold_is_ok(self):
        self.compactor().run()
        result = self.compactor().run()
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["folded"], [])
        self.assertEqual(len(result["skipped"]), 2, "both kept: no live heartbeat")

    def test_dry_run_writes_nothing(self):
        sha = _sha(self.store / INDEX_SQLITE_NAME)
        result = self.compactor(dry_run=True).run()
        self.assertEqual(result["outcome"], "dry-run")
        self.assertEqual(sorted(w["dataset_id"] for w in result["would"]), ["12", "34"])
        self.assertEqual(result["folded"], [])
        self.assert_store_untouched(sha)

    def test_large_overlay_rolls_into_several_shards(self):
        items = {f"whg:56:p{i}_0": _square(i, 0, i + 1, 1) for i in range(20)}
        _overlay(self.tmp, self.store, "56", items, "1700000000004")
        one = len(_wkb_of(_square(0, 0, 1, 1)))
        self.compactor(shard_size_bytes=one * 4).run()
        shards = sorted(p.name for p in self.store.glob("geom_shard_*.bin"))
        self.assertGreater(len(shards), 4)
        r = self.main_reader()
        for key, gj in items.items():
            self.assertEqual(r.get_wkb(key, cached=False), _wkb_of(gj), key)


# ---------------------------------------------------------------------------
# Failure injection: the live store is byte-identical afterwards
# ---------------------------------------------------------------------------

class TestFailureLeavesStoreIntact(CompactBase):
    def _run_expecting(self, exc_type=co.CompactionError):
        sha = _sha(self.store / INDEX_SQLITE_NAME)
        with self.assertRaises(exc_type):
            self.compactor().run()
        self.assert_store_untouched(sha)
        # Overlays still serve through a normal reader.
        r = GeomStoreReader(self.store, lru_maxsize=0)
        self.addCleanup(r.close)
        for key, gj in {**self.a, **self.b}.items():
            self.assertEqual(r.get_wkb(key, cached=False), _wkb_of(gj), key)
        rm = self.main_reader()
        self.assertIsNone(rm.get_wkb("whg:12:a0_0", cached=False), "nothing reached main")

    def test_copy_stage(self):
        with mock.patch.object(co.Compactor, "_copy_index",
                               side_effect=co.CompactionError("copy boom")):
            self._run_expecting()

    def test_append_stage(self):
        with mock.patch.object(co.Compactor, "_append_overlay",
                               side_effect=co.CompactionError("append boom")):
            self._run_expecting()

    def test_verify_stage_injected(self):
        with mock.patch.object(co.Compactor, "_verify",
                               side_effect=co.CompactionError("verify boom")):
            self._run_expecting()

    def test_verify_stage_catches_corrupted_bytes(self):
        # Not an injected raise: the appender writes WRONG bytes, and the
        # real verification (every folded key read back through a fresh
        # reader on the candidate index) must notice.
        real_write = co._ShardAppender.write

        def corrupt_write(self_, data: bytes):
            return real_write(self_, data[:-1] + bytes([data[-1] ^ 0xFF]))
        with mock.patch.object(co._ShardAppender, "write", corrupt_write):
            self._run_expecting()

    def test_verify_stage_catches_wrong_count(self):
        real = co.Compactor._append_overlay

        def lie(self_, conn, cand, appender):
            n_new, n_replaced = real(self_, conn, cand, appender)
            return n_new, n_replaced + 1          # claims a replacement that did not happen
        with mock.patch.object(co.Compactor, "_append_overlay", lie):
            self._run_expecting()

    def test_replace_stage(self):
        with mock.patch.object(co.Compactor, "_replace_index",
                               side_effect=OSError("replace boom")):
            self._run_expecting(OSError)

    def test_positive_control_same_fixture_folds(self):
        sha = _sha(self.store / INDEX_SQLITE_NAME)
        self.compactor().run()
        self.assertNotEqual(_sha(self.store / INDEX_SQLITE_NAME), sha)
        self.assertIsNotNone(self.main_reader().get_wkb("whg:12:a0_0", cached=False))


# ---------------------------------------------------------------------------
# Deletion rules a–d
# ---------------------------------------------------------------------------

class TestDeletion(CompactBase):
    def setUp(self):
        super().setUp()
        self.compactor().run()                       # fold both; nothing deleted yet
        self.sha_folded = _sha(self.store / INDEX_SQLITE_NAME)

    def _overlay_present(self, ds: str, gen: str) -> bool:
        return go.current_generation(self.store, ds) == gen and \
            (go.overlay_dir(self.store, ds) / gen).is_dir()

    def test_positive_control_all_rules_met_deletes(self):
        result = self.compactor(heartbeats=_live_heartbeat).run()
        self.assertEqual(sorted(d["dataset_id"] for d in result["deleted"]), ["12", "34"])
        self.assertFalse(go.overlay_dir(self.store, "12").exists())
        self.assertFalse(go.overlay_dir(self.store, "34").exists())
        self.assertEqual(co.read_compacted(self.store), [])
        self.assertEqual(_sha(self.store / INDEX_SQLITE_NAME), self.sha_folded, "deletion folds nothing")
        r = self.main_reader()
        for key, gj in {**self.a, **self.b}.items():
            self.assertEqual(r.get_wkb(key, cached=False), _wkb_of(gj))

    def test_positive_control_through_a_real_heartbeat(self):
        # A real reader announcing itself: its FIRST tick wrote a heartbeat on
        # the pre-fold generation (setUp folded after? no — the reader opens
        # now, on the folded index), so it is current and deletion proceeds.
        r = GeomStoreReader(self.store, heartbeat=True)
        self.addCleanup(r.close)
        self.assertEqual(r.main_generation, main_generation_stamp(self.store))
        result = self.compactor(heartbeats=None).run()      # the real read_live_heartbeats
        self.assertEqual(sorted(d["dataset_id"] for d in result["deleted"]), ["12", "34"])

    def test_rule_a_not_listed_is_folded_not_deleted(self):
        records = [x for x in co.read_compacted(self.store) if x["dataset_id"] != "12"]
        co.write_compacted(self.store, records)
        result = self.compactor(heartbeats=_live_heartbeat).run()
        self.assertEqual([d["dataset_id"] for d in result["deleted"]], ["34"])
        self.assertEqual([f["dataset_id"] for f in result["folded"]], ["12"])
        self.assertTrue(self._overlay_present("12", self.gen_a))

    def test_rule_b_bytes_differ_is_not_deleted_and_refolded(self):
        # Corrupt the MAIN store's copy of one folded key in place.
        r = self.main_reader()
        shard, off, ln = r._main.locate("whg:12:a1_0")
        with open(self.store / shard_filename(shard), "r+b") as fh:
            fh.seek(off)
            b = fh.read(1)
            fh.seek(off)
            fh.write(bytes([b[0] ^ 0xFF]))
        r.close()
        result = self.compactor(heartbeats=_live_heartbeat).run()
        self.assertEqual([d["dataset_id"] for d in result["deleted"]], ["34"])
        self.assertEqual([f["dataset_id"] for f in result["folded"]], ["12"], "re-folded")
        self.assertTrue(self._overlay_present("12", self.gen_a))
        self.assertTrue(any("verification failed" in s["reason"] for s in result["skipped"]))
        r2 = self.main_reader()
        self.assertEqual(r2.get_wkb("whg:12:a1_0", cached=False), _wkb_of(self.a["whg:12:a1_0"]),
                         "the re-fold restored the bytes")

    def test_rule_c_no_live_heartbeat_is_not_deleted(self):
        result = self.compactor(heartbeats=lambda s: []).run()
        self.assertEqual(result["deleted"], [])
        self.assertTrue(self._overlay_present("12", self.gen_a))
        self.assertTrue(self._overlay_present("34", self.gen_b))
        self.assertTrue(all("no live reader heartbeat" in s["reason"] for s in result["skipped"]))
        # Stale and dead heartbeats are "none" too, through the real reader of them.
        go.write_json_atomic(go.heartbeat_dir(self.store) / "1.json",
                             {"pid": 1, "main_generation": main_generation_stamp(self.store),
                              "overlays": {}, "ts": 0})
        result = self.compactor(heartbeats=None).run()
        self.assertEqual(result["deleted"], [])

    def test_rule_c_reader_behind_is_not_deleted(self):
        behind = lambda s: [{"pid": os.getpid(), "main_generation": "0:0", "ts": 0},
                            *_live_heartbeat(s)]
        result = self.compactor(heartbeats=behind).run()
        self.assertEqual(result["deleted"], [])
        self.assertTrue(all("another main generation" in s["reason"] for s in result["skipped"]))
        self.assertTrue(self._overlay_present("12", self.gen_a))

    def test_rule_d_newer_generation_is_never_deleted(self):
        newer = {"whg:12:a0_0": _square(9, 9, 10, 10)}
        _overlay(self.tmp, self.store, "12", newer, "1700000000009")
        gen2 = go.current_generation(self.store, "12")
        result = self.compactor(heartbeats=_live_heartbeat).run()
        self.assertEqual([d["dataset_id"] for d in result["deleted"]], ["34"])
        self.assertEqual([f["dataset_id"] for f in result["folded"]], ["12"])
        self.assertTrue(self._overlay_present("12", gen2))
        self.assertTrue((go.overlay_dir(self.store, "12") / self.gen_a).is_dir(),
                        "the old generation directory is prune's business, not deletion's")
        self.assertEqual(self.main_reader().get_wkb("whg:12:a0_0", cached=False), _wkb_of(newer["whg:12:a0_0"]))


# ---------------------------------------------------------------------------
# main(): lock, preflight, status
# ---------------------------------------------------------------------------

class TestMain(CompactBase):
    def _argv(self, *extra):
        return ["--store-dir", str(self.store), "--no-backup", "--min-avail-gb", "0",
                "--min-free-gb", "0", *extra]

    def _status(self) -> dict:
        return json.loads((go.overlays_dir(self.store) / co.STATUS_NAME).read_text())

    def test_folds_and_writes_status(self):
        rc = co.main(self._argv())
        self.assertEqual(rc, 0)
        st = self._status()
        self.assertEqual(st["outcome"], "ok")
        self.assertEqual(sorted(f["dataset_id"] for f in st["folded"]), ["12", "34"])
        self.assertIsNone(st["reason"])
        self.assertIsNone(dj.read_lock(), "lock released")
        self.assertIsNotNone(self.main_reader().get_wkb("whg:34:b0_0", cached=False))

    def test_lock_held_by_a_live_job_skips(self):
        self.assertTrue(dj.acquire_lock("1700000000000", "whg:99", os.getpid()))
        sha = _sha(self.store / INDEX_SQLITE_NAME)
        rc = co.main(self._argv())
        self.assertEqual(rc, 0)
        st = self._status()
        self.assertEqual(st["outcome"], "skipped")
        self.assertIn("skipped, job running", st["reason"])
        self.assert_store_untouched(sha)
        self.assertEqual(dj.read_lock()["job_id"], "1700000000000", "the job's lock is intact")
        dj.release_lock("1700000000000")
        self.assertEqual(co.main(self._argv()), 0)
        self.assertEqual(self._status()["outcome"], "ok")

    def test_low_memavailable_refused(self):
        sha = _sha(self.store / INDEX_SQLITE_NAME)
        with mock.patch.object(co, "read_meminfo_available_bytes", lambda: 1 << 30):
            rc = co.main(self._argv("--min-avail-gb", "6"))
        self.assertEqual(rc, 2)
        st = self._status()
        self.assertEqual(st["outcome"], "refused")
        self.assertIn("MemAvailable", st["reason"])
        self.assert_store_untouched(sha)
        with mock.patch.object(co, "free_bytes", lambda p: 1 << 30):
            rc = co.main(self._argv("--min-free-gb", "3"))
        self.assertEqual(rc, 2)
        self.assertIn("free space", self._status()["reason"])
        self.assert_store_untouched(sha)
        self.assertIsNone(dj.read_lock())
        with mock.patch.object(co, "read_meminfo_available_bytes", lambda: 64 << 30):
            self.assertEqual(co.main(self._argv("--min-avail-gb", "6")), 0)

    def test_failure_exits_nonzero_with_status(self):
        sha = _sha(self.store / INDEX_SQLITE_NAME)
        with mock.patch.object(co.Compactor, "_verify",
                               side_effect=co.CompactionError("verify boom")):
            rc = co.main(self._argv())
        self.assertEqual(rc, 1)
        st = self._status()
        self.assertEqual(st["outcome"], "failed")
        self.assertIn("verify boom", st["reason"])
        self.assert_store_untouched(sha)
        self.assertIsNone(dj.read_lock())

    def test_lock_file_option_selects_the_state_dir(self):
        other = self.tmp / "other-jobs"
        rc = co.main(self._argv("--lock-file", str(other / dj.LOCK_FILENAME)))
        self.assertEqual(rc, 0)
        self.assertTrue(other.is_dir())
        with self.assertRaises(SystemExit):
            co.main(self._argv("--lock-file", str(other / "wrong-name")))

    def test_meminfo_parser(self):
        p = self.tmp / "meminfo"
        p.write_text("MemTotal:       32000000 kB\nMemFree:  1000 kB\nMemAvailable:    12345678 kB\n")
        self.assertEqual(co.read_meminfo_available_bytes(str(p)), 12345678 * 1024)
        self.assertIsNone(co.read_meminfo_available_bytes(str(self.tmp / "absent")))


if __name__ == "__main__":
    unittest.main()
