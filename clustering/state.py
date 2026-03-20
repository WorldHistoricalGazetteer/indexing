# clustering/state.py
"""
High-water mark persistence for incremental runs.

State is stored as a single document in a dedicated ES index
(``cluster_state``) so it survives VM reimaging and is visible
to monitoring.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from elasticsearch import AsyncElasticsearch
from pydantic import BaseModel, Field

from .config import ClusterConfig

logger = logging.getLogger("clustering.state")

_STATE_DOC_ID = "cluster_run_state"


class HighWaterMarks(BaseModel):
    """Per-source timestamps for incremental processing."""

    places_indexed_at: Optional[datetime] = None
    toponyms_indexed_at: Optional[datetime] = None
    contributor_links_modified_at: Optional[datetime] = None


class RunStatistics(BaseModel):
    """Statistics from the last clustering run."""

    phase_1a_pairs: int = 0
    phase_1b_pairs: int = 0
    phase_2_pairs: int = 0
    phase_3_pairs: int = 0
    clusters_formed: int = 0
    singletons_excluded: int = 0
    duration_seconds: float = 0


class ClusterState(BaseModel):
    """Complete state document for the clustering pipeline."""

    last_run_timestamp: Optional[datetime] = None
    last_run_mode: str = ""  # "full" or "incremental"
    algorithm_version: str = ""
    high_water_marks: HighWaterMarks = Field(default_factory=HighWaterMarks)
    run_statistics: RunStatistics = Field(default_factory=RunStatistics)


async def load_state(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
) -> ClusterState:
    """Load the cluster state from ES (or return defaults if not found)."""
    try:
        exists = await client.indices.exists(index=cfg.state_index)
        if not exists:
            return ClusterState()

        resp = await client.get(index=cfg.state_index, id=_STATE_DOC_ID)
        return ClusterState(**resp["_source"])
    except Exception:
        logger.info("No existing cluster state found — starting fresh")
        return ClusterState()


async def save_state(
    client: AsyncElasticsearch,
    cfg: ClusterConfig,
    state: ClusterState,
) -> None:
    """Save the cluster state to ES."""
    # Ensure state index exists (single-shard, no replicas)
    exists = await client.indices.exists(index=cfg.state_index)
    if not exists:
        await client.indices.create(
            index=cfg.state_index,
            body={
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                },
            },
        )

    state.last_run_timestamp = datetime.now(timezone.utc)
    await client.index(
        index=cfg.state_index,
        id=_STATE_DOC_ID,
        body=state.model_dump(mode="json"),
    )
    logger.info("Saved cluster state (version: %s)", state.algorithm_version)

