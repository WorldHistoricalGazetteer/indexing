# gateway/reconcile.py
"""
Reconciliation search endpoint for the WHG API gateway.

Accepts a normalised reconciliation query and orchestrates a **three-step**
search across the ``places`` and ``toponyms`` ES indexes:

  1. **Discovery** — Text + optional Symphonym phonetic KNN search on the
     ``toponyms`` index.  Each toponym document carries an ``attestations``
     list of place_ids, so we accumulate a *scored* set of unique candidate
     place_ids (best toponym-match score per place).

  2. **Filtering** — Fetch the candidate places from the ``places`` index
     using a ``terms`` filter on ``place_id`` (keyword → inverted-index
     lookup, extremely fast), layering on optional spatial / temporal /
     country-code filters.  This yields the *surviving* set of place_ids.

  3. **Enrichment** — Query the ``toponyms`` index again, this time with a
     ``terms`` filter on ``attestations`` for the surviving place_ids.
     This retrieves the **full name inventory** (label + lang) for each
     place, regardless of which toponym happened to trigger the KNN match.

The three-step split avoids the isographic-flooding problem inherent in
a single KNN pass: common toponym strings (e.g. "London") that appear in
many (name, lang) combinations consume KNN result slots, but the place_id
deduplication in step 1 still yields a diverse place set, and step 3
recovers all name forms for the surviving places.

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
    PLACES_INDEX,
    TOPONYMS_INDEX,
    get_elastic_password,
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


class ReconcileResponse(BaseModel):
    hits: list[CandidateHit] = []
    max_score: float = 0
    total: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _es_auth():
    password = get_elastic_password()
    if password:
        return ("elastic", password)
    return None


def _build_toponym_query(query: str, mode: str, size: int = 200) -> dict:
    """
    Build an ES query for the toponyms index based on mode.

    Returns an ES search body dict.  The ``_source`` always includes
    ``attestations`` so the caller can collect place_ids directly.
    """
    if mode == "exact":
        text_query = {"term": {"name.keyword": query}}
    elif mode == "starts":
        text_query = {
            "bool": {
                "should": [
                    {"prefix": {"name.keyword": {"value": query.lower()}}},
                    {"match": {"name.prefix": {"query": query}}},
                ]
            }
        }
    elif mode == "in":
        text_query = {"wildcard": {"name.raw": {"value": f"*{query.lower()}*"}}}
    else:
        # "fuzzy" (default) — best-fields multi-match with fuzziness
        text_query = {
            "bool": {
                "should": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["name^3", "name_romanized^2", "name.prefix"],
                            "type": "best_fields",
                            "fuzziness": "AUTO",
                            "prefix_length": 2,
                        }
                    },
                    # Exact keyword boost
                    {"term": {"name.raw": {"value": query.lower(), "boost": 5}}},
                ]
            }
        }

    return {
        "size": size,
        "query": text_query,
        "_source": ["name", "lang", "attestations"],
    }


def _build_phonetic_knn(
    query: str,
    lang: str = "und",
    k: int = 200,
    similarity: float = 0.7,
) -> dict | None:
    """
    Build a KNN query body using Symphonym.

    Overrides ``_source`` to include ``attestations`` so the caller can
    collect place_ids.  Returns None if Symphonym is unavailable.

    Args:
        k: Number of nearest neighbours to fetch.  Set higher than the
            final desired count to compensate for isographic duplicates
            (same spelling in many languages) that consume KNN slots.
        similarity: Minimum cosine similarity threshold (ES scale:
            ``(1 + cos) / 2``, so 0.7 ≈ raw cosine ≥ 0.4).  Prevents
            genuinely dissimilar embeddings from filling result slots.
    """
    try:
        from . import symphonym
        body = symphonym.build_knn_query(
            name=query, lang=lang, k=k,
            num_candidates=max(k * 2, 400),
        )
        # Minimum similarity floor — reject low-quality KNN matches
        body["knn"]["similarity"] = similarity
        # Override _source to include attestations
        body["_source"] = ["name", "lang", "attestations"]
        return body
    except Exception as e:
        logger.warning(f"Symphonym unavailable for phonetic KNN: {e}")
        return None


def _collect_place_ids(hits: list[dict], place_scores: dict[str, float]) -> None:
    """
    Walk toponym hits and accumulate ``{place_id: best_score}`` from the
    ``attestations`` field.  Each toponym hit may reference many places;
    we keep the highest score seen for each place_id.
    """
    for hit in hits:
        score = hit.get("_score", 0.0)
        for pid in hit.get("_source", {}).get("attestations", []):
            if pid:
                prev = place_scores.get(pid, 0.0)
                if score > prev:
                    place_scores[pid] = score


def _build_places_filter(
    place_ids: list[str],
    ccodes: list[str] | None,
    bounds: dict | None,
    start_year: int | None,
    end_year: int | None,
    size: int = 50,
) -> dict:
    """
    Build an ES query that fetches places by ID with optional filters.

    The primary clause is a ``terms`` filter on ``place_id`` (keyword),
    which is a direct inverted-index lookup — the fastest possible query
    in Elasticsearch.  Optional spatial / temporal / country-code filters
    are layered on top.
    """
    filter_clauses: list[dict] = [
        {"terms": {"place_id": place_ids}},
    ]

    if ccodes:
        filter_clauses.append({"terms": {"ccodes": ccodes}})

    if bounds:
        filter_clauses.append({
            "nested": {
                "path": "geometries",
                "query": {
                    "geo_shape": {
                        "geometries.geom": {
                            "shape": bounds,
                            "relation": "intersects",
                        }
                    }
                },
            }
        })

    if start_year is not None or end_year is not None:
        temporal_conditions = []
        if start_year is not None:
            temporal_conditions.append(
                {"range": {"toponyms.timespans.end.in": {"gte": start_year}}}
            )
        if end_year is not None:
            temporal_conditions.append(
                {"range": {"toponyms.timespans.start.in": {"lte": end_year}}}
            )
        filter_clauses.append({
            "nested": {
                "path": "toponyms",
                "query": {
                    "nested": {
                        "path": "toponyms.timespans",
                        "query": {"bool": {"must": temporal_conditions}},
                    }
                },
            }
        })

    return {
        "size": size,
        "query": {"bool": {"filter": filter_clauses}},
        "_source": [
            "place_id", "namespace", "title", "ccodes",
            "geometries.repr_point",
        ],
    }


def _build_toponym_lookup(place_ids: list[str], size: int = 2000) -> dict:
    """
    Build an ES query to fetch all toponyms attested by the given place_ids.

    Used in **Step 3** (enrichment) to retrieve the full name inventory
    for every surviving place.  The ``terms`` filter on ``attestations``
    is an inverted-index lookup — very fast regardless of list length.
    """
    return {
        "size": size,
        "query": {
            "terms": {"attestations": place_ids},
        },
        "_source": ["name", "lang", "attestations"],
    }


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

      **Step 1 — Discovery.**  Text (and optionally phonetic KNN) search
      on the ``toponyms`` index.  Each hit carries an ``attestations``
      list — the ``place_id`` values of every place that uses that name
      form.  We accumulate a *scored* set of candidate place_ids, keeping
      the best toponym-match score per place.  Isographic duplicates
      (same spelling, different language) may consume KNN slots, but the
      place_id dedup ensures the *place* set stays diverse.

      **Step 2 — Filtering.**  Fetch the candidate places from the
      ``places`` index using a ``terms`` filter on ``place_id``
      (keyword → inverted-index lookup, extremely fast).  Optional
      spatial / temporal / country-code filters are applied here.

      **Step 3 — Enrichment.**  Query the ``toponyms`` index again with a
      ``terms`` filter on ``attestations`` for the surviving place_ids.
      This retrieves the **full name inventory** for each place —
      regardless of which toponym triggered the original KNN match —
      giving diverse, multilingual name forms in the response.

    The candidates are ranked by the toponym-match score carried forward
    from step 1.
    """
    import httpx
    from collections import defaultdict

    if not req.query:
        return ReconcileResponse()

    auth = _es_auth()
    headers = {"Content-Type": "application/json"}

    # place_id → best toponym-match score
    place_scores: dict[str, float] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        # ------------------------------------------------------------------
        # Step 1: Discovery — search toponyms → collect unique place_ids
        # ------------------------------------------------------------------

        # 1a. Text search
        text_body = _build_toponym_query(req.query, req.mode, size=200)
        text_resp = await client.post(
            f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
            json=text_body,
            auth=auth,
            headers=headers,
        )
        text_resp.raise_for_status()
        text_hits = text_resp.json().get("hits", {}).get("hits", [])
        _collect_place_ids(text_hits, place_scores)

        # 1b. Phonetic KNN (fuzzy / phonetic modes only)
        if req.mode in ("fuzzy", "phonetic"):
            knn_body = _build_phonetic_knn(req.query, k=200, similarity=0.7)
            if knn_body:
                try:
                    knn_resp = await client.post(
                        f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
                        json=knn_body,
                        auth=auth,
                        headers=headers,
                    )
                    knn_resp.raise_for_status()
                    knn_hits = knn_resp.json().get("hits", {}).get("hits", [])
                    _collect_place_ids(knn_hits, place_scores)
                except Exception as e:
                    logger.warning(f"Phonetic KNN search failed (non-fatal): {e}")

        if not place_scores:
            return ReconcileResponse()

        # ------------------------------------------------------------------
        # Step 2: Filtering — fetch places by ID + spatial/temporal/ccode
        # ------------------------------------------------------------------

        # Sort candidate place_ids by score descending, keep top N for
        # the terms filter (ES terms queries are efficient but we still
        # want to cap the clause count at something reasonable).
        ranked_ids = sorted(place_scores, key=place_scores.get, reverse=True)
        # Over-fetch slightly so filters can trim without losing results
        fetch_ids = ranked_ids[:req.size * 4]

        places_body = _build_places_filter(
            place_ids=fetch_ids,
            ccodes=req.ccodes,
            bounds=req.bounds,
            start_year=req.start_year,
            end_year=req.end_year,
            size=req.size,
        )
        places_resp = await client.post(
            f"{ES_BACKEND}/{PLACES_INDEX}/_search",
            json=places_body,
            auth=auth,
            headers=headers,
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
                    headers=headers,
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

    return ReconcileResponse(
        hits=candidates,
        max_score=candidates[0].score if candidates else 0,
        total=places_result.get("hits", {}).get("total", {}).get("value", len(candidates)),
    )

