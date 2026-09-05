"""
Symphonym v7 — Standalone Inference
====================================
Loads the Student (UniversalEncoder) model and computes phonetic embeddings
for toponyms from any script.  No G2P or IPA transcription required at
inference time.

Usage
-----
    from inference import SymphonymModel

    model = SymphonymModel()                        # loads from this directory
    emb   = model.embed("London", lang="en")        # (128,) numpy array
    sim   = model.similarity("London", "en",
                             "Лондон", "ru")        # cosine similarity
    pairs = model.batch_embed([
        ("London", "en"),
        ("Лондон", "ru"),
        ("伦敦",   "zh"),
    ])
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import List, Optional, Tuple, Union

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

# ---------------------------------------------------------------------------
# Minimal architecture (copy of UniversalEncoder from models/models.py)
# Keep in sync with the training code if re-training.
# ---------------------------------------------------------------------------

class SelfAttention(nn.Module):
    def __init__(self, hidden_dim: int, num_heads: int = 2, dropout: float = 0.1):
        super().__init__()
        assert hidden_dim % num_heads == 0
        self.num_heads = num_heads
        self.head_dim  = hidden_dim // num_heads
        self.scale     = math.sqrt(self.head_dim)
        self.q_proj  = nn.Linear(hidden_dim, hidden_dim)
        self.k_proj  = nn.Linear(hidden_dim, hidden_dim)
        self.v_proj  = nn.Linear(hidden_dim, hidden_dim)
        self.out_proj = nn.Linear(hidden_dim, hidden_dim)
        self.dropout  = nn.Dropout(dropout)

    def forward(self, x, mask=None):
        B, L, H = x.shape
        def reshape(t):
            return t.view(B, L, self.num_heads, self.head_dim).transpose(1, 2)
        Q, K, V = reshape(self.q_proj(x)), reshape(self.k_proj(x)), reshape(self.v_proj(x))
        scores = torch.matmul(Q, K.transpose(-2, -1)) / self.scale
        if mask is not None:
            scores = scores.masked_fill(~mask[:, None, None, :], float("-inf"))
        w = self.dropout(F.softmax(scores, dim=-1))
        out = torch.matmul(w, V).transpose(1, 2).contiguous().view(B, L, H)
        return self.out_proj(out), w


class AttentionPooling(nn.Module):
    def __init__(self, hidden_dim: int, dropout: float = 0.1):
        super().__init__()
        self.attention = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.Tanh(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim // 2, 1),
        )

    def forward(self, x, mask=None):
        scores = self.attention(x).squeeze(-1)
        if mask is not None:
            scores = scores.masked_fill(~mask, float("-inf"))
        w = F.softmax(scores, dim=-1)
        return torch.bmm(w.unsqueeze(1), x).squeeze(1), w


class UniversalEncoder(nn.Module):
    """Symphonym Student: script-/language-conditioned character encoder."""

    def __init__(
        self,
        vocab_size: int        = 113280,
        num_scripts: int       = 20,
        num_langs: int         = 1944,
        char_embed_dim: int    = 64,
        script_embed_dim: int  = 16,
        lang_embed_dim: int    = 16,
        hidden_dim: int        = 128,
        embed_dim: int         = 128,
        num_layers: int        = 2,
        num_attention_heads: int = 2,
        dropout: float         = 0.2,
        lang_dropout: float    = 0.5,
        num_length_buckets: int = 16,
        length_embed_dim: int  = 8,
    ):
        super().__init__()
        self.embed_dim          = embed_dim
        self.lang_dropout_rate  = lang_dropout
        self.num_length_buckets = num_length_buckets

        self.char_embed   = nn.Embedding(vocab_size,   char_embed_dim,   padding_idx=0)
        self.script_embed = nn.Embedding(num_scripts,  script_embed_dim)
        self.lang_embed   = nn.Embedding(num_langs,    lang_embed_dim,   padding_idx=0)
        self.length_embed = nn.Embedding(num_length_buckets, length_embed_dim)

        input_dim = char_embed_dim + script_embed_dim + lang_embed_dim + length_embed_dim
        self.input_proj = nn.Linear(input_dim, hidden_dim)
        self.input_norm = nn.LayerNorm(hidden_dim)

        self.bilstm = nn.LSTM(
            hidden_dim, hidden_dim, num_layers=num_layers,
            batch_first=True, bidirectional=True,
            dropout=dropout if num_layers > 1 else 0,
        )
        self.self_attention = SelfAttention(hidden_dim * 2, num_attention_heads, dropout)
        self.pooling        = AttentionPooling(hidden_dim * 2, dropout)
        self.output_proj    = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, embed_dim),
            nn.LayerNorm(embed_dim),
        )

    def _length_bucket(self, lengths: torch.Tensor) -> torch.Tensor:
        buckets = (lengths.to(torch.long) - 1) // 2
        return buckets.clamp(0, self.num_length_buckets - 1)

    def forward(self, char_ids, script_ids, lang_ids, lengths):
        B, L    = char_ids.shape
        device  = char_ids.device
        mask    = torch.arange(L, device=device).unsqueeze(0) < lengths.to(device).unsqueeze(1)

        c_emb = self.char_embed(char_ids)
        s_emb = self.script_embed(script_ids).unsqueeze(1).expand(-1, L, -1)
        l_emb = self.lang_embed(lang_ids).unsqueeze(1).expand(-1, L, -1)
        lb    = self._length_bucket(lengths)
        len_emb = self.length_embed(lb.to(device)).unsqueeze(1).expand(-1, L, -1)

        x = torch.cat([c_emb, s_emb, l_emb, len_emb], dim=-1)
        x = self.input_norm(self.input_proj(x))

        packed   = nn.utils.rnn.pack_padded_sequence(x, lengths.cpu(), batch_first=True, enforce_sorted=False)
        lstm_out, _ = self.bilstm(packed)
        lstm_out, _ = nn.utils.rnn.pad_packed_sequence(lstm_out, batch_first=True, total_length=L)

        attended, _ = self.self_attention(lstm_out, mask)
        attended    = attended + lstm_out
        pooled,   _ = self.pooling(attended, mask)
        emb         = self.output_proj(pooled)
        return F.normalize(emb, p=2, dim=-1)


# ---------------------------------------------------------------------------
# Tokeniser — THE CANONICAL IMPLEMENTATION, vendored from phonetics/tokenise.py
# ---------------------------------------------------------------------------
#
# This file ships to HuggingFace and must stay importable on its own, so it
# cannot import from the repo. The block between the two markers below is
# therefore a character-for-character copy of the block of the same name in
# `phonetics/tokenise.py`, and `tests/test_tokeniser_contract.py` asserts the
# two are byte-identical. Change the repo copy, then re-vendor — do not edit
# one side alone.
#
# What it fixes: until 5 September 2026 this file tokenised raw codepoints —
# no CJK/Kana romanisation, no Hangul→Jamo, no NFC, and `' '` resolving to
# vocab id 12588 instead of `<SPACE>` (2). The `toponyms` index was embedded
# through `CharacterVocabulary.encode`, which does all four, so the gateway was
# querying the index in a different tokenisation from the one it was written
# in: cos(indexed, queried) 0.9691 for "New York", 0.9429 for
# "Bury St Edmunds", and -0.3036 for "東京". Multi-word names retrieved their
# own document at rank 1 only 65.7% of the time.
# See `developer/plan-symphonym-v8.md` §2.

# --- BEGIN CANONICAL TOKENISER ---
# CANONICAL-BLOCK v1 sha256=9a879b4cc312902c2447baf6671fca9886fe5ba2d2584cc6ce16bc054405a47f
# CANONICAL-BLOCK Convention, stated because the same block has been hashed two different
# CANONICAL-BLOCK ways elsewhere and nothing ever compared them: sha256 over this block
# CANONICAL-BLOCK INCLUDING both marker lines and EXCLUDING every line beginning
# CANONICAL-BLOCK "# CANONICAL-BLOCK". Recompute with that rule or you will get a different
# CANONICAL-BLOCK answer and no error.
# CANONICAL-BLOCK What this DOES: identifies which version of the block a copy carries, for
# CANONICAL-BLOCK a consumer who does not have the indexing repo on disk (whg3 and
# CANONICAL-BLOCK London_Customs_Accounts both vendor this block).
# CANONICAL-BLOCK What it does NOT do: a vendored copy carries the ORIGINAL's stamp, so
# CANONICAL-BLOCK checking your own copy detects local modification, not that upstream has
# CANONICAL-BLOCK moved. For that you must compare this value against upstream's.
# CANONICAL-BLOCK THE CASE THAT DEFEATS THE TRIPWIRE, and it is the LIKELY one: edit the block
# CANONICAL-BLOCK and correctly re-stamp it. The test below is then satisfied, this stamp is
# CANONICAL-BLOCK honest, and a consumer checking only that upstream's stamp matches upstream's
# CANONICAL-BLOCK block learns NOTHING. A consumer needs a SECOND witness -- the stamp value it
# CANONICAL-BLOCK was ported from, recorded as a constant on its side -- and only that one
# CANONICAL-BLOCK catches a legitimate upstream change. whg3 does this (2fe733d0c) and proved
# CANONICAL-BLOCK all three modes fire, including the re-stamped one, which is the mode a
# CANONICAL-BLOCK single-witness check silently passes.

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
    """Script name → id, falling back to OTHER and then to 0.

    The fallback is the point. The pre-fix path did
    ``script_to_id.get(script_name, 0)`` — and 0 is not a sentinel, it is
    **LATIN**. So a script the DETECTOR could name but the VOCABULARY could not
    represent was silently embedded as Latin. That is not hypothetical: the
    old detector knew GURMUKHI, the 20-script vocabulary does not, and twelve
    Punjabi documents in production were embedded as Latin because of it —
    identified by their stored vectors matching a script-id-0 recomputation at
    cosine 1.0000 exactly. Falling back to a genuine OTHER repaired that as a
    side effect of the rewrite; nobody designed it.

    ⚠ The detector's table and this vocabulary must be kept in step, and the two
    directions are NOT equally visible:

      * a script MISSING from the range table is behaviourally detectable — it
        has a vocab id, so its text moves from (say) THAI to OTHER;
      * a script EXTRA in the range table is INVISIBLE — it has no vocab id, so
        the fallback lands on OTHER, which is where it already was.

    So a mutation test cannot find an extra script, and neither can a
    differential corpus generated from the implementer's own table: it would
    never generate the cases. Only comparing the two tables directly sees both
    directions. (Measured: mutating GURMUKHI into the range table produces 0 of
    6,271 differences.)
    """
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


# ---------------------------------------------------------------------------
# Main model class
# ---------------------------------------------------------------------------

class SymphonymModel:
    """
    High-level wrapper for Symphonym v7 inference.

    Parameters
    ----------
    model_dir : str or Path, optional
        Directory containing ``model.safetensors`` (or ``final_model.pt``),
        ``vocab/char_vocab.json``, ``vocab/lang_vocab.json``, and
        ``vocab/script_vocab.json``.  Defaults to the directory of this file.
    device : str, optional
        ``"cpu"`` (default) or ``"cuda"``.

    Examples
    --------
    >>> model = SymphonymModel()
    >>> model.similarity("London", "en", "Лондон", "ru")
    0.991
    >>> embeddings = model.batch_embed([("London", "en"), ("Лондон", "ru")])
    >>> embeddings.shape
    (2, 128)
    """

    def __init__(
        self,
        model_dir: Union[str, Path, None] = None,
        device: str = "cpu",
    ):
        if model_dir is None:
            model_dir = Path(__file__).parent
        model_dir = Path(model_dir)

        self.device = torch.device(device)

        # --- Load vocabularies ---
        vocab_dir = model_dir / "vocab"
        with open(vocab_dir / "char_vocab.json") as f:
            cv = json.load(f)
        with open(vocab_dir / "lang_vocab.json") as f:
            lv = json.load(f)
        with open(vocab_dir / "script_vocab.json") as f:
            sv = json.load(f)

        self._char_to_id:   dict[str, int] = cv.get("char_to_id",   cv)
        self._lang_to_id:   dict[str, int] = lv.get("lang_to_id",   lv)
        self._script_to_id: dict[str, int] = sv.get("script_to_id", sv)

        # --- Build model from config ---
        cfg_path = model_dir / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)

        self._model = UniversalEncoder(
            vocab_size          = cfg.get("vocab_size",          len(self._char_to_id) + 1),
            num_scripts         = cfg.get("num_scripts",         25),
            num_langs           = cfg.get("num_langs",           len(self._lang_to_id) + 1),
            char_embed_dim      = cfg.get("char_embed_dim",      64),
            script_embed_dim    = cfg.get("script_embed_dim",    16),
            lang_embed_dim      = cfg.get("lang_embed_dim",      16),
            hidden_dim          = cfg.get("hidden_dim",          128),
            embed_dim           = cfg.get("embed_dim",           128),
            num_layers          = cfg.get("num_layers",          2),
            num_attention_heads = cfg.get("num_attention_heads", 2),
            dropout             = cfg.get("dropout",             0.2),
            lang_dropout        = cfg.get("lang_dropout",        0.5),
            num_length_buckets  = cfg.get("num_length_buckets",  16),
            length_embed_dim    = cfg.get("length_embed_dim",    8),
        )

        # --- Load weights (prefer safetensors, fall back to .pt) ---
        st_path = model_dir / "model.safetensors"
        pt_path = model_dir / "final_model.pt"
        if st_path.exists():
            from safetensors.torch import load_file
            state = load_file(str(st_path), device=str(self.device))
            self._model.load_state_dict(state)
        elif pt_path.exists():
            ckpt = torch.load(str(pt_path), map_location=self.device)
            state = ckpt.get("model_state_dict", ckpt.get("model_state", ckpt))
            self._model.load_state_dict(state)
        else:
            raise FileNotFoundError(
                f"No model weights found in {model_dir}. "
                "Expected model.safetensors or final_model.pt"
            )

        self._model.to(self.device).eval()
        self._sanitize_vocab_ids()

    def _sanitize_vocab_ids(self) -> int:
        """Clamp any vocab id that exceeds its trained embedding table to <UNK>.

        The on-disk vocab files can carry ids beyond the model's char/script/lang
        embedding tables (vocab/model size drift). An out-of-range id is a hard
        failure at lookup time — a CUDA device-side assert that poisons the whole
        context on GPU, or an ``IndexError`` (HTTP 500) on CPU — even though such
        a token *should* simply fall back to <UNK>. The tokeniser maps via
        ``dict.get(key, in_range_default)``, so clamping every dict VALUE into
        range makes those tokens degrade gracefully to <UNK>. Valid entries are
        untouched → embeddings for normal inputs are byte-for-byte identical.
        """
        vsz = self._model.char_embed.num_embeddings
        ssz = self._model.script_embed.num_embeddings
        lsz = self._model.lang_embed.num_embeddings
        unk_char = self._char_to_id.get("<UNK>", 1)
        unk_lang = self._lang_to_id.get("<UNK>", 0)
        unk_char = unk_char if 0 <= unk_char < vsz else 0
        unk_lang = unk_lang if 0 <= unk_lang < lsz else 0
        remapped = 0
        for d, size, default in ((self._char_to_id, vsz, unk_char),
                                 (self._script_to_id, ssz, 0),
                                 (self._lang_to_id, lsz, unk_lang)):
            for k, v in list(d.items()):
                if not (0 <= v < size):
                    d[k] = default
                    remapped += 1
        if remapped:
            import sys
            print(f"[SymphonymModel] sanitised {remapped} out-of-table vocab id(s) "
                  f"→ UNK (char<{vsz}, script<{ssz}, lang<{lsz})", file=sys.stderr)
        return remapped

    # ------------------------------------------------------------------
    # Tokenisation
    # ------------------------------------------------------------------

    def _tokenise(self, text: str, lang: str) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Convert a single (text, lang) pair to model inputs."""
        char_ids, script_id, lang_id = tokenise(
            text, lang, self._char_to_id, self._lang_to_id, self._script_to_id)
        length = len(char_ids)

        return (
            torch.tensor([char_ids],  dtype=torch.long),
            torch.tensor([script_id], dtype=torch.long),
            torch.tensor([lang_id],   dtype=torch.long),
            torch.tensor([length],    dtype=torch.long),
        )

    def _pad_batch(
        self,
        items: List[Tuple[str, str]],
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Tokenise and pad a list of (text, lang) pairs."""
        char_seqs, script_ids, lang_ids, lengths = [], [], [], []
        for text, lang in items:
            char_ids, script_id, lang_id = tokenise(
                text, lang, self._char_to_id, self._lang_to_id, self._script_to_id)
            char_seqs.append(char_ids)
            script_ids.append(script_id)
            lang_ids.append(lang_id)
            lengths.append(len(char_ids))

        max_len = max(lengths)
        padded  = [ids + [0] * (max_len - len(ids)) for ids in char_seqs]

        return (
            torch.tensor(padded,      dtype=torch.long),
            torch.tensor(script_ids,  dtype=torch.long),
            torch.tensor(lang_ids,    dtype=torch.long),
            torch.tensor(lengths,     dtype=torch.long),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @torch.no_grad()
    def embed(self, text: str, lang: str = "und") -> np.ndarray:
        """
        Compute a 128-dimensional L2-normalised phonetic embedding.

        Parameters
        ----------
        text : str
            Toponym in any script.
        lang : str, optional
            ISO 639-1 language code (e.g. ``"en"``, ``"ar"``, ``"zh"``).
            Use ``"und"`` (undetermined) if unknown — the model will fall
            back to script-level generalisation.

        Returns
        -------
        numpy.ndarray of shape (128,)
        """
        char_ids, script_ids, lang_ids, lengths = self._tokenise(text, lang)
        char_ids   = char_ids.to(self.device)
        script_ids = script_ids.to(self.device)
        lang_ids   = lang_ids.to(self.device)
        emb = self._model(char_ids, script_ids, lang_ids, lengths)
        return emb.cpu().numpy()[0]

    @torch.no_grad()
    def batch_embed(self, items: List[Tuple[str, str]]) -> np.ndarray:
        """
        Compute embeddings for a list of (text, lang) pairs.

        Parameters
        ----------
        items : list of (text, lang) tuples

        Returns
        -------
        numpy.ndarray of shape (N, 128)
        """
        char_ids, script_ids, lang_ids, lengths = self._pad_batch(items)
        char_ids   = char_ids.to(self.device)
        script_ids = script_ids.to(self.device)
        lang_ids   = lang_ids.to(self.device)
        emb = self._model(char_ids, script_ids, lang_ids, lengths)
        return emb.cpu().numpy()

    def similarity(
        self,
        text1: str, lang1: str,
        text2: str, lang2: str,
    ) -> float:
        """
        Cosine similarity between two toponyms.

        Returns a float in [-1, 1]; embeddings are L2-normalised so this
        equals the dot product.  Values above 0.75 generally indicate
        phonetically similar names.
        """
        e1 = self.embed(text1, lang1)
        e2 = self.embed(text2, lang2)
        return float(np.dot(e1, e2))


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    model = SymphonymModel()
    pairs = [
        ("London",   "en", "Лондон",          "ru"),
        ("London",   "en", "伦敦",              "zh"),
        ("London",   "en", "لندن",              "ar"),
        ("London",   "en", "Londres",           "fr"),
        ("Tokyo",    "en", "東京",              "ja"),
        ("Beijing",  "en", "北京",              "zh"),
        ("Jerusalem","en", "ירושלים",           "he"),
        ("Baghdad",  "en", "بغداد",             "ar"),
        ("Tbilisi",  "en", "თბილისი",           "ka"),
    ]
    print(f"\n{'Name 1':<14} {'Name 2':<16} {'Lang':<6} {'Sim':>6}")
    print("-" * 46)
    for t1, l1, t2, l2 in pairs:
        sim = model.similarity(t1, l1, t2, l2)
        print(f"{t1:<14} {t2:<16} {l1}→{l2:<3}  {sim:>6.3f}")

