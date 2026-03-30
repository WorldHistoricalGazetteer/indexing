# gateway/reconcile.py
"""
Reconciliation search endpoint for the WHG API gateway.

Accepts a normalised reconciliation query and orchestrates a **three-step**
search across the ``places`` and ``toponyms`` ES indexes:

  1. **Discovery** — Search the ``toponyms`` index for candidate place_ids.

     - ``fuzzy`` / ``phonetic`` modes use **Symphonym KNN only**.  The
       phonetic embedding space inherently ranks exact-string matches
       highest (cosine ≈ 1.0), so a separate BM25 text search is
       redundant and creates score-scale mismatches.
     - ``exact`` / ``starts`` / ``in`` modes use **BM25 text search only**
       — these are structural queries where phonetic proximity doesn't
       apply.

     Each toponym document carries an ``attestations`` list of place_ids,
     so we accumulate a scored set of unique candidate place_ids (best
     toponym-match score per place).

  2. **Filtering** — Fetch candidate places from the ``places`` index via
     a ``terms`` filter on ``place_id`` (inverted-index lookup, very
     fast even for thousands of IDs), with optional spatial / temporal /
     country-code / namespace filters.

  3. **Enrichment** — Query the ``toponyms`` index again with a ``terms``
     filter on ``attestations`` for the surviving place_ids.  This
     retrieves the **full name inventory** (label + lang) for each place,
     regardless of which toponym triggered the original match.

Returns a flat list of candidate hits suitable for the WHG Django app
to merge with legacy results.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .config import (
    ES_BACKEND,
    CLUSTERS_INDEX,
    PLACES_INDEX,
    TOPONYMS_INDEX,
)
from .es_helpers import (
    es_auth as _es_auth,
    ES_HEADERS,
    build_toponym_query as _build_toponym_query,
    build_phonetic_knn as _build_phonetic_knn,
    collect_place_ids as _collect_place_ids,
    build_places_filter as _build_places_filter,
    build_toponym_lookup as _build_toponym_lookup,
)

logger = logging.getLogger("gateway.reconcile")

router = APIRouter(prefix="/api", tags=["Reconciliation"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ReconcileRequest(BaseModel):
    """Query shape sent by the WHG Django app."""

    query: Optional[str] = Field(None, description="Query toponym string")
    mode: str = Field("fuzzy", description="Search mode: exact | starts | in | fuzzy | phonetic")
    ccodes: Optional[list[str]] = Field(None, description="ISO-3166 country code filter")
    fclasses: Optional[list[str]] = Field(None, description="GeoNames feature-class codes (not supported on new indexes, ignored)")
    bounds: Optional[dict] = Field(None, description="GeoJSON geometry for spatial filter (intersects)")
    start_year: Optional[int] = Field(None, description="Temporal filter: start year")
    end_year: Optional[int] = Field(None, description="Temporal filter: end year")
    size: int = Field(50, ge=1, le=500, description="Max results to return")
    exclude_namespaces: list[str] = Field(
        default=["gb"],
        description="Namespace prefixes to exclude from results (e.g. ['gb'] "
                    "to suppress noisy Ordnance Survey records). "
                    "Pass [] to include all namespaces.",
    )
    group_by_cluster: bool = Field(
        default=False,
        description="When True, group results by pre-computed cluster. "
                    "The 'clusters' field in the response will be populated "
                    "with grouped results alongside the flat 'hits' list.",
    )


class CandidateName(BaseModel):
    label: str
    lang: Optional[str] = None


class CandidateGeometry(BaseModel):
    repr_point: Optional[list[float]] = None  # [lon, lat]


class CandidateHit(BaseModel):
    place_id: str = Field(description="Namespaced ID, e.g. gn:745044")
    title: str
    names: list[CandidateName] = []
    ccodes: list[str] = []
    score: float = 0
    namespace: str = ""
    geometries: list[CandidateGeometry] = []


class ClusterGroup(BaseModel):
    """A group of candidate hits that belong to the same pre-computed cluster."""
    cluster_id: str
    cluster_size: int
    representative: CandidateHit  # highest-scoring member
    members: list[CandidateHit] = []  # remaining members


class ReconcileResponse(BaseModel):
    hits: list[CandidateHit] = []  # flat list (always populated)
    clusters: list[ClusterGroup] = []  # grouped view (populated when group_by_cluster=True)
    max_score: float = 0
    total: int = 0


# ---------------------------------------------------------------------------
# Internal helpers (reconcile-specific)
# ---------------------------------------------------------------------------



def _format_candidate(
    src: dict,
    score: float,
    toponyms: list[dict] | None = None,
) -> CandidateHit:
    """Convert an ES places hit _source into a CandidateHit.

    Args:
        src: ``_source`` dict from the places index hit.
        score: Normalised toponym-match score (0–100).
        toponyms: Optional list of ``{"label": ..., "lang": ...}`` dicts
            from Step 3 (toponym enrichment).  When provided these are
            used instead of the nested ``toponyms`` in the places index.
    """
    # Extract names — prefer step-3 enrichment, fall back to nested data
    names: list[CandidateName] = []
    seen_labels: set[str] = set()
    toponym_data = toponyms if toponyms is not None else src.get("toponyms", [])
    for t in toponym_data:
        label = t.get("label", "")
        if label and label not in seen_labels:
            names.append(CandidateName(label=label, lang=t.get("lang")))
            seen_labels.add(label)

    # Extract representative points from nested geometries
    geometries = []
    for g in src.get("geometries", []):
        rp = g.get("repr_point")
        if rp:
            if isinstance(rp, dict):
                geometries.append(CandidateGeometry(
                    repr_point=[rp.get("lon", 0), rp.get("lat", 0)]
                ))
            elif isinstance(rp, list) and len(rp) == 2:
                geometries.append(CandidateGeometry(repr_point=rp))

    return CandidateHit(
        place_id=src.get("place_id", ""),
        title=src.get("title", ""),
        names=names,
        ccodes=src.get("ccodes", []),
        score=score,
        namespace=src.get("namespace", ""),
        geometries=geometries,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/reconcile", response_model=ReconcileResponse)
async def reconcile_search(req: ReconcileRequest):
    """
    Reconciliation-style search across the CRC places + toponyms indexes.

    Three-step strategy:

      **Step 1 — Discovery.**  For ``fuzzy``/``phonetic`` modes, Symphonym
      KNN search on the ``toponyms`` index (phonetic embedding space
      naturally ranks exact matches highest, so a separate text search is
      unnecessary).  For ``exact``/``starts``/``in`` modes, BM25 text
      search instead.  Each hit carries ``attestations`` — the place_ids
      of every place that uses that name form.  We accumulate a scored
      set of candidate place_ids, keeping the best score per place.

      **Step 2 — Filtering.**  Fetch the candidate places from the
      ``places`` index using a ``terms`` filter on ``place_id``.  Optional
      spatial / temporal / country-code / namespace filters are applied.

      **Step 3 — Enrichment.**  Query the ``toponyms`` index again with a
      ``terms`` filter on ``attestations`` for the surviving place_ids.
      This retrieves the **full name inventory** for each place.

    The candidates are ranked by the toponym-match score carried forward
    from step 1.
    """
    import httpx
    from collections import defaultdict

    if not req.query:
        return ReconcileResponse()

    auth = _es_auth()

    # Build exclusion prefixes from requested namespaces (e.g. ["gb"] → ("gb:",))
    exclude_prefixes = tuple(f"{ns}:" for ns in req.exclude_namespaces) if req.exclude_namespaces else ()

    # place_id → best toponym-match score
    place_scores: dict[str, float] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        # ------------------------------------------------------------------
        # Step 1: Discovery — search toponyms → collect unique place_ids
        # ------------------------------------------------------------------

        if req.mode in ("fuzzy", "phonetic"):
            knn_body = _build_phonetic_knn(req.query, k=200, similarity=0.7)
            if knn_body:
                knn_resp = await client.post(
                    f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                    json=knn_body,
                    auth=auth,
                    headers=ES_HEADERS,
                )
                knn_resp.raise_for_status()
                knn_hits = knn_resp.json().get("hits", {}).get("hits", [])
                _collect_place_ids(knn_hits, place_scores, exclude_prefixes)
        else:
            text_body = _build_toponym_query(req.query, req.mode, size=200)
            text_resp = await client.post(
                f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                json=text_body,
                auth=auth,
                headers=ES_HEADERS,
            )
            text_resp.raise_for_status()
            text_hits = text_resp.json().get("hits", {}).get("hits", [])
            _collect_place_ids(text_hits, place_scores, exclude_prefixes)

        if not place_scores:
            return ReconcileResponse()

        # ------------------------------------------------------------------
        # Step 2: Filtering — fetch places by ID + spatial/temporal/ccode
        # ------------------------------------------------------------------

        # Send ALL discovered place_ids — the terms filter is an
        # inverted-index lookup that handles thousands of IDs with
        # negligible cost.  Over-fetch (size * 4) so that re-ranking
        # in Step 4 can surface the best candidates after filters trim.
        fetch_ids = list(place_scores.keys())

        places_body = _build_places_filter(
            place_ids=fetch_ids,
            ccodes=req.ccodes,
            bounds=req.bounds,
            start_year=req.start_year,
            end_year=req.end_year,
            size=req.size * 4,
            exclude_namespaces=req.exclude_namespaces or None,
        )
        places_resp = await client.post(
            f"{ES_BACKEND}/{PLACES_INDEX}/_search",
            json=places_body,
            auth=auth,
            headers=ES_HEADERS,
        )
        places_resp.raise_for_status()
        places_result = places_resp.json()

        raw_hits = places_result.get("hits", {}).get("hits", [])
        surviving_pids = [
            h.get("_source", {}).get("place_id", "") for h in raw_hits
        ]
        surviving_set = set(surviving_pids)

        # ------------------------------------------------------------------
        # Step 3: Enrichment — fetch all toponyms for surviving places
        # ------------------------------------------------------------------

        # place_id → list of {"label": ..., "lang": ...}
        place_toponyms: dict[str, list[dict]] = defaultdict(list)

        if surviving_pids:
            topo_body = _build_toponym_lookup(
                surviving_pids,
                size=max(len(surviving_pids) * 30, 500),
            )
            try:
                topo_resp = await client.post(
                    f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                    json=topo_body,
                    auth=auth,
                    headers=ES_HEADERS,
                )
                topo_resp.raise_for_status()
                topo_hits = topo_resp.json().get("hits", {}).get("hits", [])
                for th in topo_hits:
                    src = th.get("_source", {})
                    label = src.get("name", "")
                    lang = src.get("lang")
                    for pid in src.get("attestations", []):
                        if pid in surviving_set:
                            place_toponyms[pid].append(
                                {"label": label, "lang": lang}
                            )
            except Exception as e:
                # Non-fatal: candidates will fall back to nested place data
                logger.warning(f"Toponym enrichment failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Step 4: Format response, ranking by toponym-match score
    # ------------------------------------------------------------------

    # Re-attach toponym scores and normalise to 0–100
    max_toponym_score = max(place_scores.values()) if place_scores else 1.0

    candidates = []
    for hit in raw_hits:
        pid = hit.get("_source", {}).get("place_id", "")
        raw_score = place_scores.get(pid, 0.0)
        normalised = (raw_score / max_toponym_score * 100) if max_toponym_score > 0 else 0
        # Use step-3 toponyms if available, else _format_candidate falls
        # back to nested toponyms in the places _source.
        toponyms = place_toponyms.get(pid) or None
        candidates.append(
            _format_candidate(hit.get("_source", {}), normalised, toponyms)
        )

    # Sort by score descending (ES returned them in filter order, not ranked)
    candidates.sort(key=lambda c: c.score, reverse=True)
    candidates = candidates[:req.size]

    # ------------------------------------------------------------------
    # Step 5 (optional): Cluster grouping
    # ------------------------------------------------------------------

    cluster_groups: list[ClusterGroup] = []
    if req.group_by_cluster and candidates:
        cluster_groups = await _group_by_cluster(
            [c.place_id for c in candidates],
            {c.place_id: c for c in candidates},
            auth,
        )

    return ReconcileResponse(
        hits=candidates,
        clusters=cluster_groups,
        max_score=candidates[0].score if candidates else 0,
        total=places_result.get("hits", {}).get("total", {}).get("value", len(candidates)),
    )


# ---------------------------------------------------------------------------
# Cluster grouping helper
# ---------------------------------------------------------------------------

async def _group_by_cluster(
    place_ids: list[str],
    candidates_map: dict[str, CandidateHit],
    auth,
) -> list[ClusterGroup]:
    """
    Look up cluster membership for surviving place_ids and group hits.

    Returns a list of ClusterGroup, sorted by representative score descending.
    Unclustered places are returned as singleton groups.
    """
    import httpx
    from collections import defaultdict

    cluster_body = {
        "size": len(place_ids),
        "query": {
            "bool": {
                "filter": [
                    {"term": {"doc_type": "membership"}},
                    {"terms": {"place_id": place_ids}},
                ]
            }
        },
        "_source": ["place_id", "cluster_id", "cluster_size"],
    }

    pid_to_cluster: dict[str, tuple[str, int]] = {}  # pid → (cluster_id, cluster_size)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{ES_BACKEND}/{CLUSTERS_INDEX}/_search",
                json=cluster_body,
                auth=auth,
                headers=ES_HEADERS,
            )
            if resp.status_code == 200:
                for hit in resp.json().get("hits", {}).get("hits", []):
                    src = hit["_source"]
                    pid_to_cluster[src["place_id"]] = (
                        src["cluster_id"],
                        src.get("cluster_size", 1),
                    )
    except Exception as e:
        logger.warning("Cluster lookup failed (non-fatal): %s", e)
        return []

    # Group candidates by cluster_id
    cluster_members: dict[str, list[CandidateHit]] = defaultdict(list)
    cluster_sizes: dict[str, int] = {}
    unclustered: list[CandidateHit] = []

    for pid in place_ids:
        candidate = candidates_map.get(pid)
        if not candidate:
            continue
        if pid in pid_to_cluster:
            cid, csize = pid_to_cluster[pid]
            cluster_members[cid].append(candidate)
            cluster_sizes[cid] = csize
        else:
            unclustered.append(candidate)

    groups: list[ClusterGroup] = []

    for cid, members in cluster_members.items():
        members.sort(key=lambda c: c.score, reverse=True)
        groups.append(ClusterGroup(
            cluster_id=cid,
            cluster_size=cluster_sizes.get(cid, len(members)),
            representative=members[0],
            members=members[1:],
        ))

    # Add unclustered places as singleton groups
    for candidate in unclustered:
        groups.append(ClusterGroup(
            cluster_id=f"singleton_{candidate.place_id}",
            cluster_size=1,
            representative=candidate,
            members=[],
        ))

    # Sort groups by representative score descending
    groups.sort(key=lambda g: g.representative.score, reverse=True)
    return groups


# ---------------------------------------------------------------------------
# Standalone Cluster API Endpoints (§7.3)
# ---------------------------------------------------------------------------

@router.get("/cluster/place/{place_id:path}")
async def get_cluster_for_place(place_id: str):
    """
    Return the full cluster for a given place_id.

    If the place is not in any cluster, returns an empty cluster with
    just that place.
    """
    import httpx

    auth = _es_auth()

    # Look up the place's cluster membership
    body = {
        "size": 1,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"doc_type": "membership"}},
                    {"term": {"place_id": place_id}},
                ]
            }
        },
        "_source": ["cluster_id", "cluster_size"],
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{ES_BACKEND}/{CLUSTERS_INDEX}/_search",
            json=body, auth=auth, headers=ES_HEADERS,
        )
        if resp.status_code != 200:
            return {"place_id": place_id, "cluster_id": None, "members": []}

        hits = resp.json().get("hits", {}).get("hits", [])
        if not hits:
            return {"place_id": place_id, "cluster_id": None, "members": []}

        cluster_id = hits[0]["_source"]["cluster_id"]
        cluster_size = hits[0]["_source"].get("cluster_size", 0)

        # Fetch all members of this cluster
        return await _fetch_cluster_members(
            client, cluster_id, cluster_size, auth
        )


@router.get("/cluster/id/{cluster_id}")
async def get_cluster_by_id(cluster_id: str):
    """Return all members of a cluster by cluster_id."""
    import httpx

    auth = _es_auth()

    async with httpx.AsyncClient(timeout=10) as client:
        return await _fetch_cluster_members(
            client, cluster_id, 0, auth
        )


async def _fetch_cluster_members(
    client,
    cluster_id: str,
    cluster_size: int,
    auth,
) -> dict:
    """Fetch all membership docs for a cluster and return structured response."""
    body = {
        "size": max(cluster_size, 100),
        "query": {
            "bool": {
                "filter": [
                    {"term": {"doc_type": "membership"}},
                    {"term": {"cluster_id": cluster_id}},
                ]
            }
        },
        "_source": ["place_id", "namespace", "cluster_size"],
    }

    resp = await client.post(
        f"{ES_BACKEND}/{CLUSTERS_INDEX}/_search",
        json=body, auth=auth, headers=ES_HEADERS,
    )
    if resp.status_code != 200:
        return {"cluster_id": cluster_id, "members": []}

    hits = resp.json().get("hits", {}).get("hits", [])
    members = [
        {
            "place_id": h["_source"]["place_id"],
            "namespace": h["_source"].get("namespace", ""),
        }
        for h in hits
    ]

    return {
        "cluster_id": cluster_id,
        "cluster_size": len(members),
        "members": members,
    }


