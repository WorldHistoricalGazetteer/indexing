"""
Rebuild the ES toponyms index with vocabulary generation and PanPhon embeddings.

Phase 1: Extraction and ES Indexing

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
"""
import time
# Suppress warnings early - before any imports that might trigger them
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module='epitran')
warnings.filterwarnings("ignore", category=UserWarning, module='pkg_resources')
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

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
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Dict, List, Optional, Tuple, Set

import numpy as np

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
    print("Warning: anyascii not installed. CJK romanization unavailable.")

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.utils.script_detection import (
    Script, detect_script, get_primary_namespace,
    SCRIPT_RANGES, should_romanize, should_decompose
)
from phonetics.utils.korean import decompose_text
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
    ('he', Script.HEBREW): 'heb-Hebr',
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
    ('ko', Script.HANGUL): 'kor-Hang',
    ('zh', Script.CJK): 'cmn-Hans',
    ('ja', Script.HIRAGANA): 'jpn-Hira',
}


class IPAConverter:
    """Lazy-loaded IPA converter using Epitran, PanPhon, Charsiu, and Phonikud."""

    def __init__(self):
        self._epitran_cache: Dict[str, object] = {}
        self._panphon_ft = None
        self._epitran_available = None
        self._panphon_available = None
        self._charsiu_g2p = None
        self._phonikud = None

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

    def _check_charsiu(self) -> bool:
        if self._charsiu_g2p is None:
            try:
                # Check for protobuf first
                try:
                    import google.protobuf
                except ImportError:
                    logger.warning("CharsiuG2P requires protobuf library. Install with: pip install protobuf")
                    self._charsiu_g2p = False
                    return False

                # Based on CharsiuG2P typical usage
                import torch
                import transformers
                import warnings
                import os

                # Suppress the tokenizer class warning as we're intentionally using ByT5Tokenizer
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=FutureWarning)

                    # Single multilingual model handles all languages including Chinese topolects
                    model = transformers.T5ForConditionalGeneration.from_pretrained("charsiu/g2p_multilingual_byT5_small_100")
                    # Use ByT5Tokenizer instead of T5Tokenizer for byte-level models
                    tokenizer = transformers.ByT5Tokenizer.from_pretrained("google/byt5-small")

                device = "cuda" if torch.cuda.is_available() else "cpu"
                model.to(device)

                class CharsiuWrapper:
                    def __init__(self, model, tokenizer, device):
                        self.model = model
                        self.tokenizer = tokenizer
                        self.device = device

                    def transliterate(self, text, lang):
                        # Mapping languages to ISO codes expected by the model
                        # Chinese topolects and other languages
                        # Note: gan and wuu use Mandarin (cmn) as a proxy because:
                        # 1. They share the same character set
                        # 2. Symphonym learns similarity patterns, not precise phonetics
                        # 3. Even approximate phonetics provide useful training signal
                        # This is acceptable for similarity-based matching but should be
                        # documented as a limitation for these minority topolects.
                        lang_map = {
                            'ko': 'kor',      # Korean
                            'ja': 'jpn',      # Japanese
                            'gan': 'cmn',     # Gan (use Mandarin as proxy)
                            'wuu': 'cmn',     # Wu (use Mandarin as proxy)
                            'yue': 'yue'      # Cantonese
                        }
                        char_iso = lang_map.get(lang, lang)
                        input_text = f"<{char_iso}>: {text}"
                        inputs = self.tokenizer(input_text, return_tensors="pt").to(self.device)
                        with torch.no_grad():
                            outputs = self.model.generate(**inputs)
                        return self.tokenizer.decode(outputs[0], skip_special_tokens=True)

                self._charsiu_g2p = CharsiuWrapper(model, tokenizer, device)
                # Only log from main process (PID 1 or when not in worker pool)
                if os.getenv('_REBUILD_MAIN_PROCESS') == '1':
                    logger.info("CharsiuG2P initialized for Chinese topolects and Korean")
            except Exception as e:
                logger.warning(f"Failed to initialize CharsiuG2P: {e}")
                self._charsiu_g2p = False
        # Return whether we have a valid instance (but don't log on subsequent calls)
        return self._charsiu_g2p is not False

    def _check_phonikud(self) -> bool:
        if self._phonikud is None:
            try:
                import warnings
                import os
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message=".*tokenizer class.*")
                    import phonikud as phonikud_module

                class PhonikudWrapper:
                    def __init__(self, phonikud_module):
                        self.phonikud = phonikud_module

                    def transliterate(self, text):
                        return self.phonikud.phonemize(text)

                self._phonikud = PhonikudWrapper(phonikud_module)
                # Only log from main process
                if os.getenv('_REBUILD_MAIN_PROCESS') == '1':
                    logger.info("Phonikud initialized for Hebrew G2P")
            except (ImportError, Exception) as e:
                logger.warning(f"Phonikud not available or failed to initialize: {e}")
                self._phonikud = False
        # Return whether we have a valid instance (but don't log on subsequent calls)
        return self._phonikud is not False

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

    def to_ipa(self, text: str, lang: str, script: Script) -> Optional[str]:
        # Hebrew (Phonikud)
        if lang == 'he' and script == Script.HEBREW:
            if self._check_phonikud() and self._phonikud:
                try:
                    return self._phonikud.transliterate(text)
                except Exception:
                    pass

        # Chinese Topolects (Charsiu)
        if lang in ('gan', 'wuu', 'yue') and script == Script.CJK:
            if self._check_charsiu() and self._charsiu_g2p:
                try:
                    return self._charsiu_g2p.transliterate(text, lang)
                except Exception:
                    pass

        # Korean (Charsiu as preference for both Hangul and Hanja)
        if lang == 'ko' and script in (Script.HANGUL, Script.CJK):
            if self._check_charsiu() and self._charsiu_g2p:
                try:
                    return self._charsiu_g2p.transliterate(text, lang)
                except Exception:
                    pass

        # Default to Epitran
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
                # Compute which bin this segment falls into
                # Position is normalized to [0, 1), then mapped to bin index
                position = seg_idx / num_segments
                bin_idx = min(int(position * num_bins), num_bins - 1)

                # Add segment features to the bin
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


# Global worker context - cached per process to avoid re-initialization
_WORKER_CONVERTER = None


def _init_worker():
    """Initialize worker process with cached converter and suppressed warnings."""
    global _WORKER_CONVERTER
    import warnings
    import os
    warnings.filterwarnings("ignore", category=UserWarning, module='epitran')
    warnings.filterwarnings("ignore", category=UserWarning, module='pkg_resources')
    warnings.filterwarnings("ignore", message=".*tokenizer class.*")
    # Mark this as a worker process (not main) to suppress initialization logging
    if '_REBUILD_MAIN_PROCESS' in os.environ:
        del os.environ['_REBUILD_MAIN_PROCESS']
    _WORKER_CONVERTER = IPAConverter()


def _get_worker_converter() -> IPAConverter:
    """Get the cached converter for this worker process."""
    global _WORKER_CONVERTER
    if _WORKER_CONVERTER is None:
        _init_worker()
    return _WORKER_CONVERTER


def _preload_models():
    """
    Pre-load all models in the main process before spawning workers.

    This ensures models are cached and workers can load them from cache
    rather than 46 workers simultaneously hitting the filesystem/network.

    Call this BEFORE creating the multiprocessing pool.
    """
    logger.info("Pre-loading models in main process to avoid worker contention...")
    preload_start = time.time()

    # Create a temporary converter to trigger all lazy loading
    converter = IPAConverter()

    # Force CharsiuG2P to load (the heavy one)
    logger.info("Pre-loading CharsiuG2P model (this may take 1-2 minutes)...")
    if converter._check_charsiu():
        logger.info("CharsiuG2P loaded successfully")
    else:
        logger.warning("CharsiuG2P failed to load - will skip Chinese/Korean/Japanese")

    # Force Phonikud to load
    logger.info("Pre-loading Phonikud model...")
    if converter._check_phonikud():
        logger.info("Phonikud loaded successfully")
    else:
        logger.warning("Phonikud failed to load - will skip Hebrew")

    # Force PanPhon to load
    logger.info("Pre-loading PanPhon...")
    if converter._check_panphon():
        logger.info("PanPhon loaded successfully")

    # Force Epitran to load for a common language
    logger.info("Pre-loading Epitran...")
    if converter._check_epitran():
        # Test with English to ensure it works
        test_epi = converter.get_epitran('eng-Latn')
        if test_epi:
            logger.info("Epitran loaded successfully")

    preload_elapsed = time.time() - preload_start
    logger.info(f"Model pre-loading completed in {preload_elapsed:.2f}s")

    # Return the converter so it stays in memory (helps with caching)
    return converter


def _compute_phonetics_for_batch(batch: List[Tuple]) -> Tuple:
    """
    Worker function for parallel IPA/PanPhon computation.

    Optimized for minimal serialization overhead - returns columnar data only.
    No per-row Python objects, no dicts, no Arrow in workers.

    Args:
        batch: List of (toponym_id, name, lang, script) tuples

    Returns:
        Tuple of columnar arrays: (ids, ipas, features, embeddings)
        - ids: list[str]
        - ipas: list[str]
        - features: list[bytes]
        - embeddings: np.ndarray (N, 192) dtype=float32
    """
    import numpy as np

    # REDUCED LOGGING - only log first batch per worker to avoid log spam
    pid = os.getpid()
    global _worker_batch_count

    # Initialize counter if this is first call in this worker
    try:
        _worker_batch_count += 1
    except NameError:
        _worker_batch_count = 1

    # Log every 100th batch from each worker
    if _worker_batch_count % 100 == 0:
        logger.info(f"Worker {pid} has processed {_worker_batch_count} batches")

    converter = _get_worker_converter()

    # Pre-allocate result containers (fixed size, contiguous memory)
    n = len(batch)

    ids = [None] * n
    ipas = [None] * n
    features = [None] * n
    embeddings = np.zeros((n, 192), dtype=np.float32)

    valid_idx = 0  # Track number of successful conversions

    # Tight loop - no exception propagation, no nested structures
    for i, (toponym_id, name, lang, script) in enumerate(batch):
        # Convert script
        try:
            script_enum = Script(script)
        except ValueError:
            script_enum = Script.OTHER

        # Compute IPA
        ipa = converter.to_ipa(name, lang, script_enum)
        if not ipa:
            continue

        # Compute features and embedding
        full_features = converter.to_features(ipa)
        embedding = converter.to_embedding(ipa)

        if not embedding:
            continue

        # Pack features to bytes (compact representation)
        if full_features:
            packed = struct.pack(f'{len(full_features)}f', *full_features)
        else:
            packed = None

        # Store in pre-allocated arrays
        ids[valid_idx] = toponym_id
        ipas[valid_idx] = ipa
        features[valid_idx] = packed
        embeddings[valid_idx] = embedding  # NumPy assignment (fast)
        valid_idx += 1

    # Trim to actual valid count (remove unused slots)
    ids = ids[:valid_idx]
    ipas = ipas[:valid_idx]
    features = features[:valid_idx]
    embeddings = embeddings[:valid_idx]  # NumPy slice (view, not copy)

    # Return columnar data only - no Python objects crossing boundaries
    return ids, ipas, features, embeddings


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
        # Unknown language - allow any script
        return False

    expected_scripts = LANG_EXPECTED_SCRIPTS[lang_base]

    # If the detected script is Latin but the language expects non-Latin,
    # this is likely a romanized form
    if script == Script.LATIN and Script.LATIN not in expected_scripts:
        return True

    return False


def create_db(db_path: str):
    """
    Create DuckDB database with optimized columnar storage.

    DuckDB advantages for this workload:
    - Columnar storage: better for analytical queries
    - Native Parquet export: direct export without serialization
    - Parallel query execution: automatic multi-core utilization
    - Better compression: ~30-50% smaller database files
    - Vectorized operations: faster aggregations and JOINs
    """
    conn = duckdb.connect(db_path)

    # Configure for bulk loading
    conn.execute("SET threads TO 16")
    conn.execute("SET memory_limit = '32GB'")

    # Note: No PRIMARY KEY during bulk loading for speed
    # Deduplication happens after loading via SQL
    conn.execute('''
        CREATE TABLE IF NOT EXISTS toponyms (
            toponym_id VARCHAR,
            name VARCHAR NOT NULL,
            name_romanized VARCHAR,
            lang VARCHAR,
            lang_variant VARCHAR,
            script VARCHAR,
            ipa VARCHAR,
            panphon_features BLOB,
            panphon_embedding FLOAT[]
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
    """Deduplicate and create indexes for DuckDB after bulk loading.

    Args:
        conn: DuckDB connection
        force: If True, always run deduplication. If False, skip if no duplicates exist.
    """

    # Check if deduplication is needed
    before_count = conn.execute("SELECT COUNT(*) FROM toponyms").fetchone()[0]
    distinct_count = conn.execute("SELECT COUNT(DISTINCT toponym_id) FROM toponyms").fetchone()[0]

    if before_count == distinct_count and not force:
        logger.info(f"Deduplication already done ({before_count:,} unique toponyms), skipping...")
    else:
        # Deduplicate toponyms table (keep first occurrence)
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
        # Composite index for efficient window functions
        conn.execute('CREATE INDEX IF NOT EXISTS idx_attestations_place_toponym ON toponym_attestations(place_id, toponym_id)')
        logger.info("DuckDB indexes created.")
    else:
        logger.info("DuckDB indexes already exist, skipping...")


def scan_places(es: Elasticsearch, index: str, batch_size: int = 2000) -> Iterator[Tuple[str, Dict]]:
    """Scan places index, yielding (place_id, source) tuples."""
    query = {"query": {"match_all": {}}, "_source": ["namespace", "toponyms"]}
    for doc in helpers.scan(es, index=index, query=query, scroll='60m', size=batch_size):
        yield doc['_id'], doc['_source']


def preprocess_for_vocab(name: str, script: Script) -> str:
    """Preprocess name for vocabulary extraction (romanize CJK, decompose Hangul)."""
    if should_romanize(script):
        if anyascii is None:
            return name.lower()  # Fallback if anyascii not available
        return anyascii(name).lower()
    if should_decompose(script):
        return decompose_text(name)
    return name.lower()


def generate_name_romanized(name: str, script: Script) -> Optional[str]:
    """
    Generate searchable romanized/decomposed form if different from original.

    Returns None if the name is already in a searchable form (Latin script).

    This enables cross-script search without embeddings:
    - Query "beijing" matches name_romanized for "北京"
    - Query "moskva" matches name_romanized for "Москва"
    """
    if script == Script.LATIN:
        # Already searchable, no need for duplicate
        return None

    if should_romanize(script):
        # CJK/Japanese -> romanize
        if anyascii is None:
            return None
        romanized = anyascii(name).lower().strip()
        # Only store if meaningfully different
        if romanized and romanized != name.lower():
            return romanized
        return None

    if should_decompose(script):
        # Korean Hangul -> decompose to Jamo
        # Note: Jamo isn't "searchable" in the traditional sense,
        # but we store it for consistency with vocabulary
        decomposed = decompose_text(name)
        if decomposed != name:
            return decomposed
        return None

    # For other scripts (Cyrillic, Arabic, Greek, etc.), romanize for search
    if anyascii is not None:
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

    # Character collection for vocabulary
    char_counts: Dict[str, Counter] = defaultdict(Counter)  # script -> char -> count
    script_counts: Counter = Counter()
    mismatch_counts: Counter = Counter()  # Track lang-script mismatches

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

        if not toponyms_list: continue

        for top in toponyms_list:
            top_id = top.get('toponym_id')
            label = top.get('label')

            if not top_id: continue

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

            if not name and label: name = label
            if name: name = name.strip()
            if not name: continue

            # Basic filters
            if len(name) > MAX_NAME_LEN: continue
            if len(name.encode('utf-8')) > MAX_ID_BYTES: continue

            if lang and lang.lower() in ('und', 'zxx', 'mis', 'null', 'none'):
                lang = None

            # Detect script
            script, _ = detect_script(name)
            script_value = script.value

            # Check for lang-script mismatch (pre-romanized forms)
            if is_script_mismatch(lang, script):
                mismatch_counts[f"{lang}:{script_value}"] += 1
                skipped_batch.append((top_id, 'lang_script_mismatch', lang, script_value))
                toponyms_skipped += 1
                continue

            canonical_id = f"{name}@{lang}" if lang else f"{name}@"

            # Generate searchable form (romanized/decomposed)
            name_romanized = generate_name_romanized(name, script)

            # Track script statistics
            script_counts[script_value] += 1

            # Collect characters for vocabulary (preprocessed)
            processed_name = preprocess_for_vocab(name, script)
            for char in processed_name:
                if char.strip():  # Skip whitespace
                    char_counts[script_value][char] += 1

            toponym_batch.append((canonical_id, name, name_romanized, lang, lang_variant, script_value, None, None))
            namespace_batch.append((canonical_id, namespace))
            attestation_batch.append((canonical_id, place_id))
            toponyms_extracted += 1

        places_processed += 1

        if len(toponym_batch) >= batch_size * 5:
            # Use fast bulk insert with PyArrow
            bulk_insert_duckdb(conn, 'toponyms',
                ['toponym_id', 'name', 'name_romanized', 'lang', 'lang_variant', 'script', 'ipa', 'panphon_features'],
                toponym_batch)
            bulk_insert_duckdb(conn, 'toponym_namespaces', ['toponym_id', 'namespace'], namespace_batch)
            bulk_insert_duckdb(conn, 'toponym_attestations', ['toponym_id', 'place_id'], attestation_batch)
            if skipped_batch:
                bulk_insert_duckdb(conn, 'skipped_toponyms', ['toponym_id', 'reason', 'lang', 'script'], skipped_batch)
            toponym_batch = []
            namespace_batch = []
            attestation_batch = []
            skipped_batch = []

        if limit and places_processed >= limit: break

    # Final batch
    if toponym_batch:
        bulk_insert_duckdb(conn, 'toponyms',
            ['toponym_id', 'name', 'name_romanized', 'lang', 'lang_variant', 'script', 'ipa', 'panphon_features'],
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

    # Save script statistics
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


def generate_vocabulary(conn, output_dir: Path) -> Dict:
    """
    Generate expanded character vocabulary from observed characters.

    Strategy:
    1. Start with all observed characters
    2. Expand to full Unicode blocks for all observed scripts
    3. Add full ASCII printable range (for AnyAscii output)
    4. Add Korean Jamo (for Hangul decomposition)

    Returns statistics dict.
    """
    from phonetics.utils.korean import ALL_JAMO

    output_dir.mkdir(parents=True, exist_ok=True)

    # Load observed characters and scripts
    # With composite key (char, script), a char can appear in multiple scripts
    observed_chars: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for row in conn.execute('SELECT char, script, count FROM observed_chars').fetchall():
        char, script, count = row
        observed_chars[char].append((script, count))

    observed_scripts = {row[0] for row in conn.execute('SELECT script, count FROM script_stats').fetchall()}

    logger.info(f"Observed {len(observed_chars):,} unique characters across {len(observed_scripts)} scripts")

    # Build expanded vocabulary
    # Special tokens
    vocab = {
        '<PAD>': 0,
        '<UNK>': 1,
        '<SPACE>': 2,
    }
    next_id = 3

    # Reserved range (3-9)
    next_id = 10

    # Track which scripts we include
    included_scripts = set()
    script_char_counts = defaultdict(int)

    # 1. Add full ASCII printable range (32-126) for AnyAscii output
    logger.info("Adding ASCII printable range...")
    for cp in range(32, 127):
        char = chr(cp)
        if char not in vocab and char != ' ':  # Space handled specially
            vocab[char] = next_id
            next_id += 1
            script_char_counts['ASCII'] += 1

    # 2. Add all Korean Jamo for Hangul decomposition
    logger.info("Adding Korean Jamo...")
    for jamo in ALL_JAMO:
        if jamo and jamo not in vocab:
            vocab[jamo] = next_id
            next_id += 1
            script_char_counts['JAMO'] += 1

    # 3. For each observed script (except romanized ones), add full Unicode range
    # NOTE: This "backfills" characters not seen in training data. These characters
    # will have random (untrained) embeddings at inference time - functionally similar
    # to <UNK> but with consistent IDs across model versions. This provides:
    # - Stable vocab size for model comparisons
    # - No runtime errors on rare characters
    # - Foundation for future fine-tuning on expanded data
    # Cost: ~2,700 extra embedding rows (~345K params, 1.3MB) - negligible.
    logger.info("Expanding to full Unicode ranges for observed scripts...")

    for script_name in observed_scripts:
        try:
            script = Script(script_name)
        except ValueError:
            logger.warning(f"Unknown script in data: {script_name}")
            continue

        # Skip scripts that are romanized (CJK, Hiragana, Katakana)
        # Their characters become ASCII via AnyAscii
        if should_romanize(script):
            logger.info(f"  {script_name}: romanized (using ASCII)")
            continue

        # Skip Hangul syllables (decomposed to Jamo)
        if should_decompose(script):
            logger.info(f"  {script_name}: decomposed (using Jamo)")
            continue

        if script not in SCRIPT_RANGES:
            logger.warning(f"No Unicode ranges defined for {script_name}")
            continue

        included_scripts.add(script_name)
        count_before = len(vocab)

        # Add all codepoints in the script's Unicode blocks
        for start, end in SCRIPT_RANGES[script]:
            for cp in range(start, end + 1):
                try:
                    char = chr(cp)
                    # Skip control characters, surrogates, etc.
                    cat = unicodedata.category(char)
                    if cat.startswith('C'):  # Control characters
                        continue
                    if char not in vocab:
                        vocab[char] = next_id
                        next_id += 1
                except (ValueError, OverflowError):
                    continue

        count_added = len(vocab) - count_before
        script_char_counts[script_name] = count_added
        logger.info(f"  {script_name}: {count_added:,} characters")

    # 4. Add any observed characters not yet in vocab (edge cases)
    for char, script_counts in observed_chars.items():
        if char not in vocab and char.strip():
            vocab[char] = next_id
            next_id += 1
            # Use the script with highest count for categorization
            best_script = max(script_counts, key=lambda x: x[1])[0] if script_counts else 'OTHER'
            script_char_counts[best_script] += 1

    # Save vocabulary
    vocab_data = {
        'version': 3,
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

    # Also save language vocabulary from observed languages
    languages = sorted(row[0] for row in conn.execute(
        "SELECT DISTINCT lang FROM toponyms WHERE lang IS NOT NULL AND lang != ''"
    ).fetchall())

    lang_vocab = {'<UNK>': 0}
    for i, lang in enumerate(languages, start=1):
        lang_vocab[lang] = i

    lang_data = {
        'version': 1,
        'lang_to_id': lang_vocab,
    }

    lang_path = output_dir / 'lang_vocab.json'
    with open(lang_path, 'w', encoding='utf-8') as f:
        json.dump(lang_data, f, ensure_ascii=False, indent=2)

    logger.info(f"Language vocabulary saved: {lang_path} ({len(lang_vocab):,} languages)")

    # Save script vocabulary (static from enum)
    script_vocab = {s.value: i for i, s in enumerate(Script)}
    script_data = {
        'version': 1,
        'script_to_id': script_vocab,
    }

    script_path = output_dir / 'script_vocab.json'
    with open(script_path, 'w', encoding='utf-8') as f:
        json.dump(script_data, f, indent=2)

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
    ('name_romanized', pa.string()),  # Romanized/decomposed form
    ('script', pa.string()),
    ('lang', pa.string()),
    ('char_ids', pa.list_(pa.int16())),
    ('char_length', pa.int16()),
    ('ipa', pa.string()),
    ('features', pa.list_(pa.float32())),  # Full PanPhon features (variable length)
    ('feature_length', pa.int16()),  # Number of segments (features.length / 24)
    ('namespaces', pa.list_(pa.string())),
    ('attestations', pa.list_(pa.string())),  # Back-references to places
    ('split', pa.string()),  # train/val/test
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

    This creates training-ready data with:
    - Pre-encoded char_ids (using the vocabulary)
    - IPA transcription and PanPhon features (read from DuckDB, pre-computed during Phase 1)
    - Train/val/test splits
    - Attestations for pair generation

    Args:
        conn: DuckDB connection
        vocab: Character vocabulary (char -> id mapping)
        output_dir: Output directory for Parquet files
        namespaces: Namespaces to include (e.g., ['gn', 'wd', 'tgn'])
        train_ratio: Proportion for training split
        val_ratio: Proportion for validation split
        batch_size: Records per Parquet file

    Returns:
        Statistics dictionary
    """

    logger.info("=" * 60)
    logger.info("Exporting training data to Parquet")
    logger.info("=" * 60)
    logger.info(f"Namespaces: {namespaces}")

    parquet_dir = output_dir / 'training'
    parquet_dir.mkdir(parents=True, exist_ok=True)

    # Query toponyms with their namespaces, attestations, and pre-computed IPA/PanPhon
    # Filter to only include toponyms from specified namespaces
    ns_placeholders = ','.join('?' * len(namespaces))

    query = f'''
        SELECT t.toponym_id,
               t.name,
               t.name_romanized,
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
        GROUP BY t.toponym_id, t.name, t.name_romanized, t.script, t.lang, t.ipa, t.panphon_features
    '''

    result = conn.execute(query, namespaces)

    # Get total count for progress bar
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

    # Buffer for batched writing
    buffers: Dict[str, List[Dict]] = defaultdict(list)  # script -> records
    part_counts: Dict[str, int] = defaultdict(int)

    def encode_chars(name_romanized: Optional[str], name: str, script: str) -> List[int]:
        """Encode characters using vocabulary."""
        # Use name_romanized if available (already preprocessed), else use name
        text = name_romanized if name_romanized else name.lower()
        ids = []
        for char in text:
            if char == ' ':
                ids.append(2)  # SPACE_ID
            elif char in vocab:
                ids.append(vocab[char])
            else:
                ids.append(1)  # UNK_ID
        return ids

    def unpack_features(packed: bytes) -> Optional[List[float]]:
        """Unpack PanPhon features from DuckDB BLOB."""
        if not packed:
            return None
        import struct
        num_floats = len(packed) // 4
        return list(struct.unpack(f'{num_floats}f', packed))

    def flush_buffer(script: str):
        """Write buffer to Parquet file."""
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

    # Progress tracking
    processed = 0
    pbar = None
    if tqdm:
        pbar = tqdm(total=total_count, desc="Exporting Parquet", mininterval=10.0)

    # Stream results using fetchmany() for memory efficiency
    fetch_batch_size = 10000
    while True:
        rows = result.fetchmany(fetch_batch_size)
        if not rows:
            break

        for row in rows:
            toponym_id, name, name_romanized, script, lang, ipa, panphon_packed, namespaces_str, attestations_str = row

            namespaces_list = namespaces_str.split(',') if namespaces_str else []
            attestations_list = attestations_str.split(',') if attestations_str else []

            # Encode characters
            char_ids = encode_chars(name_romanized, name, script)

            # Unpack pre-computed PanPhon features from DuckDB
            features = unpack_features(panphon_packed)
            feature_length = len(features) // 24 if features else 0

            if ipa:
                stats['with_ipa'] += 1
            if features:
                stats['with_features'] += 1

            # Assign split
            split_hash = compute_split_hash(toponym_id)
            split = assign_split(split_hash, train_ratio, val_ratio)

            record = {
                'toponym_id': toponym_id,
                'name': name,
                'name_romanized': name_romanized,
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

            # Flush if buffer is full
            if len(buffers[script]) >= batch_size:
                flush_buffer(script)

            # Progress logging
            processed += 1
            if pbar:
                pbar.update(1)
            elif processed % 100000 == 0:
                logger.info(f"Processed {processed:,} / {total_count:,} ({100*processed/total_count:.1f}%)")

    if pbar:
        pbar.close()

    # Flush remaining buffers
    for script in list(buffers.keys()):
        flush_buffer(script)

    # Log statistics
    logger.info(f"Exported {stats['total_exported']:,} toponyms to Parquet")
    logger.info(f"With IPA: {stats['with_ipa']:,} ({100*stats['with_ipa']/stats['total_exported']:.1f}%)")
    logger.info(f"With features: {stats['with_features']:,} ({100*stats['with_features']/stats['total_exported']:.1f}%)")
    logger.info(f"By script: {dict(stats['by_script'].most_common(10))}")
    logger.info(f"By split: {dict(stats['by_split'])}")
    logger.info(f"By namespace: {dict(stats['by_namespace'])}")

    # Save split IDs for reference
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

    # Save export stats
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
    - IPA transcription via Epitran
    - PanPhon embedding (192-dim position-pooled articulatory features) for ES
    - Full PanPhon feature sequence stored in DuckDB for training data assembly

    Args:
        conn: DuckDB connection
        output_path: Path for JSONL output
        training_namespaces: Namespaces for which to compute PanPhon (default: ['gn', 'wd', 'tgn'])
        num_workers: Number of parallel workers (default: CPU count - 2)
        batch_size: Number of documents to process per batch (default: 10000)

    Returns:
        Tuple of (total_count, stats_dict)
    """
    if training_namespaces is None:
        training_namespaces = ['gn', 'wd', 'tgn']

    if num_workers is None:
        num_workers = max(1, mp.cpu_count() - 2)

    training_ns_set = set(training_namespaces)

    # Increased batch sizes for better throughput at scale
    processing_batch_size = 50000  # Documents per phonetics batch (was 10000)
    io_batch_size = 5000  # Documents per JSONL write (reduce filesystem overhead)
    chunksize = max(100, processing_batch_size // num_workers // 4)  # For imap_unordered

    logger.info(f"Buffering documents to disk: {output_path}")
    logger.info(f"Computing PanPhon embeddings for namespaces: {training_namespaces}")
    logger.info(f"Using {num_workers} parallel workers for IPA/PanPhon computation")
    logger.info(f"Processing batch size: {processing_batch_size:,}, I/O batch size: {io_batch_size:,}")
    logger.info(f"Worker chunksize: {chunksize}")

    # Get total count for progress reporting
    total_count = conn.execute('SELECT COUNT(*) FROM toponyms').fetchone()[0]
    logger.info(f"Total toponyms to process: {total_count:,}")

    stats = {
        'total': 0,
        'with_romanized': 0,
        'in_training_ns': 0,
        'with_ipa': 0,
        'with_panphon': 0,
        'by_script': Counter(),
        'by_script_lang_ipa': Counter(),
    }

    # Stream from DuckDB using fetchmany() for memory efficiency
    result = conn.execute('''
        SELECT t.toponym_id,
               t.name,
               t.name_romanized,
               t.lang,
               t.lang_variant,
               t.script,
               GROUP_CONCAT(DISTINCT tn.namespace) as namespaces,
               GROUP_CONCAT(DISTINCT ta.place_id) as attestations
        FROM toponyms t
        JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
        LEFT JOIN toponym_attestations ta ON t.toponym_id = ta.toponym_id
        GROUP BY t.toponym_id, t.name, t.name_romanized, t.lang, t.lang_variant, t.script
    ''')

    # Accumulate Arrow tables from workers for zero-copy bulk DB update
    all_arrow_tables = []

    logger.info("Starting streaming from DuckDB with Producer-Consumer pipeline...")

    # Progress bar setup
    pbar = None
    if tqdm:
        pbar = tqdm(total=total_count, desc="Phonetic computation", mininterval=10.0)

    # Mark main process for logging
    os.environ['_REBUILD_MAIN_PROCESS'] = '1'

    # Use multiprocessing.Pool with initializer for cached converters
    with open(output_path, 'w', encoding='utf-8') as f, \
         mp.Pool(processes=num_workers, initializer=_init_worker) as pool:

        fetch_batch_size = processing_batch_size
        batches_processed = 0
        pending_writes = []  # Buffer for JSONL writes

        def flush_writes():
            """Write pending documents to JSONL file."""
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
            phonetics_work = []  # List of (toponym_id, name, lang, script) for workers

            for row in rows:
                toponym_id, name, name_romanized, lang, lang_variant, script, namespaces_str, attestations_str = row
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

                if name_romanized:
                    doc['name_romanized'] = name_romanized
                    stats['with_romanized'] += 1

                stats['by_script'][script] += 1

                is_in_training_ns = bool(training_ns_set & set(namespaces))
                if is_in_training_ns:
                    stats['in_training_ns'] += 1
                    phonetics_work.append((toponym_id, name, lang, script))

                batch_docs.append(doc)
                stats['total'] += 1

            # Phase 2: Parallel phonetics computation - collect Arrow tables
            # Workers return Arrow tables (zero-copy transfer)
            batch_arrow_tables = []
            if phonetics_work:
                # Create sub-batches for parallel processing
                sub_batch_size = max(100, len(phonetics_work) // num_workers)
                sub_batches = [
                    phonetics_work[i:i + sub_batch_size]
                    for i in range(0, len(phonetics_work), sub_batch_size)
                ]

                # Workers return Arrow tables - no serialization overhead
                for arrow_table in pool.imap_unordered(_compute_phonetics_for_batch, sub_batches, chunksize=1):
                    batch_arrow_tables.append(arrow_table)

            # Phase 3: Merge Arrow tables and update docs
            phonetics_results = {}
            if batch_arrow_tables:
                import pyarrow as pa
                # Concatenate all Arrow tables from this fetch batch
                combined_table = pa.concat_tables(batch_arrow_tables)
                # Store for later bulk DB update (zero-copy)
                all_arrow_tables.append(combined_table)

                # Convert to dict only for doc merging (single copy operation)
                table_dict = combined_table.to_pydict()
                for i in range(len(table_dict['toponym_id'])):
                    toponym_id = table_dict['toponym_id'][i]
                    ipa = table_dict['ipa'][i]
                    embedding = table_dict['panphon_embedding'][i]
                    phonetics_results[toponym_id] = (ipa, embedding)

            # Phase 4: Update docs with phonetics data
            for doc in batch_docs:
                toponym_id = doc['toponym_id']

                if toponym_id in phonetics_results:
                    ipa, embedding = phonetics_results[toponym_id]
                    doc['ipa'] = ipa
                    doc['panphon_embedding'] = embedding
                    stats['with_ipa'] += 1
                    stats['with_panphon'] += 1
                    stats['by_script_lang_ipa'][f"{doc['script']}:{doc['lang']}"] += 1

                pending_writes.append(doc)

                # Flush writes in batches to reduce filesystem overhead
                if len(pending_writes) >= io_batch_size:
                    flush_writes()


            batches_processed += 1

            # Log progress every batch (processing batches are now larger)
            if pbar:
                pbar.update(len(batch_docs))
            else:
                pct = 100 * stats['total'] / total_count
                logger.info(
                    f"Progress: {stats['total']:,} / {total_count:,} ({pct:.1f}%) - "
                    f"IPA: {stats['with_ipa']:,} - Batch {batches_processed}"
                )

        # Flush any remaining writes
        if pending_writes:
            flush_writes()

        if pbar:
            pbar.close()

    logger.info(f"Streaming complete. Total batches processed: {batches_processed}")

    # Flush and sync to ensure all data is written
    logger.info("Flushing JSONL file to disk...")

    # Now apply all accumulated Arrow tables to DuckDB
    # (Doing this after the streaming loop avoids cursor invalidation)
    total_db_updates = 0
    if all_arrow_tables:
        import pyarrow as pa

        # Concatenate all Arrow tables (zero-copy operation)
        logger.info(f"Concatenating {len(all_arrow_tables)} Arrow tables...")
        final_arrow_table = pa.concat_tables(all_arrow_tables)
        total_db_updates = len(final_arrow_table)

        logger.info(f"Applying {total_db_updates:,} IPA/PanPhon updates to DuckDB via Arrow...")

        # OPTIMIZATION: Drop indexes before bulk updates, rebuild afterward
        logger.info("Dropping indexes on toponyms table for faster bulk updates...")
        try:
            conn.execute("DROP INDEX IF EXISTS idx_toponyms_id")
            conn.execute("DROP INDEX IF EXISTS idx_toponyms_script")
            conn.execute("DROP INDEX IF EXISTS idx_toponyms_lang")
            logger.info("Indexes dropped. Starting bulk updates...")
        except Exception as e:
            logger.warning(f"Could not drop indexes (may not exist): {e}")

        # Process in batches to avoid memory issues
        # But use Arrow slicing instead of tuple unpacking
        update_batch_size = 100000
        num_batches = (total_db_updates + update_batch_size - 1) // update_batch_size

        if tqdm:
            batch_iterator = tqdm(range(num_batches), desc="DuckDB Arrow updates", mininterval=10.0)
        else:
            batch_iterator = range(num_batches)

        for batch_idx in batch_iterator:
            start_idx = batch_idx * update_batch_size
            end_idx = min(start_idx + update_batch_size, total_db_updates)

            # Slice Arrow table (zero-copy operation)
            batch_table = final_arrow_table.slice(start_idx, end_idx - start_idx)

            # Register Arrow table with DuckDB (zero-copy)
            conn.register('update_table', batch_table)

            # Perform bulk update
            conn.execute("""
                UPDATE toponyms 
                SET ipa = u.ipa, 
                    panphon_features = u.panphon_features,
                    panphon_embedding = u.panphon_embedding
                FROM update_table u
                WHERE toponyms.toponym_id = u.toponym_id
            """)

            # Unregister table
            conn.unregister('update_table')

            if not tqdm and end_idx % 500000 == 0:
                logger.info(f"  Updated {end_idx:,} / {total_db_updates:,} records")

        total_db_updates = total_db_updates
        logger.info(f"DuckDB updates complete: {total_db_updates:,} records")

        # OPTIMIZATION: Rebuild indexes after bulk updates
        logger.info("Rebuilding indexes on toponyms table...")
        conn.execute('CREATE INDEX IF NOT EXISTS idx_toponyms_id ON toponyms(toponym_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_toponyms_script ON toponyms(script)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_toponyms_lang ON toponyms(lang)')
        logger.info("Indexes rebuilt.")

    logger.info(f"JSONL export complete: {stats['total']:,} documents written")

    # Log final statistics
    logger.info(f"Buffering complete. Total documents: {stats['total']:,}")
    logger.info(f"Documents with name_romanized: {stats['with_romanized']:,} ({100*stats['with_romanized']/stats['total']:.1f}%)")
    logger.info(f"Documents in training namespaces: {stats['in_training_ns']:,}")
    logger.info(f"  With IPA: {stats['with_ipa']:,} ({100*stats['with_ipa']/max(1,stats['in_training_ns']):.1f}%)")
    logger.info(f"  With PanPhon embedding: {stats['with_panphon']:,} ({100*stats['with_panphon']/max(1,stats['in_training_ns']):.1f}%)")

    # Log top script+lang combinations with successful IPA
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
    Step 2: Stream from JSONL to Elasticsearch.
    This is purely I/O bound and stable.
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

    # Conservative Settings for Reliability
    iterator = helpers.parallel_bulk(
        es,
        generate_actions(),
        thread_count=4,  # Reduced threads
        queue_size=8,  # Reduced memory pressure
        chunk_size=batch_size,
        raise_on_error=False,
        request_timeout=120  # 2 minute timeout per batch
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


def _ensure_columns_exist(conn):
    """Ensure all required columns exist in the toponyms table (for backward compatibility)."""
    # Get current columns - DuckDB PRAGMA returns (cid, name, type, notnull, dflt_value, pk)
    columns_result = conn.execute("PRAGMA table_info(toponyms)").fetchall()
    existing_columns = {row[1].lower() for row in columns_result}  # row[1] is the column name

    logger.info(f"Existing columns in toponyms table: {sorted(existing_columns)}")

    # Add missing columns
    if 'ipa' not in existing_columns:
        logger.info("Adding 'ipa' column to toponyms table")
        conn.execute("ALTER TABLE toponyms ADD COLUMN ipa VARCHAR")
    else:
        logger.info("Column 'ipa' already exists, skipping")

    if 'panphon_features' not in existing_columns:
        logger.info("Adding 'panphon_features' column to toponyms table")
        conn.execute("ALTER TABLE toponyms ADD COLUMN panphon_features BLOB")
    else:
        logger.info("Column 'panphon_features' already exists, skipping")

    if 'panphon_embedding' not in existing_columns:
        logger.info("Adding 'panphon_embedding' column to toponyms table")
        conn.execute("ALTER TABLE toponyms ADD COLUMN panphon_embedding FLOAT[192]")
    else:
        # Check if existing column has correct type
        col_info = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'toponyms' AND column_name = 'panphon_embedding'"
        ).fetchone()

        if col_info:
            col_type = col_info[1]
            # Check if it's not FLOAT[192]
            if 'FLOAT[192]' not in col_type.upper() and 'FLOAT(192)' not in col_type.upper():
                logger.warning(f"panphon_embedding column has incorrect type '{col_type}', recreating as FLOAT[192]")
                conn.execute("ALTER TABLE toponyms DROP COLUMN panphon_embedding")
                conn.execute("ALTER TABLE toponyms ADD COLUMN panphon_embedding FLOAT[192]")
            else:
                logger.info("Column 'panphon_embedding' already exists with correct type, skipping")
        else:
            logger.info("Column 'panphon_embedding' already exists, skipping")


def _update_language_phonetics(
        conn,
        lang_list: List[str],
        training_namespaces: List[str],
        num_workers: int,
        batch_size: int = 25_000,
        persistent_db_path: Path = None,
        scratch_db_path: Path = None
):
    """
    Perform targeted update of IPA and PanPhon features for specific languages.

    If lang_list contains '__ALL__', processes all toponyms in training namespaces.
    Otherwise, filters by specific language codes.

    Args:
        conn: DuckDB connection
        lang_list: List of language codes to process (or ['__ALL__'] for all)
        training_namespaces: List of namespace codes (e.g., ['gn', 'wd', 'tgn'])
        num_workers: Number of parallel worker processes
        batch_size: Records per batch for processing
        persistent_db_path: Path to persistent database (for checkpointing)
        scratch_db_path: Path to scratch database (for checkpointing)
    """
    process_all = '__ALL__' in lang_list

    if process_all:
        logger.info(f"Updating phonetics for ALL toponyms in training namespaces ({', '.join(training_namespaces)})")
    else:
        logger.info(f"Targeted update for languages: {lang_list}")

    # Ensure all required columns exist (for backward compatibility)
    _ensure_columns_exist(conn)

    # Mark main process for logging
    os.environ['_REBUILD_MAIN_PROCESS'] = '1'

    # PRE-LOAD MODELS BEFORE SPAWNING WORKERS - CRITICAL!
    logger.info("=" * 60)
    logger.info("PRE-LOADING MODELS (prevents worker filesystem contention)")
    logger.info("=" * 60)
    main_converter = _preload_models()
    logger.info("Models cached and ready for workers")
    logger.info("=" * 60)

    # Create indexes for efficient query (if they don't exist)
    logger.info("Ensuring indexes exist for efficient querying...")
    index_start = time.time()

    # Check if indexes already exist before creating
    existing_indexes = set()
    index_check = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    existing_indexes = {row[0] for row in index_check}

    indexes_created = False

    if 'idx_tn_namespace' not in existing_indexes:
        conn.execute('CREATE INDEX idx_tn_namespace ON toponym_namespaces(namespace)')
        indexes_created = True
        logger.info("Created index: idx_tn_namespace")

    if 'idx_tn_toponym_id' not in existing_indexes:
        conn.execute('CREATE INDEX idx_tn_toponym_id ON toponym_namespaces(toponym_id)')
        indexes_created = True
        logger.info("Created index: idx_tn_toponym_id")

    if 'idx_toponyms_id' not in existing_indexes:
        conn.execute('CREATE INDEX idx_toponyms_id ON toponyms(toponym_id)')
        indexes_created = True
        logger.info("Created index: idx_toponyms_id")

    if not process_all and 'idx_toponyms_lang' not in existing_indexes:
        # Only needed for language-filtered queries
        conn.execute('CREATE INDEX idx_toponyms_lang ON toponyms(lang)')
        indexes_created = True
        logger.info("Created index: idx_toponyms_lang")

    index_elapsed = time.time() - index_start
    logger.info(f"Indexes ready in {index_elapsed:.2f}s")

    # If we created new indexes, save them back to persistent storage now
    # This way if the job fails later, we don't have to recreate indexes on restart
    if indexes_created and persistent_db_path and scratch_db_path:
        logger.info("=" * 60)
        logger.info("NEW INDEXES CREATED - Checkpointing to persistent storage")
        logger.info("=" * 60)

        # Close connection temporarily for safe copy
        conn.close()

        import shutil
        checkpoint_start = time.time()
        logger.info(f"Copying indexed database: {scratch_db_path} -> {persistent_db_path}")
        shutil.copy2(scratch_db_path, persistent_db_path)
        checkpoint_elapsed = time.time() - checkpoint_start
        logger.info(f"Checkpoint completed in {checkpoint_elapsed:.2f}s")

        # Reconnect
        conn = duckdb.connect(str(scratch_db_path))
        logger.info("Database reconnected, continuing with query...")
        logger.info("=" * 60)

    # Build query based on whether we're processing all or specific languages
    logger.info("Building query to fetch toponyms...")
    query_start = time.time()

    # Build namespace IN clause
    ns_placeholders = ','.join([f"'{ns}'" for ns in training_namespaces])

    if process_all:
        # Process ALL toponyms in training namespaces
        query = f'''
            SELECT DISTINCT t.toponym_id, t.name, t.lang, t.script 
            FROM toponyms t
            JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
            WHERE tn.namespace IN ({ns_placeholders})
        '''
        rows = conn.execute(query).fetchall()
    else:
        # Process only specific languages in training namespaces
        langs_str = ",".join([f"'{l}'" for l in lang_list])
        query = f'''
            SELECT DISTINCT t.toponym_id, t.name, t.lang, t.script 
            FROM toponyms t
            JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
            WHERE t.lang IN ({langs_str})
            AND tn.namespace IN ({ns_placeholders})
        '''
        rows = conn.execute(query).fetchall()

    query_elapsed = time.time() - query_start
    logger.info(f"Query completed in {query_elapsed:.2f}s, fetched {len(rows):,} rows")

    if not rows:
        logger.info("No records found for specified criteria.")
        return

    logger.info(f"Processing {len(rows):,} records...")

    # Process in batches
    logger.info(f"Creating batches (batch_size={batch_size})...")
    num_batches = (len(rows) + batch_size - 1) // batch_size
    batches = [rows[i * batch_size:(i + 1) * batch_size] for i in range(num_batches)]
    logger.info(f"Created {num_batches:,} batches")

    # Initialize workers with global converter
    logger.info(f"Initializing worker pool with {num_workers} processes...")
    pool_start = time.time()
    pool = mp.Pool(num_workers, initializer=_init_worker)
    pool_elapsed = time.time() - pool_start
    logger.info(f"Worker pool initialized in {pool_elapsed:.2f}s")

    try:
        total_updated = 0
        batches_completed = 0

        logger.info("Starting batch processing with imap_unordered...")

        # Use tqdm if available
        if tqdm:
            pbar = tqdm(total=len(rows), desc="Updating phonetics", mininterval=5.0)
        else:
            pbar = None

        # Process batches with explicit chunksize and logging
        for result_columns in pool.imap_unordered(_compute_phonetics_for_batch, batches, chunksize=1):
            batches_completed += 1

            # Log every 10 batches regardless of tqdm
            if batches_completed % 10 == 0:
                logger.info(
                    f"Completed {batches_completed}/{num_batches} batches ({100 * batches_completed / num_batches:.1f}%)")

            # result_columns is: (ids, ipas, features, embeddings)
            # where embeddings is numpy array (N, 192)
            if result_columns:
                ids, ipas, features, embeddings = result_columns

                if ids:  # Check if any results in this batch
                    # Build Arrow table column-wise (zero-copy from numpy)
                    # Convert numpy array to flat list then to fixed-size list array
                    arrow_table = pa.table({
                        'toponym_id': pa.array(ids, type=pa.string()),
                        'ipa': pa.array(ipas, type=pa.string()),
                        'panphon_features': pa.array(features, type=pa.binary()),
                        'panphon_embedding': pa.FixedSizeListArray.from_arrays(
                            pa.array(embeddings.flatten(), type=pa.float32()),
                            list_size=192
                        )
                    })

                    # Register Arrow table with DuckDB and perform update
                    conn.register('updates_temp', arrow_table)
                    conn.execute("""
                        UPDATE toponyms
                        SET ipa = updates_temp.ipa,
                            panphon_features = updates_temp.panphon_features,
                            panphon_embedding = updates_temp.panphon_embedding
                        FROM updates_temp
                        WHERE toponyms.toponym_id = updates_temp.toponym_id
                    """)
                    conn.unregister('updates_temp')

                    total_updated += len(ids)

                    # Log every 100 batches with update count
                    if batches_completed % 100 == 0:
                        logger.info(f"Total updated so far: {total_updated:,} records")

                    # Drop reference immediately to free memory
                    del arrow_table, ids, ipas, features, embeddings

            if pbar:
                pbar.update(batch_size)

        if pbar:
            pbar.close()

        logger.info(f"Successfully updated {total_updated:,} records.")

    finally:
        pool.close()
        pool.join()

    # Return connection in case it was reconnected during checkpointing
    return conn


# --- MAIN ---

def main():
    parser = argparse.ArgumentParser(
        description='Phase 1: Rebuild ES toponyms index with PanPhon embeddings'
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
    parser.add_argument('--batch-size', type=int, default=25_000)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--resume', action='store_true', help="Resume from existing DuckDB database")
    parser.add_argument('--skip-es-index', action='store_true',
                        help="Skip ES indexing (only extract to DuckDB and generate vocab)")
    parser.add_argument('--update-langs', nargs='+', default=None,
                        help="Update IPA/PanPhon for specific languages in existing DB and exit")

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

    if args.update_langs:
        if not args.db_path.exists():
            logger.error(f"Database not found at {args.db_path}. Cannot perform targeted update.")
            sys.exit(1)

        # Handle "all" keyword - process all toponyms in training namespaces
        if 'all' in [l.lower() for l in args.update_langs]:
            logger.info(f"Processing ALL toponyms in training namespaces ({', '.join(args.training_namespaces)})...")
            # Set special flag to indicate "process all languages"
            args.update_langs = ['__ALL__']

        logger.info(f"Targeted update for: {args.update_langs}")

        # Use scratch disk for database operations (critical for performance!)
        import tempfile
        import shutil

        with tempfile.TemporaryDirectory(dir=args.scratch_dir) as temp_dir:
            temp_dir_path = Path(temp_dir)
            temp_db_path = temp_dir_path / "toponyms_working.duckdb"

            logger.info(f"Copying database to scratch: {args.db_path} -> {temp_db_path}")
            copy_start = time.time()
            shutil.copy2(args.db_path, temp_db_path)
            copy_elapsed = time.time() - copy_start
            logger.info(f"Database copied to scratch in {copy_elapsed:.2f}s")

            # Connect to scratch copy
            conn = duckdb.connect(str(temp_db_path))

            # Set environment variable to indicate this is the main process
            os.environ['_REBUILD_MAIN_PROCESS'] = '1'

            # Perform updates on scratch copy
            conn = _update_language_phonetics(
                conn,
                args.update_langs,
                args.training_namespaces,
                args.num_workers,
                args.batch_size,
                persistent_db_path=args.db_path,
                scratch_db_path=temp_db_path
            )

            conn.close()

            # Copy updated database back to persistent storage
            logger.info(f"Copying updated database back: {temp_db_path} -> {args.db_path}")
            copy_start = time.time()
            shutil.copy2(temp_db_path, args.db_path)
            copy_elapsed = time.time() - copy_start
            logger.info(f"Database copied back in {copy_elapsed:.2f}s")

        logger.info("Update complete.")
        sys.exit(0)

    if not args.confirm:
        print("=" * 60)
        print("PHASE 1: EXTRACTION & ES INDEXING")
        print("=" * 60)
        print()
        print("Run with --confirm to proceed.")
        print()
        print("This script will:")
        print("  1. Scan all places to extract toponyms with attestations")
        print("  2. Filter out pre-romanized forms (lang-script mismatches)")
        print("  3. Generate expanded character vocabulary")
        print(f"  4. Compute IPA and PanPhon embeddings ({args.num_workers} parallel workers)")
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
                }, f, indent=2)
            logger.info(f"Coverage stats saved to: {coverage_stats_path}")

            if args.skip_es_index:
                logger.info("Skipping ES indexing (--skip-es-index)")
            else:
                # STEP 4: INDEXING (JSONL -> ES)
                logger.info("=" * 60)
                logger.info("STEP 4: INDEXING (JSONL -> ES)")
                logger.info("=" * 60)

                # Verify JSONL file has expected content
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

                # Ensure refresh_interval is disabled for bulk indexing speed
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

                # Re-enable refresh
                logger.info("Re-enabling refresh interval...")
                es.indices.put_settings(
                    index=args.toponyms_index,
                    body={"index": {"refresh_interval": "1s"}}
                )

                logger.info("Refreshing index...")
                es.indices.refresh(index=args.toponyms_index)

                # Get final count
                final_count = es.count(index=args.toponyms_index)['count']
                logger.info(f"Final document count: {final_count:,}")

                # Create snapshot
                logger.info("Creating snapshot...")
                snapshot_name = f"toponyms_v4"
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























