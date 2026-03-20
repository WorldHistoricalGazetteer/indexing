# clustering/harvest/phonetic.py
"""
Phase 3 — Symphonym phonetic similarity via KNN search.

For places not yet linked by Phases 1–2, use Elasticsearch KNN on the
``embedding`` field (128-dim byte vectors, cosine similarity) to find
cross-namespace phonetic near-matches.

Only processes un-clustered places.  Uses tighter similarity (0.85+) and
spatial (25km) thresholds than gateway reconciliation.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from datetime import datetime
from typing import Optional

from elasticsearch import AsyncElasticsearch
from tqdm import tqdm

from ..config import ClusterConfig, KNOWN_ES_NAMESPACES
from ..schemas import PairwiseDoc, Signals

logger = logging.getLogger("clustering.harvest.phonetic")


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


async def _knn_query_for_embedding(
    client: AsyncElasticsearch,
    index: str,
    embedding: list[int],
    k: int,
    similarity: float,
    source_attestations: list[str],
) -> list[dict]:
    """
    Run a single KNN query for one embedding and return neighbour hits.
    """
    body = {
        "knn": {
            "field": "embedding",
            "query_vector": embedding,
            "k": k,
            "num_candidates": k * 5,
            "similarity": similarity,
        },
        "_source": ["name", "attestations", "namespaces"],
        "size": k,
    }
    try:
        resp = await client.search(index=index, body=body)
        return resp.get("hits", {}).get("hits", [])
    except Exception as e:
        logger.warning("KNN query failed: %s", e)
        return []


async def harvest_phonetic_links(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    clustered_place_ids: set[str],
    since: Optional[datetime] = None,
) -> list[PairwiseDoc]:
    """
    For un-clustered places, find phonetic near-matches via KNN search.

    Args:
        client: Async ES client.
        cfg: Cluster configuration.
        clustered_place_ids: Set of place_ids already linked by Phases 1–2.
        since: If provided, only process places added after this timestamp.

    Returns:
        List of PairwiseDoc instances (after spatial/ccode filtering).
    """
    scoring = cfg.scoring

    # Step 1: Find un-clustered places that need KNN queries.
    # Get their toponym embeddings from the toponyms index.
    # Strategy: scroll places not in clustered set, then look up their toponyms.

    # Build the query for places
    must_clauses: list[dict] = []
    if since is not None:
        must_clauses.append({"range": {"indexed_at": {"gt": since.isoformat()}}})

    place_query = {"bool": {"filter": must_clauses}} if must_clauses else {"match_all": {}}

    # Collect un-clustered place_ids with their metadata
    unclustered_places: dict[str, dict] = {}  # pid → {ccodes, repr_point, types}
    processed = 0

    # Get total for progress bar
    total_places = await count_query(client, cfg.places_index, place_query)
    logger.info("Phase 3: scanning %d places to find un-clustered", total_places)

    pbar = tqdm(
        total=total_places,
        desc="Phase 3: finding un-clustered places",
        unit="place",
        miniinterval=2.0,
    )

    from ..es_client import scroll_index, count_query

    async for doc in scroll_index(
        client,
        index=cfg.places_index,
        query=place_query,
        source_fields=["place_id", "namespace", "ccodes", "geometries.repr_point", "types.identifier"],
        scroll_size=cfg.scroll_size,
    ):
        pid = doc.get("place_id", "")
        if pid in clustered_place_ids:
            continue

        # Extract repr_point
        repr_point = None
        for geom in doc.get("geometries", []):
            rp = geom.get("repr_point")
            if rp:
                if isinstance(rp, dict):
                    repr_point = (rp.get("lat", 0), rp.get("lon", 0))
                elif isinstance(rp, (list, tuple)) and len(rp) >= 2:
                    repr_point = (rp[1], rp[0])
                break

        ccodes = set(doc.get("ccodes", []))
        types_ = set()
        for t in doc.get("types", []):
            tid = t.get("identifier", "")
            if tid:
                types_.add(tid)

        unclustered_places[pid] = {
            "namespace": doc.get("namespace", ""),
            "ccodes": ccodes,
            "repr_point": repr_point,
            "types": types_,
        }
        processed += 1
        pbar.update(1)
        if processed % 50_000 == 0:
            pbar.set_postfix(unclustered=len(unclustered_places))

        # Configurable cap on un-clustered places (0 = unlimited)
        if scoring.max_phase3_places > 0 and len(unclustered_places) >= scoring.max_phase3_places:
            logger.warning(
                "Phase 3: capping at %d un-clustered places (--max-phase3-places)",
                scoring.max_phase3_places,
            )
            break

    pbar.close()
    logger.info("Phase 3: %d un-clustered places to process", len(unclustered_places))

    if not unclustered_places:
        return []

    # Step 2: For each un-clustered place, find its toponym embeddings
    # Then run KNN queries
    pairs: dict[str, dict] = {}  # doc_id → {pid_a, pid_b, max_sim, ...}

    # Process in batches
    batch_pids = list(unclustered_places.keys())
    sem = asyncio.Semaphore(scoring.knn_concurrency)

    knn_bar = tqdm(
        total=len(batch_pids),
        desc="Phase 3: KNN queries",
        unit="place",
        miniinterval=2.0,
    )

    for batch_start in range(0, len(batch_pids), cfg.batch_size):
        batch = batch_pids[batch_start : batch_start + cfg.batch_size]

        # Fetch toponym embeddings for this batch
        # Each place may have multiple toponyms; cap results to avoid
        # overwhelming ES with huge response payloads.
        toponym_body = {
            "size": min(len(batch) * 5, 5000),
            "query": {"terms": {"attestations": batch}},
            "_source": ["attestations", "embedding"],
        }
        try:
            toponym_resp = await client.search(
                index=cfg.toponyms_index, body=toponym_body
            )
        except Exception as e:
            logger.warning("Toponym fetch failed for batch: %s", e)
            continue

        # Group embeddings by place_id
        pid_embeddings: dict[str, list[list[int]]] = defaultdict(list)
        for hit in toponym_resp["hits"]["hits"]:
            src = hit["_source"]
            embedding = src.get("embedding")
            if not embedding:
                continue
            for att in src.get("attestations", []):
                if att in unclustered_places:
                    pid_embeddings[att].append(embedding)

        # Run KNN queries concurrently (with semaphore)
        async def _process_embedding(pid: str, emb: list[int]):
            async with sem:
                hits = await _knn_query_for_embedding(
                    client,
                    cfg.toponyms_index,
                    emb,
                    scoring.knn_k,
                    scoring.knn_min_similarity,
                    [],
                )
                return pid, hits

        tasks = []
        for pid, embeddings in pid_embeddings.items():
            for emb in embeddings[:3]:  # limit to 3 embeddings per place
                tasks.append(_process_embedding(pid, emb))

        if not tasks:
            continue

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.warning("KNN task failed: %s", result)
                continue

            source_pid, hits = result
            source_data = unclustered_places.get(source_pid)
            if not source_data:
                continue
            source_ns = source_data["namespace"]

            for hit in hits:
                hsrc = hit["_source"]
                sim_score = hit.get("_score", 0)

                for att in hsrc.get("attestations", []):
                    if ":" not in att:
                        continue
                    target_ns = att.split(":")[0]
                    if target_ns == source_ns:
                        continue
                    if target_ns not in KNOWN_ES_NAMESPACES:
                        continue

                    ca, cb = PairwiseDoc.canonical_pair(source_pid, att)
                    doc_id = PairwiseDoc.make_id(ca, cb)

                    if doc_id not in pairs:
                        pairs[doc_id] = {
                            "pid_a": ca,
                            "pid_b": cb,
                            "max_sim": sim_score,
                        }
                    else:
                        if sim_score > pairs[doc_id]["max_sim"]:
                            pairs[doc_id]["max_sim"] = sim_score

        if batch_start % (cfg.batch_size * 10) == 0:
            knn_bar.set_postfix(pairs=len(pairs))

        knn_bar.update(len(batch))

    knn_bar.close()
    logger.info("Phase 3 KNN done: %d raw candidate pairs", len(pairs))

    # Step 3: Filter by ccode overlap and spatial distance
    # Need to fetch place data for target place_ids not yet known
    all_target_pids = set()
    for sig in pairs.values():
        for pid in (sig["pid_a"], sig["pid_b"]):
            if pid not in unclustered_places:
                all_target_pids.add(pid)

    # Fetch target place data
    target_cache: dict[str, dict] = {}
    target_list = list(all_target_pids)
    chunk_size = 2000
    for i in range(0, len(target_list), chunk_size):
        chunk = target_list[i : i + chunk_size]
        body = {
            "size": len(chunk),
            "query": {"terms": {"place_id": chunk}},
            "_source": ["place_id", "ccodes", "geometries.repr_point", "types.identifier"],
        }
        try:
            resp = await client.search(index=cfg.places_index, body=body)
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
                target_cache[pid] = {
                    "ccodes": ccodes,
                    "repr_point": repr_point,
                    "types": types_,
                }
        except Exception as e:
            logger.warning("Place data fetch failed: %s", e)

        if i + chunk_size < len(target_list):
            await asyncio.sleep(0.1)

    # Merge with unclustered_places for a unified cache
    all_place_data = {**{k: v for k, v in unclustered_places.items()}, **target_cache}

    results: list[PairwiseDoc] = []
    skipped = 0

    for doc_id, sig in pairs.items():
        pid_a = sig["pid_a"]
        pid_b = sig["pid_b"]
        data_a = all_place_data.get(pid_a)
        data_b = all_place_data.get(pid_b)

        if not data_a or not data_b:
            skipped += 1
            continue

        # Ccode overlap
        ccodes_a = data_a.get("ccodes", set())
        ccodes_b = data_b.get("ccodes", set())
        overlap = ccodes_a & ccodes_b
        if not overlap and ccodes_a and ccodes_b:
            skipped += 1
            continue

        # Spatial distance
        rp_a = data_a.get("repr_point")
        rp_b = data_b.get("repr_point")
        if rp_a and rp_b:
            distance_km = _haversine_km(rp_a[0], rp_a[1], rp_b[0], rp_b[1])
            if distance_km > scoring.threshold_phonetic_km:
                skipped += 1
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
        skipped,
    )
    return results

