# clustering/harvest/hard_links.py
"""
Phase 1A — Harvest authority ``sameAs`` / ``closeMatch`` / ``exactMatch``
relations from the ES ``places`` index.

Scans the ``relations`` nested field for identity relation types and
emits :class:`PairwiseDoc` instances for each cross-namespace pair.

Based on RECON_NOTES findings:
- Identity relation_types: sameAs, closeMatch, exactMatch
- All ``related_place_id`` values are already namespaced
- The ``links`` field is unused — only ``relations`` matters
- Must filter: same-namespace self-references (e.g. GN closeMatch→GN)
  and targets in non-WHG namespaces (e.g. D-Place→glottolog)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from elasticsearch import AsyncElasticsearch
from tqdm import tqdm

from ..config import (
    ClusterConfig,
    IDENTITY_RELATION_TYPES,
    KNOWN_ES_NAMESPACES,
)
from ..es_client import scroll_index, count_query
from ..schemas import PairwiseDoc

logger = logging.getLogger("clustering.harvest.hard_links")


async def harvest_authority_hard_links(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    since: Optional[datetime] = None,
) -> list[PairwiseDoc]:
    """
    Scan the places index for identity relations and return pairwise docs.

    Args:
        client: Async ES client.
        cfg: Cluster configuration.
        since: If provided, only process places with ``indexed_at`` after
               this timestamp (incremental mode). If None, process all.

    Returns:
        List of PairwiseDoc instances (deduplicated by canonical pair).
    """
    # Build query: places that have at least one identity relation
    must_clauses: list[dict] = [
        {
            "nested": {
                "path": "relations",
                "query": {
                    "terms": {
                        "relations.relation_type": list(IDENTITY_RELATION_TYPES)
                    }
                },
            }
        }
    ]
    if since is not None:
        must_clauses.append({"range": {"indexed_at": {"gt": since.isoformat()}}})

    query = {"bool": {"filter": must_clauses}}

    # Get total count for progress bar
    total = await count_query(client, cfg.places_index, query)
    logger.info("Phase 1A: %d places with identity relations to scan", total)

    # Collect unique pairs
    pairs: dict[str, PairwiseDoc] = {}  # keyed by canonical _id
    processed = 0
    skipped_same_ns = 0
    skipped_unknown_ns = 0

    pbar = tqdm(
        total=total,
        desc="Phase 1A: authority hard links",
        unit="place",
        mininterval=2.0,
    )

    async for doc in scroll_index(
        client,
        index=cfg.places_index,
        query=query,
        source_fields=["place_id", "namespace", "relations"],
        scroll_size=cfg.scroll_size,
    ):
        place_id = doc.get("place_id", "")
        source_ns = doc.get("namespace", "")

        for rel in doc.get("relations", []):
            rel_type = rel.get("relation_type", "")
            if rel_type not in IDENTITY_RELATION_TYPES:
                continue

            target_id = rel.get("related_place_id", "")
            if not target_id or ":" not in target_id:
                continue

            target_ns = target_id.split(":")[0]

            # Skip same-namespace (e.g. GN closeMatch → GN self-reference)
            if target_ns == source_ns:
                skipped_same_ns += 1
                continue

            # Skip targets not in our ES index
            if target_ns not in KNOWN_ES_NAMESPACES:
                skipped_unknown_ns += 1
                continue

            # Create canonical pair
            pid_a, pid_b = PairwiseDoc.canonical_pair(place_id, target_id)
            doc_id = PairwiseDoc.make_id(pid_a, pid_b)

            if doc_id not in pairs:
                pairs[doc_id] = PairwiseDoc(
                    place_id_a=pid_a,
                    place_id_b=pid_b,
                    namespace_a=PairwiseDoc.extract_namespace(pid_a),
                    namespace_b=PairwiseDoc.extract_namespace(pid_b),
                    score=1.0,
                    link_class="authority_sameAs",
                    link_method=f"{source_ns}_relation",
                    algorithm_version=cfg.algorithm_version,
                )
            else:
                # Pair already seen (from the other direction) — merge methods
                existing = pairs[doc_id]
                method = f"{source_ns}_relation"
                if method not in existing.link_method:
                    existing.link_method = f"{existing.link_method},{method}"

        processed += 1
        pbar.update(1)
        pbar.set_postfix(pairs=len(pairs))

    pbar.close()

    logger.info(
        "Phase 1A complete: %d places processed, %d unique pairs, "
        "%d skipped (same-ns), %d skipped (unknown-ns)",
        processed,
        len(pairs),
        skipped_same_ns,
        skipped_unknown_ns,
    )
    return list(pairs.values())


