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
from typing import Literal, Optional

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
    rank_candidate_ids,
    build_lexical_exact_query,
    build_lexical_fuzzy_query,
    apply_lexical_near_miss,
    derive_name_forms,
    derived_form_weight,
    VARIANT_SCORE_WEIGHT,
    knn_pass_quality,
    absolute_confidence,
    KNN_SIMILARITY_FLOOR,
    apply_lexical_boost,
    LEXICAL_EXACT_BOOST,
    build_places_filter,
    build_toponym_lookup,
    build_suggest_query,
    collect_namespaces,
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
    temporal_mode: Literal["possibly", "definitely"] = Field(
        "possibly",
        description="How start_year/end_year are matched: 'possibly' (default) "
                    "admits a place whose bounds allow it to have been alive in "
                    "the window; 'definitely' requires its attested core to fall "
                    "inside it. Sources that record places as they were at one "
                    "moment are possibly, not definitely, alive at earlier dates.",
    )
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
    browse: bool = Field(
        False,
        description="Browse mode. With no query text, return a namespace- (and "
                    "optionally ccode/type/temporal-) filtered match-all, ordered "
                    "alphabetically by title, with a REAL `total` (track_total_hits) "
                    "and ES-level `offset` pagination. Lets a client enumerate a "
                    "whole gazetteer without supplying a query or bounds — the "
                    "Atlas 'Place List' panel. Ignored when a query is present.",
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
    # ABSOLUTE match confidence, 0–100 — the twin of the /api/reconcile field
    # (place#199 C). `score` is normalised by the pool's best, so the top hit is
    # always ~100 even when nothing matched; `confidence` means the same thing
    # for every query. Null outside fuzzy/phonetic discovery.
    confidence: Optional[float] = None
    namespace: str = ""
    # Per-hit clustering fuel — only populated when include_clustering_fields=True
    h3: Optional[str] = None                   # representative H3 centroid cell
    h3_cover: list[str] = []                   # bounded union of H3 cover cells
    # Either element may be null: an unbounded side (an open-start boundary, or an
    # ongoing one) is unknown, not a year — declaring list[int] made every hit
    # carrying one a 500 (place#169).
    temporal_range: Optional[list[Optional[int]]] = None  # [min_start, max_end], either may be null
    temporal_core: Optional[list[Optional[int]]] = None   # [latest_start, earliest_end], or null
    aat_ids: list[int] = []                    # leaf AAT concept ids
    aat_paths: list[str] = []                  # materialised root→leaf AAT paths (ancestors + depth)
    query_match: Optional[dict] = None         # {"name": ..., "score": ...} — the matching toponym


class Facets(BaseModel):
    """Aggregation facets returned alongside search hits."""
    types: list[dict] = []      # [{"identifier": ..., "label": ..., "count": ...}, ...] (raw source types)
    aat_types: list[dict] = []  # [{"aat_id": ..., "label": ..., "count": ...}, ...] (AAT, friendly labels)
    custom_types: list[dict] = []  # [{"identifier": ..., "label": ..., "count": ...}, ...] (source types w/ NO AAT mapping)
    countries: list[dict] = []  # [{"code": ..., "count": ...}, ...]


class SearchResponse(BaseModel):
    hits: list[SearchHit] = []
    total: int = 0
    max_score: float = 0
    # Source-attribution echo (place#157). `namespaces` = the distinct
    # authorities represented in `hits`, so a consumer resolves per-source
    # licence terms in one registry lookup instead of string-splitting every
    # id. `namespaces_searched` = the explicit positive namespace scope the
    # request asked for (empty when unrestricted) — it is the ONLY way to know
    # a namespace was queried but contributed no hits, which id-derivation
    # cannot express.
    namespaces: list[str] = []
    namespaces_searched: list[str] = []
    # Name forms the GATEWAY derived from `query` and searched alongside it —
    # bracketed qualifiers stripped both ways (place#199). Empty unless the query
    # contained brackets. The twin of /api/reconcile's field of the same name.
    derived_forms: list[str] = []
    # How the requested geographic scope was actually applied — populated
    # whenever `contained_in` / `bounds` was sent, `None` otherwise (so an
    # unscoped response is byte-identical to the pre-2026-08-31 shape). The
    # same model /api/reconcile returns; see gateway.spatial.ScopeInfo.
    scope: Optional[spatial.ScopeInfo] = None
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
    # The explicit namespace scope, echoed on EVERY return path — including the
    # empty ones, where "we searched chgis and it matched nothing" is precisely
    # the fact a consumer cannot recover from an empty hit list (place#157).
    ns_searched = list(req.namespaces or [])
    # Browse mode only applies when there is no query — a query always takes the
    # ranked toponym-discovery path (browse is a no-query enumeration).
    browse = req.browse and not has_query
    if not has_query and not req.contained_in and not req.bounds and not browse:
        return SearchResponse(namespaces_searched=ns_searched)
    pure_spatial = not has_query

    # Name forms the gateway derives from the query itself. Search has no
    # `variants` channel, so without these a bracketed query has no route to the
    # record it names (place#199). Empty for anything unbracketed, so the
    # overwhelming majority of requests are byte-identical to before.
    derived_forms = (derive_name_forms(req.query)
                     if has_query and req.mode in ("fuzzy", "phonetic") else [])

    auth = es_auth()

    include_prefixes = tuple(f"{ns}:" for ns in req.namespaces) if req.namespaces else ()
    # An explicit `namespaces` (positive filter) OVERRIDES `exclude_namespaces`
    # (as its field doc states). Otherwise a request for a default-excluded
    # namespace — exclude_namespaces defaults to ["gb"] — is silently dropped by
    # collect_place_ids' exclude even though the caller asked for it. Only apply
    # the exclusion when no explicit include set was given.
    exclude_prefixes = (
        ()
        if req.namespaces
        else tuple(f"{ns}:" for ns in req.exclude_namespaces) if req.exclude_namespaces
        else ()
    )

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
                # A point-only container yields an APPROXIMATE buffered-point
                # region (place#144) rather than being dropped. resolve_region
                # returns None only when nothing resolves at all — not even a
                # seed point — which is a scope we genuinely cannot apply.
                region = await spatial.resolve_region(req.contained_in, client, auth)
            except spatial.RegionError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        elif req.bounds:
            region = spatial.region_from_geojson(req.bounds)

        scope = spatial.build_scope_info(
            region=region, contained_in=req.contained_in, bounds=req.bounds)

        if scope is not None and not scope.applied:
            # FAIL CLOSED. Until 2026-08-31 search answered this case
            # GLOBALLY: `contained_in: ["un:not_a_real_place"]` returned Paris
            # in Turkey and Paris in Gabon, with nothing in the response to say
            # the scope had been dropped (HANDOVER-2026-08-31 §2b). A stale or
            # typo'd place id is indistinguishable from a deliberately wide
            # query at the client, so an unapplicable scope returns nothing plus
            # the reason — the same contract /api/reconcile already honoured.
            logger.info("search: scope requested but not applied — %s", scope.message)
            return SearchResponse(
                namespaces_searched=ns_searched,
                derived_forms=derived_forms,
                scope=scope,
            )

        if pure_spatial and region is None and not req.bounds and not browse:
            return SearchResponse(namespaces_searched=ns_searched, scope=scope)

        # ------------------------------------------------------------------
        # Step 1: Discovery — search toponyms → collect unique place_ids
        # (skipped for a pure-spatial query)
        # ------------------------------------------------------------------

        if not pure_spatial:
            if req.mode in ("fuzzy", "phonetic"):
                # Phonetic KNN, PLUS two lexical passes on the same query.
                # KNN answers "what sounds like this", which demonstrably does
                # not include "the toponym spelled exactly like this" —
                # `Newton with Scales` is indexed yet never entered the
                # 200-candidate KNN pool (place#197) — nor "the toponym spelled
                # ALMOST like this", which was reachable only via whatever the
                # KNN happened to make of it (place#199). All of it runs in one
                # gather, so the lexical halves cost no extra round-trip and
                # still answer when Symphonym is unavailable. Kept in step with
                # /api/reconcile so the two discovery scales cannot drift.
                #
                # Search has no `variants` channel, so a bracketed query used to
                # have NO route to the record it names — the brackets are the
                # asker's apparatus and no toponym is indexed with them. The
                # gateway derives those forms itself; each gets its own KNN pass
                # (embedding spaces differ per form, so they cannot be OR-ed)
                # plus a place in both lexical passes.
                forms: list[tuple[str, float]] = [(req.query, 1.0)]
                forms += [(f, derived_form_weight(req.query, f))
                          for f in derived_forms]
                lex_body = build_lexical_exact_query(
                    [f for f, _ in forms], namespaces=req.namespaces or None)
                fz_body = build_lexical_fuzzy_query(
                    [f for f, _ in forms], namespaces=req.namespaces or None,
                    variant_weight=VARIANT_SCORE_WEIGHT)
                # ORDER MATTERS on the way back: the phonetic passes set each
                # place's base score, the lexical tiers ADD to it.
                requests: list[tuple[str, float, dict]] = []
                for form, weight in forms:
                    knn_body = build_phonetic_knn(
                        form, k=200, similarity=KNN_SIMILARITY_FLOOR)
                    if knn_body:
                        requests.append(("knn", weight, knn_body))
                if fz_body:
                    requests.append(("near", 1.0, fz_body))
                if lex_body:
                    requests.append(("exact", 1.0, lex_body))
                if requests:
                    responses = await asyncio.gather(*[
                        client.post(
                            f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                            json=body, auth=auth, headers=ES_HEADERS,
                        )
                        for _, _, body in requests
                    ])
                    for (kind, weight, _), resp in zip(requests, responses):
                        resp.raise_for_status()
                        hits = resp.json().get("hits", {}).get("hits", [])
                        if kind == "knn":
                            # Normalised per pass so the phonetic band tops out
                            # at 1.0 and the lexical tiers can sit above it, and
                            # because raw cosines to different query vectors are
                            # not comparable (place#197). Scaled by the pass's
                            # ABSOLUTE quality so a pass that matched only
                            # floor-scraping noise says so (place#199 B).
                            collect_place_ids(hits, place_scores, exclude_prefixes,
                                              include_prefixes, match_names,
                                              score_scale=weight * knn_pass_quality(hits),
                                              normalise=True)
                        elif kind == "near":
                            apply_lexical_near_miss(
                                hits, place_scores,
                                {f.strip(): w for f, w in forms},
                                exclude_prefixes, include_prefixes, match_names)
                        else:
                            apply_lexical_boost(
                                hits, place_scores,
                                {f.strip().lower(): LEXICAL_EXACT_BOOST * w
                                 for f, w in forms},
                                exclude_prefixes, include_prefixes, match_names)
            else:
                text_body = build_toponym_query(
                    req.query, req.mode, size=200, namespaces=req.namespaces or None
                )
                text_resp = await client.post(
                    f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                    json=text_body, auth=auth, headers=ES_HEADERS,
                )
                text_resp.raise_for_status()
                text_hits = text_resp.json().get("hits", {}).get("hits", [])
                collect_place_ids(text_hits, place_scores, exclude_prefixes,
                                  include_prefixes, match_names)

            if not place_scores:
                return SearchResponse(namespaces_searched=ns_searched,
                                      derived_forms=derived_forms, scope=scope)

        # ------------------------------------------------------------------
        # Step 2: Filtering + Aggregations — fetch places by ID + filters
        # ------------------------------------------------------------------

        # Candidate pool size — cover the pagination window (offset+size) with a
        # filter-attrition buffer, capped at ES's 10k.
        pool_k = min((req.offset + req.size) * (8 if pure_spatial else 4), 10000)

        # Deterministic candidate pool for STABLE pagination + correct ranking:
        # take the top-K place_ids BY DISCOVERY SCORE (tiebreak place_id), not an
        # arbitrary doc-order page. This makes a larger offset fetch a consistent
        # superset (so pages don't overlap/skip), and fixes a latent quality bug
        # where high-scoring candidates beyond the doc-order fetch window were
        # silently dropped. (hit.score is a monotonic normalisation of
        # place_scores, so this pre-fetch order matches the final ranking.)
        fetch_ids = None if pure_spatial else rank_candidate_ids(place_scores, pool_k)

        places_body = build_places_filter(
            place_ids=fetch_ids,
            ccodes=req.ccodes,
            bounds=(req.bounds if region is None else None),
            region=region,
            start_year=req.start_year,
            end_year=req.end_year,
            undated=req.undated,
            temporal_mode=req.temporal_mode,
            size=pool_k,
            exclude_namespaces=req.exclude_namespaces or None,
            namespaces=req.namespaces,
            fclasses=req.fclasses,
            types=req.types,
            aat_types=req.aat_types,
            extra_source=["types"],  # needed for type facets + hit data
            geom=req.geom,
            clustering_fields=req.include_clustering_fields,
        )

        # Browse: there are no discovery scores to rank on, so page + order the
        # match-all directly in ES — alphabetically by title, tiebroken on the
        # stable place_id — and ask ES for the exact total so the client can show
        # a real gazetteer count and paginate deterministically. The Python
        # re-rank/re-slice below is skipped for browse (the ES page is final).
        if browse:
            places_body["from"] = req.offset
            places_body["size"] = req.size
            places_body["sort"] = [{"title.keyword": "asc"}, {"place_id": "asc"}]
            places_body["track_total_hits"] = True

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
                    # Custom (non-AAT) type facet — source types that carry NO AAT
                    # mapping (place#122). Same shape as by_identifier but filtered
                    # to unmapped types, so the Atlas UI can surface + filter custom
                    # types from authority or contributed gazetteers without the
                    # (unreliable) client-side guess at which source types are AAT.
                    "by_custom": {
                        "filter": {"bool": {"must_not": {
                            "exists": {"field": "types.aat_ids"}
                        }}},
                        "aggs": {
                            "by_identifier": {
                                "terms": {"field": "types.identifier", "size": 50},
                                "aggs": {
                                    "label": {
                                        "terms": {"field": "types.sourceLabel", "size": 1},
                                    }
                                }
                            }
                        }
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
            # The exact path reads real polygons and runs Shapely, so it goes
            # to a worker thread (load_geometry included); fuzzy stays inline.
            raw_hits = await spatial.apply_containment_async(
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
                    for pid in (src.get("attestations") or []):
                        if pid in surviving_set:
                            place_toponyms[pid].append(entry)
            except Exception as e:
                logger.warning(f"Toponym enrichment failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Step 4: Format response — rank by toponym-match score
    # ------------------------------------------------------------------

    max_toponym_score = max(place_scores.values()) if place_scores else 1.0

    # Only the fuzzy/phonetic tiers are on an absolute scale, so only they
    # publish an absolute confidence (place#199 C).
    absolute_scale = (not pure_spatial) and req.mode in ("fuzzy", "phonetic")

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
        for g in (src.get("geometries") or []):
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
            for g in (src.get("geometries") or []):
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
        for t in (src.get("types") or []):
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
            confidence=absolute_confidence(raw_score) if absolute_scale else None,
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
    # Sort by (score, place_id) ONLY — this is the exact same total order used to
    # select the top-K candidate pool, which is what makes offset pagination
    # consistent (a larger pool is a superset whose leading slice is identical).
    # NOTE: the old `len(r.names)` prominence tiebreaker was REMOVED — name counts
    # come from the bounded enrichment step, so they vary with pool size (which
    # grows with offset), which reordered equal-score places and made pages
    # overlap/skip. place_id is the deterministic, pool-independent tiebreak.
    # Browse already paged + ordered (alphabetically) in ES — keep that order and
    # page as-is. The ranked path re-ranks by score here and slices the window.
    if not browse:
        results.sort(key=lambda r: (r.score, r.place_id), reverse=True)
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

    # Custom (non-AAT) type facets (place#122) — source types with no AAT mapping,
    # keyed by source identifier with the source's own sourceLabel.
    custom_agg = (aggs.get("type_facets", {}).get("by_custom", {})
                  .get("by_identifier", {}).get("buckets", []))
    for bucket in custom_agg:
        label_buckets = bucket.get("label", {}).get("buckets", [])
        facets.custom_types.append({
            "identifier": bucket.get("key", ""),
            "label": label_buckets[0]["key"] if label_buckets else "",
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
        namespaces=collect_namespaces(results),
        namespaces_searched=ns_searched,
        derived_forms=derived_forms,
        scope=scope,
        facets=facets,
        edges=edges,
        clustering_params=load_clustering_params() if req.include_clustering_fields else None,
        toponym_stoplist=load_toponym_stoplist() if req.include_clustering_fields else [],
    )

