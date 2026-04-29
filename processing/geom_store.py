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

    # ── 5. Write final index ───────────────────────────────────────────────
    index_path = output_dir / "index.json"
    with open(index_path, "w") as f:
        json.dump(final_index, f)
    print(f"consolidate_geom_store: wrote {written:,} geometries across "
          f"{shard_num} shards → {output_dir}")

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

    The ``index.json`` is loaded into memory once on first use (it is
    typically 2–5 GB for 47 M records but compresses well).  An optional
    LRU cache keeps recently-accessed WKB blocks in memory.

    Usage::

        reader = GeomStoreReader("/vast/ishi/geom")
        geojson = reader.get("osm:r12345_0")
        if geojson:
            # use full geometry for precise containment check
            ...
    """

    def __init__(self, store_dir: str | Path, lru_maxsize: int = 4096):
        self._dir = Path(store_dir)
        index_path = self._dir / "index.json"
        if not index_path.exists():
            raise FileNotFoundError(f"Geometry store index not found: {index_path}")
        with open(index_path) as f:
            self._index: dict[str, dict] = json.load(f)
        self._handles: dict[str, "open"] = {}
        # Wrap the internal read method with an LRU cache on the key
        self._cached_wkb = lru_cache(maxsize=lru_maxsize)(self._read_wkb)

    def _open_handle(self, filename: str):
        if filename not in self._handles:
            self._handles[filename] = open(self._dir / filename, "rb")
        return self._handles[filename]

    def _read_wkb(self, geom_key: str) -> bytes | None:
        entry = self._index.get(geom_key)
        if entry is None:
            return None
        try:
            fh = self._open_handle(entry["file"])
            fh.seek(entry["offset"])
            return fh.read(entry["length"])
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
        return geom_key in self._index

    def __len__(self) -> int:
        return len(self._index)

    def close(self):
        for fh in self._handles.values():
            try:
                fh.close()
            except Exception:
                pass
        self._handles.clear()

    def __del__(self):
        self.close()


# ── CLI consolidation entry-point ──────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from processing.settings import GEOM_STORE_STAGING_DIR, GEOM_STORE_DIR

    parser = argparse.ArgumentParser(
        description="Consolidate per-authority geometry staging files into spatially-sharded VAST store."
    )
    parser.add_argument("--staging-dir", default=str(GEOM_STORE_STAGING_DIR),
                        help="Directory with staging *.bin + *.index.json files")
    parser.add_argument("--output-dir", default=str(GEOM_STORE_DIR),
                        help="Output directory for shards + index.json")
    parser.add_argument("--shard-size-mb", type=int, default=256,
                        help="Target shard file size in MB (default 256)")
    parser.add_argument("--keep-staging", action="store_true",
                        help="Do not delete staging files after consolidation")
    args = parser.parse_args()

    consolidate_geom_store(
        staging_dir=args.staging_dir,
        output_dir=args.output_dir,
        shard_size_bytes=args.shard_size_mb * 1024 * 1024,
        delete_staging=not args.keep_staging,
    )




