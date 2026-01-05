# vocab/char_vocab.py
"""
Script-partitioned character vocabulary for phonetic embeddings.

This module implements a hybrid vocabulary strategy:
- Alphabetic scripts (Latin, Cyrillic, Greek, Arabic, etc.) are read natively
- Logographic scripts (CJK) are romanized via AnyAscii
- Korean Hangul is decomposed to Jamo components

The vocabulary is partitioned by script for easier debugging and
to prevent token collisions across writing systems.

Token Ranges:
─────────────────────────────────────────────────────────────
[0]         <PAD>
[1]         <UNK>
[2]         <SPACE>
[3-9]       Reserved special tokens

[10-199]    LATIN (basic + extended + diacritics)
[200-299]   CYRILLIC
[300-399]   GREEK
[400-549]   ARABIC
[550-599]   HEBREW
[600-699]   DEVANAGARI
[700-749]   BENGALI
[750-799]   TAMIL
[800-849]   TELUGU
[850-879]   MALAYALAM
[880-909]   KANNADA
[910-939]   GUJARATI
[940-989]   THAI
[990-1019]  GEORGIAN
[1020-1049] ARMENIAN
[1050-1119] KOREAN_JAMO
[1120-1199] Reserved for future scripts
[1200+]     Dynamically added characters
─────────────────────────────────────────────────────────────
"""

import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from collections import Counter, defaultdict

try:
    from anyascii import anyascii
except ImportError:
    anyascii = None
    print("Warning: anyascii not available. CJK romanization will fail.")

from phonetics.utils.script_detection import (
    Script, detect_script, should_romanize, should_decompose
)
from phonetics.utils.korean import decompose_text, get_jamo_vocab

# Special tokens
PAD_TOKEN = '<PAD>'
UNK_TOKEN = '<UNK>'
SPACE_TOKEN = '<SPACE>'

PAD_ID = 0
UNK_ID = 1
SPACE_ID = 2

# Script partition ranges
SCRIPT_RANGES = {
    Script.LATIN: (10, 200),
    Script.CYRILLIC: (200, 300),
    Script.GREEK: (300, 400),
    Script.ARABIC: (400, 550),
    Script.HEBREW: (550, 600),
    Script.DEVANAGARI: (600, 700),
    Script.BENGALI: (700, 750),
    Script.TAMIL: (750, 800),
    Script.TELUGU: (800, 850),
    Script.MALAYALAM: (850, 880),
    Script.KANNADA: (880, 910),
    Script.GUJARATI: (910, 940),
    Script.THAI: (940, 990),
    Script.GEORGIAN: (990, 1020),
    Script.ARMENIAN: (1020, 1050),
    Script.HANGUL: (1050, 1120),  # Jamo characters
    Script.OTHER: (1200, 2000),  # Dynamic allocation
}


class CharacterVocabulary:
    """
    Script-partitioned character vocabulary.

    Manages encoding of characters to token IDs with:
    - Script-aware partitioning
    - Automatic romanization for CJK
    - Automatic Jamo decomposition for Korean
    - Dynamic vocabulary growth for unseen characters
    """

    def __init__(
            self,
            char_to_id: Optional[Dict[str, int]] = None,
            allow_growth: bool = True,
    ):
        """
        Initialize vocabulary.

        Args:
            char_to_id: Pre-built character to ID mapping
            allow_growth: Whether to add new characters dynamically
        """
        self.allow_growth = allow_growth

        if char_to_id is not None:
            self.char_to_id = char_to_id
            self.id_to_char = {v: k for k, v in char_to_id.items()}
        else:
            self.char_to_id = {
                PAD_TOKEN: PAD_ID,
                UNK_TOKEN: UNK_ID,
                SPACE_TOKEN: SPACE_ID,
            }
            self.id_to_char = {
                PAD_ID: PAD_TOKEN,
                UNK_ID: UNK_TOKEN,
                SPACE_ID: SPACE_TOKEN,
            }

        # Track next available ID per script
        self._next_id: Dict[Script, int] = {}
        for script, (start, _) in SCRIPT_RANGES.items():
            self._next_id[script] = start

        # Update next_id based on existing vocab
        for char, char_id in self.char_to_id.items():
            if char_id >= 10:  # Skip special tokens
                for script, (start, end) in SCRIPT_RANGES.items():
                    if start <= char_id < end:
                        self._next_id[script] = max(self._next_id[script], char_id + 1)
                        break

    def __len__(self) -> int:
        return len(self.char_to_id)

    def _get_script_for_char(self, char: str) -> Script:
        """Get the script for a single character."""
        from phonetics.utils.script_detection import detect_char_script
        return detect_char_script(char)

    def _add_char(self, char: str, script: Script) -> int:
        """
        Add a new character to the vocabulary.

        Args:
            char: Character to add
            script: Script of the character

        Returns:
            Token ID for the character
        """
        if char in self.char_to_id:
            return self.char_to_id[char]

        if not self.allow_growth:
            return UNK_ID

        if script in (Script.CJK, Script.HIRAGANA, Script.KATAKANA):
            script = Script.LATIN

        if script not in SCRIPT_RANGES:
            script = Script.OTHER

        start, end = SCRIPT_RANGES.get(script, SCRIPT_RANGES[Script.OTHER])

        if self._next_id[script] >= end:
            # Script partition full, use OTHER range
            script = Script.OTHER
            start, end = SCRIPT_RANGES[Script.OTHER]

        char_id = self._next_id[script]
        self._next_id[script] += 1

        self.char_to_id[char] = char_id
        self.id_to_char[char_id] = char

        return char_id

    def get_char_id(self, char: str) -> int:
        """
        Get token ID for a character.

        Args:
            char: Single character

        Returns:
            Token ID (UNK_ID if not in vocab and growth disabled)
        """
        if char in self.char_to_id:
            return self.char_to_id[char]

        if char == ' ':
            return SPACE_ID

        # If growth not allowed, return UNK for unknown chars
        if not self.allow_growth:
            return UNK_ID

        script = self._get_script_for_char(char)
        return self._add_char(char, script)

    def preprocess_text(self, text: str, script: Optional[Script] = None) -> str:
        """
        Preprocess text based on its script.

        - CJK scripts are romanized via AnyAscii
        - Korean is decomposed to Jamo
        - Other scripts pass through unchanged

        Args:
            text: Input text
            script: Pre-detected script (auto-detected if None)

        Returns:
            Preprocessed text
        """
        if script is None:
            script, _ = detect_script(text)

        # Romanize CJK
        if should_romanize(script):
            if anyascii is None:
                raise RuntimeError("anyascii required for CJK romanization")
            return anyascii(text).lower()

        # Decompose Korean
        if should_decompose(script):
            return decompose_text(text)

        # Normalize everything else to NFC (Canonical Composition)
        # This turns "a" + "´" into "á", and standardizes Arabic
        return unicodedata.normalize('NFC', text)

    def encode(
            self,
            text: str,
            script: Optional[Script] = None,
            max_length: Optional[int] = None,
    ) -> List[int]:
        """
        Encode text to token IDs.

        Applies preprocessing (romanization/decomposition) before encoding.

        Args:
            text: Input text
            script: Pre-detected script (auto-detected if None)
            max_length: Maximum sequence length (truncate if exceeded)

        Returns:
            List of token IDs
        """
        # Preprocess
        processed = self.preprocess_text(text, script)

        # Encode characters
        ids = []
        for char in processed:
            if char == ' ':
                ids.append(SPACE_ID)
            elif not char.strip():
                continue  # Skip other whitespace
            else:
                ids.append(self.get_char_id(char))

        # Truncate if needed
        if max_length is not None and len(ids) > max_length:
            ids = ids[:max_length]

        return ids

    def decode(self, ids: List[int]) -> str:
        """
        Decode token IDs back to text.

        Note: Information lost to romanization/decomposition cannot be recovered.

        Args:
            ids: List of token IDs

        Returns:
            Decoded text
        """
        chars = []
        for token_id in ids:
            if token_id == PAD_ID:
                continue
            elif token_id == SPACE_ID:
                chars.append(' ')
            elif token_id in self.id_to_char:
                chars.append(self.id_to_char[token_id])
            else:
                chars.append('?')  # Unknown token
        return ''.join(chars)

    def get_script_stats(self) -> Dict[str, int]:
        """
        Get count of characters per script in vocabulary.

        Returns:
            Dictionary of script name to character count
        """
        stats = defaultdict(int)

        for char_id in self.id_to_char.keys():
            if char_id < 10:
                stats['SPECIAL'] += 1
                continue

            for script, (start, end) in SCRIPT_RANGES.items():
                if start <= char_id < end:
                    stats[script.value] += 1
                    break

        return dict(stats)

    def save(self, path: str):
        """
        Save vocabulary to JSON file.

        Args:
            path: Output file path
        """
        data = {
            'version': 2,
            'char_to_id': self.char_to_id,
            'script_ranges': {k.value: list(v) for k, v in SCRIPT_RANGES.items()},
            'stats': self.get_script_stats(),
        }

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str, allow_growth: bool = False) -> 'CharacterVocabulary':
        """
        Load vocabulary from JSON file.

        Args:
            path: Input file path
            allow_growth: Whether to allow adding new characters

        Returns:
            Loaded vocabulary
        """
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        return cls(
            char_to_id=data['char_to_id'],
            allow_growth=allow_growth,
        )

    @classmethod
    def build_from_texts(
            cls,
            texts: List[str],
            min_freq: int = 1,
            show_progress: bool = True,
    ) -> 'CharacterVocabulary':
        """
        Build vocabulary from a list of texts.

        Args:
            texts: List of input texts
            min_freq: Minimum character frequency to include
            show_progress: Whether to show progress bar

        Returns:
            Built vocabulary
        """
        vocab = cls(allow_growth=True)
        char_counts = Counter()

        iterator = texts
        if show_progress:
            try:
                from tqdm import tqdm
                iterator = tqdm(texts, desc="Building vocabulary")
            except ImportError:
                pass

        # First pass: count characters
        for text in iterator:
            script, _ = detect_script(text)
            processed = vocab.preprocess_text(text, script)
            char_counts.update(processed)

        # Second pass: add characters meeting frequency threshold
        for char, count in char_counts.items():
            if count >= min_freq and char.strip():
                script = vocab._get_script_for_char(char)
                vocab._add_char(char, script)

        return vocab


class ScriptVocabulary:
    """
    Vocabulary for script types.

    Maps Script enum values to integer IDs for embedding lookup.
    """

    def __init__(self):
        self.script_to_id = {s: i for i, s in enumerate(Script)}
        self.id_to_script = {i: s for s, i in self.script_to_id.items()}

    def __len__(self) -> int:
        return len(self.script_to_id)

    def encode(self, script: Script) -> int:
        """Get ID for a script."""
        return self.script_to_id.get(script, self.script_to_id[Script.OTHER])

    def encode_text(self, text: str) -> int:
        """Detect script from text and return ID."""
        script, _ = detect_script(text)
        return self.encode(script)

    def decode(self, script_id: int) -> Script:
        """Get script for an ID."""
        return self.id_to_script.get(script_id, Script.OTHER)

    def save(self, path: str):
        """Save to JSON file."""
        data = {
            'version': 1,
            'script_to_id': {k.value: v for k, v in self.script_to_id.items()},
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: str) -> 'ScriptVocabulary':
        """Load from JSON file."""
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        vocab = cls()
        vocab.script_to_id = {Script(k): v for k, v in data['script_to_id'].items()}
        vocab.id_to_script = {v: k for k, v in vocab.script_to_id.items()}
        return vocab


class LanguageVocabulary:
    """
    Vocabulary for language codes.

    Maps ISO 639 language codes to integer IDs.
    ID 0 is reserved for unknown language (<UNK>).
    """

    UNK_ID = 0
    UNK_TOKEN = '<UNK>'

    def __init__(self, lang_to_id: Optional[Dict[str, int]] = None):
        if lang_to_id is not None:
            self.lang_to_id = lang_to_id
        else:
            self.lang_to_id = {self.UNK_TOKEN: self.UNK_ID}

        self.id_to_lang = {v: k for k, v in self.lang_to_id.items()}
        self._next_id = max(self.id_to_lang.keys()) + 1 if self.id_to_lang else 1

    def __len__(self) -> int:
        return len(self.lang_to_id)

    def add(self, lang: str) -> int:
        """Add a language to the vocabulary."""
        if lang in self.lang_to_id:
            return self.lang_to_id[lang]

        lang_id = self._next_id
        self._next_id += 1

        self.lang_to_id[lang] = lang_id
        self.id_to_lang[lang_id] = lang

        return lang_id

    def encode(self, lang: Optional[str]) -> int:
        """
        Get ID for a language code.

        Args:
            lang: Language code (e.g., 'en', 'ru') or None

        Returns:
            Language ID (UNK_ID if unknown)
        """
        if lang is None or lang == '':
            return self.UNK_ID

        # Normalize to lowercase
        lang = lang.lower().strip()

        return self.lang_to_id.get(lang, self.UNK_ID)

    def decode(self, lang_id: int) -> str:
        """Get language code for an ID."""
        return self.id_to_lang.get(lang_id, self.UNK_TOKEN)

    def save(self, path: str):
        """Save to JSON file."""
        data = {
            'version': 1,
            'lang_to_id': self.lang_to_id,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: str) -> 'LanguageVocabulary':
        """Load from JSON file."""
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        return cls(lang_to_id=data['lang_to_id'])

    @classmethod
    def build_from_langs(cls, langs: List[str]) -> 'LanguageVocabulary':
        """
        Build vocabulary from a list of language codes.

        Args:
            langs: List of language codes

        Returns:
            Built vocabulary
        """
        vocab = cls()
        for lang in sorted(set(langs)):
            if lang:
                vocab.add(lang)
        return vocab


if __name__ == '__main__':
    # Test the vocabularies
    print("Testing CharacterVocabulary")
    print("-" * 50)

    vocab = CharacterVocabulary(allow_growth=True)

    test_cases = [
        ("London", None),
        ("Москва", None),
        ("Αθήνα", None),
        ("القاهرة", None),
        ("東京", None),  # Should be romanized
        ("서울", None),  # Should be decomposed
        ("मुंबई", None),
    ]

    for text, script in test_cases:
        detected_script, _ = detect_script(text)
        processed = vocab.preprocess_text(text, detected_script)
        ids = vocab.encode(text)
        decoded = vocab.decode(ids)

        print(f"\n{text!r} ({detected_script.value}):")
        print(f"  Processed: {processed!r}")
        print(f"  IDs: {ids}")
        print(f"  Decoded: {decoded!r}")

    print(f"\nVocabulary size: {len(vocab)}")
    print(f"Stats: {vocab.get_script_stats()}")

    # Test script and language vocabs
    print("\n" + "=" * 50)
    print("Testing ScriptVocabulary")
    script_vocab = ScriptVocabulary()
    print(f"Size: {len(script_vocab)}")
    print(f"LATIN ID: {script_vocab.encode(Script.LATIN)}")

    print("\n" + "=" * 50)
    print("Testing LanguageVocabulary")
    lang_vocab = LanguageVocabulary.build_from_langs(['en', 'ru', 'de', 'fr', 'zh', 'ko'])
    print(f"Size: {len(lang_vocab)}")
    print(f"English ID: {lang_vocab.encode('en')}")
    print(f"Unknown ID: {lang_vocab.encode('xyz')}")