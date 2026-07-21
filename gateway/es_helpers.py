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
# Geometry extraction from a place ``_source`` (shared by extend + spatial)
# ---------------------------------------------------------------------------

def extract_place_geoms(src: dict) -> list[dict]:
    """Extract GeoJSON geometry objects from a place ``_source``.

    Handles ES-wrapped (``{"geom": {...}}``), raw GeoJSON, and ``location``
    forms; falls back to a Point built from ``repr_point`` when no full
    geometry is present. Returns ALL geometries (not just the first), so
    multi-geometry places are tested in full.
    """
    geoms: list[dict] = []
    for g in src.get("geometries", []) or []:
        if not isinstance(g, dict):
            continue
        geom_obj = g.get("geom")
        if isinstance(geom_obj, dict) and geom_obj.get("type") and geom_obj.get("coordinates"):
            geoms.append({"type": geom_obj["type"], "coordinates": geom_obj["coordinates"]})
            continue
        if g.get("type") and g.get("coordinates"):
            geoms.append({"type": g["type"], "coordinates": g["coordinates"]})
            continue
        loc = g.get("location")
        if isinstance(loc, dict) and loc.get("type") and loc.get("coordinates"):
            geoms.append({"type": loc["type"], "coordinates": loc["coordinates"]})

    if not geoms:
        rp = extract_repr_point(src)
        if rp:
            geoms.append({"type": "Point", "coordinates": rp})
    return geoms


def extract_repr_point(src: dict) -> list[float] | None:
    """Extract ``[lon, lat]`` from a place's top-level or per-geometry repr_point."""
    rp = src.get("repr_point")
    if rp:
        if isinstance(rp, dict):
            return [rp.get("lon", 0), rp.get("lat", 0)]
        if isinstance(rp, list) and len(rp) == 2:
            return rp
    for g in src.get("geometries", []) or []:
        if not isinstance(g, dict):
            continue
        rp = g.get("repr_point")
        if rp:
            if isinstance(rp, dict):
                return [rp.get("lon", 0), rp.get("lat", 0)]
            if isinstance(rp, list) and len(rp) == 2:
                return rp
    return None


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
        # True infix ("contains") via the 2-3 char n-gram subfields, queried
        # with match_phrase so the query's n-grams must appear *consecutively*
        # — correct substring semantics for any query length, and fast (a normal
        # inverted-index lookup) instead of the old O(N) leading-wildcard on
        # name.raw. REQUIRES the `name.ngram` / `name_romanized.ngram` subfields
        # (toponyms schema); deploy this only against a toponyms index that has
        # them, else `in` returns nothing.
        text_query = {
            "bool": {
                "should": [
                    {"match_phrase": {"name.ngram": {"query": query}}},
                    {"match_phrase": {"name_romanized.ngram": {"query": query}}},
                ],
                "minimum_should_match": 1,
            }
        }
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
    query_vector: list[int] | None = None,
) -> dict | None:
    """
    Build a KNN query body using Symphonym.

    When ``query_vector`` is supplied (a client-computed int8 embedding) the
    server-side embed is skipped. Returns None if Symphonym is unavailable.
    """
    try:
        from . import symphonym
        body = symphonym.build_knn_query(
            name=query, lang=lang, k=k,
            num_candidates=max(k * 2, 400),
            query_vector=query_vector,
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
    include_prefixes: tuple[str, ...] = (),
    match_names: dict[str, str] | None = None,
) -> None:
    """
    Walk toponym hits and accumulate ``{place_id: best_score}`` from the
    ``attestations`` field.

    Args:
        include_prefixes: When non-empty, **only** place_ids starting with
            one of these prefixes are kept (positive namespace filter).
        exclude_prefixes: Place_ids starting with any of these are dropped
            (negative namespace filter).  Applied after the inclusion check.
        match_names: When provided, records ``{place_id: toponym_name}`` for the
            hit that produced each place's *best* score — the ``query_match.name``
            shipped in the clustering fuel. Updated in lock-step with
            ``place_scores`` so name and score always agree.
    """
    for hit in hits:
        score = hit.get("_score", 0.0)
        name = hit.get("_source", {}).get("name", "")
        for pid in hit.get("_source", {}).get("attestations", []):
            if not pid:
                continue
            if include_prefixes and not pid.startswith(include_prefixes):
                continue
            if exclude_prefixes and pid.startswith(exclude_prefixes):
                continue
            prev = place_scores.get(pid, 0.0)
            if score > prev:
                place_scores[pid] = score
                if match_names is not None:
                    match_names[pid] = name


# ---------------------------------------------------------------------------
# Step 2 helpers — Place filtering
# ---------------------------------------------------------------------------


def _has_geometries(bounds: dict) -> bool:
    """Return True only if the GeoJSON geometry contains actual geometry data."""
    if not bounds:
        return False
    geom_type = bounds.get("type", "")
    if geom_type == "GeometryCollection":
        return bool(bounds.get("geometries"))
    return bool(geom_type)
def build_places_filter(
    place_ids: list[str] | None,
    ccodes: list[str] | None,
    bounds: dict | None,
    start_year: int | None,
    end_year: int | None,
    size: int = 50,
    undated: bool = False,
    exclude_namespaces: list[str] | None = None,
    namespaces: list[str] | None = None,
    fclasses: list[str] | None = None,
    types: list[str] | None = None,
    aat_types: list[int] | None = None,
    extra_source: list[str] | None = None,
    geom: str = "full",
    region=None,
    clustering_fields: bool = False,
) -> dict:
    """
    Build an ES query that fetches places by ID with optional filters.

    Args:
        exclude_namespaces: Namespaces to exclude (``must_not`` filter).
        namespaces: When set, **only** these namespaces are returned
            (positive ``filter`` clause).  Takes precedence over
            ``exclude_namespaces`` for any overlap.
        fclasses: GeoNames feature-class letters (e.g. ``["P", "A"]``).
            Translates to a nested ``types.label`` terms filter — GeoNames
            stores the feature class in the ``label`` field of its type entry.
        types: AAT type identifiers (e.g. ``["aat:300008347"]``) or
            source-vocabulary identifiers.  Translates to a nested
            ``types.identifier`` terms filter.
        extra_source: Additional ``_source`` fields beyond the default set.
        geom: ``"full"`` (default) returns ``geometries.geom`` and
            ``geometries.repr_point``; ``"repr_point"`` returns only the
            centroid, keeping responses lightweight for list/suggest views.
        region: An optional resolved containment region (duck-typed; expects
            ``.bbox_geojson`` and ``.h3_terms``). When set, adds a coarse
            spatial gate — ``repr_point`` intersecting the region bbox OR
            ``h3_cover`` matching the region's cells — to pre-trim candidates
            before the precise Python-side containment refine. ``place_ids``
            may be ``None``/empty for a pure-spatial query.
    """
    filter_clauses: list[dict] = []
    if place_ids:
        filter_clauses.append({"terms": {"place_id": place_ids}})
    must_not_clauses: list[dict] = []

    if namespaces:
        filter_clauses.append({"terms": {"namespace": namespaces}})
    elif exclude_namespaces:
        must_not_clauses.append({"terms": {"namespace": exclude_namespaces}})

    if ccodes:
        filter_clauses.append({"terms": {"ccodes": ccodes}})

    # Feature-class filter — nested on types.label (GeoNames stores fclass there)
    if fclasses:
        filter_clauses.append({
            "nested": {
                "path": "types",
                "query": {"terms": {"types.label": fclasses}},
            }
        })

    # Type identifier filter — nested on types.identifier
    if types:
        filter_clauses.append({
            "nested": {
                "path": "types",
                "query": {"terms": {"types.identifier": types}},
            }
        })

    # Hierarchical AAT type filter — a place matches if any type's aat_paths
    # contains the requested concept id (i.e. the concept OR any descendant, since
    # a descendant's materialised path includes its ancestors). AAT ids are
    # distinct 9-digit numbers and path segments are dot-delimited, so a substring
    # wildcard `*<id>*` matches only the exact segment — no false partial matches.
    if aat_types:
        filter_clauses.append({
            "nested": {
                "path": "types",
                "query": {"bool": {
                    "should": [
                        {"wildcard": {"types.aat_paths": f"*{int(aid)}*"}}
                        for aid in aat_types
                    ],
                    "minimum_should_match": 1,
                }},
            }
        })

    if bounds and _has_geometries(bounds):
        # DEGENERATE FALLBACK ONLY. The PRIMARY `bounds` path resolves the raw
        # GeoJSON into a containment region (spatial.region_from_geojson) and is
        # handled by the `region` branch below — an extent-aware `h3_cover`
        # recall gate plus a precise refine in spatial.apply_containment. This
        # branch is reached only when that resolution returned None (Shapely
        # unavailable or malformed/empty `bounds`), in which case the caller
        # passes the raw `bounds` here. All we can do without Shapely/H3 is a
        # `repr_point ∈ bounds` centroid test: coarse (it misses polygons whose
        # representative point lies outside `bounds`), and NOT refined by
        # apply_containment (region is None), but nothing better is available.
        # (`geometries.geom` is no longer a queryable geo_shape post-barrier;
        # `repr_point` is universally populated.)
        filter_clauses.append({
            "nested": {
                "path": "geometries",
                "query": {
                    "geo_shape": {
                        "geometries.repr_point": {
                            "shape": bounds,
                            "relation": "intersects",
                        }
                    }
                },
            }
        })

    # Containment region — coarse spatial gate. repr_point ∈ bbox(R) is cheap
    # and (since repr_point is guaranteed within the geometry) a true-positive
    # signal; the h3_cover terms clause adds recall for large polygons that
    # overlap R far from their repr_point. The precise containment decision is
    # made Python-side in ``spatial.apply_containment``.
    if region is not None:
        should: list[dict] = [
            {
                "nested": {
                    "path": "geometries",
                    "query": {
                        "geo_shape": {
                            "geometries.repr_point": {
                                "shape": region.bbox_geojson,
                                "relation": "intersects",
                            }
                        }
                    },
                }
            }
        ]
        if getattr(region, "h3_terms", None):
            should.append({
                "nested": {
                    "path": "geometries",
                    "query": {"terms": {"geometries.h3_cover": region.h3_terms}},
                }
            })
        filter_clauses.append({"bool": {"should": should, "minimum_should_match": 1}})

    if start_year is not None or end_year is not None:
        TS = "toponyms.timespans"
        temporal_conditions = []
        if start_year is not None:
            # Place still exists at/after the window start: its end is >= start_year
            # under ANY qualifier (in/latest/earliest), OR it is *ongoing* — a
            # timespan with a start but no end (the WHG convention for current
            # features, e.g. the UN country boundaries). Previously only
            # `end.in >= start_year` was checked, which silently dropped every
            # ongoing feature from bounded temporal queries.
            temporal_conditions.append({"bool": {"should": [
                {"range": {f"{TS}.end.in": {"gte": start_year}}},
                {"range": {f"{TS}.end.latest": {"gte": start_year}}},
                {"range": {f"{TS}.end.earliest": {"gte": start_year}}},
                {"bool": {"must_not": [
                    {"exists": {"field": f"{TS}.end.in"}},
                    {"exists": {"field": f"{TS}.end.latest"}},
                    {"exists": {"field": f"{TS}.end.earliest"}},
                ]}},
            ], "minimum_should_match": 1}})
        if end_year is not None:
            # Place started at/before the window end under ANY start qualifier
            # (in/earliest/latest). For an ongoing boundary dated only
            # `start.latest`, this matches queries whose end year is >= that bound.
            temporal_conditions.append({"bool": {"should": [
                {"range": {f"{TS}.start.in": {"lte": end_year}}},
                {"range": {f"{TS}.start.earliest": {"lte": end_year}}},
                {"range": {f"{TS}.start.latest": {"lte": end_year}}},
            ], "minimum_should_match": 1}})
        temporal_match = {
            "nested": {
                "path": "toponyms",
                "query": {
                    "nested": {
                        "path": "toponyms.timespans",
                        "query": {"bool": {"must": temporal_conditions}},
                    }
                },
            }
        }
        if undated:
            # Legacy parity: with `undated`, ALSO admit places carrying no
            # temporal information at all (a place matches if its timespans
            # overlap the range OR it has none) — otherwise a date filter
            # silently drops every undated record.
            no_timespans = {"bool": {"must_not": {
                "nested": {
                    "path": "toponyms",
                    "query": {"nested": {
                        "path": "toponyms.timespans",
                        "query": {"bool": {"should": [
                            {"exists": {"field": "toponyms.timespans.start.in"}},
                            {"exists": {"field": "toponyms.timespans.end.in"}},
                        ]}},
                    }},
                }
            }}}
            filter_clauses.append({"bool": {
                "should": [temporal_match, no_timespans],
                "minimum_should_match": 1,
            }})
        else:
            filter_clauses.append(temporal_match)

    bool_query = {"filter": filter_clauses}
    if must_not_clauses:
        bool_query["must_not"] = must_not_clauses

    geom_fields = (
        ["geometries.geom", "geometries.repr_point", "geometries.has_geom"]
        if geom == "full"
        else ["geometries.repr_point", "geometries.has_geom"]
    )
    # Fields the Python-side containment refine needs when a region is active:
    # h3_cover (fuzzy), repr_point (fast-path / fallback), bounds, and
    # geometry_index (to build the geom-store key "{place_id}_{idx}" for the
    # exact-mode polygon fetch). The full polygon is NOT in _source — exact mode
    # reads it from the /vast geom-store instead.
    if region is not None:
        for f in ("geometries.h3_cover", "geometries.repr_point",
                  "geometries.geometry_index"):
            if f not in geom_fields:
                geom_fields.append(f)
    source_fields = [
        "place_id", "namespace", "title", "ccodes", "links",
        *geom_fields,
    ]
    if extra_source:
        source_fields.extend(extra_source)

    # Per-hit clustering fuel (opt-in): AAT types + H3 cells + timespans, needed
    # by ``clustering_payload.assemble_clustering_fields``. Deduped against the
    # fields already selected above (``types`` may arrive via ``extra_source``;
    # ``geometries.h3_cover`` may arrive via the region gate).
    if clustering_fields:
        from .clustering_payload import CLUSTERING_SOURCE_FIELDS
        for f in CLUSTERING_SOURCE_FIELDS:
            if f not in source_fields:
                source_fields.append(f)

    return {
        "size": size,
        "query": {"bool": bool_query},
        "_source": source_fields,
    }


# ---------------------------------------------------------------------------
# Step 3 helpers — Toponym enrichment
# ---------------------------------------------------------------------------

def build_toponym_lookup(place_ids: list[str], size: int = 2000,
                         with_embeddings: bool = False) -> dict:
    """
    Build an ES query to fetch all toponyms attested by the given place_ids.

    Args:
        with_embeddings: also fetch each toponym's precomputed int8 128-d
            Symphonym ``embedding`` (for the Atlas ``include_embeddings`` path —
            the browser's ``s.n`` name-cosine signal). Off by default to keep the
            enrichment response lightweight.
    """
    source = ["name", "lang", "attestations"]
    if with_embeddings:
        source.append("embedding")
    return {
        "size": size,
        "query": {
            "terms": {"attestations": place_ids},
        },
        "_source": source,
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

