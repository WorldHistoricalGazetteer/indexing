#!/usr/bin/env python3
"""
Phase 1: Extract toponyms from Elasticsearch to Parquet files.
Run on CPU node - no GPU required.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
from elasticsearch import helpers, Elasticsearch
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from processing.settings import ES_HOST, TOPONYMS_INDEX

SOURCE_TEXT_FIELD = "name"
SOURCE_LANG_FIELD = "lang"
VERSION_FIELD = "model_version"
CHUNK_SIZE = 500_000


def parse_args():
    p = argparse.ArgumentParser(description="Extract toponyms to Parquet")
    p.add_argument("--output-dir", type=str, default="data/embed_pipeline",
                   help="Output directory for Parquet files")
    p.add_argument("--model-version", type=int, default=None,
                   help="Only extract docs not yet at this version")
    return p.parse_args()


def build_query(model_version):
    if model_version is None:
        return {"query": {"match_all": {}}}
    return {
        "query": {
            "bool": {
                "must_not": [{"term": {VERSION_FIELD: model_version}}]
            }
        }
    }


def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Connecting to {ES_HOST}")
    es = Elasticsearch(ES_HOST, request_timeout=300)

    query = build_query(args.model_version)

    # Count documents
    count_resp = es.count(index=TOPONYMS_INDEX, body=query)
    total_docs = count_resp["count"]
    print(f"Documents to extract: {total_docs:,}")

    if total_docs == 0:
        print("Nothing to extract.")
        return

    scan_gen = helpers.scan(
        es,
        index=TOPONYMS_INDEX,
        query={"match_all": {}} if args.model_version is None else {
            "bool": {"must_not": [{"term": {VERSION_FIELD: args.model_version}}]}
        },
        scroll="60m",
        size=10_000,
        _source=[SOURCE_TEXT_FIELD, SOURCE_LANG_FIELD]
    )

    buffer = []
    chunk_num = 0
    pbar = tqdm(total=total_docs, unit="docs")

    for hit in scan_gen:
        buffer.append({
            "_id": hit["_id"],
            "name": hit["_source"].get(SOURCE_TEXT_FIELD, ""),
            "lang": hit["_source"].get(SOURCE_LANG_FIELD, "unk")
        })
        pbar.update(1)

        if len(buffer) >= CHUNK_SIZE:
            df = pd.DataFrame(buffer)
            out_file = output_dir / f"raw_chunk_{chunk_num:04d}.parquet"
            df.to_parquet(out_file, index=False)
            print(f"\nWrote {out_file} ({len(df):,} rows)")
            buffer = []
            chunk_num += 1

    # Final chunk
    if buffer:
        df = pd.DataFrame(buffer)
        out_file = output_dir / f"raw_chunk_{chunk_num:04d}.parquet"
        df.to_parquet(out_file, index=False)
        print(f"\nWrote {out_file} ({len(df):,} rows)")

    pbar.close()
    print(f"\nExtraction complete: {chunk_num + 1} chunk(s) in {output_dir}")


if __name__ == "__main__":
    main()