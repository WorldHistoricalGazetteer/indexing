# clustering/schemas.py
"""
Pydantic models for pairwise link and membership documents
stored in the ``clusters`` Elasticsearch index.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class Signals(BaseModel):
    """Evidence breakdown for an algorithmic soft link."""

    toponym_exact_count: int = 0
    toponym_symphonym_max: float = 0.0
    spatial_distance_km: float = 0.0
    type_match: bool = False
    ccode_overlap_count: int = 0
    shared_link_ids: list[str] = Field(default_factory=list)


class PairwiseDoc(BaseModel):
    """A scored pairwise link between two places."""

    doc_type: str = "pairwise"
    place_id_a: str  # canonically ordered: a < b
    place_id_b: str
    namespace_a: str
    namespace_b: str
    score: float
    link_class: str  # authority_sameAs | contributor_sameAs | algorithmic_soft
    link_method: str  # provenance detail
    signals: Optional[Signals] = None
    algorithm_version: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def make_id(pid_a: str, pid_b: str) -> str:
        """Deterministic document _id for a pairwise doc."""
        a, b = sorted((pid_a, pid_b))
        return f"pw_{a}_{b}"

    @staticmethod
    def canonical_pair(pid_a: str, pid_b: str) -> tuple[str, str]:
        """Return (a, b) in canonical (sorted) order."""
        return tuple(sorted((pid_a, pid_b)))  # type: ignore[return-value]

    @staticmethod
    def extract_namespace(place_id: str) -> str:
        """Extract the namespace prefix from a namespaced place_id."""
        return place_id.split(":")[0] if ":" in place_id else ""


class MembershipDoc(BaseModel):
    """Cluster membership for a single place."""

    doc_type: str = "membership"
    place_id: str
    namespace: str
    cluster_id: str
    cluster_size: int
    algorithm_version: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def make_id(place_id: str) -> str:
        """Deterministic document _id for a membership doc."""
        return f"mb_{place_id}"

