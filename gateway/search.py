# gateway/search.py
"""
Search and suggest endpoints for the WHG API gateway.

**GET /api/suggest** — Fast typeahead querying only the ``toponyms`` index.
Returns deduplicated name strings, no filters, no place lookups.

**POST /api/search** — Full filtered search reusing the proven three-step
reconcile architecture (Discovery → Filtering → Enrichment) plus a
lightweight cluster-size lookup for prominence ranking.  Adds ES
aggregations for server-side type/country facets.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from .config import (
    ES_BACKEND,
    PLACES_INDEX,
    TOPONYMS_INDEX,
    CLUSTERS_INDEX,
)
from .es_helpers import (
    es_auth,
    ES_HEADERS,
    build_toponym_query,
    build_phonetic_knn,
    collect_place_ids,
    build_places_filter,
    build_toponym_lookup,
    build_cluster_lookup,
    build_suggest_query,
)

logger = logging.getLogger("gateway.search")

router = APIRouter(prefix="/api", tags=["Search"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    """Query shape sent by the WHG Django beta proxy."""

    query: str = Field(..., description="Search text")
    mode: str = Field("fuzzy", description="Search mode: exact | starts | in | fuzzy | phonetic")
    ccodes: Optional[list[str]] = Field(None, description="ISO-3166 country code filter")
    bounds: Optional[dict] = Field(None, description="GeoJSON geometry for spatial filter (intersects)")
    start_year: Optional[int] = Field(None, description="Temporal filter: start year")
    end_year: Optional[int] = Field(None, description="Temporal filter: end year")
    undated: bool = Field(False, description="Include places with no timespans when temporal filter is active")
    size: int = Field(100, ge=1, le=500, description="Max results to return")
    exclude_namespaces: list[str] = Field(
        default=["gb"],
        description="Namespace prefixes to exclude (e.g. ['gb'] to suppress noisy OS records).",
    )
    geom: str = Field(
        "full",
        description=(
            "Geometry detail level: 'full' returns complete GeoJSON geometries "
            "plus repr_point; 'repr_point' returns centroids only (lighter)."
        ),
    )


class SearchHit(BaseModel):
    """A single search result."""
    place_id: str
    title: str
    names: list[dict] = []        # [{"label": ..., "lang": ...}, ...]
    ccodes: list[str] = []
    types: list[dict] = []        # [{"identifier": ..., "label": ..., "sourceLabel": ...}, ...]
    repr_point: Optional[list[float]] = None  # [lon, lat] — always populated when any geometry exists
    geometries: Optional[list[dict]] = None   # Full GeoJSON geoms; only present when geom="full"
    score: float = 0
    namespace: str = ""
    cluster_id: Optional[str] = None
    cluster_size: int = 1


class Facets(BaseModel):
    """Aggregation facets returned alongside search hits."""
    types: list[dict] = []      # [{"identifier": ..., "label": ..., "count": ...}, ...]
    countries: list[dict] = []  # [{"code": ..., "count": ...}, ...]


class SearchResponse(BaseModel):
    hits: list[SearchHit] = []
    total: int = 0
    max_score: float = 0
    facets: Facets = Facets()


class SuggestItem(BaseModel):
    name: str
    score: float = 0


class SuggestResponse(BaseModel):
    suggestions: list[SuggestItem] = []
    total: int = 0


# ---------------------------------------------------------------------------
# GET /api/suggest — fast typeahead on toponyms only
# ---------------------------------------------------------------------------

@router.get("/suggest", response_model=SuggestResponse)
async def suggest(
    q: str = Query(..., min_length=2, description="Prefix to search for"),
    size: int = Query(10, ge=1, le=50, description="Max suggestions"),
):
    """
    Fast typeahead: queries only the ``toponyms`` index using
    ``name.prefix`` (edge_ngram) and ``name.raw`` (exact keyword).
    Returns deduplicated name strings — no filters, no place lookups.
    """
    auth = es_auth()
    body = build_suggest_query(q, size=size * 5)  # over-fetch for dedup

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
            json=body,
            auth=auth,
            headers=ES_HEADERS,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])

    # Deduplicate by lowercased name, keep best score per unique string
    seen: dict[str, tuple[str, float]] = {}  # lower → (original, score)
    for hit in hits:
        name = hit.get("_source", {}).get("name", "")
        score = hit.get("_score", 0.0)
        key = name.lower()
        if key not in seen or score > seen[key][1]:
            seen[key] = (name, score)

    sorted_names = sorted(seen.values(), key=lambda x: x[1], reverse=True)[:size]

    return SuggestResponse(
        suggestions=[SuggestItem(name=n, score=s) for n, s in sorted_names],
        total=len(sorted_names),
    )


# ---------------------------------------------------------------------------
# POST /api/search — full filtered search
# ---------------------------------------------------------------------------

@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    """
    Full filtered search using the three-step architecture:

      **Step 1 — Discovery.**  Search ``toponyms`` for candidate place_ids
      (KNN for fuzzy/phonetic, BM25 for exact/starts/in).

      **Step 2 — Filtering + Aggregations.**  Fetch candidate places from
      ``places`` with spatial/temporal/country-code filters.  Includes
      aggregations on ``types`` and ``ccodes`` for faceted UI.

      **Step 3 — Enrichment.**  Fetch full toponym inventory for surviving
      places, plus cluster membership for prominence ranking.
    """
    if not req.query or not req.query.strip():
        return SearchResponse()

    auth = es_auth()

    exclude_prefixes = tuple(f"{ns}:" for ns in req.exclude_namespaces) if req.exclude_namespaces else ()

    # place_id → best toponym-match score
    place_scores: dict[str, float] = {}

    async with httpx.AsyncClient(timeout=30) as client:

        # ------------------------------------------------------------------
        # Step 1: Discovery — search toponyms → collect unique place_ids
        # ------------------------------------------------------------------

        if req.mode in ("fuzzy", "phonetic"):
            knn_body = build_phonetic_knn(req.query, k=200, similarity=0.7)
            if knn_body:
                knn_resp = await client.post(
                    f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                    json=knn_body, auth=auth, headers=ES_HEADERS,
                )
                knn_resp.raise_for_status()
                knn_hits = knn_resp.json().get("hits", {}).get("hits", [])
                collect_place_ids(knn_hits, place_scores, exclude_prefixes)
        else:
            text_body = build_toponym_query(req.query, req.mode, size=200)
            text_resp = await client.post(
                f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                json=text_body, auth=auth, headers=ES_HEADERS,
            )
            text_resp.raise_for_status()
            text_hits = text_resp.json().get("hits", {}).get("hits", [])
            collect_place_ids(text_hits, place_scores, exclude_prefixes)

        if not place_scores:
            return SearchResponse()

        # ------------------------------------------------------------------
        # Step 2: Filtering + Aggregations — fetch places by ID + filters
        # ------------------------------------------------------------------

        fetch_ids = list(place_scores.keys())

        places_body = build_places_filter(
            place_ids=fetch_ids,
            ccodes=req.ccodes,
            bounds=req.bounds,
            start_year=req.start_year,
            end_year=req.end_year,
            size=req.size * 4,  # over-fetch for re-ranking
            exclude_namespaces=req.exclude_namespaces or None,
            extra_source=["types"],  # needed for type facets + hit data
            geom=req.geom,
        )

        # Add aggregations for faceted UI
        places_body["aggs"] = {
            "type_facets": {
                "nested": {"path": "types"},
                "aggs": {
                    "by_identifier": {
                        "terms": {
                            "field": "types.identifier",
                            "size": 50,
                        },
                        "aggs": {
                            "label": {
                                "terms": {
                                    "field": "types.sourceLabel",
                                    "size": 1,
                                }
                            }
                        }
                    }
                }
            },
            "country_facets": {
                "terms": {
                    "field": "ccodes",
                    "size": 50,
                }
            },
        }

        places_resp = await client.post(
            f"{ES_BACKEND}/{PLACES_INDEX}/_search",
            json=places_body, auth=auth, headers=ES_HEADERS,
        )
        places_resp.raise_for_status()
        places_result = places_resp.json()

        raw_hits = places_result.get("hits", {}).get("hits", [])
        surviving_pids = [
            h.get("_source", {}).get("place_id", "") for h in raw_hits
        ]
        surviving_set = set(surviving_pids)

        # ------------------------------------------------------------------
        # Step 3a: Toponym enrichment — full name inventory
        # ------------------------------------------------------------------

        place_toponyms: dict[str, list[dict]] = defaultdict(list)

        if surviving_pids:
            topo_body = build_toponym_lookup(
                surviving_pids,
                size=max(len(surviving_pids) * 30, 500),
            )
            try:
                topo_resp = await client.post(
                    f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                    json=topo_body, auth=auth, headers=ES_HEADERS,
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
                logger.warning(f"Toponym enrichment failed (non-fatal): {e}")

        # ------------------------------------------------------------------
        # Step 3b: Cluster membership lookup — for prominence ranking
        # ------------------------------------------------------------------

        pid_to_cluster: dict[str, tuple[str, int]] = {}

        if surviving_pids:
            cluster_body = build_cluster_lookup(surviving_pids)
            try:
                cluster_resp = await client.post(
                    f"{ES_BACKEND}/{CLUSTERS_INDEX}/_search",
                    json=cluster_body, auth=auth, headers=ES_HEADERS,
                )
                if cluster_resp.status_code == 200:
                    for ch in cluster_resp.json().get("hits", {}).get("hits", []):
                        csrc = ch["_source"]
                        pid_to_cluster[csrc["place_id"]] = (
                            csrc["cluster_id"],
                            csrc.get("cluster_size", 1),
                        )
            except Exception as e:
                logger.warning(f"Cluster lookup failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Step 4: Format response — rank by toponym-match score
    # ------------------------------------------------------------------

    max_toponym_score = max(place_scores.values()) if place_scores else 1.0

    results: list[SearchHit] = []
    for hit in raw_hits:
        src = hit.get("_source", {})
        pid = src.get("place_id", "")
        raw_score = place_scores.get(pid, 0.0)
        normalised = (raw_score / max_toponym_score * 100) if max_toponym_score > 0 else 0

        # Names from enrichment step
        names = []
        seen_labels: set[str] = set()
        for t in place_toponyms.get(pid, []):
            label = t.get("label", "")
            if label and label not in seen_labels:
                names.append({"label": label, "lang": t.get("lang")})
                seen_labels.add(label)

        # Representative point from first geometry
        repr_point = None
        for g in src.get("geometries", []):
            rp = g.get("repr_point")
            if rp:
                if isinstance(rp, dict):
                    repr_point = [rp.get("lon", 0), rp.get("lat", 0)]
                elif isinstance(rp, list) and len(rp) == 2:
                    repr_point = rp
                break

        # Full geometries — only populated when geom="full"
        full_geoms: list[dict] = []
        if req.geom == "full":
            for g in src.get("geometries", []):
                geom_obj = g.get("geom")
                if isinstance(geom_obj, dict) and geom_obj.get("type") and geom_obj.get("coordinates"):
                    full_geoms.append({
                        "type": geom_obj["type"],
                        "coordinates": geom_obj["coordinates"],
                    })
            # When no explicit geom field was stored, fall back to repr_point
            # so the caller always gets *something* in geometries.
            if not full_geoms and repr_point:
                full_geoms.append({
                    "type": "Point",
                    "coordinates": repr_point,
                })

        # Types from places index
        types = []
        for t in src.get("types", []):
            types.append({
                "identifier": t.get("identifier", ""),
                "label": t.get("label", ""),
                "sourceLabel": t.get("sourceLabel", ""),
            })

        # Cluster info
        cluster_id, cluster_size = pid_to_cluster.get(pid, (None, 1))

        results.append(SearchHit(
            place_id=pid,
            title=src.get("title", ""),
            names=names,
            ccodes=src.get("ccodes", []),
            types=types,
            repr_point=repr_point,
            geometries=full_geoms if full_geoms else None,
            score=normalised,
            namespace=src.get("namespace", ""),
            cluster_id=cluster_id,
            cluster_size=cluster_size,
        ))

    # Sort by score descending, then by cluster_size descending as tiebreaker
    results.sort(key=lambda r: (r.score, r.cluster_size), reverse=True)
    results = results[:req.size]

    # ------------------------------------------------------------------
    # Step 5: Build facets from aggregations
    # ------------------------------------------------------------------

    aggs = places_result.get("aggregations", {})
    facets = Facets()

    # Type facets
    type_agg = aggs.get("type_facets", {}).get("by_identifier", {}).get("buckets", [])
    for bucket in type_agg:
        identifier = bucket.get("key", "")
        count = bucket.get("doc_count", 0)
        label_buckets = bucket.get("label", {}).get("buckets", [])
        label = label_buckets[0]["key"] if label_buckets else ""
        facets.types.append({
            "identifier": identifier,
            "label": label,
            "count": count,
        })

    # Country facets
    country_agg = aggs.get("country_facets", {}).get("buckets", [])
    for bucket in country_agg:
        facets.countries.append({
            "code": bucket.get("key", ""),
            "count": bucket.get("doc_count", 0),
        })

    total = places_result.get("hits", {}).get("total", {}).get("value", len(results))

    return SearchResponse(
        hits=results,
        total=total,
        max_score=results[0].score if results else 0,
        facets=facets,
    )

