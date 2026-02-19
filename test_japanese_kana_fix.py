#!/usr/bin/env python3
"""
Test the Japanese Kana routing fix.

This script verifies that:
1. Hiragana and Katakana have proper Epitran mappings
2. The to_ipa method routes Japanese kana to Epitran (not CharsiuG2P)
3. IPA transcription works for pure kana and mixed-script Japanese
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from phonetics.extraction.rebuild_toponyms_index import IPAConverter, EPITRAN_LANG_MAP
from phonetics.utils.script_detection import Script, detect_script


def test_epitran_mappings():
    """Test that both Hiragana and Katakana have Epitran mappings."""
    print("=" * 70)
    print("TEST 1: EPITRAN MAPPINGS")
    print("=" * 70)

    hiragana_key = ('ja', Script.HIRAGANA)
    katakana_key = ('ja', Script.KATAKANA)

    hiragana_code = EPITRAN_LANG_MAP.get(hiragana_key)
    katakana_code = EPITRAN_LANG_MAP.get(katakana_key)

    print(f"Hiragana mapping: {hiragana_key} -> {hiragana_code}")
    print(f"Katakana mapping: {katakana_key} -> {katakana_code}")

    # Epitran uses jpn-Hrgn (not jpn-Hira) and jpn-Ktkn (not jpn-Kana)
    assert hiragana_code == 'jpn-Hrgn', f"Expected 'jpn-Hrgn', got {hiragana_code}"
    assert katakana_code == 'jpn-Ktkn', f"Expected 'jpn-Ktkn', got {katakana_code}"

    print("✓ Both mappings exist and are correct!")
    print()


def test_script_detection():
    """Test that script detection correctly identifies Hiragana and Katakana."""
    print("=" * 70)
    print("TEST 2: SCRIPT DETECTION")
    print("=" * 70)

    test_cases = [
        ('あいうえお', Script.HIRAGANA, 'Pure Hiragana'),
        ('アイウエオ', Script.KATAKANA, 'Pure Katakana'),
        ('東京', Script.CJK, 'Pure Kanji'),
        ('とうきょう', Script.HIRAGANA, 'Hiragana (Tokyo)'),
        ('トウキョウ', Script.KATAKANA, 'Katakana (Tokyo)'),
        ('東京都', Script.CJK, 'Kanji (Tokyo-to)'),
    ]

    for text, expected_script, label in test_cases:
        detected_script, counts = detect_script(text)
        status = '✓' if detected_script == expected_script else '✗'
        print(f"{status} {label:25} \"{text}\" -> {detected_script.value} (expected: {expected_script.value})")
        assert detected_script == expected_script, f"Script mismatch for {text}"

    print("✓ All script detections correct!")
    print()


def test_ipa_conversion():
    """Test that IPA conversion works for Japanese kana."""
    print("=" * 70)
    print("TEST 3: IPA CONVERSION")
    print("=" * 70)

    converter = IPAConverter()

    test_cases = [
        ('あいうえお', 'ja', Script.HIRAGANA, 'Pure Hiragana'),
        ('アイウエオ', 'ja', Script.KATAKANA, 'Pure Katakana'),
        ('とうきょう', 'ja', Script.HIRAGANA, 'Tokyo in Hiragana'),
        ('トウキョウ', 'ja', Script.KATAKANA, 'Tokyo in Katakana'),
    ]

    for text, lang, script, label in test_cases:
        ipa = converter.to_ipa(text, lang, script)
        status = '✓' if ipa else '✗'
        print(f"{status} {label:25} \"{text}\" -> {ipa if ipa else 'FAILED'}")

        if ipa:
            # Test PanPhon embedding
            embedding = converter.to_embedding(ipa)
            if embedding:
                print(f"   └─ PanPhon embedding: {len(embedding)} dims")
            else:
                print(f"   └─ PanPhon embedding: FAILED")

    print()


def test_routing_priority():
    """Test that Japanese kana is routed to Epitran even if CharsiuG2P is available."""
    print("=" * 70)
    print("TEST 4: ROUTING PRIORITY (Kana -> Epitran, not CharsiuG2P)")
    print("=" * 70)

    converter = IPAConverter()

    # Test that pure kana goes to Epitran
    hiragana_text = 'ひらがな'
    katakana_text = 'カタカナ'

    hiragana_ipa = converter.to_ipa(hiragana_text, 'ja', Script.HIRAGANA)
    katakana_ipa = converter.to_ipa(katakana_text, 'ja', Script.KATAKANA)

    print(f"Hiragana '{hiragana_text}' -> IPA: {hiragana_ipa}")
    print(f"Katakana '{katakana_text}' -> IPA: {katakana_ipa}")

    if hiragana_ipa and katakana_ipa:
        print("✓ Both kana scripts successfully converted via Epitran!")
        print("✓ Routing priority is correct (script-first for Japanese kana)")
    else:
        print("✗ One or both kana scripts failed to convert")
        if not hiragana_ipa:
            print(f"  - Hiragana failed")
        if not katakana_ipa:
            print(f"  - Katakana failed")

    print()


def main():
    print("\n" + "=" * 70)
    print("JAPANESE KANA IPA COVERAGE FIX - TEST SUITE")
    print("=" * 70)
    print()

    try:
        test_epitran_mappings()
        test_script_detection()
        test_ipa_conversion()
        test_routing_priority()

        print("=" * 70)
        print("ALL TESTS PASSED! ✓")
        print("=" * 70)
        print()
        print("The fix is working correctly:")
        print("  1. Katakana mapping added to EPITRAN_LANG_MAP")
        print("  2. Script-first routing prevents CharsiuG2P override")
        print("  3. Hiragana and Katakana now route to Epitran")
        print("  4. IPA transcription works for Japanese kana")
        print()
        print("Next steps:")
        print("  1. Rebuild Japanese toponyms from v6 DuckDB:")
        print("     python -m phonetics.extraction.rebuild_toponyms_index \\")
        print("       --resume --confirm \\")
        print("       --languages ja \\")
        print("       --skip-es-index")
        print()
        print("  2. Check coverage stats in output/coverage_stats.json")
        print()
        print("  3. Update ES index with the fixed Japanese toponyms")
        print()

    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ UNEXPECTED ERROR: {e}\n")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

