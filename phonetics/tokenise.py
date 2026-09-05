# phonetics/tokenise.py
"""
The canonical Symphonym tokeniser — one implementation, four call sites.

Why this module exists
----------------------
The `toponyms` index was embedded through
:meth:`phonetics.vocab.char_vocab.CharacterVocabulary.encode` (via
``phonetics/inference/encoder.py`` → ``phonetics/inference/update_es.py``),
which romanises CJK/Kana, decomposes Hangul to Jamo, NFC-normalises everything
else and maps ``' '`` to ``<SPACE>`` (id 2).

``hf/inference.py::_tokenise`` — the path the **gateway** serves queries from,
and the path ``phonetics/inference/backfill_embeddings.py`` wrote its share of
the index from — did none of that. It fed raw codepoints straight into the
character vocabulary, where ``' '`` resolves to id **12588**, a row the training
tokeniser can never emit.

Measured consequence (`developer/plan-symphonym-v8.md` §2), same weights, both
tokenisations, cosine between them: single-word Latin/Cyrillic/Arabic/Greek
names 1.0000, ``New York`` 0.9691, ``Bury St Edmunds`` 0.9429, ``北京``
**−0.2629**, ``東京`` **−0.3036**. Multi-word toponyms retrieved their own
indexed vector at rank 1 only **65.7%** of the time (n=1,486 real names). The
200 nearest neighbours of anything in this index sit above cosine 0.93
(``gateway/es_helpers.knn_pass_quality``), so a name whose own document sits at
0.90 is outside its own top-200 KNN pool. That is the documented
``Newton with Scales`` symptom.

Scope
-----
This module reproduces what ``CharacterVocabulary.encode`` +
``LanguageVocabulary.encode`` + ``ScriptVocabulary.encode`` do **today, bit for
bit**. It changes no policy: no NFKC, no casefolding, no change to the CJK
romanisation choice, no change to script detection, no change to the
vocabulary. Any of those would alter what a *correct* index contains and so
force a re-embed of all 72.7M documents — they are open decisions, not this
package.

`tests/test_tokeniser_contract.py` holds the equivalence to the vocabulary
classes, and to the vendored copy in ``hf/inference.py``.

Deliberately free of torch and of the model, so every caller and every test can
import it: it maps ``(text, lang)`` to ids, and nothing else.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# The block between the two markers below is DUPLICATED, character for
# character, in `hf/inference.py`. `hf/` ships to HuggingFace and must stay
# self-contained, so it cannot import this module; the duplication is
# deliberate and `tests/test_tokeniser_contract.py` asserts the two copies are
# byte-identical. Edit this copy — the test will tell you about the other one.
#
# Everything in the block depends only on the standard library and anyascii.
# ---------------------------------------------------------------------------

# --- BEGIN CANONICAL TOKENISER ---

import unicodedata
from typing import Dict, List, Optional, Tuple

try:  # anyascii is required only for CJK/Kana romanisation
    from anyascii import anyascii as _anyascii
except ImportError:  # pragma: no cover - exercised only where anyascii is absent
    _anyascii = None

PAD_ID = 0
UNK_ID = 1
SPACE_ID = 2
LANG_UNK_ID = 0

# Unicode blocks per script, in the SAME ORDER as
# `phonetics.utils.script_detection.SCRIPT_RANGES`. The order is load-bearing:
# the ranges overlap (Armenian ligatures sit inside the Hebrew presentation
# block) and the later entry wins, so a reordering silently reclassifies
# characters. The contract test compares the resulting codepoint map with the
# one script_detection builds, entry for entry.
_SCRIPT_UNICODE_RANGES: List[Tuple[str, List[Tuple[int, int]]]] = [
    ("LATIN", [(0x0000, 0x007F), (0x0080, 0x00FF), (0x0100, 0x017F),
               (0x0180, 0x024F), (0x0250, 0x02AF), (0x1D00, 0x1D7F),
               (0x1E00, 0x1EFF), (0x2C60, 0x2C7F), (0xA720, 0xA7FF),
               (0xAB30, 0xAB6F)]),
    ("CYRILLIC", [(0x0400, 0x04FF), (0x0500, 0x052F), (0x2DE0, 0x2DFF),
                  (0xA640, 0xA69F)]),
    ("GREEK", [(0x0370, 0x03FF), (0x1F00, 0x1FFF)]),
    ("ARABIC", [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
                (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)]),
    ("HEBREW", [(0x0590, 0x05FF), (0xFB00, 0xFB4F)]),
    ("DEVANAGARI", [(0x0900, 0x097F), (0xA8E0, 0xA8FF)]),
    ("BENGALI", [(0x0980, 0x09FF)]),
    ("TAMIL", [(0x0B80, 0x0BFF)]),
    ("TELUGU", [(0x0C00, 0x0C7F)]),
    ("MALAYALAM", [(0x0D00, 0x0D7F)]),
    ("KANNADA", [(0x0C80, 0x0CFF)]),
    ("GUJARATI", [(0x0A80, 0x0AFF)]),
    ("THAI", [(0x0E00, 0x0E7F)]),
    ("GEORGIAN", [(0x10A0, 0x10FF), (0x2D00, 0x2D2F)]),
    ("ARMENIAN", [(0x0530, 0x058F), (0xFB00, 0xFB17)]),
    ("HANGUL", [(0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F),
                (0xA960, 0xA97F), (0xD7B0, 0xD7FF)]),
    ("CJK", [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF),
             (0x2A700, 0x2B73F), (0x2B740, 0x2B81F), (0x2B820, 0x2CEAF),
             (0x2CEB0, 0x2EBEF), (0x30000, 0x3134F), (0x3000, 0x303F),
             (0xF900, 0xFAFF)]),
    ("HIRAGANA", [(0x3040, 0x309F), (0x1B000, 0x1B0FF)]),
    ("KATAKANA", [(0x30A0, 0x30FF), (0x31F0, 0x31FF), (0xFF65, 0xFF9F)]),
]

SCRIPT_OTHER = "OTHER"

# Romanised via anyascii; decomposed to Jamo. Both lists are what the index was
# built with — changing either invalidates 72.7M stored vectors.
_ROMANISE_SCRIPTS = frozenset({"CJK", "HIRAGANA", "KATAKANA"})
_DECOMPOSE_SCRIPTS = frozenset({"HANGUL"})


def _build_codepoint_map() -> Dict[int, str]:
    codepoint_map: Dict[int, str] = {}
    for script, ranges in _SCRIPT_UNICODE_RANGES:
        for start, end in ranges:
            for cp in range(start, end + 1):
                codepoint_map[cp] = script
    return codepoint_map


_CODEPOINT_MAP = _build_codepoint_map()

# --- Hangul → Jamo -------------------------------------------------------
_HANGUL_START = 0xAC00
_HANGUL_END = 0xD7A3
_N_VOWEL = 21
_N_TAIL = 28
_CHOSEONG = ['ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ', 'ㅂ', 'ㅃ', 'ㅅ',
             'ㅆ', 'ㅇ', 'ㅈ', 'ㅉ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ']
_JUNGSEONG = ['ㅏ', 'ㅐ', 'ㅑ', 'ㅒ', 'ㅓ', 'ㅔ', 'ㅕ', 'ㅖ', 'ㅗ', 'ㅘ',
              'ㅙ', 'ㅚ', 'ㅛ', 'ㅜ', 'ㅝ', 'ㅞ', 'ㅟ', 'ㅠ', 'ㅡ', 'ㅢ', 'ㅣ']
_JONGSEONG = ['', 'ㄱ', 'ㄲ', 'ㄳ', 'ㄴ', 'ㄵ', 'ㄶ', 'ㄷ', 'ㄹ', 'ㄺ',
              'ㄻ', 'ㄼ', 'ㄽ', 'ㄾ', 'ㄿ', 'ㅀ', 'ㅁ', 'ㅂ', 'ㅄ', 'ㅅ',
              'ㅆ', 'ㅇ', 'ㅈ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ']


def decompose_hangul(text: str) -> str:
    """Hangul syllables → Jamo; every other character passes through."""
    out: List[str] = []
    for char in text:
        code = ord(char)
        if _HANGUL_START <= code <= _HANGUL_END:
            code -= _HANGUL_START
            out.append(_CHOSEONG[code // (_N_VOWEL * _N_TAIL)])
            out.append(_JUNGSEONG[(code % (_N_VOWEL * _N_TAIL)) // _N_TAIL])
            tail = code % _N_TAIL
            if tail:
                out.append(_JONGSEONG[tail])
        else:
            out.append(char)
    return ''.join(out)


def detect_script(text: str) -> str:
    """The dominant script name, counting only alphabetic characters.

    Ties go to the script seen first, which is what `collections.Counter`'s
    `most_common(1)` does in the vocabulary implementation.
    """
    counts: Dict[str, int] = {}
    for char in text:
        if not char.isalpha():
            continue
        script = _CODEPOINT_MAP.get(ord(char), SCRIPT_OTHER)
        counts[script] = counts.get(script, 0) + 1
    if not counts:
        return SCRIPT_OTHER
    return max(counts.items(), key=lambda kv: kv[1])[0]


def preprocess_text(text: str, script: Optional[str] = None) -> str:
    """Romanise CJK/Kana, decompose Hangul, NFC-normalise everything else."""
    if script is None:
        script = detect_script(text)

    if script in _ROMANISE_SCRIPTS:
        if _anyascii is None:
            raise RuntimeError("anyascii required for CJK romanization")
        return _anyascii(text).lower()

    if script in _DECOMPOSE_SCRIPTS:
        return decompose_hangul(text)

    return unicodedata.normalize('NFC', text)


def encode_chars(
    text: str,
    char_to_id: Dict[str, int],
    script: Optional[str] = None,
    max_length: Optional[int] = None,
) -> List[int]:
    """Preprocessed text → character ids, with growth disabled.

    ``' '`` becomes ``<SPACE>`` (2) and never a dictionary lookup — the vocab
    file happens to carry a literal space at id 12588, which is the row the
    gateway was emitting and the training tokeniser can never produce. Other
    whitespace is dropped. An id at or beyond the vocabulary's own size is
    vocabulary corruption and degrades to ``<UNK>`` rather than indexing off
    the end of the embedding table.

    THE ONE DELIBERATE DEVIATION from ``CharacterVocabulary.encode``: an empty
    result becomes ``[<UNK>]`` rather than ``[]``. A zero-length sequence is
    not merely useless, it is fatal — ``pack_padded_sequence`` raises
    ``Cannot pack empty tensors``, and in a batch **one** such item takes the
    whole batch down with ``Length of all samples has to be greater than 0``.
    That already happens today for an empty query (a 500 from ``/api/embed``);
    dropping non-space whitespace widens the class from ``""`` alone to any
    whitespace-only input, so the guard ships with the change that widens it.
    ``<UNK>`` and not ``<SPACE>``: it says "input I cannot represent", which is
    true, where ``<SPACE>`` would assert the input was a space.

    It cannot affect a name that produces any id at all, so it cannot alter a
    single-word Latin name and cannot force a re-embed. Nor can it disagree
    with the index: ``update_es.py`` filters ``name IS NOT NULL AND TRIM(name)
    != ''``, so no indexed document is whitespace-only.
    """
    vocab_size = len(char_to_id)
    ids: List[int] = []
    for char in preprocess_text(text, script):
        if char == ' ':
            ids.append(SPACE_ID)
        elif not char.strip():
            continue
        else:
            cid = char_to_id.get(char)
            if cid is None or cid >= vocab_size:
                cid = UNK_ID
            ids.append(cid)

    if max_length is not None and len(ids) > max_length:
        ids = ids[:max_length]
    if not ids:
        return [UNK_ID]
    return ids


def encode_lang(lang: Optional[str], lang_to_id: Dict[str, int]) -> int:
    """Language tag → id. Lowercased and stripped before lookup."""
    if lang is None or lang == '':
        return LANG_UNK_ID
    return lang_to_id.get(lang.lower().strip(), LANG_UNK_ID)


def encode_script(script: str, script_to_id: Dict[str, int]) -> int:
    """Script name → id, falling back to OTHER and then to 0."""
    if script in script_to_id:
        return script_to_id[script]
    return script_to_id.get(SCRIPT_OTHER, 0)


def tokenise(
    text: str,
    lang: Optional[str],
    char_to_id: Dict[str, int],
    lang_to_id: Dict[str, int],
    script_to_id: Dict[str, int],
    script: Optional[str] = None,
    max_length: Optional[int] = None,
) -> Tuple[List[int], int, int]:
    """``(text, lang)`` → ``(char_ids, script_id, lang_id)``.

    The single entry point every caller should use. No torch, no model.
    """
    if script is None:
        script = detect_script(text)
    char_ids = encode_chars(text, char_to_id, script, max_length)
    return char_ids, encode_script(script, script_to_id), encode_lang(lang, lang_to_id)

# --- END CANONICAL TOKENISER ---
