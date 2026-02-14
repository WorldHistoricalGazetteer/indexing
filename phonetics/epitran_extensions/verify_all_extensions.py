#!/usr/bin/env python3
"""
Verify all Epitran extensions can be loaded successfully.
"""

import sys
from pathlib import Path
import epitran

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Map of all EPITRAN-EXT entries from TODO.txt
# Format: (lang_code, script_code, epitran_key)
EXTENSION_MAPPINGS = [
    ('nob', 'LATIN', 'nob-Latn'),
    ('nor', 'LATIN', 'nor-Latn'),
    ('eus', 'LATIN', 'eus-Latn'),
    ('ast', 'LATIN', 'ast-Latn'),
    ('che', 'CYRILLIC', 'che-Cyrl'),
    ('arz', 'ARABIC', 'arz-Arab'),
    ('tat', 'CYRILLIC', 'tat-Cyrl'),
    ('bul', 'CYRILLIC', 'bul-Cyrl'),
    ('hbs', 'LATIN', 'hbs-Latn'),
    ('oci', 'LATIN', 'oci-Latn'),
    ('slk', 'LATIN', 'slk-Latn'),
    ('ell', 'GREEK', 'ell-Grek'),
    ('vec', 'LATIN', 'vec-Latn'),
    ('lld', 'LATIN', 'lld-Latn'),
    ('war', 'LATIN', 'war-Latn'),
    ('hye', 'ARMENIAN', 'hye-Armn'),
    ('bre', 'LATIN', 'bre-Latn'),
    ('vol', 'LATIN', 'vol-Latn'),
    ('sco', 'LATIN', 'sco-Latn'),
    ('azb', 'ARABIC', 'azb-Arab'),
    ('mlg', 'LATIN', 'mlg-Latn'),
    ('ltz', 'LATIN', 'ltz-Latn'),
    ('min', 'LATIN', 'min-Latn'),
    ('nds', 'LATIN', 'nds-Latn'),
    ('bar', 'LATIN', 'bar-Latn'),
    ('arg', 'LATIN', 'arg-Latn'),
    ('ido', 'LATIN', 'ido-Latn'),
    ('gla', 'LATIN', 'gla-Latn'),
    ('kat', 'GEORGIAN', 'kat-Geor'),
    ('isl', 'LATIN', 'isl-Latn'),
    ('gsw', 'LATIN', 'gsw-Latn'),
    ('cos', 'LATIN', 'cos-Latn'),
    ('ina', 'LATIN', 'ina-Latn'),
    ('srd', 'LATIN', 'srd-Latn'),
    ('roh', 'LATIN', 'roh-Latn'),
    ('scn', 'LATIN', 'scn-Latn'),
    ('pms', 'LATIN', 'pms-Latn'),
    ('wln', 'LATIN', 'wln-Latn'),
    ('nap', 'LATIN', 'nap-Latn'),
    ('lmo', 'LATIN', 'lmo-Latn'),
    ('bos', 'LATIN', 'bos-Latn'),
    ('frc', 'LATIN', 'frc-Latn'),
    ('frp', 'LATIN', 'frp-Latn'),
    ('sme', 'LATIN', 'sme-Latn'),
    ('lim', 'LATIN', 'lim-Latn'),
    ('fur', 'LATIN', 'fur-Latn'),
    ('kal', 'LATIN', 'kal-Latn'),
    ('fao', 'LATIN', 'fao-Latn'),
    ('mya', 'LATIN', 'mya-Latn'),
    ('fil', 'LATIN', 'fil-Latn'),
    ('ace', 'LATIN', 'ace-Latn'),
    ('bam', 'LATIN', 'bam-Latn'),
    ('bug', 'LATIN', 'bug-Latn'),
    ('wol', 'LATIN', 'wol-Latn'),
    ('fij', 'LATIN', 'fij-Latn'),
    ('sun', 'LATIN', 'sun-Latn'),
    ('prg', 'LATIN', 'prg-Latn'),
    ('oss', 'CYRILLIC', 'oss-Cyrl'),
    ('kon', 'LATIN', 'kon-Latn'),
    ('ibo', 'LATIN', 'ibo-Latn'),
    ('vmf', 'LATIN', 'vmf-Latn'),
    ('frr', 'LATIN', 'frr-Latn'),
    ('sgs', 'LATIN', 'sgs-Latn'),
    ('pap', 'LATIN', 'pap-Latn'),
    ('nrm', 'LATIN', 'nrm-Latn'),
    ('szl', 'LATIN', 'szl-Latn'),
    ('hat', 'LATIN', 'hat-Latn'),
    ('gor', 'LATIN', 'gor-Latn'),
    ('hyw', 'ARMENIAN', 'hyw-Armn'),
    ('vls', 'LATIN', 'vls-Latn'),
    ('pcd', 'LATIN', 'pcd-Latn'),
    ('bpy', 'BENGALI', 'bpy-Beng'),
    ('nep', 'DEVANAGARI', 'nep-Deva'),
    ('rgn', 'LATIN', 'rgn-Latn'),
    ('new', 'DEVANAGARI', 'new-Deva'),
    ('chv', 'CYRILLIC', 'chv-Cyrl'),
    ('mkd', 'CYRILLIC', 'mkd-Cyrl'),
    ('guj', 'GUJARATI', 'guj-Gujr'),
    ('ban', 'LATIN', 'ban-Latn'),
    ('bjn', 'LATIN', 'bjn-Latn'),
    ('diq', 'LATIN', 'diq-Latn'),
    ('crh', 'LATIN', 'crh-Latn'),
    ('hsb', 'LATIN', 'hsb-Latn'),
    ('pnb', 'ARABIC', 'pnb-Arab'),
    ('que', 'LATIN', 'que-Latn'),
    ('khm', 'LATIN', 'khm-Latn'),
    ('lao', 'LATIN', 'lao-Latn'),
    ('bod', 'OTHER', 'bod-Tibt'),
    ('sat', 'OTHER', 'sat-Olck'),
    ('pan', 'OTHER', 'pan-Guru'),
    ('sin', 'OTHER', 'sin-Sinh'),
    ('mya', 'OTHER', 'mya-Mymr'),
    ('khm', 'OTHER', 'khm-Khmr'),
    ('kaz', 'ARABIC', 'kaz-Arab'),
    ('kur', 'ARABIC', 'kur-Arab'),
    ('kur', 'LATIN', 'kur-Latn'),
    ('glk', 'ARABIC', 'glk-Arab'),
    ('mzn', 'ARABIC', 'mzn-Arab'),
    ('bak', 'CYRILLIC', 'bak-Cyrl'),
    ('lat', 'LATIN', 'lat-Latn'),
    ('kor', 'CJK', 'kor-Hani'),
]


def main():
    extensions_dir = Path(__file__).parent
    csv_files = set(p.stem for p in extensions_dir.glob('*.csv'))

    print("=" * 80)
    print("EPITRAN EXTENSION VERIFICATION")
    print("=" * 80)
    print(f"\nFound {len(csv_files)} CSV files in {extensions_dir}")
    print(f"Testing {len(EXTENSION_MAPPINGS)} extension mappings from TODO.txt\n")

    passed = []
    failed = []
    missing_csv = []

    for lang, script, epitran_key in EXTENSION_MAPPINGS:
        csv_name = epitran_key

        # Check if CSV exists
        if csv_name not in csv_files:
            missing_csv.append((lang, script, epitran_key))
            print(f"  ✗ MISSING CSV: {epitran_key}.csv (lang={lang}, script={script})")
            continue

        # Try to load with Epitran
        try:
            epi = epitran.Epitran(epitran_key)
            # Test with a simple string
            result = epi.transliterate('test')
            passed.append((lang, script, epitran_key))
            print(f"  ✓ {epitran_key:15s} ({lang:3s}/{script:10s})")
        except Exception as e:
            failed.append((lang, script, epitran_key, str(e)))
            print(f"  ✗ FAILED {epitran_key:15s}: {e}")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Passed:      {len(passed):3d}/{len(EXTENSION_MAPPINGS)}")
    print(f"Failed:      {len(failed):3d}/{len(EXTENSION_MAPPINGS)}")
    print(f"Missing CSV: {len(missing_csv):3d}/{len(EXTENSION_MAPPINGS)}")

    if missing_csv:
        print("\n" + "=" * 80)
        print("MISSING CSV FILES")
        print("=" * 80)
        for lang, script, key in missing_csv:
            print(f"  - {key}.csv (lang={lang}, script={script})")

    if failed:
        print("\n" + "=" * 80)
        print("FAILED TO LOAD")
        print("=" * 80)
        for lang, script, key, error in failed:
            print(f"  - {key}: {error}")

    if not failed and not missing_csv:
        print("\n✓ All extensions verified successfully!")
        return 0
    else:
        print("\n✗ Some extensions need attention")
        return 1


if __name__ == '__main__':
    sys.exit(main())

