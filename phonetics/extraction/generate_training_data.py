#!/usr/bin/env python3
"""
v4 Training Data Generation Pipeline

Generates training data for all three phases of Symphonym training:
- Phase 1: Teacher training with phonetic features (triplets)
- Phase 2: Student alignment (all toponyms with PanPhon embeddings)
- Phase 3: Hard negative fine-tuning (triplets from ES similarity)

Key differences from v3:
- Uses ES KNN for PanPhon similarity queries (not Python computation)
- Balanced sampling by script+language pair across all phases
- Hard negatives selected via ES KNN (same script, similar embedding, different place)
"""

import argparse
import json
import logging
import random
import struct
import sys
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import duckdb

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    PYARROW_AVAILABLE = True
except ImportError:
    PYARROW_AVAILABLE = False

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(x, **kwargs):
        return x

try:
    from elasticsearch import Elasticsearch
    from elasticsearch.helpers import scan
except ImportError:
    print("ERROR: elasticsearch package required")
    sys.exit(1)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.getLogger("elastic_transport").setLevel(logging.WARNING)
logging.getLogger("elasticsearch").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Constants
TRAINING_NAMESPACES = ['gn', 'wd', 'tgn']

# ES KNN returns scores in range [0, 1] where score = (1 + cosine_similarity) / 2
# So cosine_similarity = 2 * score - 1
# For min cosine similarity of 0.7, the ES score threshold is (1 + 0.7) / 2 = 0.85
MIN_COSINE_SIMILARITY = 0.7  # Desired minimum cosine similarity
ES_SCORE_THRESHOLD = (1 + MIN_COSINE_SIMILARITY) / 2  # = 0.85 for ES KNN scores

MAX_TOPONYMS_PER_PLACE = 50  # Cap to prevent combinatorial explosion
PHASE1_SAMPLES_PER_BIN = 50000  # Target samples per script+lang bin for Phase 1
PHASE3_SAMPLES_PER_BIN = 50000  # Target samples per script+lang bin for Phase 3
MIN_BIN_SIZE = 100  # Minimum bin size to include (else drop with warning)
KNN_CANDIDATES = 100  # Number of candidates for KNN queries
ES_BATCH_SIZE = 500  # Batch size for ES bulk operations
ES_PARALLEL_WORKERS = 16  # Number of parallel threads for ES queries (network I/O bound)
MSEARCH_BATCH_SIZE = 100  # Number of queries per _msearch request


def es_score_to_cosine(score: float) -> float:
    """
    Convert ES KNN score to cosine similarity.

    ES KNN with cosine similarity returns: score = (1 + cosine) / 2
    So: cosine = 2 * score - 1

    This maps ES scores [0, 1] to cosine similarity [-1, 1]
    """
    return 2 * score - 1


def cosine_to_es_score(cosine: float) -> float:
    """
    Convert cosine similarity to ES KNN score.

    ES KNN with cosine similarity returns: score = (1 + cosine) / 2

    This maps cosine similarity [-1, 1] to ES scores [0, 1]
    """
    return (1 + cosine) / 2


def unpack_embedding(blob: bytes) -> Optional[List[float]]:
    """Unpack 192-dim embedding from binary blob."""
    if not blob:
        return None
    try:
        return list(struct.unpack(f'{len(blob)//4}f', blob))
    except:
        return None


def get_script_lang_key(script: str, lang: Optional[str]) -> str:
    """Create a canonical script+language key for binning."""
    lang_part = lang.split('-')[0] if lang else 'und'
    return f"{script}:{lang_part}"


class ESKNNHelper:
    """Helper class for ES KNN operations."""

    def __init__(self, es: Elasticsearch, index: str = "toponyms"):
        self.es = es
        self.index = index
        self._embedding_cache: Dict[str, List[float]] = {}

    def get_embedding(self, toponym_id: str) -> Optional[List[float]]:
        """Get embedding for a toponym from ES."""
        if toponym_id in self._embedding_cache:
            return self._embedding_cache[toponym_id]

        try:
            doc = self.es.get(index=self.index, id=toponym_id, _source=['panphon_embedding'])
            emb = doc['_source'].get('panphon_embedding')
            if emb:
                self._embedding_cache[toponym_id] = emb
            return emb
        except Exception:
            return None

    def find_similar_in_place(
        self,
        place_id: str,
        toponym_ids: List[str],
        min_similarity: float = MIN_COSINE_SIMILARITY,
    ) -> List[List[str]]:
        """
        Cluster toponyms within a place using ES KNN.

        For each toponym, find others in the same place with similarity >= threshold.
        Uses union-find to build clusters.

        Note on ES scores: ES KNN with cosine similarity returns scores in [0, 1]
        where score = (1 + cosine_similarity) / 2. We convert the min_similarity
        threshold accordingly.

        Returns:
            List of clusters, where each cluster is a list of toponym_ids
        """
        if len(toponym_ids) < 2:
            return [toponym_ids] if toponym_ids else []

        # Get embeddings for all toponyms in this place
        embeddings = {}
        for tid in toponym_ids:
            emb = self.get_embedding(tid)
            if emb:
                embeddings[tid] = emb

        if len(embeddings) < 2:
            return [list(embeddings.keys())] if embeddings else []

        # Convert cosine similarity threshold to ES score threshold
        # ES returns score = (1 + cosine) / 2, so threshold = (1 + min_sim) / 2
        es_score_threshold = cosine_to_es_score(min_similarity)

        # Use ES KNN to find similar pairs
        similar_pairs: Set[Tuple[str, str]] = set()

        for anchor_id, anchor_emb in embeddings.items():
            try:
                # KNN query filtered by attestations (same place)
                query = {
                    "size": min(len(toponym_ids), 50),
                    "knn": {
                        "field": "panphon_embedding",
                        "query_vector": anchor_emb,
                        "k": min(len(toponym_ids), 50),
                        "num_candidates": KNN_CANDIDATES,
                        "filter": {
                            "term": {"attestations": place_id}
                        }
                    },
                    "_source": False
                }

                results = self.es.search(index=self.index, body=query)

                for hit in results['hits']['hits']:
                    candidate_id = hit['_id']
                    es_score = hit['_score']

                    # ES KNN score = (1 + cosine) / 2, so compare against adjusted threshold
                    if candidate_id != anchor_id and es_score >= es_score_threshold:
                        pair = tuple(sorted([anchor_id, candidate_id]))
                        similar_pairs.add(pair)

            except Exception as e:
                logger.debug(f"KNN query failed for {anchor_id}: {e}")
                continue

        # Build clusters using union-find
        parent = {tid: tid for tid in embeddings.keys()}

        def find(x):
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]

        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        for id_a, id_b in similar_pairs:
            if id_a in parent and id_b in parent:
                union(id_a, id_b)

        # Group by root
        clusters_dict: Dict[str, List[str]] = defaultdict(list)
        for tid in embeddings.keys():
            clusters_dict[find(tid)].append(tid)

        return list(clusters_dict.values())

    def find_hard_negative(
        self,
        anchor_id: str,
        anchor_embedding: List[float],
        anchor_script: str,
        adjacency: Set[Tuple[str, str]],
        k: int = 20,
    ) -> Optional[str]:
        """
        Find a hard negative using ES KNN.

        Queries for toponyms with:
        - Same script as anchor
        - High embedding similarity (via KNN)
        - Different place (not adjacent)
        """
        try:
            # KNN query with script filter
            query = {
                "size": k,
                "knn": {
                    "field": "panphon_embedding",
                    "query_vector": anchor_embedding,
                    "k": k,
                    "num_candidates": KNN_CANDIDATES,
                    "filter": {
                        "term": {"script": anchor_script}
                    }
                },
                "_source": False
            }

            results = self.es.search(index=self.index, body=query)

            for hit in results['hits']['hits']:
                candidate_id = hit['_id']
                # Check not the anchor and not adjacent
                if candidate_id != anchor_id and (anchor_id, candidate_id) not in adjacency:
                    return candidate_id

            return None

        except Exception as e:
            logger.debug(f"KNN hard negative search failed: {e}")
            return None

    def batch_get_embeddings(self, toponym_ids: List[str]) -> Dict[str, List[float]]:
        """Get embeddings for multiple toponyms efficiently using mget."""
        result = {}

        # Filter out already cached
        to_fetch = [tid for tid in toponym_ids if tid not in self._embedding_cache]

        # Add cached ones to result
        for tid in toponym_ids:
            if tid in self._embedding_cache:
                result[tid] = self._embedding_cache[tid]

        if not to_fetch:
            return result

        # Batch fetch from ES
        try:
            docs = self.es.mget(
                index=self.index,
                body={"ids": to_fetch},
                _source=['panphon_embedding']
            )

            for doc in docs.get('docs', []):
                if doc.get('found') and '_source' in doc:
                    emb = doc['_source'].get('panphon_embedding')
                    if emb:
                        self._embedding_cache[doc['_id']] = emb
                        result[doc['_id']] = emb

        except Exception as e:
            logger.warning(f"Batch embedding fetch failed: {e}")

        return result

    def find_hard_negatives_batch(
        self,
        anchors: List[Dict],
        adjacency: Set[Tuple[str, str]],
        k: int = 20,
    ) -> List[Optional[str]]:
        """
        Find hard negatives for multiple anchors using ES _msearch.

        This reduces network round-trips by 50-100x compared to individual queries.

        Args:
            anchors: List of dicts with keys: 'anchor_id', 'embedding', 'script'
            adjacency: Set of adjacent (anchor, candidate) pairs to exclude
            k: Number of candidates to retrieve per anchor

        Returns:
            List of hard negative toponym_ids (or None if not found), same order as anchors
        """
        if not anchors:
            return []

        # Build _msearch body
        bodies = []
        for a in anchors:
            bodies.append({"index": self.index})
            bodies.append({
                "size": k,
                "knn": {
                    "field": "panphon_embedding",
                    "query_vector": a['embedding'],
                    "k": k,
                    "num_candidates": KNN_CANDIDATES,
                    "filter": {
                        "term": {"script": a['script']}
                    }
                },
                "_source": False
            })

        try:
            responses = self.es.msearch(body=bodies)['responses']
        except Exception as e:
            logger.warning(f"Batch hard negative search failed: {e}")
            return [None] * len(anchors)

        # Process responses
        results = []
        for i, response in enumerate(responses):
            anchor_id = anchors[i]['anchor_id']
            hard_neg = None

            if 'hits' in response and 'hits' in response['hits']:
                for hit in response['hits']['hits']:
                    candidate_id = hit['_id']
                    if candidate_id != anchor_id and (anchor_id, candidate_id) not in adjacency:
                        hard_neg = candidate_id
                        break

            results.append(hard_neg)

        return results


class TrainingDataGenerator:
    """Generates training data for all phases from DuckDB + ES."""

    def __init__(
        self,
        es: Elasticsearch,
        db_path: str,
        output_dir: Path,
        scratch_dir: Path,
        training_namespaces: List[str],
    ):
        self.es = es
        self.db_path = db_path
        self.output_dir = output_dir
        self.scratch_dir = scratch_dir
        self.training_namespaces = training_namespaces

        # ES KNN helper for similarity queries
        self.knn = ESKNNHelper(es, index="toponyms")

        # Connect to DuckDB
        self.conn = duckdb.connect(db_path, read_only=True)

        # Statistics
        self.stats = {
            'phase1': {'pairs': 0, 'triplets': 0, 'by_bin': {}},
            'phase2': {'samples': 0, 'by_bin': {}},
            'phase3': {'triplets': 0, 'by_bin': {}},
        }

    def generate_all(self):
        """Generate training data for all phases."""
        logger.info("=" * 60)
        logger.info("GENERATING TRAINING DATA FOR ALL PHASES")
        logger.info("=" * 60)

        # Step 1: Generate positive pairs from co-located toponyms
        logger.info("\n" + "=" * 60)
        logger.info("STEP 1: GENERATE POSITIVE PAIRS")
        logger.info("=" * 60)
        pairs_by_bin = self.generate_positive_pairs()

        # Step 2: Generate Phase 1 triplets (random negatives)
        logger.info("\n" + "=" * 60)
        logger.info("STEP 2: GENERATE PHASE 1 TRIPLETS")
        logger.info("=" * 60)
        self.generate_phase1_triplets(pairs_by_bin)

        # Step 3: Generate Phase 2 samples (all toponyms with embeddings)
        logger.info("\n" + "=" * 60)
        logger.info("STEP 3: GENERATE PHASE 2 SAMPLES")
        logger.info("=" * 60)
        self.generate_phase2_samples()

        # Step 4: Generate Phase 3 triplets (hard negatives from ES)
        logger.info("\n" + "=" * 60)
        logger.info("STEP 4: GENERATE PHASE 3 TRIPLETS")
        logger.info("=" * 60)
        self.generate_phase3_triplets(pairs_by_bin)

        # Save statistics
        stats_path = self.output_dir / 'training_stats.json'
        with open(stats_path, 'w') as f:
            json.dump(self.stats, f, indent=2)
        logger.info(f"Statistics saved to {stats_path}")

        return self.stats

    def generate_positive_pairs(self) -> Dict[str, List[Tuple]]:
        """
        Generate positive pairs from co-located toponyms using ES KNN clustering.

        For each place with multiple toponyms:
        1. Use ES KNN to find similar toponyms within the place (cosine similarity >= threshold)
        2. Build clusters using union-find on similar pairs
        3. Generate pairs only within clusters
        4. Store in bins by script+language pair

        This handles places with multiple phonetically-distinct name variants:
        - E.g., "London" (English), "Londres" (French), "Лондон" (Russian) might form
          one cluster, while "Lundúnir" (Icelandic) forms another if sufficiently different.

        Uses ThreadPoolExecutor to parallelize ES KNN queries across places.

        Returns:
            Dict mapping script+lang key to list of (toponym_id_a, toponym_id_b, similarity) tuples
        """
        pairs_by_bin: Dict[str, List[Tuple]] = defaultdict(list)
        seen_pairs: Set[Tuple[str, str]] = set()

        # Statistics
        cluster_stats = {
            'places_processed': 0,
            'places_with_clusters': 0,
            'total_clusters': 0,
            'singleton_clusters': 0,
            'multi_clusters': 0,  # Places with >1 cluster
            'cluster_sizes': Counter(),
        }

        # Query places with multiple toponyms in training namespaces
        # Get place_id -> list of (toponym_id, script, lang) from DuckDB
        query = '''
            SELECT 
                ta.place_id,
                t.toponym_id,
                t.script,
                t.lang
            FROM toponym_attestations ta
            JOIN toponyms t ON ta.toponym_id = t.toponym_id
            JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
            WHERE tn.namespace IN (''' + ','.join([f"'{ns}'" for ns in self.training_namespaces]) + ''')
              AND t.panphon_features IS NOT NULL
            ORDER BY ta.place_id
        '''

        logger.info("Querying toponyms with PanPhon embeddings from DuckDB...")
        results = self.conn.execute(query).fetchall()
        logger.info(f"Found {len(results):,} toponym attestations")

        # Group by place_id
        places: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        for row in results:
            place_id, toponym_id, script, lang = row
            places[place_id].append((toponym_id, script, lang))

        logger.info(f"Grouped into {len(places):,} places")

        # Filter to places with multiple toponyms and cap per place
        places_with_multiple = {}
        for p, t in places.items():
            if len(t) >= 2:
                if len(t) > MAX_TOPONYMS_PER_PLACE:
                    t = random.sample(t, MAX_TOPONYMS_PER_PLACE)
                places_with_multiple[p] = t

        logger.info(f"Places with ≥2 toponyms: {len(places_with_multiple):,}")

        # Helper function to process a single place (for parallel execution)
        def process_place(place_id: str, toponyms_info: List[Tuple]) -> Tuple[str, List, Dict]:
            """Process a single place and return clusters with metadata."""
            id_to_info = {t[0]: (t[1], t[2]) for t in toponyms_info}
            toponym_ids = list(id_to_info.keys())

            clusters = self.knn.find_similar_in_place(
                place_id=place_id,
                toponym_ids=toponym_ids,
                min_similarity=MIN_COSINE_SIMILARITY,
            )

            return place_id, clusters, id_to_info

        # Parallel ES KNN clustering using ThreadPoolExecutor
        logger.info(f"Running parallel ES KNN clustering with {ES_PARALLEL_WORKERS} workers...")
        total_pairs = 0

        with ThreadPoolExecutor(max_workers=ES_PARALLEL_WORKERS) as executor:
            # Submit all tasks
            futures = {
                executor.submit(process_place, pid, info): pid
                for pid, info in places_with_multiple.items()
            }

            # Process results as they complete
            iterator = as_completed(futures)
            iterator = tqdm(iterator, total=len(futures), desc="Parallel ES KNN clustering")

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

                for cluster in clusters:
                    cluster_stats['cluster_sizes'][len(cluster)] += 1

                    if len(cluster) < 2:
                        cluster_stats['singleton_clusters'] += 1
                        continue

                    # Generate pairs within this cluster
                    for i, id_a in enumerate(cluster):
                        for id_b in cluster[i+1:]:
                            script_a, lang_a = id_to_info[id_a]
                            script_b, lang_b = id_to_info[id_b]

                            # Create canonical pair key for deduplication
                            pair_key = tuple(sorted([id_a, id_b]))
                            if pair_key in seen_pairs:
                                continue
                            seen_pairs.add(pair_key)

                            # Determine bin key (use script+lang pair)
                            key_a = get_script_lang_key(script_a, lang_a)
                            key_b = get_script_lang_key(script_b, lang_b)
                            bin_key = tuple(sorted([key_a, key_b]))
                            bin_key_str = f"{bin_key[0]}|{bin_key[1]}"

                            # Similarity was already checked by ES KNN (>= threshold)
                            pairs_by_bin[bin_key_str].append((id_a, id_b, 0.0))
                            total_pairs += 1

        logger.info(f"Generated {total_pairs:,} unique positive pairs")
        logger.info(f"Distributed across {len(pairs_by_bin)} script+language bins")

        # Log clustering statistics
        logger.info("Clustering statistics:")
        logger.info(f"  Places processed: {cluster_stats['places_processed']:,}")
        logger.info(f"  Places with ≥2 toponyms: {cluster_stats['places_with_clusters']:,}")
        logger.info(f"  Total clusters formed: {cluster_stats['total_clusters']:,}")
        logger.info(f"  Places with multiple clusters: {cluster_stats['multi_clusters']:,}")
        logger.info(f"  Singleton clusters (no pairs): {cluster_stats['singleton_clusters']:,}")
        logger.info(f"  Cluster size distribution:")
        for size, count in sorted(cluster_stats['cluster_sizes'].items())[:10]:
            logger.info(f"    Size {size}: {count:,} clusters")

        # Log bin distribution
        bin_sizes = [(k, len(v)) for k, v in pairs_by_bin.items()]
        bin_sizes.sort(key=lambda x: -x[1])
        logger.info("Top 10 bins:")
        for bin_key, count in bin_sizes[:10]:
            logger.info(f"  {bin_key}: {count:,}")

        self.stats['phase1']['pairs'] = total_pairs
        self.stats['phase1']['by_bin'] = {k: len(v) for k, v in pairs_by_bin.items()}
        self.stats['phase1']['clustering'] = {
            'places_with_multiple_clusters': cluster_stats['multi_clusters'],
            'total_clusters': cluster_stats['total_clusters'],
            'singleton_clusters': cluster_stats['singleton_clusters'],
        }

        # Save pairs to Parquet
        pairs_dir = self.output_dir / 'pairs'
        pairs_dir.mkdir(exist_ok=True)

        all_pairs = []
        for bin_key, pairs in pairs_by_bin.items():
            for id_a, id_b, sim in pairs:
                all_pairs.append({
                    'anchor': id_a,
                    'positive': id_b,
                    'bin': bin_key,
                })

        if PYARROW_AVAILABLE and all_pairs:
            table = pa.Table.from_pylist(all_pairs)
            pq.write_table(table, pairs_dir / 'positive_pairs.parquet')
            logger.info(f"Saved pairs to {pairs_dir / 'positive_pairs.parquet'}")

        return pairs_by_bin

    def generate_phase1_triplets(self, pairs_by_bin: Dict[str, List[Tuple]]):
        """
        Generate Phase 1 triplets with random negatives.

        For each positive pair (anchor, positive), sample a random negative
        that is NOT in the same place (not adjacent).

        Balance across bins using round-robin sampling.
        """
        logger.info("Building adjacency set...")
        adjacency: Set[Tuple[str, str]] = set()
        for pairs in pairs_by_bin.values():
            for id_a, id_b, _ in pairs:
                adjacency.add((id_a, id_b))
                adjacency.add((id_b, id_a))

        logger.info(f"Adjacency set has {len(adjacency):,} edges")

        # Get all toponym IDs for negative sampling
        logger.info("Loading all toponym IDs for negative sampling...")
        all_ids = [row[0] for row in self.conn.execute(
            "SELECT toponym_id FROM toponyms WHERE panphon_features IS NOT NULL"
        ).fetchall()]
        logger.info(f"Loaded {len(all_ids):,} candidate negatives")

        # Sample from each bin with balancing
        triplets = []
        bin_keys = list(pairs_by_bin.keys())

        # Calculate samples per bin
        total_target = PHASE1_SAMPLES_PER_BIN * len(bin_keys)
        samples_per_bin = {k: min(len(v), PHASE1_SAMPLES_PER_BIN) for k, v in pairs_by_bin.items()}

        # Filter out bins that are too small
        valid_bins = {k: v for k, v in samples_per_bin.items() if v >= MIN_BIN_SIZE}
        dropped_bins = set(samples_per_bin.keys()) - set(valid_bins.keys())
        if dropped_bins:
            logger.warning(f"Dropping {len(dropped_bins)} bins with < {MIN_BIN_SIZE} samples")

        logger.info(f"Generating triplets from {len(valid_bins)} bins...")

        for bin_key in valid_bins:
            pairs = pairs_by_bin[bin_key]
            n_samples = samples_per_bin[bin_key]

            # Sample pairs (with replacement if needed)
            if len(pairs) >= n_samples:
                sampled_pairs = random.sample(pairs, n_samples)
            else:
                # Oversample small bins
                sampled_pairs = random.choices(pairs, k=n_samples)

            for anchor, positive, sim in sampled_pairs:
                # Find a negative that's not adjacent
                for _ in range(10):  # Max attempts
                    negative = random.choice(all_ids)
                    if (anchor, negative) not in adjacency and (positive, negative) not in adjacency:
                        triplets.append({
                            'anchor_id': anchor,
                            'positive_id': positive,
                            'negative_id': negative,
                            'bin': bin_key,
                        })
                        break

        logger.info(f"Generated {len(triplets):,} Phase 1 triplets")
        self.stats['phase1']['triplets'] = len(triplets)

        # Save to Parquet in directory structure expected by training
        triplets_dir = self.output_dir / 'triplets' / 'phase1'
        triplets_dir.mkdir(parents=True, exist_ok=True)

        if PYARROW_AVAILABLE and triplets:
            # Shuffle before saving
            random.shuffle(triplets)

            # Split into train/val
            val_size = int(len(triplets) * 0.1)
            val_triplets = triplets[:val_size]
            train_triplets = triplets[val_size:]

            train_table = pa.Table.from_pylist(train_triplets)
            val_table = pa.Table.from_pylist(val_triplets)

            pq.write_table(train_table, triplets_dir / 'train.parquet')
            pq.write_table(val_table, triplets_dir / 'val.parquet')

            logger.info(f"Saved {len(train_triplets):,} train, {len(val_triplets):,} val triplets")

    def generate_phase2_samples(self):
        """
        Generate Phase 2 samples: all toponyms with PanPhon embeddings.

        Phase 2 trains the Student to mimic Teacher outputs, so we need
        the full corpus of toponyms with their features.

        Note: Only toponyms with valid PanPhon features are included here.
        Toponyms without features (unsupported scripts/languages) cannot be
        used for Teacher-Student alignment because we can't compute a target
        embedding. However, the trained Student can still process these at
        inference time using character-level generalization.

        Exports data in the format expected by Phase2Dataset:
        - toponym_id, name, script, lang
        - char_ids (list of int), char_length (int)
        - features (list of float), feature_length (int)
        - split ('train'/'val'/'test'), epitran_supported (bool)
        """
        logger.info("Generating Phase 2 samples...")

        # Create output directory (training/ is expected by data_loading.py)
        training_dir = self.output_dir / 'training'
        training_dir.mkdir(exist_ok=True)

        # Load char vocabulary for char_ids conversion
        import json
        vocab_path = self.output_dir / 'vocab' / 'char_vocab.json'
        if not vocab_path.exists():
            logger.error(f"Char vocabulary not found at {vocab_path}")
            return

        with open(vocab_path) as f:
            char_vocab = json.load(f)
        char_to_id = char_vocab.get('char_to_id', {})
        unk_id = char_to_id.get('<UNK>', 1)
        logger.info(f"Loaded char vocabulary with {len(char_to_id)} characters")

        # Build namespace filter
        ns_filter = ','.join([f"'{ns}'" for ns in self.training_namespaces])

        # Get coverage statistics
        total_count = self.conn.execute(f'''
            SELECT COUNT(*) FROM toponyms t
            JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
            WHERE tn.namespace IN ({ns_filter})
        ''').fetchone()[0]

        with_features = self.conn.execute(f'''
            SELECT COUNT(*) FROM toponyms t
            JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
            WHERE tn.namespace IN ({ns_filter})
              AND t.panphon_features IS NOT NULL
        ''').fetchone()[0]

        without_features = total_count - with_features
        coverage_pct = (with_features / total_count * 100) if total_count > 0 else 0

        logger.info(f"Found {with_features:,} toponyms with PanPhon features")
        logger.info(f"  Total in training namespaces: {total_count:,}")
        logger.info(f"  Without features (excluded): {without_features:,}")
        logger.info(f"  PanPhon coverage: {coverage_pct:.1f}%")

        # Query all toponyms with features
        query = f'''
            SELECT DISTINCT
                t.toponym_id,
                t.name,
                t.script,
                t.lang,
                t.ipa,
                t.panphon_features
            FROM toponyms t
            JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
            WHERE tn.namespace IN ({ns_filter})
              AND t.panphon_features IS NOT NULL
        '''

        logger.info("Querying and transforming toponyms...")

        # Process in batches to manage memory
        samples_by_split = {'train': [], 'val': [], 'test': []}
        bin_stats: Counter = Counter()

        # Use cursor for streaming large results
        cursor = self.conn.execute(query)
        batch_size = 100000
        processed = 0

        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            for row in rows:
                toponym_id, name, script, lang, ipa, panphon_blob = row

                # Unpack panphon_features from binary blob
                features = unpack_embedding(panphon_blob)
                if not features:
                    continue

                # Convert name to char_ids
                char_ids = [char_to_id.get(c, unk_id) for c in name]

                # Determine split using stable hash (zlib.crc32 is deterministic across runs)
                # crc32 % 10: 0 = test, 1 = val, 2-9 = train (80/10/10 split)
                hash_val = zlib.crc32(toponym_id.encode('utf-8')) % 10
                if hash_val == 0:
                    split = 'test'
                elif hash_val == 1:
                    split = 'val'
                else:
                    split = 'train'

                # Build sample dict in format expected by training
                # Note: char_length is intentionally omitted - it's recomputed during collation
                sample = {
                    'toponym_id': toponym_id,
                    'name': name,
                    'script': script,
                    'lang': lang or '',
                    'char_ids': char_ids,
                    'features': features,
                    'feature_length': len(features) // 24,  # 24 = panphon feature dim
                    'split': split,
                    'epitran_supported': True,  # Only exporting those with features
                }

                samples_by_split[split].append(sample)

                # Track bin stats
                bin_key = get_script_lang_key(script, lang)
                bin_stats[bin_key] += 1

            processed += len(rows)
            if processed % 500000 == 0:
                logger.info(f"  Processed {processed:,} toponyms...")

        logger.info(f"Processed {processed:,} total toponyms")

        # Update stats
        total_samples = sum(len(s) for s in samples_by_split.values())
        self.stats['phase2']['samples'] = total_samples
        self.stats['phase2']['by_bin'] = dict(bin_stats.most_common(50))
        logger.info(f"Distribution across {len(bin_stats)} script+language bins")

        # Save to Parquet files with hive partitioning by split
        if PYARROW_AVAILABLE:
            for split_name, samples in samples_by_split.items():
                if not samples:
                    continue

                # Create split directory (hive partitioning: split=train/, split=val/, etc.)
                split_dir = training_dir / f'split={split_name}'
                split_dir.mkdir(exist_ok=True)

                table = pa.Table.from_pylist(samples)
                pq.write_table(table, split_dir / 'data.parquet')
                logger.info(f"Saved {len(samples):,} {split_name} samples")

        logger.info(f"Phase 2 export complete: {total_samples:,} total samples")


    def generate_phase3_triplets(self, pairs_by_bin: Dict[str, List[Tuple]]):
        """
        Generate Phase 3 triplets with hard negatives from ES KNN.

        Hard negatives are toponyms that:
        - Have the same script as the anchor
        - Are phonetically similar (high PanPhon cosine similarity via KNN)
        - But refer to DIFFERENT places (not in adjacency set)

        This teaches the model to discriminate between similar-sounding
        but geographically distinct names.

        Uses batched _msearch for 50-100x speedup over individual queries.
        """
        logger.info("Building adjacency set for Phase 3...")
        adjacency: Set[Tuple[str, str]] = set()
        for pairs in pairs_by_bin.values():
            for id_a, id_b, _ in pairs:
                adjacency.add((id_a, id_b))
                adjacency.add((id_b, id_a))

        logger.info(f"Adjacency set has {len(adjacency):,} edges")

        # Get anchor info for ES KNN queries
        logger.info("Loading anchor info for hard negative mining...")
        anchor_info = {}  # toponym_id -> (script, lang)

        results = self.conn.execute('''
            SELECT toponym_id, script, lang
            FROM toponyms
            WHERE panphon_features IS NOT NULL
        ''').fetchall()

        for toponym_id, script, lang in results:
            anchor_info[toponym_id] = (script, lang)

        logger.info(f"Loaded info for {len(anchor_info):,} toponyms")

        # Sample from bins with balancing
        valid_bins = {k: v for k, v in pairs_by_bin.items()
                      if len(v) >= MIN_BIN_SIZE}

        logger.info(f"Mining hard negatives from {len(valid_bins)} bins using batched ES KNN...")

        # Collect all (anchor, positive) pairs we need to process
        all_pairs_to_process = []
        for bin_key, pairs in valid_bins.items():
            n_samples = min(len(pairs), PHASE3_SAMPLES_PER_BIN)
            sampled_pairs = random.sample(pairs, n_samples) if len(pairs) >= n_samples else pairs
            for anchor, positive, _ in sampled_pairs:
                if anchor in anchor_info:
                    all_pairs_to_process.append((anchor, positive, bin_key))

        logger.info(f"Processing {len(all_pairs_to_process):,} pairs for hard negatives...")

        # Batch fetch embeddings from ES (using mget)
        all_anchors = list(set(p[0] for p in all_pairs_to_process))
        logger.info(f"Pre-fetching embeddings for {len(all_anchors):,} anchors...")

        # Fetch in batches to avoid memory issues
        anchor_embeddings = {}
        for i in range(0, len(all_anchors), 5000):
            batch = all_anchors[i:i+5000]
            batch_embs = self.knn.batch_get_embeddings(batch)
            anchor_embeddings.update(batch_embs)
            if (i + 5000) % 50000 < 5000:
                logger.info(f"  Fetched {min(i + 5000, len(all_anchors)):,} / {len(all_anchors):,} embeddings")

        logger.info(f"Fetched {len(anchor_embeddings):,} embeddings total")

        # Process in batches using _msearch for hard negative mining
        triplets = []
        failed_lookups = 0

        # Build batches for _msearch
        batches = []
        current_batch = []

        for anchor, positive, bin_key in all_pairs_to_process:
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
            })

            if len(current_batch) >= MSEARCH_BATCH_SIZE:
                batches.append(current_batch)
                current_batch = []

        if current_batch:
            batches.append(current_batch)

        logger.info(f"Processing {len(batches)} batches of {MSEARCH_BATCH_SIZE} queries each...")

        # Process batches with progress bar
        iterator = batches
        iterator = tqdm(iterator, desc="Batched hard negative mining (_msearch)")

        for batch in iterator:
            # Use batched _msearch
            hard_negs = self.knn.find_hard_negatives_batch(
                anchors=batch,
                adjacency=adjacency,
                k=20,
            )

            # Create triplets from results
            for item, hard_neg in zip(batch, hard_negs):
                if hard_neg:
                    triplets.append({
                        'anchor_id': item['anchor_id'],
                        'positive_id': item['positive_id'],
                        'negative_id': hard_neg,
                        'negative_type': 'hard',  # Required by training code
                        'bin': item['bin'],
                    })

        if failed_lookups > 0:
            logger.warning(f"Failed to find embedding for {failed_lookups:,} anchors")

        logger.info(f"Generated {len(triplets):,} Phase 3 triplets")
        self.stats['phase3']['triplets'] = len(triplets)

        # Save to Parquet in directory structure expected by training
        triplets_dir = self.output_dir / 'triplets' / 'phase3'
        triplets_dir.mkdir(parents=True, exist_ok=True)

        if PYARROW_AVAILABLE and triplets:
            random.shuffle(triplets)

            val_size = int(len(triplets) * 0.1)
            val_triplets = triplets[:val_size]
            train_triplets = triplets[val_size:]

            train_table = pa.Table.from_pylist(train_triplets)
            val_table = pa.Table.from_pylist(val_triplets)

            pq.write_table(train_table, triplets_dir / 'train.parquet')
            pq.write_table(val_table, triplets_dir / 'val.parquet')

            logger.info(f"Saved {len(train_triplets):,} train, {len(val_triplets):,} val triplets")



def main():
    parser = argparse.ArgumentParser(
        description='Generate training data for Symphonym v4'
    )
    parser.add_argument('--es-host', default='http://localhost:9200',
                        help='Elasticsearch host URL')
    parser.add_argument('--db-path', required=True,
                        help='Path to DuckDB database')
    parser.add_argument('--output-dir', required=True,
                        help='Output directory for training data')
    parser.add_argument('--scratch-dir', default='/tmp',
                        help='Scratch directory for temporary files')
    parser.add_argument('--training-namespaces', nargs='+',
                        default=['gn', 'wd', 'tgn'],
                        help='Namespaces to include in training')

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    scratch_dir = Path(args.scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    # Connect to ES
    es = Elasticsearch(args.es_host)
    if not es.ping():
        logger.error(f"Cannot connect to Elasticsearch at {args.es_host}")
        sys.exit(1)

    logger.info(f"Connected to Elasticsearch at {args.es_host}")

    # Generate training data
    generator = TrainingDataGenerator(
        es=es,
        db_path=args.db_path,
        output_dir=output_dir,
        scratch_dir=scratch_dir,
        training_namespaces=args.training_namespaces,
    )

    stats = generator.generate_all()

    logger.info("\n" + "=" * 60)
    logger.info("TRAINING DATA GENERATION COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Phase 1: {stats['phase1']['triplets']:,} triplets")
    logger.info(f"Phase 2: {stats['phase2']['samples']:,} samples")
    logger.info(f"Phase 3: {stats['phase3']['triplets']:,} triplets")


if __name__ == '__main__':
    main()

