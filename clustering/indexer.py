# clustering/indexer.py
"""
Bulk-index pairwise link docs and membership docs into the ``clusters``
Elasticsearch index.

Uses deterministic ``_id`` values so that reruns are idempotent.
"""

from __future__ import annotations

import logging
from pathlib import Path
import json

from elasticsearch import AsyncElasticsearch

from .config import ClusterConfig
from .es_client import bulk_index
from .schemas import PairwiseDoc, MembershipDoc

logger = logging.getLogger("clustering.indexer")

_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "clusters.json"


async def ensure_clusters_index(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
) -> None:
    """Create the clusters index if it doesn't exist."""
    exists = await client.indices.exists(index=cfg.clusters_index)
    if not exists:
        schema = json.loads(_SCHEMA_PATH.read_text())
        await client.indices.create(index=cfg.clusters_index, body=schema)
        logger.info("Created index: %s", cfg.clusters_index)
    else:
        logger.info("Index already exists: %s", cfg.clusters_index)


async def index_pairwise_docs(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    docs: list[PairwiseDoc],
) -> tuple[int, int]:
    """Index pairwise link documents. Returns (success, errors)."""
    if not docs:
        return 0, 0

    actions = []
    for doc in docs:
        doc_id = PairwiseDoc.make_id(doc.place_id_a, doc.place_id_b)
        actions.append({
            "_id": doc_id,
            "_source": doc.model_dump(mode="json"),
        })

    logger.info("Indexing %d pairwise docs", len(actions))
    return await bulk_index(
        client, cfg.clusters_index, actions, cfg.bulk_chunk,
        throttle_seconds=cfg.bulk_throttle_seconds,
    )


async def index_membership_docs(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    docs: list[MembershipDoc],
) -> tuple[int, int]:
    """Index membership documents. Returns (success, errors)."""
    if not docs:
        return 0, 0

    actions = []
    for doc in docs:
        doc_id = MembershipDoc.make_id(doc.place_id)
        actions.append({
            "_id": doc_id,
            "_source": doc.model_dump(mode="json"),
        })

    logger.info("Indexing %d membership docs", len(actions))
    return await bulk_index(
        client, cfg.clusters_index, actions, cfg.bulk_chunk,
        throttle_seconds=cfg.bulk_throttle_seconds,
    )


async def delete_stale_memberships(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    valid_place_ids: set[str],
) -> int:
    """
    Delete membership docs for places no longer in any cluster.

    This handles cluster splits when pairwise links are removed.
    Returns the number of docs deleted.
    """
    # Find all existing membership docs
    body = {
        "query": {"term": {"doc_type": "membership"}},
        "_source": ["place_id"],
    }

    stale_ids = []
    from .es_client import scroll_index
    async for doc in scroll_index(
        client, cfg.clusters_index, body["query"], ["place_id"]
    ):
        pid = doc.get("place_id", "")
        if pid not in valid_place_ids:
            stale_ids.append(MembershipDoc.make_id(pid))

    if stale_ids:
        # Delete in batches
        for i in range(0, len(stale_ids), 1000):
            chunk = stale_ids[i : i + 1000]
            body = {"query": {"ids": {"values": chunk}}}
            await client.delete_by_query(index=cfg.clusters_index, body=body)

        logger.info("Deleted %d stale membership docs", len(stale_ids))

    return len(stale_ids)

