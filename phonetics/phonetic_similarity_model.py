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
    pip install torch epitran panphon anyascii elasticsearch h5py

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
from collections import defaultdict
from typing import Optional, List, Tuple, Dict, Any

import numpy as np
import h5py
import torch
import torch.nn as nn
import torch.nn.functional as F
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
    BATCH_SIZE = 128
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

    def fit(self, texts: List[str]) -> 'CharVocab':
        """Build vocabulary from training texts."""
        char_counts = defaultdict(int)
        for text in texts:
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

    def fit(self, languages: List[str]) -> 'LangVocab':
        """Build vocabulary from language codes."""
        for lang in sorted(set(languages)):
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

class TrainingDataExtractor:
    """Extract training data from Elasticsearch, streaming directly to HDF5."""

    def __init__(self, es_host: str = 'localhost:9200', index_name: str = 'places'):
        try:
            from elasticsearch import Elasticsearch
            self.es = Elasticsearch([es_host])
            self.index = index_name
        except ImportError:
            print("Warning: elasticsearch not installed.")
            self.es = None
            self.index = None

        self.ft = FeatureTable()
        self.dst = panphon.distance.Distance()
        self._epi_cache = {}

    def get_epitran(self, lang_code: str) -> Optional[epitran.Epitran]:
        if lang_code not in self._epi_cache:
            epitran_code = Config.EPITRAN_LANGS.get(lang_code)
            if epitran_code:
                try:
                    self._epi_cache[lang_code] = epitran.Epitran(epitran_code)
                except Exception as e:
                    print(f"Warning: Failed to load Epitran for {lang_code}: {e}")
                    self._epi_cache[lang_code] = None
            else:
                self._epi_cache[lang_code] = None
        return self._epi_cache[lang_code]

    def phonetic_similarity(self, ipa_a: str, ipa_b: str) -> float:
        if not ipa_a or not ipa_b:
            return 0.0
        try:
            fed = self.dst.feature_edit_distance(ipa_a, ipa_b)
            segs_a = self.dst.fm.ipa_segs(ipa_a)
            segs_b = self.dst.fm.ipa_segs(ipa_b)
            max_len = max(len(segs_a), len(segs_b))
            if max_len == 0:
                return 0.0
            return 1.0 - (fed / max_len)
        except Exception:
            return 0.0

    def process_toponym(self, toponym: str, lang_code: str) -> Optional[Dict[str, Any]]:
        romanized = anyascii(toponym).lower().strip()
        if not romanized:
            return None

        ipa = None
        features = None

        epi = self.get_epitran(lang_code)
        if epi:
            try:
                ipa = epi.transliterate(toponym)
                if ipa:
                    features = self.ft.word_to_vector_list(ipa, numeric=True)
                    if not features:
                        ipa = None
                        features = None
            except (IndexError, KeyError, ValueError):
                ipa = None
                features = None
            except Exception as e:
                print(f"Warning: Unexpected Epitran error for '{toponym}' ({lang_code}): {type(e).__name__}")
                ipa = None
                features = None

        return {
            'toponym': toponym,
            'romanized': romanized,
            'lang': lang_code,
            'ipa': ipa,
            'features': features,
            'has_phonetic': features is not None
        }

    def extract_clusters_from_es(
            self,
            batch_size: int = 1000,
            max_docs: Optional[int] = None,
            min_cluster_size: int = 2
    ) -> List[List[Tuple[str, str]]]:
        if self.es is None:
            raise RuntimeError("Elasticsearch not available")

        query = {
            "query": {"match_all": {}},
            "_source": ["place_id", "toponyms"]
        }
        clusters = []
        count = 0
        skipped_no_lang = 0

        for hit in self._scroll_search(query, batch_size):
            source = hit['_source']
            cluster = []
            seen = set()

            toponyms = source.get('toponyms', [])
            for entry in toponyms:
                if isinstance(entry, dict):
                    toponym_id = entry.get('toponym_id', '')
                    if '@' in toponym_id:
                        at_idx = toponym_id.rfind('@')
                        toponym = toponym_id[:at_idx].strip()
                        lang = toponym_id[at_idx + 1:].strip()

                        if toponym and lang:
                            key = (toponym.lower(), lang.lower())
                            if key not in seen:
                                seen.add(key)
                                cluster.append((toponym, lang))
                    else:
                        skipped_no_lang += 1

            if len(cluster) >= min_cluster_size:
                clusters.append(cluster)

            count += 1
            if count % 10000 == 0:
                print(f"  Processed {count} documents, {len(clusters)} valid clusters...")

            if max_docs and count >= max_docs:
                break

        print(f"\nExtraction complete:")
        print(f"  Documents processed: {count}")
        print(f"  Valid clusters (>= {min_cluster_size} toponyms): {len(clusters)}")
        print(f"  Toponyms skipped (no language tag): {skipped_no_lang}")

        return clusters

    def _scroll_search(self, query: dict, batch_size: int):
        resp = self.es.search(index=self.index, body=query, scroll='5m', size=batch_size)
        scroll_id = resp['_scroll_id']
        hits = resp['hits']['hits']

        while hits:
            for hit in hits:
                yield hit
            resp = self.es.scroll(scroll_id=scroll_id, scroll='5m')
            scroll_id = resp['_scroll_id']
            hits = resp['hits']['hits']

    def build_training_data_streaming(
            self,
            clusters: List[List[Tuple[str, str]]],
            output_path: str,
            similarity_threshold: float = Config.SIMILARITY_THRESHOLD
    ):
        """Stream training data directly to HDF5 file (memory-efficient)."""

        print(f"Pass 1/2: Counting items and estimating pairs...")

        total_items = 0
        cluster_item_counts = []
        stats = {'total': 0, 'with_ipa': 0, 'without_ipa': 0}
        skipped_langs = defaultdict(int)

        for cluster_idx, cluster in enumerate(clusters):
            valid_count = 0
            for toponym, lang in cluster:
                result = self.process_toponym(toponym, lang)
                if result:
                    valid_count += 1
                    total_items += 1
                    stats['total'] += 1
                    if result['has_phonetic']:
                        stats['with_ipa'] += 1
                    else:
                        stats['without_ipa'] += 1
                        skipped_langs[lang] += 1

            cluster_item_counts.append(valid_count)

            if (cluster_idx + 1) % 1000 == 0:
                print(f"  Processed {cluster_idx + 1:,} clusters, {total_items:,} items...")

        print(f"\nPass 2/2: Writing to {output_path}...")
        print(f"  Total items: {total_items:,}")

        # Create HDF5 file
        with h5py.File(output_path, 'w') as f:
            str_dtype = h5py.special_dtype(vlen=str)

            items_grp = f.create_group('items')
            items_grp.create_dataset('toponym', (total_items,), dtype=str_dtype)
            items_grp.create_dataset('romanized', (total_items,), dtype=str_dtype)
            items_grp.create_dataset('lang', (total_items,), dtype=str_dtype)
            items_grp.create_dataset('ipa', (total_items,), dtype=str_dtype)
            items_grp.create_dataset('cluster_id', (total_items,), dtype='i4')
            items_grp.create_dataset('has_phonetic', (total_items,), dtype='bool')

            features_grp = f.create_group('features')

            pairs_phon_grp = f.create_group('pairs_with_phonetic')
            pairs_phon_grp.create_dataset('anchor_idx', (0,), maxshape=(None,), dtype='i4', chunks=True)
            pairs_phon_grp.create_dataset('positive_idx', (0,), maxshape=(None,), dtype='i4', chunks=True)
            pairs_phon_grp.create_dataset('similarity', (0,), maxshape=(None,), dtype='f4', chunks=True)

            pairs_no_phon_grp = f.create_group('pairs_without_phonetic')
            pairs_no_phon_grp.create_dataset('anchor_idx', (0,), maxshape=(None,), dtype='i4', chunks=True)
            pairs_no_phon_grp.create_dataset('positive_idx', (0,), maxshape=(None,), dtype='i4', chunks=True)

            item_idx = 0
            pair_phon_idx = 0
            pair_no_phon_idx = 0

            for cluster_idx, cluster in enumerate(clusters):
                cluster_items = []

                for toponym, lang in cluster:
                    result = self.process_toponym(toponym, lang)
                    if not result:
                        continue

                    items_grp['toponym'][item_idx] = result['toponym']
                    items_grp['romanized'][item_idx] = result['romanized']
                    items_grp['lang'][item_idx] = result['lang']
                    items_grp['ipa'][item_idx] = result['ipa'] or ''
                    items_grp['cluster_id'][item_idx] = cluster_idx
                    items_grp['has_phonetic'][item_idx] = result['has_phonetic']

                    if result['features'] is not None:
                        features_grp.create_dataset(
                            str(item_idx),
                            data=np.array(result['features'], dtype='f4')
                        )

                    cluster_items.append({
                        'idx': item_idx,
                        'ipa': result['ipa'],
                        'has_phonetic': result['has_phonetic']
                    })

                    item_idx += 1

                # Generate pairs
                for i, item_a in enumerate(cluster_items):
                    for item_b in cluster_items[i + 1:]:
                        if item_a['has_phonetic'] and item_b['has_phonetic']:
                            sim = self.phonetic_similarity(item_a['ipa'], item_b['ipa'])
                            if sim >= similarity_threshold:
                                idx = pair_phon_idx
                                pairs_phon_grp['anchor_idx'].resize((idx + 1,))
                                pairs_phon_grp['positive_idx'].resize((idx + 1,))
                                pairs_phon_grp['similarity'].resize((idx + 1,))
                                pairs_phon_grp['anchor_idx'][idx] = item_a['idx']
                                pairs_phon_grp['positive_idx'][idx] = item_b['idx']
                                pairs_phon_grp['similarity'][idx] = sim
                                pair_phon_idx += 1
                        else:
                            idx = pair_no_phon_idx
                            pairs_no_phon_grp['anchor_idx'].resize((idx + 1,))
                            pairs_no_phon_grp['positive_idx'].resize((idx + 1,))
                            pairs_no_phon_grp['anchor_idx'][idx] = item_a['idx']
                            pairs_no_phon_grp['positive_idx'][idx] = item_b['idx']
                            pair_no_phon_idx += 1

                if (cluster_idx + 1) % 1000 == 0:
                    print(f"  Written {item_idx:,} items, "
                          f"{pair_phon_idx:,} phonetic pairs, "
                          f"{pair_no_phon_idx:,} non-phonetic pairs...")

            f.attrs['total_items'] = item_idx
            f.attrs['pairs_with_phonetic'] = pair_phon_idx
            f.attrs['pairs_without_phonetic'] = pair_no_phon_idx
            f.attrs['with_ipa'] = stats['with_ipa']
            f.attrs['without_ipa'] = stats['without_ipa']
            f.attrs['similarity_threshold'] = similarity_threshold

        print(f"\nData Statistics:")
        print(f"  Total items: {stats['total']:,}")
        print(f"  With IPA: {stats['with_ipa']:,} ({100 * stats['with_ipa'] / max(1, stats['total']):.1f}%)")
        print(f"  Without IPA: {stats['without_ipa']:,} ({100 * stats['without_ipa'] / max(1, stats['total']):.1f}%)")
        print(f"  Pairs with phonetic: {pair_phon_idx:,}")
        print(f"  Pairs without phonetic: {pair_no_phon_idx:,}")

        if skipped_langs:
            top_skipped = sorted(skipped_langs.items(), key=lambda x: -x[1])[:10]
            print(f"  Top languages without Epitran: {dict(top_skipped)}")

        print(f"\nSaved training data to {output_path}")


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
        batch_size: int = Config.BATCH_SIZE,
        lr: float = Config.LEARNING_RATE
):
    """Phase 1: Train phonetic encoder (streaming from HDF5)."""

    print("=" * 60)
    print("Phase 1: Training Phonetic Encoder (Teacher)")
    print("=" * 60)

    train_dataset = StreamingPhase1Dataset(data_path, split='train')
    val_dataset = StreamingPhase1Dataset(data_path, split='val')

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

    train_dataset = StreamingPhase3Dataset(data_path, char_vocab, lang_vocab, split='train')
    val_dataset = StreamingPhase3Dataset(data_path, char_vocab, lang_vocab, split='val')

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
    parser.add_argument('--lr', type=float, default=Config.LEARNING_RATE)

    # Inference
    parser.add_argument('--model', default='final_model.pt')
    parser.add_argument('--toponym1')
    parser.add_argument('--lang1')
    parser.add_argument('--toponym2')
    parser.add_argument('--lang2')
    parser.add_argument('--gpu', action='store_true')

    args = parser.parse_args()

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

    elif args.phase == 0:
        # Data extraction - args.output is the HDF5 file
        extractor = TrainingDataExtractor(args.es_host, args.index)
        clusters = extractor.extract_clusters_from_es(max_docs=args.max_docs)
        extractor.build_training_data_streaming(clusters, args.output)  # ← Use args.output

    elif args.phase == 1:
        # Training Phase 1 - args.data is HDF5, args.output is model
        epochs = args.epochs or Config.PHASE1_EPOCHS
        train_phase1(args.data, args.output, epochs=epochs,
                     batch_size=args.batch_size, lr=args.lr)

    elif args.phase == 2:
        # Training Phase 2
        epochs = args.epochs or Config.PHASE2_EPOCHS
        train_phase2(args.data, args.phase1_model, args.output,
                     epochs=epochs, batch_size=args.batch_size, lr=args.lr)

    elif args.phase == 3:
        # Training Phase 3
        epochs = args.epochs or Config.PHASE3_EPOCHS
        train_phase3(args.data, args.phase2_model, args.output,
                     epochs=epochs, batch_size=args.batch_size, lr=args.lr or 5e-4)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()