#!/usr/bin/env python3
"""
Extract sample records from Pleiades, TGN, and GB1900 datasets.
Run this on CRC after loading Python environment.
"""

import gzip
import json
import zipfile
from collections import Counter

DATA_DIR = "/ix1/whcdh/data/"


def analyze_pleiades():
    """Analyze Pleiades JSON structure."""
    print("=" * 80)
    print("PLEIADES ANALYSIS")
    print("=" * 80)

    file_path = f"{DATA_DIR}pleiades/pleiades-places-latest/pleiades-places-latest.json.gz"

    try:
        with gzip.open(file_path, 'rt', encoding='utf-8') as f:
            data = json.load(f)

        print(f"\nFile: {file_path}")
        print(f"Type: {type(data)}")
        print(f"Total records: {len(data)}")

        if isinstance(data, list) and len(data) > 0:
            print("\n" + "-" * 80)
            print("FIRST RECORD:")
            print("-" * 80)
            print(json.dumps(data[0], indent=2))

            print("\n" + "-" * 80)
            print("SECOND RECORD:")
            print("-" * 80)
            print(json.dumps(data[1], indent=2))

            print("\n" + "-" * 80)
            print("KEYS IN FIRST RECORD:")
            print("-" * 80)
            for key in sorted(data[0].keys()):
                value = data[0][key]
                value_type = type(value).__name__
                if isinstance(value, list):
                    value_preview = f"list[{len(value)}]"
                elif isinstance(value, dict):
                    value_preview = f"dict with keys: {list(value.keys())[:3]}"
                elif isinstance(value, str):
                    value_preview = f"'{value[:50]}...'" if len(value) > 50 else f"'{value}'"
                else:
                    value_preview = str(value)
                print(f"  {key:20s} : {value_type:10s} = {value_preview}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


def analyze_tgn():
    """Analyze TGN N-Triples structure."""
    print("\n\n" + "=" * 80)
    print("TGN ANALYSIS (N-Triples)")
    print("=" * 80)

    file_path = f"{DATA_DIR}tgn/TGNOut_PlaceMap/TGNOut_PlaceMap.nt"

    try:
        print(f"\nFile: {file_path}")

        # Count lines
        with open(file_path, 'r', encoding='utf-8') as f:
            line_count = sum(1 for _ in f)
        print(f"Total triples: {line_count:,}")

        # Analyze predicates
        predicates = Counter()
        subjects = set()

        print("\n" + "-" * 80)
        print("FIRST 30 TRIPLES:")
        print("-" * 80)

        with open(file_path, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                if i < 30:
                    print(line.rstrip())

                # Parse triple (simple approach)
                parts = line.strip().split(' ', 2)
                if len(parts) >= 3:
                    subject, predicate, obj = parts[0], parts[1], ' '.join(parts[2:])
                    subjects.add(subject)
                    predicates[predicate] += 1

                # Only scan first 10000 for predicate analysis
                if i >= 10000:
                    break

        print("\n" + "-" * 80)
        print("MOST COMMON PREDICATES (from first 10k triples):")
        print("-" * 80)
        for pred, count in predicates.most_common(30):
            print(f"  {count:6d}  {pred}")

        print("\n" + "-" * 80)
        print("SAMPLE SUBJECTS (TGN IDs):")
        print("-" * 80)
        for subject in list(subjects)[:10]:
            print(f"  {subject}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


def analyze_gb1900():
    """Analyze GB1900 structure."""
    print("\n\n" + "=" * 80)
    print("GB1900 ANALYSIS")
    print("=" * 80)

    file_path = f"{DATA_DIR}gb1900/GB1900_gazetteer_abridged_july_2018/GB1900_gazetteer_abridged_july_2018.zip"

    try:
        print(f"\nFile: {file_path}")

        with zipfile.ZipFile(file_path, 'r') as zf:
            print("\n" + "-" * 80)
            print("FILES IN ARCHIVE:")
            print("-" * 80)
            for name in zf.namelist():
                info = zf.getinfo(name)
                print(f"  {name:60s} {info.file_size:>12,d} bytes")

            # Find JSON/GeoJSON file
            json_files = [n for n in zf.namelist()
                          if n.endswith('.json') or n.endswith('.geojson') or n.endswith('.txt')]

            if not json_files:
                print("\nNo JSON/GeoJSON files found. Checking all files...")
                json_files = zf.namelist()

            for json_file in json_files:
                print("\n" + "-" * 80)
                print(f"ANALYZING: {json_file}")
                print("-" * 80)

                with zf.open(json_file, 'r') as f:
                    # Read first few bytes to detect format
                    first_bytes = f.read(1000)
                    f.seek(0)

                    try:
                        # Try as regular JSON
                        content = f.read().decode('utf-8')
                        data = json.loads(content)

                        print(f"✓ Valid JSON")
                        print(f"Type: {type(data)}")

                        if isinstance(data, list):
                            print(f"Array with {len(data)} items")
                            if len(data) > 0:
                                print("\n--- FIRST ITEM ---")
                                print(json.dumps(data[0], indent=2))
                                print("\n--- KEYS IN FIRST ITEM ---")
                                for key in sorted(data[0].keys()):
                                    print(f"  {key}")

                        elif isinstance(data, dict):
                            print(f"Object with keys: {sorted(data.keys())}")

                            # Check for GeoJSON FeatureCollection
                            if data.get('type') == 'FeatureCollection':
                                features = data.get('features', [])
                                print(f"\n✓ GeoJSON FeatureCollection")
                                print(f"Features: {len(features)}")

                                if features:
                                    print("\n--- FIRST FEATURE ---")
                                    print(json.dumps(features[0], indent=2))

                                    print("\n--- PROPERTIES IN FIRST FEATURE ---")
                                    if 'properties' in features[0]:
                                        for key in sorted(features[0]['properties'].keys()):
                                            value = features[0]['properties'][key]
                                            print(f"  {key:20s} : {type(value).__name__:10s} = {str(value)[:60]}")

                    except json.JSONDecodeError:
                        # Try as JSONL (newline-delimited JSON)
                        print("Not standard JSON, trying JSONL...")
                        f.seek(0)

                        lines = []
                        for i in range(5):
                            line = f.readline().decode('utf-8').strip()
                            if line:
                                lines.append(line)

                        if lines:
                            print(f"\n✓ JSONL format detected")
                            print(f"Sample records:")
                            for i, line in enumerate(lines):
                                try:
                                    record = json.loads(line)
                                    print(f"\n--- RECORD {i + 1} ---")
                                    print(json.dumps(record, indent=2))
                                except:
                                    print(f"Line {i + 1}: {line[:100]}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print("Analyzing gazetteer data sources...\n")

    analyze_pleiades()
    analyze_tgn()
    analyze_gb1900()

    print("\n" + "=" * 80)
    print("ANALYSIS COMPLETE")
    print("=" * 80)