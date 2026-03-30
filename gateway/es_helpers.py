# gateway/es_helpers.py
"""
Shared Elasticsearch query builders and helpers used by both the
reconcile and search endpoints.

Extracted from ``reconcile.py`` to avoid duplication.
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import (
    ES_BACKEND,
    PLACES_INDEX,
    TOPONYMS_INDEX,
    CLUSTERS_INDEX,
    get_elastic_password,
)

logger = logging.getLogger("gateway.es_helpers")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def es_auth():
    """Return (user, password) tuple for ES, or None."""
    password = get_elastic_password()
    if password:
        return ("elastic", password)
    return None


ES_HEADERS = {"Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Step 1 helpers — Toponym discovery
# ---------------------------------------------------------------------------

def build_toponym_query(query: str, mode: str, size: int = 200) -> dict:
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


def build_phonetic_knn(
    query: str,
    lang: str = "und",
    k: int = 200,
    similarity: float = 0.7,
) -> dict | None:
    """
    Build a KNN query body using Symphonym.

    Returns None if Symphonym is unavailable.
    """
    try:
        from . import symphonym
        body = symphonym.build_knn_query(
            name=query, lang=lang, k=k,
            num_candidates=max(k * 2, 400),
        )
        body["knn"]["similarity"] = similarity
        body["_source"] = ["name", "lang", "attestations"]
        return body
    except Exception as e:
        logger.warning(f"Symphonym unavailable for phonetic KNN: {e}")
        return None


def collect_place_ids(
    hits: list[dict],
    place_scores: dict[str, float],
    exclude_prefixes: tuple[str, ...] = (),
) -> None:
    """
    Walk toponym hits and accumulate ``{place_id: best_score}`` from the
    ``attestations`` field.
    """
    for hit in hits:
        score = hit.get("_score", 0.0)
        for pid in hit.get("_source", {}).get("attestations", []):
            if not pid:
                continue
            if exclude_prefixes and pid.startswith(exclude_prefixes):
                continue
            prev = place_scores.get(pid, 0.0)
            if score > prev:
                place_scores[pid] = score


# ---------------------------------------------------------------------------
# Step 2 helpers — Place filtering
# ---------------------------------------------------------------------------

def build_places_filter(
    place_ids: list[str],
    ccodes: list[str] | None,
    bounds: dict | None,
    start_year: int | None,
    end_year: int | None,
    size: int = 50,
    exclude_namespaces: list[str] | None = None,
    extra_source: list[str] | None = None,
) -> dict:
    """
    Build an ES query that fetches places by ID with optional filters.

    Args:
        extra_source: Additional ``_source`` fields beyond the default set.
    """
    filter_clauses: list[dict] = [
        {"terms": {"place_id": place_ids}},
    ]
    must_not_clauses: list[dict] = []

    if exclude_namespaces:
        must_not_clauses.append({"terms": {"namespace": exclude_namespaces}})

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

    bool_query = {"filter": filter_clauses}
    if must_not_clauses:
        bool_query["must_not"] = must_not_clauses

    source_fields = [
        "place_id", "namespace", "title", "ccodes",
        "geometries.repr_point",
    ]
    if extra_source:
        source_fields.extend(extra_source)

    return {
        "size": size,
        "query": {"bool": bool_query},
        "_source": source_fields,
    }


# ---------------------------------------------------------------------------
# Step 3 helpers — Toponym enrichment
# ---------------------------------------------------------------------------

def build_toponym_lookup(place_ids: list[str], size: int = 2000) -> dict:
    """
    Build an ES query to fetch all toponyms attested by the given place_ids.
    """
    return {
        "size": size,
        "query": {
            "terms": {"attestations": place_ids},
        },
        "_source": ["name", "lang", "attestations"],
    }


# ---------------------------------------------------------------------------
# Cluster helpers
# ---------------------------------------------------------------------------

def build_cluster_lookup(place_ids: list[str]) -> dict:
    """Build an ES query to fetch cluster membership for a list of place_ids."""
    return {
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


# ---------------------------------------------------------------------------
# Suggest helper — lightweight toponym-only query
# ---------------------------------------------------------------------------

def build_suggest_query(prefix: str, size: int = 10) -> dict:
    """
    Build a lightweight ES query for typeahead suggestions.

    Queries the ``name.prefix`` (edge_ngram) field with a boost on
    ``name.raw`` for exact matches.  Returns only the ``name`` field
    to minimise payload.
    """
    return {
        "size": size,
        "query": {
            "bool": {
                "should": [
                    {"match": {"name.prefix": {"query": prefix}}},
                    {"term": {"name.raw": {"value": prefix.lower(), "boost": 5}}},
                ],
            }
        },
        "_source": ["name"],
    }

