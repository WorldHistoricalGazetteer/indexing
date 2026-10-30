# processing/geom_store.py

"""
External geometry store for the WHG places index.

Full GeoJSON geometries are no longer stored in Elasticsearch.  Instead they
are written to a chunked binary archive on the VAST filesystem
(``/vast/ishi/geom/`` by default), keyed by ``{place_id}_{geom_idx}``.

Architecture
~~~~~~~~~~~~
Ingestion (per authority)
  - One ``GeomStoreWriter`` is opened per authority run.
  - Each non-trivial geometry is serialised as WKB and appended to a
    per-authority staging file (``<staging_dir>/<namespace>.bin``).
  - A matching staging index (``<staging_dir>/<namespace>.index.json``)
    records ``{key, h3_centroid_r3, offset, length, file}`` per entry.

Consolidation (post-ingestion, run once)
  - ``consolidate_geom_store()`` reads all staging files, sorts by H3 centroid
    at resolution 3 (≈ 69 km hexagons) for geographic locality, and writes
    spatially-sharded shard files (``geom_shard_NNNN.bin``) plus a combined
    ``index.json`` mapping each key → ``{file, offset, length}``.
  - Staging files are deleted after successful consolidation.

Query / gateway
  - ``GeomStoreReader`` loads ``index.json`` on first access and exposes
    ``get(geom_key) → GeoJSON dict | None``.
  - An optional ``lru_maxsize`` parameter adds an in-process LRU cache for
    recently-loaded geometries (hot regions remain in memory after first read).

Key format
~~~~~~~~~~
  ``{place_id}_{geom_idx}``  e.g.  ``"osm:r12345_0"``,  ``"wd:Q90_0"``

Dependencies
~~~~~~~~~~~~
  - shapely (already a project dependency)
  - h3 (add to requirements: ``pip install h3``)
  - Standard library only (struct, json, os, pathlib, functools)
"""

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path

try:
    import h3 as _h3
    _H3_AVAILABLE = True
except ImportError:
    _H3_AVAILABLE = False

try:
    from shapely.geometry import shape as shapely_shape
    from shapely import wkb as shapely_wkb
    _SHAPELY_AVAILABLE = True
except ImportError:
    _SHAPELY_AVAILABLE = False

# ── H3 resolution used for spatial sharding during consolidation ───────────
# r3 ≈ 69 km hexagons — coarse enough to keep shard count manageable while
# ensuring a typical search result set spans only a few shards.
_SHARD_H3_RESOLUTION = 3

# Default shard size (bytes).  256 MB gives ~1 000–4 000 shards across 47 M
# records with an average WKB size of ~2–8 KB per polygon record.
DEFAULT_SHARD_SIZE_BYTES = 256 * 1024 * 1024  # 256 MB

# ── Index file names ───────────────────────────────────────────────────────
INDEX_JSON_NAME = "index.json"
INDEX_SQLITE_NAME = "index.sqlite"

# ── Overlay routing ────────────────────────────────────────────────────────
# Contributed-dataset geometries (``whg:<dataset_id>:<src_id>_<idx>``) may be
# served from a per-dataset OVERLAY (``processing.geom_overlay``) that a VM job
# writes without touching the main store. Routing is by the STORE KEY prefix —
# never by a place id the caller derived — and only keys of this exact shape
# are ever looked up in an overlay. Every other key pays one ``startswith``.
OVERLAY_KEY_PREFIX = "whg:"
_OVERLAY_KEY_RE = re.compile(r"^whg:(\d+):")

# Reader reload cadence: how often (at most) a reader lists the overlays
# directory and stats the main index for a new generation. Driven from the
# request path (``get``/``get_wkb``/``__contains__``), so an idle reader costs
# nothing; a busy one pays one ``listdir`` + one ``stat`` per interval.
RESCAN_ENV = "GEOM_OVERLAY_RESCAN_S"
DEFAULT_RESCAN_S = 2.0
# A superseded generation (main or overlay) stays open this long after the
# swap so a request that resolved it just before the swap can finish its
# reads on the fds it already holds. Closing an fd early would not merely
# fail a late ``pread``: the number can be reused by a newer open, and the
# read would then return bytes from the WRONG file.
DEFAULT_GENERATION_GRACE_S = 60.0
# Heartbeat cadence for readers that announce themselves (the gateway).
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0

# Shard filenames are always ``geom_shard_NNNN.bin`` (written by
# ``consolidate_geom_store``), which lets the SQLite index store the shard
# *number* as an integer instead of repeating a 20-byte filename 11.5 M times.
_SHARD_FILENAME_RE = re.compile(r"^geom_shard_(\d+)\.bin$")


def shard_filename(shard_num: int) -> str:
    """Shard number → filename. Inverse of :func:`shard_num_from_filename`."""
    return f"geom_shard_{shard_num:04d}.bin"


def shard_num_from_filename(name: str) -> int:
    """Filename → shard number.

    Raises ``ValueError`` on anything that is not a ``geom_shard_NNNN.bin``
    name. This is deliberately loud: the SQLite index cannot represent an
    arbitrary filename, so silently coercing an unexpected one would produce
    an index that resolves to the wrong bytes.
    """
    m = _SHARD_FILENAME_RE.match(name)
    if not m:
        raise ValueError(
            f"geom-store index entry has non-shard filename {name!r}; the "
            f"SQLite index only represents 'geom_shard_NNNN.bin'"
        )
    return int(m.group(1))


# ── Low-level WKB serialisation helpers ───────────────────────────────────

def _geojson_to_wkb(geojson_geom: dict) -> bytes | None:
    """Convert a GeoJSON geometry dict to WKB bytes via Shapely."""
    if not _SHAPELY_AVAILABLE:
        return None
    try:
        geom = shapely_shape(geojson_geom)
        return shapely_wkb.dumps(geom)
    except Exception as e:
        print(f"geom_store._geojson_to_wkb error: {e}")
        return None


def _wkb_to_geojson(wkb_bytes: bytes) -> dict | None:
    """Convert WKB bytes back to a GeoJSON geometry dict via Shapely."""
    if not _SHAPELY_AVAILABLE:
        return None
    try:
        geom = shapely_wkb.loads(wkb_bytes)
        # Use __geo_interface__ — available on all Shapely geometry objects
        import json as _json
        return _json.loads(_json.dumps(geom.__geo_interface__))
    except Exception as e:
        print(f"geom_store._wkb_to_geojson error: {e}")
        return None


# ── Writer ─────────────────────────────────────────────────────────────────

class GeomStoreWriter:
    """
    Append-only per-authority staging writer.

    Usage::

        with GeomStoreWriter(staging_dir, namespace="osm") as writer:
            geom_store.configure_module_writer(writer)
            # ... ingest loop calls enrich_geometry(..., geom_key=...) ...
        geom_store.configure_module_writer(None)
        # staging_dir/osm.bin  +  staging_dir/osm.index.json  are now present

    After all authorities have been ingested, call ``consolidate_geom_store()``
    to pack the staging files into spatially-sharded shard files.
    """

    def __init__(self, staging_dir: str | Path, namespace: str):
        self._staging_dir = Path(staging_dir)
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        self._namespace = namespace
        self._bin_path = self._staging_dir / f"{namespace}.bin"
        self._idx_path = self._staging_dir / f"{namespace}.index.json"
        self._fh = open(self._bin_path, "ab")
        self._offset = os.path.getsize(self._bin_path) if self._bin_path.exists() else 0
        # Load existing index if resuming
        if self._idx_path.exists():
            with open(self._idx_path) as f:
                self._index: list[dict] = json.load(f)
        else:
            self._index = []

    def write(self, geom_key: str, h3_centroid: str, geojson_geom: dict) -> bool:
        """
        Serialise *geojson_geom* as WKB and append to the staging file.

        Returns ``True`` on success (caller should set ``has_geom = True``).
        """
        wkb = _geojson_to_wkb(geojson_geom)
        if wkb is None:
            return False
        try:
            self._fh.write(wkb)
            self._index.append({
                "key": geom_key,
                "h3": h3_centroid,
                "file": self._bin_path.name,
                "offset": self._offset,
                "length": len(wkb),
            })
            self._offset += len(wkb)
            return True
        except Exception as e:
            print(f"GeomStoreWriter.write({geom_key}): {e}")
            return False

    def flush_index(self):
        """Persist the in-memory index to disk (call periodically for safety)."""
        self._fh.flush()
        with open(self._idx_path, "w") as f:
            json.dump(self._index, f)

    def close(self):
        """Flush all buffers and write the final staging index."""
        self.flush_index()
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def count(self) -> int:
        return len(self._index)


# ── Module-level singleton writer (optional convenience) ───────────────────
# Authority scripts can call ``configure_module_writer(writer)`` once at
# startup, and ``enrich_geometry()`` will automatically write to it when
# ``geom_key`` is supplied.

_module_writer: GeomStoreWriter | None = None


def configure_module_writer(writer: GeomStoreWriter | None):
    """Set (or clear) the module-level writer used by enrich_geometry()."""
    global _module_writer
    _module_writer = writer


def get_module_writer() -> GeomStoreWriter | None:
    return _module_writer


# ── SQLite index ───────────────────────────────────────────────────────────

_SQLITE_SCHEMA = """
CREATE TABLE geom(
    k     TEXT PRIMARY KEY,
    shard INTEGER NOT NULL,
    off   INTEGER NOT NULL,
    len   INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
"""


def write_sqlite_index(
    entries,
    output_dir: str | Path,
    batch_size: int = 100_000,
    progress_every: int = 2_000_000,
) -> int:
    """
    Write ``index.sqlite`` for the geom store from an iterable of entries.

    Args:
        entries: iterable of ``(key, filename, offset, length)`` tuples.
                 Streamed, never materialised — the caller may pass a
                 generator over a multi-GB ``index.json``.
        output_dir: geom-store directory (receives ``index.sqlite``).

    Returns:
        Number of rows written.

    Built into ``index.sqlite.tmp`` and ``os.replace``-d into place, matching
    the discipline ``consolidate_geom_store`` already uses for ``index.json``.
    That atomic rename is also what makes the reader's ``immutable=1`` open
    safe: a reader holding the old file keeps reading the old *inode*
    consistently rather than seeing a half-written index.

    ``journal_mode=OFF`` / ``synchronous=OFF`` are safe here precisely because
    the target is a throwaway ``.tmp``: a crash mid-build leaves the live index
    untouched and the rebuild is idempotent. They also leave no ``-wal``/``-shm``
    companion, which ``immutable=1`` requires.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = output_dir / INDEX_SQLITE_NAME
    tmp_path = output_dir / (INDEX_SQLITE_NAME + ".tmp")

    # A stale .tmp (and any journal companions) would otherwise be appended to.
    for p in (tmp_path, Path(str(tmp_path) + "-wal"), Path(str(tmp_path) + "-shm"),
              Path(str(tmp_path) + "-journal")):
        p.unlink(missing_ok=True)

    conn = sqlite3.connect(str(tmp_path))
    written = 0
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        # index.json is ordered by H3 locality, not lexicographically, so keys
        # arrive in effectively random order — the page-split-heavy path for a
        # WITHOUT ROWID B-tree. A cache large enough to hold the whole index
        # (~0.39 GB at 11.5 M rows) keeps the build from thrashing. This is a
        # batch-time cost only; the reader opens with SQLite's default cache.
        conn.execute("PRAGMA cache_size=-1500000")  # ~1.5 GB, in KiB
        conn.executescript(_SQLITE_SCHEMA)

        def _rows():
            nonlocal written
            for key, filename, offset, length in entries:
                yield (key, shard_num_from_filename(filename), offset, length)
                written += 1
                if progress_every and written % progress_every == 0:
                    print(f"  ... sqlite index {written:,} rows", flush=True)

        # executemany over a generator streams in SQLite-sized batches; the
        # explicit batch_size only bounds the transaction, not memory.
        cur = conn.cursor()
        batch: list[tuple] = []
        for row in _rows():
            batch.append(row)
            if len(batch) >= batch_size:
                cur.executemany("INSERT OR REPLACE INTO geom VALUES(?,?,?,?)", batch)
                batch.clear()
        if batch:
            cur.executemany("INSERT OR REPLACE INTO geom VALUES(?,?,?,?)", batch)

        # Cache the row count so __len__ never pays a full B-tree scan of an
        # 11.5M-row WITHOUT ROWID table.
        conn.execute("INSERT OR REPLACE INTO meta VALUES('count', ?)", (str(written),))
        conn.commit()
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()

    os.replace(tmp_path, final_path)
    size_mb = final_path.stat().st_size / (1024 * 1024)
    print(
        f"write_sqlite_index: wrote {written:,} rows → {final_path} "
        f"({size_mb:,.0f} MB)"
    )
    return written


def _index_dict_to_entries(index: dict[str, dict]):
    """Adapt an in-memory ``index.json`` dict to ``write_sqlite_index`` input."""
    for key, e in index.items():
        yield key, e["file"], e["offset"], e["length"]


def backup_sqlite_index(
    output_dir: str | Path,
    backup_dir: str | Path | None = None,
    keep: int | None = None,
) -> Path | None:
    """Copy a freshly-consolidated ``index.sqlite`` to separate storage.

    Called once per consolidation, *after* every namespace has been packed and
    the index written — so the copy is only ever taken of a complete index, and
    a run that dies mid-consolidation cannot rotate a good backup out.

    Why this exists
    ---------------
    The shards are ~61 GB of **keyless** WKB: nothing in a shard says which
    place a blob belongs to, and the key → (shard, offset, length) map lives
    only in the index. Lose the index and the store is stranded — every byte of
    geometry intact and none of it addressable. That is not hypothetical; on
    2026-08-07 a test run rewrote the live index with two synthetic rows and the
    only route back was a multi-day re-extract of every namespace carrying
    non-point geometry, because ES and the staged parquet both store just a
    ``geom_ref`` and no coordinates.

    Backups are timestamped rather than a single rolling file, so a *bad*
    consolidation can't quietly overwrite the last good copy, and land on a
    different filesystem from the store (``IX1_BASE`` vs ``IX3_BASE``) so one
    errant write path can't take both. Restoring is a plain file copy back to
    ``<store>/index.sqlite`` — the shards are untouched by all of this.

    Failure is loud but non-fatal: a completed consolidation is worth more than
    its backup, and raising here would discard hours of packing.

    Returns the backup path, or ``None`` if it could not be written.
    """
    from datetime import datetime, timezone

    from processing.settings import (
        GEOM_STORE_BACKUP_DIR, GEOM_STORE_BACKUP_KEEP,
    )

    src = Path(output_dir) / INDEX_SQLITE_NAME
    dest_dir = Path(backup_dir if backup_dir is not None else GEOM_STORE_BACKUP_DIR)
    keep = GEOM_STORE_BACKUP_KEEP if keep is None else keep

    try:
        if not src.exists():
            print(f"ERROR: geom index backup skipped — {src} does not exist",
                  flush=True)
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = dest_dir / f"index-{stamp}.sqlite"
        # Two consolidations inside the same second would otherwise collide, and
        # the second would overwrite the first — the precise failure timestamping
        # exists to prevent. Suffix with `_N`, which also keeps the filenames
        # sorting chronologically ('.' < '_'), so pruning still drops the oldest.
        collision = 2
        while dest.exists():
            dest = dest_dir / f"index-{stamp}_{collision}.sqlite"
            collision += 1
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        # Copy then rename, so a reader (or a pruning pass) never sees a
        # half-written backup and mistake it for a good one.
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        size_gb = dest.stat().st_size / 1e9
        print(f"consolidate_geom_store: backed up {INDEX_SQLITE_NAME} "
              f"({size_gb:.2f} GB) → {dest}", flush=True)

        # Prune oldest first; names sort chronologically by construction.
        existing = sorted(dest_dir.glob("index-*.sqlite"))
        for stale in existing[:-keep] if keep > 0 else []:
            try:
                stale.unlink()
                print(f"  pruned old geom index backup: {stale.name}", flush=True)
            except OSError as exc:
                print(f"  WARNING: could not prune {stale.name}: {exc}", flush=True)
        return dest
    except Exception as exc:
        print(
            f"ERROR: geom index backup FAILED ({exc}).\n"
            f"  {src} is the ONLY copy of the key→shard map for this store; "
            f"without it the shards cannot be addressed and the geometry must "
            f"be re-extracted from source. Copy it to {dest_dir} by hand.",
            flush=True,
        )
        return None


# ── Consolidation ──────────────────────────────────────────────────────────

def _load_existing_index(output_dir: Path) -> dict[str, dict]:
    """The existing store's ``{key: {file, offset, length}}`` for a merge.

    Reads ``index.sqlite`` when it exists and falls back to ``index.json``
    only when it does not. The two can diverge: ``processing.compact_overlays``
    folds overlay rows into ``index.sqlite`` and deliberately leaves
    ``index.json`` untouched (the reader never opens it when SQLite exists).
    A merge seeded from the stale JSON would rebuild ``index.sqlite`` WITHOUT
    those rows — a silent loss of exactly the geometries most recently added.
    """
    sqlite_path = output_dir / INDEX_SQLITE_NAME
    json_path = output_dir / INDEX_JSON_NAME
    if sqlite_path.exists():
        existing: dict[str, dict] = {}
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro&immutable=1", uri=True)
        try:
            cur = conn.execute("SELECT k, shard, off, len FROM geom")
            while True:
                rows = cur.fetchmany(100_000)
                if not rows:
                    break
                for k, shard, off, ln in rows:
                    existing[k] = {"file": shard_filename(shard), "offset": off,
                                   "length": ln}
        finally:
            conn.close()
        return existing
    if json_path.exists():
        with open(json_path) as f:
            return json.load(f)
    return {}


def consolidate_geom_store(
    staging_dir: str | Path,
    output_dir: str | Path,
    shard_size_bytes: int = DEFAULT_SHARD_SIZE_BYTES,
    delete_staging: bool = True,
    merge_with_existing: bool = False,
    backup: bool = True,
) -> int:
    """
    Read all per-authority staging files, sort by H3 centroid (r3), and write
    spatially-sharded shard files plus ``index.json``.

    Args:
        staging_dir:       Directory containing ``*.bin`` + ``*.index.json``
                           staging files written by ``GeomStoreWriter``.
        output_dir:        Destination for shard files and ``index.json``.
        shard_size_bytes:  Target shard file size (default 256 MB).
        delete_staging:    If True, remove staging files after consolidation.
        merge_with_existing: If True and ``output_dir`` already holds shard
                           files + an index (from a prior consolidation),
                           keep them in place and append new entries into
                           freshly-numbered shards. Existing keys overwritten
                           by re-staged entries point to the new shard. Use
                           this after each round of additional staging (e.g.
                           boundary_stage shards) so the 10M-entry base index
                           isn't rebuilt from scratch each time. The existing
                           entries are read from ``index.sqlite`` when it
                           exists (see :func:`_load_existing_index`).
        backup:            Copy the finished ``index.sqlite`` to the backup
                           location (``backup_sqlite_index``). Pass ``False``
                           for a throwaway store — an overlay generation —
                           whose index must NOT rotate the main store's
                           backups out.

    Returns:
        Total number of geometry entries written.
    """
    staging_dir = Path(staging_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # A staging directory that does not exist is never a legitimate request —
    # it is a typo or an unexpanded shell variable. Reading nothing and
    # reporting success is the exact failure this campaign was created to stop
    # (see CLAUDE.md: "a tile job that reports success is not evidence it read
    # any geometry"). On 31 Aug 2026 a mis-quoted `--staging-dir "\$STAGING"`
    # merged zero of 9,849 whg geometries and exited 0.
    if not staging_dir.is_dir():
        raise FileNotFoundError(
            f"geom staging directory does not exist: {staging_dir} — refusing to "
            f"consolidate, because reading nothing would otherwise look like a "
            f"successful merge"
        )

    # ── 1. Collect all staging index entries ──────────────────────────────
    all_entries: list[dict] = []
    staging_bin_files: list[Path] = []
    for idx_file in sorted(staging_dir.glob("*.index.json")):
        with open(idx_file) as f:
            entries = json.load(f)
        all_entries.extend(entries)
        staging_bin_files.append(staging_dir / idx_file.stem.replace(".index", "") if False
                                 else staging_dir / (idx_file.name.replace(".index.json", ".bin")))

    if not all_entries:
        print("consolidate_geom_store: no entries found in staging directory.")
        return 0

    print(f"consolidate_geom_store: sorting {len(all_entries):,} entries by H3 r3 ...")

    # ── 2. Sort by H3 cell at r3 for geographic locality ─────────────────
    def _sort_key(e: dict) -> str:
        cell = e.get("h3", "")
        if _H3_AVAILABLE and cell:
            try:
                return _h3.cell_to_parent(cell, _SHARD_H3_RESOLUTION)
            except Exception:
                return cell
        return cell

    all_entries.sort(key=_sort_key)

    # ── 3. Build open-handle map for all staging bin files ────────────────
    # Map filename → open file handle for random-access reads.
    staging_handles: dict[str, object] = {}
    for bin_path in staging_dir.glob("*.bin"):
        staging_handles[bin_path.name] = open(bin_path, "rb")

    # ── 4. Write spatially-sharded output files ───────────────────────────
    # In merge mode, preserve existing shards and continue from the next
    # shard number — the existing index.json keys still resolve correctly
    # because their (file, offset, length) tuples are unchanged. Re-staged
    # keys overwrite the existing index entry to point at the new shard.
    final_index: dict[str, dict] = {}
    starting_shard_num = 1
    if merge_with_existing:
        final_index = _load_existing_index(output_dir)
        if final_index:
            existing_shards = sorted(output_dir.glob("geom_shard_*.bin"))
            if existing_shards:
                last_name = existing_shards[-1].name
                try:
                    last_num = int(
                        last_name.replace("geom_shard_", "").replace(".bin", "")
                    )
                    starting_shard_num = last_num + 1
                except ValueError:
                    pass
            print(
                f"consolidate_geom_store: merging — kept "
                f"{len(final_index):,} existing entries, "
                f"new shards start at {starting_shard_num:04d}"
            )

    shard_num = starting_shard_num
    shard_path = output_dir / f"geom_shard_{shard_num:04d}.bin"
    shard_fh = open(shard_path, "wb")
    shard_offset = 0
    written = 0

    for entry in all_entries:
        src_file = entry["file"]
        src_offset = entry["offset"]
        length = entry["length"]
        key = entry["key"]

        fh = staging_handles.get(src_file)
        if fh is None:
            print(f"  WARNING: staging file {src_file} not found; skipping {key}")
            continue

        fh.seek(src_offset)
        wkb_bytes = fh.read(length)
        if len(wkb_bytes) != length:
            print(f"  WARNING: short read for {key} ({len(wkb_bytes)} vs {length}); skipping")
            continue

        # Roll to next shard if needed
        if shard_offset > 0 and shard_offset + length > shard_size_bytes:
            shard_fh.close()
            shard_num += 1
            shard_path = output_dir / f"geom_shard_{shard_num:04d}.bin"
            shard_fh = open(shard_path, "wb")
            shard_offset = 0

        shard_fh.write(wkb_bytes)
        final_index[key] = {
            "file": shard_path.name,
            "offset": shard_offset,
            "length": length,
        }
        shard_offset += length
        written += 1

        if written % 500_000 == 0:
            print(f"  ... consolidated {written:,} geometries", flush=True)

    shard_fh.close()
    for fh in staging_handles.values():
        fh.close()

    # ── 5. Write final index (atomically) ──────────────────────────────────
    # Write to a temp file and rename so a concurrent reader (e.g. the live
    # gateway during an incremental --merge) never sees a half-written index.
    index_path = output_dir / INDEX_JSON_NAME
    tmp_index_path = output_dir / (INDEX_JSON_NAME + ".tmp")
    with open(tmp_index_path, "w") as f:
        json.dump(final_index, f)
    os.replace(tmp_index_path, index_path)
    print(f"consolidate_geom_store: wrote {written:,} geometries across "
          f"{shard_num} shards → {output_dir}")

    # ── 5b. Write the SQLite index alongside it ───────────────────────────
    # This is what GeomStoreReader actually opens (see its docstring): the
    # 1 GB index.json costs ~5.4 GB of RSS to load, which is why exact
    # containment silently never switched itself on in the gateway.
    # index.json is still written above, as the interchange/fallback format.
    try:
        write_sqlite_index(_index_dict_to_entries(final_index), output_dir)
        # Every namespace in this consolidation is now packed and indexed, so
        # this is the first (and only) moment the index is complete. Back it up
        # to separate storage before anything else can touch it.
        if backup:
            backup_sqlite_index(output_dir)
    except Exception as exc:
        # Do not fail the whole consolidation for this — index.json is
        # written and the reader falls back to it — but be loud, because a
        # missing SQLite index silently reinstates the 5.4 GB load.
        print(
            f"ERROR: consolidate_geom_store wrote {INDEX_JSON_NAME} but FAILED "
            f"to write {INDEX_SQLITE_NAME}: {exc}\n"
            f"  The reader will fall back to {INDEX_JSON_NAME} (~5.4 GB RSS). "
            f"Rebuild with: python -m processing.build_geom_index_sqlite build",
            flush=True,
        )

    # ── 6. Optionally delete staging files ────────────────────────────────
    if delete_staging:
        for bin_path in staging_dir.glob("*.bin"):
            bin_path.unlink(missing_ok=True)
        for idx_path in staging_dir.glob("*.index.json"):
            idx_path.unlink(missing_ok=True)
        print(f"  Deleted staging files from {staging_dir}")

    return written


# ── Generation: one immutable index snapshot ───────────────────────────────

class Generation:
    """One immutable snapshot of an ``index.sqlite`` and the shard directory
    it addresses — the unit a reader swaps when the store changes.

    The main store and every overlay are each one of these. A Generation owns
    its own SQLite connections (per thread, per pid) and its own shard fd map,
    and shares neither with any other Generation: after ``index.sqlite`` is
    replaced (``os.replace`` by a consolidation or a compaction) the OLD
    inode stays readable through the connection already open on it, so a
    reader keeps answering consistently from the snapshot it resolved until
    it has retired that snapshot — never from a half-swapped mixture.

    ``stamp`` is ``"<st_ino>:<st_mtime_ns>"`` of the index file at open time:
    the reader's generation id, reported in heartbeats and compared by the
    compactor before it deletes a folded overlay.
    """

    def __init__(self, shard_dir: str | Path, sqlite_path: str | Path | None = None,
                 *, label: str = ""):
        self.dir = Path(shard_dir)
        self.sqlite_path = Path(sqlite_path) if sqlite_path is not None \
            else self.dir / INDEX_SQLITE_NAME
        self.label = label or str(self.dir)
        st = os.stat(self.sqlite_path)
        self.ino = st.st_ino
        self.mtime_ns = st.st_mtime_ns
        self.stamp = f"{st.st_ino}:{st.st_mtime_ns}"
        self._local = threading.local()
        self._conns: list[tuple[int, sqlite3.Connection]] = []
        self._conns_lock = threading.Lock()
        self._fds: dict[int, int] = {}
        self._fd_lock = threading.Lock()
        self._closed = False
        # Touch the connection now so a broken/incompatible index fails at
        # construction, where the caller can log a degradation, rather than
        # on the first request.
        row = self._conn().execute("SELECT v FROM meta WHERE k='count'").fetchone()
        self.count: int | None = int(row[0]) if row else None

    # -- SQLite connection (per thread, per process) --------------------------

    def _conn(self) -> sqlite3.Connection:
        """This thread's read-only connection, reopened after a fork.

        ``immutable=1`` skips all locking and change detection. That is
        correct because an index is only ever replaced wholesale by an
        ``os.replace``: an open connection keeps reading the old *inode*
        consistently; the reader notices the new one on its next rescan.
        """
        if self._closed:
            raise RuntimeError(f"geom-store generation {self.label} is closed")
        pid = os.getpid()
        conn = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "pid", None) == pid:
            return conn
        uri = f"file:{self.sqlite_path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._local.conn = conn
        self._local.pid = pid
        with self._conns_lock:
            self._conns.append((pid, conn))
        return conn

    # -- shard reads ----------------------------------------------------------

    def _fd(self, shard: int) -> int:
        fd = self._fds.get(shard)
        if fd is not None:
            return fd
        with self._fd_lock:
            if self._closed:
                raise RuntimeError(f"geom-store generation {self.label} is closed")
            fd = self._fds.get(shard)
            if fd is None:
                fd = os.open(self.dir / shard_filename(shard), os.O_RDONLY)
                self._fds[shard] = fd
        return fd

    def locate(self, geom_key: str) -> tuple[int, int, int] | None:
        """``(shard_number, offset, length)`` for *geom_key*, or None."""
        row = self._conn().execute(
            "SELECT shard, off, len FROM geom WHERE k=?", (geom_key,)
        ).fetchone()
        return None if row is None else (row[0], row[1], row[2])

    def read_wkb(self, geom_key: str) -> bytes | None:
        """The stored bytes for *geom_key*, or None (absent OR unreadable —
        the caller logs/falls back; a short read is reported, never returned)."""
        located = self.locate(geom_key)
        if located is None:
            return None
        shard, offset, length = located
        try:
            # os.pread takes the offset as an argument and so cannot race —
            # unlike seek()+read() on a shared handle.
            data = os.pread(self._fd(shard), length, offset)
        except Exception as e:  # noqa: BLE001
            print(f"GeomStoreReader.read_wkb({geom_key}) [{self.label}]: {e}")
            return None
        if len(data) != length:
            print(
                f"GeomStoreReader.read_wkb({geom_key}) [{self.label}]: short read "
                f"({len(data)} of {length} bytes from {shard_filename(shard)}@{offset})"
            )
            return None
        return data

    def contains(self, geom_key: str) -> bool:
        return self._conn().execute(
            "SELECT 1 FROM geom WHERE k=? LIMIT 1", (geom_key,)
        ).fetchone() is not None

    def n_rows(self) -> int:
        if self.count is None:
            # Only for an index built without the meta row: a full B-tree scan
            # of a WITHOUT ROWID table, hence cached.
            self.count = self._conn().execute("SELECT count(*) FROM geom").fetchone()[0]
        return self.count

    def iter_rows(self, batch: int = 10_000):
        """Yield ``(key, shard, offset, length)`` in shard order — a streaming
        cursor, so a 50k-row overlay or an 11.5M-row main index costs one
        batch of memory at a time."""
        cur = self._conn().execute("SELECT k, shard, off, len FROM geom ORDER BY shard, off")
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                return
            yield from rows

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self):
        """Release fds and this process's connections. Idempotent."""
        self._closed = True
        with self._fd_lock:
            for fd in self._fds.values():
                try:
                    os.close(fd)
                except Exception:  # noqa: BLE001
                    pass
            self._fds.clear()
        pid = os.getpid()
        with self._conns_lock:
            keep = []
            for cpid, conn in self._conns:
                # Never close a connection inherited across fork(): it is the
                # parent's, and sqlite's finaliser must run in the parent.
                if cpid != pid:
                    keep.append((cpid, conn))
                    continue
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            self._conns = keep
        if getattr(self._local, "pid", None) == pid:
            self._local.conn = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


def main_generation_stamp(store_dir: str | Path) -> str | None:
    """``"<ino>:<mtime_ns>"`` of ``<store>/index.sqlite`` right now, or None
    when there is no SQLite index. The same id a reader reports as
    ``main_generation``; the compactor compares the two."""
    try:
        st = os.stat(Path(store_dir) / INDEX_SQLITE_NAME)
    except OSError:
        return None
    return f"{st.st_ino}:{st.st_mtime_ns}"


# ── Reader ─────────────────────────────────────────────────────────────────

class GeomStoreReader:
    """
    O(1) geometry lookup from the consolidated VAST geometry store.

    Backend selection
    ~~~~~~~~~~~~~~~~~
    Prefers ``index.sqlite``; falls back to ``index.json`` when it is absent,
    so a store consolidated by an older build still works unchanged. The
    difference is not cosmetic — measured on the live 11.5 M-entry store:

    =================  ==================  ==================
    ..                 ``index.json``      ``index.sqlite``
    =================  ==================  ==================
    process RSS        ~5.4 GB             ~0 (paged on demand)
    cold start         parse 1.02 GB JSON  open a file
    on disk            1.02 GB             ~0.39 GB
    =================  ==================  ==================

    The 5.4 GB load is why ``containment=exact`` never actually switched
    itself on in the gateway (place#165): nothing crashed and nothing logged,
    it just quietly degraded to the ``repr_point`` path. ``backend`` reports
    which one is live, and construction emits a log line either way so this
    can never again be invisible.

    Generations, overlays and reload
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    The SQLite backend is a :class:`Generation`, and the reader follows the
    store WITHOUT a restart:

    * **Overlays** — ``<store>/overlays/whg-<dataset_id>/CURRENT`` → one mini
      store per contributed dataset, written by a VM job
      (``processing.geom_overlay.write_overlay``). A key of the form
      ``whg:<digits>:…`` is looked up in its dataset's overlay FIRST and in
      the main store only if the overlay does not hold it; every other key
      pays a single ``startswith`` and goes straight to the main store.
    * **Main reload** — the rescan tick stats ``index.sqlite``; a new inode
      or mtime (a consolidation or an overlay compaction ``os.replace``-d it)
      opens a fresh Generation, with a fresh fd map so newly-added shards are
      seen. The JSON backend is never reloaded.
    * **Rescan** is throttled (``GEOM_OVERLAY_RESCAN_S``, default 2 s) and
      runs from the request path — ``listdir`` + ``readlink`` per overlay and
      one ``stat`` for the main index, never a stat per key. A superseded
      Generation is swapped out by one assignment and closed only after a
      grace period, so in-flight reads finish on the fds they hold. Any
      change clears the WKB LRU (a cached ``None`` must not pin a key that
      has just arrived) and increments ``overlay_epoch``, which
      ``gateway.spatial`` keys its region cache on.
    * A broken overlay (no ``CURRENT``, a ``.tmp`` directory, an unreadable
      index) is skipped and logged once per overlay+reason, never raised,
      and retried on the next rescan — it is never remembered as failed.
    * ``heartbeat=True`` (the gateway) writes
      ``<store>/.reader_heartbeat/<pid>.json`` at most every 30 s from the
      same tick, naming the generations this process is serving, so the
      compactor can tell when every live reader has moved past a folded
      overlay and it is safe to delete.

    Concurrency
    ~~~~~~~~~~~
    Safe to share across threads and to inherit across ``fork()``:

    * shard reads use ``os.pread``, which takes the offset as an argument and
      so cannot race — the previous ``seek()``-then-``read()`` on a *shared*
      file object would have returned another thread's bytes under concurrency
      (latent only because the gateway's ``search`` is ``async def`` and runs
      on the event loop; wrapping the Shapely refine in ``asyncio.to_thread``
      would have made it live);
    * SQLite connections are per-thread *and* per-process, since a connection
      inherited across ``fork()`` is not usable in the child.

    Usage::

        reader = GeomStoreReader("/vast/ishi/geom")
        geojson = reader.get("osm:r12345_0")
    """

    def __init__(
        self,
        store_dir: str | Path,
        lru_maxsize: int = 4096,
        prefer_sqlite: bool = True,
        *,
        index_path: str | Path | None = None,
        overlays: bool = True,
        rescan_s: float | None = None,
        clock=None,
        grace_s: float | None = None,
        heartbeat: bool = False,
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    ):
        """
        Args:
            store_dir:     the geom store (shards, ``index.sqlite``, ``overlays/``).
            lru_maxsize:   in-process WKB LRU, by entry count.
            prefer_sqlite: use ``index.sqlite`` when present (else ``index.json``).
            index_path:    open THIS SQLite index instead of ``<store>/index.sqlite``
                           — the compactor verifies its candidate index this way.
                           Implies the shards are still read from ``store_dir``.
            overlays:      consult ``<store>/overlays``. ``False`` makes every
                           key resolve in the main store only — what a
                           verification of main-store bytes needs.
            rescan_s:      overlay/main rescan throttle (default ``$GEOM_OVERLAY_RESCAN_S``
                           or 2 s). ``0`` rescans on every call.
            clock:         monotonic time source (injectable for tests).
            grace_s:       how long a superseded generation stays open.
            heartbeat:     announce this process's generations under
                           ``<store>/.reader_heartbeat/`` (the gateway only).
        """
        self._dir = Path(store_dir)
        sqlite_path = Path(index_path) if index_path is not None else self._dir / INDEX_SQLITE_NAME
        json_path = self._dir / INDEX_JSON_NAME

        self._sqlite_path: Path | None = None
        self._main: Generation | None = None
        self._index: dict[str, dict] | None = None
        self._fds: dict[str, int] = {}          # JSON backend only
        self._fd_lock = threading.Lock()
        self._count: int | None = None

        self._clock = clock or time.monotonic
        if rescan_s is None:
            try:
                rescan_s = float(os.environ.get(RESCAN_ENV, DEFAULT_RESCAN_S))
            except ValueError:
                rescan_s = DEFAULT_RESCAN_S
        self._rescan_s = max(0.0, float(rescan_s))
        self._grace_s = DEFAULT_GENERATION_GRACE_S if grace_s is None else float(grace_s)
        self._next_rescan = 0.0
        self._tick_lock = threading.Lock()
        self._retired: list[tuple[Generation, float]] = []
        self._logged: set[str] = set()
        self.overlay_epoch = 0
        self._overlays = None
        self._heartbeat = bool(heartbeat)
        self._heartbeat_interval = float(heartbeat_interval_s)
        self._next_heartbeat = 0.0

        if prefer_sqlite and sqlite_path.exists():
            self._sqlite_path = sqlite_path
            self.backend = "sqlite"
            self._main = Generation(self._dir, sqlite_path, label="main")
            self._count = self._main.count
            print(
                f"geom-store: opened {sqlite_path} "
                f"({self._count if self._count is not None else '?'} entries, "
                f"sqlite backend, generation {self._main.stamp})",
                flush=True,
            )
        elif json_path.exists():
            self.backend = "json"
            with open(json_path) as f:
                self._index = json.load(f)
            self._count = len(self._index)
            print(
                f"geom-store: opened {json_path} ({self._count:,} entries, "
                f"JSON backend — expect ~5.4 GB RSS at full scale; build "
                f"{INDEX_SQLITE_NAME} with "
                f"'python -m processing.build_geom_index_sqlite build')",
                flush=True,
            )
        else:
            raise FileNotFoundError(
                f"Geometry store index not found: neither {sqlite_path} nor "
                f"{json_path} exists"
            )

        # Wrap the internal read method with an LRU cache on the key
        self._cached_wkb = lru_cache(maxsize=lru_maxsize)(self._read_wkb)

        if overlays:
            # Lazy import: geom_overlay imports this module, and the gateway
            # only pays for it once it actually opens a reader.
            from processing.geom_overlay import OverlayRegistry
            self._overlays = OverlayRegistry(
                self._dir, clock=self._clock, grace_s=self._grace_s,
                log_once=self._log_once,
            )
        # First tick now: overlays present at startup are served from the
        # first request, and a heartbeat-announcing reader is visible at once.
        self.tick(force=True)

    # ── Logging (once per condition, re-armed when it clears) ─────────────

    def _log_once(self, key: str, message: str | None) -> None:
        """Print *message* the first time *key* is seen; ``message=None``
        clears *key* so the same condition is reported again if it recurs."""
        if message is None:
            self._logged.discard(key)
            return
        if key in self._logged:
            return
        self._logged.add(key)
        print(f"geom-store: {message}", flush=True)

    # ── Rescan tick ───────────────────────────────────────────────────────

    def tick(self, force: bool = False) -> bool:
        """Rescan the main index and the overlays if the throttle allows.

        Returns True when a generation changed. Never raises: the store
        changing under a live reader must degrade a request, not fail it.
        Concurrent callers do not queue — a tick already in progress makes
        the others return immediately.
        """
        now = self._clock()
        if not force and now < self._next_rescan:
            return False
        if not self._tick_lock.acquire(blocking=False):
            return False
        try:
            self._next_rescan = now + self._rescan_s
            changed = False
            try:
                if self._main is not None:
                    changed |= self._reload_main_if_changed(now)
                if self._overlays is not None:
                    changed |= self._overlays.rescan(now)
            except Exception as exc:  # noqa: BLE001 — never fail a request
                self._log_once("tick-error", f"rescan failed: {exc!r}")
            if changed:
                self._cached_wkb.cache_clear()
                self.overlay_epoch += 1
            self._close_retired(now)
            if self._overlays is not None:
                self._overlays.close_retired(now)
            if self._heartbeat and now >= self._next_heartbeat:
                self._next_heartbeat = now + self._heartbeat_interval
                self._write_heartbeat()
            return changed
        finally:
            self._tick_lock.release()

    def _reload_main_if_changed(self, now: float) -> bool:
        assert self._main is not None and self._sqlite_path is not None
        try:
            st = os.stat(self._sqlite_path)
        except OSError as exc:
            self._log_once("main-stat", f"cannot stat {self._sqlite_path}: {exc}")
            return False
        if (st.st_ino, st.st_mtime_ns) == (self._main.ino, self._main.mtime_ns):
            self._log_once("main-stat", None)
            self._log_once("main-open", None)
            return False
        try:
            new = Generation(self._dir, self._sqlite_path, label="main")
        except Exception as exc:  # noqa: BLE001
            self._log_once(
                "main-open",
                f"{self._sqlite_path} changed ({self._main.stamp} → "
                f"{st.st_ino}:{st.st_mtime_ns}) but could not be opened, keeping the "
                f"previous generation: {exc!r}",
            )
            return False
        old = self._main
        self._main = new                     # single assignment = the swap
        self._count = new.count
        self._retired.append((old, now + self._grace_s))
        print(
            f"geom-store: main index reloaded {old.stamp} → {new.stamp} "
            f"({new.count if new.count is not None else '?'} entries)",
            flush=True,
        )
        return True

    def _close_retired(self, now: float) -> None:
        keep = []
        for gen, close_at in self._retired:
            if now >= close_at:
                gen.close()
            else:
                keep.append((gen, close_at))
        self._retired = keep

    # ── Heartbeat (best effort) ───────────────────────────────────────────

    def _write_heartbeat(self) -> None:
        try:
            from processing.geom_overlay import write_heartbeat
            write_heartbeat(self._dir, main_generation=self.main_generation,
                            overlays=self.overlay_generations)
        except Exception as exc:  # noqa: BLE001 — never fail a request for this
            self._log_once("heartbeat", f"heartbeat not written: {exc!r}")

    # ── Generation reporting ──────────────────────────────────────────────

    @property
    def main_generation(self) -> str:
        """``"<ino>:<mtime_ns>"`` of the main index this reader serves
        (``"json"`` on the JSON backend, which is never reloaded)."""
        return self._main.stamp if self._main is not None else "json"

    @property
    def overlay_generations(self) -> dict[str, str]:
        """``{dataset_id: generation_name}`` of the overlays currently served."""
        return self._overlays.generations() if self._overlays is not None else {}

    @property
    def overlays(self):
        return self._overlays

    # ── SQLite connection (compat) ────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """The main generation's connection for this thread (SQLite backend)."""
        if self._main is None:
            raise RuntimeError("no sqlite backend open")
        return self._main._conn()

    # ── Shard reads ───────────────────────────────────────────────────────

    def _fd(self, filename: str) -> int:
        """JSON backend: a shared read-only fd for *filename*, opened once."""
        fd = self._fds.get(filename)
        if fd is not None:
            return fd
        with self._fd_lock:
            fd = self._fds.get(filename)
            if fd is None:
                fd = os.open(self._dir / filename, os.O_RDONLY)
                self._fds[filename] = fd
        return fd

    def _locate(self, geom_key: str) -> tuple[str, int, int] | None:
        """Resolve *geom_key* in the MAIN store → ``(shard_filename, offset, length)``."""
        if self._main is not None:
            located = self._main.locate(geom_key)
            if located is None:
                return None
            return shard_filename(located[0]), located[1], located[2]
        entry = self._index.get(geom_key) if self._index else None
        if entry is None:
            return None
        return entry["file"], entry["offset"], entry["length"]

    def _overlay_for(self, geom_key: str):
        """The overlay Generation a store key routes to, or None.

        Keyed on the store key's own prefix — ``whg:<digits>:`` — never on a
        place id the caller derived. Non-whg keys pay the one ``startswith``.
        """
        if self._overlays is None or not geom_key.startswith(OVERLAY_KEY_PREFIX):
            return None
        m = _OVERLAY_KEY_RE.match(geom_key)
        if m is None:
            return None
        return self._overlays.get(m.group(1))

    def _read_main_wkb(self, geom_key: str) -> bytes | None:
        if self._main is not None:
            return self._main.read_wkb(geom_key)
        located = self._locate(geom_key)
        if located is None:
            return None
        filename, offset, length = located
        try:
            data = os.pread(self._fd(filename), length, offset)
            if len(data) != length:
                print(
                    f"GeomStoreReader._read_wkb({geom_key}): short read "
                    f"({len(data)} of {length} bytes from {filename}@{offset})"
                )
                return None
            return data
        except Exception as e:  # noqa: BLE001
            print(f"GeomStoreReader._read_wkb({geom_key}): {e}")
            return None

    def _read_wkb(self, geom_key: str) -> bytes | None:
        overlay = self._overlay_for(geom_key)
        if overlay is not None:
            data = overlay.read_wkb(geom_key)
            if data is not None:
                return data
            # Not in the overlay (or its files vanished under us, which a
            # just-deleted folded overlay can do for one rescan interval):
            # the main store is the fallback either way.
        return self._read_main_wkb(geom_key)

    def get(self, geom_key: str) -> dict | None:
        """Return the GeoJSON geometry dict for *geom_key*, or ``None``."""
        self.tick()
        wkb = self._cached_wkb(geom_key)
        if wkb is None:
            return None
        return _wkb_to_geojson(wkb)

    def get_wkb(self, geom_key: str, cached: bool = True) -> bytes | None:
        """Return the raw WKB for *geom_key*, or ``None``.

        For callers that go on to build a Shapely geometry anyway (the
        gateway's ``/api/geometry``): ``get()`` would round-trip the bytes
        through a GeoJSON dict and a JSON dump/load first, which on a
        100k-vertex boundary is the slowest part of the request.

        ``cached=False`` bypasses the LRU. That cache is bounded by ENTRY
        count (4096), not bytes, so a consumer that reads whole country
        boundaries would fill it with exactly the largest blobs and hold tens
        of MB per entry for the life of the process; the containment path's
        many small candidate reads are what it is sized for.
        """
        self.tick()
        if not cached:
            return self._read_wkb(geom_key)
        return self._cached_wkb(geom_key)

    def __contains__(self, geom_key: str) -> bool:
        self.tick()
        overlay = self._overlay_for(geom_key)
        if overlay is not None:
            try:
                if overlay.contains(geom_key):
                    return True
            except Exception:  # noqa: BLE001 — fall through to main
                pass
        if self._main is not None:
            return self._main.contains(geom_key)
        return geom_key in (self._index or {})

    def __len__(self) -> int:
        """Entries in the MAIN store (overlays are not counted)."""
        if self._main is not None:
            self._count = self._main.n_rows()
        return self._count if self._count is not None else 0

    def close(self):
        with self._fd_lock:
            for fd in self._fds.values():
                try:
                    os.close(fd)
                except Exception:  # noqa: BLE001
                    pass
            self._fds.clear()
        if self._main is not None:
            self._main.close()
        for gen, _ in self._retired:
            gen.close()
        self._retired = []
        if self._overlays is not None:
            self._overlays.close()
        if self._heartbeat:
            try:
                from processing.geom_overlay import remove_heartbeat
                remove_heartbeat(self._dir)
            except Exception:  # noqa: BLE001
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


# ── CLI consolidation entry-point ──────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path

    from processing.settings import GEOM_STORE_STAGING_DIR, GEOM_STORE_DIR

    parser = argparse.ArgumentParser(
        description="Consolidate per-authority geometry staging files into "
                    "spatially-sharded VAST store.",
    )
    parser.add_argument("--staging-dir", default=str(GEOM_STORE_STAGING_DIR),
                        help="Directory with staging *.bin + *.index.json files")
    parser.add_argument("--output-dir", default=str(GEOM_STORE_DIR),
                        help="Output directory for shards + index.json")
    parser.add_argument("--shard-size-mb", type=int, default=256,
                        help="Target shard file size in MB (default 256)")
    parser.add_argument("--keep-staging", action="store_true",
                        help="Do not delete staging files after consolidation")
    # Explicit merge mode — required for any output directory that already
    # holds a non-empty index. Without this, the rebuild-from-scratch default
    # silently orphans every existing entry and writes a fresh shard 0001
    # containing only the staged additions. The author of this CLI's prior
    # silent default lost a 10.7M-entry index.json on 2026-05-04 — that
    # accident is the reason this flag is now required.
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--merge", dest="merge", action="store_true",
                      help="Merge new staging entries into the existing index. "
                           "Existing shards are preserved; new shards are "
                           "appended; re-staged keys overwrite their index "
                           "entry to point at the new shard. The normal "
                           "incremental path.")
    mode.add_argument("--rebuild-from-scratch", dest="merge",
                      action="store_false",
                      help="Treat the output as empty; rewrite shards and "
                           "index.json from staging only. ALL existing entries "
                           "in the output index are abandoned (orphaning their "
                           "shard data). Refuses to run if the existing "
                           "index is non-empty unless --confirm-wipe is given.")
    parser.add_argument("--confirm-wipe", action="store_true",
                        help="Required acknowledgement when "
                             "--rebuild-from-scratch is chosen against a "
                             "non-empty existing index. Prints the size of "
                             "the index that will be discarded.")
    parser.add_argument("--allow-empty", action="store_true",
                        help="Exit 0 when the staging directory holds no "
                             "entries. Without this a no-op consolidation is "
                             "an error, because the overwhelmingly likely "
                             "cause is a mis-pointed --staging-dir rather than "
                             "a deliberate request to merge nothing.")
    args = parser.parse_args()

    if not args.merge:
        existing_index_path = _Path(args.output_dir) / "index.json"
        existing_count = 0
        if existing_index_path.exists():
            try:
                with existing_index_path.open() as _f:
                    existing_count = len(_json.load(_f))
            except Exception:
                existing_count = -1  # unreadable but present
        if existing_count != 0 and not args.confirm_wipe:
            print(
                f"REFUSING TO RUN: --rebuild-from-scratch would discard the "
                f"existing index at {existing_index_path} "
                f"({existing_count if existing_count >= 0 else 'unknown'} "
                f"entries). Re-invoke with --confirm-wipe to proceed, or use "
                f"--merge to add incrementally.",
                file=_sys.stderr,
            )
            _sys.exit(2)
        if existing_count != 0:
            print(
                f"WARNING: --rebuild-from-scratch --confirm-wipe — discarding "
                f"existing index of {existing_count} entries. Existing shard "
                f"files will become orphans on disk.",
                file=_sys.stderr,
            )

    written = consolidate_geom_store(
        staging_dir=args.staging_dir,
        output_dir=args.output_dir,
        shard_size_bytes=args.shard_size_mb * 1024 * 1024,
        delete_staging=not args.keep_staging,
        merge_with_existing=args.merge,
    )
    if not written and not args.allow_empty:
        print(
            f"REFUSING TO REPORT SUCCESS: consolidated 0 entries from "
            f"{args.staging_dir}. Nothing was added to the store. Check that "
            f"--staging-dir points where the authority actually staged (an "
            f"unexpanded shell variable is the usual cause); pass --allow-empty "
            f"if merging nothing really was the intent.",
            file=_sys.stderr,
        )
        _sys.exit(3)




