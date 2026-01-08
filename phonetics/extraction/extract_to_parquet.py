# extraction/extract_to_parquet.py
"""
Extract training data from ES toponyms index to Parquet format.

Two-Pass Extraction Strategy (per methodology in universal_topophones.tex):
    Pass 1 (Vocabulary): Scan the ENTIRE toponyms index (~67M records) to build
        the character vocabulary. This ensures no OOV errors at inference time.
        CJK/Japanese are romanised via AnyAscii; Korean Hangul is decomposed to Jamo.
    Pass 2 (Training Data): Extract training records filtered by specified namespaces
        (gn, wd, tgn by default), compute IPA/PanPhon features, and write to Parquet.

Output structure:
    /data/vN/
    ├── toponyms/
    │   ├── primary_namespace=gn/
    │   │   ├── script=LATIN/
    │   │   │   └── part-*.parquet
    │   │   └── ...
    │   └── ...
    ├── vocab/
    │   ├── char_vocab.json
    │   ├── script_vocab.json
    │   └── lang_vocab.json
    ├── splits/
    │   ├── train_ids.txt
    │   ├── val_ids.txt
    │   └── test_ids.txt
    └── extraction_stats.json

Usage:
    python -m phonetics.extraction.extract_to_parquet \
        --es-host localhost:9200 \
        --output-dir /ix1/whcdh/models/phonetic/data/v3 \
        --namespaces gn wd tgn \
        --train-ratio 0.8 \
        --val-ratio 0.1 \
        --workers 8
"""

import argparse
import hashlib
import json
import logging
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue
from threading import Lock
from typing import Dict, Iterator, List, Optional, Tuple

try:
    from elasticsearch import Elasticsearch
    from elasticsearch.helpers import scan
except ImportError:
    print("Error: elasticsearch package required")
    sys.exit(1)

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("Error: pyarrow package required")
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

try:
    from anyascii import anyascii
except ImportError:
    anyascii = None
    print("Warning: anyascii not available. CJK romanization will fail.")

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.utils.script_detection import Script, detect_script, should_romanize, should_decompose
from phonetics.utils.korean import decompose_text
from phonetics.vocab.char_vocab import (
    CharacterVocabulary, ScriptVocabulary, LanguageVocabulary
)

from processing.settings import ES_HOST

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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
    """Lazy-loaded IPA converter using Epitran."""

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


def preprocess_for_vocab(name: str, script: Script) -> str:
    """
    Preprocess a toponym for vocabulary building.

    Applies the same transformations that will be used during encoding:
    - CJK/Japanese: romanized via AnyAscii
    - Korean Hangul: decomposed to Jamo
    - Others: pass through (but normalized)

    Args:
        name: Raw toponym string
        script: Detected script of the toponym

    Returns:
        Preprocessed string ready for character extraction
    """
    if should_romanize(script):
        if anyascii is None:
            raise RuntimeError("anyascii required for CJK romanization")
        return anyascii(name).lower()

    if should_decompose(script):
        return decompose_text(name)

    return name.lower()


def build_vocabulary_from_corpus(
    es: Elasticsearch,
    index: str = 'toponyms',
    batch_size: int = 5000,
    num_workers: int = 4,
) -> Tuple[CharacterVocabulary, LanguageVocabulary, Dict]:
    """
    Pass 1: Build vocabulary from the ENTIRE toponyms corpus.

    This ensures all characters that might be encountered at inference time
    are included in the vocabulary, preventing OOV errors.

    Args:
        es: Elasticsearch client
        index: Index name
        batch_size: Scroll batch size
        num_workers: Number of parallel workers for processing

    Returns:
        Tuple of (char_vocab, lang_vocab, stats_dict)
    """
    logger.info("=" * 60)
    logger.info("PASS 1: Building vocabulary from entire corpus")
    logger.info("=" * 60)

    # Get total count
    total = es.count(index=index, body={"query": {"match_all": {}}})['count']
    logger.info(f"Total toponyms in index: {total:,}")

    char_vocab = CharacterVocabulary(allow_growth=True)
    lang_vocab = LanguageVocabulary()

    # Thread-safe counters
    stats_lock = Lock()
    stats = {
        'total_scanned': 0,
        'by_script': defaultdict(int),
        'by_lang': defaultdict(int),
        'unique_chars_by_script': defaultdict(set),
    }

    def process_batch(docs: List[Dict]) -> Dict:
        """Process a batch of documents and extract vocabulary."""
        local_chars = defaultdict(set)
        local_langs = set()
        local_scripts = defaultdict(int)

        for doc in docs:
            name = doc.get('name', '')
            lang = doc.get('lang', '')
            script_str = doc.get('script', 'OTHER')

            if not name:
                continue

            try:
                script = Script(script_str)
            except ValueError:
                script = Script.OTHER

            # Preprocess according to script type
            processed = preprocess_for_vocab(name, script)

            # Extract unique characters
            for char in processed:
                if char.strip():  # Skip whitespace
                    local_chars[script].add(char)

            local_scripts[script] += 1

            if lang:
                local_langs.add(lang)

        return {
            'chars': local_chars,
            'langs': local_langs,
            'scripts': local_scripts,
            'count': len(docs),
        }

    # Parallel processing with batched scroll
    query = {"query": {"match_all": {}}}

    # Collect batches for parallel processing
    batch_buffer = []
    buffer_size = num_workers * 2  # Process this many batches in parallel

    iterator = scan(es, index=index, query=query, scroll='30m', size=batch_size)
    if tqdm:
        iterator = tqdm(iterator, total=total, desc="Scanning corpus for vocabulary")

    current_batch = []
    for doc in iterator:
        current_batch.append(doc['_source'])

        if len(current_batch) >= batch_size:
            batch_buffer.append(current_batch)
            current_batch = []

            # Process batches in parallel when buffer is full
            if len(batch_buffer) >= buffer_size:
                with ThreadPoolExecutor(max_workers=num_workers) as executor:
                    futures = [executor.submit(process_batch, b) for b in batch_buffer]
                    for future in as_completed(futures):
                        result = future.result()
                        with stats_lock:
                            stats['total_scanned'] += result['count']
                            for script, chars in result['chars'].items():
                                stats['unique_chars_by_script'][script].update(chars)
                            for script, count in result['scripts'].items():
                                stats['by_script'][script] += count
                            for lang in result['langs']:
                                lang_vocab.add(lang)
                                stats['by_lang'][lang] += 1
                batch_buffer = []

    # Process remaining batches
    if current_batch:
        batch_buffer.append(current_batch)

    if batch_buffer:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(process_batch, b) for b in batch_buffer]
            for future in as_completed(futures):
                result = future.result()
                with stats_lock:
                    stats['total_scanned'] += result['count']
                    for script, chars in result['chars'].items():
                        stats['unique_chars_by_script'][script].update(chars)
                    for script, count in result['scripts'].items():
                        stats['by_script'][script] += count
                    for lang in result['langs']:
                        lang_vocab.add(lang)
                        stats['by_lang'][lang] += 1

    # Build vocabulary from collected characters
    logger.info("Building character vocabulary from collected characters...")
    for script, chars in stats['unique_chars_by_script'].items():
        for char in chars:
            char_vocab.get_char_id(char)

    # Convert sets to counts for JSON serialization
    char_counts = {script.value: len(chars) for script, chars in stats['unique_chars_by_script'].items()}

    final_stats = {
        'total_scanned': stats['total_scanned'],
        'by_script': {k.value if hasattr(k, 'value') else k: v for k, v in stats['by_script'].items()},
        'unique_chars_by_script': char_counts,
        'vocab_size': len(char_vocab),
        'num_languages': len(lang_vocab),
    }

    logger.info(f"Vocabulary built: {len(char_vocab):,} characters, {len(lang_vocab):,} languages")
    logger.info(f"Script distribution: {dict(sorted(final_stats['by_script'].items(), key=lambda x: -x[1])[:10])}")

    return char_vocab, lang_vocab, final_stats


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


TOPONYM_SCHEMA = pa.schema([
    ('toponym_id', pa.string()),
    ('name', pa.string()),
    ('name_normalized', pa.string()),
    ('script', pa.string()),
    ('lang', pa.string()),
    ('epitran_code', pa.string()),
    ('epitran_supported', pa.bool_()),
    ('namespaces', pa.list_(pa.string())),
    ('char_ids', pa.list_(pa.int16())),
    ('char_length', pa.int16()),
    ('ipa', pa.string()),
    ('features', pa.list_(pa.float32())),
    ('feature_length', pa.int16()),
    ('split_hash', pa.int32()),
    ('split', pa.string()),
])


def scan_toponyms(
        es: Elasticsearch,
        index: str = 'toponyms',
        namespaces: Optional[List[str]] = None,
        batch_size: int = 1000,
) -> Iterator[Dict]:
    """Scan toponyms index, optionally filtering by namespace."""
    if namespaces:
        query = {"query": {"terms": {"primary_namespace": namespaces}}}
    else:
        query = {"query": {"match_all": {}}}
    for doc in scan(es, index=index, query=query, scroll='10m', size=batch_size):
        yield doc['_source']


class ParquetWriter:
    """Partitioned Parquet writer."""

    def __init__(
            self,
            output_dir: Path,
            schema: pa.Schema,
            partition_cols: List[str],
            batch_size: int = 50000,
    ):
        self.output_dir = output_dir
        self.schema = schema
        self.partition_cols = partition_cols
        self.batch_size = batch_size
        self._buffers: Dict[Tuple, List[Dict]] = defaultdict(list)
        self._part_counts: Dict[Tuple, int] = defaultdict(int)
        self._total_written = 0

    def add(self, record: Dict, partition_key: Tuple):
        self._buffers[partition_key].append(record)
        if len(self._buffers[partition_key]) >= self.batch_size:
            self._flush_partition(partition_key)

    def _flush_partition(self, partition_key: Tuple):
        records = self._buffers[partition_key]
        if not records:
            return
        parts = [f"{col}={val}" for col, val in zip(self.partition_cols, partition_key)]
        partition_dir = self.output_dir / '/'.join(parts)
        partition_dir.mkdir(parents=True, exist_ok=True)
        part_num = self._part_counts[partition_key]
        self._part_counts[partition_key] += 1
        output_path = partition_dir / f"part-{part_num:04d}.parquet"
        table = pa.Table.from_pylist(records, schema=self.schema)
        pq.write_table(table, output_path, compression='snappy')
        self._total_written += len(records)
        self._buffers[partition_key] = []
        logger.debug(f"Wrote {len(records)} records to {output_path}")

    def flush_all(self):
        for partition_key in list(self._buffers.keys()):
            self._flush_partition(partition_key)

    @property
    def total_written(self) -> int:
        return self._total_written


def extract_to_parquet(
        es: Elasticsearch,
        output_dir: Path,
        namespaces: List[str],
        train_ratio: float = 0.8,
        val_ratio: float = 0.1,
        batch_size: int = 1000,
        num_workers: int = 4,
        limit: Optional[int] = None,
        skip_vocab_pass: bool = False,
) -> Dict:
    """
    Extract toponyms to partitioned Parquet files using two-pass strategy.

    Pass 1: Scan ENTIRE corpus to build vocabulary (prevents OOV at inference)
    Pass 2: Extract training records from specified namespaces

    Args:
        es: Elasticsearch client
        output_dir: Output directory
        namespaces: Namespaces to include in training data
        train_ratio: Proportion for training split
        val_ratio: Proportion for validation split
        batch_size: ES scroll batch size
        num_workers: Number of parallel workers for Pass 1
        limit: Optional limit on records (for testing)
        skip_vocab_pass: Skip Pass 1 if vocabulary already exists

    Returns:
        Statistics dictionary
    """
    logger.info(f"Extracting toponyms to {output_dir}")
    logger.info(f"Training namespaces: {namespaces}")
    logger.info(f"Workers: {num_workers}")

    toponyms_dir = output_dir / 'toponyms'
    vocab_dir = output_dir / 'vocab'
    splits_dir = output_dir / 'splits'
    for d in [toponyms_dir, vocab_dir, splits_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # PASS 1: Build vocabulary from entire corpus
    # =========================================================================
    vocab_path = vocab_dir / 'char_vocab.json'

    if skip_vocab_pass and vocab_path.exists():
        logger.info("Loading existing vocabulary (skip_vocab_pass=True)")
        char_vocab = CharacterVocabulary.load(str(vocab_path), allow_growth=False)
        lang_vocab = LanguageVocabulary.load(str(vocab_dir / 'lang_vocab.json'))
        vocab_stats = {}
    else:
        char_vocab, lang_vocab, vocab_stats = build_vocabulary_from_corpus(
            es, 'toponyms', batch_size=5000, num_workers=num_workers
        )
        # Save vocabulary immediately as checkpoint
        logger.info("Saving vocabulary (checkpoint)...")
        char_vocab.save(vocab_dir / 'char_vocab.json')
        lang_vocab.save(vocab_dir / 'lang_vocab.json')

    # Script vocab is static
    script_vocab = ScriptVocabulary()
    script_vocab.save(vocab_dir / 'script_vocab.json')

    # =========================================================================
    # PASS 2: Extract training data from specified namespaces
    # =========================================================================
    logger.info("=" * 60)
    logger.info("PASS 2: Extracting training data from specified namespaces")
    logger.info("=" * 60)
    logger.info(f"Namespaces: {namespaces}")

    ipa_converter = IPAConverter()

    writer = ParquetWriter(
        toponyms_dir, TOPONYM_SCHEMA,
        partition_cols=['primary_namespace', 'script'],
        batch_size=50000,
    )

    split_ids = {'train': [], 'val': [], 'test': []}
    stats = {
        'total_processed': 0,
        'with_ipa': 0,
        'with_features': 0,
        'by_script': defaultdict(int),
        'by_namespace': defaultdict(int),
        'by_split': defaultdict(int),
        'by_lang': defaultdict(int),
    }

    # Get count for filtered namespaces
    query = {"query": {"terms": {"primary_namespace": namespaces}}}
    total = es.count(index='toponyms', body=query)['count']
    if limit:
        total = min(total, limit)
    logger.info(f"Processing {total:,} toponyms from namespaces {namespaces}")

    iterator = scan_toponyms(es, 'toponyms', namespaces, batch_size)
    if tqdm:
        iterator = tqdm(iterator, total=total, desc="Extracting training data")

    for doc in iterator:
        toponym_id = doc['toponym_id']
        name = doc['name']
        lang = doc.get('lang') or ''
        script_str = doc.get('script', 'OTHER')
        namespaces_list = doc.get('namespaces', [])
        primary_ns = doc.get('primary_namespace', 'other')

        try:
            script = Script(script_str)
        except ValueError:
            script = Script.OTHER

        name_normalized = name.lower().strip()
        epitran_code = ipa_converter.get_epitran_code(lang, script)
        epitran_supported = epitran_code is not None

        ipa = None
        features = None
        feature_length = 0

        if epitran_supported:
            ipa = ipa_converter.to_ipa(name, lang, script)
            if ipa:
                stats['with_ipa'] += 1
                features = ipa_converter.to_features(ipa)
                if features:
                    feature_length = len(features) // 24
                    stats['with_features'] += 1

        # Use pre-built vocabulary (allow_growth should be False for Pass 2)
        char_ids = char_vocab.encode(name, script)

        split_hash = compute_split_hash(toponym_id)
        split = assign_split(split_hash, train_ratio, val_ratio)
        split_ids[split].append(toponym_id)

        record = {
            'toponym_id': toponym_id,
            'name': name,
            'name_normalized': name_normalized,
            'script': script.value,
            'lang': lang if lang else None,
            'epitran_code': epitran_code,
            'epitran_supported': epitran_supported,
            'namespaces': namespaces_list,
            'char_ids': char_ids,
            'char_length': len(char_ids),
            'ipa': ipa,
            'features': features,
            'feature_length': feature_length,
            'split_hash': split_hash,
            'split': split,
        }

        partition_key = (primary_ns, script.value)
        writer.add(record, partition_key)

        stats['total_processed'] += 1
        stats['by_script'][script.value] += 1
        stats['by_namespace'][primary_ns] += 1
        stats['by_split'][split] += 1
        if lang:
            stats['by_lang'][lang] += 1

        if limit and stats['total_processed'] >= limit:
            break

    writer.flush_all()

    logger.info("Saving split files")
    for split_name, ids in split_ids.items():
        with open(splits_dir / f'{split_name}_ids.txt', 'w') as f:
            f.write('\n'.join(ids))

    # Finalize stats
    stats['by_script'] = dict(stats['by_script'])
    stats['by_namespace'] = dict(stats['by_namespace'])
    stats['by_split'] = dict(stats['by_split'])
    stats['by_lang'] = dict(sorted(stats['by_lang'].items(), key=lambda x: -x[1])[:50])  # Top 50 languages
    stats['vocab_size'] = len(char_vocab)
    stats['num_languages'] = len(lang_vocab)
    stats['vocab_stats'] = vocab_stats

    with open(output_dir / 'extraction_stats.json', 'w') as f:
        json.dump(stats, f, indent=2)

    logger.info("=" * 60)
    logger.info("EXTRACTION COMPLETE")
    logger.info("=" * 60)
    logger.info(f"  Total processed: {stats['total_processed']:,}")
    logger.info(f"  With IPA: {stats['with_ipa']:,}")
    logger.info(f"  With features: {stats['with_features']:,}")
    logger.info(f"  Vocabulary size: {stats['vocab_size']:,}")
    logger.info(f"  Languages: {stats['num_languages']:,}")
    logger.info(f"  Splits: {stats['by_split']}")
    logger.info(f"  Top scripts: {dict(sorted(stats['by_script'].items(), key=lambda x: -x[1])[:10])}")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description='Extract training data from ES toponyms to Parquet (two-pass strategy)'
    )
    parser.add_argument('--es-host', default=ES_HOST,
                        help='Elasticsearch host URL')
    parser.add_argument('--toponyms-index', default='toponyms',
                        help='Toponyms index name')
    parser.add_argument('--output-dir', type=Path, required=True,
                        help='Output directory for Parquet files')
    parser.add_argument('--namespaces', nargs='+', default=['gn', 'wd', 'tgn'],
                        help='Namespaces to include in training data (default: gn wd tgn)')
    parser.add_argument('--train-ratio', type=float, default=0.8,
                        help='Train split ratio (default: 0.8)')
    parser.add_argument('--val-ratio', type=float, default=0.1,
                        help='Validation split ratio (default: 0.1)')
    parser.add_argument('--batch-size', type=int, default=2000,
                        help='ES scroll batch size (default: 2000)')
    parser.add_argument('--workers', type=int, default=8,
                        help='Number of parallel workers for vocabulary building (default: 8)')
    parser.add_argument('--limit', type=int, default=None,
                        help='Limit number of records (for testing)')
    parser.add_argument('--skip-vocab-pass', action='store_true',
                        help='Skip vocabulary pass if vocab already exists')
    args = parser.parse_args()

    if args.train_ratio + args.val_ratio >= 1.0:
        logger.error("train_ratio + val_ratio must be < 1.0")
        sys.exit(1)

    es = Elasticsearch(args.es_host)
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {args.es_host}")
        sys.exit(1)
    logger.info(f"Connected to Elasticsearch at {args.es_host}")

    extract_to_parquet(
        es=es,
        output_dir=args.output_dir,
        namespaces=args.namespaces,
        train_ratio=args.train_ratio,
        val_ratio=args.val_ratio,
        batch_size=args.batch_size,
        num_workers=args.workers,
        limit=args.limit,
        skip_vocab_pass=args.skip_vocab_pass,
    )


if __name__ == '__main__':
    main()