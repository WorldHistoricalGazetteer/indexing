# clustering/es_client.py
"""
Async Elasticsearch client wrapper for the clustering module.

Provides connection management, scroll helpers, and bulk indexing
utilities.  Uses the official ``elasticsearch[async]`` client.
"""

from __future__ import annotations

import asyncio
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
    throttle_seconds: float = 0.0,
) -> tuple[int, int]:
    """
    Bulk-index a list of action dicts into ``index``.

    Each action dict must have ``_id`` and ``_source`` keys.
    If *throttle_seconds* > 0, sleeps between chunks to reduce
    ES merge pressure.
    Returns (success_count, error_count).
    """
    total_success = 0
    total_errors = 0

    for start in range(0, len(actions), chunk_size):
        chunk = actions[start : start + chunk_size]

        def gen():
            for a in chunk:
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
        total_success += success
        total_errors += errors

        if throttle_seconds > 0 and start + chunk_size < len(actions):
            await asyncio.sleep(throttle_seconds)

    if total_errors:
        logger.warning(
            "Bulk indexing: %d succeeded, %d errors", total_success, total_errors
        )
    else:
        logger.info("Bulk indexing: %d succeeded", total_success)
    return total_success, total_errors


