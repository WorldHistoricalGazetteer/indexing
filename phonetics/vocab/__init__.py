"""
Vocabulary modules for phonetic embeddings.
"""

from phonetics.vocab.char_vocab import (
    CharacterVocabulary,
    ScriptVocabulary,
    LanguageVocabulary,
    PAD_ID,
    UNK_ID,
    SPACE_ID,
)

__all__ = [
    'CharacterVocabulary',
    'ScriptVocabulary',
    'LanguageVocabulary',
    'PAD_ID',
    'UNK_ID',
    'SPACE_ID',
]