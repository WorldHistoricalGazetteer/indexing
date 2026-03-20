# clustering/clustering.py
"""
Transitive closure, spatial coherence checking, and cluster ID generation.

Implements CLUSTERS.md §5.2 Phase 4:
- Build an undirected graph from pairwise docs above the score threshold.
- Compute connected components.
- Split spatially incoherent clusters using DBSCAN or edge removal.
- Assign deterministic cluster IDs.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections import defaultdict

import numpy as np
from scipy.sparse import lil_matrix
from scipy.sparse.csgraph import connected_components
from sklearn.cluster import DBSCAN
from tqdm import tqdm

from .config import ScoringConfig
from .schemas import PairwiseDoc, MembershipDoc

logger = logging.getLogger("clustering.clustering")


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


def _cluster_id_from_members(members: list[str]) -> str:
    """Generate a deterministic cluster_id from sorted member place_ids."""
    key = "|".join(sorted(members))
    h = hashlib.sha256(key.encode()).hexdigest()[:16]
    return f"c_{h}"


def compute_clusters(
    pairwise_docs: list[PairwiseDoc],
    place_coords: dict[str, tuple[float, float] | None],
    scoring_cfg: ScoringConfig,
    algorithm_version: str,
) -> list[MembershipDoc]:
    """
    Build the graph, find connected components, check spatial coherence,
    and return membership docs.

    Args:
        pairwise_docs: All pairwise link docs (from all phases).
        place_coords: Mapping of place_id → (lat, lon) or None.
        scoring_cfg: Scoring/clustering thresholds.
        algorithm_version: String to stamp on membership docs.

    Returns:
        List of MembershipDoc instances (one per place in a cluster of size ≥ 2).
    """
    threshold = scoring_cfg.cluster_score_threshold
    max_diameter = scoring_cfg.max_cluster_diameter_km

    # Filter docs above threshold
    eligible = [d for d in pairwise_docs if d.score >= threshold]
    logger.info(
        "Clustering: %d of %d pairwise docs above threshold %.2f",
        len(eligible),
        len(pairwise_docs),
        threshold,
    )

    if not eligible:
        return []

    # Build node index
    node_set: set[str] = set()
    for d in eligible:
        node_set.add(d.place_id_a)
        node_set.add(d.place_id_b)

    nodes = sorted(node_set)
    node_idx = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)

    logger.info("Graph: %d nodes, %d edges", n, len(eligible))

    # Build sparse adjacency matrix
    adj = lil_matrix((n, n), dtype=np.float32)
    for d in eligible:
        i, j = node_idx[d.place_id_a], node_idx[d.place_id_b]
        adj[i, j] = d.score
        adj[j, i] = d.score

    # Connected components
    n_components, labels = connected_components(adj.tocsr(), directed=False)
    logger.info("Found %d connected components", n_components)

    # Group nodes by component
    components: dict[int, list[str]] = defaultdict(list)
    for i, label in enumerate(labels):
        components[label].append(nodes[i])

    # Check spatial coherence and sub-cluster if needed
    memberships: list[MembershipDoc] = []

    # Filter to non-singleton components for progress
    multi_components = {k: v for k, v in components.items() if len(v) >= 2}
    cbar = tqdm(
        total=len(multi_components),
        desc="Phase 4: spatial coherence",
        unit="component",
        miniinterval=2.0,
    )

    for comp_id, members in multi_components.items():

        # Compute spatial diameter
        coords = [place_coords.get(m) for m in members]
        valid_coords = [(m, c) for m, c in zip(members, coords) if c is not None]

        if len(valid_coords) >= 2:
            max_dist = 0.0
            for i in range(len(valid_coords)):
                for j in range(i + 1, len(valid_coords)):
                    d = _haversine_km(
                        valid_coords[i][1][0],
                        valid_coords[i][1][1],
                        valid_coords[j][1][0],
                        valid_coords[j][1][1],
                    )
                    max_dist = max(max_dist, d)

            if max_dist > max_diameter:
                # Sub-cluster using DBSCAN
                sub_clusters = _spatial_subcluster(
                    members, place_coords, max_diameter
                )
                for sub_members in sub_clusters:
                    if len(sub_members) < 2:
                        continue
                    cid = _cluster_id_from_members(sub_members)
                    for pid in sub_members:
                        memberships.append(
                            MembershipDoc(
                                place_id=pid,
                                namespace=PairwiseDoc.extract_namespace(pid),
                                cluster_id=cid,
                                cluster_size=len(sub_members),
                                algorithm_version=algorithm_version,
                            )
                        )
                cbar.update(1)
                continue

        # Spatially coherent — emit as one cluster
        cid = _cluster_id_from_members(members)
        for pid in members:
            memberships.append(
                MembershipDoc(
                    place_id=pid,
                    namespace=PairwiseDoc.extract_namespace(pid),
                    cluster_id=cid,
                    cluster_size=len(members),
                    algorithm_version=algorithm_version,
                )
            )
        cbar.update(1)

    cbar.close()

    logger.info(
        "Clustering complete: %d membership docs, %d clusters",
        len(memberships),
        len(set(m.cluster_id for m in memberships)),
    )
    return memberships


def _spatial_subcluster(
    members: list[str],
    place_coords: dict[str, tuple[float, float] | None],
    max_diameter_km: float,
) -> list[list[str]]:
    """
    Sub-cluster a spatially incoherent component using DBSCAN.

    Members without coordinates are placed in a separate "unknown" group.
    """
    with_coords = []
    without_coords = []
    for m in members:
        c = place_coords.get(m)
        if c is not None:
            with_coords.append((m, c))
        else:
            without_coords.append(m)

    if len(with_coords) < 2:
        return [members]

    # Build distance matrix in km
    n = len(with_coords)
    X = np.array([[c[0], c[1]] for _, c in with_coords])  # lat, lon

    # DBSCAN with haversine metric
    # eps is in radians for haversine; convert km to radians
    eps_rad = (max_diameter_km / 2.0) / 6371.0
    X_rad = np.radians(X)

    db = DBSCAN(eps=eps_rad, min_samples=1, metric="haversine")
    cluster_labels = db.fit_predict(X_rad)

    # Group by sub-cluster label
    sub_groups: dict[int, list[str]] = defaultdict(list)
    for (pid, _), label in zip(with_coords, cluster_labels):
        sub_groups[label].append(pid)

    # Assign no-coord members to the largest sub-cluster
    result = list(sub_groups.values())
    if without_coords and result:
        largest = max(result, key=len)
        largest.extend(without_coords)

    logger.debug(
        "Sub-clustered %d members into %d spatial groups",
        len(members),
        len(result),
    )
    return result

