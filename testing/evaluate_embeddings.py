#!/usr/bin/env python3
"""
Embedding Similarity Evaluation for Phonetic Model

EDITED VERSION:
- Redesigns noise evaluation to test phonetic neighbourhood stability
- Removes exact original-recovery criterion
- Adds neighbourhood overlap and embedding drift metrics
"""

import argparse
import random
import sys
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Set
from enum import Enum

from anyascii import anyascii
from elasticsearch import Elasticsearch

from phonetics.config import Config
from processing.settings import ES_HOST, IX1_BASE

EPITRAN_LANGUAGE_CODES = set(Config.EPITRAN_LANGS.keys())


# =============================================================================
# UTILS
# =============================================================================

def cosine_distance(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - dot / (na * nb)


# =============================================================================
# NOISE GENERATION (single-edit only)
# =============================================================================

class NoiseType(Enum):
    VOWEL_SHIFT = "vowel"
    CONSONANT_SHIFT = "consonant"
    TRANSPOSITION = "transposition"
    DELETION = "deletion"
    DUPLICATION = "duplication"


PHONETIC_SUBSTITUTIONS = {
    'latin': {
        'vowels': {'a': ['e'], 'e': ['i'], 'i': ['e'], 'o': ['u'], 'u': ['o']},
        'consonants': {'b': ['p'], 'd': ['t'], 'g': ['k'], 'v': ['f'], 'z': ['s']}
    }
}


def apply_noise(text: str, script: str, seed: int = None) -> Tuple[str, str]:
    if seed is not None:
        random.seed(seed)

    if not text or len(text) < 2:
        return text, "none"

    chars = list(text)
    pos = random.randint(0, len(chars) - 1)
    char = chars[pos].lower()

    subs = PHONETIC_SUBSTITUTIONS.get(script, PHONETIC_SUBSTITUTIONS['latin'])

    noise_type = random.choice(list(NoiseType))

    if noise_type == NoiseType.VOWEL_SHIFT and char in subs['vowels']:
        chars[pos] = subs['vowels'][char][0]
        return ''.join(chars), "vowel"

    if noise_type == NoiseType.CONSONANT_SHIFT and char in subs['consonants']:
        chars[pos] = subs['consonants'][char][0]
        return ''.join(chars), "consonant"

    if noise_type == NoiseType.TRANSPOSITION and pos < len(chars) - 1:
        chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]
        return ''.join(chars), "transposition"

    if noise_type == NoiseType.DELETION and len(chars) > 3:
        chars.pop(pos)
        return ''.join(chars), "deletion"

    if noise_type == NoiseType.DUPLICATION:
        chars.insert(pos, chars[pos])
        return ''.join(chars), "duplication"

    return text, "none"


# =============================================================================
# SCRIPT DETECTION (unchanged)
# =============================================================================

SCRIPT_RANGES = {
    'latin': [(0x0000, 0x024F)],
    'cyrillic': [(0x0400, 0x04FF)],
    'greek': [(0x0370, 0x03FF)],
    'arabic': [(0x0600, 0x06FF)]
}


def detect_script(text: str) -> str:
    counts = {}
    for ch in text:
        if not ch.isalpha():
            continue
        code = ord(ch)
        for s, ranges in SCRIPT_RANGES.items():
            for a, b in ranges:
                if a <= code <= b:
                    counts[s] = counts.get(s, 0) + 1
    return max(counts, key=counts.get) if counts else 'latin'


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class Toponym:
    doc_id: str
    name: str
    lang: str
    embedding: List[float]
    script: str = field(default='')

    def __post_init__(self):
        if not self.script:
            self.script = detect_script(self.name)


@dataclass
class Neighbour:
    rank: int
    score: float
    name: str
    lang: str


# =============================================================================
# ES QUERIES
# =============================================================================

def find_neighbours(es, index, embedding, k):
    query = {
        "size": k,
        "knn": {
            "field": "embedding",
            "query_vector": embedding,
            "k": k,
            "num_candidates": 200
        },
        "_source": ["name", "lang"]
    }

    resp = es.search(index=index, body=query)
    out = []
    for i, hit in enumerate(resp['hits']['hits'], start=1):
        s = hit['_source']
        out.append(Neighbour(i, hit['_score'], s['name'], s['lang']))
    return out


# =============================================================================
# NOISE EVALUATION
# =============================================================================

def run_noise_evaluation(es_host, index, n_samples, k, output, model_path, seed=None):
    if not es_host.startswith("http"):
        es_host = "http://" + es_host

    es = Elasticsearch([es_host])
    es.indices.refresh(index=index)

    import torch
    from pathlib import Path
    from phonetics.models import HybridPhoneticModel, PhoneticEncoder, CharEncoder
    from phonetics.vocab import CharVocab, LangVocab

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    ckpt = torch.load(model_path, map_location=device)
    base = Path(model_path).stem
    vocab_dir = Path(model_path).parent

    char_vocab = CharVocab.load(vocab_dir / f"{base}_char_vocab.pkl")
    lang_vocab = LangVocab.load(vocab_dir / f"{base}_lang_vocab.pkl")

    model = HybridPhoneticModel(PhoneticEncoder(),
                               CharEncoder(ckpt['char_vocab_size'], ckpt['num_langs']))
    model.load_state_dict(ckpt['model_state'])
    model.to(device).eval()

    def encode(name, lang):
        name = anyascii(name).lower()
        encoded_chars = char_vocab.encode(name)

        # Safety check: Ensure we don't pass empty sequences
        if not encoded_chars:
            encoded_chars = [0]

        c = torch.tensor([encoded_chars], dtype=torch.long, device=device)
        l = torch.tensor([lang_vocab.encode(lang)], dtype=torch.long, device=device)

        # Lengths must be on CPU for pack_padded_sequence
        ln = torch.tensor([len(encoded_chars)], dtype=torch.long, device='cpu')

        with torch.no_grad():
            return model.encode_char_only(c, l, ln).squeeze().cpu().tolist()

    results = []

    docs = es.search(index=index, body={
        "size": n_samples,
        "query": {"exists": {"field": "embedding"}},
        "_source": ["name", "lang", "embedding"]
    })['hits']['hits']

    for i, d in enumerate(docs):
        name = d['_source']['name']
        lang = d['_source']['lang']
        orig_emb = encode(name, lang)

        orig_nn = find_neighbours(es, index, orig_emb, k)
        orig_ids = {(n.name, n.lang) for n in orig_nn}

        noisy, noise_type = apply_noise(name, detect_script(name), seed)
        noisy_emb = encode(noisy, lang)
        noisy_nn = find_neighbours(es, index, noisy_emb, k)
        noisy_ids = {(n.name, n.lang) for n in noisy_nn}

        overlap = len(orig_ids & noisy_ids) / k
        drift = cosine_distance(orig_emb, noisy_emb)

        results.append((noise_type, overlap, drift))

    by_noise = defaultdict(list)
    for n, o, d in results:
        by_noise[n].append((o, d))

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\caption{Neighbourhood stability under single-edit noise}",
        r"\begin{tabular}{lrr}",
        r"\toprule",
        r"Noise type & Overlap@k & Mean drift \\\\",
        r"\midrule"
    ]

    for n, vals in by_noise.items():
        ao = sum(v[0] for v in vals) / len(vals)
        ad = sum(v[1] for v in vals) / len(vals)
        lines.append(f"{n} & {ao:.2f} & {ad:.3f} \\\\")

    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]

    Path(output).write_text("\n".join(lines), encoding="utf-8")


# =============================================================================
# MAIN
# =============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--es-host", default=ES_HOST)
    ap.add_argument("--index", default="toponyms")
    ap.add_argument("-n", "--samples", type=int, default=50)
    ap.add_argument("-k", "--neighbours", type=int, default=10)
    ap.add_argument("-o", "--output", default="noise-evaluation.tex")
    ap.add_argument("--model-path", required=True)
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()

    run_noise_evaluation(
        args.es_host, args.index, args.samples,
        args.neighbours, args.output,
        args.model_path, args.seed
    )


if __name__ == "__main__":
    main()