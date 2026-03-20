# clustering/es_client.py
"""
Async Elasticsearch client wrapper for the clustering module.

Provides connection management, scroll helpers, and bulk indexing
utilities.  Uses the official ``elasticsearch[async]`` client.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk, async_scan

from .config import ClusterConfig

logger = logging.getLogger("clustering.es_client")


@asynccontextmanager
async def es_client(cfg: ClusterConfig) -> AsyncIterator[AsyncElasticsearch]:
    """Yield an async ES client, ensuring it is closed on exit."""
    auth = ("elastic", cfg.es_password) if cfg.es_password else None
    client = AsyncElasticsearch(
        cfg.es_url,
        basic_auth=auth,
        request_timeout=120,
        max_retries=3,
        retry_on_timeout=True,
    )
    try:
        yield client
    finally:
        await client.close()


async def count_query(
    client: AsyncElasticsearch,
    index: str,
    query: dict,
) -> int:
    """Return the number of documents matching ``query`` in ``index``."""
    try:
        resp = await client.count(index=index, body={"query": query})
        return resp["count"]
    except Exception:
        return 0


async def scroll_index(
    client: AsyncElasticsearch,
    index: str,
    query: dict,
    source_fields: list[str] | None = None,
    scroll_size: int = 5000,
) -> AsyncIterator[dict]:
    """
    Async generator that scrolls through an ES index, yielding
    each hit's ``_source`` dict (with ``_id`` injected).
    """
    body: dict = {"query": query}
    if source_fields is not None:
        body["_source"] = source_fields  # type: ignore[assignment]

    async for hit in async_scan(
        client,
        index=index,
        query=body,
        scroll="10m",
        size=scroll_size,
        preserve_order=False,
    ):
        doc = hit["_source"]
        doc["_id"] = hit["_id"]
        yield doc


async def bulk_index(
    client: AsyncElasticsearch,
    index: str,
    actions: list[dict],
    chunk_size: int = 5000,
) -> tuple[int, int]:
    """
    Bulk-index a list of action dicts into ``index``.

    Each action dict must have ``_id`` and ``_source`` keys.
    Returns (success_count, error_count).
    """
    def gen():
        for a in actions:
            yield {
                "_op_type": "index",
                "_index": index,
                "_id": a["_id"],
                "_source": a["_source"],
            }

    success, errors = await async_bulk(
        client,
        gen(),
        chunk_size=chunk_size,
        raise_on_error=False,
        stats_only=True,
    )
    if errors:
        logger.warning("Bulk indexing: %d succeeded, %d errors", success, errors)
    else:
        logger.info("Bulk indexing: %d succeeded", success)
    return success, errors


