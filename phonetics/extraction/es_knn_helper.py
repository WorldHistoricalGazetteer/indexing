#!/usr/bin/env python3
"""
Elasticsearch KNN helper with caching and retry support.

Part of phonetics.extraction package.
"""

import random
import zlib
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import hdbscan
import numpy as np
from sklearn.metrics.pairwise import cosine_distances

from phonetics.extraction.constants import (
    ES_FAILURE_THRESHOLD, KNN_CANDIDATES, PAIR_SIMILARITY_THRESHOLD,
    RANDOM_SEED, es_retry_with_backoff, logger
)


class ESKNNHelper:
    """Helper class for ES KNN operations with retry/throttle support."""

    MAX_CACHE_SIZE = 100000

    def __init__(self, es, index: str = "toponyms"):
        self.es = es
        self.index = index
        self._embedding_cache: Dict[str, List[float]] = {}
        self._cache_order: List[str] = []
        self._total_requests = 0
        self._failed_requests = 0

    def _record_request(self, success: bool):
        self._total_requests += 1
        if not success:
            self._failed_requests += 1

    def get_failure_rate(self) -> float:
        if self._total_requests == 0:
            return 0.0
        return self._failed_requests / self._total_requests

    def check_failure_threshold(self):
        if self._total_requests > 100:
            rate = self.get_failure_rate()
            if rate > ES_FAILURE_THRESHOLD:
                raise RuntimeError(
                    f"ES failure rate ({rate:.1%}) exceeds threshold ({ES_FAILURE_THRESHOLD:.0%}). "
                    f"({self._failed_requests}/{self._total_requests} requests failed)"
                )

    def reset_failure_tracking(self):
        self._total_requests = 0
        self._failed_requests = 0

    def _cache_embedding(self, toponym_id: str, embedding: List[float]):
        if toponym_id in self._embedding_cache:
            return
        while len(self._embedding_cache) >= self.MAX_CACHE_SIZE:
            oldest = self._cache_order.pop(0)
            self._embedding_cache.pop(oldest, None)
        self._embedding_cache[toponym_id] = embedding
        self._cache_order.append(toponym_id)

    def clear_cache(self):
        self._embedding_cache.clear()
        self._cache_order.clear()

    def get_embedding(self, toponym_id: str) -> Optional[List[float]]:
        if toponym_id in self._embedding_cache:
            return self._embedding_cache[toponym_id]

        def _fetch():
            doc = self.es.get(index=self.index, id=toponym_id, _source=['panphon_embedding'])
            return doc['_source'].get('panphon_embedding')

        emb = es_retry_with_backoff(_fetch)
        self._record_request(emb is not None)

        if emb:
            self._cache_embedding(toponym_id, emb)
        return emb

    def find_similar_in_place(
            self,
            place_id: str,
            toponym_ids: List[str],
    ) -> List[List[str]]:
        """Cluster toponyms within a place using HDBSCAN."""
        n = len(toponym_ids)

        if n == 0:
            return []
        if n == 1:
            return [toponym_ids]

        # Batch fetch all embeddings at once (more efficient than individual calls)
        embeddings = self.batch_get_embeddings(toponym_ids)

        ids = list(embeddings.keys())
        n_with_emb = len(ids)

        if n_with_emb == 0:
            return []
        if n_with_emb == 1:
            return [ids]

        # Edge case: exactly 2 toponyms
        if n_with_emb == 2:
            vec1 = np.array(embeddings[ids[0]])
            vec2 = np.array(embeddings[ids[1]])
            norm1, norm2 = np.linalg.norm(vec1), np.linalg.norm(vec2)
            if norm1 > 0 and norm2 > 0:
                cos_sim = np.dot(vec1, vec2) / (norm1 * norm2)
                if cos_sim >= PAIR_SIMILARITY_THRESHOLD:
                    return [ids]
            return [[ids[0]], [ids[1]]]

        # Main case: ≥3 toponyms - use HDBSCAN
        vectors = np.array([embeddings[tid] for tid in ids])

        try:
            distance_matrix = cosine_distances(vectors)
            clusterer = hdbscan.HDBSCAN(
                min_cluster_size=2,
                min_samples=2,
                metric='precomputed',
                cluster_selection_epsilon=0.2,
                allow_single_cluster=True
            )
            labels = clusterer.fit_predict(distance_matrix)
        except Exception as e:
            logger.warning(f"HDBSCAN failed for place {place_id}: {e}")
            return [ids]

        clusters_dict: Dict[int, List[str]] = defaultdict(list)
        noise_points = []

        for tid, label in zip(ids, labels):
            if label >= 0:
                clusters_dict[label].append(tid)
            else:
                noise_points.append(tid)

        result = list(clusters_dict.values())
        for tid in noise_points:
            result.append([tid])

        return result

    def batch_get_embeddings(self, toponym_ids: List[str]) -> Dict[str, List[float]]:
        """Get embeddings for multiple toponyms efficiently using mget."""
        result = {}
        to_fetch = [tid for tid in toponym_ids if tid not in self._embedding_cache]

        for tid in toponym_ids:
            if tid in self._embedding_cache:
                result[tid] = self._embedding_cache[tid]

        if not to_fetch:
            return result

        def _mget():
            return self.es.mget(
                index=self.index,
                body={"ids": to_fetch},
                _source=['panphon_embedding']
            )

        docs = es_retry_with_backoff(_mget)
        self._record_request(docs is not None)

        if docs:
            for doc in docs.get('docs', []):
                if doc.get('found') and '_source' in doc:
                    emb = doc['_source'].get('panphon_embedding')
                    if emb:
                        self._cache_embedding(doc['_id'], emb)
                        result[doc['_id']] = emb

        return result

    def find_hard_negatives_batch(
            self,
            anchors: List[Dict],
            adjacency: Set[Tuple[str, str]] = None,
            attestation_map: Dict[str, Set[str]] = None,
            k: int = 20,
            stochastic: bool = True,
            return_lists: bool = True,
    ) -> List[Optional[str]] | List[List[str]]:
        """
        Find hard negatives for multiple anchors using ES _msearch.

        A valid negative must:
        1. Not be the anchor itself
        2. Not be in the adjacency set (not a known positive pair) - OPTIONAL
        3. Share NO attestations with the anchor (disjoint place sets) - OPTIONAL

        Args:
            anchors: List of anchor dicts with 'anchor_id', 'embedding', 'script', etc.
            adjacency: OPTIONAL Set of (toponym_id, toponym_id) positive pairs (for backward compat)
            attestation_map: OPTIONAL Dict mapping toponym_id -> Set of attestation strings
            k: Number of candidates to retrieve from KNN
            stochastic: If True, randomly select from valid candidates (only if return_lists=False)
            return_lists: If True, return list of candidate lists; if False, return single selected negatives

        Returns:
            If return_lists=True: List of candidate ID lists (generator handles filtering/selection)
            If return_lists=False: List of negative toponym_ids (or None if no valid negative found)
        """
        if not anchors:
            return []

        # Request more candidates since some may be filtered out by attestation check
        fetch_k = min(k * 3, 100)

        bodies = []
        for a in anchors:
            bodies.append({"index": self.index})
            bodies.append({
                "size": fetch_k,
                "knn": {
                    "field": "panphon_embedding",
                    "query_vector": a['embedding'],
                    "k": fetch_k,
                    "num_candidates": KNN_CANDIDATES,
                    "filter": {"term": {"script": a['script']}}
                },
                "_source": ["attestations"]
            })

        def _msearch():
            return self.es.msearch(body=bodies)['responses']

        responses = es_retry_with_backoff(_msearch)
        self._record_request(responses is not None)

        if responses is None:
            return [None] * len(anchors) if not return_lists else [[] for _ in anchors]

        results = []
        for i, response in enumerate(responses):
            anchor_id = anchors[i]['anchor_id']

            if 'hits' in response and 'hits' in response['hits']:
                # Extract candidate IDs from ES hits
                candidates = [hit['_id'] for hit in response['hits']['hits']]

                if return_lists:
                    # Return raw candidate list for generator to filter
                    results.append(candidates)
                else:
                    # Legacy behavior: filter and select here
                    anchor_attestations = attestation_map.get(anchor_id, set()) if attestation_map else set()
                    valid_candidates = []

                    for hit in response['hits']['hits']:
                        candidate_id = hit['_id']

                        # Skip if same as anchor
                        if candidate_id == anchor_id:
                            continue

                        # Skip if in adjacency (known positive pair)
                        if adjacency and (anchor_id, candidate_id) in adjacency:
                            continue

                        # Skip if shares any attestations with anchor
                        if attestation_map:
                            candidate_attestations = set(hit.get('_source', {}).get('attestations', []))
                            if not anchor_attestations.isdisjoint(candidate_attestations):
                                continue

                        valid_candidates.append(candidate_id)

                    hard_neg = None
                    if valid_candidates:
                        if stochastic and len(valid_candidates) > 1:
                            sample_idx = anchors[i].get('sample_idx', 0)
                            seed = RANDOM_SEED + (zlib.crc32(anchor_id.encode('utf-8')) & 0xffffffff) + sample_idx
                            rng = random.Random(seed)
                            hard_neg = rng.choice(valid_candidates)
                        else:
                            hard_neg = valid_candidates[0]

                    results.append(hard_neg)
            else:
                results.append([] if return_lists else None)

        return results

    def find_hard_negatives_batch_with_attestations(
            self,
            anchors: List[Dict],
            k: int = 20,
    ) -> List[List[Dict]]:
        """
        Find hard negative candidates for multiple anchors, returning raw hits with attestations.

        This high-performance variant returns the raw ES hit list (with _id and attestations)
        so the caller can do filtering with O(1) lookups and reservoir sampling.

        Args:
            anchors: List of anchor dicts with 'anchor_id', 'embedding', 'script', etc.
            k: Number of candidates to retrieve from KNN

        Returns:
            List of hit lists, where each hit is: {'_id': str, '_source': {'attestations': [...]}}
        """
        if not anchors:
            return []

        # Request more candidates since some will be filtered out
        fetch_k = min(k * 3, 100)

        bodies = []
        for a in anchors:
            bodies.append({"index": self.index})
            bodies.append({
                "size": fetch_k,
                "knn": {
                    "field": "panphon_embedding",
                    "query_vector": a['embedding'],
                    "k": fetch_k,
                    "num_candidates": KNN_CANDIDATES,
                    "filter": {"term": {"script": a['script']}}
                },
                "_source": ["attestations"]
            })

        def _msearch():
            return self.es.msearch(body=bodies)['responses']

        responses = es_retry_with_backoff(_msearch)
        self._record_request(responses is not None)

        if responses is None:
            return [[] for _ in anchors]

        results = []
        for response in responses:
            if 'hits' in response and 'hits' in response['hits']:
                # Return raw hits with _id and _source
                results.append(response['hits']['hits'])
            else:
                results.append([])

        return results