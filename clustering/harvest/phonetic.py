# clustering/harvest/phonetic.py
"""
Phase 3 — Symphonym phonetic similarity via KNN search.

For places not yet linked by Phases 1–2, use Elasticsearch KNN on the
``embedding`` field (128-dim byte vectors, cosine similarity) to find
cross-namespace phonetic near-matches.

**Strategy (toponym-side scan):**

Instead of scrolling 47M places and issuing one KNN query per place, we
scroll the *toponyms* index for docs that:
  (a) have an ``embedding``,
  (b) attest to ≥1 unclustered place, and
  (c) attest in ≥2 namespaces (only multi-namespace toponyms can
      produce cross-namespace pairs).

For each qualifying toponym we fire a KNN query against the rest of the
toponyms index.  Queries are batched via ES ``_msearch`` (50–100 per
HTTP request) for dramatically better throughput than individual requests.

Pairs are filtered by ccode overlap and spatial distance, then emitted
in streaming batches to keep memory bounded.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from typing import Optional

from elasticsearch import AsyncElasticsearch
from tqdm import tqdm

from ..config import ClusterConfig, KNOWN_ES_NAMESPACES
from ..es_client import scroll_index, count_query
from ..schemas import PairwiseDoc, Signals

logger = logging.getLogger("clustering.harvest.phonetic")

# How many KNN queries to pack into a single _msearch call
MSEARCH_BATCH_SIZE = 50


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


# ── place-data cache ────────────────────────────────────────────────────────

async def _fetch_place_data_batch(
    client: AsyncElasticsearch,
    pids: list[str],
    places_index: str,
) -> dict[str, dict]:
    """Fetch ccodes, repr_point, types for a batch of place_ids."""
    if not pids:
        return {}
    body = {
        "size": len(pids),
        "query": {"terms": {"place_id": pids}},
        "_source": ["place_id", "namespace", "ccodes",
                     "geometries.repr_point", "types.identifier"],
    }
    cache: dict[str, dict] = {}
    try:
        resp = await client.search(index=places_index, body=body)
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
                        repr_point = (rp[1], rp[0])  # GeoJSON [lon, lat]
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
    return cache


# ── msearch KNN helper ──────────────────────────────────────────────────────

async def _msearch_knn(
    client: AsyncElasticsearch,
    index: str,
    queries: list[tuple[str, list[int]]],  # [(toponym_id, embedding), ...]
    k: int,
    similarity: float,
) -> list[tuple[str, list[dict]]]:
    """
    Fire multiple KNN queries via a single _msearch call.
    Returns [(toponym_id, [hit, ...]), ...].
    """
    if not queries:
        return []

    body_lines: list[dict] = []
    for _tid, emb in queries:
        body_lines.append({"index": index})
        body_lines.append({
            "size": k,
            "knn": {
                "field": "embedding",
                "query_vector": emb,
                "k": k,
                "num_candidates": k * 5,
                "similarity": similarity,
            },
            "_source": ["attestations", "namespaces"],
        })

    try:
        resp = await client.msearch(body=body_lines)
    except Exception as e:
        logger.warning("_msearch KNN failed: %s", e)
        return [(tid, []) for tid, _ in queries]

    results = []
    for i, sub_resp in enumerate(resp.get("responses", [])):
        tid = queries[i][0]
        hits = sub_resp.get("hits", {}).get("hits", [])
        results.append((tid, hits))
    return results


# ── main entry point ────────────────────────────────────────────────────────

async def harvest_phonetic_links(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    clustered_place_ids: set[str],
    since: Optional[datetime] = None,
) -> list[PairwiseDoc]:
    """
    For un-clustered places, find phonetic near-matches via KNN search.

    Uses a toponym-side approach:
      1. Scroll toponyms that have embeddings
      2. Skip those whose attestations are all already-clustered
      3. Batch KNN via _msearch (50 per call)
      4. Build cross-namespace pairs and filter by ccode/spatial distance
    """
    scoring = cfg.scoring

    # ── Step 1: Count qualifying toponyms ────────────────────────────────
    # Only toponyms with embeddings can participate.
    toponym_query: dict = {
        "bool": {
            "filter": [
                {"exists": {"field": "embedding"}},
            ]
        }
    }
    if since is not None:
        toponym_query["bool"]["filter"].append(
            {"range": {"indexed_at": {"gt": since.isoformat()}}}
        )

    total_toponyms = await count_query(client, cfg.toponyms_index, toponym_query)
    logger.info(
        "Phase 3: %d toponyms with embeddings to scan", total_toponyms
    )

    # ── Step 2: Scroll toponyms, batch KNN queries ───────────────────────
    pairs: dict[str, dict] = {}  # doc_id → {pid_a, pid_b, max_sim}
    place_cache: dict[str, dict] = {}  # pid → {namespace, ccodes, repr_point, types}

    # Accumulate a batch of (toponym_id, embedding, unclustered_attestations)
    knn_batch: list[tuple[str, list[int], list[str]]] = []

    skipped_all_clustered = 0
    skipped_single_ns = 0
    processed = 0
    knn_issued = 0

    bar = tqdm(
        total=total_toponyms,
        desc="Phase 3: scanning toponyms",
        unit="toponym",
        mininterval=2.0,
    )

    async def _flush_knn_batch():
        """Fire the accumulated KNN batch and process results."""
        nonlocal knn_issued
        if not knn_batch:
            return

        # Build the queries list for _msearch_knn
        queries = [(tid, emb) for tid, emb, _atts in knn_batch]

        results = await _msearch_knn(
            client, cfg.toponyms_index, queries,
            scoring.knn_k, scoring.knn_min_similarity,
        )
        knn_issued += len(queries)

        # Collect place_ids we need metadata for
        pids_needed: set[str] = set()

        for (source_tid, source_emb, source_atts), (_, hits) in zip(knn_batch, results):
            # source_atts: unclustered place_ids attested by this toponym
            source_pids = set(source_atts)

            for hit in hits:
                hsrc = hit.get("_source", {})
                target_atts = hsrc.get("attestations", [])
                sim_score = hit.get("_score", 0)

                # Build cross-namespace pairs
                for s_pid in source_pids:
                    s_ns = s_pid.split(":")[0] if ":" in s_pid else ""
                    if s_ns not in KNOWN_ES_NAMESPACES:
                        continue

                    for t_pid in target_atts:
                        if t_pid == s_pid:
                            continue
                        t_ns = t_pid.split(":")[0] if ":" in t_pid else ""
                        if t_ns == s_ns:
                            continue
                        if t_ns not in KNOWN_ES_NAMESPACES:
                            continue

                        ca, cb = PairwiseDoc.canonical_pair(s_pid, t_pid)
                        doc_id = PairwiseDoc.make_id(ca, cb)

                        if doc_id not in pairs:
                            pairs[doc_id] = {
                                "pid_a": ca,
                                "pid_b": cb,
                                "max_sim": sim_score,
                            }
                            pids_needed.add(ca)
                            pids_needed.add(cb)
                        else:
                            if sim_score > pairs[doc_id]["max_sim"]:
                                pairs[doc_id]["max_sim"] = sim_score

        # Pre-fetch any place data we don't already have
        missing = [p for p in pids_needed if p not in place_cache]
        if missing:
            for i in range(0, len(missing), 2000):
                chunk = missing[i:i + 2000]
                fetched = await _fetch_place_data_batch(
                    client, chunk, cfg.places_index
                )
                place_cache.update(fetched)

        knn_batch.clear()

    # ── Scroll ───────────────────────────────────────────────────────────
    async for doc in scroll_index(
        client,
        index=cfg.toponyms_index,
        query=toponym_query,
        source_fields=["attestations", "namespaces", "embedding"],
        scroll_size=cfg.scroll_size,
    ):
        processed += 1
        bar.update(1)

        embedding = doc.get("embedding")
        if not embedding:
            continue

        attestations = doc.get("attestations", [])
        namespaces = doc.get("namespaces", [])

        # Skip single-namespace toponyms (can never produce cross-ns pairs)
        if len(set(namespaces)) < 2:
            # Even if namespaces field is incomplete, check attestations
            ns_from_atts = set()
            for att in attestations:
                if ":" in att:
                    ns_from_atts.add(att.split(":")[0])
            if len(ns_from_atts) < 2:
                skipped_single_ns += 1
                continue

        # Skip if ALL attested places are already clustered
        unclustered_atts = [a for a in attestations if a not in clustered_place_ids]
        if not unclustered_atts:
            skipped_all_clustered += 1
            continue

        toponym_id = doc.get("_id", "")
        knn_batch.append((toponym_id, embedding, unclustered_atts))

        # Flush when we have enough for an _msearch batch
        if len(knn_batch) >= MSEARCH_BATCH_SIZE:
            await _flush_knn_batch()
            if processed % 100_000 == 0:
                bar.set_postfix(
                    pairs=len(pairs),
                    knn=knn_issued,
                    skip_clust=skipped_all_clustered,
                    skip_ns=skipped_single_ns,
                )

    # Flush remaining
    await _flush_knn_batch()
    bar.close()

    logger.info(
        "Phase 3 toponym scan done: %d toponyms processed, "
        "%d KNN queries issued, %d raw candidate pairs, "
        "%d skipped (all clustered), %d skipped (single namespace)",
        processed, knn_issued, len(pairs),
        skipped_all_clustered, skipped_single_ns,
    )

    if not pairs:
        return []

    # ── Step 3: Filter by ccode overlap and spatial distance ─────────────
    # Ensure we have place data for all referenced pids
    all_pids = set()
    for sig in pairs.values():
        all_pids.add(sig["pid_a"])
        all_pids.add(sig["pid_b"])
    missing = [p for p in all_pids if p not in place_cache]
    if missing:
        logger.info("Fetching place data for %d remaining pids", len(missing))
        for i in range(0, len(missing), 2000):
            chunk = missing[i:i + 2000]
            fetched = await _fetch_place_data_batch(
                client, chunk, cfg.places_index
            )
            place_cache.update(fetched)
            if i + 2000 < len(missing):
                await asyncio.sleep(0.05)

    results: list[PairwiseDoc] = []
    skipped_filter = 0

    for doc_id, sig in pairs.items():
        pid_a = sig["pid_a"]
        pid_b = sig["pid_b"]
        data_a = place_cache.get(pid_a)
        data_b = place_cache.get(pid_b)

        if not data_a or not data_b:
            skipped_filter += 1
            continue

        # Ccode overlap
        ccodes_a = data_a.get("ccodes", set())
        ccodes_b = data_b.get("ccodes", set())
        overlap = ccodes_a & ccodes_b
        if not overlap and ccodes_a and ccodes_b:
            skipped_filter += 1
            continue

        # Spatial distance
        rp_a = data_a.get("repr_point")
        rp_b = data_b.get("repr_point")
        if rp_a and rp_b:
            distance_km = _haversine_km(rp_a[0], rp_a[1], rp_b[0], rp_b[1])
            if distance_km > scoring.threshold_phonetic_km:
                skipped_filter += 1
                continue
        else:
            distance_km = 0.0

        types_a = data_a.get("types", set())
        types_b = data_b.get("types", set())
        type_match = bool(types_a & types_b) if types_a and types_b else False

        signals = Signals(
            toponym_symphonym_max=sig["max_sim"],
            spatial_distance_km=distance_km,
            type_match=type_match,
            ccode_overlap_count=len(overlap),
        )

        results.append(
            PairwiseDoc(
                place_id_a=pid_a,
                place_id_b=pid_b,
                namespace_a=PairwiseDoc.extract_namespace(pid_a),
                namespace_b=PairwiseDoc.extract_namespace(pid_b),
                score=0.0,  # set by composite scoring
                link_class="algorithmic_soft",
                link_method=cfg.algorithm_version,
                signals=signals,
                algorithm_version=cfg.algorithm_version,
            )
        )

    logger.info(
        "Phase 3 filtering done: %d pairs survived, %d skipped",
        len(results),
        skipped_filter,
    )
    return results

