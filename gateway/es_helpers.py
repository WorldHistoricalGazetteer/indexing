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
# Source attribution — which authorities does a result set draw on?
# ---------------------------------------------------------------------------

def collect_namespaces(records) -> list[str]:
    """Return the sorted set of distinct namespaces represented in ``records``.

    Every multi-record response echoes this at its root (place#157) so a
    consumer can resolve each source's licence terms in ONE registry lookup
    instead of re-deriving the set by string-splitting every result id. The
    gateway already knows the set per query, so the echo is both cheaper and
    robust against any future change to the ``{ns}:{id}`` id format.

    ``records`` may be response models (``SearchHit`` / ``CandidateHit`` /
    ``PlaceDetail``, all of which carry ``namespace`` + ``place_id``) or raw
    ES ``_source`` dicts. The stored ``namespace`` field is authoritative;
    the ``place_id`` prefix is only a fallback for a doc indexed before the
    ``extract_namespace`` pipeline populated it.
    """
    out: set[str] = set()
    for rec in records:
        if isinstance(rec, dict):
            ns = rec.get("namespace") or ""
            pid = rec.get("place_id") or ""
        else:
            ns = getattr(rec, "namespace", "") or ""
            pid = getattr(rec, "place_id", "") or ""
        if not ns and ":" in pid:
            ns = pid.split(":", 1)[0]
        if ns:
            out.add(ns)
    return sorted(out)


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

def build_toponym_text_clause(query: str, mode: str) -> dict:
    """Return the bare ES query clause for one toponym string in one mode.

    Split out of ``build_toponym_query`` so several name forms (a primary query
    plus its variants) can be OR-ed into a single discovery request.
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

    return text_query


def build_toponym_query(
    query: str, mode: str, size: int = 200, namespaces: list[str] | None = None,
    variants: list[str] | None = None, variant_weight: float = 0.9,
) -> dict:
    """
    Build an ES query for the toponyms index based on mode.

    Returns an ES search body dict.  The ``_source`` always includes
    ``attestations`` so the caller can collect place_ids directly.

    ``namespaces`` (namespace *codes*, e.g. ``["iv"]``) pushes a
    ``terms`` filter on the toponym ``namespaces`` field INTO discovery, so the
    top-``size`` window is drawn only from the requested namespaces. Without it,
    a narrow namespace's matches for a common substring get squeezed out of the
    global top-``size`` and post-filtering yields nothing (the #127 symptom).

    ``variants`` are alternative name forms tried alongside ``query`` (issue
    place#144 / website #143 item 4). They are combined with ``dis_max`` +
    ``tie_breaker: 0`` so a toponym's score is its *best* single match rather
    than a sum over the forms it happens to resemble — matching the "keep the
    best score per place" accumulation in ``collect_place_ids``. Variant clauses
    carry ``variant_weight`` (< 1) so an equally good primary match still wins.
    """
    text_query = build_toponym_text_clause(query, mode)

    if variants:
        clauses = [text_query]
        for variant in variants:
            clauses.append({"bool": {
                "must": [build_toponym_text_clause(variant, mode)],
                "boost": variant_weight,
            }})
        text_query = {"dis_max": {"queries": clauses, "tie_breaker": 0.0}}

    if namespaces:
        # Scope discovery to the requested namespaces so the top-`size` window
        # isn't dominated by large namespaces (fixes narrow-namespace `in`).
        text_query = {
            "bool": {
                "must": [text_query],
                "filter": [{"terms": {"namespaces": namespaces}}],
            }
        }

    return {
        "size": size,
        "query": text_query,
        "_source": ["name", "lang", "attestations"],
    }


#: What an EXACT (case-insensitive) match on a name form is worth in
#: ``fuzzy``/``phonetic`` discovery, added on top of whatever the phonetic passes
#: scored that place.
#:
#: Deliberately above the phonetic ceiling. A KNN pass contributes at most 1.0
#: once normalised per pass, so any exactly-spelled candidate outranks any
#: purely-phonetic one, and a discounted variant's exact match still clears it.
#: Phonetic proximity is not discarded — it rides along as the ordering *within*
#: the exact-match tier.
#:
#: Shared by ``/api/search`` and ``/api/reconcile`` so the two cannot drift.
LEXICAL_EXACT_BOOST = 2.0


def build_lexical_exact_query(
    forms: list[str], namespaces: list[str] | None = None, size: int = 500,
) -> dict | None:
    """Exact, case-insensitive toponym lookup for a set of name forms.

    ``name.raw`` is a keyword field under ``lowercase_normalizer``, so a ``terms``
    clause over lowercased forms is a true exact match that still tolerates case
    ("NEWTON WITH SCALES" matches "Newton with Scales") without the asciifolding
    that would start conflating distinct names.

    This is the lexical half of ``fuzzy``/``phonetic`` discovery. KNN answers
    "what sounds like this" and is the only thing that ever answered; it does not
    reliably retrieve a toponym that is spelled *exactly* as asked. "Newton with
    Scales" is indexed with 3 attestations and never appeared anywhere in the
    200-candidate KNN pool, while cross-script neighbours filled it (place#197).

    Returns None when there is nothing to look up.
    """
    lowered = [f.strip().lower() for f in forms if f and f.strip()]
    if not lowered:
        return None
    query: dict = {"terms": {"name.raw": sorted(set(lowered))}}
    if namespaces:
        query = {"bool": {"must": [query],
                          "filter": [{"terms": {"namespaces": namespaces}}]}}
    return {
        "size": size,
        "query": query,
        "_source": ["name", "lang", "attestations"],
    }


def apply_lexical_boost(
    hits: list[dict],
    place_scores: dict[str, float],
    form_boosts: dict[str, float],
    exclude_prefixes: tuple[str, ...] = (),
    include_prefixes: tuple[str, ...] = (),
    match_names: dict[str, str] | None = None,
) -> int:
    """Add a flat boost to every place attested by an exactly-matching toponym.

    ``form_boosts`` maps a lowercased name form to the boost an exact match on it
    earns — the primary query more than a variant, mirroring
    ``variant_weight``/``score_scale`` elsewhere.

    The boost is **added** to whatever the phonetic passes found, not maxed with
    it, and it is larger than any score those passes can produce. Three
    consequences, all wanted:

    * a place spelled exactly as asked outranks every purely-phonetic neighbour,
      even one the KNN scored at its own ceiling;
    * within the exact-match tier, phonetic proximity survives as the tiebreak,
      so the 17 places called "Long Melford" still order sensibly;
    * a place found ONLY by exact spelling still enters the pool, which is how a
      record the KNN never retrieves becomes reachable at all.

    A place attested by several matching forms takes the single largest boost
    rather than their sum — matching a primary AND a variant is not evidence
    twice over.

    Returns the number of places boosted.
    """
    best: dict[str, float] = {}
    best_name: dict[str, str] = {}
    for hit in hits:
        source = hit.get("_source", {})
        name = source.get("name") or ""
        boost = form_boosts.get(name.strip().lower())
        if not boost:
            continue
        for pid in source.get("attestations", []):
            if not pid:
                continue
            if include_prefixes and not pid.startswith(include_prefixes):
                continue
            if exclude_prefixes and pid.startswith(exclude_prefixes):
                continue
            if boost > best.get(pid, 0.0):
                best[pid] = boost
                best_name[pid] = name
    for pid, boost in best.items():
        place_scores[pid] = place_scores.get(pid, 0.0) + boost
        if match_names is not None:
            # The exact hit now dominates this place's score, so it is the match
            # to report — keep name and score agreeing as collect_place_ids does.
            match_names[pid] = best_name[pid]
    return len(best)


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
    score_scale: float = 1.0,
    normalise: bool = False,
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
        score_scale: Multiplier applied to each hit's score before it competes
            for the per-place best. Folds in *separate* discovery passes — e.g.
            the per-variant KNN searches — at a slight discount to the primary
            query, mirroring the ``variant_weight`` boost of the text path.
        normalise: Divide every hit's score by the top score **of this call's
            own hit list** before applying ``score_scale``. Required whenever a
            call folds in a pass run against a *different* query — the KNN
            searches per name variant. Their raw ``_score``s are cosines to
            different query vectors and are NOT commensurable: how close a
            toponym sits to ``"Newton-with-Scales"`` says nothing about how it
            ranks among the neighbours of ``"Newton with Scales"``, so a variant
            with a tight neighbourhood returned higher raw cosines than the
            primary's genuine hits and its junk won the ``max`` outright
            (place#197). Normalising makes each pass contribute a *relative*
            score in ``(0, 1]``, so the discounted variant tops out at
            ``score_scale`` and the primary's best match — the exact name the
            user supplied — can never be displaced by a variant's neighbour.
    """
    if normalise:
        top = max((hit.get("_score") or 0.0) for hit in hits) if hits else 0.0
        score_scale = (score_scale / top) if top > 0 else 0.0
    for hit in hits:
        score = (hit.get("_score") or 0.0) * score_scale
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


def rank_candidate_ids(place_scores: dict[str, float], pool_k: int) -> list[str]:
    """Return the top-``pool_k`` discovered place_ids, best discovery score first.

    The Step-2 places query is a pure ``filter`` bool, so every hit scores the
    same and ES's ``size`` cut hands back an ARBITRARY doc-order page. Handing it
    the whole discovery set therefore drops candidates *before* they are ranked
    whenever discovery finds more ids than the fetch window. Pre-ordering the
    ``terms`` list by discovery score (tiebroken on place_id for stable
    pagination) makes the window the genuine top-K instead.

    Shared by ``/api/search`` and ``/api/reconcile`` — reconcile lacked it, which
    is how ``variants`` (up to 200 extra toponyms' worth of attestations per
    extra form) evicted the correct match entirely (place#197).
    """
    return sorted(
        place_scores.keys(),
        key=lambda p: (place_scores[p], p), reverse=True,
    )[:pool_k]


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
# ---------------------------------------------------------------------------
# Temporal filtering — the four-bound encoding (place#164, place#169)
# ---------------------------------------------------------------------------

#: Nested path carrying the timespans the place filter tests. Geometry- and
#: relation-level timespans exist in the schema but have never been consulted
#: here; see place#169 ("out of scope") before changing that.
_TS = "toponyms.timespans"

#: How a date window is matched against a place's temporal bounds.
#:
#:   possibly   (start.earliest ?? -inf) <= Q <= (end.latest ?? +inf)
#:   definitely  start.latest <= Q <= end.earliest
#:
#: A source recording places *as they were* at a moment (OSM's dump, Index
#: Villaris in 1680) constrains one side of each bound only; the absent outer
#: bounds are what carry "unbounded", which is why no sentinel year is needed.
TEMPORAL_MODES = ("possibly", "definitely")
DEFAULT_TEMPORAL_MODE = "possibly"


def _bound_clause(primary: str, fallback: str, op: str, value: int,
                  unbounded_passes: bool) -> dict:
    """One side of the overlap test, reading ``primary ?? fallback``.

    ``in`` is the fallback everywhere because the sources that legitimately
    still use it (``ohm``, ``clio``, ``hgis``) mean it as an exact year — which
    is simultaneously the earliest and the latest that endpoint could be, so it
    stands in for whichever outer bound is being asked for.

    ``unbounded_passes`` distinguishes the two modes: with neither sub-field
    present the endpoint is unknown, which means *unbounded* to a possibly-alive
    query (it passes) and *no definite core* to a definitely-alive one (it fails).
    """
    should = [
        {"range": {primary: {op: value}}},
        {"bool": {
            "must_not": [{"exists": {"field": primary}}],
            "must": [{"range": {fallback: {op: value}}}],
        }},
    ]
    if unbounded_passes:
        should.append({"bool": {"must_not": [
            {"exists": {"field": primary}},
            {"exists": {"field": fallback}},
        ]}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def _temporal_filter(start_year: int | None, end_year: int | None,
                     mode: str, undated: bool) -> dict:
    """Filter clause for a date window, in ``possibly`` or ``definitely`` mode.

    The window is an interval, so the test is interval overlap: the place's start
    must not fall after the window ends, and its end must not fall before the
    window begins. Which bound stands for "the place's start" is what the mode
    selects.
    """
    if mode not in TEMPORAL_MODES:
        mode = DEFAULT_TEMPORAL_MODE
    definite = mode == "definitely"
    conditions: list[dict] = []

    if end_year is not None:
        # Started at/before the window ends. Possibly: the earliest it could have
        # started. Definitely: the latest it could have started — an open-start
        # feature (ukhc, un, vob_*, kain_par) has no definite start, so it can be
        # *possibly* alive in a window but never *definitely* so.
        conditions.append(_bound_clause(
            f"{_TS}.start.earliest" if not definite else f"{_TS}.start.latest",
            f"{_TS}.start.in", "lte", end_year, unbounded_passes=not definite,
        ))
    if start_year is not None:
        # Still there at/after the window begins. Possibly: the latest it could
        # have ended, absent meaning ongoing. Definitely: the earliest it could
        # have ended.
        conditions.append(_bound_clause(
            f"{_TS}.end.latest" if not definite else f"{_TS}.end.earliest",
            f"{_TS}.end.in", "gte", start_year, unbounded_passes=not definite,
        ))

    match = {"nested": {"path": "toponyms", "query": {
        "nested": {"path": _TS, "query": {"bool": {"must": conditions}}},
    }}}
    if not undated:
        return match

    # `undated` admits places carrying no temporal information at all, so a date
    # filter doesn't silently drop every undated record. The probe must name all
    # six sub-fields: testing `in` alone (as it did until place#169) reads every
    # re-encoded attestation — which carries only earliest/latest — as undated.
    no_timespans = {"bool": {"must_not": {"nested": {"path": "toponyms", "query": {
        "nested": {"path": _TS, "query": {"bool": {
            "should": [
                {"exists": {"field": f"{_TS}.{side}.{qualifier}"}}
                for side in ("start", "end")
                for qualifier in ("in", "earliest", "latest")
            ],
            "minimum_should_match": 1,
        }}},
    }}}}}
    return {"bool": {"should": [match, no_timespans], "minimum_should_match": 1}}


def build_places_filter(
    place_ids: list[str] | None,
    ccodes: list[str] | None,
    bounds: dict | None,
    start_year: int | None,
    end_year: int | None,
    size: int = 50,
    undated: bool = False,
    temporal_mode: str = DEFAULT_TEMPORAL_MODE,
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
        temporal_mode: ``"possibly"`` (default) or ``"definitely"`` — which of
            the four temporal bounds ``start_year``/``end_year`` are tested
            against. See ``_temporal_filter``.
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
        # Match an AAT concept however the source happens to store it. The three
        # forms are all live in the index today:
        #   * types.aat_ids  — numeric; Getty TGN's 3M records (and only those)
        #   * types.identifier — "aat:300008347" (WHG's own data) or the bare
        #     number (TGN's identifier field)
        #   * types.aat_paths — materialised ancestry, for concept-OR-descendant
        #     matching. Currently EMPTY on every namespace, so on its own it
        #     matched nothing: an AAT filter silently returned zero results even
        #     for TGN, which is 100% AAT-typed. Kept so the hierarchy works the
        #     moment the paths are populated. See WHG place#184.
        ids = [int(aid) for aid in aat_types]
        filter_clauses.append({
            "nested": {
                "path": "types",
                "query": {"bool": {
                    "should": (
                        [{"terms": {"types.aat_ids": ids}}]
                        + [{"terms": {"types.identifier":
                                      [f"aat:{i}" for i in ids] + [str(i) for i in ids]}}]
                        + [{"wildcard": {"types.aat_paths": f"*{i}*"}} for i in ids]
                    ),
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
        filter_clauses.append(
            _temporal_filter(start_year, end_year, temporal_mode, undated)
        )

    bool_query = {"filter": filter_clauses}
    if must_not_clauses:
        bool_query["must_not"] = must_not_clauses

    # geom_class ships alongside has_geom so the endpoint can mark a candidate as
    # a valid containment parent (is_area) by shape rather than mere retrievability.
    geom_fields = (
        ["geometries.geom", "geometries.repr_point", "geometries.has_geom",
         "geometries.geom_class"]
        if geom == "full"
        else ["geometries.repr_point", "geometries.has_geom", "geometries.geom_class"]
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

