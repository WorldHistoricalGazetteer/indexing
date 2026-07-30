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
import sqlite3
import threading
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


# ── Consolidation ──────────────────────────────────────────────────────────

def consolidate_geom_store(
    staging_dir: str | Path,
    output_dir: str | Path,
    shard_size_bytes: int = DEFAULT_SHARD_SIZE_BYTES,
    delete_staging: bool = True,
    merge_with_existing: bool = False,
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
                           files + ``index.json`` (from a prior consolidation),
                           keep them in place and append new entries into
                           freshly-numbered shards. Existing keys overwritten
                           by re-staged entries point to the new shard. Use
                           this after each round of additional staging (e.g.
                           boundary_stage shards) so the 10M-entry base index
                           isn't rebuilt from scratch each time.

    Returns:
        Total number of geometry entries written.
    """
    staging_dir = Path(staging_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

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
        existing_index_path = output_dir / "index.json"
        if existing_index_path.exists():
            with open(existing_index_path) as f:
                final_index = json.load(f)
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
    ):
        self._dir = Path(store_dir)
        sqlite_path = self._dir / INDEX_SQLITE_NAME
        json_path = self._dir / INDEX_JSON_NAME

        self._sqlite_path: Path | None = None
        self._index: dict[str, dict] | None = None
        self._local = threading.local()
        self._fds: dict[str, int] = {}
        self._fd_lock = threading.Lock()
        self._count: int | None = None

        if prefer_sqlite and sqlite_path.exists():
            self._sqlite_path = sqlite_path
            self.backend = "sqlite"
            # Touch the connection now rather than on the first request, so a
            # broken/incompatible index fails at construction where
            # get_geom_reader() can catch it and log a degradation.
            row = self._conn().execute(
                "SELECT v FROM meta WHERE k='count'"
            ).fetchone()
            self._count = int(row[0]) if row else None
            print(
                f"geom-store: opened {sqlite_path} "
                f"({self._count if self._count is not None else '?'} entries, "
                f"sqlite backend)",
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

    # ── SQLite connection (per thread, per process) ───────────────────────

    def _conn(self) -> sqlite3.Connection:
        """Return this thread's read-only connection, reopening after a fork.

        ``immutable=1`` skips all locking and change detection. That is
        correct here because the index is only ever replaced wholesale by an
        ``os.replace`` (see :func:`write_sqlite_index`): an open reader keeps
        reading the old inode consistently, exactly as the in-memory
        ``index.json`` snapshot used to behave, until the process restarts.
        The re-ingest runbook already ends with ``es gateway-restart``.
        """
        pid = os.getpid()
        conn = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "pid", None) == pid:
            return conn
        uri = f"file:{self._sqlite_path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._local.conn = conn
        self._local.pid = pid
        return conn

    # ── Shard reads ───────────────────────────────────────────────────────

    def _fd(self, filename: str) -> int:
        """Return a shared read-only fd for *filename*, opening it once."""
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
        """Resolve *geom_key* → ``(shard_filename, offset, length)``."""
        if self._sqlite_path is not None:
            row = self._conn().execute(
                "SELECT shard, off, len FROM geom WHERE k=?", (geom_key,)
            ).fetchone()
            if row is None:
                return None
            return shard_filename(row[0]), row[1], row[2]
        entry = self._index.get(geom_key) if self._index else None
        if entry is None:
            return None
        return entry["file"], entry["offset"], entry["length"]

    def _read_wkb(self, geom_key: str) -> bytes | None:
        located = self._locate(geom_key)
        if located is None:
            return None
        filename, offset, length = located
        try:
            # os.pread is atomic w.r.t. the file offset — unlike seek()+read()
            # on a shared handle, it is safe from multiple threads.
            data = os.pread(self._fd(filename), length, offset)
            if len(data) != length:
                print(
                    f"GeomStoreReader._read_wkb({geom_key}): short read "
                    f"({len(data)} of {length} bytes from {filename}@{offset})"
                )
                return None
            return data
        except Exception as e:
            print(f"GeomStoreReader._read_wkb({geom_key}): {e}")
            return None

    def get(self, geom_key: str) -> dict | None:
        """Return the GeoJSON geometry dict for *geom_key*, or ``None``."""
        wkb = self._cached_wkb(geom_key)
        if wkb is None:
            return None
        return _wkb_to_geojson(wkb)

    def __contains__(self, geom_key: str) -> bool:
        if self._sqlite_path is not None:
            return self._conn().execute(
                "SELECT 1 FROM geom WHERE k=? LIMIT 1", (geom_key,)
            ).fetchone() is not None
        return geom_key in (self._index or {})

    def __len__(self) -> int:
        if self._count is None:
            # Only reached for a SQLite index built without the meta row;
            # a full B-tree scan of a WITHOUT ROWID table, hence cached.
            self._count = self._conn().execute(
                "SELECT count(*) FROM geom"
            ).fetchone()[0]
        return self._count

    def close(self):
        with self._fd_lock:
            for fd in self._fds.values():
                try:
                    os.close(fd)
                except Exception:
                    pass
            self._fds.clear()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def __del__(self):
        self.close()


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

    consolidate_geom_store(
        staging_dir=args.staging_dir,
        output_dir=args.output_dir,
        shard_size_bytes=args.shard_size_mb * 1024 * 1024,
        delete_staging=not args.keep_staging,
        merge_with_existing=args.merge,
    )




