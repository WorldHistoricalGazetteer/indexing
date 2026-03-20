# clustering/harvest/exact_coattest.py
"""
Phase 2 — Exact toponym co-attestation.

Scans the ``toponyms`` index for toponym documents whose ``attestations``
array spans multiple ES namespaces.  For each cross-namespace pair of
attesting places, checks country-code overlap and spatial distance before
emitting a candidate pair.

Based on RECON_NOTES findings:
- 7.3M multi-namespace toponyms (``namespaces`` keyword field has >1 value)
- The ``namespaces`` keyword field enables efficient ES filtering
- Must cap pairs per toponym to avoid combinatorial explosion on common names
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from typing import Optional

from elasticsearch import AsyncElasticsearch
from tqdm import tqdm

from ..config import ClusterConfig, KNOWN_ES_NAMESPACES
from ..es_client import scroll_index, count_query
from ..schemas import PairwiseDoc, Signals

logger = logging.getLogger("clustering.harvest.exact_coattest")


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in kilometres between two (lat, lon) points."""
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


async def _fetch_place_data(
    client: AsyncElasticsearch,
    place_ids: list[str],
    index: str,
) -> dict[str, dict]:
    """
    Multi-get place documents from the places index.

    Returns a dict keyed by place_id with:
    - repr_point: (lat, lon) or None
    - ccodes: set of country codes
    - types: set of type identifiers
    """
    if not place_ids:
        return {}

    # Use a terms query to fetch in bulk
    body = {
        "size": len(place_ids),
        "query": {"terms": {"place_id": place_ids}},
        "_source": ["place_id", "geometries.repr_point", "ccodes", "types.identifier"],
    }
    resp = await client.search(index=index, body=body)

    result = {}
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        pid = src.get("place_id", "")

        # Extract repr_point from first geometry
        repr_point = None
        for geom in src.get("geometries", []):
            rp = geom.get("repr_point")
            if rp:
                # ES geo_point: can be {"lat": ..., "lon": ...} or [lon, lat] or "lat,lon"
                if isinstance(rp, dict):
                    repr_point = (rp.get("lat", 0), rp.get("lon", 0))
                elif isinstance(rp, (list, tuple)) and len(rp) >= 2:
                    repr_point = (rp[1], rp[0])  # [lon, lat] → (lat, lon)
                elif isinstance(rp, str) and "," in rp:
                    parts = rp.split(",")
                    repr_point = (float(parts[0]), float(parts[1]))
                break

        ccodes = set(src.get("ccodes", []))
        types_ = set()
        for t in src.get("types", []):
            tid = t.get("identifier", "")
            if tid:
                types_.add(tid)

        result[pid] = {
            "repr_point": repr_point,
            "ccodes": ccodes,
            "types": types_,
        }

    return result


async def harvest_exact_coattestations(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    since: Optional[datetime] = None,
) -> list[PairwiseDoc]:
    """
    Scan the toponyms index for cross-namespace toponym co-attestations
    and return candidate pairwise docs after spatial/ccode filtering.

    Args:
        client: Async ES client.
        cfg: Cluster configuration.
        since: If provided, only process toponyms with ``indexed_at`` after
               this timestamp.

    Returns:
        List of PairwiseDoc instances.
    """
    # Query: toponyms where namespaces array has >1 element (multi-namespace)
    must_clauses: list[dict] = [
        {
            "script": {
                "script": {
                    "source": 'doc["namespaces"].size() > 1',
                }
            }
        }
    ]
    if since is not None:
        must_clauses.append({"range": {"indexed_at": {"gt": since.isoformat()}}})

    query = {"bool": {"filter": must_clauses}}

    # Get total count for progress bar
    total = await count_query(client, cfg.toponyms_index, query)
    logger.info("Phase 2: %d multi-namespace toponyms to scan", total)

    # Accumulate candidate pairs with toponym counts
    pair_signals: dict[str, dict] = {}  # doc_id → {count, place_ids}
    toponyms_processed = 0
    toponyms_skipped_overflow = 0

    # Process in batches: scroll toponyms, batch-fetch place data
    batch_place_ids: set[str] = set()
    batch_pairs: list[tuple[str, str]] = []  # (pid_a, pid_b)
    place_cache: dict[str, dict] = {}

    pbar = tqdm(
        total=total,
        desc="Phase 2: toponym co-attestation",
        unit="toponym",
        miniinterval=2.0,
    )

    async for doc in scroll_index(
        client,
        index=cfg.toponyms_index,
        query=query,
        source_fields=["attestations", "namespaces"],
        scroll_size=cfg.scroll_size,
    ):
        attestations = doc.get("attestations", [])

        # Group attestations by namespace
        ns_groups: dict[str, list[str]] = defaultdict(list)
        for att in attestations:
            if ":" not in att:
                continue
            ns = att.split(":")[0]
            if ns in KNOWN_ES_NAMESPACES:
                ns_groups[ns].append(att)

        if len(ns_groups) < 2:
            continue

        # Generate cross-namespace pairs
        cross_pairs = []
        for ns_a, ns_b in combinations(sorted(ns_groups.keys()), 2):
            for pid_a in ns_groups[ns_a]:
                for pid_b in ns_groups[ns_b]:
                    cross_pairs.append((pid_a, pid_b))

        # Skip toponyms with too many cross-namespace pairs
        if len(cross_pairs) > cfg.scoring.max_attestations_per_toponym:
            toponyms_skipped_overflow += 1
            continue

        for pid_a, pid_b in cross_pairs:
            ca, cb = PairwiseDoc.canonical_pair(pid_a, pid_b)
            doc_id = PairwiseDoc.make_id(ca, cb)
            if doc_id not in pair_signals:
                pair_signals[doc_id] = {
                    "pid_a": ca,
                    "pid_b": cb,
                    "toponym_exact_count": 0,
                }
            pair_signals[doc_id]["toponym_exact_count"] += 1
            batch_place_ids.add(ca)
            batch_place_ids.add(cb)

        toponyms_processed += 1
        pbar.update(1)
        if toponyms_processed % 10_000 == 0:
            pbar.set_postfix(pairs=len(pair_signals), overflow=toponyms_skipped_overflow)

        # Periodically flush place lookups (every 100K unique place IDs)
        if len(batch_place_ids) - len(place_cache) > 10_000:
            new_ids = [pid for pid in batch_place_ids if pid not in place_cache]
            if new_ids:
                # Fetch in chunks of 10K
                for i in range(0, len(new_ids), 10_000):
                    chunk = new_ids[i : i + 10_000]
                    fetched = await _fetch_place_data(client, chunk, cfg.places_index)
                    place_cache.update(fetched)

    pbar.close()

    # Final flush of place lookups
    remaining = [pid for pid in batch_place_ids if pid not in place_cache]
    if remaining:
        for i in range(0, len(remaining), 10_000):
            chunk = remaining[i : i + 10_000]
            fetched = await _fetch_place_data(client, chunk, cfg.places_index)
            place_cache.update(fetched)

    logger.info(
        "Phase 2 toponyms done: %d processed, %d overflow-skipped, "
        "%d raw candidate pairs, %d unique places to check",
        toponyms_processed,
        toponyms_skipped_overflow,
        len(pair_signals),
        len(place_cache),
    )

    # Now filter pairs by ccode overlap and spatial distance
    results: list[PairwiseDoc] = []
    skipped_no_geom = 0
    skipped_no_ccode = 0
    skipped_too_far = 0

    fbar = tqdm(
        total=len(pair_signals),
        desc="Phase 2: filtering pairs",
        unit="pair",
        miniinterval=2.0,
    )

    for doc_id, sig in pair_signals.items():
        pid_a = sig["pid_a"]
        pid_b = sig["pid_b"]
        data_a = place_cache.get(pid_a)
        data_b = place_cache.get(pid_b)

        if not data_a or not data_b:
            skipped_no_geom += 1
            fbar.update(1)
            continue

        # Ccode overlap pre-filter
        ccodes_a = data_a.get("ccodes", set())
        ccodes_b = data_b.get("ccodes", set())
        overlap = ccodes_a & ccodes_b
        if not overlap and ccodes_a and ccodes_b:
            skipped_no_ccode += 1
            fbar.update(1)
            continue

        # Spatial distance
        rp_a = data_a.get("repr_point")
        rp_b = data_b.get("repr_point")
        if rp_a is None or rp_b is None:
            # Allow pair if one is missing geometry — spatial signal is null
            distance_km = 0.0
        else:
            distance_km = _haversine_km(rp_a[0], rp_a[1], rp_b[0], rp_b[1])
            if distance_km > cfg.scoring.threshold_exact_km:
                skipped_too_far += 1
                fbar.update(1)
                continue

        # Type overlap
        types_a = data_a.get("types", set())
        types_b = data_b.get("types", set())
        type_match = bool(types_a & types_b) if types_a and types_b else False

        signals = Signals(
            toponym_exact_count=sig["toponym_exact_count"],
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
                score=0.0,  # will be set by composite scoring in Phase 4
                link_class="algorithmic_soft",
                link_method=cfg.algorithm_version,
                signals=signals,
                algorithm_version=cfg.algorithm_version,
            )
        )
        fbar.update(1)

    fbar.close()

    logger.info(
        "Phase 2 filtering done: %d pairs survived, "
        "%d no-geom, %d no-ccode-overlap, %d too-far",
        len(results),
        skipped_no_geom,
        skipped_no_ccode,
        skipped_too_far,
    )
    return results

