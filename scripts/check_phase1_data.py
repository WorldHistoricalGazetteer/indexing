#!/usr/bin/env python3
"""
Diagnostic script to check Phase 1 training data structure.

Run this on the cluster to verify the Parquet files have the expected columns.

Usage:
    python scripts/check_phase1_data.py /ix1/whcdh/models/phonetic/data/v4
"""

import sys
from pathlib import Path
import pyarrow.parquet as pq

def main():
    if len(sys.argv) < 2:
        print("Usage: python check_phase1_data.py <data_dir>")
        sys.exit(1)

    data_dir = Path(sys.argv[1])

    # Check Phase 1 triplets
    triplets_dir = data_dir / 'triplets' / 'phase1'
    print(f"\n{'='*60}")
    print("PHASE 1 TRIPLETS")
    print(f"{'='*60}")
    print(f"Directory: {triplets_dir}")

    if not triplets_dir.exists():
        print("ERROR: Phase 1 triplets directory does not exist!")
        sys.exit(1)

    for split in ['train', 'val']:
        path = triplets_dir / f'{split}.parquet'
        if not path.exists():
            print(f"\n{split}: FILE NOT FOUND")
            continue

        # Read schema only (fast)
        pf = pq.ParquetFile(path)
        schema = pf.schema_arrow
        num_rows = pf.metadata.num_rows

        print(f"\n{split.upper()}: {path}")
        print(f"  Rows: {num_rows:,}")
        print(f"  Columns: {len(schema)}")
        print(f"  Schema:")
        for field in schema:
            print(f"    - {field.name}: {field.type}")

        # Check if features are present
        has_features = 'anchor_features' in [f.name for f in schema]
        print(f"\n  Self-contained (has features): {has_features}")

        if has_features:
            # Read a small sample to verify data
            print(f"\n  Sample data (first row):")
            table = pq.read_table(path, columns=['anchor_id', 'anchor_features', 'anchor_feature_length'])
            df = table.slice(0, 1).to_pandas()
            for col in df.columns:
                val = df.iloc[0][col]
                if isinstance(val, (list, )):
                    print(f"    {col}: list of {len(val)} elements")
                else:
                    print(f"    {col}: {val}")

    # Check Phase 2 training data
    training_dir = data_dir / 'training'
    print(f"\n{'='*60}")
    print("PHASE 2 TRAINING DATA")
    print(f"{'='*60}")
    print(f"Directory: {training_dir}")

    if not training_dir.exists():
        print("WARNING: Phase 2 training directory does not exist")
    else:
        for split_dir in training_dir.iterdir():
            if split_dir.is_dir():
                data_file = split_dir / 'data.parquet'
                if data_file.exists():
                    try:
                        pf = pq.ParquetFile(data_file)
                        print(f"\n{split_dir.name}: {pf.metadata.num_rows:,} rows")
                    except Exception as e:
                        print(f"\n{split_dir.name}: ERROR - {e}")
                else:
                    print(f"\n{split_dir.name}: No data.parquet found")

    # Check Phase 3 triplets
    phase3_dir = data_dir / 'triplets' / 'phase3'
    print(f"\n{'='*60}")
    print("PHASE 3 TRIPLETS")
    print(f"{'='*60}")
    print(f"Directory: {phase3_dir}")

    if not phase3_dir.exists():
        print("WARNING: Phase 3 triplets directory does not exist")
        print("Run: es -generate-training-data VERSION to generate Phase 3 data")
    else:
        for split in ['train', 'val']:
            path = phase3_dir / f'{split}.parquet'
            if path.exists():
                pf = pq.ParquetFile(path)
                print(f"\n{split}: {pf.metadata.num_rows:,} rows")
            else:
                print(f"\n{split}: FILE NOT FOUND")

if __name__ == '__main__':
    main()

