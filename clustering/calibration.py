# clustering/calibration.py
"""
Automated threshold calibration for Phase 3 using authority hard links
as ground-truth positives and random cross-namespace pairs as negatives.

Computes the five evidence signals (cosine similarity, spatial distance,
ccode overlap, type match, toponym exact count) for both sets, then fits
optimal thresholds and composite score weights via logistic regression.

Called between Phase 2 and Phase 3 in the runner pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from dataclasses import asdict

import numpy as np
from elasticsearch import AsyncElasticsearch
from tqdm import tqdm

from .config import ClusterConfig, ScoringConfig, KNOWN_ES_NAMESPACES
from .es_client import scroll_index, count_query
from .schemas import PairwiseDoc

logger = logging.getLogger("clustering.calibration")

# ── Constants ────────────────────────────────────────────────────────────────

# How many positive / negative pairs to sample (configurable via config)
DEFAULT_SAMPLE_SIZE = 20_000

# How many toponym lookups to pack into a single _msearch call
_MSEARCH_BATCH = 50

# Minimum positive pairs required for reliable calibration
_MIN_POSITIVES = 500


# ── Helpers ──────────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in kilometres."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _cosine_sim_byte(a: list[int], b: list[int]) -> float:
    """Cosine similarity between two byte-quantised embedding vectors."""
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    dot = np.dot(va, vb)
    norm = float(np.linalg.norm(va)) * float(np.linalg.norm(vb))
    if norm == 0:
        return 0.0
    return float(dot / norm)


# ── Place data fetching ─────────────────────────────────────────────────────

async def _fetch_place_data_batch(
    client: AsyncElasticsearch,
    pids: list[str],
    places_index: str,
) -> dict[str, dict]:
    """Fetch ccodes, repr_point, types for a batch of place_ids."""
    if not pids:
        return {}
    cache: dict[str, dict] = {}
    for start in range(0, len(pids), 2000):
        chunk = pids[start:start + 2000]
        try:
            resp = await client.search(
                index=places_index,
                body={
                    "size": len(chunk),
                    "query": {"terms": {"place_id": chunk}},
                    "_source": ["place_id", "namespace", "ccodes",
                                "geometries.repr_point", "types.identifier"],
                },
            )
            for hit in resp["hits"]["hits"]:
                src = hit["_source"]
                pid = src.get("place_id", "")
                repr_point = None
                for geom in src.get("geometries", []):
                    rp = geom.get("repr_point")
                    if rp:
                        if isinstance(rp, dict):
                            repr_point = (rp.get("lat", 0), rp.get("lon", 0))
                        elif isinstance(rp, (list, tuple)) and len(rp) >= 2:
                            repr_point = (rp[1], rp[0])
                        break
                ccodes = set(src.get("ccodes", []))
                types_ = set()
                for t in src.get("types", []):
                    tid = t.get("identifier", "")
                    if tid:
                        types_.add(tid)
                cache[pid] = {
                    "namespace": src.get("namespace", ""),
                    "ccodes": ccodes,
                    "repr_point": repr_point,
                    "types": types_,
                }
        except Exception as e:
            logger.warning("Place data fetch failed: %s", e)
        if start + 2000 < len(pids):
            await asyncio.sleep(0.05)
    return cache


# ── Toponym embedding fetching ──────────────────────────────────────────────

async def _fetch_embeddings_for_place(
    client: AsyncElasticsearch,
    place_id: str,
    toponyms_index: str,
) -> list[list[int]]:
    """Return all toponym embeddings that attest the given place_id."""
    try:
        resp = await client.search(
            index=toponyms_index,
            body={
                "size": 50,  # most places have <50 toponyms
                "query": {"term": {"attestations": place_id}},
                "_source": ["embedding"],
            },
        )
        embeddings = []
        for hit in resp["hits"]["hits"]:
            emb = hit["_source"].get("embedding")
            if emb:
                embeddings.append(emb)
        return embeddings
    except Exception as e:
        logger.debug("Embedding fetch failed for %s: %s", place_id, e)
        return []


async def _fetch_embeddings_batch(
    client: AsyncElasticsearch,
    place_ids: list[str],
    toponyms_index: str,
) -> dict[str, list[list[int]]]:
    """Fetch toponym embeddings for multiple place_ids via _msearch."""
    if not place_ids:
        return {}

    result: dict[str, list[list[int]]] = {}

    for start in range(0, len(place_ids), _MSEARCH_BATCH):
        chunk = place_ids[start:start + _MSEARCH_BATCH]
        body_lines: list[dict] = []
        for pid in chunk:
            body_lines.append({"index": toponyms_index})
            body_lines.append({
                "size": 50,
                "query": {"term": {"attestations": pid}},
                "_source": ["embedding"],
            })

        try:
            resp = await client.msearch(body=body_lines)
            for i, sub_resp in enumerate(resp.get("responses", [])):
                pid = chunk[i]
                embeddings = []
                for hit in sub_resp.get("hits", {}).get("hits", []):
                    emb = hit["_source"].get("embedding")
                    if emb:
                        embeddings.append(emb)
                result[pid] = embeddings
        except Exception as e:
            logger.warning("_msearch embedding fetch failed: %s", e)
            for pid in chunk:
                result.setdefault(pid, [])

        if start + _MSEARCH_BATCH < len(place_ids):
            await asyncio.sleep(0.05)

    return result


# ── Signal computation ──────────────────────────────────────────────────────

def _compute_signals_for_pair(
    emb_a: list[list[int]],
    emb_b: list[list[int]],
    data_a: dict | None,
    data_b: dict | None,
) -> dict | None:
    """
    Compute the 5 evidence signals for a single pair.

    Returns None if insufficient data (no embeddings on either side,
    or missing place data).
    """
    if not emb_a or not emb_b:
        return None
    if not data_a or not data_b:
        return None

    # Best cosine similarity across toponym pairs
    best_sim = 0.0
    for ea in emb_a:
        for eb in emb_b:
            sim = _cosine_sim_byte(ea, eb)
            if sim > best_sim:
                best_sim = sim

    # Spatial distance
    rp_a = data_a.get("repr_point")
    rp_b = data_b.get("repr_point")
    if rp_a and rp_b:
        distance_km = _haversine_km(rp_a[0], rp_a[1], rp_b[0], rp_b[1])
    else:
        distance_km = 0.0

    # Ccode overlap
    ccodes_a = data_a.get("ccodes", set())
    ccodes_b = data_b.get("ccodes", set())
    ccode_overlap = len(ccodes_a & ccodes_b) if ccodes_a and ccodes_b else 0

    # Type match
    types_a = data_a.get("types", set())
    types_b = data_b.get("types", set())
    type_match = bool(types_a & types_b) if types_a and types_b else False

    return {
        "cosine_sim": best_sim,
        "distance_km": distance_km,
        "ccode_overlap": ccode_overlap,
        "type_match": type_match,
        "toponym_exact_count": 0,  # filled separately for positives
    }


# ── Pair loading ─────────────────────────────────────────────────────────────

async def _load_positive_pairs(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    max_pairs: int,
) -> list[tuple[str, str]]:
    """
    Load authority hard-link pairs from the clusters index.
    Returns a randomly sampled list of (place_id_a, place_id_b) tuples.
    """
    query = {
        "bool": {
            "filter": [
                {"term": {"doc_type": "pairwise"}},
                {"term": {"link_class": "authority_sameAs"}},
            ]
        }
    }
    total = await count_query(client, cfg.clusters_index, query)
    if total == 0:
        return []

    logger.info("Calibration: %d authority hard-link pairs available", total)

    # If there are fewer than max_pairs, take them all; otherwise
    # use reservoir sampling via a random scroll
    pairs: list[tuple[str, str]] = []
    async for doc in scroll_index(
        client,
        index=cfg.clusters_index,
        query=query,
        source_fields=["place_id_a", "place_id_b"],
    ):
        pairs.append((doc["place_id_a"], doc["place_id_b"]))

    if len(pairs) > max_pairs:
        pairs = random.sample(pairs, max_pairs)

    return pairs


async def _sample_negative_pairs(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    positive_set: set[str],
    n: int,
) -> list[tuple[str, str]]:
    """
    Sample random cross-namespace pairs that are not in the positive set.

    Uses random sampling from the places index: draw random place_ids,
    group by namespace, then form cross-namespace pairs.
    """
    # Collect a pool of random place_ids using random_score
    pool_size = max(n * 4, 10_000)  # oversample to allow pairing
    try:
        resp = await client.search(
            index=cfg.places_index,
            body={
                "size": min(pool_size, 10_000),
                "query": {
                    "function_score": {
                        "query": {"match_all": {}},
                        "random_score": {},
                    }
                },
                "_source": ["place_id", "namespace"],
            },
        )
    except Exception as e:
        logger.warning("Negative sampling failed: %s", e)
        return []

    by_ns: dict[str, list[str]] = {}
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        ns = src.get("namespace", "")
        pid = src.get("place_id", "")
        if ns in KNOWN_ES_NAMESPACES and pid:
            by_ns.setdefault(ns, []).append(pid)

    # Form cross-namespace pairs
    namespaces = list(by_ns.keys())
    negatives: list[tuple[str, str]] = []
    attempts = 0
    max_attempts = n * 20

    while len(negatives) < n and attempts < max_attempts:
        attempts += 1
        if len(namespaces) < 2:
            break
        ns_a, ns_b = random.sample(namespaces, 2)
        if not by_ns.get(ns_a) or not by_ns.get(ns_b):
            continue
        pid_a = random.choice(by_ns[ns_a])
        pid_b = random.choice(by_ns[ns_b])
        pair_key = PairwiseDoc.make_id(*PairwiseDoc.canonical_pair(pid_a, pid_b))
        if pair_key not in positive_set:
            negatives.append(PairwiseDoc.canonical_pair(pid_a, pid_b))

    return negatives


# ── Signal computation for a batch of pairs ─────────────────────────────────

async def _compute_signals_batch(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    pairs: list[tuple[str, str]],
    desc: str,
) -> list[dict]:
    """
    Compute signals for a batch of (pid_a, pid_b) pairs.
    Returns list of signal dicts (one per pair with valid data).
    """
    # Collect all unique place_ids
    all_pids = set()
    for a, b in pairs:
        all_pids.add(a)
        all_pids.add(b)
    pid_list = sorted(all_pids)

    # Fetch place data and embeddings in parallel
    place_data = await _fetch_place_data_batch(
        client, pid_list, cfg.places_index
    )
    embeddings = await _fetch_embeddings_batch(
        client, pid_list, cfg.toponyms_index
    )

    # Compute signals for each pair
    signals = []
    bar = tqdm(pairs, desc=desc, unit="pair", mininterval=2.0)
    for pid_a, pid_b in bar:
        sig = _compute_signals_for_pair(
            embeddings.get(pid_a, []),
            embeddings.get(pid_b, []),
            place_data.get(pid_a),
            place_data.get(pid_b),
        )
        if sig is not None:
            signals.append(sig)
    bar.close()
    return signals


# ── Threshold fitting ────────────────────────────────────────────────────────

def _fit_thresholds(
    pos_signals: list[dict],
    neg_signals: list[dict],
    old_cfg: ScoringConfig,
) -> ScoringConfig:
    """
    Fit optimal thresholds and weights from labeled signal data.

    Returns a new ScoringConfig with calibrated values.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_curve, f1_score

    # Build feature matrix: [cosine_sim, 1/(1+dist/10), type_match, ccode_overlap/2]
    # (same normalisation as composite_score in scoring.py)
    def _to_features(sig: dict) -> list[float]:
        return [
            sig["cosine_sim"],
            1.0 / (1.0 + sig["distance_km"] / 10.0),
            1.0 if sig["type_match"] else 0.0,
            min(1.0, sig["ccode_overlap"] / 2.0),
            min(1.0, math.log1p(sig["toponym_exact_count"]) / math.log1p(5)),
        ]

    X_pos = [_to_features(s) for s in pos_signals]
    X_neg = [_to_features(s) for s in neg_signals]
    X = np.array(X_pos + X_neg, dtype=np.float64)
    y = np.array([1] * len(X_pos) + [0] * len(X_neg), dtype=np.int32)

    new_cfg = ScoringConfig(**{
        k: v for k, v in asdict(old_cfg).items()
    })

    # ── 1. Fit logistic regression for composite score weights ────────
    try:
        lr = LogisticRegression(
            max_iter=1000, solver="lbfgs", class_weight="balanced"
        )
        lr.fit(X, y)

        # Extract coefficients as weights (normalised to sum to 1.0)
        coefs = np.abs(lr.coef_[0])
        weight_sum = coefs.sum()
        if weight_sum > 0:
            normed = coefs / weight_sum
            # Order: symphonym, spatial, type_match, ccode_overlap, toponym_exact
            new_cfg.weight_symphonym = float(round(normed[0], 4))
            new_cfg.weight_spatial = float(round(normed[1], 4))
            new_cfg.weight_type_match = float(round(normed[2], 4))
            new_cfg.weight_ccode_overlap = float(round(normed[3], 4))
            new_cfg.weight_toponym_exact = float(round(normed[4], 4))

            # Ensure they sum to exactly 1.0 (absorb rounding error)
            w_total = (new_cfg.weight_symphonym + new_cfg.weight_spatial +
                       new_cfg.weight_type_match + new_cfg.weight_ccode_overlap +
                       new_cfg.weight_toponym_exact)
            if w_total > 0:
                new_cfg.weight_symphonym = round(new_cfg.weight_symphonym / w_total, 4)
                new_cfg.weight_spatial = round(new_cfg.weight_spatial / w_total, 4)
                new_cfg.weight_type_match = round(new_cfg.weight_type_match / w_total, 4)
                new_cfg.weight_ccode_overlap = round(new_cfg.weight_ccode_overlap / w_total, 4)
                new_cfg.weight_toponym_exact = round(
                    1.0 - new_cfg.weight_symphonym - new_cfg.weight_spatial -
                    new_cfg.weight_type_match - new_cfg.weight_ccode_overlap, 4
                )
    except Exception as e:
        logger.warning("Logistic regression failed, keeping default weights: %s", e)

    # ── 2. Find optimal cosine similarity threshold (Youden index) ────
    try:
        cos_pos = np.array([s["cosine_sim"] for s in pos_signals])
        cos_neg = np.array([s["cosine_sim"] for s in neg_signals])
        cos_all = np.concatenate([cos_pos, cos_neg])
        cos_labels = np.array([1] * len(cos_pos) + [0] * len(cos_neg))

        fpr, tpr, thresholds = roc_curve(cos_labels, cos_all)
        youden = tpr - fpr
        best_idx = np.argmax(youden)
        optimal_cos = float(thresholds[best_idx])
        # Clamp to reasonable range [0.5, 0.95]
        optimal_cos = max(0.5, min(0.95, optimal_cos))
        new_cfg.knn_min_similarity = round(optimal_cos, 3)
    except Exception as e:
        logger.warning("Cosine threshold fitting failed: %s", e)

    # ── 3. Find optimal spatial distance threshold ────────────────────
    try:
        dist_pos = np.array([s["distance_km"] for s in pos_signals])
        dist_neg = np.array([s["distance_km"] for s in neg_signals])

        # Use the percentile approach: the threshold should capture
        # most positives while excluding most negatives.
        # Set to the 95th percentile of positive distances (capped at 200km).
        p95 = float(np.percentile(dist_pos[dist_pos > 0], 95)) if np.any(dist_pos > 0) else 25.0
        optimal_dist = max(5.0, min(200.0, p95))
        new_cfg.threshold_phonetic_km = round(optimal_dist, 1)
    except Exception as e:
        logger.warning("Spatial threshold fitting failed: %s", e)

    # ── 4. Sweep composite score threshold for best F1 ────────────────
    try:
        from .scoring import composite_score
        from .schemas import Signals

        scores = []
        for sig in pos_signals + neg_signals:
            s = Signals(
                toponym_exact_count=sig["toponym_exact_count"],
                toponym_symphonym_max=sig["cosine_sim"],
                spatial_distance_km=sig["distance_km"],
                type_match=sig["type_match"],
                ccode_overlap_count=sig["ccode_overlap"],
            )
            scores.append(composite_score(s, new_cfg))

        scores = np.array(scores)
        labels = np.array([1] * len(pos_signals) + [0] * len(neg_signals))

        best_f1 = 0.0
        best_thresh = 0.4
        for t in np.arange(0.15, 0.80, 0.01):
            preds = (scores >= t).astype(int)
            f1 = f1_score(labels, preds, zero_division=0)
            if f1 > best_f1:
                best_f1 = f1
                best_thresh = float(t)

        new_cfg.cluster_score_threshold = round(best_thresh, 2)
    except Exception as e:
        logger.warning("Composite threshold sweep failed: %s", e)

    return new_cfg


# ── Main entry point ─────────────────────────────────────────────────────────

async def calibrate_thresholds(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    sample_size: int | None = None,
) -> bool:
    """
    Run automated threshold calibration and update cfg.scoring in-place.

    Returns True if calibration succeeded, False if skipped.
    """
    if sample_size is None:
        sample_size = cfg.scoring.calibration_sample_size
    print(flush=True)
    print("=" * 60, flush=True)
    print("  Calibration: fitting thresholds from authority hard links",
          flush=True)
    print("=" * 60, flush=True)

    # ── Load positive pairs ──────────────────────────────────────────
    pos_pairs = await _load_positive_pairs(client, cfg, sample_size)
    if len(pos_pairs) < _MIN_POSITIVES:
        print(f"  ⏭ Skipped — only {len(pos_pairs):,} authority pairs "
              f"(need ≥{_MIN_POSITIVES:,} for reliable calibration)",
              flush=True)
        return False

    print(f"  Sampled {len(pos_pairs):,} positive pairs", flush=True)

    # ── Sample negative pairs ────────────────────────────────────────
    positive_ids = set()
    for a, b in pos_pairs:
        positive_ids.add(PairwiseDoc.make_id(a, b))

    neg_pairs = await _sample_negative_pairs(
        client, cfg, positive_ids,
        int(len(pos_pairs) * cfg.scoring.calibration_neg_ratio),
    )
    print(f"  Sampled {len(neg_pairs):,} negative pairs", flush=True)

    if len(neg_pairs) < _MIN_POSITIVES:
        print(f"  ⏭ Skipped — insufficient negative samples", flush=True)
        return False

    # ── Compute signals ──────────────────────────────────────────────
    print("  Computing signals for positive pairs...", flush=True)
    pos_signals = await _compute_signals_batch(
        client, cfg, pos_pairs, "Calibration: positive signals"
    )

    print("  Computing signals for negative pairs...", flush=True)
    neg_signals = await _compute_signals_batch(
        client, cfg, neg_pairs, "Calibration: negative signals"
    )

    print(f"  Signals computed: {len(pos_signals):,} positive, "
          f"{len(neg_signals):,} negative", flush=True)

    if len(pos_signals) < _MIN_POSITIVES // 2:
        print(f"  ⏭ Skipped — too few pairs with valid signals", flush=True)
        return False

    # ── Fit thresholds ───────────────────────────────────────────────
    old_cfg = cfg.scoring
    new_cfg = _fit_thresholds(pos_signals, neg_signals, old_cfg)

    # ── Report ───────────────────────────────────────────────────────
    print(flush=True)
    print("  Calibration results:", flush=True)
    print(f"  {'Parameter':<30s} {'Default':>10s} {'Calibrated':>10s}",
          flush=True)
    print(f"  {'-'*30} {'-'*10} {'-'*10}", flush=True)

    _report = [
        ("knn_min_similarity", old_cfg.knn_min_similarity, new_cfg.knn_min_similarity),
        ("threshold_phonetic_km", old_cfg.threshold_phonetic_km, new_cfg.threshold_phonetic_km),
        ("cluster_score_threshold", old_cfg.cluster_score_threshold, new_cfg.cluster_score_threshold),
        ("weight_toponym_exact", old_cfg.weight_toponym_exact, new_cfg.weight_toponym_exact),
        ("weight_symphonym", old_cfg.weight_symphonym, new_cfg.weight_symphonym),
        ("weight_spatial", old_cfg.weight_spatial, new_cfg.weight_spatial),
        ("weight_type_match", old_cfg.weight_type_match, new_cfg.weight_type_match),
        ("weight_ccode_overlap", old_cfg.weight_ccode_overlap, new_cfg.weight_ccode_overlap),
    ]
    for name, old_val, new_val in _report:
        changed = " *" if old_val != new_val else ""
        print(f"  {name:<30s} {old_val:>10.4f} {new_val:>10.4f}{changed}",
              flush=True)

    # ── Apply ────────────────────────────────────────────────────────
    cfg.scoring = new_cfg
    print(flush=True)
    print("  ✓ Thresholds updated for this run", flush=True)

    logger.info(
        "Calibration complete: knn_min_similarity=%.3f, "
        "threshold_phonetic_km=%.1f, cluster_score_threshold=%.2f",
        new_cfg.knn_min_similarity,
        new_cfg.threshold_phonetic_km,
        new_cfg.cluster_score_threshold,
    )
    return True





