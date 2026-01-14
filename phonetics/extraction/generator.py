#!/usr/bin/env python3
"""
Training Data Generator - Main class for generating Symphonym training data.
"""

import gc
import json
import random
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from elasticsearch.helpers import scan

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(x, **kwargs):
        return x

from .constants import (
    ES_PARALLEL_WORKERS, MAX_OVERSAMPLE_FACTOR, MAX_TOPONYMS_PER_PLACE,
    MIN_BIN_SIZE, MSEARCH_BATCH_SIZE, PARQUET_BATCH_SIZE,
    PHASE1_SAME_SCRIPT_NEGATIVE_RATIO, RANDOM_SEED, TARGET_SAMPLES_PER_BIN,
    apply_bin_balancing, get_script_lang_key, logger
)
from .es_knn_helper import ESKNNHelper
from .streaming_writer import TripletStreamingWriter, MultiSplitStreamingWriter


class TrainingDataGenerator:
    """Generates training data for all phases from ES."""

    def __init__(
        self,
        es,
        db_path: str,
        output_dir: Path,
        scratch_dir: Path,
        training_namespaces: List[str],
        force_regenerate: bool = False,
    ):
        self.es = es
        self.db_path = db_path
        self.output_dir = output_dir
        self.scratch_dir = scratch_dir
        self.training_namespaces = training_namespaces
        self.force_regenerate = force_regenerate

        self.knn = ESKNNHelper(es, index="toponyms")

        # DuckDB connection is optional
        self.conn = None

        self.stats = {
            'phase1': {'pairs': 0, 'triplets': 0, 'by_bin': {}},
            'phase2': {'samples': 0, 'by_bin': {}},
            'phase3': {'triplets': 0, 'by_bin': {}},
        }

    def _check_phase_complete(self, phase: str) -> bool:
        """Check if a phase's output files already exist."""
        if self.force_regenerate:
            return False

        if phase == 'pairs':
            return (self.output_dir / 'pairs' / 'positive_pairs.parquet').exists()
        elif phase == 'phase1':
            train = self.output_dir / 'triplets' / 'phase1' / 'train.parquet'
            val = self.output_dir / 'triplets' / 'phase1' / 'val.parquet'
            return train.exists() and val.exists()
        elif phase == 'phase2':
            train = self.output_dir / 'training' / 'split=train' / 'data.parquet'
            val = self.output_dir / 'training' / 'split=val' / 'data.parquet'
            return train.exists() and val.exists()
        elif phase == 'phase3':
            train = self.output_dir / 'triplets' / 'phase3' / 'train.parquet'
            val = self.output_dir / 'triplets' / 'phase3' / 'val.parquet'
            return train.exists() and val.exists()
        return False

    def _load_pairs_from_checkpoint(self) -> Dict[str, List[Tuple]]:
        """Load positive pairs from checkpoint Parquet file."""
        pairs_file = self.output_dir / 'pairs' / 'positive_pairs.parquet'
        logger.info(f"Loading pairs from checkpoint: {pairs_file}")

        table = pq.read_table(pairs_file)
        df = table.to_pandas()

        pairs_by_bin: Dict[str, List[Tuple]] = defaultdict(list)
        for _, row in df.iterrows():
            pairs_by_bin[row['bin']].append((row['anchor'], row['positive'], 1.0))

        logger.info(f"Loaded {len(df):,} pairs across {len(pairs_by_bin)} bins")
        return pairs_by_bin

    def generate_all(self):
        """Generate training data for all phases."""
        logger.info("=" * 60)
        logger.info("GENERATING TRAINING DATA FOR ALL PHASES")
        logger.info("=" * 60)

        # Step 1: Generate positive pairs
        logger.info("\n" + "=" * 60)
        logger.info("STEP 1: GENERATE POSITIVE PAIRS")
        logger.info("=" * 60)

        if self._check_phase_complete('pairs'):
            logger.info("✓ Positive pairs checkpoint found, loading...")
            pairs_by_bin = self._load_pairs_from_checkpoint()
        else:
            pairs_by_bin = self.generate_positive_pairs()

        # Step 2: Generate Phase 2 samples
        logger.info("\n" + "=" * 60)
        logger.info("STEP 2: GENERATE PHASE 2 SAMPLES")
        logger.info("=" * 60)

        if self._check_phase_complete('phase2'):
            logger.info("✓ Phase 2 checkpoint found, skipping...")
            training_dir = self.output_dir / 'training'
            total_samples = 0
            for split in ['train', 'val', 'test']:
                split_file = training_dir / f'split={split}' / 'data.parquet'
                if split_file.exists():
                    total_samples += pq.read_table(split_file).num_rows
            self.stats['phase2']['samples'] = total_samples
        else:
            self.generate_phase2_samples()

        # Clear cache between phases
        logger.info("Clearing embedding cache...")
        self.knn.clear_cache()
        self.knn.reset_failure_tracking()
        gc.collect()

        # Step 3: Generate Phase 1 triplets (MEMORY-EFFICIENT STREAMING)
        logger.info("\n" + "=" * 60)
        logger.info("STEP 3: GENERATE PHASE 1 TRIPLETS (streaming)")
        logger.info("=" * 60)

        if self._check_phase_complete('phase1'):
            logger.info("✓ Phase 1 checkpoint found, skipping...")
            phase1_train = pq.read_table(self.output_dir / 'triplets' / 'phase1' / 'train.parquet')
            phase1_val = pq.read_table(self.output_dir / 'triplets' / 'phase1' / 'val.parquet')
            self.stats['phase1']['triplets'] = len(phase1_train) + len(phase1_val)
        else:
            self.generate_phase1_triplets_streaming(pairs_by_bin)

        # Clear memory before Phase 3
        gc.collect()
        self.knn.reset_failure_tracking()

        # Step 4: Generate Phase 3 triplets
        logger.info("\n" + "=" * 60)
        logger.info("STEP 4: GENERATE PHASE 3 TRIPLETS")
        logger.info("=" * 60)

        if self._check_phase_complete('phase3'):
            logger.info("✓ Phase 3 checkpoint found, skipping...")
            phase3_train = pq.read_table(self.output_dir / 'triplets' / 'phase3' / 'train.parquet')
            phase3_val = pq.read_table(self.output_dir / 'triplets' / 'phase3' / 'val.parquet')
            self.stats['phase3']['triplets'] = len(phase3_train) + len(phase3_val)
        else:
            self.generate_phase3_triplets_streaming(pairs_by_bin)

        # Save statistics
        stats_path = self.output_dir / 'training_stats.json'
        with open(stats_path, 'w') as f:
            json.dump(self.stats, f, indent=2)
        logger.info(f"Statistics saved to {stats_path}")

        return self.stats

    def generate_positive_pairs(self) -> Dict[str, List[Tuple]]:
        """Generate positive pairs from co-located toponyms."""
        pairs_by_bin: Dict[str, List[Tuple]] = defaultdict(list)

        cluster_stats = {
            'places_processed': 0,
            'places_with_clusters': 0,
            'total_clusters': 0,
            'singleton_clusters': 0,
            'multi_clusters': 0,
            'cluster_sizes': Counter(),
        }

        logger.info("Querying ES for toponyms with PanPhon embeddings...")

        ns_filter = [{"term": {"namespaces": ns}} for ns in self.training_namespaces]

        query = {
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "panphon_embedding"}}],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            },
            "_source": ["attestations", "script", "lang"]
        }

        places: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        total_attestations = 0

        for hit in tqdm(scan(self.es, index="toponyms", query=query, scroll='5m', size=5000),
                       desc="Scanning ES for toponyms"):
            toponym_id = hit['_id']
            source = hit['_source']
            script = source.get('script', 'UNKNOWN')
            lang = source.get('lang', '')
            attestations = source.get('attestations', [])

            for place_id in attestations:
                places[place_id].append((toponym_id, script, lang))
                total_attestations += 1

        logger.info(f"Found {total_attestations:,} toponym attestations")
        logger.info(f"Grouped into {len(places):,} places")

        # Filter and cap
        places_with_multiple = {}
        for p, t in places.items():
            if len(t) >= 2:
                if len(t) > MAX_TOPONYMS_PER_PLACE:
                    t = random.sample(t, MAX_TOPONYMS_PER_PLACE)
                places_with_multiple[p] = t

        logger.info(f"Places with ≥2 toponyms: {len(places_with_multiple):,}")

        def process_place(place_id: str, toponyms_info: List[Tuple]) -> Tuple[str, List, Dict]:
            id_to_info = {t[0]: (t[1], t[2]) for t in toponyms_info}
            toponym_ids = list(id_to_info.keys())
            clusters = self.knn.find_similar_in_place(place_id=place_id, toponym_ids=toponym_ids)
            return place_id, clusters, id_to_info

        logger.info(f"Running parallel ES KNN clustering...")
        total_pairs = 0

        with ThreadPoolExecutor(max_workers=ES_PARALLEL_WORKERS) as executor:
            futures = {
                executor.submit(process_place, pid, info): pid
                for pid, info in places_with_multiple.items()
            }

            iterator = tqdm(as_completed(futures), total=len(futures), desc="Clustering")

            for future in iterator:
                try:
                    place_id, clusters, id_to_info = future.result()
                except Exception as e:
                    logger.debug(f"Place processing failed: {e}")
                    continue

                cluster_stats['places_processed'] += 1
                cluster_stats['places_with_clusters'] += 1
                cluster_stats['total_clusters'] += len(clusters)

                if len(clusters) > 1:
                    cluster_stats['multi_clusters'] += 1

                seen_in_place: Set[Tuple[str, str]] = set()

                for cluster in clusters:
                    cluster_stats['cluster_sizes'][len(cluster)] += 1

                    if len(cluster) < 2:
                        cluster_stats['singleton_clusters'] += 1
                        continue

                    for i, id_a in enumerate(cluster):
                        for id_b in cluster[i+1:]:
                            script_a, lang_a = id_to_info[id_a]
                            script_b, lang_b = id_to_info[id_b]

                            pair_key = tuple(sorted([id_a, id_b]))
                            if pair_key in seen_in_place:
                                continue
                            seen_in_place.add(pair_key)

                            key_a = get_script_lang_key(script_a, lang_a)
                            key_b = get_script_lang_key(script_b, lang_b)
                            bin_key = tuple(sorted([key_a, key_b]))
                            bin_key_str = f"{bin_key[0]}|{bin_key[1]}"

                            pairs_by_bin[bin_key_str].append((id_a, id_b, 0.0))
                            total_pairs += 1

                if cluster_stats['places_processed'] % 10000 == 0:
                    self.knn.check_failure_threshold()

        failure_rate = self.knn.get_failure_rate()
        logger.info(f"ES failure rate: {failure_rate:.2%}")
        self.knn.check_failure_threshold()

        logger.info(f"Generated {total_pairs:,} positive pairs")
        logger.info(f"Distributed across {len(pairs_by_bin)} bins")

        # Log statistics
        logger.info("Clustering statistics:")
        logger.info(f"  Places processed: {cluster_stats['places_processed']:,}")
        logger.info(f"  Total clusters: {cluster_stats['total_clusters']:,}")

        bin_sizes = [(k, len(v)) for k, v in pairs_by_bin.items()]
        bin_sizes.sort(key=lambda x: -x[1])
        logger.info("Top 10 bins:")
        for bin_key, count in bin_sizes[:10]:
            logger.info(f"  {bin_key}: {count:,}")

        self.stats['phase1']['pairs'] = total_pairs
        self.stats['phase1']['by_bin'] = {k: len(v) for k, v in pairs_by_bin.items()}

        # Save pairs
        pairs_dir = self.output_dir / 'pairs'
        pairs_dir.mkdir(exist_ok=True)

        all_pairs = []
        for bin_key, pairs in pairs_by_bin.items():
            for id_a, id_b, sim in pairs:
                all_pairs.append({'anchor': id_a, 'positive': id_b, 'bin': bin_key})

        if all_pairs:
            table = pa.Table.from_pylist(all_pairs)
            pq.write_table(table, pairs_dir / 'positive_pairs.parquet')
            logger.info(f"Saved pairs to {pairs_dir / 'positive_pairs.parquet'}")

        return pairs_by_bin

    def generate_phase2_samples(self):
        """Generate Phase 2 samples with streaming writes."""
        logger.info("=" * 60)
        logger.info("PHASE 2: GENERATING BALANCED TRAINING SAMPLES")
        logger.info("=" * 60)

        training_dir = self.output_dir / 'training'
        training_dir.mkdir(exist_ok=True)

        # Load char vocabulary
        vocab_path = self.output_dir / 'vocab' / 'char_vocab.json'
        if not vocab_path.exists():
            logger.error(f"Char vocabulary not found at {vocab_path}")
            return

        with open(vocab_path) as f:
            char_vocab = json.load(f)
        char_to_id = char_vocab.get('char_to_id', {})
        unk_id = char_to_id.get('<UNK>', 1)
        logger.info(f"Loaded char vocabulary with {len(char_to_id)} characters")

        ns_filter = [{"term": {"namespaces": ns}} for ns in self.training_namespaces]

        # Count with embedding
        with_emb_query = {
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "panphon_embedding"}}],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            }
        }
        with_features = self.es.count(index="toponyms", body=with_emb_query)['count']
        logger.info(f"Found {with_features:,} toponyms with PanPhon embeddings")

        # Count bin sizes using ES aggregation
        logger.info("Pass 1: Counting samples per bin...")
        bin_counts: Counter = Counter()

        agg_query = {
            "size": 0,
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "panphon_embedding"}}],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            },
            "aggs": {
                "script_lang": {
                    "composite": {
                        "size": 10000,
                        "sources": [
                            {"script": {"terms": {"field": "script"}}},
                            {"lang": {"terms": {"field": "lang"}}}
                        ]
                    }
                }
            }
        }

        after_key = None
        while True:
            if after_key:
                agg_query["aggs"]["script_lang"]["composite"]["after"] = after_key

            result = self.es.search(index="toponyms", body=agg_query)
            buckets = result['aggregations']['script_lang']['buckets']

            if not buckets:
                break

            for bucket in buckets:
                script = bucket['key']['script'] or 'UNKNOWN'
                lang = bucket['key']['lang'] or ''
                count = bucket['doc_count']
                bin_key = get_script_lang_key(script, lang)
                bin_counts[bin_key] = count

            after_key = result['aggregations']['script_lang'].get('after_key')
            if not after_key:
                break

        logger.info(f"Found {len(bin_counts)} bins")

        # Calculate sampling quotas
        bin_quotas = {}
        dropped_bins = []
        stats = {'bins_total': len(bin_counts), 'bins_dropped': 0, 'bins_capped': 0,
                 'bins_oversampled': 0, 'bins_unchanged': 0}

        for bin_key, count in bin_counts.items():
            if count < MIN_BIN_SIZE:
                dropped_bins.append((bin_key, count))
                stats['bins_dropped'] += 1
                continue

            if count >= TARGET_SAMPLES_PER_BIN:
                target = TARGET_SAMPLES_PER_BIN
                prob = TARGET_SAMPLES_PER_BIN / count
                stats['bins_capped'] += 1
            else:
                max_target = min(TARGET_SAMPLES_PER_BIN, count * MAX_OVERSAMPLE_FACTOR)
                if max_target > count:
                    target = max_target
                    prob = max_target / count
                    stats['bins_oversampled'] += 1
                else:
                    target = count
                    prob = 1.0
                    stats['bins_unchanged'] += 1

            bin_quotas[bin_key] = (target, prob)

        logger.info(f"Bin balancing: dropped={stats['bins_dropped']}, capped={stats['bins_capped']}, oversampled={stats['bins_oversampled']}")

        # Use streaming writer
        writer = MultiSplitStreamingWriter(training_dir, batch_size=PARQUET_BATCH_SIZE)

        # Stream from ES
        logger.info("Pass 2: Streaming and writing...")

        es_query = {
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "panphon_embedding"}}],
                    "should": ns_filter,
                    "minimum_should_match": 1
                }
            },
            "_source": ["name", "script", "lang", "ipa", "panphon_embedding"]
        }

        processed = 0
        for hit in tqdm(scan(self.es, index="toponyms", query=es_query, scroll='5m', size=5000),
                       total=with_features, desc="Streaming"):
            toponym_id = hit['_id']
            source = hit['_source']
            name = source.get('name', '')
            script = source.get('script', 'UNKNOWN')
            lang = source.get('lang', '')
            panphon_features = source.get('panphon_embedding', [])

            bin_key = get_script_lang_key(script, lang)

            if bin_key not in bin_quotas:
                continue

            target, prob = bin_quotas[bin_key]

            rng = random.Random(RANDOM_SEED + (zlib.crc32(toponym_id.encode('utf-8')) & 0xffffffff))

            if prob >= 1.0:
                num_copies = int(prob)
                if rng.random() < (prob - num_copies):
                    num_copies += 1
            else:
                num_copies = 1 if rng.random() < prob else 0

            if num_copies == 0:
                continue

            features = panphon_features
            if not features:
                continue

            char_ids = [char_to_id.get(c, unk_id) for c in name]

            hash_val = (zlib.crc32(toponym_id.encode('utf-8')) & 0xffffffff) % 10
            if hash_val == 0:
                split = 'test'
            elif hash_val == 1:
                split = 'val'
            else:
                split = 'train'

            sample = {
                'toponym_id': toponym_id,
                'name': name,
                'script': script,
                'lang': lang or '',
                'char_ids': char_ids,
                'features': features,
                'feature_length': len(features) // 24 if len(features) >= 24 else len(features),
                'epitran_supported': True,
                'split': split,
            }

            for _ in range(num_copies):
                writer.add_sample(split, sample.copy())

            processed += 1
            if processed % 500000 == 0:
                logger.info(f"  Processed {processed:,}...")

        writer.close_all()
        split_counts = writer.get_counts()

        total_samples = sum(split_counts.values())
        self.stats['phase2']['samples'] = total_samples
        self.stats['phase2']['balance_stats'] = stats
        self.stats['phase2']['by_split'] = split_counts

        logger.info(f"Sampled {total_samples:,} total samples")
        for split_name, count in split_counts.items():
            logger.info(f"  {split_name}: {count:,}")

    def generate_phase1_triplets_streaming(self, pairs_by_bin: Dict[str, List[Tuple]]):
        """
        Generate Phase 1 triplets with STREAMING writes to avoid OOM.

        This is the key memory optimization: instead of building 27M+ triplets
        in memory, we stream them to disk in batches.
        """
        logger.info("=" * 60)
        logger.info("PHASE 1: Streaming triplet generation (memory-efficient)")
        logger.info("=" * 60)

        # Build adjacency set
        logger.info("Building adjacency set...")
        adjacency: Set[Tuple[str, str]] = set()
        for pairs in pairs_by_bin.values():
            for id_a, id_b, _ in pairs:
                adjacency.add((id_a, id_b))
                adjacency.add((id_b, id_a))
        logger.info(f"Adjacency set has {len(adjacency):,} edges")

        # Collect all unique toponym IDs we need features for
        logger.info("Collecting unique toponym IDs...")
        all_anchor_ids = set()
        for pairs in pairs_by_bin.values():
            for id_a, id_b, _ in pairs:
                all_anchor_ids.add(id_a)
                all_anchor_ids.add(id_b)

        # Load toponym data with features using NUMPY arrays (60-70% memory savings)
        logger.info(f"Loading toponym data for {len(all_anchor_ids):,} toponyms...")
        toponym_data_map = {}
        anchor_list = list(all_anchor_ids)
        batch_size = 5000

        for i in range(0, len(anchor_list), batch_size):
            batch = anchor_list[i:i+batch_size]
            docs = self.es.mget(index="toponyms", body={"ids": batch},
                               _source=['script', 'lang', 'panphon_embedding'])
            for doc in docs.get('docs', []):
                if doc.get('found') and '_source' in doc:
                    toponym_id = doc['_id']
                    source = doc['_source']
                    embedding = source.get('panphon_embedding')

                    if embedding and len(embedding) > 0:
                        # Store as numpy float32 to save memory
                        toponym_data_map[toponym_id] = {
                            'script': source.get('script', 'UNKNOWN'),
                            'lang': source.get('lang', ''),
                            'features': np.array(embedding, dtype=np.float32),
                            'feature_length': len(embedding) // 24,
                        }

            if (i + batch_size) % 50000 < batch_size:
                logger.info(f"  Loaded {min(i + batch_size, len(anchor_list)):,} / {len(anchor_list):,}...")

        logger.info(f"Loaded data for {len(toponym_data_map):,} toponyms")

        # Load negative candidates grouped by script
        logger.info("Loading negative candidates by script...")
        all_ids = []
        ids_by_script: Dict[str, List[str]] = defaultdict(list)

        neg_query = {
            "query": {"exists": {"field": "panphon_embedding"}},
            "_source": ["script"]
        }

        for hit in tqdm(scan(self.es, index="toponyms", query=neg_query, scroll='5m', size=10000),
                       desc="Loading negatives"):
            toponym_id = hit['_id']
            script = hit['_source'].get('script', 'UNKNOWN')
            all_ids.append(toponym_id)
            if script:
                ids_by_script[script].append(toponym_id)

        logger.info(f"Loaded {len(all_ids):,} negative candidates")

        # Apply bin balancing
        logger.info("Applying bin-balancing...")
        balanced_pairs, balance_stats = apply_bin_balancing(
            pairs_by_bin,
            target_per_bin=TARGET_SAMPLES_PER_BIN,
            min_bin_size=MIN_BIN_SIZE,
            max_oversample=MAX_OVERSAMPLE_FACTOR,
        )
        logger.info(f"Balanced pairs: {len(balanced_pairs):,}")

        # Set up streaming writer
        triplets_dir = self.output_dir / 'triplets' / 'phase1'
        triplets_dir.mkdir(parents=True, exist_ok=True)
        writer = TripletStreamingWriter(triplets_dir, batch_size=PARQUET_BATCH_SIZE)

        # STREAMING GENERATION: Process triplets and write incrementally
        logger.info("Streaming triplet generation and writing...")

        triplet_count = 0
        skipped_missing = 0
        negative_feature_cache = {}  # Small cache for negative features

        for triplet_idx, pair in enumerate(tqdm(balanced_pairs, desc="Generating triplets")):
            anchor, positive, _ = pair if isinstance(pair, tuple) else (pair[0], pair[1], 0)

            # Skip if anchor or positive don't have features
            if anchor not in toponym_data_map or positive not in toponym_data_map:
                skipped_missing += 1
                continue

            anchor_data = toponym_data_map[anchor]
            script = anchor_data['script']
            lang = anchor_data['lang']

            # Deterministic negative sampling
            seed = RANDOM_SEED + (zlib.crc32(anchor.encode('utf-8')) & 0xffffffff) + triplet_idx
            rng = random.Random(seed)

            negative = None
            for _ in range(10):
                use_same_script = rng.random() < PHASE1_SAME_SCRIPT_NEGATIVE_RATIO

                if use_same_script and script and script in ids_by_script and len(ids_by_script[script]) > 0:
                    candidate = rng.choice(ids_by_script[script])
                else:
                    candidate = rng.choice(all_ids)

                if (anchor, candidate) not in adjacency and (positive, candidate) not in adjacency:
                    negative = candidate
                    break

            if negative is None:
                skipped_missing += 1
                continue

            # Get negative features (from cache, toponym_data_map, or ES)
            if negative in toponym_data_map:
                negative_data = toponym_data_map[negative]
            elif negative in negative_feature_cache:
                negative_data = negative_feature_cache[negative]
            else:
                # Fetch from ES
                try:
                    doc = self.es.get(index="toponyms", id=negative, _source=['panphon_embedding'])
                    emb = doc['_source'].get('panphon_embedding', [])
                    if emb:
                        negative_data = {
                            'features': np.array(emb, dtype=np.float32),
                            'feature_length': len(emb) // 24,
                        }
                        # Keep cache bounded
                        if len(negative_feature_cache) > 100000:
                            negative_feature_cache.clear()
                        negative_feature_cache[negative] = negative_data
                    else:
                        skipped_missing += 1
                        continue
                except Exception:
                    skipped_missing += 1
                    continue

            # Build triplet (convert numpy back to list for Parquet)
            bin_key = get_script_lang_key(script, lang)
            triplet = {
                'anchor_id': anchor,
                'positive_id': positive,
                'negative_id': negative,
                'bin': bin_key,
                'anchor_features': anchor_data['features'].tolist(),
                'anchor_feature_length': anchor_data['feature_length'],
                'positive_features': toponym_data_map[positive]['features'].tolist(),
                'positive_feature_length': toponym_data_map[positive]['feature_length'],
                'negative_features': negative_data['features'].tolist(),
                'negative_feature_length': negative_data['feature_length'],
            }

            # Determine split and write
            split_hash = (zlib.crc32(anchor.encode('utf-8')) & 0xffffffff) % 10
            writer.add_triplet(triplet, split_hash)
            triplet_count += 1

        # Close writer
        train_count, val_count = writer.close()

        logger.info(f"Generated {triplet_count:,} triplets (skipped {skipped_missing:,})")
        logger.info(f"Saved {train_count:,} train, {val_count:,} val triplets")

        self.stats['phase1']['triplets'] = triplet_count
        self.stats['phase1']['balance_stats'] = balance_stats

        # Free memory
        del toponym_data_map
        del negative_feature_cache
        gc.collect()

    def generate_phase3_triplets_streaming(self, pairs_by_bin: Dict[str, List[Tuple]]):
        """
        Generate Phase 3 triplets with hard negatives using streaming writes.
        """
        logger.info("=" * 60)
        logger.info("PHASE 3: Streaming hard negative triplet generation")
        logger.info("=" * 60)

        # Build adjacency set
        logger.info("Building adjacency set...")
        adjacency: Set[Tuple[str, str]] = set()
        for pairs in pairs_by_bin.values():
            for id_a, id_b, _ in pairs:
                adjacency.add((id_a, id_b))
                adjacency.add((id_b, id_a))
        logger.info(f"Adjacency set has {len(adjacency):,} edges")

        # Apply bin balancing
        logger.info("Applying bin-balancing...")
        balanced_pairs, balance_stats = apply_bin_balancing(
            pairs_by_bin,
            target_per_bin=TARGET_SAMPLES_PER_BIN,
            min_bin_size=MIN_BIN_SIZE,
            max_oversample=MAX_OVERSAMPLE_FACTOR,
        )
        logger.info(f"Balanced pairs: {len(balanced_pairs):,}")

        # Collect unique anchors
        unique_anchors = set()
        for pair in balanced_pairs:
            anchor, positive, _ = pair if isinstance(pair, tuple) else (pair[0], pair[1], 0)
            unique_anchors.add(anchor)
            unique_anchors.add(positive)
        logger.info(f"Unique toponyms: {len(unique_anchors):,}")

        # Load anchor info
        logger.info("Loading anchor info...")
        anchor_info = {}
        anchor_list = list(unique_anchors)
        batch_size = 5000

        for i in range(0, len(anchor_list), batch_size):
            batch = anchor_list[i:i+batch_size]
            docs = self.es.mget(index="toponyms", body={"ids": batch}, _source=['script', 'lang'])
            for doc in docs.get('docs', []):
                if doc.get('found') and '_source' in doc:
                    toponym_id = doc['_id']
                    source = doc['_source']
                    anchor_info[toponym_id] = (source.get('script', 'UNKNOWN'), source.get('lang', ''))

            if (i + batch_size) % 50000 < batch_size:
                logger.info(f"  Loaded {min(i + batch_size, len(anchor_list)):,} / {len(anchor_list):,}...")

        logger.info(f"Loaded info for {len(anchor_info):,} toponyms")

        # Build pairs to process
        all_pairs_to_process = []
        for pair in balanced_pairs:
            anchor, positive, _ = pair if isinstance(pair, tuple) else (pair[0], pair[1], 0)
            if anchor in anchor_info:
                script, lang = anchor_info[anchor]
                bin_key = get_script_lang_key(script, lang)
                all_pairs_to_process.append((anchor, positive, bin_key))

        logger.info(f"Processing {len(all_pairs_to_process):,} pairs...")

        # Pre-fetch embeddings
        all_anchors = list(set(p[0] for p in all_pairs_to_process))
        logger.info(f"Pre-fetching embeddings for {len(all_anchors):,} anchors...")

        anchor_embeddings = {}
        for i in range(0, len(all_anchors), 5000):
            batch = all_anchors[i:i+5000]
            batch_embs = self.knn.batch_get_embeddings(batch)
            anchor_embeddings.update(batch_embs)
            if (i + 5000) % 50000 < 5000:
                logger.info(f"  Fetched {min(i + 5000, len(all_anchors)):,} / {len(all_anchors):,}")

        logger.info(f"Fetched {len(anchor_embeddings):,} embeddings")

        # Set up streaming writer
        triplets_dir = self.output_dir / 'triplets' / 'phase3'
        triplets_dir.mkdir(parents=True, exist_ok=True)
        writer = TripletStreamingWriter(triplets_dir, batch_size=PARQUET_BATCH_SIZE)

        # Build batches for _msearch
        batches = []
        current_batch = []
        failed_lookups = 0

        for sample_idx, (anchor, positive, bin_key) in enumerate(all_pairs_to_process):
            if anchor not in anchor_embeddings:
                failed_lookups += 1
                continue

            script, lang = anchor_info[anchor]
            current_batch.append({
                'anchor_id': anchor,
                'positive_id': positive,
                'embedding': anchor_embeddings[anchor],
                'script': script,
                'bin': bin_key,
                'sample_idx': sample_idx,
            })

            if len(current_batch) >= MSEARCH_BATCH_SIZE:
                batches.append(current_batch)
                current_batch = []

        if current_batch:
            batches.append(current_batch)

        logger.info(f"Processing {len(batches)} batches...")

        triplet_count = 0
        batches_processed = 0

        for batch in tqdm(batches, desc="Hard negative mining"):
            hard_negs = self.knn.find_hard_negatives_batch(
                anchors=batch,
                adjacency=adjacency,
                k=20,
            )

            for item, hard_neg in zip(batch, hard_negs):
                if hard_neg:
                    triplet = {
                        'anchor_id': item['anchor_id'],
                        'positive_id': item['positive_id'],
                        'negative_id': hard_neg,
                        'negative_type': 'hard',
                        'bin': item['bin'],
                    }

                    split_hash = (zlib.crc32(item['anchor_id'].encode('utf-8')) & 0xffffffff) % 10
                    writer.add_triplet(triplet, split_hash)
                    triplet_count += 1

            batches_processed += 1
            if batches_processed % 100 == 0:
                self.knn.check_failure_threshold()

        # Final checks
        failure_rate = self.knn.get_failure_rate()
        logger.info(f"ES failure rate: {failure_rate:.2%}")
        self.knn.check_failure_threshold()

        if failed_lookups > 0:
            logger.warning(f"Failed lookups: {failed_lookups:,}")

        train_count, val_count = writer.close()

        logger.info(f"Generated {triplet_count:,} Phase 3 triplets")
        logger.info(f"Saved {train_count:,} train, {val_count:,} val triplets")

        self.stats['phase3']['triplets'] = triplet_count
        self.stats['phase3']['balance_stats'] = balance_stats