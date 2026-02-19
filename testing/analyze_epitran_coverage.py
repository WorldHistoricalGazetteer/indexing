#!/usr/bin/env python3
"""
Analyze Epitran coverage and test Charsiu G2P (ByT5) as a fallback.

This script:
1. Tests which language codes work with Epitran
2. Identifies scripts with zero coverage (missing .csv rule files)
3. Tests Charsiu G2P (ByT5) via Transformers on sample toponyms from missing scripts
4. Validates compatibility with PanPhon feature extraction

Usage:
    srun -p htc --mem=64G --cpus-per-task=4 --pty bash
    cd /ix1/ishi/elastic
    python -m testing.analyze_epitran_coverage
"""

import sys
import os
import warnings
import json
import torch
from collections import defaultdict

# Suppress epitran and transformer warnings
warnings.filterwarnings('ignore', module='epitran')
warnings.filterwarnings('ignore', category=UserWarning)

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
    from transformers import T5ForConditionalGeneration, AutoTokenizer

    HAS_TRANSFORMERS = True
except ImportError:
    HAS_TRANSFORMERS = False
    print("WARNING: transformers not available. Install with: pip install transformers torch sentencepiece")

try:
    import panphon

    HAS_PANPHON = True
except ImportError:
    HAS_PANPHON = False
    print("WARNING: panphon not available")

# Standard language→Epitran code mapping
EPITRAN_LANG_MAP = {
    'ar': 'ara-Arab', 'fa': 'fas-Arab', 'ur': 'urd-Arab', 'hy': 'hye-Armn',
    'bn': 'ben-Beng', 'zh': 'cmn-Hans', 'bg': 'bul-Cyrl', 'mk': 'mkd-Cyrl',
    'ru': 'rus-Cyrl', 'sr': 'srp-Cyrl', 'uk': 'ukr-Cyrl', 'hi': 'hin-Deva',
    'mr': 'mar-Deva', 'ne': 'nep-Deva', 'sa': 'san-Deva', 'ka': 'kat-Geor',
    'el': 'ell-Grek', 'gu': 'guj-Gujr', 'ko': 'kor-Hang', 'he': 'heb-Hebr',
    'ja': 'jpn-Hrgn', 'kn': 'kan-Knda', 'cs': 'ces-Latn', 'da': 'dan-Latn',
    'de': 'deu-Latn', 'en': 'eng-Latn', 'es': 'spa-Latn', 'fi': 'fin-Latn',
    'fr': 'fra-Latn', 'hu': 'hun-Latn', 'id': 'ind-Latn', 'it': 'ita-Latn',
    'la': 'lat-Latn', 'ms': 'msa-Latn', 'nl': 'nld-Latn', 'no': 'nor-Latn',
    'pl': 'pol-Latn', 'pt': 'por-Latn', 'ro': 'ron-Latn', 'sv': 'swe-Latn',
    'sw': 'swa-Latn', 'tr': 'tur-Latn', 'vi': 'vie-Latn', 'ml': 'mal-Mlym',
    'ta': 'tam-Taml', 'te': 'tel-Telu', 'th': 'tha-Thai',
}

# ISO Mapping for Charsiu (ByT5-small model)
CHARSIU_ISO_MAP = {
    'el': 'ell', 'he': 'heb', 'hy': 'hye', 'gu': 'guj',
    'kn': 'kan', 'ja': 'jpn', 'bg': 'bul', 'mk': 'mkd'
}

SCRIPT_TO_LANGS = defaultdict(list)
for lang, epitran_code in EPITRAN_LANG_MAP.items():
    script = epitran_code.split('-')[1]
    SCRIPT_TO_LANGS[script].append(lang)


def test_epitran_coverage():
    """Test which Epitran language codes actually work vs. missing CSVs."""
    print("=" * 70)
    print("EPITRAN LANGUAGE CODE LOADING TEST")
    print("=" * 70)

    if not HAS_EPITRAN: return {}, {}
    successful, failed = {}, {}

    for script, langs in sorted(SCRIPT_TO_LANGS.items()):
        print(f"\n{script} SCRIPT:")
        print("-" * 70)
        for lang in langs:
            code = EPITRAN_LANG_MAP[lang]
            try:
                epi = epitran.Epitran(code)
                test_str = "test" if script == "Latn" else "αβγ"
                result = epi.transliterate(test_str)
                print(f"  ✓ {lang:5} → {code:15}: SUCCESS ('{test_str}' → '{result}')")
                successful[lang] = code
            except Exception as e:
                print(f"  ✗ {lang:5} → {code:15}: FAILED - {str(e)[:60]}")
                failed[lang] = str(e)
    return successful, failed


def test_charsiu_fallback(es, failed_langs):
    """Test Charsiu G2P using ByT5-small with corrected PanPhon API calls."""
    print("\n" + "=" * 70)
    print("CHARSIU G2P (BYT5) FALLBACK TEST")
    print("=" * 70)

    # Initialize Models
    model_id = "charsiu/g2p_multilingual_byT5_small_100"
    tokenizer = AutoTokenizer.from_pretrained("google/byt5-small")
    model = T5ForConditionalGeneration.from_pretrained(model_id)

    # Correct PanPhon Initialization
    ft = panphon.FeatureTable()

    script_map = {
        'Armn': 'ARMENIAN', 'Grek': 'GREEK', 'Gujr': 'GUJARATI',
        'Hebr': 'HEBREW', 'Hira': 'HIRAGANA', 'Knda': 'KANNADA',
        'Cyrl': 'CYRILLIC', 'Deva': 'DEVANAGARI', 'Latn': 'LATIN'
    }

    results = {}
    for lang in failed_langs:
        code = EPITRAN_LANG_MAP[lang]
        v_script = script_map.get(code.split('-')[1])
        char_iso = CHARSIU_ISO_MAP.get(lang, lang)

        print(f"\nTesting {v_script} ({lang}) via Charsiu...")
        query = {"query": {"bool": {"must": [{"term": {"script": v_script}}, {"term": {"lang": lang}}]}}}

        try:
            sample = es.search(index="toponyms", body={**query, "size": 5})
            hits = sample["hits"]["hits"]
            if not hits: continue

            success_count = 0
            for h in hits:
                name = h["_source"]["name"]
                input_text = f"<{char_iso}>: {name}"
                inputs = tokenizer(input_text, return_tensors="pt")

                with torch.no_grad():
                    out = model.generate(**inputs, max_length=50)
                ipa = tokenizer.decode(out[0], skip_special_tokens=True)

                # Use ipa_segs to get the list of phonemes
                segs = ft.ipa_segs(ipa)

                if segs:
                    # Use word_to_vector_list to get actual articulatory features
                    vectors = ft.word_to_vector_list(ipa)
                    success_count += 1
                    print(f"  ✓ '{name}' → IPA: '{ipa}' ({len(segs)} segments, {len(vectors)} vectors)")
                else:
                    print(f"  ✗ '{name}' → Predicted IPA '{ipa}' yielded 0 PanPhon segments")

            results[f"{v_script}:{lang}"] = (success_count, len(hits))
        except Exception as e:
            print(f"  ERROR: {e}")

    # Final Summary
    print("\n" + "=" * 70)
    print("FINAL RECOMMENDATION")
    print("=" * 70)
    for key, (s, t) in results.items():
        rate = (s / t) * 100
        status = "✓" if rate > 80 else "⚠"
        print(f"{status} {key:25}: {rate:5.1f}% Success")


def main():
    successful, failed = test_epitran_coverage()

    # Connect to ES
    try:
        es = Elasticsearch([ES_HOST])
        if es.ping():
            test_charsiu_fallback(es, failed)
    except Exception as e:
        print(f"ES Connection failed: {e}")


if __name__ == "__main__":
    main()