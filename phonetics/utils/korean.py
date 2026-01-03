# utils/korean.py
"""
Korean Hangul decomposition utilities.

Hangul syllables are composed of Jamo (자모) components:
- Choseong (초성): Initial consonant
- Jungseong (중성): Medial vowel
- Jongseong (종성): Final consonant (optional)

For example: 한 (han) = ㅎ (h) + ㅏ (a) + ㄴ (n)

This decomposition reduces the Korean vocabulary from 11,172 syllable
blocks to ~70 Jamo characters, making it tractable for the character
embedding model while preserving phonetic information.
"""

from typing import List, Optional

# Hangul syllable block range
HANGUL_SYLLABLE_START = 0xAC00  # '가'
HANGUL_SYLLABLE_END = 0xD7A3  # '힣'

# Hangul Jamo ranges
JAMO_LEAD_START = 0x1100  # Choseong (initial consonants)
JAMO_VOWEL_START = 0x1161  # Jungseong (vowels)
JAMO_TAIL_START = 0x11A8  # Jongseong (final consonants)

# Component counts
N_LEAD = 19  # Number of initial consonants
N_VOWEL = 21  # Number of vowels
N_TAIL = 28  # Number of final consonants (including none)

# Choseong (initial consonants) - 19 total
CHOSEONG = [
    'ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ', 'ㅂ', 'ㅃ', 'ㅅ',
    'ㅆ', 'ㅇ', 'ㅈ', 'ㅉ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ'
]

# Jungseong (vowels) - 21 total
JUNGSEONG = [
    'ㅏ', 'ㅐ', 'ㅑ', 'ㅒ', 'ㅓ', 'ㅔ', 'ㅕ', 'ㅖ', 'ㅗ', 'ㅘ',
    'ㅙ', 'ㅚ', 'ㅛ', 'ㅜ', 'ㅝ', 'ㅞ', 'ㅟ', 'ㅠ', 'ㅡ', 'ㅢ', 'ㅣ'
]

# Jongseong (final consonants) - 27 total + empty
# Index 0 = no final consonant
JONGSEONG = [
    '', 'ㄱ', 'ㄲ', 'ㄳ', 'ㄴ', 'ㄵ', 'ㄶ', 'ㄷ', 'ㄹ', 'ㄺ',
    'ㄻ', 'ㄼ', 'ㄽ', 'ㄾ', 'ㄿ', 'ㅀ', 'ㅁ', 'ㅂ', 'ㅄ', 'ㅅ',
    'ㅆ', 'ㅇ', 'ㅈ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ'
]

# All unique Jamo characters for vocabulary
ALL_JAMO = list(set(CHOSEONG + JUNGSEONG + [j for j in JONGSEONG if j]))


def is_hangul_syllable(char: str) -> bool:
    """
    Check if a character is a composed Hangul syllable.

    Args:
        char: Single character

    Returns:
        True if it's a Hangul syllable block
    """
    if len(char) != 1:
        return False
    code = ord(char)
    return HANGUL_SYLLABLE_START <= code <= HANGUL_SYLLABLE_END


def is_hangul_jamo(char: str) -> bool:
    """
    Check if a character is a Hangul Jamo.

    Args:
        char: Single character

    Returns:
        True if it's a Jamo character
    """
    if len(char) != 1:
        return False
    code = ord(char)
    # Check compatibility Jamo range
    return 0x3130 <= code <= 0x318F


def decompose_syllable(char: str) -> List[str]:
    """
    Decompose a single Hangul syllable into Jamo components.

    The decomposition formula is:
    syllable_code = 0xAC00 + (lead * 21 * 28) + (vowel * 28) + tail

    Args:
        char: Single Hangul syllable character

    Returns:
        List of Jamo characters [choseong, jungseong, jongseong?]

    Raises:
        ValueError: If not a valid Hangul syllable

    Examples:
        >>> decompose_syllable('한')
        ['ㅎ', 'ㅏ', 'ㄴ']
        >>> decompose_syllable('가')
        ['ㄱ', 'ㅏ']
    """
    if not is_hangul_syllable(char):
        raise ValueError(f"'{char}' is not a Hangul syllable")

    code = ord(char) - HANGUL_SYLLABLE_START

    # Decompose indices
    lead_idx = code // (N_VOWEL * N_TAIL)
    vowel_idx = (code % (N_VOWEL * N_TAIL)) // N_TAIL
    tail_idx = code % N_TAIL

    result = [CHOSEONG[lead_idx], JUNGSEONG[vowel_idx]]

    if tail_idx > 0:
        result.append(JONGSEONG[tail_idx])

    return result


def decompose_text(text: str) -> str:
    """
    Decompose all Hangul syllables in text to Jamo.

    Non-Hangul characters are passed through unchanged.

    Args:
        text: Input text containing Korean

    Returns:
        Text with Hangul syllables decomposed to Jamo

    Examples:
        >>> decompose_text('서울')
        'ㅅㅓㅇㅜㄹ'
        >>> decompose_text('Seoul 서울')
        'Seoul ㅅㅓㅇㅜㄹ'
    """
    result = []
    for char in text:
        if is_hangul_syllable(char):
            result.extend(decompose_syllable(char))
        else:
            result.append(char)
    return ''.join(result)


def compose_syllable(choseong: str, jungseong: str, jongseong: Optional[str] = None) -> str:
    """
    Compose Jamo components back into a Hangul syllable.

    Args:
        choseong: Initial consonant
        jungseong: Vowel
        jongseong: Final consonant (optional)

    Returns:
        Composed Hangul syllable

    Raises:
        ValueError: If invalid Jamo components

    Examples:
        >>> compose_syllable('ㅎ', 'ㅏ', 'ㄴ')
        '한'
    """
    try:
        lead_idx = CHOSEONG.index(choseong)
        vowel_idx = JUNGSEONG.index(jungseong)
        tail_idx = JONGSEONG.index(jongseong) if jongseong else 0
    except ValueError as e:
        raise ValueError(f"Invalid Jamo component: {e}")

    code = HANGUL_SYLLABLE_START + (lead_idx * N_VOWEL * N_TAIL) + (vowel_idx * N_TAIL) + tail_idx
    return chr(code)


def get_jamo_vocab() -> List[str]:
    """
    Get the complete Jamo vocabulary for character embedding.

    Returns:
        Sorted list of all unique Jamo characters
    """
    return sorted(ALL_JAMO)


def get_jamo_info(char: str) -> dict:
    """
    Get information about a Jamo character.

    Args:
        char: Single Jamo character

    Returns:
        Dictionary with Jamo type and romanization
    """
    JAMO_ROMANIZATION = {
        'ㄱ': 'g', 'ㄲ': 'kk', 'ㄴ': 'n', 'ㄷ': 'd', 'ㄸ': 'tt',
        'ㄹ': 'r', 'ㅁ': 'm', 'ㅂ': 'b', 'ㅃ': 'pp', 'ㅅ': 's',
        'ㅆ': 'ss', 'ㅇ': 'ng', 'ㅈ': 'j', 'ㅉ': 'jj', 'ㅊ': 'ch',
        'ㅋ': 'k', 'ㅌ': 't', 'ㅍ': 'p', 'ㅎ': 'h',
        'ㅏ': 'a', 'ㅐ': 'ae', 'ㅑ': 'ya', 'ㅒ': 'yae', 'ㅓ': 'eo',
        'ㅔ': 'e', 'ㅕ': 'yeo', 'ㅖ': 'ye', 'ㅗ': 'o', 'ㅘ': 'wa',
        'ㅙ': 'wae', 'ㅚ': 'oe', 'ㅛ': 'yo', 'ㅜ': 'u', 'ㅝ': 'wo',
        'ㅞ': 'we', 'ㅟ': 'wi', 'ㅠ': 'yu', 'ㅡ': 'eu', 'ㅢ': 'ui',
        'ㅣ': 'i',
        # Compound finals
        'ㄳ': 'gs', 'ㄵ': 'nj', 'ㄶ': 'nh', 'ㄺ': 'lg', 'ㄻ': 'lm',
        'ㄼ': 'lb', 'ㄽ': 'ls', 'ㄾ': 'lt', 'ㄿ': 'lp', 'ㅀ': 'lh',
        'ㅄ': 'bs',
    }

    jamo_type = 'unknown'
    if char in CHOSEONG:
        jamo_type = 'choseong'  # Initial consonant
    elif char in JUNGSEONG:
        jamo_type = 'jungseong'  # Vowel
    elif char in JONGSEONG:
        jamo_type = 'jongseong'  # Final consonant

    return {
        'char': char,
        'type': jamo_type,
        'romanization': JAMO_ROMANIZATION.get(char, '?'),
    }


if __name__ == '__main__':
    # Test decomposition
    test_words = [
        '한글',  # Hangul
        '서울',  # Seoul
        '부산',  # Busan
        '대한민국',  # Republic of Korea
        '가',  # Single syllable without final
        '강',  # Single syllable with final
    ]

    print("Hangul Decomposition Tests:")
    print("-" * 50)

    for word in test_words:
        decomposed = decompose_text(word)
        print(f"{word} → {decomposed}")

        # Show component breakdown
        for char in word:
            if is_hangul_syllable(char):
                components = decompose_syllable(char)
                roman = [get_jamo_info(j)['romanization'] for j in components]
                print(f"  {char}: {' + '.join(components)} ({' '.join(roman)})")

    print(f"\nJamo vocabulary size: {len(get_jamo_vocab())}")
    print(f"Jamo characters: {' '.join(get_jamo_vocab())}")