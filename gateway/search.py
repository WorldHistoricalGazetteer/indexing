# gateway/search.py
"""
Search and suggest endpoints for the WHG API gateway.

**GET /api/suggest** — Fast typeahead querying only the ``toponyms`` index.
Returns deduplicated name strings, no filters, no place lookups.

**POST /api/search** — Full filtered search reusing the proven three-step
reconcile architecture (Discovery → Filtering → Enrichment).  Adds ES
aggregations for server-side type/country facets.  Ranks by toponym-match
score, tiebroken by name-variant count (the legacy `clusters`-index
`cluster_size` prominence lookup was retired with client-side clustering).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Optional

import httpx
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field

from . import spatial
from .hard_link_expansion import HardLinkEdge, expand_hard_links
from .clustering_payload import (
    assemble_clustering_fields,
    load_clustering_params,
    load_toponym_stoplist,
)
from .config import (
    ES_BACKEND,
    PLACES_INDEX,
    TOPONYMS_INDEX,
)
from .es_helpers import (
    es_auth,
    ES_HEADERS,
    build_toponym_query,
    build_phonetic_knn,
    collect_place_ids,
    build_places_filter,
    build_toponym_lookup,
    build_suggest_query,
)

logger = logging.getLogger("gateway.search")

router = APIRouter(prefix="/api", tags=["Search"])

# The AAT hierarchy index (alias). Holds one doc per AAT concept with `aat_id`
# + `term` (friendly label) + `path`. Used to label the AAT type facets.
TYPES_INDEX = "types"


async def _resolve_aat_labels(aat_ids: list[int], auth) -> dict[int, str]:
    """``{aat_id: term}`` friendly labels from the `types` index (best-effort)."""
    if not aat_ids:
        return {}
    body = {
        "size": len(aat_ids),
        "query": {"terms": {"aat_id": aat_ids}},
        "_source": ["aat_id", "term"],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{ES_BACKEND}/{TYPES_INDEX}/_search",
                json=body, auth=auth, headers=ES_HEADERS,
            )
            resp.raise_for_status()
            return {
                h["_source"]["aat_id"]: (h["_source"].get("term") or "")
                for h in resp.json().get("hits", {}).get("hits", [])
            }
    except Exception as e:  # non-fatal — facet falls back to bare ids
        logger.warning("AAT label resolution failed (non-fatal): %s", e)
        return {}


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    """Query shape sent by the WHG Django beta proxy."""

    query: Optional[str] = Field(
        None,
        description="Search text. Optional — omit for a pure-spatial query "
                    "(must then supply contained_in or bounds).",
    )
    mode: str = Field("fuzzy", description="Search mode: exact | starts | in | fuzzy | phonetic")
    ccodes: Optional[list[str]] = Field(None, description="ISO-3166 country code filter")
    contained_in: Optional[list[str]] = Field(
        None,
        description="Place_ids whose geometries define a containment region. "
                    "Results are filtered to places spatially contained-in / "
                    "intersecting the union of those geometries.",
    )
    containment: str = Field(
        "fuzzy",
        description="Containment test for contained_in/bounds: 'fuzzy' (H3 "
                    "cell-based, fast, tolerant) | 'exact' (Shapely geometry).",
    )
    relation: str = Field(
        "intersects",
        description="Spatial relation: 'intersects' (any overlap, default) | "
                    "'within' (candidate geometry fully inside the region).",
    )
    fclasses: Optional[list[str]] = Field(
        None,
        description="GeoNames feature-class letters (e.g. ['P', 'A']). "
                    "Filters on the nested types.label field.",
    )
    types: Optional[list[str]] = Field(
        None,
        description="Source-vocabulary type identifiers (e.g. ['city', 'Q515']). "
                    "Filters on nested types.identifier (exact source type).",
    )
    aat_types: Optional[list[int]] = Field(
        None,
        description="AAT concept ids (e.g. [300008347]). HIERARCHICAL: matches "
                    "places whose type is that concept OR any descendant of it "
                    "(via types.aat_paths). Use with the aat_types facet for a "
                    "friendly, cross-source type filter.",
    )
    bounds: Optional[dict] = Field(None, description="GeoJSON geometry for spatial filter (intersects)")
    start_year: Optional[int] = Field(None, description="Temporal filter: start year")
    end_year: Optional[int] = Field(None, description="Temporal filter: end year")
    undated: bool = Field(False, description="Include places with no timespans when temporal filter is active")
    size: int = Field(100, ge=1, le=500, description="Max results to return")
    offset: int = Field(
        0, ge=0, le=10000,
        description="Pagination offset into the ranked result list (0-based). "
                    "Returns hits [offset, offset+size). The pipeline scores + "
                    "re-ranks in the gateway (not an ES sort), so pagination is "
                    "offset-based rather than search_after; `total` reports the "
                    "full candidate count for page-count math. Practical depth is "
                    "bounded by the candidate over-fetch window (~a few thousand).",
    )
    exclude_namespaces: list[str] = Field(
        default=["gb"],
        description="Namespace prefixes to exclude (e.g. ['gb'] to suppress noisy OS records).",
    )
    namespaces: Optional[list[str]] = Field(
        None,
        description="When set, only return results from these namespaces "
                    "(e.g. ['gn', 'tgn']). Overrides exclude_namespaces.",
    )
    geom: str = Field(
        "full",
        description=(
            "Geometry detail level: 'full' returns complete GeoJSON geometries "
            "plus repr_point; 'repr_point' returns centroids only (lighter)."
        ),
    )
    include_hard_links: bool = Field(
        default=False,
        description="When True, ship the co-reference hard-link edges "
                    "(sameAs/exactMatch/closeMatch/distinct) touching the result "
                    "set — the union of the batch overlay + live-delta, deduped. "
                    "Fuel for the browser-side scorer/clustering; additive "
                    "(empty when no overlay is present). Off by default.",
    )
    include_clustering_fields: bool = Field(
        default=False,
        description="When True, add per-hit clustering fuel — h3, h3_cover, "
                    "temporal_range, aat_ids, aat_paths, query_match{name,score} — "
                    "consumed by the browser-side scorer (s.sp/s.t/s.ty signals). "
                    "Additive; off by default (responses are byte-identical when "
                    "unset). Orthogonal to include_hard_links.",
    )
    include_embeddings: bool = Field(
        default=False,
        description="When True, attach each name's precomputed int8 128-d "
                    "Symphonym embedding (phon_emb) — the Atlas path for the "
                    "browser's s.n name-cosine signal (no client model). Heavier "
                    "payload; off by default.",
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
    # Per-hit clustering fuel — only populated when include_clustering_fields=True
    h3: Optional[str] = None                   # representative H3 centroid cell
    h3_cover: list[str] = []                   # bounded union of H3 cover cells
    temporal_range: Optional[list[int]] = None  # [min_start, max_end] years, or null
    aat_ids: list[int] = []                    # leaf AAT concept ids
    aat_paths: list[str] = []                  # materialised root→leaf AAT paths (ancestors + depth)
    query_match: Optional[dict] = None         # {"name": ..., "score": ...} — the matching toponym


class Facets(BaseModel):
    """Aggregation facets returned alongside search hits."""
    types: list[dict] = []      # [{"identifier": ..., "label": ..., "count": ...}, ...] (raw source types)
    aat_types: list[dict] = []  # [{"aat_id": ..., "label": ..., "count": ...}, ...] (AAT, friendly labels)
    countries: list[dict] = []  # [{"code": ..., "count": ...}, ...]


class SearchResponse(BaseModel):
    hits: list[SearchHit] = []
    total: int = 0
    max_score: float = 0
    facets: Facets = Facets()
    edges: list[HardLinkEdge] = []  # hard-link co-reference edges (when include_hard_links=True)
    # Offline calibration fuel — populated when include_clustering_fields=True
    clustering_params: Optional[dict] = None
    toponym_stoplist: list[str] = []


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

      **Step 3 — Enrichment.**  Fetch the full toponym inventory for surviving
      places (optionally with per-name embeddings).
    """
    has_query = bool(req.query and req.query.strip())
    if not has_query and not req.contained_in and not req.bounds:
        return SearchResponse()
    pure_spatial = not has_query

    auth = es_auth()

    exclude_prefixes = tuple(f"{ns}:" for ns in req.exclude_namespaces) if req.exclude_namespaces else ()
    include_prefixes = tuple(f"{ns}:" for ns in req.namespaces) if req.namespaces else ()

    # place_id → best toponym-match score
    place_scores: dict[str, float] = {}
    # place_id → matching toponym name (only tracked when clustering fuel wanted)
    match_names: dict[str, str] = {} if req.include_clustering_fields else None

    async with httpx.AsyncClient(timeout=30) as client:

        # ------------------------------------------------------------------
        # Step 0: Resolve containment region (contained_in place_ids or bounds)
        # ------------------------------------------------------------------

        region = None
        if req.contained_in:
            try:
                # resolve_region returns None when no id yields a usable AREA
                # geometry (point-only / unresolvable) — the query then runs
                # unconstrained, exactly as if contained_in were omitted.
                region = await spatial.resolve_region(req.contained_in, client, auth)
            except spatial.RegionError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        elif req.bounds:
            region = spatial.region_from_geojson(req.bounds)

        if pure_spatial and region is None and not req.bounds:
            return SearchResponse()

        # ------------------------------------------------------------------
        # Step 1: Discovery — search toponyms → collect unique place_ids
        # (skipped for a pure-spatial query)
        # ------------------------------------------------------------------

        if not pure_spatial:
            if req.mode in ("fuzzy", "phonetic"):
                knn_body = build_phonetic_knn(req.query, k=200, similarity=0.7)
                if knn_body:
                    knn_resp = await client.post(
                        f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                        json=knn_body, auth=auth, headers=ES_HEADERS,
                    )
                    knn_resp.raise_for_status()
                    knn_hits = knn_resp.json().get("hits", {}).get("hits", [])
                    collect_place_ids(knn_hits, place_scores, exclude_prefixes,
                                      include_prefixes, match_names)
            else:
                text_body = build_toponym_query(req.query, req.mode, size=200)
                text_resp = await client.post(
                    f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                    json=text_body, auth=auth, headers=ES_HEADERS,
                )
                text_resp.raise_for_status()
                text_hits = text_resp.json().get("hits", {}).get("hits", [])
                collect_place_ids(text_hits, place_scores, exclude_prefixes,
                                  include_prefixes, match_names)

            if not place_scores:
                return SearchResponse()

        # ------------------------------------------------------------------
        # Step 2: Filtering + Aggregations — fetch places by ID + filters
        # ------------------------------------------------------------------

        fetch_ids = list(place_scores.keys()) if not pure_spatial else None

        places_body = build_places_filter(
            place_ids=fetch_ids,
            ccodes=req.ccodes,
            bounds=(req.bounds if region is None else None),
            region=region,
            start_year=req.start_year,
            end_year=req.end_year,
            undated=req.undated,
            # over-fetch more for pure-spatial (region refine drops candidates);
            # cover the pagination window (offset+size), capped at ES's 10k.
            size=min((req.offset + req.size) * (8 if pure_spatial else 4), 10000),
            exclude_namespaces=req.exclude_namespaces or None,
            namespaces=req.namespaces,
            fclasses=req.fclasses,
            types=req.types,
            aat_types=req.aat_types,
            extra_source=["types"],  # needed for type facets + hit data
            geom=req.geom,
            clustering_fields=req.include_clustering_fields,
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
                    },
                    # AAT-based type facets — aggregate on the mapped AAT concept
                    # ids (cross-source, friendly labels resolved from the `types`
                    # index post-query). Replaces the raw-identifier facets in the
                    # AAT-aware UI (§7).
                    "by_aat": {
                        "terms": {"field": "types.aat_ids", "size": 60},
                    },
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

        # --------------------------------------------------------------
        # Step 2.5: Precise containment refine (fuzzy H3 / exact Shapely)
        # --------------------------------------------------------------
        if region is not None:
            reader = None
            if req.containment == "exact":
                reader = spatial.get_geom_reader()
                region.load_geometry(reader)  # no-op for bounds-built regions
            raw_hits = spatial.apply_containment(
                raw_hits, region, req.containment, req.relation, reader=reader,
            )
            # keep enrichment bounded, but cover the pagination window
            raw_hits = raw_hits[: (req.offset + req.size) * 4]

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
                with_embeddings=req.include_embeddings,
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
                    entry = {"label": label, "lang": lang}
                    if req.include_embeddings and src.get("embedding"):
                        entry["phon_emb"] = src["embedding"]
                    for pid in src.get("attestations", []):
                        if pid in surviving_set:
                            place_toponyms[pid].append(entry)
            except Exception as e:
                logger.warning(f"Toponym enrichment failed (non-fatal): {e}")

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
                entry = {"label": label, "lang": t.get("lang")}
                if req.include_embeddings and t.get("phon_emb"):
                    entry["phon_emb"] = t["phon_emb"]
                names.append(entry)
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

        hit_kwargs = dict(
            place_id=pid,
            title=src.get("title", "") or "",
            names=names,
            ccodes=src.get("ccodes") or [],  # _source may carry ccodes: null
            types=types,
            repr_point=repr_point,
            geometries=full_geoms if full_geoms else None,
            score=normalised,
            namespace=src.get("namespace", ""),
        )

        # Per-hit clustering fuel (opt-in) — h3/temporal/aat from _source plus
        # the query_match captured in discovery.
        if req.include_clustering_fields:
            hit_kwargs.update(assemble_clustering_fields(src))
            matched = match_names.get(pid)
            hit_kwargs["query_match"] = (
                {"name": matched, "score": normalised} if matched else None
            )

        results.append(SearchHit(**hit_kwargs))

    # Sort by score descending, then by name-variant count as the prominence
    # tiebreaker. (The legacy `clusters`-index `cluster_size` tiebreaker was
    # retired 2026-07-12 with client-side clustering — plan §1. Name-variant
    # count is a cheap, already-available prominence proxy: well-attested places
    # carry more name forms across languages, which is exactly the "more name
    # variants rank higher" behaviour the search UI documents.)
    results.sort(key=lambda r: (r.score, len(r.names)), reverse=True)
    # Offset pagination on the ranked list: return the [offset, offset+size) page.
    results = results[req.offset : req.offset + req.size]

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

    # AAT type facets — aggregate on the mapped AAT concept ids and resolve
    # friendly labels from the `types` index (one lookup). Gives the AAT-aware
    # type filter its human-readable, cross-source facet (§7).
    aat_buckets = aggs.get("type_facets", {}).get("by_aat", {}).get("buckets", [])
    if aat_buckets:
        aat_labels = await _resolve_aat_labels([b["key"] for b in aat_buckets], auth)
        for bucket in aat_buckets:
            aid = bucket.get("key")
            facets.aat_types.append({
                "aat_id": aid,
                "label": aat_labels.get(aid, str(aid)),
                "count": bucket.get("doc_count", 0),
            })

    # Country facets
    country_agg = aggs.get("country_facets", {}).get("buckets", [])
    for bucket in country_agg:
        facets.countries.append({
            "code": bucket.get("key", ""),
            "count": bucket.get("doc_count", 0),
        })

    # When a containment region is active the ES total counts pre-refine
    # candidates; report the post-refine survivor count instead (within the
    # over-fetch window — facets/aggregations still reflect the candidate set).
    if region is not None:
        total = len(surviving_pids)
    else:
        total = places_result.get("hits", {}).get("total", {}).get("value", len(results))

    # ------------------------------------------------------------------
    # Step 6 (optional): Hard-link expansion — ship co-reference edges
    # ------------------------------------------------------------------

    edges: list[HardLinkEdge] = []
    if req.include_hard_links and results:
        try:
            edges = await asyncio.to_thread(
                expand_hard_links, [r.place_id for r in results])
        except Exception as e:  # best-effort enrichment — never fail the query
            logger.warning("Hard-link expansion failed (non-fatal): %s", e)

    return SearchResponse(
        hits=results,
        total=total,
        max_score=results[0].score if results else 0,
        facets=facets,
        edges=edges,
        clustering_params=load_clustering_params() if req.include_clustering_fields else None,
        toponym_stoplist=load_toponym_stoplist() if req.include_clustering_fields else [],
    )

