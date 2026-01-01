#!/usr/bin/env python3
"""
Phase 3: Load embeddings from Parquet back to Elasticsearch.
Run on CPU node - no GPU required.
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from elasticsearch import Elasticsearch, helpers
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from processing.settings import ES_HOST, STAGING_REPO_NAME, TOPONYMS_INDEX

VECTOR_FIELD = "embedding_bilstm"
VERSION_FIELD = "model_version"
BULK_CHUNK_SIZE = 2000
SNAPSHOT_INTERVAL = 3 * 60 * 60  # 3 hours


def parse_args():
    p = argparse.ArgumentParser(description="Load embeddings to Elasticsearch")
    p.add_argument("--input-dir", type=str, default="data/embed_pipeline",
                   help="Directory with vector Parquet chunks")
    p.add_argument("--model-version", type=int, required=True,
                   help="Model version for snapshot naming")
    p.add_argument("--snapshot", action="store_true",
                   help="Create snapshots during loading")
    return p.parse_args()


def ensure_version_field(es, index):
    mapping = es.indices.get_mapping(index=index)
    props = mapping[index]["mappings"].get("properties", {})
    if VERSION_FIELD not in props:
        es.indices.put_mapping(
            index=index,
            body={"properties": {VERSION_FIELD: {"type": "integer"}}}
        )


def trigger_snapshot(es, version, wait=False):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"checkpoint_embeddings_v{version}_{timestamp}"
    try:
        es.snapshot.create(
            repository=STAGING_REPO_NAME,
            snapshot=name,
            body={
                "indices": TOPONYMS_INDEX,
                "ignore_unavailable": True,
                "include_global_state": False
            },
            wait_for_completion=wait
        )
        print(f"\nSnapshot triggered: {name}")
    except Exception as e:
        print(f"\nSnapshot warning: {e}")


def load_chunk(es, df, pbar):
    """Bulk update a chunk to ES."""
    actions = []
    for _, row in df.iterrows():
        emb = row["embedding_bilstm"]
        if emb is None:
            continue
        actions.append({
            "_op_type": "update",
            "_index": TOPONYMS_INDEX,
            "_id": row["_id"],
            "doc": {
                VECTOR_FIELD: emb,
                VERSION_FIELD: int(row["model_version"])
            }
        })

        if len(actions) >= BULK_CHUNK_SIZE:
            try:
                helpers.bulk(es, actions)
            except helpers.BulkIndexError as e:
                # Print errors to stderr for visibility
                import json
                sys.stderr.write(f"\nBulk error: {len(e.errors)} failures\n")
                for err in e.errors[:5]:
                    sys.stderr.write(f"  {json.dumps(err, indent=2)}\n")
                sys.stderr.flush()
                raise
            pbar.update(len(actions))
            actions = []

    if actions:
        try:
            helpers.bulk(es, actions)
        except helpers.BulkIndexError as e:
            import json
            sys.stderr.write(f"\nBulk error: {len(e.errors)} failures\n")
            for err in e.errors[:5]:
                sys.stderr.write(f"  {json.dumps(err, indent=2)}\n")
            sys.stderr.flush()
            raise
        pbar.update(len(actions))


def main():
    args = parse_args()
    input_dir = Path(args.input_dir)

    print(f"Connecting to {ES_HOST}")
    es = Elasticsearch(ES_HOST, request_timeout=300)
    ensure_version_field(es, TOPONYMS_INDEX)

    vector_files = sorted(input_dir.glob("vectors_chunk_*.parquet"))
    if not vector_files:
        print(f"No vectors_chunk_*.parquet files in {input_dir}")
        return

    # Count total documents
    total = sum(len(pd.read_parquet(f)) for f in vector_files)
    print(f"Loading {total:,} vectors from {len(vector_files)} chunk(s)")

    pbar = tqdm(total=total, unit="docs")
    last_snapshot = time.time()

    for vec_file in vector_files:
        df = pd.read_parquet(vec_file)
        load_chunk(es, df, pbar)

        if args.snapshot and (time.time() - last_snapshot > SNAPSHOT_INTERVAL):
            trigger_snapshot(es, args.model_version)
            last_snapshot = time.time()

    pbar.close()

    if args.snapshot:
        print("Creating final snapshot...")
        trigger_snapshot(es, args.model_version, wait=True)

    print("Load complete")


if __name__ == "__main__":
    main()