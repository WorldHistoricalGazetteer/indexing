#!/usr/bin/env python3
"""
Test which Epitran language codes actually load successfully.

This diagnoses why certain scripts aren't getting PanPhon embeddings.

Usage:

srun -p htc --mem=64G --cpus-per-task=4 --pty bash
cd /ix1/ishi/elastic
python -m testing.test_epitran_loading
"""

import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from phonetics.utils.script_detection import Script

# Import the map from rebuild_toponyms_index
EPITRAN_LANG_MAP = {
    ('en', Script.LATIN): 'eng-Latn',
    ('de', Script.LATIN): 'deu-Latn',
    ('fr', Script.LATIN): 'fra-Latn',
    ('es', Script.LATIN): 'spa-Latn',
    ('it', Script.LATIN): 'ita-Latn',
    ('pt', Script.LATIN): 'por-Latn',
    ('nl', Script.LATIN): 'nld-Latn',
    ('pl', Script.LATIN): 'pol-Latn',
    ('cs', Script.LATIN): 'ces-Latn',
    ('ro', Script.LATIN): 'ron-Latn',
    ('hu', Script.LATIN): 'hun-Latn',
    ('fi', Script.LATIN): 'fin-Latn',
    ('sv', Script.LATIN): 'swe-Latn',
    ('no', Script.LATIN): 'nor-Latn',
    ('da', Script.LATIN): 'dan-Latn',
    ('tr', Script.LATIN): 'tur-Latn',
    ('vi', Script.LATIN): 'vie-Latn',
    ('id', Script.LATIN): 'ind-Latn',
    ('ms', Script.LATIN): 'msa-Latn',
    ('sw', Script.LATIN): 'swa-Latn',
    ('la', Script.LATIN): 'lat-Latn',
    ('ru', Script.CYRILLIC): 'rus-Cyrl',
    ('uk', Script.CYRILLIC): 'ukr-Cyrl',
    ('bg', Script.CYRILLIC): 'bul-Cyrl',
    ('sr', Script.CYRILLIC): 'srp-Cyrl',
    ('mk', Script.CYRILLIC): 'mkd-Cyrl',
    ('el', Script.GREEK): 'ell-Grek',
    ('ar', Script.ARABIC): 'ara-Arab',
    ('fa', Script.ARABIC): 'fas-Arab',
    ('ur', Script.ARABIC): 'urd-Arab',
    ('he', Script.HEBREW): 'heb-Hebr',
    ('hi', Script.DEVANAGARI): 'hin-Deva',
    ('mr', Script.DEVANAGARI): 'mar-Deva',
    ('ne', Script.DEVANAGARI): 'nep-Deva',
    ('sa', Script.DEVANAGARI): 'san-Deva',
    ('bn', Script.BENGALI): 'ben-Beng',
    ('ta', Script.TAMIL): 'tam-Taml',
    ('te', Script.TELUGU): 'tel-Telu',
    ('ml', Script.MALAYALAM): 'mal-Mlym',
    ('kn', Script.KANNADA): 'kan-Knda',
    ('gu', Script.GUJARATI): 'guj-Gujr',
    ('th', Script.THAI): 'tha-Thai',
    ('ka', Script.GEORGIAN): 'kat-Geor',
    ('hy', Script.ARMENIAN): 'hye-Armn',
    ('ko', Script.HANGUL): 'kor-Hang',
    ('zh', Script.CJK): 'cmn-Hans',
    ('ja', Script.HIRAGANA): 'jpn-Hira',
}


def test_all_epitran_codes():
    """Test loading every Epitran code in our map."""
    try:
        import epitran
    except ImportError:
        print("ERROR: epitran not installed!")
        return

    print("=" * 70)
    print("EPITRAN LANGUAGE CODE LOADING TEST")
    print("=" * 70)
    print()

    # Group by script for easier reading
    by_script = {}
    for (lang, script), epi_code in sorted(EPITRAN_LANG_MAP.items(), key=lambda x: (x[0][1].value, x[0][0])):
        if script not in by_script:
            by_script[script] = []
        by_script[script].append((lang, epi_code))

    failed_codes = []
    successful_codes = []

    for script in sorted(by_script.keys(), key=lambda s: s.value):
        print(f"\n{script.value} SCRIPT:")
        print("-" * 70)

        for lang, epi_code in by_script[script]:
            try:
                epi = epitran.Epitran(epi_code)
                # Test with a simple string
                test_word = "test" if script == Script.LATIN else "αβγ"
                result = epi.transliterate(test_word)
                print(f"  ✓ {lang:4s} → {epi_code:12s} : SUCCESS ('{test_word}' → '{result}')")
                successful_codes.append((lang, script, epi_code))
            except Exception as e:
                error_msg = str(e)
                if len(error_msg) > 50:
                    error_msg = error_msg[:47] + "..."
                print(f"  ✗ {lang:4s} → {epi_code:12s} : FAILED - {error_msg}")
                failed_codes.append((lang, script, epi_code, str(e)))

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Successful: {len(successful_codes)}/{len(EPITRAN_LANG_MAP)}")
    print(f"Failed:     {len(failed_codes)}/{len(EPITRAN_LANG_MAP)}")

    if failed_codes:
        print("\nFAILED CODES (scripts with ZERO pairs):")
        print("-" * 70)
        for lang, script, epi_code, error in failed_codes:
            print(f"  {script.value:12s} {lang:4s} → {epi_code:12s}")
            print(f"      Error: {error}")

    # Check which of the missing scripts are affected
    missing_scripts = {'ARMENIAN', 'GREEK', 'GUJARATI', 'HEBREW', 'HIRAGANA', 'KANNADA', 'KATAKANA', 'OTHER'}
    affected_missing = set()
    for lang, script, epi_code, error in failed_codes:
        if script.value in missing_scripts:
            affected_missing.add(script.value)

    if affected_missing:
        print("\n" + "!" * 70)
        print("CRITICAL: Failed codes affect these MISSING scripts:")
        for script_name in sorted(affected_missing):
            print(f"  - {script_name}")
        print("!" * 70)


if __name__ == '__main__':
    test_all_epitran_codes()
