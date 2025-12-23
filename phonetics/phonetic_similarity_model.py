"""
Phonetic Similarity Model for Multilingual Toponym Matching

A Student-Teacher architecture that learns phonetic embeddings from toponyms.
- Teacher: Epitran + PanPhon → IPA features → BiLSTM (phonetically grounded)
- Student: anyascii + Language ID → BiLSTM (universal fallback)

Uses HDF5 for memory-efficient training on large datasets.
Memory footprint stays constant (~100MB) regardless of dataset size.

Training proceeds in three phases:
1. Train Teacher on IPA features (triplet loss)
2. Align Student to Teacher (MSE + cosine loss)
3. Fine-tune Student on all data including non-Epitran languages (triplet loss with hard negatives)

Requirements:
    pip install torch epitran panphon anyascii elasticsearch h5py pybloom-live

Usage:
    # Phase 0: Extract data from Elasticsearch
    python phonetic_similarity_model.py --phase 0 --es-host localhost:9200 --index places --output data.pkl

    # Phase 1: Train phonetic encoder (Teacher)
    python phonetic_similarity_model.py --phase 1 --data data.pkl --output phase1.pt

    # Phase 2: Alignment training
    python phonetic_similarity_model.py --phase 2 --data data.pkl --phase1-model phase1.pt --output phase2.pt

    # Phase 3: Generalization training
    python phonetic_similarity_model.py --phase 3 --data data.pkl --phase2-model phase2.pt --output final_model.pt

    # Inference
    python phonetic_similarity_model.py --infer --model final_model.pt --toponym1 "London" --lang1 "en" --toponym2 "Londres" --lang2 "fr"

"""

import os
import random
import pickle
import argparse
import orjson
from collections import defaultdict
from typing import Optional, List, Tuple, Dict, Any

import numpy as np
import h5py
import torch
import torch.nn as nn
import torch.nn.functional as F
from elasticsearch import helpers
from torch.utils.data import Dataset, DataLoader

from streaming_datasets import (
    StreamingPhase1Dataset,
    StreamingPhase2Dataset,
    StreamingPhase3Dataset
)

# Optional imports with graceful fallback
try:
    from anyascii import anyascii
except ImportError:
    raise ImportError("Please install anyascii: pip install anyascii")

try:
    import epitran
    from panphon import FeatureTable
    import panphon.distance
except ImportError:
    raise ImportError("Please install epitran and panphon: pip install epitran panphon")

try:
    from pybloom_live import BloomFilter
    HAVE_BLOOM = True
except ImportError:
    print("Warning: pybloom-live not installed. Using set-based deduplication (higher memory).")
    print("Install with: pip install pybloom-live")
    HAVE_BLOOM = False

from processing.utilities import create_checkpoint_snapshot

# =============================================================================
# Configuration
# =============================================================================

class Config:
    """Model and training configuration."""

    # Vocabulary
    VOCAB_SIZE = 10000
    NUM_LANGS = 300

    # Model dimensions
    CHAR_EMBED_DIM = 64
    LANG_EMBED_DIM = 32
    PHONETIC_FEAT_DIM = 24  # PanPhon feature dimension
    HIDDEN_DIM = 128
    EMBED_DIM = 64
    NUM_LAYERS = 2
    DROPOUT = 0.2

    # Training
    BATCH_SIZE = 256
    SUBSAMPLE_PAIRS = 5000000
    LEARNING_RATE = 1e-3
    PHASE1_EPOCHS = 50
    PHASE2_EPOCHS = 30
    PHASE3_EPOCHS = 20
    TRIPLET_MARGIN = 0.3
    ALIGNMENT_COSINE_WEIGHT = 0.5

    # Data
    SIMILARITY_THRESHOLD = 0.5
    MAX_TOPONYM_LEN = 50

    # Epitran language mappings (ISO 639-1 → Epitran code)
    EPITRAN_LANGS = {
        'af': 'afr-Latn',
        'am': 'amh-Ethi',
        'ar': 'ara-Arab',
        'az': 'aze-Latn',
        'be': 'bel-Cyrl',
        'bg': 'bul-Cyrl',
        'bn': 'ben-Beng',
        'bs': 'bos-Latn',
        'ca': 'cat-Latn',
        'cs': 'ces-Latn',
        'cy': 'cym-Latn',
        'da': 'dan-Latn',
        'de': 'deu-Latn',
        'el': 'ell-Grek',
        'en': 'eng-Latn',
        'es': 'spa-Latn',
        'et': 'est-Latn',
        'fa': 'fas-Arab',
        'fi': 'fin-Latn',
        'fr': 'fra-Latn',
        'ga': 'gle-Latn',
        'ha': 'hau-Latn',
        'he': 'heb-Hebr',
        'hi': 'hin-Deva',
        'hr': 'hrv-Latn',
        'hu': 'hun-Latn',
        'hy': 'hye-Armn',
        'id': 'ind-Latn',
        'is': 'isl-Latn',
        'it': 'ita-Latn',
        'ja': 'jpn-Hrgn',  # Hiragana only
        'ka': 'kat-Geor',
        'kk': 'kaz-Cyrl',
        'km': 'khm-Khmr',
        'ko': 'kor-Hang',
        'ky': 'kir-Cyrl',
        'la': 'lat-Latn',
        'lt': 'lit-Latn',
        'lv': 'lav-Latn',
        'mk': 'mkd-Cyrl',
        'ml': 'mal-Mlym',
        'mn': 'mon-Cyrl',
        'mr': 'mar-Deva',
        'ms': 'msa-Latn',
        'my': 'mya-Mymr',
        'nl': 'nld-Latn',
        'no': 'nor-Latn',
        'pa': 'pan-Guru',
        'pl': 'pol-Latn',
        'pt': 'por-Latn',
        'ro': 'ron-Latn',
        'ru': 'rus-Cyrl',
        'si': 'sin-Sinh',
        'sk': 'slk-Latn',
        'sl': 'slv-Latn',
        'sq': 'sqi-Latn',
        'sr': 'srp-Cyrl',
        'sv': 'swe-Latn',
        'sw': 'swa-Latn',
        'ta': 'tam-Taml',
        'te': 'tel-Telu',
        'th': 'tha-Thai',
        'tl': 'tgl-Latn',
        'tr': 'tur-Latn',
        'uk': 'ukr-Cyrl',
        'ur': 'urd-Arab',
        'uz': 'uzb-Latn',
        'vi': 'vie-Latn',
        'yo': 'yor-Latn',
        'zh': 'cmn-Hans',  # Simplified Chinese
        'zh-Hans': 'cmn-Hans',
        'zh-Hant': 'cmn-Hans',  # <--- Approximate, but better than nothing
        'zh-cn': 'cmn-Hans',    # <--- USEFUL ALIAS
    }


# =============================================================================
# Vocabulary Management
# =============================================================================

class CharVocab:
    """
    Character vocabulary with stable hashing for unseen characters.
    Built from training data, with fallback hash for inference on unseen chars.
    """

    PAD_TOKEN = '<PAD>'
    UNK_TOKEN = '<UNK>'

    def __init__(self, vocab_size: int = Config.VOCAB_SIZE):
        self.vocab_size = vocab_size
        self.char_to_id = {self.PAD_TOKEN: 0, self.UNK_TOKEN: 1}
        self.id_to_char = {0: self.PAD_TOKEN, 1: self.UNK_TOKEN}
        self.next_id = 2
        self.frozen = False
        self.hash_space_start = None

    def fit(self, hdf5_path: str) -> 'CharVocab':
        """Build vocabulary from HDF5 training data."""
        print("Building character vocabulary...")
        char_counts = defaultdict(int)

        with h5py.File(hdf5_path, 'r') as f:
            items = f['items']
            total_items = f.attrs['total_items']

            for idx in range(total_items):
                text = items['romanized'][idx]
                # Handle bytes if stored as bytes
                if isinstance(text, bytes):
                    text = text.decode('utf-8')
                for c in text:
                    char_counts[c] += 1

        # Reserve 20% of vocab for hash fallback
        max_vocab_chars = int(self.vocab_size * 0.8)

        # Add most frequent characters to vocab
        for char, _ in sorted(char_counts.items(), key=lambda x: -x[1]):
            if self.next_id >= max_vocab_chars:
                break
            if char not in self.char_to_id:
                self.char_to_id[char] = self.next_id
                self.id_to_char[self.next_id] = char
                self.next_id += 1

        self.hash_space_start = self.next_id
        self.frozen = True

        hash_slots = self.vocab_size - self.hash_space_start
        print(f"CharVocab: {len(self.char_to_id)} explicit chars, {hash_slots} hash slots")

        return self

    def encode(self, text: str, max_len: int = Config.MAX_TOPONYM_LEN) -> List[int]:
        """Convert text to character IDs."""
        ids = []
        for c in text[:max_len]:
            if c in self.char_to_id:
                ids.append(self.char_to_id[c])
            elif self.hash_space_start is not None:
                # Stable hash for unseen characters using FNV-1a variant
                h = ((ord(c) * 2654435761) % (self.vocab_size - self.hash_space_start)) + self.hash_space_start
                ids.append(h)
            else:
                ids.append(self.char_to_id[self.UNK_TOKEN])
        return ids

    def save(self, path: str):
        """Save vocabulary to file."""
        data = {
            'char_to_id': self.char_to_id,
            'id_to_char': self.id_to_char,
            'vocab_size': self.vocab_size,
            'next_id': self.next_id,
            'hash_space_start': self.hash_space_start,
            'frozen': self.frozen
        }
        with open(path, 'wb') as f:
            pickle.dump(data, f)

    @classmethod
    def load(cls, path: str) -> 'CharVocab':
        """Load vocabulary from file."""
        with open(path, 'rb') as f:
            data = pickle.load(f)

        vocab = cls(data['vocab_size'])
        vocab.char_to_id = data['char_to_id']
        vocab.id_to_char = data['id_to_char']
        vocab.next_id = data['next_id']
        vocab.hash_space_start = data['hash_space_start']
        vocab.frozen = data['frozen']
        return vocab


class LangVocab:
    """
    Language vocabulary mapping ISO 639-1 codes to integer IDs.
    """

    UNK_LANG = '<UNK_LANG>'

    def __init__(self):
        self.lang_to_id = {self.UNK_LANG: 0}
        self.id_to_lang = {0: self.UNK_LANG}
        self.next_id = 1

    def fit(self, hdf5_path: str) -> 'LangVocab':
        """Build vocabulary from HDF5 training data."""
        print("Building language vocabulary...")
        with h5py.File(hdf5_path, 'r') as f:
            items = f['items']
            total_items = f.attrs['total_items']

            languages = set()
            for idx in range(total_items):
                lang = items['lang'][idx]
                if isinstance(lang, bytes):
                    lang = lang.decode('utf-8')
                languages.add(lang)

            for lang in sorted(languages):
                if lang not in self.lang_to_id:
                    self.lang_to_id[lang] = self.next_id
                    self.id_to_lang[self.next_id] = lang
                    self.next_id += 1

        print(f"LangVocab: {len(self.lang_to_id)} languages")
        return self

    def encode(self, lang: str) -> int:
        """Convert language code to ID."""
        return self.lang_to_id.get(lang, self.lang_to_id[self.UNK_LANG])

    def save(self, path: str):
        """Save vocabulary to file."""
        data = {
            'lang_to_id': self.lang_to_id,
            'id_to_lang': self.id_to_lang,
            'next_id': self.next_id
        }
        with open(path, 'wb') as f:
            pickle.dump(data, f)

    @classmethod
    def load(cls, path: str) -> 'LangVocab':
        """Load vocabulary from file."""
        with open(path, 'rb') as f:
            data = pickle.load(f)

        vocab = cls()
        vocab.lang_to_id = data['lang_to_id']
        vocab.id_to_lang = data['id_to_lang']
        vocab.next_id = data['next_id']
        return vocab


# =============================================================================
# Data Extraction
# =============================================================================

class ToponymEnricher:
    """
    Scans the 'toponyms' index and computes phonetic features for supported languages.
    Updates the documents in-place so they can be retrieved quickly later.
    """

    def __init__(self, es_host='localhost:9200', index='toponyms'):
        from elasticsearch import Elasticsearch
        self.es = Elasticsearch([es_host], request_timeout=60)
        self.index = index
        self.ft = FeatureTable()
        self._epi_cache = {}

        # Load all Epitran models upfront (optional, but good for speed)
        print("Loading Epitran models...")
        for lang in Config.EPITRAN_LANGS:
            self._get_epitran(lang)

    def _get_epitran(self, lang_code):
        if lang_code not in self._epi_cache:
            epitran_code = Config.EPITRAN_LANGS.get(lang_code)
            try:
                self._epi_cache[lang_code] = epitran.Epitran(epitran_code) if epitran_code else None
            except:
                self._epi_cache[lang_code] = None
        return self._epi_cache[lang_code]

    def _compute_phonetics(self, name, lang):
        epi = self._get_epitran(lang)
        if not epi: return None

        try:
            ipa = epi.transliterate(name)
            if not ipa: return None
            features = self.ft.word_to_vector_list(ipa, numeric=True)
            if not features: return None
            return {'ipa': ipa, 'features': features}
        except:
            return None

    def run(self):
        print(f"Adding phonetic fields to index '{self.index}' mapping...")
        # 1. Update Mapping (Safe: non-destructive)
        try:
            self.es.indices.put_mapping(index=self.index, body={
                "properties": {
                    "ipa_cached": {"type": "keyword", "index": False, "doc_values": False},
                    "features_cached_json": {"type": "keyword", "index": False, "doc_values": False}
                }
            })
        except Exception as e:
            print(f"Mapping update warning (might already exist): {e}")

        # 2. Query: Find docs in supported langs that MISS the cache
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"lang": list(Config.EPITRAN_LANGS.keys())}},
                        {"bool": {"must_not": {"exists": {"field": "ipa_cached"}}}}
                    ]
                }
            }
        }

        print("Scanning and enriching toponyms...")
        updates = []
        count = 0

        try:
            # Scan efficiently
            scanner = helpers.scan(
                self.es,
                query=query,
                index=self.index,
                _source=["name", "lang"],
                size=1000,
                scroll='2h'
            )

            for hit in scanner:
                doc_id = hit['_id']
                name = hit['_source'].get('name')
                lang = hit['_source'].get('lang')

                if not name or not lang: continue

                result = self._compute_phonetics(name, lang)

                if result:
                    updates.append({
                        "_op_type": "update",
                        "_index": self.index,
                        "_id": doc_id,
                        "doc": {
                            "ipa_cached": result['ipa'],
                            # Serialize list-of-lists to JSON string to prevent flattening
                            "features_cached_json": orjson.dumps(
                                result['features'],
                                option=orjson.OPT_SERIALIZE_NUMPY
                            ).decode('utf-8')
                        }
                    })

                if len(updates) >= 500:
                    helpers.bulk(self.es, updates, request_timeout=60)
                    count += len(updates)
                    print(f"  Enriched {count} documents...", end='\r', flush=True)
                    updates = []

            if updates:
                helpers.bulk(self.es, updates)
                count += len(updates)

            print(f"\nDone! Enriched {count} documents. The 'toponyms' index is now hydrated.")

        except KeyboardInterrupt:
            print("\n\n[Warning] Process interrupted by user.")
        except Exception as e:
            print(f"\n\n[Error] Process crashed: {e}")
        finally:
            # AUTOMATIC SAFETY SNAPSHOT
            print("\n" + "="*60)
            print("AUTOMATIC CHECKPOINT TRIGGERED")
            print("="*60)

            # If we processed anything, or if it was a long run, save it.
            if count > 0:
                create_checkpoint_snapshot(self.es, snapshot_name="toponym_enrichment_checkpoint")
            else:
                print("No documents were enriched, skipping snapshot.")


class TrainingDataExtractor:
    """
    Extracts clusters from 'places' index and looks up phonetics from 'toponyms'.
    Uses Single-Pass HDF5 writing with global pair deduplication.
    """

    def __init__(self, es_host='localhost:9200', index_name='places'):
        from elasticsearch import Elasticsearch
        self.es = Elasticsearch([es_host])
        self.index = index_name
        self.dst = panphon.distance.Distance()

    def phonetic_similarity(self, ipa_a: str, ipa_b: str) -> float:
        if not ipa_a or not ipa_b:
            return 0.0
        try:
            fed = self.dst.feature_edit_distance(ipa_a, ipa_b)
            # Simple length normalization
            max_len = max(len(ipa_a), len(ipa_b)) / 3  # Rough approximation of segments
            if max_len == 0:
                return 0.0
            return max(0.0, 1.0 - (fed / max(1, max_len)))
        except:
            return 0.0

    def _normalize_str(self, s):
        """Normalize string for comparison (handle bytes, strip, lowercase)."""
        if isinstance(s, bytes):
            s = s.decode('utf-8')
        return s.lower().strip()

    def _make_pair_key(self, top_a: str, lang_a: str, top_b: str, lang_b: str) -> str:
        """
        Create canonical pair key for deduplication.
        Ensures (A,B) and (B,A) have the same key.
        """
        # Normalize
        top_a = self._normalize_str(top_a)
        lang_a = self._normalize_str(lang_a)
        top_b = self._normalize_str(top_b)
        lang_b = self._normalize_str(lang_b)

        # Canonical ordering (alphabetical)
        if (top_a, lang_a) > (top_b, lang_b):
            top_a, lang_a, top_b, lang_b = top_b, lang_b, top_a, lang_a

        return f"{top_a}@{lang_a}|{top_b}@{lang_b}"

    def extract_and_stream(self, output_path: str, max_docs=None):
        """
        Extract training data with global pair deduplication.

        Args:
            output_path: Path to output HDF5 file
            max_docs: Maximum documents to process (None = all)
        """
        print(f"Starting Harvest with Global Pair Deduplication")
        print(f"Output: {output_path}")
        print(f"Deduplication method: {'Bloom filter' if HAVE_BLOOM else 'Set (high memory)'}")
        print()

        # Initialize deduplication structures
        if HAVE_BLOOM:
            # Bloom filter: memory-efficient, ~0.1% false positive rate acceptable
            seen_phonetic = BloomFilter(capacity=50_000_000, error_rate=0.001)
            seen_non_phonetic = BloomFilter(capacity=50_000_000, error_rate=0.001)
        else:
            # Fallback to sets (higher memory but exact)
            seen_phonetic = set()
            seen_non_phonetic = set()

        # Statistics tracking
        stats = {
            'docs_processed': 0,
            'clusters_found': 0,
            'items_written': 0,
            'phonetic_pairs_attempted': 0,
            'phonetic_pairs_written': 0,
            'phonetic_pairs_duplicates': 0,
            'phonetic_pairs_below_threshold': 0,
            'non_phonetic_pairs_attempted': 0,
            'non_phonetic_pairs_written': 0,
            'non_phonetic_pairs_duplicates': 0,
        }

        # Open HDF5 with Write mode
        with h5py.File(output_path, 'w') as f:
            # 1. Setup Dynamic Datasets
            str_dtype = h5py.special_dtype(vlen=str)

            grp_items = f.create_group('items')
            grp_feats = f.create_group('features')
            grp_pairs_p = f.create_group('pairs_with_phonetic')
            grp_pairs_np = f.create_group('pairs_without_phonetic')

            def make_dset(grp, name, dtype):
                return grp.create_dataset(name, (100000,), maxshape=(None,), dtype=dtype, chunks=True)

            dsets = {
                'toponym': make_dset(grp_items, 'toponym', str_dtype),
                'romanized': make_dset(grp_items, 'romanized', str_dtype),
                'lang': make_dset(grp_items, 'lang', str_dtype),
                'ipa': make_dset(grp_items, 'ipa', str_dtype),
                'cluster_id': make_dset(grp_items, 'cluster_id', 'i4'),
                'has_phonetic': make_dset(grp_items, 'has_phonetic', 'bool'),
                # Pairs
                'p_anc': make_dset(grp_pairs_p, 'anchor_idx', 'i4'),
                'p_pos': make_dset(grp_pairs_p, 'positive_idx', 'i4'),
                'p_sim': make_dset(grp_pairs_p, 'similarity', 'f4'),
                'np_anc': make_dset(grp_pairs_np, 'anchor_idx', 'i4'),
                'np_pos': make_dset(grp_pairs_np, 'positive_idx', 'i4'),
            }

            # State tracking
            counters = {'items': 0, 'phon_pairs': 0, 'non_phon_pairs': 0}
            caps = {'items': 100000, 'phon_pairs': 100000, 'non_phon_pairs': 100000}

            # 2. Scroll and Batch
            cluster_batch = []

            query = {"query": {"match_all": {}}, "_source": ["toponyms"]}

            # Use helpers.scan for safer iteration
            scanner = helpers.scan(self.es, index=self.index, query=query, scroll='5m', size=1000)

            for i, hit in enumerate(scanner):
                if max_docs and i >= max_docs:
                    break

                stats['docs_processed'] = i + 1

                # Extract Cluster
                raw_toponyms = hit['_source'].get('toponyms', [])
                cluster = []
                seen = set()
                for t in raw_toponyms:
                    if isinstance(t, dict):
                        tid = t.get('toponym_id', '')
                        if '@' in tid:
                            name, lang = tid.rsplit('@', 1)
                            name, lang = name.strip(), lang.strip()
                            if name and lang:
                                k = (name.lower(), lang.lower())
                                if k not in seen:
                                    seen.add(k)
                                    cluster.append((name, lang))

                if len(cluster) >= 2:
                    stats['clusters_found'] += 1
                    cluster_batch.append((i, cluster))

                # Process Batch every 1000 clusters
                if len(cluster_batch) >= 1000:
                    self._process_batch_deduplicated(
                        cluster_batch, dsets, grp_feats, counters, caps,
                        seen_phonetic, seen_non_phonetic, stats
                    )
                    cluster_batch = []

                # Progress report every 10k docs
                if stats['docs_processed'] % 10000 == 0:
                    self._print_progress(stats)

            # Process remaining
            if cluster_batch:
                self._process_batch_deduplicated(
                    cluster_batch, dsets, grp_feats, counters, caps,
                    seen_phonetic, seen_non_phonetic, stats
                )

            # 3. Final Trim
            print("\nTrimming datasets to final size...")
            for k in ['toponym', 'romanized', 'lang', 'ipa', 'cluster_id', 'has_phonetic']:
                dsets[k].resize((counters['items'],))

            dsets['p_anc'].resize((counters['phon_pairs'],))
            dsets['p_pos'].resize((counters['phon_pairs'],))
            dsets['p_sim'].resize((counters['phon_pairs'],))

            dsets['np_anc'].resize((counters['non_phon_pairs'],))
            dsets['np_pos'].resize((counters['non_phon_pairs'],))

            # Metadata
            f.attrs['total_items'] = counters['items']
            f.attrs['pairs_with_phonetic'] = counters['phon_pairs']
            f.attrs['pairs_without_phonetic'] = counters['non_phon_pairs']
            f.attrs['similarity_threshold'] = 0.5  # Config.SIMILARITY_THRESHOLD

        # Final statistics
        print("\n" + "=" * 70)
        print("EXTRACTION COMPLETE WITH DEDUPLICATION")
        print("=" * 70)
        self._print_final_stats(stats, counters)

    def _process_batch_deduplicated(self, batch, dsets, grp_feats, counters, caps,
                                    seen_phonetic, seen_non_phonetic, stats):
        """
        Process a batch of clusters with global pair deduplication.
        """
        # A. Collect IDs for MGET
        ids_to_fetch = set()
        for _, cluster in batch:
            for name, lang in cluster:
                ids_to_fetch.add(f"{name}@{lang}")

        # B. MGET Cache
        cache = {}
        if ids_to_fetch:
            try:
                resp = self.es.mget(
                    index='toponyms',
                    body={'ids': list(ids_to_fetch)},
                    _source=['ipa_cached', 'features_cached_json']
                )
                for doc in resp['docs']:
                    if doc['found'] and 'ipa_cached' in doc['_source']:
                        cache[doc['_id']] = {
                            'ipa': doc['_source']['ipa_cached'],
                            'feats': orjson.loads(doc['_source']['features_cached_json'])
                        }
            except Exception as e:
                print(f"\nWarning: MGET failed: {e}")

        # C. Write with Deduplication
        CHUNK_EXPAND = 50000

        for cluster_idx, cluster in batch:
            cluster_items = []

            # Process Items
            for name, lang in cluster:
                # Resize Check (Items)
                if counters['items'] >= caps['items']:
                    new_cap = caps['items'] + CHUNK_EXPAND
                    for k in ['toponym', 'romanized', 'lang', 'ipa', 'cluster_id', 'has_phonetic']:
                        dsets[k].resize((new_cap,))
                    caps['items'] = new_cap

                # Check Cache
                doc_id = f"{name}@{lang}"
                cached = cache.get(doc_id)

                # Prepare Data
                c_idx = counters['items']
                dsets['toponym'][c_idx] = name
                dsets['romanized'][c_idx] = anyascii(name).lower()
                dsets['lang'][c_idx] = lang
                dsets['cluster_id'][c_idx] = cluster_idx

                has_phonetic = False
                ipa_str = ""

                if cached:
                    has_phonetic = True
                    ipa_str = cached['ipa']
                    # Write Feature Vector
                    grp_feats.create_dataset(str(c_idx), data=np.array(cached['feats'], dtype='f4'))

                dsets['ipa'][c_idx] = ipa_str
                dsets['has_phonetic'][c_idx] = has_phonetic

                cluster_items.append({
                    'idx': c_idx,
                    'toponym': name,
                    'lang': lang,
                    'ipa': ipa_str,
                    'has_p': has_phonetic
                })
                counters['items'] += 1
                stats['items_written'] += 1

            # Process Pairs with Global Deduplication
            for i, item_a in enumerate(cluster_items):
                for item_b in cluster_items[i + 1:]:
                    # Create canonical pair key
                    pair_key = self._make_pair_key(
                        item_a['toponym'], item_a['lang'],
                        item_b['toponym'], item_b['lang']
                    )

                    if item_a['has_p'] and item_b['has_p']:
                        # Phonetic Pair
                        stats['phonetic_pairs_attempted'] += 1

                        # Check for duplicate
                        if pair_key in seen_phonetic:
                            stats['phonetic_pairs_duplicates'] += 1
                            continue

                        # Check similarity threshold
                        sim = self.phonetic_similarity(item_a['ipa'], item_b['ipa'])

                        if sim < 0.5:  # Config.SIMILARITY_THRESHOLD
                            stats['phonetic_pairs_below_threshold'] += 1
                            continue

                        # Mark as seen
                        seen_phonetic.add(pair_key)

                        # Resize Check
                        if counters['phon_pairs'] >= caps['phon_pairs']:
                            new_cap = caps['phon_pairs'] + CHUNK_EXPAND
                            dsets['p_anc'].resize((new_cap,))
                            dsets['p_pos'].resize((new_cap,))
                            dsets['p_sim'].resize((new_cap,))
                            caps['phon_pairs'] = new_cap

                        # Write
                        pidx = counters['phon_pairs']
                        dsets['p_anc'][pidx] = item_a['idx']
                        dsets['p_pos'][pidx] = item_b['idx']
                        dsets['p_sim'][pidx] = sim
                        counters['phon_pairs'] += 1
                        stats['phonetic_pairs_written'] += 1

                    else:
                        # Non-Phonetic Pair
                        stats['non_phonetic_pairs_attempted'] += 1

                        # Check for duplicate
                        if pair_key in seen_non_phonetic:
                            stats['non_phonetic_pairs_duplicates'] += 1
                            continue

                        # Mark as seen
                        seen_non_phonetic.add(pair_key)

                        # Resize Check
                        if counters['non_phon_pairs'] >= caps['non_phon_pairs']:
                            new_cap = caps['non_phon_pairs'] + CHUNK_EXPAND
                            dsets['np_anc'].resize((new_cap,))
                            dsets['np_pos'].resize((new_cap,))
                            caps['non_phon_pairs'] = new_cap

                        # Write
                        pidx = counters['non_phon_pairs']
                        dsets['np_anc'][pidx] = item_a['idx']
                        dsets['np_pos'][pidx] = item_b['idx']
                        counters['non_phon_pairs'] += 1
                        stats['non_phonetic_pairs_written'] += 1

    def _print_progress(self, stats):
        """Print progress update during extraction."""
        print(f"\rDocs: {stats['docs_processed']:,} | "
              f"Clusters: {stats['clusters_found']:,} | "
              f"Items: {stats['items_written']:,} | "
              f"Phon pairs: {stats['phonetic_pairs_written']:,} "
              f"(dupes: {stats['phonetic_pairs_duplicates']:,}) | "
              f"Non-phon: {stats['non_phonetic_pairs_written']:,} "
              f"(dupes: {stats['non_phonetic_pairs_duplicates']:,})",
              end='', flush=True)

    def _print_final_stats(self, stats, counters):
        """Print final extraction statistics."""
        print(f"\nDocuments processed: {stats['docs_processed']:,}")
        print(f"Clusters found: {stats['clusters_found']:,}")
        print(f"Items written: {stats['items_written']:,}")
        print()

        # Phonetic pairs
        phon_total = stats['phonetic_pairs_attempted']
        phon_written = stats['phonetic_pairs_written']
        phon_dupes = stats['phonetic_pairs_duplicates']
        phon_below = stats['phonetic_pairs_below_threshold']

        if phon_total > 0:
            print(f"Phonetic pairs:")
            print(f"  Attempted:        {phon_total:,}")
            print(f"  Written:          {phon_written:,} ({100 * phon_written / phon_total:.1f}%)")
            print(f"  Duplicates:       {phon_dupes:,} ({100 * phon_dupes / phon_total:.1f}%)")
            print(f"  Below threshold:  {phon_below:,} ({100 * phon_below / phon_total:.1f}%)")
        else:
            print(f"Phonetic pairs: 0 (no IPA data available)")

        print()

        # Non-phonetic pairs
        non_phon_total = stats['non_phonetic_pairs_attempted']
        non_phon_written = stats['non_phonetic_pairs_written']
        non_phon_dupes = stats['non_phonetic_pairs_duplicates']

        if non_phon_total > 0:
            print(f"Non-phonetic pairs:")
            print(f"  Attempted:        {non_phon_total:,}")
            print(f"  Written:          {non_phon_written:,} ({100 * non_phon_written / non_phon_total:.1f}%)")
            print(f"  Duplicates:       {non_phon_dupes:,} ({100 * non_phon_dupes / non_phon_total:.1f}%)")

        print()
        print(f"FINAL COUNTS:")
        print(f"  Items:            {counters['items']:,}")
        print(f"  Phonetic pairs:   {counters['phon_pairs']:,}")
        print(f"  Non-phonetic:     {counters['non_phon_pairs']:,}")
        print(f"  Total pairs:      {counters['phon_pairs'] + counters['non_phon_pairs']:,}")

        # Deduplication effectiveness
        total_attempted = phon_total + non_phon_total
        total_written = phon_written + non_phon_written
        total_dupes = phon_dupes + non_phon_dupes

        if total_attempted > 0:
            print()
            print(f"Deduplication effectiveness:")
            print(f"  Original pairs:   {total_attempted:,}")
            print(f"  Unique pairs:     {total_written:,}")
            print(f"  Duplicates:       {total_dupes:,} ({100 * total_dupes / total_attempted:.1f}%)")
            print(f"  Reduction:        {100 * (total_attempted - total_written) / total_attempted:.1f}%")


# =============================================================================
# Neural Network Models
# =============================================================================

class PhoneticEncoder(nn.Module):
    """
    Teacher Model: Pure phonetic (IPA) encoder.
    Takes PanPhon feature vectors and produces embeddings.
    """

    def __init__(
        self,
        phonetic_feat_dim: int = Config.PHONETIC_FEAT_DIM,
        hidden_dim: int = Config.HIDDEN_DIM,
        embed_dim: int = Config.EMBED_DIM,
        num_layers: int = Config.NUM_LAYERS,
        dropout: float = Config.DROPOUT
    ):
        super().__init__()

        self.bilstm = nn.LSTM(
            input_size=phonetic_feat_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout if num_layers > 1 else 0
        )

        self.projection = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, embed_dim)
        )

    def forward(self, phonetic_seq: torch.Tensor, seq_lengths: torch.Tensor) -> torch.Tensor:
        """
        Args:
            phonetic_seq: (batch, max_seq_len, phonetic_feat_dim)
            seq_lengths: (batch,)

        Returns:
            (batch, embed_dim) normalized embeddings
        """
        packed = nn.utils.rnn.pack_padded_sequence(
            phonetic_seq, seq_lengths.cpu(),
            batch_first=True, enforce_sorted=False
        )
        _, (hidden, _) = self.bilstm(packed)

        # Concatenate final hidden states from both directions
        combined = torch.cat([hidden[-2], hidden[-1]], dim=-1)

        embedding = self.projection(combined)
        return F.normalize(embedding, p=2, dim=-1)


class CharEncoder(nn.Module):
    """
    Student Model: Language-Conditioned Character Encoder.
    Learns to approximate phonetic space from (Romanized Text + Language ID).

    The language embedding is concatenated at every timestep to condition
    the LSTM on the source language, resolving grapheme-phoneme ambiguities
    like 'j' → /x/ (Spanish), /dʒ/ (English), /ʒ/ (French).
    """

    def __init__(
        self,
        vocab_size: int = Config.VOCAB_SIZE,
        num_langs: int = Config.NUM_LANGS,
        char_embed_dim: int = Config.CHAR_EMBED_DIM,
        lang_embed_dim: int = Config.LANG_EMBED_DIM,
        hidden_dim: int = Config.HIDDEN_DIM,
        embed_dim: int = Config.EMBED_DIM,
        num_layers: int = Config.NUM_LAYERS,
        dropout: float = Config.DROPOUT
    ):
        super().__init__()

        self.char_embed = nn.Embedding(vocab_size, char_embed_dim, padding_idx=0)
        self.lang_embed = nn.Embedding(num_langs, lang_embed_dim)

        # Input is char embedding + language embedding at each timestep
        self.bilstm = nn.LSTM(
            input_size=char_embed_dim + lang_embed_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout if num_layers > 1 else 0
        )

        self.projection = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, embed_dim)
        )

    def forward(
        self,
        char_ids: torch.Tensor,
        lang_ids: torch.Tensor,
        seq_lengths: torch.Tensor
    ) -> torch.Tensor:
        """
        Args:
            char_ids: (batch, max_seq_len) - romanized character IDs
            lang_ids: (batch,) - language IDs
            seq_lengths: (batch,) - actual sequence lengths

        Returns:
            (batch, embed_dim) normalized embeddings
        """
        batch_size, max_len = char_ids.shape

        # Embed characters: (batch, seq, char_embed_dim)
        c_emb = self.char_embed(char_ids)

        # Embed language and broadcast: (batch, seq, lang_embed_dim)
        l_emb = self.lang_embed(lang_ids)
        l_emb = l_emb.unsqueeze(1).expand(-1, max_len, -1)

        # Concatenate: (batch, seq, char_embed_dim + lang_embed_dim)
        combined_input = torch.cat([c_emb, l_emb], dim=-1)

        # BiLSTM
        packed = nn.utils.rnn.pack_padded_sequence(
            combined_input, seq_lengths.cpu(),
            batch_first=True, enforce_sorted=False
        )
        _, (hidden, _) = self.bilstm(packed)

        # Concatenate final hidden states from both directions
        combined_hidden = torch.cat([hidden[-2], hidden[-1]], dim=-1)

        # Project and normalize
        embedding = self.projection(combined_hidden)
        return F.normalize(embedding, p=2, dim=-1)


class HybridPhoneticModel(nn.Module):
    """
    Full hybrid model combining Teacher (phonetic) and Student (character) pathways.
    Uses gated fusion when both pathways are available.
    """

    def __init__(
        self,
        phonetic_encoder: PhoneticEncoder,
        char_encoder: CharEncoder,
        embed_dim: int = Config.EMBED_DIM
    ):
        super().__init__()

        self.phonetic_encoder = phonetic_encoder
        self.char_encoder = char_encoder

        # Learnable gate for blending pathways
        self.gate = nn.Sequential(
            nn.Linear(embed_dim * 2, embed_dim),
            nn.ReLU(),
            nn.Linear(embed_dim, 1),
            nn.Sigmoid()
        )

    def forward(
        self,
        char_ids: torch.Tensor,
        lang_ids: torch.Tensor,
        char_lengths: torch.Tensor,
        phonetic_seq: Optional[torch.Tensor] = None,
        phonetic_lengths: Optional[torch.Tensor] = None,
        has_phonetic: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """
        Forward pass with optional phonetic pathway.

        Args:
            char_ids: (batch, max_char_len) - romanized character IDs
            lang_ids: (batch,) - language IDs
            char_lengths: (batch,) - character sequence lengths
            phonetic_seq: (batch, max_phone_len, feat_dim) - IPA features, optional
            phonetic_lengths: (batch,) - phonetic sequence lengths, optional
            has_phonetic: (batch,) - boolean mask for items with phonetic features

        Returns:
            (batch, embed_dim) normalized embeddings
        """
        batch_size = char_ids.size(0)
        device = char_ids.device

        # Character pathway (always available)
        char_emb = self.char_encoder(char_ids, lang_ids, char_lengths)

        # Phonetic pathway (when available)
        if phonetic_seq is not None and has_phonetic is not None and has_phonetic.any():
            phone_emb = torch.zeros_like(char_emb)

            # Only process items with phonetic features
            mask = has_phonetic
            if mask.any():
                phone_subset = self.phonetic_encoder(
                    phonetic_seq[mask],
                    phonetic_lengths[mask]
                )
                phone_emb[mask] = phone_subset

            # Gated fusion
            combined = torch.cat([char_emb, phone_emb], dim=-1)
            gate_value = self.gate(combined)

            # Apply gate only where we have phonetic
            gate_value = gate_value * has_phonetic.float().unsqueeze(-1)
            fused = gate_value * phone_emb + (1 - gate_value) * char_emb

            return F.normalize(fused, p=2, dim=-1)
        else:
            return char_emb

    def encode_phonetic_only(
        self,
        phonetic_seq: torch.Tensor,
        phonetic_lengths: torch.Tensor
    ) -> torch.Tensor:
        """Direct phonetic encoding (for Phase 1 training)."""
        return self.phonetic_encoder(phonetic_seq, phonetic_lengths)

    def encode_char_only(
        self,
        char_ids: torch.Tensor,
        lang_ids: torch.Tensor,
        char_lengths: torch.Tensor
    ) -> torch.Tensor:
        """Direct character encoding (for Phase 3 training)."""
        return self.char_encoder(char_ids, lang_ids, char_lengths)


# =============================================================================
# Loss Functions
# =============================================================================

class TripletLoss(nn.Module):
    """Standard triplet loss with margin."""

    def __init__(self, margin: float = Config.TRIPLET_MARGIN):
        super().__init__()
        self.margin = margin

    def forward(
        self,
        anchor: torch.Tensor,
        positive: torch.Tensor,
        negative: torch.Tensor
    ) -> torch.Tensor:
        """
        Args:
            anchor: (batch, embed_dim)
            positive: (batch, embed_dim)
            negative: (batch, embed_dim)

        Returns:
            Scalar loss
        """
        pos_dist = (anchor - positive).pow(2).sum(dim=-1)
        neg_dist = (anchor - negative).pow(2).sum(dim=-1)
        loss = F.relu(pos_dist - neg_dist + self.margin)
        return loss.mean()


class RobustAlignmentLoss(nn.Module):
    """
    Combined MSE and Cosine loss for Student-Teacher alignment.
    MSE fixes position, Cosine fixes orientation in embedding space.
    """

    def __init__(self, cosine_weight: float = Config.ALIGNMENT_COSINE_WEIGHT):
        super().__init__()
        self.cosine_weight = cosine_weight

    def forward(self, char_emb: torch.Tensor, phone_emb: torch.Tensor) -> torch.Tensor:
        """
        Args:
            char_emb: (batch, embed_dim) - Student output
            phone_emb: (batch, embed_dim) - Teacher output (target)

        Returns:
            Scalar loss
        """
        # MSE loss (Euclidean distance proxy)
        mse = F.mse_loss(char_emb, phone_emb)

        # Cosine distance (orientation proxy)
        cosine_dist = 1.0 - F.cosine_similarity(char_emb, phone_emb).mean()

        return mse + (self.cosine_weight * cosine_dist)


# =============================================================================
# Collate Functions
# =============================================================================

def collate_phase1(batch: List[Dict]) -> Dict[str, Tuple[torch.Tensor, torch.Tensor]]:
    """Collate function for Phase 1 (phonetic features only)."""

    def pad_features(features_list):
        lengths = torch.tensor([len(f) for f in features_list])
        max_len = max(lengths)
        feat_dim = features_list[0].shape[1]
        padded = torch.zeros(len(features_list), max_len, feat_dim)
        for i, f in enumerate(features_list):
            padded[i, :len(f)] = f
        return padded, lengths

    anchor, anchor_len = pad_features([b['anchor_features'] for b in batch])
    positive, pos_len = pad_features([b['positive_features'] for b in batch])
    negative, neg_len = pad_features([b['negative_features'] for b in batch])

    return {
        'anchor': (anchor, anchor_len),
        'positive': (positive, pos_len),
        'negative': (negative, neg_len)
    }


def collate_phase2(batch: List[Dict]) -> Dict[str, torch.Tensor]:
    """Collate function for Phase 2 (alignment training)."""

    def pad_ids(ids_list):
        lengths = torch.tensor([len(ids) for ids in ids_list])
        max_len = max(lengths)
        padded = torch.zeros(len(ids_list), max_len, dtype=torch.long)
        for i, ids in enumerate(ids_list):
            padded[i, :len(ids)] = ids
        return padded, lengths

    def pad_features(features_list):
        lengths = torch.tensor([len(f) for f in features_list])
        max_len = max(lengths)
        feat_dim = features_list[0].shape[1]
        padded = torch.zeros(len(features_list), max_len, feat_dim)
        for i, f in enumerate(features_list):
            padded[i, :len(f)] = f
        return padded, lengths

    char_ids, char_lengths = pad_ids([b['char_ids'] for b in batch])
    lang_ids = torch.stack([b['lang_id'] for b in batch])
    phone_feats, phone_lengths = pad_features([b['phonetic_features'] for b in batch])

    return {
        'char_ids': char_ids,
        'char_lengths': char_lengths,
        'lang_ids': lang_ids,
        'phonetic_features': phone_feats,
        'phonetic_lengths': phone_lengths
    }


def collate_phase3(batch: List[Dict]) -> Dict[str, Tuple[torch.Tensor, ...]]:
    """Collate function for Phase 3 (character triplets)."""

    def pad_ids(ids_list):
        lengths = torch.tensor([len(ids) for ids in ids_list])
        max_len = max(lengths)
        padded = torch.zeros(len(ids_list), max_len, dtype=torch.long)
        for i, ids in enumerate(ids_list):
            padded[i, :len(ids)] = ids
        return padded, lengths

    anchor_ids, anchor_lens = pad_ids([b['anchor_char_ids'] for b in batch])
    anchor_langs = torch.stack([b['anchor_lang_id'] for b in batch])

    pos_ids, pos_lens = pad_ids([b['positive_char_ids'] for b in batch])
    pos_langs = torch.stack([b['positive_lang_id'] for b in batch])

    neg_ids, neg_lens = pad_ids([b['negative_char_ids'] for b in batch])
    neg_langs = torch.stack([b['negative_lang_id'] for b in batch])

    return {
        'anchor': (anchor_ids, anchor_langs, anchor_lens),
        'positive': (pos_ids, pos_langs, pos_lens),
        'negative': (neg_ids, neg_langs, neg_lens)
    }


# =============================================================================
# Training Functions
# =============================================================================

def train_phase1(
        data_path: str,
        output_path: str,
        epochs: int = Config.PHASE1_EPOCHS,
        subsample_pairs: int = Config.SUBSAMPLE_PAIRS,
        batch_size: int = Config.BATCH_SIZE,
        lr: float = Config.LEARNING_RATE
):
    """Phase 1: Train phonetic encoder (streaming from HDF5)."""

    print("=" * 60)
    print("Phase 1: Training Phonetic Encoder (Teacher)")
    print("=" * 60)

    train_dataset = StreamingPhase1Dataset(data_path, split='train', subsample_pairs=subsample_pairs)
    val_dataset = StreamingPhase1Dataset(data_path, split='val', subsample_pairs=subsample_pairs)

    print(f"Training pairs: {len(train_dataset)}")
    print(f"Validation pairs: {len(val_dataset)}")

    train_loader = DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True,
        collate_fn=collate_phase1, num_workers=4, pin_memory=True
    )
    val_loader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False,
        collate_fn=collate_phase1, num_workers=4, pin_memory=True
    )

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Device: {device}")

    model = PhoneticEncoder().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', patience=5, factor=0.5, verbose=True
    )
    criterion = TripletLoss()

    best_val_loss = float('inf')

    for epoch in range(epochs):
        # Training
        model.train()
        train_loss = 0

        for batch in train_loader:
            anchor_seq, anchor_len = batch['anchor']
            pos_seq, pos_len = batch['positive']
            neg_seq, neg_len = batch['negative']

            anchor_seq = anchor_seq.to(device)
            pos_seq = pos_seq.to(device)
            neg_seq = neg_seq.to(device)

            optimizer.zero_grad()

            anchor_emb = model(anchor_seq, anchor_len)
            pos_emb = model(pos_seq, pos_len)
            neg_emb = model(neg_seq, neg_len)

            loss = criterion(anchor_emb, pos_emb, neg_emb)
            loss.backward()

            # Gradient clipping
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)

            optimizer.step()
            train_loss += loss.item()

        # Validation
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for batch in val_loader:
                anchor_seq, anchor_len = batch['anchor']
                pos_seq, pos_len = batch['positive']
                neg_seq, neg_len = batch['negative']

                anchor_emb = model(anchor_seq.to(device), anchor_len)
                pos_emb = model(pos_seq.to(device), pos_len)
                neg_emb = model(neg_seq.to(device), neg_len)

                loss = criterion(anchor_emb, pos_emb, neg_emb)
                val_loss += loss.item()

        train_loss /= len(train_loader)
        val_loss /= len(val_loader)
        scheduler.step(val_loss)

        print(f"Epoch {epoch+1:3d}/{epochs} | Train: {train_loss:.4f} | Val: {val_loss:.4f}")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save({
                'model_state': model.state_dict(),
                'epoch': epoch,
                'val_loss': val_loss
            }, output_path)
            print(f"  → Saved best model (val_loss: {val_loss:.4f})")

    print(f"\nPhase 1 complete. Best model saved to {output_path}")
    return model


def train_phase2(
        data_path: str,
        phase1_path: str,
        output_path: str,
        epochs: int = Config.PHASE2_EPOCHS,
        batch_size: int = Config.BATCH_SIZE,
        lr: float = Config.LEARNING_RATE
):
    """Phase 2: Alignment training (streaming from HDF5)."""

    print("=" * 60)
    print("Phase 2: Alignment Training (Student → Teacher)")
    print("=" * 60)

    # Build vocabularies from HDF5
    char_vocab = CharVocab(vocab_size=Config.VOCAB_SIZE)
    char_vocab.fit(data_path)

    lang_vocab = LangVocab()
    lang_vocab.fit(data_path)

    train_dataset = StreamingPhase2Dataset(data_path, char_vocab, lang_vocab, split='train')
    val_dataset = StreamingPhase2Dataset(data_path, char_vocab, lang_vocab, split='val')

    print(f"Training items: {len(train_dataset)}")
    print(f"Validation items: {len(val_dataset)}")

    train_loader = DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True,
        collate_fn=collate_phase2, num_workers=4, pin_memory=True
    )
    val_loader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False,
        collate_fn=collate_phase2, num_workers=4, pin_memory=True
    )

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Device: {device}")

    # Load pre-trained phonetic encoder (Teacher)
    phonetic_encoder = PhoneticEncoder().to(device)
    checkpoint = torch.load(phase1_path, map_location=device)
    phonetic_encoder.load_state_dict(checkpoint['model_state'])
    phonetic_encoder.eval()

    # Freeze Teacher
    for param in phonetic_encoder.parameters():
        param.requires_grad = False
    print("Teacher (phonetic encoder) frozen")

    # Create Student (character encoder)
    char_encoder = CharEncoder(
        vocab_size=char_vocab.vocab_size,
        num_langs=lang_vocab.next_id
    ).to(device)

    optimizer = torch.optim.Adam(char_encoder.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', patience=5, factor=0.5, verbose=True
    )
    criterion = RobustAlignmentLoss()

    best_val_loss = float('inf')

    for epoch in range(epochs):
        # Training
        char_encoder.train()
        train_loss = 0

        for batch in train_loader:
            char_ids = batch['char_ids'].to(device)
            char_lengths = batch['char_lengths']
            lang_ids = batch['lang_ids'].to(device)
            phone_feats = batch['phonetic_features'].to(device)
            phone_lengths = batch['phonetic_lengths']

            optimizer.zero_grad()

            # Get target from frozen Teacher
            with torch.no_grad():
                target_emb = phonetic_encoder(phone_feats, phone_lengths)

            # Train Student to match
            char_emb = char_encoder(char_ids, lang_ids, char_lengths)

            loss = criterion(char_emb, target_emb)
            loss.backward()

            torch.nn.utils.clip_grad_norm_(char_encoder.parameters(), max_norm=1.0)

            optimizer.step()
            train_loss += loss.item()

        # Validation
        char_encoder.eval()
        val_loss = 0
        with torch.no_grad():
            for batch in val_loader:
                char_ids = batch['char_ids'].to(device)
                char_lengths = batch['char_lengths']
                lang_ids = batch['lang_ids'].to(device)
                phone_feats = batch['phonetic_features'].to(device)
                phone_lengths = batch['phonetic_lengths']

                target_emb = phonetic_encoder(phone_feats, phone_lengths)
                char_emb = char_encoder(char_ids, lang_ids, char_lengths)

                loss = criterion(char_emb, target_emb)
                val_loss += loss.item()

        train_loss /= len(train_loader)
        val_loss /= len(val_loader)
        scheduler.step(val_loss)

        print(f"Epoch {epoch+1:3d}/{epochs} | Train: {train_loss:.4f} | Val: {val_loss:.4f}")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save({
                'phonetic_state': phonetic_encoder.state_dict(),
                'char_state': char_encoder.state_dict(),
                'epoch': epoch,
                'val_loss': val_loss
            }, output_path)

            # Save vocabularies
            vocab_dir = os.path.dirname(output_path) or '.'
            base_name = os.path.splitext(os.path.basename(output_path))[0]
            char_vocab.save(os.path.join(vocab_dir, f'{base_name}_char_vocab.pkl'))
            lang_vocab.save(os.path.join(vocab_dir, f'{base_name}_lang_vocab.pkl'))

            print(f"  → Saved best model (val_loss: {val_loss:.4f})")

    print(f"\nPhase 2 complete. Best model saved to {output_path}")
    return phonetic_encoder, char_encoder, char_vocab, lang_vocab


def train_phase3(
        data_path: str,
        phase2_path: str,
        output_path: str,
        subsample_pairs: int = Config.SUBSAMPLE_PAIRS,
        epochs: int = Config.PHASE3_EPOCHS,
        batch_size: int = Config.BATCH_SIZE,
        lr: float = 5e-4
):
    """Phase 3: Fine-tune on all data (streaming from HDF5)."""

    print("=" * 60)
    print("Phase 3: Generalization Training (Hard Negatives)")
    print("=" * 60)

    # Load vocabularies
    vocab_dir = os.path.dirname(phase2_path) or '.'
    base_name = os.path.splitext(os.path.basename(phase2_path))[0]
    char_vocab = CharVocab.load(os.path.join(vocab_dir, f'{base_name}_char_vocab.pkl'))
    lang_vocab = LangVocab.load(os.path.join(vocab_dir, f'{base_name}_lang_vocab.pkl'))

    train_dataset = StreamingPhase3Dataset(data_path, char_vocab, lang_vocab, split='train', subsample_pairs=subsample_pairs)
    val_dataset = StreamingPhase3Dataset(data_path, char_vocab, lang_vocab, split='val', subsample_pairs=subsample_pairs)

    print(f"Training pairs: {len(train_dataset)}")
    print(f"Validation pairs: {len(val_dataset)}")

    train_loader = DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True,
        collate_fn=collate_phase3, num_workers=4, pin_memory=True
    )
    val_loader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False,
        collate_fn=collate_phase3, num_workers=4, pin_memory=True
    )

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Device: {device}")

    # Load Phase 2 models
    checkpoint = torch.load(phase2_path, map_location=device)

    phonetic_encoder = PhoneticEncoder().to(device)
    phonetic_encoder.load_state_dict(checkpoint['phonetic_state'])

    char_encoder = CharEncoder(
        vocab_size=char_vocab.vocab_size,
        num_langs=lang_vocab.next_id
    ).to(device)
    char_encoder.load_state_dict(checkpoint['char_state'])

    # Create hybrid model
    model = HybridPhoneticModel(phonetic_encoder, char_encoder).to(device)

    # CRITICAL: Freeze phonetic encoder
    for param in model.phonetic_encoder.parameters():
        param.requires_grad = False
    print("Phonetic encoder frozen")

    # CRITICAL: Freeze gate (learned in Phase 2, don't corrupt it)
    for param in model.gate.parameters():
        param.requires_grad = False
    print("Gate frozen")

    # Only train character encoder
    optimizer = torch.optim.Adam(
        model.char_encoder.parameters(),
        lr=lr
    )
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', patience=3, factor=0.5, verbose=True
    )
    criterion = TripletLoss()

    best_val_loss = float('inf')

    for epoch in range(epochs):
        # Training
        model.train()
        train_loss = 0

        for batch in train_loader:
            anchor_ids, anchor_langs, anchor_lens = batch['anchor']
            pos_ids, pos_langs, pos_lens = batch['positive']
            neg_ids, neg_langs, neg_lens = batch['negative']

            anchor_ids = anchor_ids.to(device)
            anchor_langs = anchor_langs.to(device)
            pos_ids = pos_ids.to(device)
            pos_langs = pos_langs.to(device)
            neg_ids = neg_ids.to(device)
            neg_langs = neg_langs.to(device)

            optimizer.zero_grad()

            # Use character-only encoding in Phase 3
            anchor_emb = model.encode_char_only(anchor_ids, anchor_langs, anchor_lens)
            pos_emb = model.encode_char_only(pos_ids, pos_langs, pos_lens)
            neg_emb = model.encode_char_only(neg_ids, neg_langs, neg_lens)

            loss = criterion(anchor_emb, pos_emb, neg_emb)
            loss.backward()

            torch.nn.utils.clip_grad_norm_(model.char_encoder.parameters(), max_norm=1.0)

            optimizer.step()
            train_loss += loss.item()

        # Validation
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for batch in val_loader:
                anchor_ids, anchor_langs, anchor_lens = batch['anchor']
                pos_ids, pos_langs, pos_lens = batch['positive']
                neg_ids, neg_langs, neg_lens = batch['negative']

                anchor_emb = model.encode_char_only(
                    anchor_ids.to(device), anchor_langs.to(device), anchor_lens
                )
                pos_emb = model.encode_char_only(
                    pos_ids.to(device), pos_langs.to(device), pos_lens
                )
                neg_emb = model.encode_char_only(
                    neg_ids.to(device), neg_langs.to(device), neg_lens
                )

                loss = criterion(anchor_emb, pos_emb, neg_emb)
                val_loss += loss.item()

        train_loss /= len(train_loader)
        val_loss /= len(val_loader)
        scheduler.step(val_loss)

        print(f"Epoch {epoch+1:3d}/{epochs} | Train: {train_loss:.4f} | Val: {val_loss:.4f}")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save({
                'model_state': model.state_dict(),
                'char_vocab_size': char_vocab.vocab_size,
                'num_langs': lang_vocab.next_id,
                'epoch': epoch,
                'val_loss': val_loss
            }, output_path)

            # Copy vocabularies to final output location
            final_vocab_dir = os.path.dirname(output_path) or '.'
            final_base_name = os.path.splitext(os.path.basename(output_path))[0]
            char_vocab.save(os.path.join(final_vocab_dir, f'{final_base_name}_char_vocab.pkl'))
            lang_vocab.save(os.path.join(final_vocab_dir, f'{final_base_name}_lang_vocab.pkl'))

            print(f"  → Saved best model (val_loss: {val_loss:.4f})")

    print(f"\nPhase 3 complete. Final model saved to {output_path}")
    return model


# =============================================================================
# Inference
# =============================================================================

class PhoneticSimilarityModel:
    """
    Production inference wrapper.

    Handles both Epitran-supported and unsupported languages transparently.
    Uses phonetic pathway when available, character pathway as fallback,
    and gated fusion when both are present.

    Example:
        model = PhoneticSimilarityModel('final_model.pt')

        # Get similarity score
        sim = model.similarity('London', 'en', 'Londres', 'fr')
        print(f"Similarity: {sim:.3f}")

        # Get embedding
        emb = model.embed('東京', 'ja')

        # Batch embedding
        embeddings = model.batch_embed([
            ('London', 'en'),
            ('Londres', 'fr'),
            ('Londra', 'it')
        ])
    """

    def __init__(self, model_path: str, device: str = 'cpu'):
        self.device = torch.device(device)

        # Load model checkpoint
        checkpoint = torch.load(model_path, map_location=self.device)

        # Load vocabularies
        model_dir = os.path.dirname(model_path) or '.'
        base_name = os.path.splitext(os.path.basename(model_path))[0]

        self.char_vocab = CharVocab.load(os.path.join(model_dir, f'{base_name}_char_vocab.pkl'))
        self.lang_vocab = LangVocab.load(os.path.join(model_dir, f'{base_name}_lang_vocab.pkl'))

        # Create model
        phonetic_encoder = PhoneticEncoder()
        char_encoder = CharEncoder(
            vocab_size=checkpoint.get('char_vocab_size', self.char_vocab.vocab_size),
            num_langs=checkpoint.get('num_langs', self.lang_vocab.next_id)
        )

        self.model = HybridPhoneticModel(phonetic_encoder, char_encoder)
        self.model.load_state_dict(checkpoint['model_state'])
        self.model.to(self.device)
        self.model.eval()

        # Epitran instances (lazy loading)
        self._epi_cache = {}
        self.ft = FeatureTable()

        print(f"Model loaded from {model_path}")
        print(f"  Char vocab: {len(self.char_vocab.char_to_id)} chars")
        print(f"  Lang vocab: {len(self.lang_vocab.lang_to_id)} languages")

    def _get_epitran(self, lang: str) -> Optional[epitran.Epitran]:
        """Get or create Epitran instance for a language."""
        if lang not in self._epi_cache:
            code = Config.EPITRAN_LANGS.get(lang)
            if code:
                try:
                    self._epi_cache[lang] = epitran.Epitran(code)
                except Exception:
                    self._epi_cache[lang] = None
            else:
                self._epi_cache[lang] = None
        return self._epi_cache[lang]

    def embed(self, toponym: str, lang: str) -> np.ndarray:
        """
        Get embedding for a toponym@lang pair.

        Uses phonetic pathway when Epitran supports the language,
        character pathway otherwise, gated fusion when both available.

        Args:
            toponym: The place name
            lang: ISO 639-1 language code

        Returns:
            64-dimensional normalized embedding as numpy array
        """
        # Romanize (always available)
        romanized = anyascii(toponym).lower().strip()
        char_ids = torch.tensor([self.char_vocab.encode(romanized)], dtype=torch.long)
        char_lengths = torch.tensor([char_ids.size(1)])
        lang_ids = torch.tensor([self.lang_vocab.encode(lang)], dtype=torch.long)

        # Try phonetic pathway
        phonetic_seq = None
        phonetic_lengths = None
        has_phonetic = torch.tensor([False])

        epi = self._get_epitran(lang)
        if epi:
            try:
                ipa = epi.transliterate(toponym)
                features = self.ft.word_to_vector_list(ipa, numeric=True)
                if features:
                    phonetic_seq = torch.tensor([features], dtype=torch.float32)
                    phonetic_lengths = torch.tensor([len(features)])
                    has_phonetic = torch.tensor([True])
            except Exception:
                pass

        with torch.no_grad():
            embedding = self.model(
                char_ids.to(self.device),
                lang_ids.to(self.device),
                char_lengths,
                phonetic_seq.to(self.device) if phonetic_seq is not None else None,
                phonetic_lengths,
                has_phonetic.to(self.device)
            )

        return embedding.cpu().numpy()[0]

    def similarity(
        self,
        toponym_a: str,
        lang_a: str,
        toponym_b: str,
        lang_b: str
    ) -> float:
        """
        Compute cosine similarity between two toponyms.

        Args:
            toponym_a: First place name
            lang_a: Language of first place name
            toponym_b: Second place name
            lang_b: Language of second place name

        Returns:
            Cosine similarity score in range [-1, 1]
        """
        emb_a = self.embed(toponym_a, lang_a)
        emb_b = self.embed(toponym_b, lang_b)
        return float(np.dot(emb_a, emb_b))

    def batch_embed(self, toponyms_and_langs: List[Tuple[str, str]]) -> np.ndarray:
        """
        Batch embedding for multiple toponyms.

        Args:
            toponyms_and_langs: List of (toponym, lang) tuples

        Returns:
            (N, 64) array of embeddings
        """
        embeddings = []
        for toponym, lang in toponyms_and_langs:
            embeddings.append(self.embed(toponym, lang))
        return np.array(embeddings)

    def find_similar(
        self,
        query_toponym: str,
        query_lang: str,
        candidates: List[Tuple[str, str]],
        top_k: int = 10
    ) -> List[Tuple[str, str, float]]:
        """
        Find most similar toponyms from candidates.

        Args:
            query_toponym: Query place name
            query_lang: Query language
            candidates: List of (toponym, lang) candidates
            top_k: Number of results to return

        Returns:
            List of (toponym, lang, similarity) tuples, sorted by similarity descending
        """
        query_emb = self.embed(query_toponym, query_lang)
        candidate_embs = self.batch_embed(candidates)

        similarities = candidate_embs @ query_emb

        indices = np.argsort(similarities)[::-1][:top_k]

        results = []
        for idx in indices:
            toponym, lang = candidates[idx]
            results.append((toponym, lang, float(similarities[idx])))

        return results


# =============================================================================
# Demo Data Generation (for testing without Elasticsearch)
# =============================================================================

def generate_demo_data(output_path: str, num_clusters: int = 1000):
    """Generate synthetic demo data for testing."""

    print("Generating demo data...")

    # Sample toponyms representing same places in different languages
    demo_clusters = [
        [('London', 'en'), ('Londres', 'fr'), ('Londres', 'es'), ('Londra', 'it'), ('Londen', 'nl')],
        [('Paris', 'en'), ('Paris', 'fr'), ('París', 'es'), ('Parigi', 'it'), ('Parijs', 'nl')],
        [('Berlin', 'en'), ('Berlin', 'de'), ('Berlín', 'es'), ('Berlino', 'it'), ('Berlijn', 'nl')],
        [('Rome', 'en'), ('Rom', 'de'), ('Roma', 'it'), ('Rome', 'fr'), ('Roma', 'es')],
        [('Vienna', 'en'), ('Wien', 'de'), ('Vienne', 'fr'), ('Viena', 'es'), ('Vienna', 'it')],
        [('Moscow', 'en'), ('Moskau', 'de'), ('Moscou', 'fr'), ('Moscú', 'es'), ('Mosca', 'it')],
        [('Warsaw', 'en'), ('Warschau', 'de'), ('Varsovie', 'fr'), ('Varsovia', 'es'), ('Varsavia', 'it')],
        [('Prague', 'en'), ('Prag', 'de'), ('Prague', 'fr'), ('Praga', 'es'), ('Praga', 'it')],
        [('Munich', 'en'), ('München', 'de'), ('Munich', 'fr'), ('Múnich', 'es'), ('Monaco', 'it')],
        [('Athens', 'en'), ('Athen', 'de'), ('Athènes', 'fr'), ('Atenas', 'es'), ('Atene', 'it')],
        [('Beijing', 'en'), ('Peking', 'de'), ('Pékin', 'fr'), ('Pekín', 'es'), ('Pechino', 'it')],
        [('Tokyo', 'en'), ('Tokio', 'de'), ('Tokyo', 'fr'), ('Tokio', 'es'), ('Tokyo', 'it')],
        [('Cairo', 'en'), ('Kairo', 'de'), ('Le Caire', 'fr'), ('El Cairo', 'es'), ('Il Cairo', 'it')],
        [('Lisbon', 'en'), ('Lissabon', 'de'), ('Lisbonne', 'fr'), ('Lisboa', 'es'), ('Lisbona', 'it')],
        [('Copenhagen', 'en'), ('Kopenhagen', 'de'), ('Copenhague', 'fr'), ('Copenhague', 'es'), ('Copenaghen', 'it')],
    ]

    # Expand to requested size by generating variations
    while len(demo_clusters) < num_clusters:
        base = random.choice(demo_clusters[:15])
        # Create variation by adding suffix
        suffix = random.choice(['burg', 'stadt', 'ville', 'ton', 'field', 'worth'])
        new_cluster = []
        for toponym, lang in base:
            new_name = toponym + suffix if random.random() > 0.3 else toponym
            new_cluster.append((new_name, lang))
        demo_clusters.append(new_cluster)

    extractor = TrainingDataExtractor.__new__(TrainingDataExtractor)
    extractor.ft = FeatureTable()
    extractor.dst = panphon.distance.Distance()
    extractor._epi_cache = {}

    pairs_p, pairs_np, items = extractor.build_training_data(demo_clusters[:num_clusters])
    extractor.save(pairs_p, pairs_np, items, output_path)

    print(f"Demo data saved to {output_path}")


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Phonetic Similarity Model (Streaming Version)',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('--enrich', action='store_true', help="Hydrate 'toponyms' index with phonetics")

    parser.add_argument('--phase', type=int, choices=[0, 1, 2, 3])
    parser.add_argument('--infer', action='store_true')

    # Data extraction (Phase 0)
    parser.add_argument('--es-host', default='localhost:9200')
    parser.add_argument('--index', default='places')
    parser.add_argument('--max-docs', type=int, default=None)

    # Training
    parser.add_argument('--data', default='training_data.h5',
                        help='Training data file (HDF5 for phases 1-3)')
    parser.add_argument('--output', default='model.pt',
                        help='Output file (HDF5 for phase 0, .pt for phases 1-3)')
    parser.add_argument('--phase1-model', default='phase1.pt')
    parser.add_argument('--phase2-model', default='phase2.pt')
    parser.add_argument('--epochs', type=int, default=None)
    parser.add_argument('--batch-size', type=int, default=Config.BATCH_SIZE)
    parser.add_argument('--subsample-pairs', type=int, default=Config.SUBSAMPLE_PAIRS)
    parser.add_argument('--lr', type=float, default=Config.LEARNING_RATE)

    # Inference
    parser.add_argument('--model', default='final_model.pt')
    parser.add_argument('--toponym1')
    parser.add_argument('--lang1')
    parser.add_argument('--toponym2')
    parser.add_argument('--lang2')
    parser.add_argument('--gpu', action='store_true')

    args = parser.parse_args()

    if args.enrich:
        enricher = ToponymEnricher(args.es_host, 'toponyms')  # Explicitly targeting toponyms index
        enricher.run()
        return

    if args.infer:
        # Inference code
        if not all([args.toponym1, args.lang1, args.toponym2, args.lang2]):
            parser.error("Inference requires --toponym1, --lang1, --toponym2, --lang2")

        device = 'cuda' if args.gpu and torch.cuda.is_available() else 'cpu'
        model = PhoneticSimilarityModel(args.model, device=device)

        sim = model.similarity(args.toponym1, args.lang1, args.toponym2, args.lang2)

        print(f"\n'{args.toponym1}' ({args.lang1}) vs '{args.toponym2}' ({args.lang2})")
        print(f"Similarity: {sim:.4f}")

        rom1 = anyascii(args.toponym1).lower()
        rom2 = anyascii(args.toponym2).lower()
        print(f"Romanized: '{rom1}' vs '{rom2}'")

    elif args.phase == 0:# Optimized Extraction
        extractor = TrainingDataExtractor(args.es_host, args.index)
        extractor.extract_and_stream(args.output, max_docs=args.max_docs)

    elif args.phase == 1:
        # Training Phase 1 - args.data is HDF5, args.output is model
        epochs = args.epochs or Config.PHASE1_EPOCHS
        train_phase1(args.data, args.output, epochs=epochs, subsample_pairs=args.subsample_pairs,
                     batch_size=args.batch_size, lr=args.lr)

    elif args.phase == 2:
        # Training Phase 2
        epochs = args.epochs or Config.PHASE2_EPOCHS
        train_phase2(args.data, args.phase1_model, args.output,
                     epochs=epochs, batch_size=args.batch_size, lr=args.lr)

    elif args.phase == 3:
        # Training Phase 3
        epochs = args.epochs or Config.PHASE3_EPOCHS
        train_phase3(args.data, args.phase2_model, args.output, subsample_pairs=args.subsample_pairs,
                     epochs=epochs, batch_size=args.batch_size, lr=args.lr or 5e-4)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()