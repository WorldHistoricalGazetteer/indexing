#!/usr/bin/env python3
"""
Extended test to find where the actual place name text is stored in TGN.

We know from the structure analysis:
- tgn:7011179 has skosxl:prefLabel -> tgn/term/47413-en
- But where is "Siena" actually stored?

This script will search for:
1. The term URI tgn/term/47413-en
2. Any predicate that gives us the literal text
"""

import re
import zipfile

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


def find_term_literals(zip_path, term_ids_to_find):
    """
    Search TGNOut_2Terms.nt for specific term IDs and show ALL their triples.
    """
    print("=" * 80)
    print("SEARCHING FOR TERM LITERALS")
    print("=" * 80)
    print(f"Looking for term IDs: {term_ids_to_find}")
    print()

    found_triples = {term_id: [] for term_id in term_ids_to_find}

    with zipfile.ZipFile(zip_path, 'r') as zf:
        with zf.open('TGNOut_2Terms.nt', 'r') as f:
            for i, line in enumerate(f):
                if (i + 1) % 1000000 == 0:
                    print(f"  Scanned {i + 1:,} lines...")

                try:
                    line_str = line.decode('utf-8')

                    # Check if any of our target term IDs are in this line
                    for term_id in term_ids_to_find:
                        if term_id in line_str:
                            parsed = parse_ntriple(line_str)
                            if parsed:
                                found_triples[term_id].append(parsed)

                except (UnicodeDecodeError, ValueError):
                    continue

    # Print results
    for term_id, triples in found_triples.items():
        print(f"\n{'=' * 80}")
        print(f"TERM: {term_id}")
        print(f"Found {len(triples)} triples")
        print('=' * 80)

        if triples:
            for subject, predicate, obj_value, obj_type in triples:
                print(f"\nSubject:   {subject}")
                print(f"Predicate: {predicate}")
                print(f"Object:    {obj_value}")
                print(f"Type:      {obj_type}")
        else:
            print("NO TRIPLES FOUND for this term!")


def sample_terms_with_literals(zip_path, max_examples=10):
    """
    Find ANY terms that have literal text, just to see the pattern.
    """
    print("\n" + "=" * 80)
    print("SAMPLING TERMS WITH LITERAL TEXT")
    print("=" * 80)
    print(f"Finding first {max_examples} examples of terms with text literals...")
    print()

    examples = []

    with zipfile.ZipFile(zip_path, 'r') as zf:
        with zf.open('TGNOut_2Terms.nt', 'r') as f:
            for i, line in enumerate(f):
                if len(examples) >= max_examples:
                    break

                if (i + 1) % 1000000 == 0:
                    print(f"  Scanned {i + 1:,} lines, found {len(examples)} examples...")

                try:
                    line_str = line.decode('utf-8')
                    parsed = parse_ntriple(line_str)

                    if not parsed:
                        continue

                    subject, predicate, obj_value, obj_type = parsed

                    # Look for literal text (not URIs, not typed literals)
                    # Likely has language tag
                    if obj_type not in ['uri', 'typed_literal', 'unknown']:
                        # This is a literal with language tag
                        examples.append({
                            'subject': subject,
                            'predicate': predicate,
                            'text': obj_value,
                            'lang': obj_type
                        })

                except (UnicodeDecodeError, ValueError):
                    continue

    print(f"\nFound {len(examples)} examples:\n")

    for i, ex in enumerate(examples, 1):
        print(f"Example {i}:")
        print(f"  Subject:   {ex['subject']}")
        print(f"  Predicate: {ex['predicate']}")
        print(f"  Text:      \"{ex['text']}\"@{ex['lang']}")

        # Extract term ID from subject
        if '/term/' in ex['subject']:
            term_id = ex['subject'].split('/term/')[-1]
            print(f"  Term ID:   {term_id}")
        print()


def main():
    print("EXTENDED TGN STRUCTURE TEST")
    print("Finding where literal text is actually stored")
    print()

    # From our earlier analysis, we know Siena (tgn:7011179) has these term URIs
    siena_terms = [
        'term/47413-en',  # English preferred
        'term/47413-fr',  # French preferred
        'term/47413-it',  # Italian preferred
    ]

    # First, find some examples of ANY terms with literal text
    sample_terms_with_literals(TGN_ZIP, max_examples=10)

    # Then search for Siena's specific terms
    find_term_literals(TGN_ZIP, siena_terms)

    print("\n" + "=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()