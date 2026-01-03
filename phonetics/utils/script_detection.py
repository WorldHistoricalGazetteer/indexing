# /utils/script_detection.py
"""
Script detection utilities based on Unicode block analysis.

This module provides deterministic script detection for toponyms,
supporting the hybrid vocabulary strategy where alphabetic scripts
are read natively while CJK scripts are romanized.
"""

from typing import Tuple, Dict, Optional
from collections import Counter
from enum import Enum


class Script(str, Enum):
    """Supported script types with priority ordering for primary_namespace."""
    LATIN = "LATIN"
    CYRILLIC = "CYRILLIC"
    GREEK = "GREEK"
    ARABIC = "ARABIC"
    HEBREW = "HEBREW"
    DEVANAGARI = "DEVANAGARI"
    BENGALI = "BENGALI"
    TAMIL = "TAMIL"
    TELUGU = "TELUGU"
    MALAYALAM = "MALAYALAM"
    KANNADA = "KANNADA"
    GUJARATI = "GUJARATI"
    THAI = "THAI"
    GEORGIAN = "GEORGIAN"
    ARMENIAN = "ARMENIAN"
    HANGUL = "HANGUL"  # Korean - will be decomposed to Jamo
    CJK = "CJK"  # Chinese/Japanese Kanji - will be romanized
    HIRAGANA = "HIRAGANA"  # Japanese - will be romanized
    KATAKANA = "KATAKANA"  # Japanese - will be romanized
    OTHER = "OTHER"


# Scripts that should be romanized (AnyAscii) rather than read natively
ROMANIZE_SCRIPTS = {Script.CJK, Script.HIRAGANA, Script.KATAKANA}

# Scripts that should be decomposed (Korean Jamo)
DECOMPOSE_SCRIPTS = {Script.HANGUL}

# Scripts with Epitran support (can generate IPA)
EPITRAN_SUPPORTED_SCRIPTS = {
    Script.LATIN,
    Script.CYRILLIC,
    Script.GREEK,
    Script.ARABIC,
    Script.HEBREW,
    Script.DEVANAGARI,
    Script.BENGALI,
    Script.TAMIL,
    Script.TELUGU,
    Script.MALAYALAM,
    Script.KANNADA,
    Script.GUJARATI,
    Script.THAI,
    Script.GEORGIAN,
    Script.ARMENIAN,
    Script.HANGUL,
    Script.CJK,  # Mandarin via cmn-Hans
    Script.HIRAGANA,  # Japanese
    Script.KATAKANA,  # Japanese
}

# Unicode block ranges for each script
# Format: list of (start, end) inclusive ranges
SCRIPT_RANGES: Dict[Script, list] = {
    Script.LATIN: [
        (0x0000, 0x007F),  # Basic Latin
        (0x0080, 0x00FF),  # Latin-1 Supplement
        (0x0100, 0x017F),  # Latin Extended-A
        (0x0180, 0x024F),  # Latin Extended-B
        (0x0250, 0x02AF),  # IPA Extensions
        (0x1D00, 0x1D7F),  # Phonetic Extensions
        (0x1E00, 0x1EFF),  # Latin Extended Additional
        (0x2C60, 0x2C7F),  # Latin Extended-C
        (0xA720, 0xA7FF),  # Latin Extended-D
        (0xAB30, 0xAB6F),  # Latin Extended-E
    ],
    Script.CYRILLIC: [
        (0x0400, 0x04FF),  # Cyrillic
        (0x0500, 0x052F),  # Cyrillic Supplement
        (0x2DE0, 0x2DFF),  # Cyrillic Extended-A
        (0xA640, 0xA69F),  # Cyrillic Extended-B
    ],
    Script.GREEK: [
        (0x0370, 0x03FF),  # Greek and Coptic
        (0x1F00, 0x1FFF),  # Greek Extended
    ],
    Script.ARABIC: [
        (0x0600, 0x06FF),  # Arabic
        (0x0750, 0x077F),  # Arabic Supplement
        (0x08A0, 0x08FF),  # Arabic Extended-A
        (0xFB50, 0xFDFF),  # Arabic Presentation Forms-A
        (0xFE70, 0xFEFF),  # Arabic Presentation Forms-B
    ],
    Script.HEBREW: [
        (0x0590, 0x05FF),  # Hebrew
        (0xFB00, 0xFB4F),  # Alphabetic Presentation Forms (Hebrew)
    ],
    Script.DEVANAGARI: [
        (0x0900, 0x097F),  # Devanagari
        (0xA8E0, 0xA8FF),  # Devanagari Extended
    ],
    Script.BENGALI: [
        (0x0980, 0x09FF),  # Bengali
    ],
    Script.TAMIL: [
        (0x0B80, 0x0BFF),  # Tamil
    ],
    Script.TELUGU: [
        (0x0C00, 0x0C7F),  # Telugu
    ],
    Script.MALAYALAM: [
        (0x0D00, 0x0D7F),  # Malayalam
    ],
    Script.KANNADA: [
        (0x0C80, 0x0CFF),  # Kannada
    ],
    Script.GUJARATI: [
        (0x0A80, 0x0AFF),  # Gujarati
    ],
    Script.THAI: [
        (0x0E00, 0x0E7F),  # Thai
    ],
    Script.GEORGIAN: [
        (0x10A0, 0x10FF),  # Georgian
        (0x2D00, 0x2D2F),  # Georgian Supplement
    ],
    Script.ARMENIAN: [
        (0x0530, 0x058F),  # Armenian
        (0xFB00, 0xFB17),  # Armenian ligatures in Alphabetic PF
    ],
    Script.HANGUL: [
        (0xAC00, 0xD7AF),  # Hangul Syllables
        (0x1100, 0x11FF),  # Hangul Jamo
        (0x3130, 0x318F),  # Hangul Compatibility Jamo
        (0xA960, 0xA97F),  # Hangul Jamo Extended-A
        (0xD7B0, 0xD7FF),  # Hangul Jamo Extended-B
    ],
    Script.CJK: [
        (0x4E00, 0x9FFF),  # CJK Unified Ideographs
        (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
        (0x20000, 0x2A6DF),  # CJK Unified Ideographs Extension B
        (0x2A700, 0x2B73F),  # CJK Unified Ideographs Extension C
        (0x2B740, 0x2B81F),  # CJK Unified Ideographs Extension D
        (0x2B820, 0x2CEAF),  # CJK Unified Ideographs Extension E
        (0x2CEB0, 0x2EBEF),  # CJK Unified Ideographs Extension F
        (0x30000, 0x3134F),  # CJK Unified Ideographs Extension G
        (0x3000, 0x303F),  # CJK Symbols and Punctuation
        (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    ],
    Script.HIRAGANA: [
        (0x3040, 0x309F),  # Hiragana
        (0x1B000, 0x1B0FF),  # Kana Supplement
    ],
    Script.KATAKANA: [
        (0x30A0, 0x30FF),  # Katakana
        (0x31F0, 0x31FF),  # Katakana Phonetic Extensions
        (0xFF65, 0xFF9F),  # Halfwidth Katakana
    ],
}


def _build_codepoint_map() -> Dict[int, Script]:
    """
    Build a lookup dict mapping codepoints to scripts.
    This is cached for fast repeated lookups.
    """
    codepoint_map = {}
    for script, ranges in SCRIPT_RANGES.items():
        for start, end in ranges:
            for cp in range(start, end + 1):
                codepoint_map[cp] = script
    return codepoint_map


# Build the map once at module load
_CODEPOINT_MAP = _build_codepoint_map()


def detect_char_script(char: str) -> Script:
    """
    Detect the script of a single character.

    Args:
        char: A single character

    Returns:
        Script enum value
    """
    if len(char) != 1:
        raise ValueError(f"Expected single character, got {len(char)}")

    return _CODEPOINT_MAP.get(ord(char), Script.OTHER)


def detect_script(text: str) -> Tuple[Script, Dict[Script, int]]:
    """
    Detect the dominant script in a text string.

    Uses character counting to determine the primary script,
    ignoring spaces, punctuation, and digits.

    Args:
        text: Input text

    Returns:
        Tuple of (dominant_script, {script: count})
    """
    if not text:
        return Script.OTHER, {}

    counts: Counter = Counter()

    for char in text:
        # Skip non-letter characters
        if not char.isalpha():
            continue

        script = detect_char_script(char)
        counts[script] += 1

    if not counts:
        return Script.OTHER, {}

    # Find dominant script
    dominant = counts.most_common(1)[0][0]

    return dominant, dict(counts)


def should_romanize(script: Script) -> bool:
    """
    Determine if a script should be romanized via AnyAscii.

    CJK scripts have vocabulary explosion issues and should
    be romanized to Latin characters.

    Args:
        script: Script enum value

    Returns:
        True if the script should be romanized
    """
    return script in ROMANIZE_SCRIPTS


def should_decompose(script: Script) -> bool:
    """
    Determine if a script should be decomposed.

    Korean Hangul syllables should be decomposed to Jamo
    components (onset, nucleus, coda).

    Args:
        script: Script enum value

    Returns:
        True if the script should be decomposed
    """
    return script in DECOMPOSE_SCRIPTS


def has_epitran_support(script: Script) -> bool:
    """
    Check if a script has Epitran support for IPA generation.

    Args:
        script: Script enum value

    Returns:
        True if Epitran can generate IPA for this script
    """
    return script in EPITRAN_SUPPORTED_SCRIPTS


def get_mixed_script_info(text: str) -> Dict:
    """
    Get detailed information about scripts in a text.

    Useful for debugging mixed-script inputs like "Москва (Moscow)".

    Args:
        text: Input text

    Returns:
        Dictionary with script analysis
    """
    dominant, counts = detect_script(text)
    total = sum(counts.values())

    return {
        'dominant_script': dominant.value,
        'script_counts': {s.value: c for s, c in counts.items()},
        'script_percentages': {
            s.value: round(c / total * 100, 1)
            for s, c in counts.items()
        } if total > 0 else {},
        'is_mixed': len(counts) > 1,
        'should_romanize': should_romanize(dominant),
        'should_decompose': should_decompose(dominant),
        'has_epitran': has_epitran_support(dominant),
    }


# Namespace priority order
# Higher priority = more authoritative source
NAMESPACE_PRIORITY = {
    'gn': 0,  # GeoNames - large, authoritative
    'wd': 1,  # Wikidata - curated
    'tgn': 2,  # Getty TGN - art historical
    'pl': 3,  # Pleiades - ancient world
    'iv': 4,  # Index Villaris - historical England
    'gb': 5,  # GB1900 - historical Britain
    'other': 6,  # Catch-all
    'osm': 7,  # OpenStreetMap - variable quality
}


def get_primary_namespace(namespaces: list) -> str:
    """
    Determine the primary (most authoritative) namespace.

    Args:
        namespaces: List of namespace strings

    Returns:
        The highest priority namespace
    """
    if not namespaces:
        return 'other'

    # Sort by priority and return the highest (lowest number)
    sorted_ns = sorted(
        namespaces,
        key=lambda ns: NAMESPACE_PRIORITY.get(ns, NAMESPACE_PRIORITY['other'])
    )
    return sorted_ns[0]


if __name__ == '__main__':
    # Test cases
    test_cases = [
        "London",
        "Москва",
        "Αθήνα",
        "القاهرة",
        "東京",
        "서울",
        "मुंबई",
        "กรุงเทพ",
        "Москва (Moscow)",  # Mixed
        "北京 Beijing",  # Mixed
    ]

    for text in test_cases:
        info = get_mixed_script_info(text)
        print(f"\n{text!r}:")
        print(f"  Dominant: {info['dominant_script']}")
        print(f"  Counts: {info['script_counts']}")
        print(f"  Romanize: {info['should_romanize']}")
        print(f"  Decompose: {info['should_decompose']}")