#!/usr/bin/env python3
"""
Test ALL language-script pairs from TODO.txt to determine:
1. Natively supported by Epitran (no CSV needed)
2. Supported via extension CSV
3. Not supported at all (needs new CSV)
"""

import epitran
import sys
from pathlib import Path
from collections import defaultdict

# Parse TODO.txt to extract all language-script pairs
TODO_PATH = Path(__file__).parent / "TODO.txt"

def parse_todo():
    """Extract all language-script pairs from TODO.txt"""
    pairs = []

    with open(TODO_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('|---') or line.startswith('| Lang'):
                continue
            if line.startswith('|'):
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 6:
                    lang = parts[1]
                    script = parts[2]
                    epitran_key = parts[5]
                    status = parts[6] if len(parts) > 6 else ""

                    # Skip Charsiu and Phonikud items
                    if 'CHARSIU' in status or 'PHONIKUD' in status:
                        continue

                    # Skip if epitran_key is None
                    if epitran_key in ['None', 'N/A', '']:
                        continue

                    pairs.append({
                        'lang': lang,
                        'script': script,
                        'epitran_key': epitran_key,
                        'status': status
                    })

    return pairs

def test_epitran_support(epitran_key, test_char='test'):
    """
    Test if an Epitran code is natively supported
    Returns: ('NATIVE', ipa) or ('EXTENSION', ipa) or ('MISSING', error)
    """
    try:
        epi = epitran.Epitran(epitran_key)
        ipa = epi.transliterate(test_char)

        # Check if this required an extension file
        extension_path = Path(epitran.__file__).parent / 'data' / 'map' / f'{epitran_key}.csv'
        custom_extension = Path(__file__).parent / f'{epitran_key}.csv'

        if custom_extension.exists() and extension_path.exists():
            # Check if they're the same file (our extension was installed)
            return ('EXTENSION', ipa)
        else:
            return ('NATIVE', ipa)

    except FileNotFoundError as e:
        return ('MISSING', str(e))
    except Exception as e:
        return ('ERROR', str(e))

def main():
    pairs = parse_todo()

    print("=" * 80)
    print("COMPLETE EPITRAN SUPPORT TEST")
    print("=" * 80)
    print(f"Testing {len(pairs)} language-script pairs from TODO.txt")
    print("(Excluding CHARSIU and PHONIKUD items)")
    print("=" * 80)
    print()

    results = defaultdict(list)

    for pair in pairs:
        lang = pair['lang']
        script = pair['script']
        epitran_key = pair['epitran_key']
        status = pair['status']

        support_type, result = test_epitran_support(epitran_key)
        results[support_type].append({
            'lang': lang,
            'script': script,
            'epitran_key': epitran_key,
            'status': status,
            'result': result
        })

    # Print results grouped by support type
    print("\n" + "=" * 80)
    print("NATIVELY SUPPORTED BY EPITRAN (no extension needed)")
    print("=" * 80)
    if results['NATIVE']:
        for item in sorted(results['NATIVE'], key=lambda x: x['lang']):
            print(f"  ✓ {item['lang']:6s} | {item['script']:12s} | {item['epitran_key']:15s} | {item['result'][:30]}")
    else:
        print("  (none)")
    print(f"\nTotal: {len(results['NATIVE'])}")

    print("\n" + "=" * 80)
    print("SUPPORTED VIA EXTENSION CSV")
    print("=" * 80)
    if results['EXTENSION']:
        for item in sorted(results['EXTENSION'], key=lambda x: x['lang']):
            print(f"  ✓ {item['lang']:6s} | {item['script']:12s} | {item['epitran_key']:15s} | {item['result'][:30]}")
    else:
        print("  (none)")
    print(f"\nTotal: {len(results['EXTENSION'])}")

    print("\n" + "=" * 80)
    print("MISSING (needs extension CSV)")
    print("=" * 80)
    if results['MISSING']:
        for item in sorted(results['MISSING'], key=lambda x: x['lang']):
            print(f"  ✗ {item['lang']:6s} | {item['script']:12s} | {item['epitran_key']:15s}")
            print(f"      Error: {item['result']}")
    else:
        print("  (none)")
    print(f"\nTotal: {len(results['MISSING'])}")

    print("\n" + "=" * 80)
    print("ERRORS (other issues)")
    print("=" * 80)
    if results['ERROR']:
        for item in sorted(results['ERROR'], key=lambda x: x['lang']):
            print(f"  ✗ {item['lang']:6s} | {item['script']:12s} | {item['epitran_key']:15s}")
            print(f"      Error: {item['result']}")
    else:
        print("  (none)")
    print(f"\nTotal: {len(results['ERROR'])}")

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"  Native support:     {len(results['NATIVE']):3d}")
    print(f"  Extension support:  {len(results['EXTENSION']):3d}")
    print(f"  Missing:            {len(results['MISSING']):3d}")
    print(f"  Errors:             {len(results['ERROR']):3d}")
    print(f"  Total tested:       {len(pairs):3d}")
    print("=" * 80)

    # Check for items marked as EPITRAN but actually MISSING
    print("\n" + "=" * 80)
    print("DISCREPANCIES (marked as EPITRAN but not working)")
    print("=" * 80)
    discrepancies = [item for item in results['MISSING'] + results['ERROR']
                     if 'EPITRAN' in item['status'] and 'Inferred' not in item['status']]
    if discrepancies:
        for item in discrepancies:
            print(f"  ⚠ {item['lang']:6s} | {item['script']:12s} | {item['epitran_key']:15s} | Status: {item['status']}")
    else:
        print("  (none found)")
    print("=" * 80)

if __name__ == '__main__':
    main()

