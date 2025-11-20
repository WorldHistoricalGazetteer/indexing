#!/usr/bin/env python3
"""
Test script to examine the actual structure of TGN N-Triples files.

This will help us understand:
1. How places link to their labels
2. What files contain what information
3. The actual RDF predicates used
"""

import re
import zipfile
from collections import Counter, defaultdict

DATA_DIR = "/ix1/whcdh/data"
TGN_ZIP = f"{DATA_DIR}/tgn/TGNOut_PlaceMap/TGNOut_PlaceMap.nt"


def parse_ntriple(line):
    """Parse an N-Triple line."""
    line = line.strip()
    if not line or line.startswith('#'):
        return None

    match = re.match(r'<([^>]+)>\s+<([^>]+)>\s+(.+)\s+\.$', line)
    if not match:
        return None

    subject = match.group(1)
    predicate = match.group(2)
    obj_part = match.group(3).strip()

    # Parse object
    if obj_part.startswith('<') and obj_part.endswith('>'):
        obj_value = obj_part[1:-1]
        obj_type = 'uri'
    elif obj_part.startswith('"'):
        quote_end = 1
        while quote_end < len(obj_part):
            if obj_part[quote_end] == '"' and obj_part[quote_end - 1] != '\\':
                break
            quote_end += 1

        obj_value = obj_part[1:quote_end]
        obj_type = 'literal'

        remainder = obj_part[quote_end + 1:].strip()
        if remainder.startswith('@'):
            obj_type = remainder[1:]  # Language code
        elif remainder.startswith('^^'):
            obj_type = 'typed_literal'
    else:
        obj_value = obj_part
        obj_type = 'unknown'

    return (subject, predicate, obj_value, obj_type)


def analyze_file(zip_path, filename, max_lines=100000):
    """
    Analyze a specific N-Triples file in the ZIP.

    Shows:
    - Sample triples
    - Predicate frequency
    - Sample subjects/objects
    """
    print("\n" + "=" * 80)
    print(f"ANALYZING: {filename}")
    print("=" * 80)

    predicates = Counter()
    subjects_sample = set()
    sample_triples = []

    # Track specific patterns we're looking for
    label_patterns = defaultdict(list)  # predicate -> list of examples
    place_examples = []  # Full records for a few places

    with zipfile.ZipFile(zip_path, 'r') as zf:
        try:
            with zf.open(filename, 'r') as f:
                for i, line in enumerate(f):
                    if i >= max_lines:
                        break

                    try:
                        line_str = line.decode('utf-8')
                        parsed = parse_ntriple(line_str)

                        if not parsed:
                            continue

                        subject, predicate, obj_value, obj_type = parsed

                        # Collect statistics
                        predicates[predicate] += 1
                        subjects_sample.add(subject)

                        # Save first 20 lines
                        if i < 20:
                            sample_triples.append(line_str.strip())

                        # Track label-related predicates
                        if 'label' in predicate.lower() or 'literal' in predicate.lower():
                            if len(label_patterns[predicate]) < 5:
                                label_patterns[predicate].append({
                                    'subject': subject,
                                    'object': obj_value,
                                    'type': obj_type
                                })

                        # Collect full records for first few TGN places
                        if '/tgn/' in subject and len(place_examples) < 3:
                            tgn_id = subject.split('/tgn/')[-1]
                            # Only track numbered IDs (not -place, -geometry, etc)
                            if tgn_id.isdigit() and int(tgn_id) < 10000000:
                                place_examples.append({
                                    'subject': subject,
                                    'predicate': predicate,
                                    'object': obj_value,
                                    'obj_type': obj_type
                                })

                    except (UnicodeDecodeError, ValueError) as e:
                        continue

        except KeyError:
            print(f"  File not found in archive: {filename}")
            return

    # Print results
    print(f"\nProcessed {min(i + 1, max_lines):,} lines")
    print(f"Unique subjects: {len(subjects_sample):,}")

    print("\n" + "-" * 80)
    print("FIRST 20 TRIPLES:")
    print("-" * 80)
    for triple in sample_triples[:20]:
        # Truncate long lines
        if len(triple) > 120:
            triple = triple[:117] + "..."
        print(triple)

    print("\n" + "-" * 80)
    print("TOP 30 PREDICATES:")
    print("-" * 80)
    for pred, count in predicates.most_common(30):
        # Shorten predicate URIs
        short_pred = pred.split('#')[-1] if '#' in pred else pred.split('/')[-1]
        print(f"  {count:>8,}  {short_pred:40s}  {pred}")

    if label_patterns:
        print("\n" + "-" * 80)
        print("LABEL-RELATED PREDICATES (examples):")
        print("-" * 80)
        for pred, examples in sorted(label_patterns.items()):
            print(f"\n{pred}:")
            for ex in examples[:3]:
                print(f"  Subject: {ex['subject']}")
                print(f"  Object:  {ex['object']} (type: {ex['type']})")

    if place_examples:
        print("\n" + "-" * 80)
        print("EXAMPLE PLACE RECORDS (first few complete places):")
        print("-" * 80)

        # Group by subject
        places_grouped = defaultdict(list)
        for ex in place_examples:
            places_grouped[ex['subject']].append(ex)

        for subject, triples in list(places_grouped.items())[:3]:
            print(f"\n{subject}")
            for triple in triples[:10]:  # First 10 triples for each place
                short_pred = triple['predicate'].split('#')[-1] if '#' in triple['predicate'] else \
                triple['predicate'].split('/')[-1]
                obj = triple['object'][:60] if isinstance(triple['object'], str) and len(triple['object']) > 60 else \
                triple['object']
                print(f"  {short_pred:30s} -> {obj} (type: {triple['obj_type']})")


def find_sample_place_complete_record(zip_path, place_id="7011179"):
    """
    Find ALL triples for a specific TGN place across all files.

    Default is Siena (7011179) from the Getty documentation examples.
    """
    print("\n" + "=" * 80)
    print(f"COMPLETE RECORD FOR TGN:{place_id} (Siena)")
    print("=" * 80)

    place_uri = f"http://vocab.getty.edu/tgn/{place_id}"

    files_to_check = [
        'TGNOut_1Subjects.nt',
        'TGNOut_2Terms.nt',
        'TGNOut_Coordinates.nt',
        'TGNOut_PlaceMap.nt'
    ]

    with zipfile.ZipFile(zip_path, 'r') as zf:
        for filename in files_to_check:
            print(f"\n--- In {filename} ---")
            found_any = False

            try:
                with zf.open(filename, 'r') as f:
                    for line in f:
                        try:
                            line_str = line.decode('utf-8')
                            if place_uri in line_str:
                                print(line_str.strip())
                                found_any = True
                        except:
                            continue

                if not found_any:
                    print("  (no triples found)")

            except KeyError:
                print(f"  (file not in archive)")


def main():
    print("TGN RDF STRUCTURE ANALYSIS")
    print("=" * 80)

    # Analyze each file
    files_to_analyze = [
        ('TGNOut_1Subjects.nt', 100000),
        ('TGNOut_2Terms.nt', 100000),
        ('TGNOut_Coordinates.nt', 50000),
        ('TGNOut_PlaceMap.nt', 50000)
    ]

    for filename, max_lines in files_to_analyze:
        analyze_file(TGN_ZIP, filename, max_lines)

    # Get complete record for one place
    find_sample_place_complete_record(TGN_ZIP)

    print("\n" + "=" * 80)
    print("ANALYSIS COMPLETE")
    print("=" * 80)
    print("\nNow we know:")
    print("  1. What predicates link places to labels")
    print("  2. What files contain what information")
    print("  3. How to properly extract place names")


if __name__ == "__main__":
    main()