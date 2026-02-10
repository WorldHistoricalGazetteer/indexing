"""
Rebuild the ES toponyms index with vocabulary generation and PanPhon embeddings.

v6 Pipeline - Phase 1: Extraction and ES Indexing

Based on the proven v4/v5 architecture (66M+ toponyms processed successfully).
Adds Phonikud (Hebrew) and CharsiuG2P (Chinese topolects, Korean) to IPAConverter
while keeping the same pipeline, worker function, and output format.

This script:
1. Scans all places to extract toponyms with attestations (back-references to places)
2. Builds character vocabulary covering all observed scripts + full Unicode ranges
3. Computes IPA and PanPhon embeddings for toponyms in training namespaces
4. Indexes toponyms to ES with panphon_embedding for phonetic similarity queries
5. Refreshes index and creates snapshot

The panphon_embedding field enables:
- Phonetic clustering for pair generation (replacing string similarity thresholds)
- Phonetic hard negative mining (replacing orthographic prefix matching)

Vocabulary output enables subsequent training without re-scanning the corpus.

Reliability: Uses JSONL buffer on scratch disk to decouple DuckDB from HTTP.
Supports resuming from an existing DuckDB database.

IPA backends:
- Epitran: Latin, Cyrillic, Greek, Arabic, Devanagari, Bengali, Tamil, Telugu,
  Malayalam, Kannada, Gujarati, Thai, Georgian, Armenian, Japanese (kana)
- Phonikud: Hebrew (he) — neural diacritization + phonemization (no Epitran support)
- CharsiuG2P: Mandarin (zh), Korean (ko), Cantonese (yue), Gan (gan), Wu (wuu)
"""

# Suppress warnings early - before any imports that might trigger them
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module='epitran')
warnings.filterwarnings("ignore", category=UserWarning, module='pkg_resources')
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")
warnings.filterwarnings("ignore", message=".*tokenizer class.*")

import argparse
import hashlib
import json
import logging
import multiprocessing as mp
import os
import shutil
import struct
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Dict, List, Optional, Tuple, Set

from processing.utilities import create_checkpoint_snapshot

import duckdb

try:
    from elasticsearch import Elasticsearch, helpers
except ImportError:
    print("Error: elasticsearch package required.")
    sys.exit(1)

import pyarrow as pa
import pyarrow.parquet as pq

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

try:
    from anyascii import anyascii
except ImportError:
    anyascii = None

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.utils.script_detection import (
    Script, detect_script, get_primary_namespace,
    SCRIPT_RANGES
)
from processing.settings import ES_HOST, IX1_BASE, STAGING_REPO_NAME


def bulk_insert_duckdb(conn, table_name: str, columns: List[str], data: List[Tuple]):
    """
    Fast bulk insert into DuckDB using PyArrow.
    """
    if not data:
        return

    # Build columnar data efficiently using zip (avoids nested loops)
    # zip(*data) transposes rows to columns
    col_data = dict(zip(columns, zip(*data)))
    arrow_table = pa.table(col_data)
    conn.execute(f"INSERT INTO {table_name} SELECT * FROM arrow_table")


# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.getLogger("elastic_transport").setLevel(logging.WARNING)
logging.getLogger("elasticsearch").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent.parent.parent / 'schemas' / 'toponyms.json'

# --- CONSTANTS ---
MAX_ID_BYTES = 450
MAX_NAME_LEN = 200

# Epitran language mappings: (lang, script) -> Epitran code
EPITRAN_LANG_MAP = {
    ('en', Script.LATIN): 'eng-Latn',
    ('de', Script.LATIN): 'deu-Latn',
    ('fr', Script.LATIN): 'fra-Latn',
    ('es', Script.LATIN): 'spa-Latn',
    ('it', Script.LATIN): 'ita-Latn',
    ('pt', Script.LATIN): 'por-Latn',
    ('nl', Script.LATIN): 'nld-Latn',
    ('pl', Script.LATIN): 'pol-Latn',
    ('cs', Script.LATIN): 'ces-Latn',
    ('ro', Script.LATIN): 'ron-Latn',
    ('hu', Script.LATIN): 'hun-Latn',
    ('fi', Script.LATIN): 'fin-Latn',
    ('sv', Script.LATIN): 'swe-Latn',
    ('no', Script.LATIN): 'nor-Latn',
    ('da', Script.LATIN): 'dan-Latn',
    ('tr', Script.LATIN): 'tur-Latn',
    ('vi', Script.LATIN): 'vie-Latn',
    ('id', Script.LATIN): 'ind-Latn',
    ('ms', Script.LATIN): 'msa-Latn',
    ('sw', Script.LATIN): 'swa-Latn',
    ('la', Script.LATIN): 'lat-Latn',
    ('ru', Script.CYRILLIC): 'rus-Cyrl',
    ('uk', Script.CYRILLIC): 'ukr-Cyrl',
    ('bg', Script.CYRILLIC): 'bul-Cyrl',
    ('sr', Script.CYRILLIC): 'srp-Cyrl',
    ('mk', Script.CYRILLIC): 'mkd-Cyrl',
    ('el', Script.GREEK): 'ell-Grek',
    ('ar', Script.ARABIC): 'ara-Arab',
    ('fa', Script.ARABIC): 'fas-Arab',
    ('ur', Script.ARABIC): 'urd-Arab',
    # Note: Hebrew (he) is NOT supported by Epitran — handled by Phonikud
    ('hi', Script.DEVANAGARI): 'hin-Deva',
    ('mr', Script.DEVANAGARI): 'mar-Deva',
    ('ne', Script.DEVANAGARI): 'nep-Deva',
    ('sa', Script.DEVANAGARI): 'san-Deva',
    ('bn', Script.BENGALI): 'ben-Beng',
    ('ta', Script.TAMIL): 'tam-Taml',
    ('te', Script.TELUGU): 'tel-Telu',
    ('ml', Script.MALAYALAM): 'mal-Mlym',
    ('kn', Script.KANNADA): 'kan-Knda',
    ('gu', Script.GUJARATI): 'guj-Gujr',
    ('th', Script.THAI): 'tha-Thai',
    ('ka', Script.GEORGIAN): 'kat-Geor',
    ('hy', Script.ARMENIAN): 'hye-Armn',
    # Note: Korean (ko) handled by CharsiuG2P, not Epitran
    # Note: Mandarin Chinese (zh) handled by CharsiuG2P;
    #   Epitran cmn-Hans requires CC-CEDict file and is not used here.
    ('ja', Script.HIRAGANA): 'jpn-Hira',
}

# Languages routed to CharsiuG2P instead of Epitran
CHARSIU_LANGUAGES = {'zh', 'ko', 'gan', 'wuu', 'yue'}

# CharsiuG2P language code mapping
CHARSIU_LANG_MAP = {
    'zh': 'cmn',     # Mandarin Chinese
    'ko': 'kor',     # Korean
    'ja': 'jpn',     # Japanese (fallback if Epitran fails)
    'gan': 'cmn',    # Gan Chinese (Mandarin proxy — same characters, approximate phonetics)
    'wuu': 'cmn',    # Wu Chinese (Mandarin proxy)
    'yue': 'yue',    # Cantonese
}


# ============================================================================
# GLOBAL MODEL CACHING (one instance per worker process)
# ============================================================================

_GLOBAL_CHARSIU = None
_GLOBAL_PHONIKUD = None
_WORKER_MODEL_TYPE = 'epitran'  # Tracks which models this worker should load


def get_global_charsiu():
    """
    Load CharsiuG2P once per worker process.
    Returns None immediately if this worker is specialized for a different model.
    """
    global _GLOBAL_CHARSIU, _WORKER_MODEL_TYPE

    # Skip loading if this worker is specialized for a different model
    if _WORKER_MODEL_TYPE not in ('charsiu', 'epitran'):
        return None

    if _GLOBAL_CHARSIU is None:
        try:
            import google.protobuf  # noqa: F401
            import torch
            import transformers

            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=FutureWarning)
                warnings.filterwarnings("ignore", message=".*tokenizer class.*")
                model = transformers.T5ForConditionalGeneration.from_pretrained(
                    "charsiu/g2p_multilingual_byT5_small_100"
                )
                tokenizer = transformers.ByT5Tokenizer.from_pretrained("google/byt5-small")

            device = "cuda" if torch.cuda.is_available() else "cpu"
            model.to(device)

            class _CharsiuWrapper:
                def __init__(self, m, t, d):
                    self.model, self.tokenizer, self.device = m, t, d

                def transliterate(self, text, lang):
                    char_iso = CHARSIU_LANG_MAP.get(lang, lang)
                    input_text = f"<{char_iso}>: {text}"
                    inputs = self.tokenizer(input_text, return_tensors="pt").to(self.device)
                    with torch.no_grad():
                        outputs = self.model.generate(**inputs)
                    return self.tokenizer.decode(outputs[0], skip_special_tokens=True)

            _GLOBAL_CHARSIU = _CharsiuWrapper(model, tokenizer, device)
            logger.info("CharsiuG2P initialized (Mandarin, Korean, Cantonese, Gan, Wu)")
        except Exception as e:
            logger.warning(f"CharsiuG2P unavailable: {e}")
            _GLOBAL_CHARSIU = False
    return _GLOBAL_CHARSIU if _GLOBAL_CHARSIU is not False else None


def get_global_phonikud():
    """
    Load Phonikud once per worker process.
    Returns None immediately if this worker is specialized for a different model.
    """
    global _GLOBAL_PHONIKUD, _WORKER_MODEL_TYPE

    # Skip loading if this worker is specialized for a different model
    if _WORKER_MODEL_TYPE not in ('phonikud', 'epitran'):
        return None

    if _GLOBAL_PHONIKUD is None:
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*tokenizer class.*")
                import phonikud as phonikud_module

            class _PhonikudWrapper:
                def __init__(self, mod):
                    self._mod = mod

                def transliterate(self, text):
                    return self._mod.phonemize(text)

            _GLOBAL_PHONIKUD = _PhonikudWrapper(phonikud_module)
            logger.info("Phonikud initialized (Hebrew G2P)")
        except Exception as e:
            logger.warning(f"Phonikud unavailable: {e}")
            _GLOBAL_PHONIKUD = False
    return _GLOBAL_PHONIKUD if _GLOBAL_PHONIKUD is not False else None


class IPAConverter:
    """
    Lazy-loaded IPA converter with three backends:
    - Epitran: default for most languages (Latin, Cyrillic, Greek, Arabic, Indic, etc.)
    - Phonikud: Hebrew (he) — neural diacritization then phonemization (no Epitran fallback)
    - CharsiuG2P: Chinese (zh, gan, wuu, yue) and Korean (ko) — multilingual neural G2P

    Each backend is lazy-loaded once per process and cached for the process lifetime.

    Supports specialized mode where only required models are loaded (memory optimization).
    """

    def __init__(self, model_type='epitran'):
        """
        Initialize converter with optional model specialization.

        Args:
            model_type: One of 'charsiu', 'phonikud', or 'epitran'
                       Controls which models are actually loaded:
                       - 'charsiu': Only CharsiuG2P + PanPhon (saves ~120GB with 62 workers)
                       - 'phonikud': Only Phonikud + PanPhon
                       - 'epitran': Only Epitran + PanPhon (default)
        """
        self._model_type = model_type
        self._epitran_cache: Dict[str, object] = {}
        self._panphon_ft = None
        self._epitran_available = None if model_type == 'epitran' else False
        self._panphon_available = None

    # --- Backend availability checks (lazy, one-shot) ---

    def _check_epitran(self) -> bool:
        if self._epitran_available is None:
            try:
                import epitran
                self._epitran_available = True
            except ImportError:
                logger.warning("Epitran not available. IPA conversion disabled.")
                self._epitran_available = False
        return self._epitran_available

    def _check_panphon(self) -> bool:
        if self._panphon_available is None:
            try:
                import panphon
                self._panphon_ft = panphon.FeatureTable()
                self._panphon_available = True
            except ImportError:
                logger.warning("PanPhon not available. Feature extraction disabled.")
                self._panphon_available = False
        return self._panphon_available

    # --- Epitran helpers ---

    def get_epitran(self, lang_code: str) -> Optional[object]:
        if not self._check_epitran():
            return None
        if lang_code not in self._epitran_cache:
            try:
                import epitran
                self._epitran_cache[lang_code] = epitran.Epitran(lang_code)
            except Exception as e:
                logger.debug(f"Failed to create Epitran for {lang_code}: {e}")
                self._epitran_cache[lang_code] = None
        return self._epitran_cache[lang_code]

    def get_epitran_code(self, lang: str, script: Script) -> Optional[str]:
        return EPITRAN_LANG_MAP.get((lang, script))

    # --- Core conversion methods ---

    def to_ipa(self, text: str, lang: str, script: Script) -> Optional[str]:
        """
        Convert text to IPA, dispatching to the appropriate backend:
        1. Hebrew → Phonikud (no Epitran fallback)
        2. Chinese (all varieties) → CharsiuG2P
        3. Korean → CharsiuG2P (no Epitran fallback)
        4. Everything else → Epitran
        """
        # 1. Hebrew (Phonikud only — Epitran does not support Hebrew)
        if lang == 'he' and script == Script.HEBREW:
            phonikud = get_global_phonikud()
            if phonikud:
                try:
                    return phonikud.transliterate(text)
                except Exception:
                    pass
            return None  # No Epitran fallback available

        # 2. Chinese — all varieties via CharsiuG2P (no CC-CEDict dependency)
        if lang in ('zh', 'gan', 'wuu', 'yue') and script == Script.CJK:
            charsiu = get_global_charsiu()
            if charsiu:
                try:
                    return charsiu.transliterate(text, lang)
                except Exception:
                    pass
            return None  # No Epitran fallback for these

        # 3. Korean (CharsiuG2P preferred for both Hangul and Hanja)
        if lang == 'ko' and script in (Script.HANGUL, Script.CJK):
            charsiu = get_global_charsiu()
            if charsiu:
                try:
                    return charsiu.transliterate(text, lang)
                except Exception:
                    pass
            return None  # No Epitran fallback

        # 4. Default: Epitran
        epitran_code = self.get_epitran_code(lang, script)
        if epitran_code is None:
            return None
        epi = self.get_epitran(epitran_code)
        if epi is None:
            return None
        try:
            return epi.transliterate(text)
        except Exception:
            return None

    def to_features(self, ipa: str) -> Optional[List[float]]:
        """Get raw PanPhon features (24 features per segment, variable length)."""
        if not self._check_panphon() or ipa is None:
            return None
        try:
            segments = self._panphon_ft.word_fts(ipa)
            if not segments:
                return None
            features = []
            for seg in segments:
                features.extend(seg.numeric())
            return features
        except Exception:
            return None

    def to_embedding(self, ipa: str) -> Optional[List[float]]:
        """
        Compute 192-dimensional PanPhon embedding using 8-bin position pooling.

        This creates a fixed-size vector suitable for ES dense_vector storage
        and cosine similarity queries. The embedding preserves positional
        information by dividing the word into 8 bins and averaging features
        within each bin.

        Architecture:
            - 8 positional bins (capturing word shape: start, middle, end)
            - 24 articulatory features per bin
            - Total: 8 × 24 = 192 dimensions

        This preserves more information than simple averaging because:
            - "Paris" and "Sirap" will have different embeddings
            - Word beginnings/endings are captured distinctly
            - Variable-length sequences map to fixed representation

        Returns:
            List of 192 floats, or None if conversion fails
        """
        if not self._check_panphon() or ipa is None:
            return None
        try:
            segments = self._panphon_ft.word_fts(ipa)
            if not segments:
                return None

            num_segments = len(segments)
            num_bins = 8
            features_per_bin = 24

            # Initialize bins: 8 bins × 24 features each
            bins = [[0.0] * features_per_bin for _ in range(num_bins)]
            bin_counts = [0] * num_bins

            # Assign each segment to a bin based on position
            for seg_idx, seg in enumerate(segments):
                position = seg_idx / num_segments
                bin_idx = min(int(position * num_bins), num_bins - 1)

                features = seg.numeric()
                for i, val in enumerate(features):
                    bins[bin_idx][i] += val
                bin_counts[bin_idx] += 1

            # Compute mean for each bin (zero-padded bins stay zero)
            embedding = []
            for bin_idx in range(num_bins):
                if bin_counts[bin_idx] > 0:
                    bin_avg = [v / bin_counts[bin_idx] for v in bins[bin_idx]]
                else:
                    bin_avg = [0.0] * features_per_bin
                embedding.extend(bin_avg)

            return embedding
        except Exception:
            return None


# --- Worker pool machinery (identical to proven v4/v5) ---

_WORKER_CONVERTER = None


def _init_worker(model_type='epitran'):
    """
    Initialize worker process with specialized model loading.

    Args:
        model_type: One of 'charsiu', 'phonikud', or 'epitran'
                   Determines which models to load (memory optimization)
    """
    global _WORKER_CONVERTER, _WORKER_MODEL_TYPE
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning, module='epitran')
    warnings.filterwarnings("ignore", category=UserWarning, module='pkg_resources')
    warnings.filterwarnings("ignore", message=".*tokenizer class.*")
    warnings.filterwarnings("ignore", category=FutureWarning)

    # Track which models this worker should load
    _WORKER_MODEL_TYPE = model_type

    # Create specialized converter that only loads required models
    _WORKER_CONVERTER = IPAConverter(model_type=model_type)


def _get_worker_converter() -> IPAConverter:
    """Get the cached converter for this worker process."""
    global _WORKER_CONVERTER
    if _WORKER_CONVERTER is None:
        _init_worker()
    return _WORKER_CONVERTER


def _compute_phonetics_for_batch(batch: List[Tuple]) -> List[Tuple]:
    """
    Worker function for parallel IPA/PanPhon computation.

    Uses global cached converter to avoid re-initialization overhead.
    Clean, minimal return type — proven stable at 66M+ scale.

    Args:
        batch: List of (toponym_id, name, lang, script) tuples

    Returns:
        List of (toponym_id, ipa, full_features_packed, embedding_192) tuples
    """
    converter = _get_worker_converter()
    results = []

    for toponym_id, name, lang, script in batch:
        try:
            script_enum = Script(script)
        except ValueError:
            script_enum = Script.OTHER

        ipa = converter.to_ipa(name, lang, script_enum)
        if not ipa:
            continue

        full_features = converter.to_features(ipa)
        embedding = converter.to_embedding(ipa)

        if embedding:
            packed = struct.pack(f'{len(full_features)}f', *full_features) if full_features else None
            results.append((toponym_id, ipa, packed, embedding))

    return results


# Languages that are expected to use specific scripts
# Used to detect and filter pre-romanized forms (e.g., "Beijing" tagged as zh)
LANG_EXPECTED_SCRIPTS: Dict[str, Set[Script]] = {
    # CJK languages - should NOT be in Latin script
    'zh': {Script.CJK},
    'ja': {Script.CJK, Script.HIRAGANA, Script.KATAKANA},
    'ko': {Script.HANGUL, Script.CJK},  # Korean can use Hanja

    # Cyrillic languages
    'ru': {Script.CYRILLIC},
    'uk': {Script.CYRILLIC},
    'bg': {Script.CYRILLIC},
    'sr': {Script.CYRILLIC, Script.LATIN},  # Serbian uses both
    'mk': {Script.CYRILLIC},
    'be': {Script.CYRILLIC},
    'kk': {Script.CYRILLIC},
    'ky': {Script.CYRILLIC},
    'mn': {Script.CYRILLIC},
    'tg': {Script.CYRILLIC},

    # Greek
    'el': {Script.GREEK},

    # Arabic script languages
    'ar': {Script.ARABIC},
    'fa': {Script.ARABIC},
    'ur': {Script.ARABIC},
    'ps': {Script.ARABIC},

    # Hebrew
    'he': {Script.HEBREW},
    'yi': {Script.HEBREW},

    # Indic scripts
    'hi': {Script.DEVANAGARI},
    'mr': {Script.DEVANAGARI},
    'ne': {Script.DEVANAGARI},
    'sa': {Script.DEVANAGARI},
    'bn': {Script.BENGALI},
    'as': {Script.BENGALI},
    'ta': {Script.TAMIL},
    'te': {Script.TELUGU},
    'ml': {Script.MALAYALAM},
    'kn': {Script.KANNADA},
    'gu': {Script.GUJARATI},

    # Thai
    'th': {Script.THAI},

    # Georgian
    'ka': {Script.GEORGIAN},

    # Armenian
    'hy': {Script.ARMENIAN},
}


def is_script_mismatch(lang: Optional[str], script: Script) -> bool:
    """
    Check if the detected script is inconsistent with the language tag.

    Returns True if this appears to be a pre-romanized form that should be filtered.

    Examples of mismatches we want to filter:
    - "Beijing" with lang=zh and script=LATIN (should be 北京)
    - "Moskva" with lang=ru and script=LATIN (should be Москва)

    We do NOT filter:
    - "London" with lang=en and script=LATIN (correct)
    - "München" with lang=de and script=LATIN (correct)
    - "北京" with lang=zh and script=CJK (correct)
    """
    if not lang:
        return False

    lang_base = lang.lower().split('-')[0]  # Handle zh-Hans, etc.

    if lang_base not in LANG_EXPECTED_SCRIPTS:
        return False

    expected_scripts = LANG_EXPECTED_SCRIPTS[lang_base]

    if script == Script.LATIN and Script.LATIN not in expected_scripts:
        return True

    return False


def create_db(db_path: str):
    """
    Create DuckDB database with optimized columnar storage.
    """
    conn = duckdb.connect(db_path)

    # Configure for bulk loading
    conn.execute("SET threads TO 16")
    conn.execute("SET memory_limit = '32GB'")

    conn.execute('''
        CREATE TABLE IF NOT EXISTS toponyms (
            toponym_id VARCHAR,
            name VARCHAR NOT NULL,
            lang VARCHAR,
            lang_variant VARCHAR,
            script VARCHAR,
            ipa VARCHAR,
            panphon_features BLOB
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS toponym_namespaces (
            toponym_id VARCHAR NOT NULL,
            namespace VARCHAR NOT NULL
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS toponym_attestations (
            toponym_id VARCHAR NOT NULL,
            place_id VARCHAR NOT NULL
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS observed_chars (
            char VARCHAR,
            script VARCHAR,
            count INTEGER DEFAULT 1,
            PRIMARY KEY (char, script)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS script_stats (
            script VARCHAR PRIMARY KEY,
            count INTEGER DEFAULT 0
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS skipped_toponyms (
            toponym_id VARCHAR,
            reason VARCHAR,
            lang VARCHAR,
            script VARCHAR
        )
    ''')

    return conn


def optimize_db_after_load(conn, force: bool = False):
    """Deduplicate and create indexes for DuckDB after bulk loading."""

    before_count = conn.execute("SELECT COUNT(*) FROM toponyms").fetchone()[0]
    distinct_count = conn.execute("SELECT COUNT(DISTINCT toponym_id) FROM toponyms").fetchone()[0]

    if before_count == distinct_count and not force:
        logger.info(f"Deduplication already done ({before_count:,} unique toponyms), skipping...")
    else:
        logger.info("Deduplicating toponyms table...")
        conn.execute('''
            CREATE TABLE toponyms_deduped AS
            SELECT DISTINCT ON (toponym_id) *
            FROM toponyms
        ''')
        conn.execute('DROP TABLE toponyms')
        conn.execute('ALTER TABLE toponyms_deduped RENAME TO toponyms')

        after_count = conn.execute("SELECT COUNT(*) FROM toponyms").fetchone()[0]
        logger.info(f"Deduplication: {before_count:,} -> {after_count:,} ({before_count - after_count:,} duplicates removed)")

    # Check if indexes exist before creating
    existing_indexes = set(row[0] for row in conn.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'toponyms'"
    ).fetchall())

    if 'idx_toponyms_id' not in existing_indexes:
        logger.info("Creating DuckDB indexes...")
        conn.execute('CREATE INDEX IF NOT EXISTS idx_toponyms_id ON toponyms(toponym_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tn_id ON toponym_namespaces(toponym_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ta_id ON toponym_attestations(toponym_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ta_place ON toponym_attestations(place_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_toponyms_script ON toponyms(script)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_toponyms_lang ON toponyms(lang)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_attestations_place_toponym ON toponym_attestations(place_id, toponym_id)')
        logger.info("DuckDB indexes created.")
    else:
        logger.info("DuckDB indexes already exist, skipping...")


def scan_places(es: Elasticsearch, index: str, batch_size: int = 2000) -> Iterator[Tuple[str, Dict]]:
    """Scan places index, yielding (place_id, source) tuples."""
    query = {"query": {"match_all": {}}, "_source": ["namespace", "toponyms"]}
    for doc in helpers.scan(es, index=index, query=query, scroll='60m', size=batch_size):
        yield doc['_id'], doc['_source']


def romanize_for_search(name: str, script: str) -> Optional[str]:
    """
    Generate a romanized form for ES text search (e.g. query "beijing" matches 北京).

    This is an ES indexing concern only — not used by the training pipeline.
    Returns None for Latin-script names (already searchable) or if anyascii
    is unavailable.
    """
    if anyascii is None:
        return None
    if script == Script.LATIN.value:
        return None
    romanized = anyascii(name).lower().strip()
    if romanized and romanized != name.lower():
        return romanized
    return None


def extract_toponyms_to_db(es, conn, places_index, batch_size, limit=None):
    """
    Extract toponyms from places index to DuckDB, collecting:
    - Toponym records with script detection
    - Namespace associations
    - Attestation back-references (place_ids)
    - Character vocabulary for each script

    Filters out:
    - Pre-romanized forms (e.g., "Beijing" tagged as zh with Latin script)
    """
    try:
        total = es.count(index=places_index)['count']
    except Exception:
        total = 0
    if limit:
        total = min(total, limit)

    logger.info(f"Scanning {total:,} places from '{places_index}'")
    places_processed = 0
    toponyms_extracted = 0
    toponyms_skipped = 0

    char_counts: Dict[str, Counter] = defaultdict(Counter)
    script_counts: Counter = Counter()
    mismatch_counts: Counter = Counter()

    iterator = scan_places(es, places_index, batch_size)
    if tqdm:
        iterator = tqdm(iterator, total=total, desc="Extracting", mininterval=10.0)

    toponym_batch = []
    namespace_batch = []
    attestation_batch = []
    skipped_batch = []

    for place_id, place in iterator:
        namespace = place.get('namespace', 'other')
        toponyms_list = place.get('toponyms', [])

        if not toponyms_list:
            continue

        for top in toponyms_list:
            top_id = top.get('toponym_id')
            label = top.get('label')

            if not top_id:
                continue

            if '@' in top_id:
                at_pos = top_id.rfind('@')
                name = top_id[:at_pos]
                lang_part = top_id[at_pos + 1:]
                if '-' in lang_part:
                    parts = lang_part.split('-', 1)
                    lang = parts[0]
                    lang_variant = parts[1]
                else:
                    lang = lang_part
                    lang_variant = None
            else:
                name = top_id
                lang = None
                lang_variant = None

            if not name and label:
                name = label
            if name:
                name = name.strip()
            if not name:
                continue

            if len(name) > MAX_NAME_LEN:
                continue
            if len(name.encode('utf-8')) > MAX_ID_BYTES:
                continue

            if lang and lang.lower() in ('und', 'zxx', 'mis', 'null', 'none'):
                lang = None

            script, _ = detect_script(name)
            script_value = script.value

            if is_script_mismatch(lang, script):
                mismatch_counts[f"{lang}:{script_value}"] += 1
                skipped_batch.append((top_id, 'lang_script_mismatch', lang, script_value))
                toponyms_skipped += 1
                continue

            canonical_id = f"{name}@{lang}" if lang else f"{name}@"

            script_counts[script_value] += 1

            # Collect characters for vocabulary (native script, lowercased)
            for char in name.lower():
                if char.strip():
                    char_counts[script_value][char] += 1

            toponym_batch.append((canonical_id, name, lang, lang_variant, script_value, None, None))
            namespace_batch.append((canonical_id, namespace))
            attestation_batch.append((canonical_id, place_id))
            toponyms_extracted += 1

        places_processed += 1

        if len(toponym_batch) >= batch_size * 5:
            bulk_insert_duckdb(conn, 'toponyms',
                ['toponym_id', 'name', 'lang', 'lang_variant', 'script', 'ipa', 'panphon_features'],
                toponym_batch)
            bulk_insert_duckdb(conn, 'toponym_namespaces', ['toponym_id', 'namespace'], namespace_batch)
            bulk_insert_duckdb(conn, 'toponym_attestations', ['toponym_id', 'place_id'], attestation_batch)
            if skipped_batch:
                bulk_insert_duckdb(conn, 'skipped_toponyms', ['toponym_id', 'reason', 'lang', 'script'], skipped_batch)
            toponym_batch = []
            namespace_batch = []
            attestation_batch = []
            skipped_batch = []

        if limit and places_processed >= limit:
            break

    # Final batch
    if toponym_batch:
        bulk_insert_duckdb(conn, 'toponyms',
            ['toponym_id', 'name', 'lang', 'lang_variant', 'script', 'ipa', 'panphon_features'],
            toponym_batch)
        bulk_insert_duckdb(conn, 'toponym_namespaces', ['toponym_id', 'namespace'], namespace_batch)
        bulk_insert_duckdb(conn, 'toponym_attestations', ['toponym_id', 'place_id'], attestation_batch)
    if skipped_batch:
        bulk_insert_duckdb(conn, 'skipped_toponyms', ['toponym_id', 'reason', 'lang', 'script'], skipped_batch)

    # Save character vocabulary
    logger.info("Saving character vocabulary to database...")
    char_batch = []
    for script_val, counts in char_counts.items():
        for char, count in counts.items():
            char_batch.append((char, script_val, count))

    if char_batch:
        bulk_insert_duckdb(conn, 'observed_chars', ['char', 'script', 'count'], char_batch)

    for script_val, count in script_counts.items():
        conn.execute(
            'INSERT OR REPLACE INTO script_stats VALUES (?, ?)',
            (script_val, count)
        )

    conn.commit()
    optimize_db_after_load(conn)

    # Log statistics
    logger.info(f"Extraction complete:")
    logger.info(f"  Places processed: {places_processed:,}")
    logger.info(f"  Toponyms extracted: {toponyms_extracted:,}")
    logger.info(f"  Toponyms skipped (lang-script mismatch): {toponyms_skipped:,}")

    logger.info(f"Script distribution:")
    for script_val, count in script_counts.most_common(25):
        logger.info(f"  {script_val}: {count:,}")

    if mismatch_counts:
        logger.info(f"Lang-script mismatches filtered (top 20):")
        for mismatch, count in mismatch_counts.most_common(20):
            logger.info(f"  {mismatch}: {count:,}")

    total_chars = sum(len(counts) for counts in char_counts.values())
    logger.info(f"Unique characters observed: {total_chars:,}")

    return places_processed, toponyms_extracted, toponyms_skipped


def rebuild_vocab_stats_from_toponyms(conn, batch_size: int = 50000):
    """Rebuild observed character/script stats directly from toponyms when tables are empty."""
    logger.warning("observed_chars/script_stats empty; rebuilding from toponyms table (one-time scan)...")
    char_counts: Dict[str, Counter] = defaultdict(Counter)
    script_counts: Counter = Counter()

    cursor = conn.execute("SELECT name, script FROM toponyms")
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for name, script in rows:
            if not name or not script:
                continue
            script_counts[script] += 1
            for ch in name.lower():
                if ch.strip():
                    char_counts[ch][script] += 1

    # Persist rebuilt stats so subsequent runs can skip this pass
    conn.execute("DELETE FROM observed_chars")
    conn.execute("DELETE FROM script_stats")

    char_batch = [(ch, script, cnt) for ch, scripts in char_counts.items() for script, cnt in scripts.items()]
    if char_batch:
        bulk_insert_duckdb(conn, 'observed_chars', ['char', 'script', 'count'], char_batch)
    for script, cnt in script_counts.items():
        conn.execute('INSERT OR REPLACE INTO script_stats VALUES (?, ?)', (script, cnt))
    conn.commit()

    logger.info(f"Rebuilt vocab stats from toponyms: {len(char_counts):,} chars across {len(script_counts)} scripts")
    return {ch: list(scripts.items()) for ch, scripts in char_counts.items()}, set(script_counts.keys())


def generate_vocabulary(conn, output_dir: Path) -> Dict:
    """
    Generate expanded character vocabulary from observed characters.

    Strategy:
    1. Start with all observed characters (native scripts)
    2. Expand to full Unicode blocks for ALL observed scripts (including CJK, Hangul)
    3. Add full ASCII printable range
    4. Catch any remaining observed characters

    The character encoder sees native script — no romanization or decomposition.
    Cross-script matching is handled by Symphonym's phonetic embeddings, not
    by reducing everything to ASCII.

    Returns statistics dict.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    observed_chars: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for row in conn.execute('SELECT char, script, count FROM observed_chars').fetchall():
        char, script, count = row
        observed_chars[char].append((script, count))

    observed_scripts = {row[0] for row in conn.execute('SELECT script, count FROM script_stats').fetchall()}

    if not observed_chars or not observed_scripts:
        logger.warning("observed_chars/script_stats empty; rebuilding from toponyms table (one-time scan)...")
        observed_chars, observed_scripts = rebuild_vocab_stats_from_toponyms(conn)
        if not observed_chars or not observed_scripts:
            raise RuntimeError("Vocabulary rebuild failed: no observed characters/scripts found in toponyms table")

    logger.info(f"Observed {len(observed_chars):,} unique characters across {len(observed_scripts)} scripts")

    vocab = {
        '<PAD>': 0,
        '<UNK>': 1,
        '<SPACE>': 2,
    }
    next_id = 10  # Reserve 3-9

    included_scripts = set()
    script_char_counts = defaultdict(int)

    # 1. ASCII printable range (32-126)
    logger.info("Adding ASCII printable range...")
    for cp in range(32, 127):
        char = chr(cp)
        if char not in vocab and char != ' ':
            vocab[char] = next_id
            next_id += 1
            script_char_counts['ASCII'] += 1

    # 2. Full Unicode ranges for ALL observed scripts
    logger.info("Expanding to full Unicode ranges for observed scripts...")
    for script_name in observed_scripts:
        try:
            script = Script(script_name)
        except ValueError:
            logger.warning(f"Unknown script in data: {script_name}")
            continue

        if script not in SCRIPT_RANGES:
            logger.warning(f"No Unicode ranges defined for {script_name}")
            continue

        included_scripts.add(script_name)
        count_before = len(vocab)

        for start, end in SCRIPT_RANGES[script]:
            for cp in range(start, end + 1):
                try:
                    char = chr(cp)
                    cat = unicodedata.category(char)
                    if cat.startswith('C'):
                        continue
                    if char not in vocab:
                        vocab[char] = next_id
                        next_id += 1
                except (ValueError, OverflowError):
                    continue

        count_added = len(vocab) - count_before
        script_char_counts[script_name] = count_added
        logger.info(f"  {script_name}: {count_added:,} characters")

    # 3. Remaining observed characters not yet in vocab (edge cases)
    for char, char_script_counts in observed_chars.items():
        if char not in vocab and char.strip():
            vocab[char] = next_id
            next_id += 1
            best_script = max(char_script_counts, key=lambda x: x[1])[0] if char_script_counts else 'OTHER'
            script_char_counts[best_script] += 1

    # Save vocabulary
    vocab_data = {
        'version': 4,  # v4: native script (no romanization/decomposition)
        'char_to_id': vocab,
        'observed_scripts': list(observed_scripts),
        'included_scripts': list(included_scripts),
        'stats': {
            'total_chars': len(vocab),
            'by_script': dict(script_char_counts),
        }
    }

    vocab_path = output_dir / 'char_vocab.json'
    with open(vocab_path, 'w', encoding='utf-8') as f:
        json.dump(vocab_data, f, ensure_ascii=False, indent=2)

    logger.info(f"Vocabulary saved: {vocab_path}")
    logger.info(f"Total vocabulary size: {len(vocab):,}")

    # Language vocabulary
    languages = sorted(row[0] for row in conn.execute(
        "SELECT DISTINCT lang FROM toponyms WHERE lang IS NOT NULL AND lang != ''"
    ).fetchall())

    lang_vocab = {'<UNK>': 0}
    for i, lang in enumerate(languages, start=1):
        lang_vocab[lang] = i

    lang_path = output_dir / 'lang_vocab.json'
    with open(lang_path, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'lang_to_id': lang_vocab}, f, ensure_ascii=False, indent=2)

    logger.info(f"Language vocabulary saved: {lang_path} ({len(lang_vocab):,} languages)")

    # Script vocabulary
    script_vocab = {s.value: i for i, s in enumerate(Script)}
    script_path = output_dir / 'script_vocab.json'
    with open(script_path, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'script_to_id': script_vocab}, f, indent=2)

    logger.info(f"Script vocabulary saved: {script_path}")

    return vocab_data['stats'], vocab


def compute_split_hash(toponym_id: str, num_buckets: int = 1000) -> int:
    """Compute deterministic hash for train/val/test splitting."""
    hash_bytes = hashlib.md5(toponym_id.encode()).digest()
    hash_int = int.from_bytes(hash_bytes[:4], 'big')
    return hash_int % num_buckets


def assign_split(
        split_hash: int,
        train_ratio: float = 0.8,
        val_ratio: float = 0.1,
        num_buckets: int = 1000,
) -> str:
    """Assign split based on hash value."""
    train_cutoff = int(train_ratio * num_buckets)
    val_cutoff = train_cutoff + int(val_ratio * num_buckets)
    if split_hash < train_cutoff:
        return 'train'
    elif split_hash < val_cutoff:
        return 'val'
    return 'test'


# Parquet schema for training data
TRAINING_SCHEMA = pa.schema([
    ('toponym_id', pa.string()),
    ('name', pa.string()),
    ('script', pa.string()),
    ('lang', pa.string()),
    ('char_ids', pa.list_(pa.int16())),
    ('char_length', pa.int16()),
    ('ipa', pa.string()),
    ('features', pa.list_(pa.float32())),
    ('feature_length', pa.int16()),
    ('namespaces', pa.list_(pa.string())),
    ('attestations', pa.list_(pa.string())),
    ('split', pa.string()),
])


def export_training_parquet(
    conn,
    vocab: Dict[str, int],
    output_dir: Path,
    namespaces: List[str],
    train_ratio: float = 0.8,
    val_ratio: float = 0.1,
    batch_size: int = 100000,
) -> Dict:
    """
    Export training data to partitioned Parquet files.
    """
    logger.info("=" * 60)
    logger.info("Exporting training data to Parquet")
    logger.info("=" * 60)
    logger.info(f"Namespaces: {namespaces}")

    parquet_dir = output_dir / 'training'
    parquet_dir.mkdir(parents=True, exist_ok=True)

    ns_placeholders = ','.join('?' * len(namespaces))

    query = f'''
        SELECT t.toponym_id,
               t.name,
               t.script,
               t.lang,
               t.ipa,
               t.panphon_features,
               GROUP_CONCAT(DISTINCT tn.namespace) as namespaces,
               GROUP_CONCAT(DISTINCT ta.place_id) as attestations
        FROM toponyms t
        JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
        LEFT JOIN toponym_attestations ta ON t.toponym_id = ta.toponym_id
        WHERE EXISTS (
            SELECT 1 FROM toponym_namespaces tn2
            WHERE tn2.toponym_id = t.toponym_id
            AND tn2.namespace IN ({ns_placeholders})
        )
        GROUP BY t.toponym_id, t.name, t.script, t.lang, t.ipa, t.panphon_features
    '''

    result = conn.execute(query, namespaces)

    count_cursor = conn.execute(f'''
        SELECT COUNT(DISTINCT t.toponym_id)
        FROM toponyms t
        JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
        WHERE tn.namespace IN ({ns_placeholders})
    ''', namespaces)
    total_count = count_cursor.fetchone()[0]
    logger.info(f"Processing {total_count:,} toponyms...")

    stats = {
        'total_exported': 0,
        'with_ipa': 0,
        'with_features': 0,
        'by_script': Counter(),
        'by_split': Counter(),
        'by_namespace': Counter(),
    }

    buffers: Dict[str, List[Dict]] = defaultdict(list)
    part_counts: Dict[str, int] = defaultdict(int)

    def encode_chars(name: str) -> List[int]:
        """Encode characters using vocabulary (native script, lowercased)."""
        ids = []
        for char in name.lower():
            if char == ' ':
                ids.append(2)
            elif char in vocab:
                ids.append(vocab[char])
            else:
                ids.append(1)
        return ids

    def unpack_features(packed: bytes) -> Optional[List[float]]:
        if not packed:
            return None
        num_floats = len(packed) // 4
        return list(struct.unpack(f'{num_floats}f', packed))

    def flush_buffer(script: str):
        records = buffers[script]
        if not records:
            return

        script_dir = parquet_dir / f'script={script}'
        script_dir.mkdir(parents=True, exist_ok=True)

        part_num = part_counts[script]
        part_counts[script] += 1

        output_path = script_dir / f'part-{part_num:04d}.parquet'
        table = pa.Table.from_pylist(records, schema=TRAINING_SCHEMA)
        pq.write_table(table, output_path, compression='snappy')

        logger.debug(f"Wrote {len(records)} records to {output_path}")
        buffers[script] = []

    processed = 0
    last_log = 0

    fetch_batch_size = 10000
    while True:
        rows = result.fetchmany(fetch_batch_size)
        if not rows:
            break

        for row in rows:
            toponym_id, name, script, lang, ipa, panphon_packed, namespaces_str, attestations_str = row

            namespaces_list = namespaces_str.split(',') if namespaces_str else []
            attestations_list = attestations_str.split(',') if attestations_str else []

            char_ids = encode_chars(name)

            features = unpack_features(panphon_packed)
            feature_length = len(features) // 24 if features else 0

            if ipa:
                stats['with_ipa'] += 1
            if features:
                stats['with_features'] += 1

            split_hash = compute_split_hash(toponym_id)
            split = assign_split(split_hash, train_ratio, val_ratio)

            record = {
                'toponym_id': toponym_id,
                'name': name,
                'script': script,
                'lang': lang or '',
                'char_ids': char_ids,
                'char_length': len(char_ids),
                'ipa': ipa,
                'features': features,
                'feature_length': feature_length,
                'namespaces': namespaces_list,
                'attestations': attestations_list,
                'split': split,
            }

            buffers[script].append(record)
            stats['total_exported'] += 1
            stats['by_script'][script] += 1
            stats['by_split'][split] += 1
            for ns in namespaces_list:
                if ns in namespaces:
                    stats['by_namespace'][ns] += 1

            if len(buffers[script]) >= batch_size:
                flush_buffer(script)

            processed += 1
            if processed - last_log >= 100000:
                logger.info(f"Processed {processed:,} / {total_count:,} ({100*processed/total_count:.1f}%)")
                last_log = processed

    for script in list(buffers.keys()):
        flush_buffer(script)

    # Log statistics
    logger.info(f"Exported {stats['total_exported']:,} toponyms to Parquet")
    logger.info(f"With IPA: {stats['with_ipa']:,} ({100*stats['with_ipa']/stats['total_exported']:.1f}%)")
    logger.info(f"With features: {stats['with_features']:,} ({100*stats['with_features']/stats['total_exported']:.1f}%)")
    logger.info(f"By script: {dict(stats['by_script'].most_common(10))}")
    logger.info(f"By split: {dict(stats['by_split'])}")
    logger.info(f"By namespace: {dict(stats['by_namespace'])}")

    # Save split IDs
    splits_dir = output_dir / 'splits'
    splits_dir.mkdir(parents=True, exist_ok=True)

    for split_name in ['train', 'val', 'test']:
        cursor = conn.execute(f'''
            SELECT DISTINCT t.toponym_id FROM toponyms t
            JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
            WHERE tn.namespace IN ({ns_placeholders})
        ''', namespaces)

        split_ids = []
        for (tid,) in cursor:
            if assign_split(compute_split_hash(tid), train_ratio, val_ratio) == split_name:
                split_ids.append(tid)

        with open(splits_dir / f'{split_name}_ids.txt', 'w') as f:
            f.write('\n'.join(split_ids))
        logger.info(f"Saved {len(split_ids):,} {split_name} IDs")

    stats_path = output_dir / 'export_stats.json'
    with open(stats_path, 'w') as f:
        json.dump({
            'total_exported': stats['total_exported'],
            'with_ipa': stats['with_ipa'],
            'with_features': stats['with_features'],
            'by_script': dict(stats['by_script']),
            'by_split': dict(stats['by_split']),
            'by_namespace': dict(stats['by_namespace']),
            'namespaces_included': namespaces,
            'train_ratio': train_ratio,
            'val_ratio': val_ratio,
        }, f, indent=2)

    return stats


def dump_to_jsonl(
    conn,
    output_path: Path,
    training_namespaces: List[str] = None,
    num_workers: int = None,
    batch_size: int = 10000,
) -> Tuple[int, Dict]:
    """
    Dump aggregated documents to a flat JSONL file on Scratch.

    Uses streaming batch architecture for O(1) memory usage regardless of corpus size.

    For toponyms in training_namespaces, computes (in parallel):
    - IPA transcription via Epitran / Phonikud / CharsiuG2P
    - PanPhon embedding (192-dim position-pooled articulatory features) for ES
    - Full PanPhon feature sequence stored in DuckDB for training data assembly

    Returns:
        Tuple of (total_count, stats_dict)
    """
    if training_namespaces is None:
        training_namespaces = ['gn', 'wd', 'tgn']

    if num_workers is None:
        num_workers = max(1, mp.cpu_count() - 2)

    training_ns_set = set(training_namespaces)

    processing_batch_size = 50000
    io_batch_size = 5000

    # Specialized worker pool configuration to minimize memory overhead
    # Each pool loads only its required models
    charsiu_workers = min(5, max(1, num_workers // 12))    # ~8% for CJK/Korean
    phonikud_workers = min(2, max(1, num_workers // 30))   # ~3% for Hebrew
    epitran_workers = num_workers - charsiu_workers - phonikud_workers

    chunksize = max(100, processing_batch_size // num_workers // 4)

    logger.info(f"Buffering documents to disk: {output_path}")
    logger.info(f"Computing PanPhon embeddings for namespaces: {training_namespaces}")
    logger.info(f"Specialized worker pool allocation:")
    logger.info(f"  Charsiu (gan/wuu/yue/ko): {charsiu_workers} workers")
    logger.info(f"  Phonikud (Hebrew): {phonikud_workers} workers")
    logger.info(f"  Epitran (all others): {epitran_workers} workers")
    logger.info(f"Processing batch size: {processing_batch_size:,}, I/O batch size: {io_batch_size:,}")
    logger.info(f"Worker chunksize: {chunksize}")

    total_count = conn.execute('SELECT COUNT(*) FROM toponyms').fetchone()[0]
    logger.info(f"Total toponyms to process: {total_count:,}")

    stats = {
        'total': 0,
        'in_training_ns': 0,
        'with_ipa': 0,
        'with_panphon': 0,
        'by_script': Counter(),
        'by_script_lang_ipa': Counter(),
    }

    result = conn.execute('''
        SELECT t.toponym_id,
               t.name,
               t.lang,
               t.lang_variant,
               t.script,
               GROUP_CONCAT(DISTINCT tn.namespace) as namespaces,
               GROUP_CONCAT(DISTINCT ta.place_id) as attestations
        FROM toponyms t
        JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
        LEFT JOIN toponym_attestations ta ON t.toponym_id = ta.toponym_id
        GROUP BY t.toponym_id, t.name, t.lang, t.lang_variant, t.script
    ''')

    # Accumulate IPA/PanPhon updates during streaming, apply after cursor is done
    all_db_updates = []

    logger.info("Starting streaming from DuckDB with Producer-Consumer pipeline...")

    # Create specialized worker pools - each loads only required models
    with open(output_path, 'w', encoding='utf-8') as f, \
         mp.Pool(processes=charsiu_workers, initializer=_init_worker, initargs=('charsiu',)) as charsiu_pool, \
         mp.Pool(processes=phonikud_workers, initializer=_init_worker, initargs=('phonikud',)) as phonikud_pool, \
         mp.Pool(processes=epitran_workers, initializer=_init_worker, initargs=('epitran',)) as epitran_pool:

        fetch_batch_size = processing_batch_size
        batches_processed = 0
        pending_writes = []

        def flush_writes():
            nonlocal pending_writes
            for doc in pending_writes:
                f.write(json.dumps(doc) + '\n')
            pending_writes = []

        while True:
            rows = result.fetchmany(fetch_batch_size)
            if not rows:
                break

            # Phase 1: Build documents and identify those needing phonetics
            batch_docs = []
            charsiu_work = []   # CJK topolects and Korean
            phonikud_work = []  # Hebrew
            epitran_work = []   # Everything else

            for row in rows:
                toponym_id, name, lang, lang_variant, script, namespaces_str, attestations_str = row
                namespaces = namespaces_str.split(',') if namespaces_str else []
                attestations = attestations_str.split(',') if attestations_str else []
                primary_ns = get_primary_namespace(namespaces)

                doc = {
                    'toponym_id': toponym_id,
                    'name': name,
                    'lang': lang,
                    'lang_variant': lang_variant,
                    'script': script,
                    'namespaces': namespaces,
                    'primary_namespace': primary_ns,
                    'attestations': attestations,
                    'embedding': None,
                    'embedding_version': None,
                    'indexed_at': datetime.now(timezone.utc).isoformat(),
                }

                # Romanized form for ES text search (not stored in DuckDB)
                name_romanized = romanize_for_search(name, script)
                if name_romanized:
                    doc['name_romanized'] = name_romanized

                stats['by_script'][script] += 1

                is_in_training_ns = bool(training_ns_set & set(namespaces))
                if is_in_training_ns:
                    stats['in_training_ns'] += 1
                    # Route to appropriate worker pool based on language
                    work_item = (toponym_id, name, lang, script)
                    if lang in ('gan', 'wuu', 'yue', 'ko'):
                        charsiu_work.append(work_item)
                    elif lang == 'he':
                        phonikud_work.append(work_item)
                    else:
                        epitran_work.append(work_item)

                batch_docs.append(doc)
                stats['total'] += 1

            # Phase 2: Parallel phonetics computation using specialized pools
            phonetics_results = {}

            # Process each pool's work separately
            for work_queue, pool, pool_name in [
                (charsiu_work, charsiu_pool, 'Charsiu'),
                (phonikud_work, phonikud_pool, 'Phonikud'),
                (epitran_work, epitran_pool, 'Epitran')
            ]:
                if work_queue:
                    sub_batch_size = max(100, len(work_queue) // (pool._processes if hasattr(pool, '_processes') else 1))
                    sub_batches = [
                        work_queue[i:i + sub_batch_size]
                        for i in range(0, len(work_queue), sub_batch_size)
                    ]

                    for batch_results in pool.imap_unordered(_compute_phonetics_for_batch, sub_batches, chunksize=1):
                        for toponym_id, ipa, packed_features, embedding in batch_results:
                            phonetics_results[toponym_id] = (ipa, packed_features, embedding)

            # Phase 3: Merge results and queue for writing
            current_db_updates = []
            for doc in batch_docs:
                toponym_id = doc['toponym_id']

                if toponym_id in phonetics_results:
                    ipa, packed_features, embedding = phonetics_results[toponym_id]
                    doc['ipa'] = ipa
                    doc['panphon_embedding'] = embedding
                    stats['with_ipa'] += 1
                    stats['with_panphon'] += 1
                    stats['by_script_lang_ipa'][f"{doc['script']}:{doc['lang']}"] += 1
                    current_db_updates.append((ipa, packed_features, toponym_id))

                pending_writes.append(doc)

                if len(pending_writes) >= io_batch_size:
                    flush_writes()

            if current_db_updates:
                all_db_updates.extend(current_db_updates)

            batches_processed += 1

            pct = 100 * stats['total'] / total_count
            logger.info(
                f"Progress: {stats['total']:,} / {total_count:,} ({pct:.1f}%) - "
                f"IPA: {stats['with_ipa']:,} - Batch {batches_processed}"
            )

        # Flush remaining writes
        if pending_writes:
            flush_writes()

    logger.info(f"Streaming complete. Total batches processed: {batches_processed}")

    # Apply accumulated DuckDB updates in batches
    # (After streaming loop to avoid cursor invalidation)
    total_db_updates = 0
    if all_db_updates:
        logger.info(f"Applying {len(all_db_updates):,} IPA/PanPhon updates to DuckDB...")

        # Drop indexes for faster bulk updates
        logger.info("Dropping indexes on toponyms table for faster bulk updates...")
        try:
            conn.execute("DROP INDEX IF EXISTS idx_toponyms_id")
            conn.execute("DROP INDEX IF EXISTS idx_toponyms_script")
            conn.execute("DROP INDEX IF EXISTS idx_toponyms_lang")
            logger.info("Indexes dropped. Starting bulk updates...")
        except Exception as e:
            logger.warning(f"Could not drop indexes (may not exist): {e}")

        update_batch_size = 100000
        for i in range(0, len(all_db_updates), update_batch_size):
            batch = all_db_updates[i:i + update_batch_size]
            ipas, features, ids = zip(*batch)
            update_table = pa.table({
                'ipa': ipas,
                'panphon_features': features,
                'toponym_id': ids
            })
            conn.execute("CREATE TEMP TABLE updates AS SELECT * FROM update_table")
            conn.execute("""
                UPDATE toponyms
                SET ipa = u.ipa, panphon_features = u.panphon_features
                FROM updates u
                WHERE toponyms.toponym_id = u.toponym_id
            """)
            conn.execute("DROP TABLE updates")

            if (i + update_batch_size) % 500000 == 0:
                logger.info(f"  Updated {i + len(batch):,} / {len(all_db_updates):,} records")

        total_db_updates = len(all_db_updates)
        logger.info(f"DuckDB updates complete: {total_db_updates:,} records")

        # Rebuild indexes
        logger.info("Rebuilding indexes on toponyms table...")
        conn.execute('CREATE INDEX IF NOT EXISTS idx_toponyms_id ON toponyms(toponym_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_toponyms_script ON toponyms(script)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_toponyms_lang ON toponyms(lang)')
        logger.info("Indexes rebuilt.")

    logger.info(f"JSONL export complete: {stats['total']:,} documents written")

    # Log final statistics
    logger.info(f"Buffering complete. Total documents: {stats['total']:,}")
    logger.info(f"Documents in training namespaces: {stats['in_training_ns']:,}")
    logger.info(f"  With IPA: {stats['with_ipa']:,} ({100*stats['with_ipa']/max(1,stats['in_training_ns']):.1f}%)")
    logger.info(f"  With PanPhon embedding: {stats['with_panphon']:,} ({100*stats['with_panphon']/max(1,stats['in_training_ns']):.1f}%)")

    logger.info("Top script+lang pairs with IPA transcription:")
    for key, count in stats['by_script_lang_ipa'].most_common(20):
        logger.info(f"  {key}: {count:,}")

    return stats['total'], stats


def yield_from_jsonl(file_path: Path) -> Iterator[Dict]:
    """Reads the JSONL file for the ES Bulk loader."""
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line:
                yield json.loads(line)


def bulk_index_from_file(
        es: Elasticsearch,
        jsonl_path: Path,
        total_docs: int,
        index: str,
        batch_size: int = 2500
) -> int:
    """
    Stream from JSONL to Elasticsearch. Purely I/O bound and stable.
    """
    logger.info(f"Bulk indexing {total_docs:,} documents from file...")

    def generate_actions():
        for doc in yield_from_jsonl(jsonl_path):
            yield {
                '_index': index,
                '_id': doc['toponym_id'],
                '_source': doc,
            }

    indexed = 0
    errors = 0

    iterator = helpers.parallel_bulk(
        es,
        generate_actions(),
        thread_count=4,
        queue_size=8,
        chunk_size=batch_size,
        raise_on_error=False,
        request_timeout=120
    )

    if tqdm:
        iterator = tqdm(iterator, total=total_docs, desc="Indexing", mininterval=5.0)

    for success, info in iterator:
        if success:
            indexed += 1
        else:
            errors += 1
            if errors <= 5:
                logger.error(f"Error: {info}")

    return indexed


# --- MAIN ---

def main():
    parser = argparse.ArgumentParser(
        description='v6 Pipeline Phase 1: Rebuild ES toponyms index with PanPhon embeddings'
    )
    parser.add_argument('--es-host', default=ES_HOST)
    parser.add_argument('--places-index', default='places')
    parser.add_argument('--toponyms-index', default='toponyms')
    parser.add_argument('--schema-path', type=Path, default=SCHEMA_PATH)
    parser.add_argument('--db-path', type=Path, default=f'{IX1_BASE}/data/toponyms.db',
                        help='Path for DuckDB database file')
    parser.add_argument('--output-dir', type=Path, default=None,
                        help='Output directory for vocab and stats (default: db-path parent)')
    parser.add_argument('--scratch-dir', type=Path, default=None)
    parser.add_argument('--batch-size', type=int, default=2500)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--resume', action='store_true', help="Resume from existing DuckDB database")
    parser.add_argument('--skip-es-index', action='store_true',
                        help="Skip ES indexing (only extract to DuckDB and generate vocab)")

    # Parallelism options
    parser.add_argument('--num-workers', type=int, default=None,
                        help='Number of parallel workers for IPA/PanPhon (default: CPU count - 2)')

    # Training namespace options
    parser.add_argument('--training-namespaces', nargs='+', default=['gn', 'wd', 'tgn'],
                        help='Namespaces for which to compute PanPhon embeddings (default: gn wd tgn)')

    args = parser.parse_args()

    # Force HuggingFace to use local scratch for cache
    if args.scratch_dir:
        os.environ['HF_HOME'] = str(args.scratch_dir / 'hf_cache')
        os.environ['TRANSFORMERS_CACHE'] = str(args.scratch_dir / 'hf_cache')

    # Determine number of workers
    if args.num_workers is None:
        args.num_workers = max(1, mp.cpu_count() - 2)

    if not args.confirm:
        print("=" * 60)
        print("v6 PIPELINE - PHASE 1: EXTRACTION & ES INDEXING")
        print("=" * 60)
        print()
        print("Run with --confirm to proceed.")
        print()
        print("This script will:")
        print("  1. Scan all places to extract toponyms with attestations")
        print("  2. Filter out pre-romanized forms (lang-script mismatches)")
        print("  3. Generate expanded character vocabulary")
        print(f"  4. Compute IPA and PanPhon embeddings ({args.num_workers} parallel workers)")
        print("     IPA backends: Epitran (default), Phonikud (Hebrew), CharsiuG2P (zh/ko/yue/gan/wuu)")
        print("  5. Rebuild the ES toponyms index with panphon_embedding field")
        print("  6. Refresh index and create snapshot")
        print()
        print(f"Training namespaces (for PanPhon): {args.training_namespaces}")
        print()
        print("Next steps after this phase:")
        print("  - Run coverage analysis: es -analyse-coverage VERSION")
        print("  - Generate training pairs: es -generate-training-data VERSION")
        sys.exit(1)

    # Set output directory
    output_dir = args.output_dir or args.db_path.parent

    # Connect to ES
    es = Elasticsearch(args.es_host, max_retries=5, retry_on_timeout=True)
    if not es.ping():
        logger.error(f"Cannot connect to {args.es_host}")
        sys.exit(1)

    final_db_path = args.db_path
    final_db_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    vocab_dir = output_dir / 'vocab'

    logger.info(f"Using DuckDB database engine")
    logger.info(f"Parallel workers: {args.num_workers}")

    with tempfile.TemporaryDirectory(dir=args.scratch_dir) as temp_dir:
        temp_dir_path = Path(temp_dir)
        temp_db_path = temp_dir_path / "toponyms_working.duckdb"
        jsonl_path = temp_dir_path / "buffer.jsonl"

        try:
            if args.resume and final_db_path.exists():
                logger.info(f"--- RESUMING from {final_db_path} ---")
                logger.info(f"Copying existing DB to scratch: {temp_db_path}")
                shutil.copy2(final_db_path, temp_db_path)
            else:
                # STEP 1: EXTRACTION (ES places -> DuckDB)
                logger.info("=" * 60)
                logger.info("STEP 1: EXTRACTION (ES places -> DuckDB)")
                logger.info("=" * 60)

                conn = create_db(str(temp_db_path))

                places_count, toponyms_count, skipped_count = extract_toponyms_to_db(
                    es, conn, args.places_index, args.batch_size, args.limit
                )
                logger.info(f"Extracted {toponyms_count:,} toponyms from {places_count:,} places")
                logger.info(f"Skipped {skipped_count:,} pre-romanized/mismatched toponyms")

                # Checkpoint DB to persistent storage
                logger.info("--- Checkpointing database to persistent storage ---")
                conn.close()
                shutil.copy2(temp_db_path, final_db_path)

            # Reopen DB from scratch (whether resumed or just created)
            conn = create_db(str(temp_db_path))
            optimize_db_after_load(conn)

            # STEP 2: VOCABULARY GENERATION
            logger.info("=" * 60)
            logger.info("STEP 2: VOCABULARY GENERATION")
            logger.info("=" * 60)
            vocab_stats, char_vocab = generate_vocabulary(conn, vocab_dir)
            logger.info(f"Vocabulary stats: {json.dumps(vocab_stats, indent=2)}")

            # STEP 3: BUFFER TO JSONL WITH PANPHON (DuckDB -> scratch)
            # Always regenerate — ensures consistent phonetics across all languages
            logger.info("=" * 60)
            logger.info("STEP 3: BUFFERING WITH PANPHON (DuckDB -> JSONL)")
            logger.info("=" * 60)
            total_docs, buffer_stats = dump_to_jsonl(
                conn,
                jsonl_path,
                training_namespaces=args.training_namespaces,
                num_workers=args.num_workers,
            )
            conn.close()

            # CRITICAL: Checkpoint DB with PanPhon features to persistent storage
            logger.info("--- Checkpointing DuckDB with PanPhon features to persistent storage ---")
            shutil.copy2(temp_db_path, final_db_path)
            logger.info(f"DuckDB saved to: {final_db_path}")

            # Save coverage stats for analysis
            coverage_stats_path = output_dir / 'coverage_stats.json'
            with open(coverage_stats_path, 'w') as f:
                json.dump({
                    'total_toponyms': buffer_stats['total'],
                    'in_training_namespaces': buffer_stats['in_training_ns'],
                    'with_ipa': buffer_stats['with_ipa'],
                    'with_panphon_embedding': buffer_stats['with_panphon'],
                    'panphon_coverage_pct': 100 * buffer_stats['with_panphon'] / max(1, buffer_stats['in_training_ns']),
                    'by_script': dict(buffer_stats['by_script']),
                    'by_script_lang_ipa': dict(buffer_stats['by_script_lang_ipa'].most_common(100)),
                    'training_namespaces': args.training_namespaces,
                    'num_workers': args.num_workers,
                    'db_engine': 'DuckDB',
                    'ipa_backends': ['epitran', 'phonikud', 'charsiu_g2p'],
                }, f, indent=2)
            logger.info(f"Coverage stats saved to: {coverage_stats_path}")

            if args.skip_es_index:
                logger.info("Skipping ES indexing (--skip-es-index)")
            else:
                # STEP 4: INDEXING (JSONL -> ES)
                logger.info("=" * 60)
                logger.info("STEP 4: INDEXING (JSONL -> ES)")
                logger.info("=" * 60)

                # Verify JSONL file
                logger.info("Verifying JSONL file...")
                jsonl_line_count = sum(1 for _ in open(jsonl_path, 'r', encoding='utf-8'))
                logger.info(f"JSONL file contains {jsonl_line_count:,} lines")

                if jsonl_line_count != total_docs:
                    logger.warning(f"JSONL line count ({jsonl_line_count:,}) != expected ({total_docs:,})")
                    logger.warning("This may indicate incomplete processing. Proceeding anyway...")

                if jsonl_line_count < 1000:
                    logger.error(f"JSONL file has only {jsonl_line_count} lines - this seems too few!")
                    raise RuntimeError(f"JSONL file appears incomplete: only {jsonl_line_count} lines")

                # Delete existing index
                if es.indices.exists(index=args.toponyms_index):
                    logger.info(f"Deleting existing index: {args.toponyms_index}")
                    es.indices.delete(index=args.toponyms_index)

                # Create index with schema
                with open(args.schema_path, 'r') as f:
                    schema = json.load(f)

                if 'settings' not in schema:
                    schema['settings'] = {}
                schema['settings']['refresh_interval'] = "-1"

                logger.info(f"Creating index: {args.toponyms_index}")
                es.indices.create(index=args.toponyms_index, body=schema)

                # Bulk index
                indexed = bulk_index_from_file(
                    es, jsonl_path, total_docs, args.toponyms_index, args.batch_size
                )
                logger.info(f"Indexed {indexed:,} documents")

                # STEP 5: FINALIZE (refresh + snapshot)
                logger.info("=" * 60)
                logger.info("STEP 5: FINALIZE (refresh + snapshot)")
                logger.info("=" * 60)

                logger.info("Re-enabling refresh interval...")
                es.indices.put_settings(
                    index=args.toponyms_index,
                    body={"index": {"refresh_interval": "1s"}}
                )

                logger.info("Refreshing index...")
                es.indices.refresh(index=args.toponyms_index)

                final_count = es.count(index=args.toponyms_index)['count']
                logger.info(f"Final document count: {final_count:,}")

                logger.info("Creating snapshot...")
                snapshot_name = "toponyms_v6"
                create_checkpoint_snapshot(
                    es,
                    snapshot_name=snapshot_name,
                    repo_name=STAGING_REPO_NAME
                )
                logger.info(f"Snapshot created: {snapshot_name}")

            # Final summary
            logger.info("=" * 60)
            logger.info("PHASE 1 COMPLETE")
            logger.info("=" * 60)
            logger.info(f"DuckDB database: {final_db_path}")
            logger.info(f"Output directory: {output_dir}")
            logger.info(f"  - Vocabulary: {vocab_dir}")
            logger.info(f"  - Coverage stats: {coverage_stats_path}")
            if not args.skip_es_index:
                logger.info(f"ES index: {args.toponyms_index}")
                logger.info(f"  - Total documents: {final_count:,}")
                logger.info(f"  - With PanPhon embeddings: {buffer_stats['with_panphon']:,}")
            logger.info("")
            logger.info("Next steps:")
            logger.info("  1. Review coverage stats")
            logger.info("  2. Generate training pairs: es -generate-training-data VERSION")

        except Exception as e:
            logger.error(f"Process failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            sys.exit(1)


if __name__ == '__main__':
    main()
