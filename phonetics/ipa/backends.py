#!/usr/bin/env python3
"""
G2P backends, lazily loaded, one process each.

THE GENERATION-LENGTH BUG THIS FIXES
------------------------------------
The existing CharsiuG2P wrapper calls `model.generate(**inputs)` with no length
argument. HuggingFace defaults max_length to 20 TOKENS and the tokenizer is
ByT5 -- byte level. IPA is heavily multi-byte, so 20 tokens is ~13-15 IPA
characters no matter how long the input is.

Measured on the live corpus (6 Sep 2026, 60 real toponyms per route, same model
and inputs, default generate() vs max_new_tokens=128):

    route         truncated
    yue+CJK          80.0%
    ko+HANGUL        71.7%
    ja+CJK           33.3%
    zh+CJK           16.7%

e.g. yue 'PungWuLitTou' came back as pʰa:ŋ˨˩wu:˨˩li -- two syllables missing.
A truncated IPA string is indistinguishable from a short one downstream, so
this never surfaced as an error. 2,026,765 corpus rows sit on these routes
today, plus 465,177 more once ja+CJK is routed at all.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

# Generous ceiling: the longest IPA seen in probing was ~30 chars / ~60 bytes.
CHARSIU_MAX_NEW_TOKENS = 256
CHARSIU_MODEL = "charsiu/g2p_multilingual_byT5_small_100"
CHARSIU_TOKENIZER = "google/byt5-small"


class EpitranBackend:
    """One Epitran mode per instance; instantiation is the expensive part, so
    work is sharded by mode and each shard loads exactly one."""

    def __init__(self, mode: str):
        import epitran
        self.mode = mode
        self.epi = epitran.Epitran(mode)

    def transliterate(self, text: str) -> str:
        return self.epi.transliterate(text)


class CharsiuBackend:
    def __init__(self, device: Optional[str] = None,
                 max_new_tokens: int = CHARSIU_MAX_NEW_TOKENS):
        import torch
        import transformers
        self.torch = torch
        self.max_new_tokens = max_new_tokens
        self.model = transformers.T5ForConditionalGeneration.from_pretrained(
            CHARSIU_MODEL)
        self.tokenizer = transformers.ByT5Tokenizer.from_pretrained(
            CHARSIU_TOKENIZER)
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.model.eval()

    def transliterate_batch(self, texts: Sequence[str], tag: str) -> List[Optional[str]]:
        if not texts:
            return []
        prompts = [f"<{tag}>: {t}" for t in texts]
        enc = self.tokenizer(prompts, return_tensors="pt", padding=True,
                             truncation=True, max_length=256).to(self.device)
        with self.torch.no_grad():
            out = self.model.generate(**enc, max_new_tokens=self.max_new_tokens)
        return [self.tokenizer.decode(o, skip_special_tokens=True) for o in out]

    def transliterate(self, text: str, tag: str) -> Optional[str]:
        return self.transliterate_batch([text], tag)[0]


class PhonikudBackend:
    def __init__(self):
        import phonikud
        self._mod = phonikud

    def transliterate(self, text: str) -> str:
        return self._mod.phonemize(text)
