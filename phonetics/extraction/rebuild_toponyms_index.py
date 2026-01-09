"""
Rebuild the ES toponyms index with vocabulary generation.

This script:
1. Scans all places to extract toponyms with attestations (back-references to places)
2. Builds character vocabulary covering all observed scripts + full Unicode ranges
3. Indexes toponyms to ES with attestations for simplified pair generation

Vocabulary output enables subsequent training without re-scanning the corpus.

Reliability Update: Uses JSONL buffer on scratch disk to decouple SQL from HTTP.
Supports resuming from an existing SQLite database.
"""

import argparse
import hashlib
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Dict, List, Optional, Tuple, Set

from processing.utilities import create_checkpoint_snapshot

try:
    from elasticsearch import Elasticsearch, helpers
except ImportError:
    print("Error: elasticsearch package required.")
    sys.exit(1)

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    PYARROW_AVAILABLE = True
except ImportError:
    PYARROW_AVAILABLE = False
    print("Warning: pyarrow not available. Parquet export disabled.")

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
    """Lazy-loaded IPA converter using Epitran and PanPhon."""

    def __init__(self):
        self._epitran_cache: Dict[str, object] = {}
        self._panphon_ft = None
        self._epitran_available = None
        self._panphon_available = None

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


def create_sqlite_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=OFF')
    conn.execute('PRAGMA cache_size=-4000000')  # 4GB Cache
    conn.execute('PRAGMA temp_store=MEMORY')

    conn.executescript('''
                       CREATE TABLE IF NOT EXISTS toponyms
                       (
                           toponym_id TEXT PRIMARY KEY,
                           name TEXT NOT NULL,
                           name_romanized TEXT,
                           lang TEXT,
                           lang_variant TEXT,
                           script TEXT
                       );
                       CREATE TABLE IF NOT EXISTS toponym_namespaces
                       (
                           toponym_id TEXT NOT NULL,
                           namespace TEXT NOT NULL
                       );
                       CREATE TABLE IF NOT EXISTS toponym_attestations
                       (
                           toponym_id TEXT NOT NULL,
                           place_id TEXT NOT NULL
                       );
                       CREATE TABLE IF NOT EXISTS observed_chars
                       (
                           char TEXT PRIMARY KEY,
                           script TEXT,
                           count INTEGER DEFAULT 1
                       );
                       CREATE TABLE IF NOT EXISTS script_stats
                       (
                           script TEXT PRIMARY KEY,
                           count INTEGER DEFAULT 0
                       );
                       CREATE TABLE IF NOT EXISTS skipped_toponyms
                       (
                           toponym_id TEXT,
                           reason TEXT,
                           lang TEXT,
                           script TEXT
                       );
                       ''')
    conn.commit()
    return conn


def optimize_db_after_load(conn: sqlite3.Connection):
    logger.info("Optimizing SQLite database...")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tn_id ON toponym_namespaces(toponym_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ta_id ON toponym_attestations(toponym_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ta_place ON toponym_attestations(place_id)')
    # Composite index for efficient ROW_NUMBER() window function in generate_pairs.py
    conn.execute('CREATE INDEX IF NOT EXISTS idx_attestations_place_toponym ON toponym_attestations(place_id, toponym_id)')
    conn.execute('ANALYZE')
    conn.commit()
    logger.info("Database optimized.")


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


def extract_toponyms_to_sqlite(es, conn, places_index, batch_size, limit=None):
    """
    Extract toponyms from places index to SQLite, collecting:
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

            toponym_batch.append((canonical_id, name, name_romanized, lang, lang_variant, script_value))
            namespace_batch.append((canonical_id, namespace))
            attestation_batch.append((canonical_id, place_id))
            toponyms_extracted += 1

        places_processed += 1

        if len(toponym_batch) >= batch_size * 5:
            conn.executemany('INSERT OR IGNORE INTO toponyms VALUES (?, ?, ?, ?, ?, ?)', toponym_batch)
            conn.executemany('INSERT INTO toponym_namespaces VALUES (?, ?)', namespace_batch)
            conn.executemany('INSERT INTO toponym_attestations VALUES (?, ?)', attestation_batch)
            if skipped_batch:
                conn.executemany('INSERT INTO skipped_toponyms VALUES (?, ?, ?, ?)', skipped_batch)
            toponym_batch = []
            namespace_batch = []
            attestation_batch = []
            skipped_batch = []

        if limit and places_processed >= limit: break

    # Final batch
    if toponym_batch:
        conn.executemany('INSERT OR IGNORE INTO toponyms VALUES (?, ?, ?, ?, ?, ?)', toponym_batch)
        conn.executemany('INSERT INTO toponym_namespaces VALUES (?, ?)', namespace_batch)
        conn.executemany('INSERT INTO toponym_attestations VALUES (?, ?)', attestation_batch)
    if skipped_batch:
        conn.executemany('INSERT INTO skipped_toponyms VALUES (?, ?, ?, ?)', skipped_batch)

    # Save character vocabulary to SQLite
    logger.info("Saving character vocabulary to database...")
    char_batch = []
    for script_val, counts in char_counts.items():
        for char, count in counts.items():
            char_batch.append((char, script_val, count))

    conn.executemany(
        'INSERT OR REPLACE INTO observed_chars VALUES (?, ?, ?)',
        char_batch
    )

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


def generate_vocabulary(conn: sqlite3.Connection, output_dir: Path) -> Dict:
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
    cursor = conn.execute('SELECT char, script, count FROM observed_chars')
    observed_chars = {row[0]: (row[1], row[2]) for row in cursor}

    cursor = conn.execute('SELECT script, count FROM script_stats')
    observed_scripts = {row[0] for row in cursor}

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
    for char, (script, count) in observed_chars.items():
        if char not in vocab and char.strip():
            vocab[char] = next_id
            next_id += 1
            script_char_counts['OTHER'] += 1

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
    cursor = conn.execute('SELECT DISTINCT lang FROM toponyms WHERE lang IS NOT NULL AND lang != ""')
    languages = sorted(row[0] for row in cursor)

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
    ('epitran_code', pa.string()),
    ('epitran_supported', pa.bool_()),
    ('ipa', pa.string()),
    ('features', pa.list_(pa.float32())),
    ('feature_length', pa.int16()),
    ('namespaces', pa.list_(pa.string())),
    ('attestations', pa.list_(pa.string())),  # Back-references to places
    ('split', pa.string()),  # train/val/test
]) if PYARROW_AVAILABLE else None


def export_training_parquet(
    conn: sqlite3.Connection,
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
    - IPA transcription and PanPhon features (for Teacher training)
    - Train/val/test splits
    - Attestations for pair generation

    Args:
        conn: SQLite connection
        vocab: Character vocabulary (char -> id mapping)
        output_dir: Output directory for Parquet files
        namespaces: Namespaces to include (e.g., ['gn', 'wd', 'tgn'])
        train_ratio: Proportion for training split
        val_ratio: Proportion for validation split
        batch_size: Records per Parquet file

    Returns:
        Statistics dictionary
    """
    if not PYARROW_AVAILABLE:
        logger.warning("PyArrow not available, skipping Parquet export")
        return {}

    logger.info("=" * 60)
    logger.info("Exporting training data to Parquet")
    logger.info("=" * 60)
    logger.info(f"Namespaces: {namespaces}")

    parquet_dir = output_dir / 'training'
    parquet_dir.mkdir(parents=True, exist_ok=True)

    # Initialize IPA converter
    ipa_converter = IPAConverter()

    # Query toponyms with their namespaces and attestations
    # Filter to only include toponyms from specified namespaces
    ns_placeholders = ','.join('?' * len(namespaces))

    cursor = conn.execute(f'''
        SELECT t.toponym_id,
               t.name,
               t.name_romanized,
               t.script,
               t.lang,
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
        GROUP BY t.toponym_id
    ''', namespaces)

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
    last_log = 0

    for row in cursor:
        toponym_id, name, name_romanized, script, lang, namespaces_str, attestations_str = row

        namespaces_list = namespaces_str.split(',') if namespaces_str else []
        attestations_list = attestations_str.split(',') if attestations_str else []

        # Parse script
        try:
            script_enum = Script(script)
        except ValueError:
            script_enum = Script.OTHER

        # Encode characters
        char_ids = encode_chars(name_romanized, name, script)

        # Get IPA and features
        epitran_code = ipa_converter.get_epitran_code(lang or '', script_enum)
        epitran_supported = epitran_code is not None

        ipa = None
        features = None
        feature_length = 0

        if epitran_supported:
            ipa = ipa_converter.to_ipa(name, lang, script_enum)
            if ipa:
                stats['with_ipa'] += 1
                features = ipa_converter.to_features(ipa)
                if features:
                    feature_length = len(features) // 24  # PanPhon has 24 features per segment
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
            'epitran_code': epitran_code,
            'epitran_supported': epitran_supported,
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
        if processed - last_log >= 100000:
            logger.info(f"Processed {processed:,} / {total_count:,} ({100*processed/total_count:.1f}%)")
            last_log = processed

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


def dump_to_jsonl(conn: sqlite3.Connection, output_path: Path) -> int:
    """
    Step 1: Dump aggregated documents to a flat JSONL file on Scratch.
    Now includes attestations (list of place_ids) and name_romanized (romanized form).
    """
    logger.info(f"Buffering documents to disk: {output_path}")

    cursor = conn.execute('''
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
                          GROUP BY t.toponym_id
                          ''')

    count = 0
    with_search = 0
    with open(output_path, 'w', encoding='utf-8') as f:
        for row in cursor:
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

            # Only include name_romanized if it exists and differs from name
            if name_romanized:
                doc['name_romanized'] = name_romanized
                with_search += 1

            f.write(json.dumps(doc) + '\n')
            count += 1

            if count % 1000000 == 0:
                logger.info(f"Buffered {count:,} documents...")

    logger.info(f"Buffering complete. Total documents: {count:,}")
    logger.info(f"Documents with name_romanized: {with_search:,} ({100*with_search/count:.1f}%)")
    return count


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


# --- MAIN ---

def main():
    parser = argparse.ArgumentParser(
        description='Rebuild ES toponyms index with attestations, generate vocabulary, and export training data'
    )
    parser.add_argument('--es-host', default=ES_HOST)
    parser.add_argument('--places-index', default='places')
    parser.add_argument('--toponyms-index', default='toponyms')
    parser.add_argument('--schema-path', type=Path, default=SCHEMA_PATH)
    parser.add_argument('--sqlite-path', type=Path, default=f'{IX1_BASE}/data/toponyms.db')
    parser.add_argument('--output-dir', type=Path, default=None,
                        help='Output directory for vocab and training data (default: sqlite-path parent)')
    parser.add_argument('--scratch-dir', type=Path, default=None)
    parser.add_argument('--batch-size', type=int, default=2500)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--resume', action='store_true', help="Resume from existing SQLite DB")
    parser.add_argument('--skip-es-index', action='store_true',
                        help="Skip ES indexing (only extract to SQLite, generate vocab, and export training)")

    # Training export options
    parser.add_argument('--training-namespaces', nargs='+', default=['gn', 'wd', 'tgn'],
                        help='Namespaces to include in training data (default: gn wd tgn)')
    parser.add_argument('--train-ratio', type=float, default=0.8,
                        help='Proportion of data for training split (default: 0.8)')
    parser.add_argument('--val-ratio', type=float, default=0.1,
                        help='Proportion of data for validation split (default: 0.1)')
    parser.add_argument('--skip-training-export', action='store_true',
                        help='Skip training data export to Parquet')

    args = parser.parse_args()

    if not args.confirm:
        print("Run with --confirm to proceed.")
        print("This script will:")
        print("  1. Scan all places to extract toponyms with attestations")
        print("  2. Filter out pre-romanized forms (lang-script mismatches)")
        print("  3. Generate expanded character vocabulary")
        print("  4. Export training data to Parquet with IPA/PanPhon features")
        print("  5. Rebuild the ES toponyms index (unless --skip-es-index)")
        print()
        print(f"Training namespaces: {args.training_namespaces}")
        print(f"Train/Val/Test split: {args.train_ratio}/{args.val_ratio}/{1-args.train_ratio-args.val_ratio}")
        sys.exit(1)

    # Set output directory
    output_dir = args.output_dir or args.sqlite_path.parent

    es = None
    if not args.skip_es_index:
        es = Elasticsearch(
            args.es_host,
            max_retries=5,
            retry_on_timeout=True
        )
        if not es.ping():
            logger.error(f"Cannot connect to {args.es_host}")
            sys.exit(1)
    else:
        # Still need ES for extraction
        es = Elasticsearch(args.es_host, max_retries=5, retry_on_timeout=True)
        if not es.ping():
            logger.error(f"Cannot connect to {args.es_host}")
            sys.exit(1)

    final_db_path = args.sqlite_path
    final_db_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    vocab_dir = output_dir / 'vocab'

    with tempfile.TemporaryDirectory(dir=args.scratch_dir) as temp_dir:
        temp_dir_path = Path(temp_dir)
        temp_db_path = temp_dir_path / "toponyms_working.db"
        jsonl_path = temp_dir_path / "buffer.jsonl"

        try:
            if args.resume and final_db_path.exists():
                logger.info(f"--- RESUMING from {final_db_path} ---")
                logger.info(f"Copying existing DB to scratch: {temp_db_path}")
                shutil.copy2(final_db_path, temp_db_path)
            else:
                # PHASE 1: EXTRACTION (ES -> SQLite)
                logger.info("=" * 60)
                logger.info("PHASE 1: EXTRACTION (ES -> SQLite)")
                logger.info("=" * 60)
                conn = create_sqlite_db(str(temp_db_path))
                places_count, toponyms_count, skipped_count = extract_toponyms_to_sqlite(
                    es, conn, args.places_index, args.batch_size, args.limit
                )
                logger.info(f"Extracted {toponyms_count:,} toponyms from {places_count:,} places")
                logger.info(f"Skipped {skipped_count:,} pre-romanized/mismatched toponyms")

                # Checkpoint DB
                logger.info("--- Checkpointing database ---")
                conn.close()
                shutil.copy2(temp_db_path, final_db_path)

            # Reopen DB from Scratch (whether resumed or just created)
            conn = create_sqlite_db(str(temp_db_path))

            # PHASE 2: VOCABULARY GENERATION
            logger.info("=" * 60)
            logger.info("PHASE 2: VOCABULARY GENERATION")
            logger.info("=" * 60)
            vocab_stats, char_vocab = generate_vocabulary(conn, vocab_dir)
            logger.info(f"Vocabulary stats: {json.dumps(vocab_stats, indent=2)}")

            # PHASE 3: TRAINING DATA EXPORT (SQLite -> Parquet on scratch)
            if args.skip_training_export:
                logger.info("Skipping training data export (--skip-training-export)")
            else:
                logger.info("=" * 60)
                logger.info("PHASE 3: TRAINING DATA EXPORT (SQLite -> Parquet)")
                logger.info("=" * 60)

                # Write to scratch first for speed
                scratch_training_dir = temp_dir_path / 'training_output'
                scratch_training_dir.mkdir(parents=True, exist_ok=True)

                export_stats = export_training_parquet(
                    conn,
                    char_vocab,
                    scratch_training_dir,
                    args.training_namespaces,
                    args.train_ratio,
                    args.val_ratio,
                )

                # Copy from scratch to network storage
                logger.info("Copying training data from scratch to network storage...")
                import subprocess
                subprocess.run([
                    'rsync', '-av', '--progress',
                    str(scratch_training_dir) + '/',
                    str(output_dir) + '/'
                ], check=True)
                logger.info("Training data copied to network storage.")

            # PHASE 4: BUFFER TO JSONL (SQLite -> scratch) for ES indexing
            logger.info("=" * 60)
            logger.info("PHASE 4: BUFFERING (SQLite -> JSONL)")
            logger.info("=" * 60)
            total_docs = dump_to_jsonl(conn, jsonl_path)
            conn.close()

            if args.skip_es_index:
                logger.info("Skipping ES indexing (--skip-es-index)")
            else:
                # PHASE 5: INDEXING (Disk -> ES)
                logger.info("=" * 60)
                logger.info("PHASE 5: INDEXING (JSONL -> ES)")
                logger.info("=" * 60)

                if es.indices.exists(index=args.toponyms_index):
                    es.indices.delete(index=args.toponyms_index)

                with open(args.schema_path, 'r') as f:
                    schema = json.load(f)

                # Ensure refresh_interval is disabled for speed
                if 'settings' not in schema: schema['settings'] = {}
                schema['settings']['refresh_interval'] = "-1"

                es.indices.create(index=args.toponyms_index, body=schema)

                indexed = bulk_index_from_file(
                    es, jsonl_path, total_docs, args.toponyms_index, args.batch_size
                )

                # Finalize
                logger.info("--- FINALIZING ---")
                logger.info("Refreshing and creating snapshot...")
                es.indices.refresh(index=args.toponyms_index)
                create_checkpoint_snapshot(
                    es,
                    snapshot_name="rebuilt_toponyms",
                    repo_name=STAGING_REPO_NAME
                )

            logger.info("=" * 60)
            logger.info("SUCCESS")
            logger.info("=" * 60)
            logger.info(f"Database saved: {final_db_path}")
            logger.info(f"Output directory: {output_dir}")
            logger.info(f"  - Vocabulary: {vocab_dir}")
            if not args.skip_training_export:
                logger.info(f"  - Training data: {output_dir / 'training'}")
                logger.info(f"  - Splits: {output_dir / 'splits'}")
            if not args.skip_es_index:
                logger.info(f"ES index: {args.toponyms_index}")

        except Exception as e:
            logger.error(f"Process failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            sys.exit(1)


if __name__ == '__main__':
    main()