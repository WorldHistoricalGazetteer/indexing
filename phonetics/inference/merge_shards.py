"""Concatenate sharded compute output files into a single embeddings parquet.

When ``update_es.py compute`` runs in sharded multi-GPU mode (one Slurm
array task per shard), each task writes its slice to
``<output>.shard_<id>.parquet``. This module concatenates those into the
canonical single ``<output>`` that ``update_es.py index`` expects.

The merge is a streaming row-group concat (no full materialisation), so
even very large shard sets (e.g. 67 M × 128 byte embeddings ~ 8.5 GB) fit
in modest memory.

Usage::

    python -m phonetics.inference.merge_shards \\
        --output-file /vast/.../embeddings_v7.parquet \\
        --num-shards 4
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq


logger = logging.getLogger("merge_shards")


def merge(
    *,
    output_file: Path,
    num_shards: int,
    delete_shards: bool = False,
) -> dict:
    if num_shards < 1:
        raise ValueError(f"num_shards must be ≥1, got {num_shards}")

    shard_paths = [
        output_file.with_suffix(f".shard_{i}{output_file.suffix}")
        for i in range(num_shards)
    ]

    missing = [p for p in shard_paths if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing {len(missing)} shard(s): "
            f"{[str(p) for p in missing]}"
        )

    if output_file.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing {output_file} — "
            f"delete it first if you want to remerge."
        )

    started = time.time()
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Take the schema from the first shard (all shards have the same one,
    # since they're produced by the same compute path).
    first = pq.ParquetFile(shard_paths[0])
    schema = first.schema_arrow
    total_rows = 0

    writer = pq.ParquetWriter(output_file, schema, compression="snappy")
    try:
        for path in shard_paths:
            reader = pq.ParquetFile(path)
            shard_rows = 0
            for i in range(reader.num_row_groups):
                table = reader.read_row_group(i)
                writer.write_table(table)
                shard_rows += table.num_rows
            total_rows += shard_rows
            logger.info(f"  merged {path.name}: {shard_rows:,} rows")
    finally:
        writer.close()

    elapsed = time.time() - started
    logger.info(
        f"Merge complete: {total_rows:,} rows → {output_file} "
        f"({output_file.stat().st_size / 1e9:.2f} GB) in {elapsed:.0f}s"
    )

    if delete_shards:
        for path in shard_paths:
            path.unlink()
            logger.info(f"  deleted shard {path.name}")

    return {
        "output_file": str(output_file),
        "num_shards": num_shards,
        "total_rows": total_rows,
        "size_bytes": output_file.stat().st_size,
        "elapsed_seconds": round(elapsed, 1),
        "shards_deleted": delete_shards,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Merge sharded embeddings parquet files",
    )
    parser.add_argument("--output-file", type=Path, required=True,
                        help="Canonical (un-suffixed) output path. Shards are "
                             "expected at <output>.shard_0.parquet, "
                             ".shard_1.parquet, etc.")
    parser.add_argument("--num-shards", type=int, required=True,
                        help="Number of shards to expect")
    parser.add_argument("--delete-shards", action="store_true",
                        help="Remove the shard files after a successful merge")
    args = parser.parse_args()

    try:
        summary = merge(
            output_file=args.output_file,
            num_shards=args.num_shards,
            delete_shards=args.delete_shards,
        )
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        logger.error(str(exc))
        sys.exit(1)
    print(summary)


if __name__ == "__main__":
    main()
