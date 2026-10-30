#!/usr/bin/env python
"""Fold contributed-dataset geometry OVERLAYS into the main geom store, and
delete overlays that an earlier run folded once every live reader has moved
past them (place#341).

::

    python -m processing.compact_overlays [--dry-run] [--store-dir DIR]
        [--lock-file PATH] [--min-avail-gb 6] [--min-free-gb 3]

Runs nightly from ``scripts/compact_overlays_cron.sh`` on the Pitt VM, as the
``gazetteer`` user that owns the gateway. Exit status: 0 folded/deleted/
nothing to do/skipped (job lock held)/dry run; 2 refused by preflight; 1 failed
(store and overlays untouched).

What a run does, in order
~~~~~~~~~~~~~~~~~~~~~~~~~
1. **Lock.** Takes the SHARED dataset-job lock (``gateway.dataset_jobs``:
   ``O_CREAT|O_EXCL``, stale-breaking, the same file the VM dataset jobs use)
   so no job writes an overlay while it is being folded. Held → exit 0,
   ``skipped``.
2. **Preflight.** Refuses when ``MemAvailable`` (``/proc/meminfo``) is under
   ``--min-avail-gb`` or the store filesystem has under ``--min-free-gb``
   free: the VM also hosts production Elasticsearch.
3. **Delete** overlays folded by an EARLIER run — and only when every one of
   these holds, each verified now, not remembered:

   a. the overlay's CURRENT generation is listed in ``overlays/.compacted.json``;
   b. the CURRENT main index holds every one of its keys with identical bytes;
   c. at least one live reader heartbeat exists and every live heartbeat
      reports ``main_generation`` equal to the main index's current generation
      — i.e. no gateway worker is still serving an index that lacks the rows;
   d. the generation on disk is the one that was folded. A NEWER generation
      of the same dataset is never deleted; it is a fold candidate instead.

   Deletion runs BEFORE folding, because folding changes the main generation
   and heartbeats will not have caught up until the readers' next tick.
4. **Fold** every overlay not yet folded, streaming and with bounded RSS:
   copy ``index.sqlite`` to a temp file beside it; append each overlay's
   bytes into NEW ``geom_shard_NNNN.bin`` files numbered after the last;
   ``INSERT OR REPLACE`` the rows (a re-indexed dataset replaces its earlier
   rows); set ``meta.count = old + new − replaced``; fsync. Then **verify**
   through a fresh ``GeomStoreReader`` opened on the TEMP index with overlays
   disabled: the row count, and the bytes of EVERY folded key against the
   overlay's own bytes. Only then ``os.replace`` the temp index over
   ``index.sqlite``, refresh the backup (``backup_sqlite_index``) and record
   the fold in ``.compacted.json``. Any failure removes the temp index and the
   new shard files and leaves the live index byte-identical.

``index.json`` is not touched: the reader never opens it when
``index.sqlite`` exists, and ``consolidate_geom_store --merge`` now seeds from
``index.sqlite``. It is simply no longer maintained by this path.

Every run writes ``overlays/.last_compaction.json`` (one line) and logs.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from processing import geom_overlay as go
from processing.geom_store import (
    DEFAULT_SHARD_SIZE_BYTES,
    INDEX_SQLITE_NAME,
    Generation,
    GeomStoreReader,
    _SHARD_FILENAME_RE,
    backup_sqlite_index,
    main_generation_stamp,
    shard_filename,
)

logger = logging.getLogger("compact_overlays")

COMPACTED_NAME = ".compacted.json"
COMPACTED_SCHEMA = "whg-geom-overlay-compacted/1"
STATUS_NAME = ".last_compaction.json"
LOCK_NAMESPACE = "compact-overlays"
DEFAULT_MIN_AVAIL_GB = 6.0
DEFAULT_MIN_FREE_GB = 3.0
_BATCH_ROWS = 2000
_IN_CHUNK = 500


class CompactionError(RuntimeError):
    """A stage failed; the caller has restored the store to its prior state."""


class PreflightRefused(RuntimeError):
    """Memory or disk under the floor; nothing was attempted."""


# ---------------------------------------------------------------------------
# Small helpers (injectable)
# ---------------------------------------------------------------------------

def read_meminfo_available_bytes(path: str = "/proc/meminfo") -> Optional[int]:
    try:
        with open(path, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def free_bytes(path: str | Path) -> int:
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def last_shard_number(store_dir: Path) -> int:
    last = 0
    for p in store_dir.iterdir():
        m = _SHARD_FILENAME_RE.match(p.name)
        if m:
            last = max(last, int(m.group(1)))
    return last


def read_compacted(store_dir: Path) -> list[dict]:
    data = go.read_json_file(go.overlays_dir(store_dir) / COMPACTED_NAME)
    if not data:
        return []
    folded = data.get("folded")
    return [r for r in folded if isinstance(r, dict)] if isinstance(folded, list) else []


def write_compacted(store_dir: Path, records: list[dict]) -> None:
    go.write_json_atomic(go.overlays_dir(store_dir) / COMPACTED_NAME,
                          {"schema": COMPACTED_SCHEMA, "folded": records})


def write_status(store_dir: Path, payload: dict) -> None:
    try:
        go.write_json_atomic(go.overlays_dir(store_dir) / STATUS_NAME, payload)
    except OSError as exc:
        logger.warning("could not write %s: %s", STATUS_NAME, exc)


@dataclass
class Candidate:
    dataset_id: str
    generation: str
    gen_dir: Path
    manifest: dict

    @property
    def label(self) -> str:
        return f"whg-{self.dataset_id}/{self.generation}"


def discover_candidates(store_dir: Path) -> list[Candidate]:
    out: list[Candidate] = []
    for ds, gen in go.list_overlays(store_dir).items():
        gen_dir = go.overlay_dir(store_dir, ds) / gen
        manifest = go.read_manifest(gen_dir)
        if manifest is None or str(manifest.get("dataset_id")) != ds:
            logger.warning("overlay %s/%s: manifest missing or names another dataset; "
                           "skipped", ds, gen)
            continue
        if not (gen_dir / INDEX_SQLITE_NAME).exists():
            logger.warning("overlay whg-%s/%s: no %s; skipped", ds, gen, INDEX_SQLITE_NAME)
            continue
        out.append(Candidate(ds, gen, gen_dir, manifest))
    return out


# ---------------------------------------------------------------------------
# Shard appender: streams overlay bytes into new main-store shards
# ---------------------------------------------------------------------------

class _ShardAppender:
    """Writes into ``geom_shard_NNNN.bin.compact-tmp`` files, rolling at the
    shard size; ``finish()`` fsyncs and renames them to their final names.
    ``abort()`` removes everything it created."""

    def __init__(self, store_dir: Path, first_shard: int, shard_size: int):
        self.store = store_dir
        self.shard_size = shard_size
        self.num = first_shard - 1
        self.fh = None
        self.offset = 0
        self.tmp_paths: list[Path] = []
        self.final_paths: list[Path] = []
        self.bytes_written = 0

    def _roll(self) -> None:
        if self.fh is not None:
            self.fh.flush()
            os.fsync(self.fh.fileno())
            self.fh.close()
        self.num += 1
        path = self.store / (shard_filename(self.num) + ".compact-tmp")
        self.fh = open(path, "wb")
        self.tmp_paths.append(path)
        self.offset = 0

    def write(self, data: bytes) -> tuple[int, int]:
        if self.fh is None or (self.offset > 0 and self.offset + len(data) > self.shard_size):
            self._roll()
        off = self.offset
        self.fh.write(data)
        self.offset += len(data)
        self.bytes_written += len(data)
        return self.num, off

    def finish(self) -> list[Path]:
        if self.fh is not None:
            self.fh.flush()
            os.fsync(self.fh.fileno())
            self.fh.close()
            self.fh = None
        for tmp in self.tmp_paths:
            final = self.store / tmp.name[: -len(".compact-tmp")]
            os.replace(tmp, final)
            self.final_paths.append(final)
        self.tmp_paths = []
        go.fsync_path(self.store)
        return list(self.final_paths)

    def abort(self) -> None:
        if self.fh is not None:
            try:
                self.fh.close()
            except Exception:  # noqa: BLE001
                pass
            self.fh = None
        for p in self.tmp_paths + self.final_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        self.tmp_paths = []
        self.final_paths = []


# ---------------------------------------------------------------------------
# The compactor
# ---------------------------------------------------------------------------

@dataclass
class Compactor:
    store_dir: Path
    dry_run: bool = False
    min_avail_gb: float = DEFAULT_MIN_AVAIL_GB
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    shard_size_bytes: int = DEFAULT_SHARD_SIZE_BYTES
    backup: bool = True
    backup_dir: Optional[Path] = None
    job_id: str = ""
    # Injectable probes; None means the module-level function AT CALL TIME,
    # so a test may patch ``read_meminfo_available_bytes`` and drive ``main()``.
    meminfo: Optional[Callable[[], Optional[int]]] = None
    free_space: Optional[Callable[[Path], int]] = None
    heartbeats: Optional[Callable[[Path], list[dict]]] = None
    log: logging.Logger = logger

    def __post_init__(self):
        self.store_dir = Path(self.store_dir)
        self.folded: list[dict] = []
        self.deleted: list[dict] = []
        self.skipped: list[dict] = []
        self.would: list[dict] = []

    def _meminfo(self) -> Optional[int]:
        return (self.meminfo or read_meminfo_available_bytes)()

    def _free_space(self) -> int:
        return (self.free_space or free_bytes)(self.store_dir)

    def _heartbeats(self) -> list[dict]:
        if self.heartbeats is not None:
            return self.heartbeats(self.store_dir)
        return go.read_live_heartbeats(self.store_dir)

    # -- preflight -------------------------------------------------------------

    def preflight(self) -> None:
        live = self.store_dir / INDEX_SQLITE_NAME
        if not live.exists():
            raise PreflightRefused(f"{live} does not exist — nothing to fold into")
        avail = self._meminfo()
        floor = int(self.min_avail_gb * (1 << 30))
        if avail is None:
            raise PreflightRefused("MemAvailable unreadable")
        if avail < floor:
            raise PreflightRefused(
                f"MemAvailable {avail / (1 << 30):.2f} GB < {self.min_avail_gb:g} GB floor")
        free = self._free_space()
        floor = int(self.min_free_gb * (1 << 30))
        if free < floor:
            raise PreflightRefused(
                f"free space on {self.store_dir} {free / (1 << 30):.2f} GB < "
                f"{self.min_free_gb:g} GB floor")

    # -- deletion of previously folded overlays --------------------------------

    def _bytes_match_main(self, cand: Candidate, reader: GeomStoreReader) -> tuple[bool, str]:
        gen = Generation(cand.gen_dir, label=cand.label)
        try:
            n = 0
            for key, shard, off, ln in gen.iter_rows():
                ov = gen.read_wkb(key)
                mn = reader.get_wkb(key, cached=False)
                if ov is None:
                    return False, f"overlay bytes unreadable for {key}"
                if mn != ov:
                    return False, f"main store bytes differ for {key}"
                n += 1
            return True, f"{n} keys verified"
        finally:
            gen.close()

    def delete_folded(self, candidates: list[Candidate], records: list[dict]) -> list[Candidate]:
        """Rules a–d; returns the candidates that remain (to be folded, or kept)."""
        remaining: list[Candidate] = []
        by_key = {(str(r.get("dataset_id")), str(r.get("generation"))): r for r in records}
        main_gen = main_generation_stamp(self.store_dir)
        hbs = self._heartbeats()
        behind = [hb for hb in hbs if hb.get("main_generation") != main_gen]
        reader: Optional[GeomStoreReader] = None
        try:
            for cand in candidates:
                rec = by_key.get((cand.dataset_id, cand.generation))
                if rec is None:
                    # (a) not folded — or (d) a newer generation than the one
                    # folded. Either way it stays and will be folded.
                    remaining.append(cand)
                    continue
                reason = None
                if not hbs:
                    reason = "no live reader heartbeat"
                elif behind:
                    reason = (f"{len(behind)} live reader(s) still on another main generation "
                              f"(current {main_gen})")
                if reason is None:
                    if reader is None:
                        reader = GeomStoreReader(self.store_dir, lru_maxsize=0, overlays=False)
                    ok, detail = self._bytes_match_main(cand, reader)
                    if not ok:
                        # (b) failed: the main store does not hold what the
                        # record claims (a later full consolidation from an
                        # older staged tree, say). The overlay is still the
                        # authoritative copy, so forget the fold and fold it
                        # again this run rather than keep a record that lies.
                        self.log.error("NOT deleting %s — %s; its folded record is "
                                       "dropped and it will be folded again", cand.label, detail)
                        records[:] = [r for r in records
                                      if (str(r.get("dataset_id")), str(r.get("generation")))
                                      != (cand.dataset_id, cand.generation)]
                        if not self.dry_run:
                            write_compacted(self.store_dir, records)
                        self.skipped.append({"overlay": cand.label, "action": "delete",
                                             "reason": f"verification failed: {detail}"})
                        remaining.append(cand)
                        continue
                if reason is not None:
                    self.log.info("keeping folded overlay %s: %s", cand.label, reason)
                    self.skipped.append({"overlay": cand.label, "action": "delete",
                                         "reason": reason})
                    continue
                entry = {"dataset_id": cand.dataset_id, "generation": cand.generation,
                         "count": int(cand.manifest.get("count") or 0)}
                if self.dry_run:
                    self.log.info("DRY RUN: would delete folded overlay %s", cand.label)
                    self.would.append({"action": "delete", **entry})
                    continue
                go.remove_overlay(self.store_dir, cand.dataset_id)
                shutil.rmtree(cand.gen_dir, ignore_errors=True)
                go.prune_overlay_generations(self.store_dir, cand.dataset_id, keep=0,
                                             min_age_s=0.0)
                records[:] = [r for r in records
                              if (str(r.get("dataset_id")), str(r.get("generation")))
                              != (cand.dataset_id, cand.generation)]
                write_compacted(self.store_dir, records)
                self.deleted.append(entry)
                self.log.info("deleted folded overlay %s (%s)", cand.label, detail)
        finally:
            if reader is not None:
                reader.close()
        return remaining

    # -- fold stages (each separately patchable for failure injection) --------

    def _copy_index(self, live: Path, tmp: Path) -> None:
        shutil.copyfile(live, tmp)

    def _append_overlay(self, conn: sqlite3.Connection, cand: Candidate,
                        appender: _ShardAppender) -> tuple[int, int]:
        """Stream one overlay into the new shards + temp index. Returns
        ``(rows_added, rows_that_replaced_existing)``."""
        gen = Generation(cand.gen_dir, label=cand.label)
        n_new = n_replaced = 0

        def _flush(rows: list[tuple]) -> None:
            nonlocal n_new, n_replaced
            # Count BEFORE inserting: "replaced" means the key was already in
            # the index this batch is about to overwrite.
            n_replaced += self._count_existing(conn, [r[0] for r in rows])
            batch: list[tuple] = []
            for key, _shard, _off, ln in rows:
                data = gen.read_wkb(key)
                if data is None or len(data) != ln:
                    raise CompactionError(f"{cand.label}: unreadable bytes for {key}")
                new_shard, new_off = appender.write(data)
                batch.append((key, new_shard, new_off, ln))
            conn.executemany("INSERT OR REPLACE INTO geom VALUES(?,?,?,?)", batch)
            n_new += len(batch)

        try:
            rows_buf: list[tuple] = []
            for row in gen.iter_rows(batch=_BATCH_ROWS):
                rows_buf.append(row)
                if len(rows_buf) >= _BATCH_ROWS:
                    _flush(rows_buf)
                    rows_buf = []
            if rows_buf:
                _flush(rows_buf)
        finally:
            gen.close()
        return n_new, n_replaced

    @staticmethod
    def _count_existing(conn: sqlite3.Connection, keys: list[str]) -> int:
        n = 0
        for i in range(0, len(keys), _IN_CHUNK):
            chunk = keys[i:i + _IN_CHUNK]
            marks = ",".join("?" * len(chunk))
            n += conn.execute(f"SELECT count(*) FROM geom WHERE k IN ({marks})",
                              chunk).fetchone()[0]
        return n

    def _verify(self, tmp: Path, candidates: list[Candidate], expected_rows: int) -> None:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro&immutable=1", uri=True)
        try:
            n = conn.execute("SELECT count(*) FROM geom").fetchone()[0]
            meta = conn.execute("SELECT v FROM meta WHERE k='count'").fetchone()
        finally:
            conn.close()
        if n != expected_rows:
            raise CompactionError(f"row count {n:,} != expected {expected_rows:,}")
        if meta is None or int(meta[0]) != expected_rows:
            raise CompactionError(f"meta.count {meta and meta[0]} != expected {expected_rows:,}")
        reader = GeomStoreReader(self.store_dir, lru_maxsize=0, overlays=False, index_path=tmp)
        try:
            for cand in candidates:
                ok, detail = self._bytes_match_main(cand, reader)
                if not ok:
                    raise CompactionError(f"{cand.label}: {detail}")
                self.log.info("verified %s against the candidate index: %s", cand.label, detail)
        finally:
            reader.close()

    def _replace_index(self, tmp: Path, live: Path) -> None:
        go.fsync_path(tmp)
        os.replace(tmp, live)
        go.fsync_path(live.parent)

    def fold(self, candidates: list[Candidate], records: list[dict]) -> None:
        if not candidates:
            self.log.info("nothing to fold")
            return
        live = self.store_dir / INDEX_SQLITE_NAME
        if self.dry_run:
            for cand in candidates:
                self.log.info("DRY RUN: would fold %s (%s geometries)", cand.label,
                              cand.manifest.get("count"))
                self.would.append({"action": "fold", "dataset_id": cand.dataset_id,
                                   "generation": cand.generation,
                                   "count": int(cand.manifest.get("count") or 0)})
            return
        tmp = self.store_dir / f"{INDEX_SQLITE_NAME}.compact-{os.getpid()}.tmp"
        appender = _ShardAppender(self.store_dir, last_shard_number(self.store_dir) + 1,
                                  self.shard_size_bytes)
        conn: Optional[sqlite3.Connection] = None
        t0 = time.time()
        try:
            self._copy_index(live, tmp)
            conn = sqlite3.connect(str(tmp))
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            row = conn.execute("SELECT v FROM meta WHERE k='count'").fetchone()
            old_count = int(row[0]) if row else conn.execute(
                "SELECT count(*) FROM geom").fetchone()[0]
            per_overlay: list[tuple[Candidate, int, int]] = []
            total_new = total_replaced = 0
            for cand in candidates:
                n_new, n_replaced = self._append_overlay(conn, cand, appender)
                self.log.info("appended %s: %d rows (%d replace existing keys)",
                              cand.label, n_new, n_replaced)
                per_overlay.append((cand, n_new, n_replaced))
                total_new += n_new
                total_replaced += n_replaced
            expected = old_count + total_new - total_replaced
            conn.execute("INSERT OR REPLACE INTO meta VALUES('count', ?)", (str(expected),))
            conn.commit()
            conn.close()
            conn = None
            new_shards = appender.finish()
            self._verify(tmp, candidates, expected)
            self._replace_index(tmp, live)
            after = main_generation_stamp(self.store_dir)
            self.log.info("index.sqlite replaced: %d -> %d rows, generation %s, "
                          "%d new shard(s) (%s), %.1fs", old_count, expected, after,
                          len(new_shards), ", ".join(p.name for p in new_shards),
                          time.time() - t0)
            now = _utc_iso()
            for cand, n_new, n_replaced in per_overlay:
                records[:] = [r for r in records if str(r.get("dataset_id")) != cand.dataset_id]
                rec = {"dataset_id": cand.dataset_id, "generation": cand.generation,
                       "folded_at": now, "main_generation_after": after, "count": n_new,
                       "replaced": n_replaced, "job_id": self.job_id}
                records.append(rec)
                self.folded.append(rec)
            write_compacted(self.store_dir, records)
            if self.backup:
                backup_sqlite_index(self.store_dir, backup_dir=self.backup_dir)
        except BaseException as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            appender.abort()
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self.log.error("fold FAILED (%s); live index and overlays untouched", exc)
            raise

    # -- run -------------------------------------------------------------------

    def run(self) -> dict:
        """Preflight → delete → fold. Returns the status payload; raises
        :class:`PreflightRefused` / :class:`CompactionError` / others."""
        self.folded, self.deleted, self.skipped, self.would = [], [], [], []
        before = main_generation_stamp(self.store_dir)
        self.preflight()
        candidates = discover_candidates(self.store_dir)
        records = read_compacted(self.store_dir)
        self.log.info("store %s: main generation %s, %d overlay(s), %d folded record(s)",
                      self.store_dir, before, len(candidates), len(records))
        remaining = self.delete_folded(candidates, records)
        self.fold(remaining, records)
        return {
            "outcome": "dry-run" if self.dry_run else "ok",
            "main_generation_before": before,
            "main_generation_after": main_generation_stamp(self.store_dir),
            "folded": self.folded,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "would": self.would,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    if logger.handlers:
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _resolve_lock(lock_file: Optional[str]):
    """The shared dataset-job lock lives at ``<REINGEST_STATE_DIR>/.lock``;
    ``--lock-file`` names that file and so selects its directory."""
    from gateway import dataset_jobs as dj
    if lock_file:
        p = Path(lock_file)
        if p.name != dj.LOCK_FILENAME:
            raise SystemExit(f"--lock-file must be named {dj.LOCK_FILENAME} "
                             f"(the dataset-job lock), got {p.name!r}")
        os.environ[dj.STATE_DIR_ENV] = str(p.parent)
    return dj


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be folded/deleted; write nothing but the status")
    ap.add_argument("--store-dir", default=None, help="geom store (default GEOM_STORE_DIR)")
    ap.add_argument("--lock-file", default=None,
                    help="the shared dataset-job lock (default <REINGEST_STATE_DIR>/.lock)")
    ap.add_argument("--min-avail-gb", type=float, default=DEFAULT_MIN_AVAIL_GB)
    ap.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB)
    ap.add_argument("--no-backup", action="store_true",
                    help="skip backup_sqlite_index after a fold (tests)")
    args = ap.parse_args(argv)
    _configure_logging()

    if args.store_dir:
        store_dir = Path(args.store_dir)
    else:
        from processing.settings import GEOM_STORE_DIR
        store_dir = Path(GEOM_STORE_DIR)
    dj = _resolve_lock(args.lock_file)
    job_id = dj.new_job_id()
    ts = _utc_iso()
    status: dict = {"ts": ts, "job_id": job_id, "dry_run": bool(args.dry_run),
                    "store_dir": str(store_dir)}

    if not dj.acquire_lock(job_id, LOCK_NAMESPACE, os.getpid()):
        lock = dj.read_lock() or {}
        reason = (f"skipped, job running: lock {dj.lock_path()} held by job "
                  f"{lock.get('job_id')} ({lock.get('namespace')}, pid {lock.get('pid')})")
        logger.info(reason)
        write_status(store_dir, {**status, "outcome": "skipped", "folded": [],
                                 "deleted": [], "reason": reason})
        return 0
    try:
        comp = Compactor(store_dir, dry_run=args.dry_run, min_avail_gb=args.min_avail_gb,
                         min_free_gb=args.min_free_gb, backup=not args.no_backup,
                         job_id=job_id)
        try:
            result = comp.run()
        except PreflightRefused as exc:
            logger.error("REFUSED: %s", exc)
            write_status(store_dir, {**status, "outcome": "refused", "folded": [],
                                     "deleted": [], "reason": str(exc)})
            return 2
        except Exception as exc:  # noqa: BLE001
            logger.exception("FAILED: %s", exc)
            write_status(store_dir, {**status, "outcome": "failed", "folded": comp.folded,
                                     "deleted": comp.deleted,
                                     "reason": f"{exc.__class__.__name__}: {exc}"[:500]})
            return 1
        payload = {**status, **result, "reason": None}
        write_status(store_dir, payload)
        logger.info("done: outcome=%s folded=%d deleted=%d skipped=%d would=%d",
                    payload["outcome"], len(payload["folded"]), len(payload["deleted"]),
                    len(payload["skipped"]), len(payload["would"]))
        return 0
    finally:
        dj.release_lock(job_id)


if __name__ == "__main__":
    sys.exit(main())
