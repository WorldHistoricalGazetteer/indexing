#!/usr/bin/env python3
"""
Analyze Epitran coverage and test Charsiu G2P as a fallback.

This script:
1. Tests which language codes work with Epitran
2. Identifies scripts with zero coverage
3. Tests Charsiu G2P on sample toponyms from missing scripts
4. Compares Charsiu IPA output quality against known good cases

Usage:
    srun -p htc --mem=64G --cpus-per-task=4 --pty bash
    cd /ix1/whcdh/elastic
    python -m testing.analyze_epitran_coverage
"""

import sys
import os
import warnings
import json
from collections import defaultdict

# Suppress epitran warnings
warnings.filterwarnings('ignore', module='epitran')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from elasticsearch import Elasticsearch
from processing.settings import ES_HOST

try:
    import epitran
    HAS_EPITRAN = True
except ImportError:
    HAS_EPITRAN = False
    print("WARNING: epitran not available")

try:
    from charsiu import charsiu_predictive_g2p
    HAS_CHARSIU = True
except ImportError:
    HAS_CHARSIU = False
    print("WARNING: charsiu not available")
    print("Install with: pip install charsiu")

try:
    import panphon
    HAS_PANPHON = True
except ImportError:
    HAS_PANPHON = False
    print("WARNING: panphon not available")


# Standard language→Epitran code mapping
EPITRAN_LANG_MAP = {
    # Arabic script
    'ar': 'ara-Arab',
    'fa': 'fas-Arab',
    'ur': 'urd-Arab',
    # Armenian
    'hy': 'hye-Armn',
    # Bengali
    'bn': 'ben-Beng',
    # CJK
    'zh': 'cmn-Hans',
    # Cyrillic
    'bg': 'bul-Cyrl',
    'mk': 'mkd-Cyrl',
    'ru': 'rus-Cyrl',
    'sr': 'srp-Cyrl',
    'uk': 'ukr-Cyrl',
    # Devanagari
    'hi': 'hin-Deva',
    'mr': 'mar-Deva',
    'ne': 'nep-Deva',
    'sa': 'san-Deva',
    # Georgian
    'ka': 'kat-Geor',
    # Greek
    'el': 'ell-Grek',
    # Gujarati
    'gu': 'guj-Gujr',
    # Hangul
    'ko': 'kor-Hang',
    # Hebrew
    'he': 'heb-Hebr',
    # Japanese
    'ja': 'jpn-Hira',
    # Kannada
    'kn': 'kan-Knda',
    # Latin
    'cs': 'ces-Latn',
    'da': 'dan-Latn',
    'de': 'deu-Latn',
    'en': 'eng-Latn',
    'es': 'spa-Latn',
    'fi': 'fin-Latn',
    'fr': 'fra-Latn',
    'hu': 'hun-Latn',
    'id': 'ind-Latn',
    'it': 'ita-Latn',
    'la': 'lat-Latn',
    'ms': 'msa-Latn',
    'nl': 'nld-Latn',
    'no': 'nor-Latn',
    'pl': 'pol-Latn',
    'pt': 'por-Latn',
    'ro': 'ron-Latn',
    'sv': 'swe-Latn',
    'sw': 'swa-Latn',
    'tr': 'tur-Latn',
    'vi': 'vie-Latn',
    # Malayalam
    'ml': 'mal-Mlym',
    # Tamil
    'ta': 'tam-Taml',
    # Telugu
    'te': 'tel-Telu',
    # Thai
    'th': 'tha-Thai',
}

# Group by script for reporting
SCRIPT_TO_LANGS = defaultdict(list)
for lang, epitran_code in EPITRAN_LANG_MAP.items():
    script = epitran_code.split('-')[1]
    SCRIPT_TO_LANGS[script].append(lang)


def test_epitran_coverage():
    """Test which Epitran language codes actually work."""
    print("=" * 70)
    print("EPITRAN LANGUAGE CODE LOADING TEST")
    print("=" * 70)
    print()

    if not HAS_EPITRAN:
        print("ERROR: epitran not installed")
        return {}, {}

    successful = {}
    failed = {}

    for script, langs in sorted(SCRIPT_TO_LANGS.items()):
        print(f"\n{script} SCRIPT:")
        print("-" * 70)

        for lang in langs:
            epitran_code = EPITRAN_LANG_MAP[lang]

            try:
                epi = epitran.Epitran(epitran_code)
                # Test with a simple string
                test_str = "test" if script == "Latn" else "αβγ"
                result = epi.transliterate(test_str)
                print(f"  ✓ {lang:5} → {epitran_code:15}: SUCCESS ('{test_str}' → '{result}')")
                successful[lang] = epitran_code

            except FileNotFoundError as e:
                print(f"  ✗ {lang:5} → {epitran_code:15}: FAILED - {str(e)[:60]}...")
                failed[lang] = str(e)
            except Exception as e:
                print(f"  ✗ {lang:5} → {epitran_code:15}: FAILED - {type(e).__name__}: {str(e)[:60]}")
                failed[lang] = str(e)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Successful: {len(successful)}/{len(EPITRAN_LANG_MAP)}")
    print(f"Failed:     {len(failed)}/{len(EPITRAN_LANG_MAP)}")

    if failed:
        # Group failures by script
        failed_by_script = defaultdict(list)
        for lang in failed:
            epitran_code = EPITRAN_LANG_MAP[lang]
            script = epitran_code.split('-')[1]
            failed_by_script[script].append(lang)

        print("\nFAILED CODES (scripts with ZERO pairs):")
        print("-" * 70)
        for script, langs in sorted(failed_by_script.items()):
            for lang in langs:
                epitran_code = EPITRAN_LANG_MAP[lang]
                print(f"  {script:15} {lang:5} → {epitran_code:15}")
                print(f"      Error: {failed[lang][:80]}")

        # Highlight critical missing scripts
        critical_scripts = set()
        vocab_path = '/ix1/whcdh/models/phonetic/data/v5/vocab/script_vocab.json'
        if os.path.exists(vocab_path):
            with open(vocab_path, 'r') as f:
                vocab_data = json.load(f)
                vocab_scripts = set(vocab_data['script_to_id'].keys())

            for lang in failed:
                epitran_code = EPITRAN_LANG_MAP[lang]
                script = epitran_code.split('-')[1]
                # Map Epitran script codes to our vocab script names
                script_map = {
                    'Armn': 'ARMENIAN',
                    'Grek': 'GREEK',
                    'Gujr': 'GUJARATI',
                    'Hebr': 'HEBREW',
                    'Hira': 'HIRAGANA',
                    'Knda': 'KANNADA',
                }
                vocab_script = script_map.get(script)
                if vocab_script and vocab_script in vocab_scripts:
                    critical_scripts.add(vocab_script)

        if critical_scripts:
            print("\n" + "!" * 70)
            print("CRITICAL: Failed codes affect these MISSING scripts:")
            for script in sorted(critical_scripts):
                print(f"  - {script}")
            print("!" * 70)

    return successful, failed


def test_charsiu_fallback(es, failed_langs):
    """Test Charsiu G2P on sample toponyms from scripts where Epitran failed."""
    print("\n" + "=" * 70)
    print("CHARSIU G2P FALLBACK TEST")
    print("=" * 70)

    if not HAS_CHARSIU:
        print("ERROR: charsiu not installed")
        print("Install with: pip install charsiu")
        return

    if not HAS_PANPHON:
        print("ERROR: panphon not installed")
        return

    # Initialize Charsiu
    print("\nInitializing Charsiu G2P model (this may take a moment)...")
    try:
        g2p = charsiu_predictive_g2p()
        print("✓ Charsiu model loaded")
    except Exception as e:
        print(f"✗ Failed to load Charsiu: {e}")
        return

    # Initialize PanPhon
    ft = panphon.FeatureTable()

    # Map Epitran failures to our script names
    script_map = {
        'Armn': 'ARMENIAN',
        'Grek': 'GREEK',
        'Gujr': 'GUJARATI',
        'Hebr': 'HEBREW',
        'Hira': 'HIRAGANA',
        'Knda': 'KANNADA',
        'Cyrl': 'CYRILLIC',  # For bg, mk
        'Deva': 'DEVANAGARI',  # For ne, sa
        'Latn': 'LATIN',  # For da, la, no
    }

    failed_scripts = set()
    for lang in failed_langs:
        epitran_code = EPITRAN_LANG_MAP[lang]
        script_code = epitran_code.split('-')[1]
        vocab_script = script_map.get(script_code)
        if vocab_script:
            failed_scripts.add((vocab_script, lang))

    print(f"\nTesting Charsiu on {len(failed_scripts)} failed language/script combinations...")

    results = {}

    for vocab_script, lang in sorted(failed_scripts):
        print(f"\n{'-' * 70}")
        print(f"SCRIPT: {vocab_script}, LANGUAGE: {lang}")
        print('-' * 70)

        # Get sample toponyms from ES
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"script": vocab_script}},
                        {"term": {"lang": lang}}
                    ]
                }
            }
        }

        try:
            sample = es.search(
                index="toponyms",
                body={**query, "size": 10, "_source": ["name", "lang", "script"]}
            )

            hits = sample["hits"]["hits"]
            if not hits:
                print(f"  No toponyms found for {vocab_script}:{lang}")
                continue

            print(f"  Found {len(hits)} sample toponyms")

            successful_conversions = 0

            for h in hits:
                name = h["_source"]["name"]

                try:
                    # Predict IPA using Charsiu
                    # Charsiu expects language code (e.g., 'el' for Greek)
                    ipa = g2p.predict(name, lang)

                    # Convert IPA to PanPhon features
                    try:
                        segments = ft.word_to_segs(ipa)
                        if segments:
                            successful_conversions += 1
                            print(f"  ✓ '{name}' → IPA: '{ipa}' → {len(segments)} PanPhon segments")
                        else:
                            print(f"  ⚠ '{name}' → IPA: '{ipa}' → NO PanPhon segments")
                    except Exception as panphon_err:
                        print(f"  ⚠ '{name}' → IPA: '{ipa}' → PanPhon error: {panphon_err}")

                except Exception as e:
                    print(f"  ✗ '{name}' → Charsiu error: {e}")

            success_rate = (successful_conversions / len(hits)) * 100 if hits else 0
            results[f"{vocab_script}:{lang}"] = {
                'total': len(hits),
                'successful': successful_conversions,
                'rate': success_rate
            }

            print(f"  Success rate: {successful_conversions}/{len(hits)} ({success_rate:.1f}%)")

        except Exception as e:
            print(f"  ERROR querying ES: {e}")

    # Summary
    print("\n" + "=" * 70)
    print("CHARSIU FALLBACK SUMMARY")
    print("=" * 70)

    if results:
        total_toponyms = sum(r['total'] for r in results.values())
        total_successful = sum(r['successful'] for r in results.values())
        overall_rate = (total_successful / total_toponyms * 100) if total_toponyms > 0 else 0

        print(f"\nOverall: {total_successful}/{total_toponyms} ({overall_rate:.1f}%) successfully converted")
        print("\nPer script/language:")
        for key, data in sorted(results.items()):
            print(f"  {key:30} {data['successful']:3}/{data['total']:3} ({data['rate']:5.1f}%)")

        print("\n" + "!" * 70)
        print("RECOMMENDATION:")
        if overall_rate > 70:
            print(f"  ✓ Charsiu achieves {overall_rate:.1f}% success rate")
            print("  ✓ PROCEED with Charsiu integration as Epitran fallback")
        elif overall_rate > 50:
            print(f"  ⚠ Charsiu achieves {overall_rate:.1f}% success rate")
            print("  ⚠ Consider integration with quality monitoring")
        else:
            print(f"  ✗ Charsiu achieves only {overall_rate:.1f}% success rate")
            print("  ✗ May need alternative approach")
        print("!" * 70)


def main():
    # Test Epitran coverage
    successful, failed = test_epitran_coverage()

    if not failed:
        print("\n✓ All Epitran language codes work!")
        return

    # Connect to ES for Charsiu testing
    print(f"\n\nConnecting to Elasticsearch at {ES_HOST}...")
    try:
        es = Elasticsearch([ES_HOST])
        if not es.ping():
            print("ERROR: Cannot connect to Elasticsearch")
            return
        print("✓ Connected to ES")
    except Exception as e:
        print(f"ERROR connecting to ES: {e}")
        return

    # Test Charsiu on failed languages
    test_charsiu_fallback(es, failed)


if __name__ == "__main__":
    main()
