# processing/geom_overlay.py

"""
Per-dataset geometry OVERLAYS for the geom store (place#341).

Why
~~~
A contributed WHG dataset is indexed by a VM-local job (``gateway/dataset_jobs``
→ ``processing/local_dataset_job``) on the host that runs production. That job
cannot merge polygons into the main geom store: a merge rewrites
``index.sqlite`` for 11.5 M rows, and the one time a partial write reached the
live index it stranded the whole store (``geom_store.backup_sqlite_index``).
So the job writes the dataset's geometries to an overlay of its own, and the
reader serves them from there until a nightly compaction folds them into the
main store (``processing.compact_overlays``).

Layout
~~~~~~
::

    <store>/overlays/
        whg-<dataset_id>/
            CURRENT  -> <generation>            (relative symlink, swapped atomically)
            <generation>/                        (a normal mini store:)
                geom_shard_0001.bin
                index.sqlite                     (same schema/code as the main index)
                index.json                       (written by consolidate; unused)
                manifest.json                    {schema, dataset_id, generation,
                                                  job_id, count, bytes, created_at}
            .tmp-<generation>/                   (being written; readers skip '.' names)
        .compacted.json                          (compactor: which generations were folded)
        .last_compaction.json                    (compactor: one-line status)
    <store>/.reader_heartbeat/<pid>.json         (readers announcing their generations)

Rules the reader (``geom_store.GeomStoreReader``) and this module share:

* A key ``whg:<digits>:…`` is looked up in overlay ``whg-<digits>`` FIRST and
  in the main store only if the overlay lacks it. Routing is by the STORE KEY
  prefix, validated by a regex; any other key never touches an overlay.
* An overlay is live iff ``CURRENT`` resolves to a generation directory the
  reader can open. No ``CURRENT``, a ``.``-prefixed name, a missing or corrupt
  ``index.sqlite``, a manifest naming another dataset, or an over-budget
  generation: skipped, logged once per overlay+reason, re-tried next rescan.
* The writer takes NO lock. The VM job that calls it already holds the global
  dataset-job lock (``gateway.dataset_jobs``); ``write_overlay`` only asserts
  that the lock file it is told about exists, so an unlocked call fails loudly.

Caps (env-overridable) bound what a reader can be made to hold:
``GEOM_OVERLAY_MAX_GEOMETRIES`` (50 000 rows per overlay),
``GEOM_OVERLAY_MAX_MB`` (256 MB of shards per overlay) and
``GEOM_OVERLAY_MAX_OVERLAYS`` (200 overlays per reader). The writer refuses
over budget with :class:`OverlayBudgetExceeded`; the registry skips (and, in
strict mode, raises).
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from processing.geom_store import (
    DEFAULT_GENERATION_GRACE_S,
    INDEX_SQLITE_NAME,
    Generation,
    consolidate_geom_store,
)

OVERLAYS_DIRNAME = "overlays"
CURRENT_LINK = "CURRENT"
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "whg-geom-overlay/1"
TMP_PREFIX = ".tmp-"
HEARTBEAT_DIRNAME = ".reader_heartbeat"
HEARTBEAT_SCHEMA = "whg-geom-reader-heartbeat/1"
#: A heartbeat older than this is not evidence of a live reader.
HEARTBEAT_STALE_S = 600.0

MAX_GEOMETRIES_ENV = "GEOM_OVERLAY_MAX_GEOMETRIES"
MAX_MB_ENV = "GEOM_OVERLAY_MAX_MB"
MAX_OVERLAYS_ENV = "GEOM_OVERLAY_MAX_OVERLAYS"
DEFAULT_MAX_GEOMETRIES = 50_000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_OVERLAYS = 200

_OVERLAY_DIR_RE = re.compile(r"^whg-(\d+)$")
_DATASET_ID_RE = re.compile(r"^\d+$")
_GEN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class OverlayError(RuntimeError):
    """Base for overlay write/registry failures."""


class OverlayBudgetExceeded(OverlayError):
    """An overlay (or the overlay set) is over the configured cap."""


class OverlayEmpty(OverlayError):
    """The staging directory held no geometry and ``allow_empty`` was not given."""


class OverlayLockMissing(OverlayError):
    """``write_overlay`` was called without the caller's lock token present."""


# ---------------------------------------------------------------------------
# Caps and small helpers
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def max_geometries() -> int:
    return _env_int(MAX_GEOMETRIES_ENV, DEFAULT_MAX_GEOMETRIES)


def max_bytes() -> int:
    mb = os.environ.get(MAX_MB_ENV)
    if mb is None:
        return DEFAULT_MAX_BYTES
    try:
        return int(float(mb) * 1024 * 1024)
    except ValueError:
        return DEFAULT_MAX_BYTES


def max_overlays() -> int:
    return _env_int(MAX_OVERLAYS_ENV, DEFAULT_MAX_OVERLAYS)


def validate_dataset_id(dataset_id) -> str:
    """Strictly digits — it becomes a directory name and a key prefix."""
    ds = str(dataset_id).strip()
    if not _DATASET_ID_RE.match(ds):
        raise ValueError(f"dataset id must be digits, got {dataset_id!r}")
    return ds


def overlays_dir(store_dir: str | Path) -> Path:
    return Path(store_dir) / OVERLAYS_DIRNAME


def overlay_dir(store_dir: str | Path, dataset_id) -> Path:
    return overlays_dir(store_dir) / f"whg-{validate_dataset_id(dataset_id)}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root: Path) -> None:
    for p in root.iterdir():
        if p.is_file():
            fsync_path(p)
    fsync_path(root)


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json_file(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def shard_bytes(gen_dir: Path) -> int:
    return sum(p.stat().st_size for p in gen_dir.glob("geom_shard_*.bin"))


def read_manifest(gen_dir: Path) -> Optional[dict]:
    return read_json_file(Path(gen_dir) / MANIFEST_NAME)


def current_generation(store_dir: str | Path, dataset_id) -> Optional[str]:
    """The generation name ``CURRENT`` points at, or None (no overlay)."""
    try:
        target = os.readlink(overlay_dir(store_dir, dataset_id) / CURRENT_LINK)
    except OSError:
        return None
    name = os.path.basename(target.rstrip("/"))
    return name or None


def list_overlays(store_dir: str | Path) -> dict[str, str]:
    """``{dataset_id: generation}`` for every overlay with a readable ``CURRENT``.
    ``listdir`` + ``readlink`` only — nothing is opened."""
    out: dict[str, str] = {}
    try:
        names = os.listdir(overlays_dir(store_dir))
    except OSError:
        return out
    for name in sorted(names):
        m = _OVERLAY_DIR_RE.match(name)
        if not m:
            continue
        gen = current_generation(store_dir, m.group(1))
        if gen and not gen.startswith(".") and _GEN_NAME_RE.match(gen):
            out[m.group(1)] = gen
    return out


# ---------------------------------------------------------------------------
# Registry (one per reader)
# ---------------------------------------------------------------------------

class OverlayRegistry:
    """The overlays one reader is serving: ``{dataset_id: Generation}``.

    ``rescan(now)`` lists the overlays directory and readlinks each
    ``CURRENT`` — never a stat per key, never opening anything whose link
    text has not changed — and swaps the whole map by one assignment.
    A replaced or removed generation is retired and closed after
    ``grace_s``; a change of any kind returns True so the reader can clear
    its LRU and bump its epoch. Nothing here raises into a request:
    unreadable overlays are skipped and reported through ``log_once``.
    """

    def __init__(self, store_dir: str | Path, *, clock: Callable[[], float] | None = None,
                 grace_s: float = DEFAULT_GENERATION_GRACE_S,
                 log_once: Callable[[str, Optional[str]], None] | None = None,
                 max_overlays_: int | None = None,
                 max_geometries_: int | None = None,
                 max_bytes_: int | None = None):
        self.dir = overlays_dir(store_dir)
        self._clock = clock or time.monotonic
        self._grace_s = float(grace_s)
        self._log_once = log_once or self._default_log_once
        self._logged: set[str] = set()
        self._max_overlays = max_overlays_
        self._max_geometries = max_geometries_
        self._max_bytes = max_bytes_
        self._gens: dict[str, Generation] = {}
        self._names: dict[str, str] = {}
        self._retired: list[tuple[Generation, float]] = []
        self.epoch = 0

    # -- logging ---------------------------------------------------------------

    def _default_log_once(self, key: str, message: Optional[str]) -> None:
        if message is None:
            self._logged.discard(key)
            return
        if key in self._logged:
            return
        self._logged.add(key)
        print(f"geom-overlay: {message}", flush=True)

    # -- queries (hot path: dict lookups only) ---------------------------------

    def get(self, dataset_id: str) -> Optional[Generation]:
        return self._gens.get(dataset_id)

    def generations(self) -> dict[str, str]:
        return dict(self._names)

    def __len__(self) -> int:
        return len(self._gens)

    # -- rescan ----------------------------------------------------------------

    def _scan_current(self) -> dict[str, str]:
        """``{dataset_id: generation}`` from ``listdir`` + ``readlink``."""
        try:
            names = os.listdir(self.dir)
        except FileNotFoundError:
            self._log_once("overlays-dir", None)
            return {}
        except OSError as exc:
            self._log_once("overlays-dir", f"cannot list {self.dir}: {exc}")
            return {}
        self._log_once("overlays-dir", None)
        seen: dict[str, str] = {}
        for name in sorted(names):
            if name.startswith("."):
                continue
            m = _OVERLAY_DIR_RE.match(name)
            if not m:
                continue
            ds = m.group(1)
            link = self.dir / name / CURRENT_LINK
            try:
                target = os.readlink(link)
            except FileNotFoundError:
                # Removed, or being written for the first time: not live.
                self._log_once(f"overlay:{ds}:no-current", None)
                continue
            except OSError as exc:
                self._log_once(f"overlay:{ds}:bad-current",
                               f"overlay whg-{ds}: CURRENT unreadable ({exc}); skipped")
                continue
            gen = os.path.basename(target.rstrip("/"))
            if not gen or gen.startswith(".") or not _GEN_NAME_RE.match(gen):
                self._log_once(f"overlay:{ds}:bad-current",
                               f"overlay whg-{ds}: CURRENT -> {target!r} is not a "
                               f"generation name; skipped")
                continue
            self._log_once(f"overlay:{ds}:bad-current", None)
            seen[ds] = gen
        return seen

    def _open(self, ds: str, gen_name: str) -> Generation:
        """Open and validate one generation; raises on anything unusable."""
        gen_dir = self.dir / f"whg-{ds}" / gen_name
        manifest = read_manifest(gen_dir)
        if manifest is None:
            raise OverlayError(f"{MANIFEST_NAME} missing or unreadable")
        if str(manifest.get("dataset_id")) != ds:
            raise OverlayError(
                f"manifest names dataset {manifest.get('dataset_id')!r}, directory is whg-{ds}")
        cap_rows = self._max_geometries if self._max_geometries is not None else max_geometries()
        cap_bytes = self._max_bytes if self._max_bytes is not None else max_bytes()
        count = int(manifest.get("count") or 0)
        if count > cap_rows:
            raise OverlayBudgetExceeded(
                f"overlay whg-{ds}/{gen_name} holds {count:,} geometries, cap {cap_rows:,}")
        size = shard_bytes(gen_dir)
        if size > cap_bytes:
            raise OverlayBudgetExceeded(
                f"overlay whg-{ds}/{gen_name} shards are {size:,} bytes, cap {cap_bytes:,}")
        return Generation(gen_dir, label=f"overlay whg-{ds}/{gen_name}")

    def rescan(self, now: float | None = None, *, strict: bool = False) -> bool:
        """Bring the map up to date with the directory. Returns True on change.

        ``strict=True`` re-raises :class:`OverlayBudgetExceeded` instead of
        skipping the offending overlay (for tooling); the reader never uses it.
        """
        now = self._clock() if now is None else now
        seen = self._scan_current()
        cap = self._max_overlays if self._max_overlays is not None else max_overlays()
        if len(seen) > cap:
            msg = (f"{len(seen)} overlays present, cap {cap}; serving the first {cap} "
                   f"by dataset id and skipping the rest")
            self._log_once("overlay-count", msg)
            if strict:
                raise OverlayBudgetExceeded(msg)
            seen = dict(sorted(seen.items(), key=lambda kv: int(kv[0]))[:cap])
        else:
            self._log_once("overlay-count", None)

        new_gens = dict(self._gens)
        new_names = dict(self._names)
        changed = False
        for ds in list(new_gens):
            if ds not in seen:
                self._retired.append((new_gens.pop(ds), now + self._grace_s))
                new_names.pop(ds, None)
                changed = True
                print(f"geom-overlay: overlay whg-{ds} removed", flush=True)
        for ds, gen_name in seen.items():
            if new_names.get(ds) == gen_name:
                continue
            try:
                gen = self._open(ds, gen_name)
            except OverlayBudgetExceeded as exc:
                self._log_once(f"overlay:{ds}:budget", f"{exc}; skipped")
                if strict:
                    raise
                gen = None
            except Exception as exc:  # noqa: BLE001 — skip, retry next rescan
                self._log_once(f"overlay:{ds}:open",
                               f"overlay whg-{ds}/{gen_name} unusable ({exc!r}); skipped")
                gen = None
            else:
                self._log_once(f"overlay:{ds}:budget", None)
                self._log_once(f"overlay:{ds}:open", None)
            old = new_gens.pop(ds, None)
            new_names.pop(ds, None)
            if old is not None:
                self._retired.append((old, now + self._grace_s))
                changed = True
            if gen is not None:
                new_gens[ds] = gen
                new_names[ds] = gen_name
                changed = True
                print(f"geom-overlay: overlay whg-{ds} -> {gen_name} "
                      f"({gen.count if gen.count is not None else '?'} entries)", flush=True)
        if changed:
            # Two assignments, but each is atomic and readers only ever need
            # _gens; _names is reporting.
            self._gens = new_gens
            self._names = new_names
            self.epoch += 1
        return changed

    def close_retired(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        keep = []
        for gen, close_at in self._retired:
            if now >= close_at:
                gen.close()
            else:
                keep.append((gen, close_at))
        self._retired = keep

    def close(self) -> None:
        for gen in self._gens.values():
            gen.close()
        for gen, _ in self._retired:
            gen.close()
        self._gens = {}
        self._names = {}
        self._retired = []


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def _generation_name(job_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-j{job_id}"


def _staging_totals(staging_dir: Path) -> tuple[int, int]:
    """``(entries, bytes)`` promised by the staging indexes, before packing."""
    n = total = 0
    for idx in sorted(staging_dir.glob("*.index.json")):
        with idx.open() as fh:
            entries = json.load(fh)
        n += len(entries)
        total += sum(int(e.get("length") or 0) for e in entries)
    return n, total


def write_overlay(
    store_dir: str | Path,
    dataset_id,
    staging_dir: str | Path,
    job_id,
    *,
    lock_token: str | Path,
    allow_empty: bool = False,
    max_geometries_: int | None = None,
    max_bytes_: int | None = None,
    delete_staging: bool = False,
) -> dict:
    """Pack ``staging_dir`` (``GeomStoreWriter`` output) into a new generation of
    the dataset's overlay and make it CURRENT. Returns the manifest.

    * Builds into ``.tmp-<generation>`` with the ordinary
      :func:`consolidate_geom_store` (``merge_with_existing=False``,
      ``backup=False``), fsyncs every file and the directory, renames it into
      place, then swaps ``CURRENT`` by creating a temporary symlink and
      ``os.replace``-ing it over the old one — a reader sees the previous
      generation or the new one, never a missing link.
    * Takes no lock: ``lock_token`` is the path of the lock the CALLER holds
      (the VM dataset-job lock) and must exist, else :class:`OverlayLockMissing`.
    * Refuses an empty staging set (:class:`OverlayEmpty`) unless ``allow_empty``.
    * Refuses over budget (:class:`OverlayBudgetExceeded`), before packing
      when the staging indexes already show it and again on the packed result.
    """
    ds = validate_dataset_id(dataset_id)
    jid = str(job_id).strip()
    if not jid.isdigit():
        raise ValueError(f"job id must be digits, got {job_id!r}")
    token = Path(lock_token)
    if not token.exists():
        raise OverlayLockMissing(
            f"lock token {token} does not exist — write_overlay must be called by a "
            f"job that holds the dataset-job lock")
    staging = Path(staging_dir)
    if not staging.is_dir():
        raise FileNotFoundError(f"overlay staging directory does not exist: {staging}")

    cap_rows = max_geometries_ if max_geometries_ is not None else max_geometries()
    cap_bytes = max_bytes_ if max_bytes_ is not None else max_bytes()
    n_staged, bytes_staged = _staging_totals(staging)
    if n_staged == 0 and not allow_empty:
        raise OverlayEmpty(f"staging {staging} holds 0 geometry entries for whg:{ds}")
    if n_staged > cap_rows:
        raise OverlayBudgetExceeded(
            f"whg:{ds} stages {n_staged:,} geometries, cap {cap_rows:,}")
    if bytes_staged > cap_bytes:
        raise OverlayBudgetExceeded(
            f"whg:{ds} stages {bytes_staged:,} bytes of WKB, cap {cap_bytes:,}")

    odir = overlay_dir(store_dir, ds)
    odir.mkdir(parents=True, exist_ok=True)
    gen_name = _generation_name(jid)
    n = 2
    while (odir / gen_name).exists() or (odir / (TMP_PREFIX + gen_name)).exists():
        gen_name = f"{_generation_name(jid)}_{n}"
        n += 1
    tmp_dir = odir / (TMP_PREFIX + gen_name)
    gen_dir = odir / gen_name

    try:
        tmp_dir.mkdir()
        written = consolidate_geom_store(
            staging_dir=staging, output_dir=tmp_dir, delete_staging=delete_staging,
            merge_with_existing=False, backup=False,
        )
        if written == 0 and not allow_empty:
            raise OverlayEmpty(f"consolidation packed 0 geometries for whg:{ds}")
        if written > cap_rows:
            raise OverlayBudgetExceeded(
                f"whg:{ds} packed {written:,} geometries, cap {cap_rows:,}")
        size = shard_bytes(tmp_dir)
        if size > cap_bytes:
            raise OverlayBudgetExceeded(
                f"whg:{ds} packed {size:,} bytes of shards, cap {cap_bytes:,}")
        if written and not (tmp_dir / INDEX_SQLITE_NAME).exists():
            raise OverlayError(f"consolidation left no {INDEX_SQLITE_NAME} in {tmp_dir}")
        if not written:
            # consolidate writes nothing for an empty set; an empty overlay
            # still needs an index so a reader can open it.
            from processing.geom_store import write_sqlite_index
            write_sqlite_index(iter(()), tmp_dir)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "dataset_id": ds,
            "generation": gen_name,
            "job_id": jid,
            "count": int(written),
            "bytes": int(size),
            "created_at": _utc_now_iso(),
        }
        write_json_atomic(tmp_dir / MANIFEST_NAME, manifest)
        fsync_tree(tmp_dir)
        os.rename(tmp_dir, gen_dir)
        fsync_path(odir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    _swap_current(odir, gen_name)
    print(f"geom-overlay: wrote whg-{ds}/{gen_name} ({written:,} geometries, "
          f"{size:,} bytes) and made it CURRENT", flush=True)
    return manifest


def _swap_current(odir: Path, gen_name: str) -> None:
    """Point ``CURRENT`` at *gen_name* atomically (tmp symlink + os.replace)."""
    tmp_link = odir / f".{CURRENT_LINK}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.unlink(tmp_link)
    except FileNotFoundError:
        pass
    os.symlink(gen_name, tmp_link)
    os.replace(tmp_link, odir / CURRENT_LINK)
    fsync_path(odir)


def remove_overlay(store_dir: str | Path, dataset_id) -> bool:
    """Unlink ``CURRENT`` so readers drop the overlay on their next rescan.
    Generation directories are left for :func:`prune_overlay_generations`.
    Returns True if a link was removed."""
    link = overlay_dir(store_dir, dataset_id) / CURRENT_LINK
    try:
        os.unlink(link)
    except FileNotFoundError:
        return False
    fsync_path(link.parent)
    print(f"geom-overlay: overlay whg-{validate_dataset_id(dataset_id)} CURRENT removed",
          flush=True)
    return True


def prune_overlay_generations(
    store_dir: str | Path,
    dataset_id=None,
    *,
    keep: int = 2,
    min_age_s: float = 300.0,
    now: float | None = None,
) -> list[Path]:
    """Delete superseded generation directories (and stale ``.tmp-`` builds).

    Keeps the ``CURRENT`` target always, plus the ``keep`` newest other
    generations; deletes nothing younger than ``min_age_s`` (by directory
    mtime), which must exceed a reader's rescan interval + grace so a reader
    still finishing on a just-superseded generation is never pulled out from
    under. An overlay directory with no ``CURRENT`` and nothing left in it is
    removed. Returns the paths removed.
    """
    now = time.time() if now is None else now
    removed: list[Path] = []
    root = overlays_dir(store_dir)
    if dataset_id is not None:
        dirs = [overlay_dir(store_dir, dataset_id)]
    else:
        try:
            dirs = [root / n for n in sorted(os.listdir(root)) if _OVERLAY_DIR_RE.match(n)]
        except OSError:
            return removed
    for odir in dirs:
        if not odir.is_dir():
            continue
        try:
            current = os.path.basename(os.readlink(odir / CURRENT_LINK).rstrip("/"))
        except OSError:
            current = None
        gens, tmps = [], []
        for p in odir.iterdir():
            if p.name == CURRENT_LINK or not p.is_dir():
                continue
            (tmps if p.name.startswith(".") else gens).append(p)
        gens.sort(key=lambda p: p.name)       # names are UTC stamps: chronological
        candidates = [p for p in gens if p.name != current]
        doomed = candidates[:-keep] if keep > 0 else candidates
        for p in doomed + tmps:
            try:
                if now - p.stat().st_mtime < min_age_s:
                    continue
                shutil.rmtree(p)
                removed.append(p)
            except OSError:
                continue
        if current is None:
            try:
                if not any(odir.iterdir()):
                    odir.rmdir()
                    removed.append(odir)
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# Reader heartbeats
# ---------------------------------------------------------------------------

def heartbeat_dir(store_dir: str | Path) -> Path:
    return Path(store_dir) / HEARTBEAT_DIRNAME


def heartbeat_path(store_dir: str | Path, pid: int | None = None) -> Path:
    return heartbeat_dir(store_dir) / f"{os.getpid() if pid is None else pid}.json"


_atexit_registered: set[tuple[str, int]] = set()
_atexit_lock = threading.Lock()


def _atexit_remove(store: str, pid: int) -> None:
    # Handlers are inherited across fork(): only the process that wrote the
    # file may remove it, or a forked child exiting would delete its parent's.
    if os.getpid() != pid:
        return
    try:
        os.unlink(Path(store) / HEARTBEAT_DIRNAME / f"{pid}.json")
    except OSError:
        pass


def write_heartbeat(store_dir: str | Path, *, main_generation: str,
                    overlays: dict[str, str], now: float | None = None) -> Path:
    """Announce this process's generations. Atomic; registers removal at exit."""
    pid = os.getpid()
    path = heartbeat_path(store_dir, pid)
    ts = time.time() if now is None else now
    payload = {
        "schema": HEARTBEAT_SCHEMA,
        "pid": pid,
        "host": socket.gethostname(),
        "main_generation": main_generation,
        "overlays": dict(overlays),
        "ts": ts,
        "ts_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"),
    }
    write_json_atomic(path, payload)
    key = (str(store_dir), pid)
    with _atexit_lock:
        if key not in _atexit_registered:
            _atexit_registered.add(key)
            atexit.register(_atexit_remove, str(store_dir), pid)
    return path


def remove_heartbeat(store_dir: str | Path, pid: int | None = None) -> bool:
    try:
        os.unlink(heartbeat_path(store_dir, pid))
    except OSError:
        return False
    return True


def pid_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_live_heartbeats(store_dir: str | Path, *, now: float | None = None,
                         stale_s: float = HEARTBEAT_STALE_S,
                         pid_alive_fn: Callable[[int], bool] = pid_alive,
                         host: str | None = None) -> list[dict]:
    """Heartbeats that count as evidence of a live reader.

    Ignored: unparsable files; ``ts`` older than ``stale_s``; and, for a
    heartbeat written on THIS host, a dead pid. A heartbeat from another host
    cannot be pid-checked and is judged on its timestamp alone.
    """
    now = time.time() if now is None else now
    host = socket.gethostname() if host is None else host
    out: list[dict] = []
    d = heartbeat_dir(store_dir)
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in sorted(names):
        if not name.endswith(".json") or name.startswith("."):
            continue
        hb = read_json_file(d / name)
        if hb is None:
            continue
        ts = hb.get("ts")
        if not isinstance(ts, (int, float)) or now - ts > stale_s:
            continue
        if hb.get("host", host) == host and not pid_alive_fn(hb.get("pid")):
            continue
        hb["_path"] = str(d / name)
        out.append(hb)
    return out
