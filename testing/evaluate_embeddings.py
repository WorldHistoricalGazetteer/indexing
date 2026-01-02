#!/usr/bin/env python3
"""
Embedding Similarity Evaluation for Phonetic Model

Samples random toponyms from Elasticsearch and finds their k-nearest neighbours
using cosine similarity on the embedding field. Generates LaTeX tables suitable
for inclusion in academic papers.

Runs two separate test series:
1. Toponyms in languages WITH Epitran support (phonetically grounded embeddings)
2. Toponyms in languages WITHOUT Epitran support (character-only embeddings)

Optional cross-script mode samples toponyms from specific scripts and highlights
neighbours from different scripts, demonstrating cross-lingual phonetic matching.

Optional noise mode introduces realistic perturbations (phonetic drift, typos)
to test robustness of the embedding space.

Usage:
    python -m testing.evaluate_embeddings \
        --es-host localhost:9200 \
        --index toponyms \
        --samples 10 \
        --neighbours 15 \
        --output article/embedding-evaluation.tex

    # Cross-script evaluation
    python -m testing.evaluate_embeddings \
        --cross-script \
        --samples 5 \
        --neighbours 15 \
        --output article/cross-script-evaluation.tex

    # Noise robustness evaluation
    python -m testing.evaluate_embeddings \
        --noise \
        --noise-level 0.3 \
        --samples 10 \
        --neighbours 15 \
        --output article/noise-evaluation.tex
"""

import argparse
import random
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Set
from enum import Enum

from anyascii import anyascii
from elasticsearch import Elasticsearch

from phonetics.config import Config
from processing.settings import ES_HOST, IX1_BASE

# Build set of supported language codes from config
EPITRAN_LANGUAGE_CODES = set(Config.EPITRAN_LANGS.keys())


# =============================================================================
# NOISE GENERATION
# =============================================================================

class NoiseType(Enum):
    """Types of noise that can be applied."""
    VOWEL_SHIFT = "vowel_shift"
    CONSONANT_SHIFT = "consonant_shift"
    TRANSPOSITION = "transposition"
    DELETION = "deletion"
    DUPLICATION = "duplication"
    INSERTION = "insertion"


# Phonetically similar character substitutions by script
PHONETIC_SUBSTITUTIONS = {
    'latin': {
        # Vowel confusions (common in historical texts and transliterations)
        'vowels': {
            'a': ['e', 'o', 'á', 'à', 'ä', 'â'],
            'e': ['a', 'i', 'é', 'è', 'ë', 'ê'],
            'i': ['e', 'y', 'í', 'ì', 'ï', 'î'],
            'o': ['a', 'u', 'ó', 'ò', 'ö', 'ô'],
            'u': ['o', 'ü', 'ú', 'ù', 'û'],
            'y': ['i', 'j'],
        },
        # Consonant confusions (voicing, place of articulation)
        'consonants': {
            'b': ['p', 'v'],
            'p': ['b', 'f'],
            'c': ['k', 's', 'z'],
            'k': ['c', 'q', 'g'],
            'g': ['k', 'j'],
            'd': ['t', 'th'],
            't': ['d', 'th'],
            'f': ['v', 'ph'],
            'v': ['f', 'b', 'w'],
            's': ['z', 'c', 'ss'],
            'z': ['s', 'ts'],
            'm': ['n'],
            'n': ['m', 'nn'],
            'l': ['r', 'll'],
            'r': ['l', 'rr'],
            'j': ['g', 'y', 'i'],
            'w': ['v', 'u'],
            'x': ['ks', 'z'],
            'q': ['k', 'c'],
            'h': [''],  # H-dropping
            'ph': ['f'],
            'th': ['t', 'd'],
            'ch': ['k', 'sh', 'tch'],
            'sh': ['s', 'ch'],
            'ck': ['k', 'c'],
        }
    },
    'cyrillic': {
        'vowels': {
            'а': ['о', 'я'],
            'е': ['и', 'э', 'ё'],
            'и': ['е', 'ы', 'й'],
            'о': ['а', 'у'],
            'у': ['о', 'ю'],
            'ы': ['и'],
            'э': ['е'],
            'ю': ['у'],
            'я': ['а'],
            'ё': ['е', 'о'],
        },
        'consonants': {
            'б': ['п', 'в'],
            'п': ['б', 'ф'],
            'в': ['ф', 'б'],
            'ф': ['в', 'п'],
            'г': ['к', 'х'],
            'к': ['г', 'х'],
            'д': ['т'],
            'т': ['д'],
            'з': ['с', 'ж'],
            'с': ['з', 'ц'],
            'ж': ['ш', 'з'],
            'ш': ['ж', 'щ'],
            'щ': ['ш'],
            'ц': ['с', 'ч'],
            'ч': ['ц', 'щ'],
            'м': ['н'],
            'н': ['м'],
            'л': ['р'],
            'р': ['л'],
            'х': ['г', 'к'],
        }
    },
    'greek': {
        'vowels': {
            'α': ['ε', 'ο'],
            'ε': ['α', 'η', 'ι'],
            'η': ['ε', 'ι'],
            'ι': ['η', 'υ', 'ει', 'οι'],
            'ο': ['α', 'ω'],
            'υ': ['ι', 'οι'],
            'ω': ['ο'],
        },
        'consonants': {
            'β': ['π', 'φ'],
            'π': ['β', 'φ'],
            'φ': ['π', 'β'],
            'γ': ['κ', 'χ'],
            'κ': ['γ', 'χ'],
            'χ': ['κ', 'γ'],
            'δ': ['τ', 'θ'],
            'τ': ['δ', 'θ'],
            'θ': ['τ', 'δ'],
            'ζ': ['σ'],
            'σ': ['ζ', 'ς'],
            'ς': ['σ'],
            'μ': ['ν'],
            'ν': ['μ'],
            'λ': ['ρ'],
            'ρ': ['λ'],
        }
    },
    'arabic': {
        'vowels': {
            'ا': ['و', 'ي'],
            'و': ['ا', 'ي'],
            'ي': ['ا', 'و'],
        },
        'consonants': {
            'ب': ['پ', 'ت'],
            'ت': ['ط', 'ث'],
            'ث': ['ت', 'س'],
            'ج': ['چ', 'ح'],
            'ح': ['خ', 'ج'],
            'خ': ['ح', 'غ'],
            'د': ['ذ', 'ض'],
            'ذ': ['د', 'ظ'],
            'ر': ['ز'],
            'ز': ['ر', 'س'],
            'س': ['ص', 'ث'],
            'ش': ['س'],
            'ص': ['س', 'ض'],
            'ض': ['ص', 'ظ'],
            'ط': ['ت', 'ظ'],
            'ظ': ['ط', 'ذ'],
            'ع': ['غ', 'ء'],
            'غ': ['ع', 'خ'],
            'ف': ['ق'],
            'ق': ['ف', 'ك'],
            'ك': ['ق', 'گ'],
            'ل': ['ن'],
            'م': ['ن'],
            'ن': ['م', 'ل'],
            'ه': ['ح'],
        }
    },
    'hebrew': {
        'vowels': {
            'א': ['ע'],
            'ו': ['י'],
            'י': ['ו'],
        },
        'consonants': {
            'ב': ['פ', 'ו'],
            'ג': ['כ'],
            'ד': ['ת'],
            'ה': ['ח', 'א'],
            'ז': ['ס'],
            'ח': ['כ', 'ה'],
            'ט': ['ת'],
            'כ': ['ח', 'ק'],
            'ל': ['ר'],
            'מ': ['נ'],
            'נ': ['מ'],
            'ס': ['ש', 'ז'],
            'פ': ['ב'],
            'צ': ['ס'],
            'ק': ['כ'],
            'ר': ['ל'],
            'ש': ['ס'],
            'ת': ['ט', 'ד'],
        }
    },
    'cjk': {
        # CJK is logographic, so we mainly do transposition/deletion
        # Some visually similar characters
        'vowels': {},
        'consonants': {}
    },
    'devanagari': {
        'vowels': {
            'अ': ['आ', 'इ'],
            'आ': ['अ', 'ए'],
            'इ': ['ई', 'उ'],
            'ई': ['इ'],
            'उ': ['ऊ', 'इ'],
            'ऊ': ['उ'],
            'ए': ['ऐ', 'आ'],
            'ऐ': ['ए'],
            'ओ': ['औ'],
            'औ': ['ओ'],
        },
        'consonants': {
            'क': ['ख', 'ग'],
            'ख': ['क', 'घ'],
            'ग': ['घ', 'क'],
            'घ': ['ग', 'ख'],
            'च': ['छ', 'ज'],
            'छ': ['च'],
            'ज': ['झ', 'च'],
            'झ': ['ज'],
            'ट': ['ठ', 'ड'],
            'ठ': ['ट'],
            'ड': ['ढ', 'ट'],
            'ढ': ['ड'],
            'त': ['थ', 'द'],
            'थ': ['त'],
            'द': ['ध', 'त'],
            'ध': ['द'],
            'प': ['फ', 'ब'],
            'फ': ['प'],
            'ब': ['भ', 'प'],
            'भ': ['ब'],
            'म': ['न'],
            'न': ['म', 'ण'],
            'ण': ['न'],
            'र': ['ल'],
            'ल': ['र'],
            'स': ['श', 'ष'],
            'श': ['स', 'ष'],
            'ष': ['श', 'स'],
        }
    },
    'thai': {
        'vowels': {
            'า': ['ะ'],
            'ิ': ['ี'],
            'ี': ['ิ'],
            'ุ': ['ู'],
            'ู': ['ุ'],
            'เ': ['แ'],
            'แ': ['เ'],
            'โ': ['อ'],
        },
        'consonants': {
            'ก': ['ค', 'ข'],
            'ค': ['ก', 'ฆ'],
            'ง': ['น'],
            'จ': ['ช'],
            'ช': ['จ', 'ฌ'],
            'ด': ['ต'],
            'ต': ['ด', 'ถ'],
            'ท': ['ธ', 'ถ'],
            'น': ['ณ', 'ง'],
            'บ': ['ป'],
            'ป': ['บ', 'พ'],
            'พ': ['ป', 'ภ'],
            'ม': ['น'],
            'ร': ['ล'],
            'ล': ['ร'],
            'ส': ['ศ', 'ษ'],
        }
    },
}


def apply_noise(text: str, script: str, noise_level: float = 0.3, seed: int = None) -> Tuple[str, List[str]]:
    """
    Apply realistic noise to a toponym.
    Refactored to handle dynamic list length changes safely.
    """
    if seed is not None:
        random.seed(seed)

    if not text or len(text) < 2:
        return text, []

    chars = list(text)
    applied_noise = []

    # Get substitution maps for this script
    script_subs = PHONETIC_SUBSTITUTIONS.get(script, PHONETIC_SUBSTITUTIONS['latin'])
    vowel_subs = script_subs.get('vowels', {})
    consonant_subs = script_subs.get('consonants', {})

    # Determine number of modifications based on noise level and text length
    max_modifications = max(1, int(len(text) * noise_level))
    num_modifications = random.randint(1, max_modifications)

    modifications_made = 0

    # Safety counter to prevent infinite loops if no noise can be applied
    attempts = 0
    max_attempts = num_modifications * 5

    while modifications_made < num_modifications and attempts < max_attempts:
        attempts += 1

        # Always pick a valid position based on CURRENT length
        if len(chars) < 1:
            break

        pos = random.randint(0, len(chars) - 1)
        char = chars[pos]
        char_lower = char.lower()

        # Choose noise type
        noise_type = random.choice([
            NoiseType.VOWEL_SHIFT,
            NoiseType.CONSONANT_SHIFT,
            NoiseType.TRANSPOSITION,
            NoiseType.DELETION,
            NoiseType.DUPLICATION,
        ])

        if noise_type == NoiseType.VOWEL_SHIFT and char_lower in vowel_subs:
            # Vowel substitution
            replacement = random.choice(vowel_subs[char_lower])
            if char.isupper() and len(replacement) == 1:
                replacement = replacement.upper()
            old_char = chars[pos]
            chars[pos] = replacement
            applied_noise.append(f"vowel '{old_char}'→'{replacement}'")
            modifications_made += 1

        elif noise_type == NoiseType.CONSONANT_SHIFT and char_lower in consonant_subs:
            # Consonant substitution
            replacement = random.choice(consonant_subs[char_lower])
            if char.isupper() and len(replacement) == 1:
                replacement = replacement.upper()
            old_char = chars[pos]
            chars[pos] = replacement
            applied_noise.append(f"consonant '{old_char}'→'{replacement}'")
            modifications_made += 1

        elif noise_type == NoiseType.TRANSPOSITION and pos < len(chars) - 1:
            # Swap adjacent characters (typo simulation)
            if chars[pos].isalpha() and chars[pos + 1].isalpha():
                chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]
                applied_noise.append(f"transposition at {pos}")
                modifications_made += 1

        elif noise_type == NoiseType.DELETION and len(chars) > 3:
            # Delete a character (only if result isn't too short)
            if chars[pos].isalpha():
                deleted = chars.pop(pos)
                applied_noise.append(f"deletion '{deleted}'")
                modifications_made += 1
                # No need to adjust index list, we pick a new random int next loop

        elif noise_type == NoiseType.DUPLICATION:
            # Duplicate a character (common typo)
            if chars[pos].isalpha():
                chars.insert(pos, chars[pos])
                applied_noise.append(f"duplication '{chars[pos]}'")
                modifications_made += 1
                # No need to adjust index list, we pick a new random int next loop

    return ''.join(chars), applied_noise


# =============================================================================
# SCRIPT DETECTION
# =============================================================================

SCRIPT_RANGES = {
    'latin': [(0x0000, 0x024F), (0x1E00, 0x1EFF), (0x2C60, 0x2C7F)],
    'cyrillic': [(0x0400, 0x04FF), (0x0500, 0x052F), (0x2DE0, 0x2DFF)],
    'greek': [(0x0370, 0x03FF), (0x1F00, 0x1FFF)],
    'arabic': [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF)],
    'hebrew': [(0x0590, 0x05FF)],
    'cjk': [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3000, 0x303F)],
    'devanagari': [(0x0900, 0x097F)],
    'thai': [(0x0E00, 0x0E7F)],
    'georgian': [(0x10A0, 0x10FF)],
    'armenian': [(0x0530, 0x058F)],
}


def detect_script(text: str) -> str:
    """Detect the primary script of a text string."""
    if not text:
        return 'unknown'

    script_counts: Dict[str, int] = {}

    for char in text:
        if char.isspace() or not char.isalpha():
            continue

        code = ord(char)

        for script_name, ranges in SCRIPT_RANGES.items():
            for start, end in ranges:
                if start <= code <= end:
                    script_counts[script_name] = script_counts.get(script_name, 0) + 1
                    break

    if not script_counts:
        return 'latin'

    return max(script_counts, key=script_counts.get)


def has_epitran_support(lang: str) -> bool:
    """Check if a language has Epitran support."""
    if not lang:
        return False
    lang_lower = lang.lower()
    if lang_lower in EPITRAN_LANGUAGE_CODES:
        return True
    lang_base = lang_lower.split('-')[0].split('_')[0]
    return lang_base in EPITRAN_LANGUAGE_CODES


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class Toponym:
    """Represents a toponym document from ES."""
    doc_id: str
    name: str
    lang: str
    ipa: Optional[str]
    embedding: List[float]
    script: str = field(default='')
    # For noise mode
    original_name: str = field(default='')
    noise_applied: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.script:
            self.script = detect_script(self.name)
        if not self.original_name:
            self.original_name = self.name

    @property
    def has_ipa(self) -> bool:
        return self.ipa is not None and len(self.ipa) > 0

    @property
    def has_epitran(self) -> bool:
        return has_epitran_support(self.lang)

    @property
    def is_noisy(self) -> bool:
        return len(self.noise_applied) > 0


@dataclass
class Neighbour:
    """A nearest neighbour result."""
    rank: int
    score: float
    name: str
    lang: str
    ipa: Optional[str]
    script: str = field(default='')

    def __post_init__(self):
        if not self.script:
            self.script = detect_script(self.name)

    @property
    def has_ipa(self) -> bool:
        return self.ipa is not None and len(self.ipa) > 0


# =============================================================================
# LATEX FORMATTING
# =============================================================================

def escape_latex(text: str) -> str:
    """Escape special LaTeX characters, romanizing non-Latin scripts."""
    if not text:
        return ""

    # Romanize to avoid font issues with non-Latin scripts
    text = anyascii(text)

    replacements = [
        ('\\', r'\textbackslash{}'),
        ('&', r'\&'),
        ('%', r'\%'),
        ('$', r'\$'),
        ('#', r'\#'),
        ('_', r'\_'),
        ('{', r'\{'),
        ('}', r'\}'),
        ('~', r'\textasciitilde{}'),
        ('^', r'\textasciicircum{}'),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def format_ipa(ipa: Optional[str]) -> str:
    """Format IPA for LaTeX output."""
    if not ipa:
        return r"\textemdash"
    return rf"\textipa{{{escape_latex(ipa)}}}"


# =============================================================================
# ELASTICSEARCH QUERIES
# =============================================================================

def get_random_toponyms_by_epitran(
        es: Elasticsearch,
        index: str,
        n: int,
        with_epitran: bool,
        seed: int = None
) -> List[Toponym]:
    """Sample n random toponyms from ES, filtered by Epitran language support."""
    if seed is not None:
        random.seed(seed)

    if with_epitran:
        lang_filter = {"terms": {"lang": list(EPITRAN_LANGUAGE_CODES)}}
    else:
        lang_filter = {"bool": {"must_not": {"terms": {"lang": list(EPITRAN_LANGUAGE_CODES)}}}}

    query = {
        "size": n,
        "query": {
            "function_score": {
                "query": {
                    "bool": {
                        "must": [{"exists": {"field": "embedding"}}],
                        "filter": lang_filter
                    }
                },
                "random_score": {"seed": seed or random.randint(0, 2 ** 31)},
                "boost_mode": "replace"
            }
        },
        "_source": ["name", "lang", "ipa_cached", "embedding"]
    }

    resp = es.search(index=index, body=query)

    toponyms = []
    for hit in resp['hits']['hits']:
        source = hit['_source']
        toponyms.append(Toponym(
            doc_id=hit['_id'],
            name=source.get('name', ''),
            lang=source.get('lang', 'und'),
            ipa=source.get('ipa_cached'),
            embedding=source.get('embedding', [])
        ))

    return toponyms


def get_toponyms_by_script(
        es: Elasticsearch,
        index: str,
        script: str,
        n: int,
        seed: int = None
) -> List[Toponym]:
    """Sample n random toponyms from ES that are primarily in the specified script."""
    if seed is not None:
        random.seed(seed)

    script_patterns = {
        'cyrillic': '[А-Яа-яЁёҐґЄєІіЇї]',
        'greek': '[Α-Ωα-ωάέήίόύώ]',
        'arabic': '[\u0600-\u06FF]',
        'hebrew': '[\u0590-\u05FF]',
        'cjk': '[\u4E00-\u9FFF]',
        'devanagari': '[\u0900-\u097F]',
        'thai': '[\u0E00-\u0E7F]',
        'georgian': '[\u10A0-\u10FF]',
        'armenian': '[\u0530-\u058F]',
    }

    if script not in script_patterns:
        print(f"Warning: No regex pattern for script '{script}', falling back to random sampling")
        return get_random_toponyms_by_epitran(es, index, n, with_epitran=True, seed=seed)

    pattern = script_patterns[script]

    query = {
        "size": n,
        "query": {
            "function_score": {
                "query": {
                    "bool": {
                        "must": [
                            {"exists": {"field": "embedding"}},
                            {"regexp": {"name.keyword": f".*{pattern}.*"}}
                        ]
                    }
                },
                "random_score": {"seed": seed or random.randint(0, 2 ** 31)},
                "boost_mode": "replace"
            }
        },
        "_source": ["name", "lang", "ipa_cached", "embedding"]
    }

    resp = es.search(index=index, body=query)

    toponyms = []
    for hit in resp['hits']['hits']:
        source = hit['_source']
        toponyms.append(Toponym(
            doc_id=hit['_id'],
            name=source.get('name', ''),
            lang=source.get('lang', 'und'),
            ipa=source.get('ipa_cached'),
            embedding=source.get('embedding', [])
        ))

    return toponyms


def find_neighbours(
        es: Elasticsearch,
        index: str,
        toponym: Toponym,
        k: int = 15
) -> List[Neighbour]:
    """Find k nearest neighbours using ES kNN search."""
    if not toponym.embedding:
        return []

    query = {
        "size": k + 1,
        "knn": {
            "field": "embedding",
            "query_vector": toponym.embedding,
            "k": k + 1,
            "num_candidates": 100
        },
        "_source": ["name", "lang", "ipa_cached"]
    }

    resp = es.search(index=index, body=query)

    neighbours = []
    rank = 0

    for hit in resp['hits']['hits']:
        if hit['_id'] == toponym.doc_id:
            continue

        rank += 1
        if rank > k:
            break

        source = hit['_source']
        neighbours.append(Neighbour(
            rank=rank,
            score=hit['_score'],
            name=source.get('name', ''),
            lang=source.get('lang', 'und'),
            ipa=source.get('ipa_cached')
        ))

    return neighbours


def encode_and_search_noisy(
        es: Elasticsearch,
        index: str,
        noisy_name: str,
        lang: str,
        original_doc_id: str,
        k: int = 15,
        model=None,
        char_vocab=None,
        lang_vocab=None,
        device: str = 'cpu'
) -> Tuple[List[float], List[Neighbour]]:
    """
    Encode a noisy toponym and find its neighbours.

    This requires the trained model to generate embeddings for the perturbed text.
    """
    import torch
    import torch.nn.functional as F

    if model is None:
        raise ValueError("Model required for noise evaluation")

    # Detect language and romanize
    from anyascii import anyascii as aa
    romanized = aa(noisy_name).lower()

    # Encode
    char_ids = torch.tensor([char_vocab.encode(romanized)], dtype=torch.long, device=device)
    lang_ids = torch.tensor([lang_vocab.encode(lang)], dtype=torch.long, device=device)
    lengths = torch.tensor([len(char_vocab.encode(romanized))], dtype=torch.long, device='cpu')

    with torch.no_grad():
        embedding = model.encode_char_only(char_ids, lang_ids, lengths)
        embedding_list = embedding.squeeze().cpu().tolist()

    # Search with the noisy embedding
    query = {
        "size": k + 1,
        "knn": {
            "field": "embedding",
            "query_vector": embedding_list,
            "k": k + 1,
            "num_candidates": 100
        },
        "_source": ["name", "lang", "ipa_cached"]
    }

    resp = es.search(index=index, body=query)

    neighbours = []
    rank = 0

    for hit in resp['hits']['hits']:
        # Don't skip original - we want to see if it's recovered
        rank += 1
        if rank > k:
            break

        source = hit['_source']
        neighbours.append(Neighbour(
            rank=rank,
            score=hit['_score'],
            name=source.get('name', ''),
            lang=source.get('lang', 'und'),
            ipa=source.get('ipa_cached')
        ))

    return embedding_list, neighbours


# =============================================================================
# LATEX TABLE GENERATION
# =============================================================================

def generate_latex_table(
        toponym: Toponym,
        neighbours: List[Neighbour],
        table_id: int,
        series: str
) -> str:
    """Generate a LaTeX table for one toponym and its neighbours."""
    label = f"tab:embed-{series}-{table_id}"
    epitran_status = "Epitran-supported" if toponym.has_epitran else "no Epitran"

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\small",
        rf"\caption{{Nearest neighbours for \textbf{{{escape_latex(toponym.name)}}} [{toponym.lang}] ({epitran_status})}}",
        rf"\label{{{label}}}",
        r"\begin{tabular}{rlllp{4cm}}",
        r"\toprule",
        r"\textbf{Rank} & \textbf{Score} & \textbf{Name} & \textbf{Lang} & \textbf{IPA} \\",
        r"\midrule",
    ]

    query_ipa = format_ipa(toponym.ipa)
    lines.append(
        rf"\rowcolor{{gray!20}} Q & --- & {escape_latex(toponym.name)} & {toponym.lang} & {query_ipa} \\"
    )
    lines.append(r"\midrule")

    for n in neighbours:
        ipa_cell = format_ipa(n.ipa)
        lines.append(
            rf"{n.rank} & {n.score:.4f} & {escape_latex(n.name)} & {n.lang} & {ipa_cell} \\"
        )

    lines.extend([
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ])

    return "\n".join(lines)


def generate_cross_script_table(
        toponym: Toponym,
        neighbours: List[Neighbour],
        table_id: int
) -> str:
    """Generate a LaTeX table highlighting cross-script matches."""
    label = f"tab:cross-script-{table_id}"
    cross_script_count = sum(1 for n in neighbours if n.script != toponym.script)

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\small",
        rf"\caption{{Cross-script neighbours for \textbf{{{escape_latex(toponym.name)}}} [{toponym.lang}, {toponym.script}] "
        rf"({cross_script_count}/{len(neighbours)} cross-script)}}",
        rf"\label{{{label}}}",
        r"\begin{tabular}{rllllp{3.5cm}}",
        r"\toprule",
        r"\textbf{Rank} & \textbf{Score} & \textbf{Name} & \textbf{Lang} & \textbf{Script} & \textbf{IPA} \\",
        r"\midrule",
    ]

    query_ipa = format_ipa(toponym.ipa)
    lines.append(
        rf"\rowcolor{{gray!20}} Q & --- & {escape_latex(toponym.name)} & {toponym.lang} & {toponym.script} & {query_ipa} \\"
    )
    lines.append(r"\midrule")

    for n in neighbours:
        ipa_cell = format_ipa(n.ipa)
        if n.script != toponym.script:
            lines.append(
                rf"\rowcolor{{blue!10}} {n.rank} & {n.score:.4f} & {escape_latex(n.name)} & {n.lang} & {n.script} & {ipa_cell} \\"
            )
        else:
            lines.append(
                rf"{n.rank} & {n.score:.4f} & {escape_latex(n.name)} & {n.lang} & {n.script} & {ipa_cell} \\"
            )

    lines.extend([
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ])

    return "\n".join(lines)


def generate_noise_table(
        toponym: Toponym,
        neighbours: List[Neighbour],
        table_id: int,
        original_recovered_rank: Optional[int] = None
) -> str:
    """Generate a LaTeX table for noise robustness evaluation."""
    label = f"tab:noise-{table_id}"

    noise_desc = ", ".join(toponym.noise_applied) if toponym.noise_applied else "none"
    recovery_status = f"original at rank {original_recovered_rank}" if original_recovered_rank else "original not in top-k"

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\small",
        rf"\caption{{Noise robustness: \textbf{{{escape_latex(toponym.original_name)}}} $\rightarrow$ \textbf{{{escape_latex(toponym.name)}}} [{toponym.lang}, {toponym.script}]}}",
        rf"\label{{{label}}}",
        r"\begin{tabular}{rllll}",
        r"\toprule",
        r"\multicolumn{5}{l}{\small Noise applied: " + escape_latex(noise_desc) + r"} \\",
        r"\multicolumn{5}{l}{\small Recovery: " + recovery_status + r"} \\",
        r"\midrule",
        r"\textbf{Rank} & \textbf{Score} & \textbf{Name} & \textbf{Lang} & \textbf{Script} \\",
        r"\midrule",
    ]

    # Show the noisy query
    lines.append(
        rf"\rowcolor{{gray!20}} Q & --- & {escape_latex(toponym.name)} & {toponym.lang} & {toponym.script} \\"
    )
    lines.append(r"\midrule")

    for n in neighbours:
        # Highlight if this is the original (exact match or very close)
        is_original = (escape_latex(n.name) == escape_latex(toponym.original_name) and n.lang == toponym.lang)

        if is_original:
            lines.append(
                rf"\rowcolor{{green!20}} {n.rank} & {n.score:.4f} & {escape_latex(n.name)} & {n.lang} & {n.script} \\"
            )
        else:
            lines.append(
                rf"{n.rank} & {n.score:.4f} & {escape_latex(n.name)} & {n.lang} & {n.script} \\"
            )

    lines.extend([
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ])

    return "\n".join(lines)


# =============================================================================
# SUMMARY TABLE GENERATION
# =============================================================================

def generate_summary_table(
        results: List[Tuple[Toponym, List[Neighbour]]],
        series: str
) -> str:
    """Generate a summary statistics table for a series."""
    if not results:
        return ""

    all_scores = []
    same_lang_counts = []
    has_ipa_counts = []

    for toponym, neighbours in results:
        scores = [n.score for n in neighbours]
        all_scores.extend(scores)
        same_lang = sum(1 for n in neighbours if n.lang == toponym.lang)
        same_lang_counts.append(same_lang)
        with_ipa = sum(1 for n in neighbours if n.has_ipa)
        has_ipa_counts.append(with_ipa)

    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0
    min_score = min(all_scores) if all_scores else 0
    max_score = max(all_scores) if all_scores else 0
    avg_same_lang = sum(same_lang_counts) / len(same_lang_counts) if same_lang_counts else 0
    avg_with_ipa = sum(has_ipa_counts) / len(has_ipa_counts) if has_ipa_counts else 0

    k = len(results[0][1]) if results and results[0][1] else 15
    series_label = "Epitran-supported languages" if series == "epitran" else "Non-Epitran languages"

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        rf"\caption{{Summary statistics for {series_label} ($n={len(results)}$, $k={k}$)}}",
        rf"\label{{tab:embed-summary-{series}}}",
        r"\begin{tabular}{lr}",
        r"\toprule",
        r"\textbf{Metric} & \textbf{Value} \\",
        r"\midrule",
        rf"Mean similarity score & {avg_score:.4f} \\",
        rf"Min similarity score & {min_score:.4f} \\",
        rf"Max similarity score & {max_score:.4f} \\",
        r"\midrule",
        rf"Avg. same-language neighbours & {avg_same_lang:.1f} / {k} \\",
        rf"Avg. neighbours with IPA & {avg_with_ipa:.1f} / {k} \\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ]

    return "\n".join(lines)


def generate_cross_script_summary(
        results: List[Tuple[Toponym, List[Neighbour]]]
) -> str:
    """Generate summary statistics for cross-script evaluation."""
    if not results:
        return ""

    all_scores = []
    cross_script_counts = []
    cross_script_scores = []
    same_script_scores = []
    scripts_found: Set[str] = set()

    for toponym, neighbours in results:
        scripts_found.add(toponym.script)
        for n in neighbours:
            all_scores.append(n.score)
            scripts_found.add(n.script)
            if n.script != toponym.script:
                cross_script_scores.append(n.score)
            else:
                same_script_scores.append(n.score)
        cross_count = sum(1 for n in neighbours if n.script != toponym.script)
        cross_script_counts.append(cross_count)

    k = len(results[0][1]) if results and results[0][1] else 15
    avg_cross = sum(cross_script_counts) / len(cross_script_counts) if cross_script_counts else 0
    avg_cross_score = sum(cross_script_scores) / len(cross_script_scores) if cross_script_scores else 0
    avg_same_score = sum(same_script_scores) / len(same_script_scores) if same_script_scores else 0

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\caption{Cross-script evaluation summary}",
        r"\label{tab:cross-script-summary}",
        r"\begin{tabular}{lr}",
        r"\toprule",
        r"\textbf{Metric} & \textbf{Value} \\",
        r"\midrule",
        rf"Query toponyms evaluated & {len(results)} \\",
        rf"Neighbours per query & {k} \\",
        rf"Scripts encountered & {len(scripts_found)} \\",
        r"\midrule",
        rf"Avg. cross-script neighbours & {avg_cross:.1f} / {k} \\",
        rf"Cross-script neighbour rate & {100 * avg_cross / k:.1f}\% \\",
        r"\midrule",
        rf"Mean score (cross-script) & {avg_cross_score:.4f} \\",
        rf"Mean score (same-script) & {avg_same_score:.4f} \\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ]

    return "\n".join(lines)


def generate_noise_summary(
        results: List[Tuple[Toponym, List[Neighbour], Optional[int]]]
) -> str:
    """Generate summary statistics for noise robustness evaluation."""
    if not results:
        return ""

    recovery_ranks = []
    recovered_count = 0
    top1_recovered = 0
    top5_recovered = 0
    top10_recovered = 0

    all_top1_scores = []
    noise_type_counts: Dict[str, int] = {}

    for toponym, neighbours, original_rank in results:
        if neighbours:
            all_top1_scores.append(neighbours[0].score)

        if original_rank is not None:
            recovery_ranks.append(original_rank)
            recovered_count += 1
            if original_rank == 1:
                top1_recovered += 1
            if original_rank <= 5:
                top5_recovered += 1
            if original_rank <= 10:
                top10_recovered += 1

        # Count noise types
        for noise in toponym.noise_applied:
            noise_type = noise.split()[0] if noise else "unknown"
            noise_type_counts[noise_type] = noise_type_counts.get(noise_type, 0) + 1

    n = len(results)
    avg_recovery_rank = sum(recovery_ranks) / len(recovery_ranks) if recovery_ranks else float('inf')
    avg_top1_score = sum(all_top1_scores) / len(all_top1_scores) if all_top1_scores else 0

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\caption{Noise robustness evaluation summary}",
        r"\label{tab:noise-summary}",
        r"\begin{tabular}{lr}",
        r"\toprule",
        r"\textbf{Metric} & \textbf{Value} \\",
        r"\midrule",
        rf"Total queries & {n} \\",
        rf"Original recovered in top-k & {recovered_count} ({100 * recovered_count / n:.1f}\%) \\",
        rf"Original at rank 1 & {top1_recovered} ({100 * top1_recovered / n:.1f}\%) \\",
        rf"Original in top-5 & {top5_recovered} ({100 * top5_recovered / n:.1f}\%) \\",
        rf"Original in top-10 & {top10_recovered} ({100 * top10_recovered / n:.1f}\%) \\",
        r"\midrule",
        rf"Mean recovery rank & {avg_recovery_rank:.1f} \\",
        rf"Mean top-1 similarity & {avg_top1_score:.4f} \\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ]

    return "\n".join(lines)


def generate_comparison_table(
        epitran_results: List[Tuple[Toponym, List[Neighbour]]],
        noepitran_results: List[Tuple[Toponym, List[Neighbour]]]
) -> str:
    """Generate a comparison table between Epitran and non-Epitran series."""

    def calc_stats(results):
        if not results:
            return {}
        all_scores = []
        top1_scores = []
        top5_scores = []
        for _, neighbours in results:
            scores = [n.score for n in neighbours]
            all_scores.extend(scores)
            if scores:
                top1_scores.append(scores[0])
                top5_scores.extend(scores[:5])
        return {
            'mean': sum(all_scores) / len(all_scores) if all_scores else 0,
            'top1_mean': sum(top1_scores) / len(top1_scores) if top1_scores else 0,
            'top5_mean': sum(top5_scores) / len(top5_scores) if top5_scores else 0,
        }

    epitran_stats = calc_stats(epitran_results)
    noepitran_stats = calc_stats(noepitran_results)

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\caption{Comparison of embedding quality: Epitran-supported vs non-Epitran languages}",
        r"\label{tab:embed-comparison}",
        r"\begin{tabular}{lrr}",
        r"\toprule",
        r"\textbf{Metric} & \textbf{Epitran} & \textbf{Non-Epitran} \\",
        r"\midrule",
        rf"Mean similarity (all neighbours) & {epitran_stats.get('mean', 0):.4f} & {noepitran_stats.get('mean', 0):.4f} \\",
        rf"Mean similarity (top-1) & {epitran_stats.get('top1_mean', 0):.4f} & {noepitran_stats.get('top1_mean', 0):.4f} \\",
        rf"Mean similarity (top-5) & {epitran_stats.get('top5_mean', 0):.4f} & {noepitran_stats.get('top5_mean', 0):.4f} \\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        ""
    ]

    return "\n".join(lines)


# =============================================================================
# MAIN EVALUATION FUNCTIONS
# =============================================================================

def run_evaluation(
        es_host: str,
        index: str,
        n_samples: int,
        k_neighbours: int,
        output_path: str,
        seed: int = None
):
    """Run the standard Epitran/non-Epitran evaluation."""
    if not es_host.startswith(('http://', 'https://')):
        es_host = f'http://{es_host}'

    es = Elasticsearch([es_host], request_timeout=60)

    if not es.ping():
        print(f"ERROR: Cannot connect to Elasticsearch at {es_host}")
        sys.exit(1)

    print(f"Connected to Elasticsearch at {es_host}")
    print(f"Index: {index}")
    print(f"Samples per series: {n_samples}")
    print(f"Neighbours per query: {k_neighbours}")
    print(f"Epitran languages configured: {len(EPITRAN_LANGUAGE_CODES)}")
    print()

    print("Refreshing index...")
    es.indices.refresh(index=index)

    stats = es.count(index=index, body={"query": {"match_all": {}}})
    total_docs = stats['count']

    with_embedding = es.count(index=index, body={
        "query": {"exists": {"field": "embedding"}}
    })['count']

    epitran_count = es.count(index=index, body={
        "query": {
            "bool": {
                "must": [
                    {"exists": {"field": "embedding"}},
                    {"terms": {"lang": list(EPITRAN_LANGUAGE_CODES)}}
                ]
            }
        }
    })['count']

    print(f"Index statistics:")
    print(f"  Total documents: {total_docs:,}")
    print(f"  With embeddings: {with_embedding:,}")
    print(f"  Epitran-supported languages: {epitran_count:,}")
    print(f"  Non-Epitran languages: {with_embedding - epitran_count:,}")
    print()

    latex_parts = [
        r"""% =============================================================================
% EMBEDDING SIMILARITY EVALUATION
% Generated by testing.evaluate_embeddings
% =============================================================================

% Required packages (add to document preamble if not present):
% \usepackage{booktabs}
% \usepackage{colortbl}
% \usepackage{xcolor}

"""
    ]

    # Series 1: Epitran-supported
    print("=" * 60)
    print("SERIES 1: Epitran-supported languages")
    print("=" * 60)

    latex_parts.append(r"\subsection*{Series 1: Epitran-supported languages}")
    latex_parts.append("")

    epitran_toponyms = get_random_toponyms_by_epitran(es, index, n_samples, with_epitran=True, seed=seed)
    print(f"Sampled {len(epitran_toponyms)} toponyms with Epitran support")

    epitran_results = []
    for i, toponym in enumerate(epitran_toponyms):
        print(
            f"  [{i + 1}/{len(epitran_toponyms)}] {toponym.name} ({toponym.lang}) - IPA: {'yes' if toponym.has_ipa else 'no'}")
        neighbours = find_neighbours(es, index, toponym, k_neighbours)
        epitran_results.append((toponym, neighbours))
        table = generate_latex_table(toponym, neighbours, i + 1, "epitran")
        latex_parts.append(table)

    summary_epitran = generate_summary_table(epitran_results, "epitran")
    latex_parts.append(summary_epitran)

    # Series 2: Non-Epitran
    print()
    print("=" * 60)
    print("SERIES 2: Non-Epitran languages")
    print("=" * 60)

    latex_parts.append(r"\subsection*{Series 2: Non-Epitran languages}")
    latex_parts.append("")

    seed2 = (seed + 12345) if seed else None
    noepitran_toponyms = get_random_toponyms_by_epitran(es, index, n_samples, with_epitran=False, seed=seed2)
    print(f"Sampled {len(noepitran_toponyms)} toponyms without Epitran support")

    noepitran_results = []
    for i, toponym in enumerate(noepitran_toponyms):
        print(f"  [{i + 1}/{len(noepitran_toponyms)}] {toponym.name} ({toponym.lang})")
        neighbours = find_neighbours(es, index, toponym, k_neighbours)
        noepitran_results.append((toponym, neighbours))
        table = generate_latex_table(toponym, neighbours, i + 1, "noepitran")
        latex_parts.append(table)

    summary_noepitran = generate_summary_table(noepitran_results, "noepitran")
    latex_parts.append(summary_noepitran)

    latex_parts.append(generate_comparison_table(epitran_results, noepitran_results))

    latex_content = "\n".join(latex_parts)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(latex_content)

    print()
    print("=" * 60)
    print(f"Output written to: {output_path}")
    print("=" * 60)


def run_cross_script_evaluation(
        es_host: str,
        index: str,
        n_samples: int,
        k_neighbours: int,
        output_path: str,
        seed: int = None,
        scripts: List[str] = None
):
    """Run cross-script evaluation."""
    if scripts is None:
        scripts = ['cyrillic', 'greek', 'arabic', 'hebrew', 'cjk']

    if not es_host.startswith(('http://', 'https://')):
        es_host = f'http://{es_host}'

    es = Elasticsearch([es_host], request_timeout=60)

    if not es.ping():
        print(f"ERROR: Cannot connect to Elasticsearch at {es_host}")
        sys.exit(1)

    print(f"Connected to Elasticsearch at {es_host}")
    print(f"Index: {index}")
    print(f"Scripts to evaluate: {scripts}")
    print(f"Samples per script: {n_samples}")
    print(f"Neighbours per query: {k_neighbours}")
    print()

    print("Refreshing index...")
    es.indices.refresh(index=index)

    latex_parts = [
        r"""% =============================================================================
% CROSS-SCRIPT EMBEDDING EVALUATION
% Generated by testing.evaluate_embeddings --cross-script
% =============================================================================

% Required packages:
% \usepackage{booktabs}
% \usepackage{colortbl}
% \usepackage{xcolor}

"""
    ]

    all_results = []

    for script in scripts:
        print("=" * 60)
        print(f"SCRIPT: {script.upper()}")
        print("=" * 60)

        latex_parts.append(rf"\subsection*{{Queries in {script.title()} script}}")
        latex_parts.append("")

        script_seed = (seed + hash(script) % (2 ** 31)) if seed else None
        toponyms = get_toponyms_by_script(es, index, script, n_samples, seed=script_seed)

        if not toponyms:
            print(f"  No toponyms found for script: {script}")
            continue

        print(f"  Sampled {len(toponyms)} toponyms")

        for i, toponym in enumerate(toponyms):
            print(f"    [{i + 1}/{len(toponyms)}] {toponym.name} ({toponym.lang}, {toponym.script})")
            neighbours = find_neighbours(es, index, toponym, k_neighbours)
            all_results.append((toponym, neighbours))

            cross_count = sum(1 for n in neighbours if n.script != toponym.script)
            print(f"      -> {cross_count}/{len(neighbours)} cross-script neighbours")

            table = generate_cross_script_table(toponym, neighbours, len(all_results))
            latex_parts.append(table)

        print()

    if all_results:
        latex_parts.append(generate_cross_script_summary(all_results))

    latex_content = "\n".join(latex_parts)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(latex_content)

    print("=" * 60)
    print(f"Output written to: {output_path}")
    print("=" * 60)


def run_noise_evaluation(
        es_host: str,
        index: str,
        n_samples: int,
        k_neighbours: int,
        output_path: str,
        noise_level: float = 0.3,
        model_path: str = None,
        seed: int = None,
        scripts: List[str] = None
):
    """Run noise robustness evaluation."""
    if scripts is None:
        scripts = ['latin', 'cyrillic', 'greek', 'arabic']

    if not es_host.startswith(('http://', 'https://')):
        es_host = f'http://{es_host}'

    es = Elasticsearch([es_host], request_timeout=60)

    if not es.ping():
        print(f"ERROR: Cannot connect to Elasticsearch at {es_host}")
        sys.exit(1)

    # Load model for encoding noisy toponyms
    if model_path is None:
        print("ERROR: --model-path required for noise evaluation")
        sys.exit(1)

    print(f"Loading model from {model_path}...")

    import torch
    from pathlib import Path

    # Import model classes
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from phonetics.models import HybridPhoneticModel, PhoneticEncoder, CharEncoder
    from phonetics.vocab import CharVocab, LangVocab

    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    checkpoint = torch.load(model_path, map_location=device)
    vocab_dir = Path(model_path).parent
    base_name = Path(model_path).stem

    char_vocab = CharVocab.load(vocab_dir / f'{base_name}_char_vocab.pkl')
    lang_vocab = LangVocab.load(vocab_dir / f'{base_name}_lang_vocab.pkl')

    phonetic_encoder = PhoneticEncoder()
    char_encoder = CharEncoder(
        vocab_size=checkpoint['char_vocab_size'],
        num_langs=checkpoint['num_langs']
    )
    model = HybridPhoneticModel(phonetic_encoder, char_encoder)
    model.load_state_dict(checkpoint['model_state'])
    model = model.to(device)
    model.eval()

    print(f"Model loaded. Device: {device}")

    print(f"Connected to Elasticsearch at {es_host}")
    print(f"Index: {index}")
    print(f"Scripts to evaluate: {scripts}")
    print(f"Samples per script: {n_samples}")
    print(f"Neighbours per query: {k_neighbours}")
    print(f"Noise level: {noise_level}")
    print()

    print("Refreshing index...")
    es.indices.refresh(index=index)

    latex_parts = [
        rf"""% =============================================================================
% NOISE ROBUSTNESS EVALUATION
% Generated by testing.evaluate_embeddings --noise
% Noise level: {noise_level}
% =============================================================================

% Required packages:
% \usepackage{{booktabs}}
% \usepackage{{colortbl}}
% \usepackage{{xcolor}}

"""
    ]

    all_results = []  # (toponym, neighbours, original_rank)

    for script in scripts:
        print("=" * 60)
        print(f"SCRIPT: {script.upper()}")
        print("=" * 60)

        latex_parts.append(rf"\subsection*{{Noise robustness: {script.title()} script}}")
        latex_parts.append("")

        script_seed = (seed + hash(script) % (2 ** 31)) if seed else None

        # Get toponyms - for Latin use Epitran filter, for others use script filter
        if script == 'latin':
            toponyms = get_random_toponyms_by_epitran(es, index, n_samples, with_epitran=True, seed=script_seed)
        else:
            toponyms = get_toponyms_by_script(es, index, script, n_samples, seed=script_seed)

        if not toponyms:
            print(f"  No toponyms found for script: {script}")
            continue

        print(f"  Sampled {len(toponyms)} toponyms")

        for i, toponym in enumerate(toponyms):
            # Apply noise
            noise_seed = (seed + i * 1000) if seed else None
            noisy_name, noise_applied = apply_noise(
                toponym.name,
                toponym.script,
                noise_level,
                seed=noise_seed
            )

            # Create noisy toponym
            noisy_toponym = Toponym(
                doc_id=toponym.doc_id,
                name=noisy_name,
                lang=toponym.lang,
                ipa=None,  # Noisy version won't have IPA
                embedding=[],  # Will be computed
                original_name=toponym.name,
                noise_applied=noise_applied
            )

            print(f"    [{i + 1}/{len(toponyms)}] {toponym.name} -> {noisy_name}")
            print(f"        Noise: {', '.join(noise_applied)}")

            # Encode noisy name and search
            try:
                noisy_embedding, neighbours = encode_and_search_noisy(
                    es, index, noisy_name, toponym.lang, toponym.doc_id,
                    k_neighbours, model, char_vocab, lang_vocab, device
                )
                noisy_toponym.embedding = noisy_embedding
            except Exception as e:
                print(f"        ERROR encoding: {e}")
                neighbours = []

            # Check if original was recovered
            original_rank = None
            for n in neighbours:
                if n.name == toponym.name and n.lang == toponym.lang:
                    original_rank = n.rank
                    break

            if original_rank:
                print(f"        Original recovered at rank {original_rank}")
            else:
                print(f"        Original NOT in top-{k_neighbours}")

            all_results.append((noisy_toponym, neighbours, original_rank))

            table = generate_noise_table(noisy_toponym, neighbours, len(all_results), original_rank)
            latex_parts.append(table)

        print()

    if all_results:
        latex_parts.append(generate_noise_summary(all_results))

    latex_content = "\n".join(latex_parts)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(latex_content)

    print("=" * 60)
    print(f"Output written to: {output_path}")
    print("=" * 60)


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Evaluate phonetic embeddings via kNN search in Elasticsearch',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('--es-host', default=ES_HOST,
                        help=f'Elasticsearch host (default: {ES_HOST})')
    parser.add_argument('--index', default='toponyms',
                        help='Index name (default: toponyms)')
    parser.add_argument('-n', '--samples', type=int, default=10,
                        help='Number of random samples per series (default: 10)')
    parser.add_argument('-k', '--neighbours', type=int, default=15,
                        help='Number of neighbours to retrieve (default: 15)')
    parser.add_argument('-o', '--output', default='article/embedding-evaluation.tex',
                        help='Output LaTeX file (default: article/embedding-evaluation.tex)')
    parser.add_argument('--seed', type=int, default=None,
                        help='Random seed for reproducibility')

    # Mode selection
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('--cross-script', action='store_true',
                            help='Run cross-script evaluation')
    mode_group.add_argument('--noise', action='store_true',
                            help='Run noise robustness evaluation')

    # Cross-script options
    parser.add_argument('--scripts', nargs='+',
                        default=['cyrillic', 'greek', 'arabic', 'hebrew', 'cjk'],
                        help='Scripts to evaluate (default: cyrillic greek arabic hebrew cjk)')

    # Noise options
    parser.add_argument('--noise-level', type=float, default=0.3,
                        help='Noise level 0.0-1.0 (default: 0.3)')
    parser.add_argument('--model-path', type=str, default=f"{IX1_BASE}/models/phonetic/checkpoints/final_model_b.pt",
                        help='Path to trained model (required for --noise mode)')

    args = parser.parse_args()

    if args.noise:
        run_noise_evaluation(
            es_host=args.es_host,
            index=args.index,
            n_samples=args.samples,
            k_neighbours=args.neighbours,
            output_path=args.output,
            noise_level=args.noise_level,
            model_path=args.model_path,
            seed=args.seed,
            scripts=args.scripts if args.scripts != ['cyrillic', 'greek', 'arabic', 'hebrew', 'cjk']
            else ['latin', 'cyrillic', 'greek', 'arabic']
        )
    elif args.cross_script:
        run_cross_script_evaluation(
            es_host=args.es_host,
            index=args.index,
            n_samples=args.samples,
            k_neighbours=args.neighbours,
            output_path=args.output,
            seed=args.seed,
            scripts=args.scripts
        )
    else:
        run_evaluation(
            es_host=args.es_host,
            index=args.index,
            n_samples=args.samples,
            k_neighbours=args.neighbours,
            output_path=args.output,
            seed=args.seed
        )


if __name__ == '__main__':
    main()