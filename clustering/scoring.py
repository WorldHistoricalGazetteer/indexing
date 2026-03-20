# clustering/scoring.py
"""
Composite score calculation for algorithmic soft links.

Implements the weighted combination of evidence signals described in
CLUSTERS.md §5.2 Phase 4.  Hard links (authority_sameAs, contributor_sameAs)
always have score=1.0 and are not re-scored.
"""

from __future__ import annotations

import math

from .config import ScoringConfig
from .schemas import PairwiseDoc, Signals


def composite_score(signals: Signals, cfg: ScoringConfig) -> float:
    """
    Weighted combination of evidence signals.

    All component scores are normalised to 0.0–1.0 before weighting.
    """
    s = 0.0

    # Exact toponym co-attestation count (log-scaled, capped at 5)
    s += cfg.weight_toponym_exact * min(
        1.0, math.log1p(signals.toponym_exact_count) / math.log1p(5)
    )

    # Best phonetic embedding similarity (already 0–1 scale)
    s += cfg.weight_symphonym * signals.toponym_symphonym_max

    # Inverse spatial distance (sigmoid-scaled: 10km half-point)
    s += cfg.weight_spatial * (
        1.0 / (1.0 + signals.spatial_distance_km / 10.0)
    )

    # Type match (boolean)
    s += cfg.weight_type_match * (1.0 if signals.type_match else 0.0)

    # Country code overlap count (capped at 2)
    s += cfg.weight_ccode_overlap * min(
        1.0, signals.ccode_overlap_count / 2.0
    )

    return round(s, 4)


def score_pairwise_docs(
    docs: list[PairwiseDoc],
    cfg: ScoringConfig,
) -> list[PairwiseDoc]:
    """
    Compute composite scores for all algorithmic soft link docs.

    Hard links (score=1.0) are passed through unchanged.
    """
    for doc in docs:
        if doc.link_class in ("authority_sameAs", "contributor_sameAs"):
            # Hard links keep score=1.0
            continue
        if doc.signals is not None:
            doc.score = composite_score(doc.signals, cfg)
    return docs

