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

import asyncio
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
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
    es_auth as _es_auth,
    ES_HEADERS,
    _has_geometries,
    build_toponym_query as _build_toponym_query,
    build_phonetic_knn as _build_phonetic_knn,
    collect_place_ids as _collect_place_ids,
    build_places_filter as _build_places_filter,
    build_toponym_lookup as _build_toponym_lookup,
    collect_namespaces as _collect_namespaces,
)

logger = logging.getLogger("gateway.reconcile")

router = APIRouter(prefix="/api", tags=["Reconciliation"])

# Max name variants honoured per request — bounds discovery fan-out (one extra
# KNN round trip per variant in phonetic modes; one extra should-clause in text
# modes). Anything beyond this is dropped and reported in the response.
MAX_VARIANTS = 10


def _aat_types(types) -> list[int] | None:
    """The AAT concept ids in a `types` list, as integers.

    `types` accepts AAT identifiers OR a source's own vocabulary, but AAT ids were
    being matched literally against `types.identifier` — and almost nothing stores
    them in the form clients send. Getty TGN, which is 100% AAT-typed, stores the
    bare number (`300000774`) plus a numeric `types.aat_ids`, so a filter for
    `aat:300008347` matched none of its 3M records; WHG's own data happens to store
    the `aat:`-prefixed string and so was the only thing an AAT filter could find.

    Routing AAT ids to the `aat_types` filter instead matches `types.aat_paths`,
    which also gives concept-OR-descendant semantics for free — selecting
    "inhabited places" now finds cities and villages, as the Workbench's type
    picker has always implied it would. See WHG place#184.
    """
    if not types:
        return None
    out = []
    for t in types:
        raw = str(t).strip()
        if raw.lower().startswith("aat:"):
            raw = raw[4:]
        if raw.isdigit():
            out.append(int(raw))
    return out or None


def _native_types(types) -> list[str] | None:
    """The non-AAT entries of a `types` list — a source's own vocabulary
    (GeoNames feature codes, Wikidata QIDs), matched literally as before."""
    if not types:
        return None
    out = [str(t) for t in types
           if not str(t).strip().lower().startswith("aat:")
           and not str(t).strip().isdigit()]
    return out or None
# Variant matches are worth marginally less than an equally good match on the
# primary query: the primary is the value the user actually supplied.
VARIANT_SCORE_WEIGHT = 0.9


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ReconcileRequest(BaseModel):
    """Query shape sent by the WHG Django app."""

    query: Optional[str] = Field(None, description="Query toponym string")
    variants: Optional[list[str]] = Field(
        None,
        description="Alternative name forms to ALSO try during discovery (the "
                    "Map-your-Data `alt_names` column). Candidates matching the "
                    "primary query OR any variant are unioned, each place keeping "
                    "its best match; variant matches are scored slightly below an "
                    f"equally good primary match. At most {MAX_VARIANTS} are honoured.",
    )
    variant_vectors: Optional[list[list[int]]] = Field(
        None,
        description="Optional client-computed int8 (128-d) Symphonym embeddings "
                    "for `variants`, positionally aligned. Any variant without a "
                    "vector is embedded server-side (fuzzy/phonetic modes only).",
    )
    mode: str = Field("fuzzy", description="Search mode: exact | starts | in | fuzzy | phonetic")
    ccodes: Optional[list[str]] = Field(None, description="ISO-3166 country code filter")
    fclasses: Optional[list[str]] = Field(
        None,
        description="GeoNames feature-class letters (e.g. ['P', 'A']). "
                    "Filters on the nested types.label field.",
    )
    types: Optional[list[str]] = Field(
        None,
        description="AAT or source-vocabulary type identifiers "
                    "(e.g. ['aat:300008347']). Filters on nested types.identifier.",
    )
    bounds: Optional[dict] = Field(None, description="GeoJSON geometry for spatial filter (intersects)")
    lat: Optional[float] = Field(None, description="Latitude for a radial filter; use with lng + radius")
    lng: Optional[float] = Field(None, description="Longitude for a radial filter; use with lat + radius")
    radius: Optional[float] = Field(
        None,
        description="Radius in km for a radial filter. Resolved as an H3 disc — no "
                    "polygon is built — at a resolution chosen from the radius.",
    )
    contained_in: Optional[list[str]] = Field(
        None,
        description="Place_ids whose geometries define a containment region. "
                    "Candidates are filtered to places spatially contained-in / "
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
    start_year: Optional[int] = Field(None, description="Temporal filter: start year")
    end_year: Optional[int] = Field(None, description="Temporal filter: end year")
    temporal_mode: Literal["possibly", "definitely"] = Field(
        "possibly",
        description="How start_year/end_year are matched: 'possibly' (default) "
                    "or 'definitely' — see /api/search.",
    )
    size: int = Field(50, ge=1, le=500, description="Max results to return")
    exclude_namespaces: list[str] = Field(
        default=["gb"],
        description="Namespace prefixes to exclude from results (e.g. ['gb'] "
                    "to suppress noisy Ordnance Survey records). "
                    "Pass [] to include all namespaces.",
    )
    namespaces: Optional[list[str]] = Field(
        None,
        description="When set, only return results from these namespaces "
                    "(e.g. ['gn', 'tgn']). Overrides exclude_namespaces.",
    )
    query_vector: Optional[list[int]] = Field(
        None,
        description="Client-computed int8 (128-d) Symphonym embedding for the query. "
                    "When supplied with mode='phonetic'/'fuzzy', the gateway uses it "
                    "directly for KNN and skips the server-side embed (offloads that "
                    "cost; also lets the client language-condition the embedding).",
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
        description="When True, attach each candidate name's precomputed int8 "
                    "128-d Symphonym embedding (phon_emb) — the Atlas path for the "
                    "browser's s.n name-cosine signal (no client model). The "
                    "Workbench leaves this False and self-embeds in a worker. "
                    "Heavier payload; off by default.",
    )


class CandidateName(BaseModel):
    label: str
    lang: Optional[str] = None
    phon_emb: Optional[list[int]] = None  # int8 128-d Symphonym embedding (include_embeddings)


class CandidateGeometry(BaseModel):
    repr_point: Optional[list[float]] = Field(
        None, description="Representative point [lon, lat] (always present for a located place).")
    is_area: bool = Field(
        False, description="True iff this candidate is AREAL (a polygon) — i.e. it can serve as a "
                           "`contained_in` containment region / a valid hierarchical parent. Keyed on "
                           "`geom_class == 'area'`. Point and LineString candidates return False.")
    has_geom: bool = Field(
        False, description="True iff the full geometry is retrievable from the store (RETRIEVABILITY, not "
                           "shape). A LineString is `has_geom=true` but `is_area=false`. Use `is_area` to "
                           "pick containment parents; `has_geom` only tells you a stored geometry exists.")


class CandidateHit(BaseModel):
    place_id: str = Field(description="Namespaced ID, e.g. gn:745044")
    title: str
    names: list[CandidateName] = []
    ccodes: list[str] = []
    score: float = 0
    namespace: str = ""
    geometries: list[CandidateGeometry] = []
    links: list[dict] = []  # authority / Wikipedia links from the place _source (e.g. Wikidata sitelinks)
    # Per-hit clustering fuel — only populated when include_clustering_fields=True
    h3: Optional[str] = None
    h3_cover: list[str] = []
    # Either element may be null — see the twin in search.py (place#169).
    temporal_range: Optional[list[Optional[int]]] = None
    temporal_core: Optional[list[Optional[int]]] = None
    aat_ids: list[int] = []
    aat_paths: list[str] = []
    query_match: Optional[dict] = None


class ScopeInfo(BaseModel):
    """How the requested geographic scope was actually applied.

    Present whenever ``contained_in`` / ``bounds`` was sent, so the client can
    warn the user instead of trusting a scope the gateway could not honour
    verbatim (place#144). ``applied=False`` means **no** spatial constraint could
    be built — the request is failed closed (no hits) rather than answered with
    an unscoped result set.
    """
    requested: bool = False
    applied: bool = False
    mode: str = "none"                     # polygon | linked-polygon | geojson
                                           # | bbox | none
    approximate: bool = False              # True when the constraint is coarser
                                           # than the geometry that was asked for
    containers_polygon: list[str] = []     # containers that contributed a real polygon
    containers_linked: list[str] = []      # co-referent (sameAs) places whose polygon was
                                           # borrowed because the container had none
    containers_approximated: list[str] = []  # point-only containers (buffered, or ignored
                                             # when a polygon container was also given)
    containers_unresolved: list[str] = []  # requested ids with no usable geometry at all
    message: Optional[str] = None


class ReconcileResponse(BaseModel):
    hits: list[CandidateHit] = []  # flat ranked candidates (clustering is client-side)
    # Source-attribution echo (place#157). Reconciliation blends candidates from
    # many differently-licensed authorities into one response, so the consumer
    # needs the source set to state per-source terms. `namespaces` = distinct
    # authorities present in `hits` (each candidate also carries its own
    # `namespace`); `namespaces_searched` = the explicit positive scope asked
    # for, the only way to see a namespace that was queried but matched nothing.
    namespaces: list[str] = []
    namespaces_searched: list[str] = []
    scope: Optional[ScopeInfo] = None  # geographic-scope diagnostics (when scope requested)
    variants_used: list[str] = []  # name variants actually queried in discovery (post-cap)
    edges: list[HardLinkEdge] = []  # hard-link co-reference edges (when include_hard_links=True)
    # Offline calibration fuel — populated when include_clustering_fields=True
    clustering_params: Optional[dict] = None
    toponym_stoplist: list[str] = []
    max_score: float = 0
    total: int = 0


# ---------------------------------------------------------------------------
# Internal helpers (reconcile-specific)
# ---------------------------------------------------------------------------


def _normalise_variants(
    req: "ReconcileRequest",
) -> tuple[list[str], list[Optional[list[int]]]]:
    """Clean the requested name variants: strip blanks, drop forms that repeat
    the primary query or an earlier variant (case-insensitively), cap the count,
    and carry each surviving variant's client-supplied embedding (if any).

    Returns ``(variants, vectors)`` — positionally aligned, ``vectors[i]`` None
    when that form must be embedded server-side.
    """
    variants: list[str] = []
    vectors: list[Optional[list[int]]] = []
    if not req.variants:
        return variants, vectors
    seen = {(req.query or "").strip().lower()}
    for i, raw in enumerate(req.variants):
        if not isinstance(raw, str):
            continue
        form = raw.strip()
        if not form or form.lower() in seen:
            continue
        seen.add(form.lower())
        variants.append(form)
        vectors.append(
            req.variant_vectors[i]
            if req.variant_vectors and i < len(req.variant_vectors)
            else None
        )
        if len(variants) >= MAX_VARIANTS:
            break
    return variants, vectors


def _scope_message(region) -> Optional[str]:
    """Human-readable note on any way the region departs from what was asked."""
    if region.source == "linked-polygon":
        return (
            "No container has its own polygon; scope taken from the boundary of "
            f"{', '.join(region.linked_ids) or 'a co-referent place'} "
            "(sameAs/exactMatch)."
        )
    if region.point_ids:
        return (f"{len(region.point_ids)} point-only container(s) ignored — the "
                f"scope is the union of the polygon containers.")
    return None


def _build_scope_info(req: "ReconcileRequest", region) -> Optional["ScopeInfo"]:
    """Describe how the requested geographic scope was applied.

    Returns ``None`` when no scope was requested (response is then byte-identical
    to the pre-place#144 shape). Otherwise the caller MUST honour
    ``applied=False`` by refusing to answer with unscoped results.
    """
    radial = req.lat is not None and req.lng is not None and bool(req.radius)
    if not req.contained_in and not req.bounds and not radial:
        return None

    if region is not None:
        return ScopeInfo(
            requested=True,
            applied=True,
            mode=region.source,
            containers_polygon=list(region.area_ids),
            containers_linked=list(region.linked_ids),
            containers_approximated=list(region.point_ids),
            containers_unresolved=list(region.unresolved_ids),
            message=_scope_message(region),
        )

    if req.bounds and _has_geometries(req.bounds):
        # region_from_geojson failed (Shapely unavailable / unsupported shape) but
        # build_places_filter still applies its degenerate `repr_point ∈ bounds`
        # gate, so the query IS constrained — just coarsely and without a refine.
        return ScopeInfo(
            requested=True, applied=True, mode="bbox", approximate=True,
            message="Bounds could not be resolved into a containment region; "
                    "applied a coarse repr_point-in-bounds filter instead.",
        )

    return ScopeInfo(
        requested=True,
        applied=False,
        mode="none",
        containers_unresolved=[
            spatial._strip_place_prefix(p) for p in (req.contained_in or []) if p],
        message=(
            "None of the containment place_ids resolved to a usable geometry "
            "(not even a representative point), so the requested scope could not "
            "be applied. No results are returned rather than unscoped ones."
            if req.contained_in else
            "The supplied bounds contained no usable geometry, so the requested "
            "scope could not be applied."
        ),
    )


def _format_candidate(
    src: dict,
    score: float,
    toponyms: list[dict] | None = None,
    clustering: dict | None = None,
    query_match: dict | None = None,
) -> CandidateHit:
    """Convert an ES places hit _source into a CandidateHit.

    Args:
        src: ``_source`` dict from the places index hit.
        score: Normalised toponym-match score (0–100).
        toponyms: Optional list of ``{"label": ..., "lang": ...}`` dicts
            from Step 3 (toponym enrichment).  When provided these are
            used instead of the nested ``toponyms`` in the places index.
        clustering: Optional per-hit clustering fuel from
            ``assemble_clustering_fields`` (h3/h3_cover/temporal_range/
            aat_ids/aat_paths). Attached only when clustering fuel is requested.
        query_match: Optional ``{"name": ..., "score": ...}`` — the toponym that
            produced this hit in discovery.
    """
    # Extract names — prefer step-3 enrichment, fall back to nested data
    names: list[CandidateName] = []
    seen_labels: set[str] = set()
    toponym_data = toponyms if toponyms is not None else (src.get("toponyms") or [])
    for t in toponym_data:
        label = t.get("label", "")
        if label and label not in seen_labels:
            names.append(CandidateName(label=label, lang=t.get("lang"),
                                       phon_emb=t.get("phon_emb")))
            seen_labels.add(label)

    # Extract representative points + the areal flag from nested geometries.
    # is_area (geom_class == "area") tells a client which candidates are valid
    # containment parents — a polygon, not a point/line — which repr_point alone
    # can't, and which has_geom (retrievability) gets wrong for LineStrings.
    geometries = []
    for g in (src.get("geometries") or []):
        rp = g.get("repr_point")
        has_geom = bool(g.get("has_geom"))
        is_area = spatial.is_areal(g)
        point = None
        if isinstance(rp, dict):
            point = [rp.get("lon", 0), rp.get("lat", 0)]
        elif isinstance(rp, list) and len(rp) == 2:
            point = rp
        if point or has_geom:
            geometries.append(CandidateGeometry(repr_point=point, is_area=is_area,
                                                has_geom=has_geom))

    return CandidateHit(
        place_id=src.get("place_id", ""),
        title=src.get("title", ""),
        names=names,
        ccodes=src.get("ccodes") or [],
        score=score,
        namespace=src.get("namespace", ""),
        geometries=geometries,
        links=src.get("links") or [],
        query_match=query_match,
        **(clustering or {}),
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
      Any ``variants`` (alternative name forms, e.g. a Map-your-Data
      ``alt_names`` column) are tried **alongside** ``query`` and unioned
      into the same candidate set, at a slight score discount.

      **Step 2 — Filtering.**  Fetch the candidate places from the
      ``places`` index using a ``terms`` filter on ``place_id``.  Optional
      spatial / temporal / country-code / namespace filters are applied.

      **Step 3 — Enrichment.**  Query the ``toponyms`` index again with a
      ``terms`` filter on ``attestations`` for the surviving place_ids.
      This retrieves the **full name inventory** for each place.

    The candidates are ranked by the toponym-match score carried forward
    from step 1.

    **Response.**  A ``ReconcileResponse`` with a flat ``hits`` list of
    ``CandidateHit`` objects (clustering, if wanted, is done client-side from
    the shipped ``edges[]`` + fuel).  Each hit carries ``geometries[]``, where
    every entry has a ``repr_point``, an ``is_area`` flag and a ``has_geom``
    flag. **``is_area=True``** marks a candidate backed by an areal (**polygon**)
    geometry — one usable as a ``contained_in`` region / a valid hierarchical
    parent; point and LineString candidates report ``is_area=False``. ``has_geom``
    is a separate *retrievability* signal (a stored geometry exists) and is
    ``True`` for LineStrings too — use ``is_area``, not ``has_geom``, to pick
    containment parents.

    **Geographic scope is never silently dropped** (place#144).  When
    ``contained_in`` names a container with no polygon, the scope is taken from a
    ``sameAs`` co-referent's boundary where one exists; when no real boundary can
    be found the request is **failed closed** (no hits).  Either way ``scope`` in
    the response records exactly what was applied, so the client can warn the user.
    """
    import httpx
    from collections import defaultdict

    has_query = bool(req.query and req.query.strip())
    # Echoed on every return path, empty ones included (place#157).
    ns_searched = list(req.namespaces or [])
    if not has_query and not req.contained_in and not req.bounds:
        return ReconcileResponse(namespaces_searched=ns_searched)
    pure_spatial = not has_query

    variants, variant_vectors = _normalise_variants(req) if has_query else ([], [])

    auth = _es_auth()

    # Build inclusion prefixes when a positive namespace filter is given
    include_prefixes = tuple(f"{ns}:" for ns in req.namespaces) if req.namespaces else ()

    # An explicit `namespaces` OVERRIDES `exclude_namespaces` (per its field doc):
    # apply the exclusion only when no positive include set was given, else a
    # request for a default-excluded namespace (exclude_namespaces defaults to
    # ["gb"]) is silently dropped despite being explicitly requested.
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
                # A container with no polygon no longer drops the scope:
                # resolve_region falls back to an APPROXIMATE buffered-point
                # region (flagged approximate=True). None now means "not even a
                # seed point" — a scope we genuinely cannot apply.
                region = await spatial.resolve_region(req.contained_in, client, auth)
            except spatial.RegionError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        elif req.lat is not None and req.lng is not None and req.radius:
            # Radial filter as an H3 disc. Answering "near this point" needs no
            # geometry at all — every indexed geometry already carries an H3 cover —
            # so this path builds no polygon, unions nothing and prepares nothing.
            region = spatial.region_from_circle(req.lat, req.lng, req.radius)
        elif req.bounds:
            region = spatial.region_from_geojson(req.bounds)

        scope = _build_scope_info(req, region)

        if scope is not None and not scope.applied:
            # FAIL CLOSED. A scope was explicitly requested but no spatial
            # constraint of any kind could be built, so answering with the
            # unconstrained result set would silently ignore it (place#144).
            # Return nothing plus the reason, and let the client warn the user.
            logger.info("reconcile: scope requested but not applied — %s", scope.message)
            return ReconcileResponse(
                scope=scope, variants_used=variants,
                namespaces_searched=ns_searched,
            )

        if pure_spatial and region is None and not req.bounds:
            return ReconcileResponse(
                scope=scope, variants_used=variants,
                namespaces_searched=ns_searched,
            )

        # ------------------------------------------------------------------
        # Step 1: Discovery — search toponyms → collect unique place_ids
        # (skipped for a pure-spatial query)
        # ------------------------------------------------------------------

        if not pure_spatial:
            if req.mode in ("fuzzy", "phonetic"):
                # One KNN pass per name form. The embedding space differs per
                # form, so (unlike the text path) these cannot be OR-ed into a
                # single query — but they are independent, so run them together
                # and union the results, each place keeping its best score.
                passes: list[tuple[str, Optional[list[int]], float]] = [
                    (req.query, req.query_vector, 1.0)
                ]
                passes += [
                    (form, vec, VARIANT_SCORE_WEIGHT)
                    for form, vec in zip(variants, variant_vectors)
                ]
                bodies = [
                    (_build_phonetic_knn(form, k=200, similarity=0.7, query_vector=vec), weight)
                    for form, vec, weight in passes
                ]
                bodies = [(b, w) for b, w in bodies if b]
                if bodies:
                    responses = await asyncio.gather(*[
                        client.post(
                            f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                            json=body,
                            auth=auth,
                            headers=ES_HEADERS,
                        )
                        for body, _ in bodies
                    ])
                    for (_, weight), knn_resp in zip(bodies, responses):
                        knn_resp.raise_for_status()
                        knn_hits = knn_resp.json().get("hits", {}).get("hits", [])
                        _collect_place_ids(knn_hits, place_scores, exclude_prefixes,
                                           include_prefixes, match_names,
                                           score_scale=weight)
            else:
                # Text modes OR the variants into ONE request (dis_max keeps the
                # best single-form score per toponym rather than summing).
                text_body = _build_toponym_query(
                    req.query, req.mode,
                    # Widen the discovery window so the extra forms don't squeeze
                    # each other out of a fixed top-200 (cf. the #127 symptom).
                    size=min(200 * (1 + len(variants)), 800),
                    namespaces=req.namespaces or None,
                    variants=variants or None, variant_weight=VARIANT_SCORE_WEIGHT,
                )
                text_resp = await client.post(
                    f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                    json=text_body,
                    auth=auth,
                    headers=ES_HEADERS,
                )
                text_resp.raise_for_status()
                text_hits = text_resp.json().get("hits", {}).get("hits", [])
                _collect_place_ids(text_hits, place_scores, exclude_prefixes,
                                   include_prefixes, match_names)

            if not place_scores:
                return ReconcileResponse(
                scope=scope, variants_used=variants,
                namespaces_searched=ns_searched,
            )

        # ------------------------------------------------------------------
        # Step 2: Filtering — fetch places by ID + spatial/temporal/ccode
        # ------------------------------------------------------------------

        # Send ALL discovered place_ids — the terms filter is an
        # inverted-index lookup that handles thousands of IDs with
        # negligible cost.  Over-fetch (size * 4) so that re-ranking
        # in Step 4 can surface the best candidates after filters trim.
        fetch_ids = list(place_scores.keys()) if not pure_spatial else None

        places_body = _build_places_filter(
            place_ids=fetch_ids,
            ccodes=req.ccodes,
            bounds=(req.bounds if region is None else None),
            region=region,
            start_year=req.start_year,
            end_year=req.end_year,
            temporal_mode=req.temporal_mode,
            size=min(req.size * (8 if pure_spatial else 4), 10000),
            exclude_namespaces=req.exclude_namespaces or None,
            namespaces=req.namespaces,
            fclasses=req.fclasses,
            types=_native_types(req.types),
            aat_types=_aat_types(req.types),
            clustering_fields=req.include_clustering_fields,
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

        # --------------------------------------------------------------
        # Step 2.5: Precise containment refine (fuzzy H3 / exact Shapely)
        # --------------------------------------------------------------
        if region is not None:
            reader = None
            if req.containment == "exact":
                reader = spatial.get_geom_reader()
            # See search.py: exact runs off the event loop, fuzzy inline.
            raw_hits = await spatial.apply_containment_async(
                raw_hits, region, req.containment, req.relation, reader=reader,
            )
            raw_hits = raw_hits[: req.size * 4]

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
                with_embeddings=req.include_embeddings,
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
                    entry = {"label": label, "lang": lang}
                    if req.include_embeddings and src.get("embedding"):
                        entry["phon_emb"] = src["embedding"]
                    for pid in (src.get("attestations") or []):
                        if pid in surviving_set:
                            place_toponyms[pid].append(entry)
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
        src = hit.get("_source", {})
        pid = src.get("place_id", "")
        raw_score = place_scores.get(pid, 0.0)
        normalised = (raw_score / max_toponym_score * 100) if max_toponym_score > 0 else 0
        # Use step-3 toponyms if available, else _format_candidate falls
        # back to nested toponyms in the places _source.
        toponyms = place_toponyms.get(pid) or None

        clustering = None
        query_match = None
        if req.include_clustering_fields:
            clustering = assemble_clustering_fields(src)
            matched = match_names.get(pid)
            query_match = {"name": matched, "score": normalised} if matched else None

        candidates.append(
            _format_candidate(src, normalised, toponyms, clustering, query_match)
        )

    # Sort by score descending (ES returned them in filter order, not ranked)
    candidates.sort(key=lambda c: c.score, reverse=True)
    candidates = candidates[:req.size]

    # ------------------------------------------------------------------
    # Step 5 (optional): Hard-link expansion — ship co-reference edges
    # ------------------------------------------------------------------
    # (Server-side cluster grouping was retired 2026-07-12 — reconciliation
    #  returns flat ranked candidates; the consumer clusters client-side.)

    edges: list[HardLinkEdge] = []
    if req.include_hard_links and candidates:
        try:
            edges = await asyncio.to_thread(
                expand_hard_links, [c.place_id for c in candidates])
        except Exception as e:  # best-effort enrichment — never fail the query
            logger.warning("Hard-link expansion failed (non-fatal): %s", e)

    return ReconcileResponse(
        hits=candidates,
        namespaces=_collect_namespaces(candidates),
        namespaces_searched=ns_searched,
        scope=scope,
        variants_used=variants,
        edges=edges,
        clustering_params=load_clustering_params() if req.include_clustering_fields else None,
        toponym_stoplist=load_toponym_stoplist() if req.include_clustering_fields else [],
        max_score=candidates[0].score if candidates else 0,
        total=places_result.get("hits", {}).get("total", {}).get("value", len(candidates)),
    )

