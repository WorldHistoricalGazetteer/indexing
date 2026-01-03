# extraction/extract_to_parquet.py
"""
Extract training data from ES toponyms index to Parquet format.

This script:
1. Scans the ES toponyms index (after rebuild)
2. Filters by specified namespaces (excluding OSM by default)
3. Computes IPA and PanPhon features for supported languages
4. Builds script-partitioned character vocabulary
5. Encodes all toponyms
6. Writes partitioned Parquet files

Output structure:
    /data/v2/
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
    └── splits/
        ├── train_ids.txt
        ├── val_ids.txt
        └── test_ids.txt

Usage:
    python -m phonetics.extraction.extract_to_parquet \
        --es-host localhost:9200 \
        --output-dir /ix1/whcdh/models/phonetic/data/v2 \
        --namespaces gn wd tgn pl iv gb \
        --train-ratio 0.8 \
        --val-ratio 0.1
"""

import argparse
import hashlib
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from processing.utilities import create_checkpoint_snapshot

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

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.utils.script_detection import Script, detect_script
from phonetics.vocab.char_vocab import (
    CharacterVocabulary, ScriptVocabulary, LanguageVocabulary
)

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
        limit: Optional[int] = None,
) -> Dict:
    """Extract toponyms to partitioned Parquet files."""
    logger.info(f"Extracting toponyms to {output_dir}")
    logger.info(f"Namespaces: {namespaces}")

    toponyms_dir = output_dir / 'toponyms'
    vocab_dir = output_dir / 'vocab'
    splits_dir = output_dir / 'splits'
    for d in [toponyms_dir, vocab_dir, splits_dir]:
        d.mkdir(parents=True, exist_ok=True)

    ipa_converter = IPAConverter()
    char_vocab = CharacterVocabulary(allow_growth=True)
    script_vocab = ScriptVocabulary()
    lang_vocab = LanguageVocabulary()

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
    }

    # Get count
    if namespaces:
        query = {"query": {"terms": {"primary_namespace": namespaces}}}
    else:
        query = {"query": {"match_all": {}}}
    total = es.count(index='toponyms', body=query)['count']
    if limit:
        total = min(total, limit)
    logger.info(f"Processing {total:,} toponyms")

    iterator = scan_toponyms(es, 'toponyms', namespaces, batch_size)
    if tqdm:
        iterator = tqdm(iterator, total=total, desc="Extracting")

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

        char_ids = char_vocab.encode(name, script)
        if lang:
            lang_vocab.add(lang)

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

        if limit and stats['total_processed'] >= limit:
            break

    writer.flush_all()

    logger.info("Saving vocabularies")
    char_vocab.save(vocab_dir / 'char_vocab.json')
    script_vocab.save(vocab_dir / 'script_vocab.json')
    lang_vocab.save(vocab_dir / 'lang_vocab.json')

    logger.info("Saving split files")
    for split_name, ids in split_ids.items():
        with open(splits_dir / f'{split_name}_ids.txt', 'w') as f:
            f.write('\n'.join(ids))

    stats['by_script'] = dict(stats['by_script'])
    stats['by_namespace'] = dict(stats['by_namespace'])
    stats['by_split'] = dict(stats['by_split'])
    stats['vocab_size'] = len(char_vocab)
    stats['num_languages'] = len(lang_vocab)

    with open(output_dir / 'extraction_stats.json', 'w') as f:
        json.dump(stats, f, indent=2)

    logger.info("Extraction complete!")
    logger.info(f"  Total processed: {stats['total_processed']:,}")
    logger.info(f"  With IPA: {stats['with_ipa']:,}")
    logger.info(f"  With features: {stats['with_features']:,}")
    logger.info(f"  Vocabulary size: {stats['vocab_size']:,}")
    logger.info(f"  Languages: {stats['num_languages']:,}")
    logger.info(f"  Splits: {stats['by_split']}")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description='Extract training data from ES toponyms to Parquet'
    )
    parser.add_argument('--es-host', default='localhost:9200')
    parser.add_argument('--toponyms-index', default='toponyms')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--namespaces', nargs='+', default=['gn', 'pl', 'iv'])
    parser.add_argument('--train-ratio', type=float, default=0.8)
    parser.add_argument('--val-ratio', type=float, default=0.1)
    parser.add_argument('--batch-size', type=int, default=1000)
    parser.add_argument('--limit', type=int, default=None)
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
        limit=args.limit,
    )


if __name__ == '__main__':
    main()