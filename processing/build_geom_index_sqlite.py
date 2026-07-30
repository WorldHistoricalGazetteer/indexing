# processing/build_geom_index_sqlite.py

"""
One-off backfill: build ``index.sqlite`` for an existing geom store, and
verify it byte-for-byte against ``index.json`` before it goes near the gateway.

Why (place#165)
~~~~~~~~~~~~~~~
``GeomStoreReader`` used to ``json.load()`` a 1.02 GB ``index.json``, costing
~5.4 GB of RSS. That is why ``containment=exact`` had never actually run in
production: nothing crashed and nothing logged — the gateway simply never
loaded the store and every exact request quietly degraded to the ``repr_point``
path. ``processing/geom_store.py`` now prefers a SQLite index; this script
builds one for the store that already exists, without re-running consolidation
(which would rewrite shards).

From here on ``consolidate_geom_store`` writes ``index.sqlite`` itself, so this
script is only needed for stores consolidated by an older build.

Memory
~~~~~~
``index.json`` is streamed with ``ijson`` and rows are inserted in batches, so
peak memory is one batch — not the 5.4 GB the whole index would cost. The same
is true of ``verify``, which reservoir-samples while streaming rather than
materialising the index.

Usage::

    python -m processing.build_geom_index_sqlite build
    python -m processing.build_geom_index_sqlite verify --sample 10000
    python -m processing.build_geom_index_sqlite build --store-dir /path/to/geom
"""

import argparse
import os
import random
import sys
from pathlib import Path

from processing.geom_store import (
    INDEX_JSON_NAME,
    INDEX_SQLITE_NAME,
    GeomStoreReader,
    write_sqlite_index,
)

try:
    from processing.settings import GEOM_STORE_DIR as _DEFAULT_STORE_DIR
except Exception:  # pragma: no cover - settings optional for standalone use
    _DEFAULT_STORE_DIR = None


def _stream_index_json(json_path: Path):
    """Yield ``(key, filename, offset, length)`` from a large ``index.json``.

    ``index.json`` is a single JSON object mapping key → {file, offset,
    length}, so ``kvitems`` at the root walks it one entry at a time.
    """
    import ijson

    with open(json_path, "rb") as f:
        for key, entry in ijson.kvitems(f, ""):
            yield key, entry["file"], int(entry["offset"]), int(entry["length"])


def cmd_build(store_dir: Path) -> int:
    json_path = store_dir / INDEX_JSON_NAME
    if not json_path.exists():
        print(f"ERROR: {json_path} not found — nothing to convert.", file=sys.stderr)
        return 2

    size_gb = json_path.stat().st_size / (1024 ** 3)
    print(f"Streaming {json_path} ({size_gb:.2f} GB) → {store_dir / INDEX_SQLITE_NAME}")
    written = write_sqlite_index(_stream_index_json(json_path), store_dir)
    if written == 0:
        print("ERROR: wrote 0 rows — refusing to call that a success.", file=sys.stderr)
        return 1
    print(f"OK: {written:,} rows. Now run 'verify' before restarting the gateway.")
    return 0


def _sample_entries(json_path: Path, sample: int, seed: int):
    """Reservoir-sample ``sample`` entries while streaming ``index.json``.

    Returns ``(sampled_entries, total_seen)``. Memory is O(sample).
    """
    rng = random.Random(seed)
    reservoir: list[tuple] = []
    total = 0
    for row in _stream_index_json(json_path):
        total += 1
        if len(reservoir) < sample:
            reservoir.append(row)
        else:
            j = rng.randrange(total)
            if j < sample:
                reservoir[j] = row
        if total % 2_000_000 == 0:
            print(f"  ... scanned {total:,} entries", flush=True)
    return reservoir, total


def cmd_verify(store_dir: Path, sample: int, seed: int) -> int:
    json_path = store_dir / INDEX_JSON_NAME
    sqlite_path = store_dir / INDEX_SQLITE_NAME
    if not sqlite_path.exists():
        print(f"ERROR: {sqlite_path} not found — run 'build' first.", file=sys.stderr)
        return 2
    if not json_path.exists():
        print(
            f"ERROR: {json_path} not found — cannot verify without the "
            f"reference index.",
            file=sys.stderr,
        )
        return 2

    print(f"Sampling {sample:,} entries from {json_path} ...")
    sampled, total = _sample_entries(json_path, sample, seed)
    print(f"  {total:,} entries scanned, {len(sampled):,} sampled")

    reader = GeomStoreReader(store_dir, prefer_sqlite=True)
    if reader.backend != "sqlite":
        print(
            f"ERROR: reader chose backend {reader.backend!r}, not 'sqlite'.",
            file=sys.stderr,
        )
        return 1

    # Row-count agreement is the cheap global check; the byte comparison below
    # is the one that matters.
    if len(reader) != total:
        print(
            f"ERROR: row-count mismatch — sqlite {len(reader):,} vs "
            f"index.json {total:,}.",
            file=sys.stderr,
        )
        return 1
    print(f"  row counts agree: {total:,}")

    fds: dict[str, int] = {}

    def _read_direct(filename: str, offset: int, length: int) -> bytes:
        fd = fds.get(filename)
        if fd is None:
            fd = os.open(store_dir / filename, os.O_RDONLY)
            fds[filename] = fd
        return os.pread(fd, length, offset)

    mismatches: list[str] = []
    missing: list[str] = []
    checked = 0
    try:
        for key, filename, offset, length in sampled:
            expected = _read_direct(filename, offset, length)
            # Compare raw WKB, not decoded GeoJSON: decoding would mask an
            # off-by-one in offset/length that happened to still parse.
            actual = reader._cached_wkb(key)
            if actual is None:
                missing.append(key)
                continue
            if actual != expected:
                mismatches.append(key)
            checked += 1
    finally:
        for fd in fds.values():
            try:
                os.close(fd)
            except Exception:
                pass
        reader.close()

    print(f"  compared {checked:,} keys byte-for-byte via both paths")
    if missing:
        print(
            f"FAIL: {len(missing):,} sampled keys resolve in index.json but "
            f"NOT in index.sqlite, e.g. {missing[:5]}",
            file=sys.stderr,
        )
    if mismatches:
        print(
            f"FAIL: {len(mismatches):,} sampled keys return DIFFERENT bytes, "
            f"e.g. {mismatches[:5]}",
            file=sys.stderr,
        )
    if missing or mismatches:
        return 1

    print(
        f"OK: index.sqlite is byte-identical to index.json across "
        f"{checked:,} sampled keys."
    )
    return 0


def cmd_stat(store_dir: Path) -> int:
    """Report what is present and which backend a reader would pick."""
    for name in (INDEX_JSON_NAME, INDEX_SQLITE_NAME):
        p = store_dir / name
        if p.exists():
            print(f"  {name}: {p.stat().st_size / (1024 ** 3):.3f} GB")
        else:
            print(f"  {name}: absent")
    reader = GeomStoreReader(store_dir)
    print(f"  reader backend: {reader.backend}, entries: {len(reader):,}")
    reader.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "command", choices=["build", "verify", "stat"],
        help="build: index.json → index.sqlite; verify: byte-compare a "
             "sample through both paths; stat: report sizes and backend",
    )
    parser.add_argument(
        "--store-dir", default=str(_DEFAULT_STORE_DIR or ""),
        help="Geom store directory (default: settings.GEOM_STORE_DIR)",
    )
    parser.add_argument(
        "--sample", type=int, default=10_000,
        help="verify: number of keys to byte-compare (default 10000)",
    )
    parser.add_argument(
        "--seed", type=int, default=20260730,
        help="verify: RNG seed for reproducible sampling",
    )
    args = parser.parse_args()

    if not args.store_dir:
        print("ERROR: --store-dir is required (settings.GEOM_STORE_DIR unset).",
              file=sys.stderr)
        return 2
    store_dir = Path(args.store_dir)
    if not store_dir.is_dir():
        print(f"ERROR: {store_dir} is not a directory.", file=sys.stderr)
        return 2

    if args.command == "build":
        return cmd_build(store_dir)
    if args.command == "verify":
        return cmd_verify(store_dir, args.sample, args.seed)
    return cmd_stat(store_dir)


if __name__ == "__main__":
    sys.exit(main())
