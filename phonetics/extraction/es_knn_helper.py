#!/usr/bin/env python3
"""
Elasticsearch KNN helper with caching and retry support.
"""

import random
import zlib
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import hdbscan
import numpy as np
from sklearn.metrics.pairwise import cosine_distances

from .constants import (
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

        embeddings = {}
        for tid in toponym_ids:
            emb = self.get_embedding(tid)
            if emb:
                embeddings[tid] = emb

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
        adjacency: Set[Tuple[str, str]],
        k: int = 20,
        stochastic: bool = True,
    ) -> List[Optional[str]]:
        """Find hard negatives for multiple anchors using ES _msearch."""
        if not anchors:
            return []

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
                    "filter": {"term": {"script": a['script']}}
                },
                "_source": False
            })

        def _msearch():
            return self.es.msearch(body=bodies)['responses']

        responses = es_retry_with_backoff(_msearch)
        self._record_request(responses is not None)

        if responses is None:
            return [None] * len(anchors)

        results = []
        for i, response in enumerate(responses):
            anchor_id = anchors[i]['anchor_id']
            hard_neg = None

            if 'hits' in response and 'hits' in response['hits']:
                valid_candidates = []
                for hit in response['hits']['hits']:
                    candidate_id = hit['_id']
                    if candidate_id != anchor_id and (anchor_id, candidate_id) not in adjacency:
                        valid_candidates.append(candidate_id)

                if valid_candidates:
                    if stochastic and len(valid_candidates) > 1:
                        sample_idx = anchors[i].get('sample_idx', 0)
                        seed = RANDOM_SEED + (zlib.crc32(anchor_id.encode('utf-8')) & 0xffffffff) + sample_idx
                        rng = random.Random(seed)
                        hard_neg = rng.choice(valid_candidates)
                    else:
                        hard_neg = valid_candidates[0]

            results.append(hard_neg)

        return results