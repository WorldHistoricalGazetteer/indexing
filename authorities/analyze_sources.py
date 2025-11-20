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
            # Try reading line by line in case it's JSONL
            first_line = f.readline()
            f.seek(0)

            # Check if it's a single JSON object or array
            try:
                data = json.load(f)

                print(f"\nFile: {file_path}")
                print(f"Type: {type(data)}")

                if isinstance(data, dict):
                    print(f"JSON object with keys: {sorted(data.keys())}")

                    # If it's a wrapper object with @graph or features
                    if '@graph' in data:
                        records = data['@graph']
                        print(f"Contains @graph with {len(records)} records")
                    elif 'features' in data:
                        records = data['features']
                        print(f"GeoJSON with {len(records)} features")
                    else:
                        records = [data]

                elif isinstance(data, list):
                    records = data
                    print(f"JSON array with {len(records)} records")
                else:
                    print(f"Unexpected JSON structure")
                    return

                if records:
                    print("\n" + "-" * 80)
                    print("FIRST RECORD:")
                    print("-" * 80)
                    print(json.dumps(records[0], indent=2))

                    if len(records) > 1:
                        print("\n" + "-" * 80)
                        print("SECOND RECORD:")
                        print("-" * 80)
                        print(json.dumps(records[1], indent=2))

                    print("\n" + "-" * 80)
                    print("KEYS IN FIRST RECORD:")
                    print("-" * 80)
                    for key in sorted(records[0].keys()):
                        value = records[0][key]
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

            except json.JSONDecodeError:
                # Try JSONL format
                print("\nNot standard JSON, trying JSONL (newline-delimited)...")
                f.seek(0)

                count = 0
                for i, line in enumerate(f):
                    if i >= 3:
                        break
                    try:
                        record = json.loads(line)
                        count += 1
                        print(f"\n--- RECORD {i + 1} ---")
                        print(json.dumps(record, indent=2))
                    except:
                        continue

                if count > 0:
                    print(f"\n✓ JSONL format with records")

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

        # Try different encodings - RDF files often use latin-1 or iso-8859-1
        for encoding in ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']:
            try:
                with open(file_path, 'r', encoding=encoding) as f:
                    # Test reading
                    test_line = f.readline()
                    print(f"✓ Successfully opened with encoding: {encoding}")
                    break
            except UnicodeDecodeError:
                continue
        else:
            print("Could not find working encoding")
            return

        # Count lines
        with open(file_path, 'r', encoding=encoding) as f:
            line_count = sum(1 for _ in f)
        print(f"Total triples: {line_count:,}")

        # Analyze predicates and show samples
        predicates = Counter()
        subjects = set()

        print("\n" + "-" * 80)
        print("FIRST 30 TRIPLES:")
        print("-" * 80)

        with open(file_path, 'r', encoding=encoding) as f:
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

        # Show all triples for one subject
        print("\n" + "-" * 80)
        print("ALL TRIPLES FOR FIRST SUBJECT:")
        print("-" * 80)
        if subjects:
            first_subject = list(subjects)[0]
            with open(file_path, 'r', encoding=encoding) as f:
                count = 0
                for line in f:
                    if first_subject in line:
                        print(line.rstrip())
                        count += 1
                        if count >= 50:
                            print("... (truncated)")
                            break

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
                size_mb = info.file_size / (1024 * 1024)
                print(f"  {name:60s} {size_mb:>10.2f} MB")

            # Find CSV file
            csv_files = [n for n in zf.namelist() if n.endswith('.csv')]

            if csv_files:
                csv_file = csv_files[0]
                print("\n" + "-" * 80)
                print(f"ANALYZING CSV: {csv_file}")
                print("-" * 80)

                import csv

                with zf.open(csv_file, 'r') as f:
                    # Try different encodings
                    for encoding in ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']:
                        try:
                            f.seek(0)
                            text_file = (line.decode(encoding) for line in f)
                            reader = csv.DictReader(text_file)

                            # Get header
                            first_row = next(reader)
                            print(f"\n✓ Successfully opened with encoding: {encoding}")

                            print("\n--- CSV COLUMNS ---")
                            for col in reader.fieldnames:
                                print(f"  {col}")

                            print("\n--- FIRST RECORD ---")
                            for key, value in first_row.items():
                                display_value = value[:60] if value and len(value) > 60 else value
                                print(f"  {key:20s} : {display_value}")

                            # Read a couple more records
                            print("\n--- SECOND RECORD ---")
                            second_row = next(reader)
                            for key, value in second_row.items():
                                display_value = value[:60] if value and len(value) > 60 else value
                                print(f"  {key:20s} : {display_value}")

                            print("\n--- THIRD RECORD ---")
                            third_row = next(reader)
                            for key, value in third_row.items():
                                display_value = value[:60] if value and len(value) > 60 else value
                                print(f"  {key:20s} : {display_value}")

                            break

                        except (UnicodeDecodeError, StopIteration) as e:
                            if encoding == 'cp1252':
                                print(f"Could not decode with any encoding: {e}")
                            continue
            else:
                print("\nNo CSV files found in archive")

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