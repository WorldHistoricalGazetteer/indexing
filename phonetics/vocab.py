"""
Vocabulary Management for Phonetic Similarity Model.

Handles character and language vocabularies with stable hashing for unseen tokens.
"""

import pickle
from collections import defaultdict
from typing import List

import h5py

from .config import Config


class CharVocab:
    """
    Character vocabulary with stable hashing for unseen characters.
    Built from training data, with fallback hash for inference on unseen chars.
    """

    PAD_TOKEN = '<PAD>'
    UNK_TOKEN = '<UNK>'

    def __init__(self, vocab_size: int = Config.VOCAB_SIZE):
        self.vocab_size = vocab_size
        self.char_to_id = {self.PAD_TOKEN: 0, self.UNK_TOKEN: 1}
        self.id_to_char = {0: self.PAD_TOKEN, 1: self.UNK_TOKEN}
        self.next_id = 2
        self.frozen = False
        self.hash_space_start = None

    def fit(self, hdf5_path: str) -> 'CharVocab':
        """Build vocabulary from HDF5 training data."""
        print("Building character vocabulary...")
        char_counts = defaultdict(int)

        with h5py.File(hdf5_path, 'r') as f:
            items = f['items']
            total_items = f.attrs['total_items']

            for idx in range(total_items):
                text = items['romanized'][idx]
                # Handle bytes if stored as bytes
                if isinstance(text, bytes):
                    text = text.decode('utf-8')
                for c in text:
                    char_counts[c] += 1

        # Reserve 20% of vocab for hash fallback
        max_vocab_chars = int(self.vocab_size * 0.8)

        # Add most frequent characters to vocab
        for char, _ in sorted(char_counts.items(), key=lambda x: -x[1]):
            if self.next_id >= max_vocab_chars:
                break
            if char not in self.char_to_id:
                self.char_to_id[char] = self.next_id
                self.id_to_char[self.next_id] = char
                self.next_id += 1

        self.hash_space_start = self.next_id
        self.frozen = True

        hash_slots = self.vocab_size - self.hash_space_start
        print(f"CharVocab: {len(self.char_to_id)} explicit chars, {hash_slots} hash slots")

        return self

    def fit_multi(self, hdf5_paths: List[str]) -> 'CharVocab':
        """Build vocabulary from multiple HDF5 training data files."""
        print(f"Building character vocabulary from {len(hdf5_paths)} sources...")
        char_counts = defaultdict(int)

        for path in hdf5_paths:
            with h5py.File(path, 'r') as f:
                items = f['items']
                total_items = f.attrs['total_items']

                for idx in range(total_items):
                    text = items['romanized'][idx]
                    if isinstance(text, bytes):
                        text = text.decode('utf-8')
                    for c in text:
                        char_counts[c] += 1

        # Reserve 20% of vocab for hash fallback
        max_vocab_chars = int(self.vocab_size * 0.8)

        # Add most frequent characters to vocab
        for char, _ in sorted(char_counts.items(), key=lambda x: -x[1]):
            if self.next_id >= max_vocab_chars:
                break
            if char not in self.char_to_id:
                self.char_to_id[char] = self.next_id
                self.id_to_char[self.next_id] = char
                self.next_id += 1

        self.hash_space_start = self.next_id
        self.frozen = True

        hash_slots = self.vocab_size - self.hash_space_start
        print(f"CharVocab: {len(self.char_to_id)} explicit chars, {hash_slots} hash slots")

        return self

    def encode(self, text: str, max_len: int = Config.MAX_TOPONYM_LEN) -> List[int]:
        """Convert text to character IDs."""
        if isinstance(text, bytes):
            text = text.decode('utf-8')

        ids = []
        for c in text[:max_len]:
            if c in self.char_to_id:
                ids.append(self.char_to_id[c])
            elif self.hash_space_start is not None:
                # Stable hash for unseen characters using FNV-1a variant
                h = ((ord(c) * 2654435761) % (self.vocab_size - self.hash_space_start)) + self.hash_space_start
                ids.append(h)
            else:
                ids.append(self.char_to_id[self.UNK_TOKEN])
        return ids

    def save(self, path: str):
        """Save vocabulary to file."""
        data = {
            'char_to_id': self.char_to_id,
            'id_to_char': self.id_to_char,
            'vocab_size': self.vocab_size,
            'next_id': self.next_id,
            'hash_space_start': self.hash_space_start,
            'frozen': self.frozen
        }
        with open(path, 'wb') as f:
            pickle.dump(data, f)

    @classmethod
    def load(cls, path: str) -> 'CharVocab':
        """Load vocabulary from file."""
        with open(path, 'rb') as f:
            data = pickle.load(f)

        vocab = cls(data['vocab_size'])
        vocab.char_to_id = data['char_to_id']
        vocab.id_to_char = data['id_to_char']
        vocab.next_id = data['next_id']
        vocab.hash_space_start = data['hash_space_start']
        vocab.frozen = data['frozen']
        return vocab


class LangVocab:
    """
    Language vocabulary mapping ISO 639-1 codes to integer IDs.
    """

    UNK_LANG = '<UNK_LANG>'

    def __init__(self):
        self.lang_to_id = {self.UNK_LANG: 0}
        self.id_to_lang = {0: self.UNK_LANG}
        self.next_id = 1

    def fit(self, hdf5_path: str) -> 'LangVocab':
        """Build vocabulary from HDF5 training data."""
        print("Building language vocabulary...")
        with h5py.File(hdf5_path, 'r') as f:
            items = f['items']
            total_items = f.attrs['total_items']

            languages = set()
            for idx in range(total_items):
                lang = items['lang'][idx]
                if isinstance(lang, bytes):
                    lang = lang.decode('utf-8')
                languages.add(lang)

            for lang in sorted(languages):
                if lang not in self.lang_to_id:
                    self.lang_to_id[lang] = self.next_id
                    self.id_to_lang[self.next_id] = lang
                    self.next_id += 1

        print(f"LangVocab: {len(self.lang_to_id)} languages")
        return self

    def fit_multi(self, hdf5_paths: List[str]) -> 'LangVocab':
        """Build vocabulary from multiple HDF5 training data files."""
        print(f"Building language vocabulary from {len(hdf5_paths)} sources...")
        languages = set()

        for path in hdf5_paths:
            with h5py.File(path, 'r') as f:
                items = f['items']
                total_items = f.attrs['total_items']

                for idx in range(total_items):
                    lang = items['lang'][idx]
                    if isinstance(lang, bytes):
                        lang = lang.decode('utf-8')
                    languages.add(lang)

        for lang in sorted(languages):
            if lang not in self.lang_to_id:
                self.lang_to_id[lang] = self.next_id
                self.id_to_lang[self.next_id] = lang
                self.next_id += 1

        print(f"LangVocab: {len(self.lang_to_id)} languages")
        return self

    def encode(self, lang: str) -> int:
        """Convert language code to ID."""
        if isinstance(lang, bytes):
            lang = lang.decode('utf-8')
        return self.lang_to_id.get(lang, self.lang_to_id[self.UNK_LANG])

    def save(self, path: str):
        """Save vocabulary to file."""
        data = {
            'lang_to_id': self.lang_to_id,
            'id_to_lang': self.id_to_lang,
            'next_id': self.next_id
        }
        with open(path, 'wb') as f:
            pickle.dump(data, f)

    @classmethod
    def load(cls, path: str) -> 'LangVocab':
        """Load vocabulary from file."""
        with open(path, 'rb') as f:
            data = pickle.load(f)

        vocab = cls()
        vocab.lang_to_id = data['lang_to_id']
        vocab.id_to_lang = data['id_to_lang']
        vocab.next_id = data['next_id']
        return vocab