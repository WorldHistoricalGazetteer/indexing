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
# Tokeniser helpers
# ---------------------------------------------------------------------------

# Unicode script ranges used during training (deterministic detection)
_SCRIPT_RANGES = [
    ("LATIN",      [(0x0041, 0x007A), (0x00C0, 0x024F), (0x1E00, 0x1EFF)]),
    ("CYRILLIC",   [(0x0400, 0x04FF), (0x0500, 0x052F)]),
    ("ARABIC",     [(0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)]),
    ("CJK",        [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF), (0xF900, 0xFAFF)]),
    ("HANGUL",     [(0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)]),
    ("HIRAGANA",   [(0x3041, 0x3096)]),
    ("KATAKANA",   [(0x30A1, 0x30FA), (0x31F0, 0x31FF)]),
    ("DEVANAGARI", [(0x0900, 0x097F)]),
    ("BENGALI",    [(0x0980, 0x09FF)]),
    ("GUJARATI",   [(0x0A80, 0x0AFF)]),
    ("GURMUKHI",   [(0x0A00, 0x0A7F)]),
    ("TAMIL",      [(0x0B80, 0x0BFF)]),
    ("TELUGU",     [(0x0C00, 0x0C7F)]),
    ("KANNADA",    [(0x0C80, 0x0CFF)]),
    ("MALAYALAM",  [(0x0D00, 0x0D7F)]),
    ("THAI",       [(0x0E00, 0x0E7F)]),
    ("GEORGIAN",   [(0x10A0, 0x10FF)]),
    ("ARMENIAN",   [(0x0530, 0x058F)]),
    ("HEBREW",     [(0x0590, 0x05FF), (0xFB1D, 0xFB4F)]),
    ("GREEK",      [(0x0370, 0x03FF), (0x1F00, 0x1FFF)]),
]

def _detect_script(text: str) -> str:
    """Return the dominant script name for a text string."""
    counts: dict[str, int] = {}
    for ch in text:
        cp = ord(ch)
        for name, ranges in _SCRIPT_RANGES:
            if any(lo <= cp <= hi for lo, hi in ranges):
                counts[name] = counts.get(name, 0) + 1
                break
        else:
            counts["OTHER"] = counts.get("OTHER", 0) + 1
    if not counts:
        return "OTHER"
    return max(counts, key=counts.__getitem__)


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
        unk_char   = self._char_to_id.get("<UNK>", 1)
        unk_lang   = self._lang_to_id.get("<UNK>", 0)
        script_name = _detect_script(text)

        char_ids  = [self._char_to_id.get(ch, unk_char) for ch in text]
        lang_id   = self._lang_to_id.get(lang, unk_lang)
        script_id = self._script_to_id.get(script_name, 0)
        length    = len(char_ids)

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
        unk_char   = self._char_to_id.get("<UNK>", 1)
        unk_lang   = self._lang_to_id.get("<UNK>", 0)

        char_seqs, script_ids, lang_ids, lengths = [], [], [], []
        for text, lang in items:
            script_name = _detect_script(text)
            char_ids    = [self._char_to_id.get(ch, unk_char) for ch in text]
            char_seqs.append(char_ids)
            script_ids.append(self._script_to_id.get(script_name, 0))
            lang_ids.append(self._lang_to_id.get(lang, unk_lang))
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

