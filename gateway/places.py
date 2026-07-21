# gateway/places.py
"""
Place data endpoint for the WHG API gateway.

Provides ``POST /api/places`` — a simple two-step fetch that retrieves
full place records by ID from the ``places`` index and enriches them
with toponym data from the ``toponyms`` index.

This powers the OpenRefine "data extension" flow: after reconciliation
returns namespaced place IDs (e.g. ``gn:745044``), the Django app calls
this endpoint to fetch property values (name, geometry, types, etc.)
for each matched entity.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from .config import (
    ES_BACKEND,
    PLACES_INDEX,
    TOPONYMS_INDEX,
)
from .es_helpers import (
    es_auth,
    ES_HEADERS,
    build_toponym_lookup,
)

logger = logging.getLogger("gateway.places")

router = APIRouter(prefix="/api", tags=["Places"])

# The Reconciliation API convention prefixes entity IDs with their type,
# e.g. "place:gn:2650225".  The ES places index stores just the namespaced
# ID "gn:2650225".  We strip the prefix transparently so callers can send
# either form.
_ENTITY_PREFIX = "place:"

# Relation types that mark a genuine co-reference (a "Wikidata match"), as
# opposed to spatial/other bridges (e.g. the ``within`` wikidata bridge from
# interlink_ottgaz). Only these inherit the matched wd record's Wikipedia links.
_WD_MATCH_RELATION_TYPES = {"sameAs", "exactMatch", "closeMatch"}


def _strip_prefix(raw_id: str) -> str:
    """Strip the ``place:`` entity-type prefix if present."""
    return raw_id[len(_ENTITY_PREFIX):] if raw_id.startswith(_ENTITY_PREFIX) else raw_id


def _wd_match_targets(src: dict) -> list[str]:
    """``wd:Q…`` place_ids this place reconciles to (match relations only)."""
    out: list[str] = []
    for r in src.get("relations") or []:
        rid = r.get("related_place_id") or ""
        if rid.startswith("wd:") and r.get("relation_type") in _WD_MATCH_RELATION_TYPES:
            out.append(rid)
    return out


def _wikipedia_links(links: list[dict] | None) -> list[dict]:
    """Wikipedia ``seeAlso`` links carried by a wd record's ``links[]``."""
    out: list[dict] = []
    for lnk in links or []:
        if not isinstance(lnk, dict):
            continue
        ident = lnk.get("identifier") or ""
        if "wikipedia.org" in ident:
            out.append({"type": lnk.get("type") or "seeAlso", "identifier": ident})
    return out


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class PlacesRequest(BaseModel):
    ids: list[str] = Field(
        ..., min_length=1, max_length=500,
        description="Namespaced place IDs from the places index",
    )
    fields: list[str] | None = Field(
        None,
        description="Optional field filter; omit to return all available fields",
    )


class CandidateName(BaseModel):
    label: str
    lang: str | None = None
    timespans: list[dict] = []


class PlaceDetail(BaseModel):
    place_id: str
    namespace: str = ""
    title: str = ""
    names: list[CandidateName] = []
    ccodes: list[str] = []
    types: list[dict] = []
    geometries: list[dict] = []
    repr_point: list[float] | None = None
    links: list[dict] = []
    descriptions: list[dict] = []
    depictions: list[dict] = []
    relations: list[dict] = []
    fclasses: list[str] = []
    population: int | None = None
    elevation: int | None = None
    boundary: str | None = None
    timespans: list[dict] = []


class PlacesResponse(BaseModel):
    places: list[PlaceDetail] = []
    not_found: list[str] = []


# ---------------------------------------------------------------------------
# All fields that can be requested from the places index
# ---------------------------------------------------------------------------

ALL_PLACE_FIELDS = [
    "place_id", "namespace", "title", "ccodes", "types",
    "geometries", "links", "descriptions", "depictions",
    "relations", "population", "elevation", "fclasses",
    "boundary", "timespans",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_places_by_id(
    ids: list[str],
    source_fields: list[str] | None = None,
) -> dict:
    """Build an ES terms query to fetch places by place_id."""
    body: dict = {
        "size": len(ids),
        "query": {
            "terms": {"place_id": ids}
        },
    }
    if source_fields:
        body["_source"] = source_fields
    else:
        body["_source"] = True
    return body


def _extract_repr_point(geometries: list[dict]) -> list[float] | None:
    """Extract the first representative point from geometry objects."""
    for g in geometries:
        rp = g.get("repr_point")
        if rp:
            if isinstance(rp, dict):
                return [rp.get("lon", 0), rp.get("lat", 0)]
            if isinstance(rp, list) and len(rp) == 2:
                return rp
    return None


def _normalise_timespans(raw: list) -> list[dict]:
    """Flatten nested ``{start: {in: <int>}, end: {in: <int>}}`` shapes into
    plain ``[{start: <int>, end: <int>}]`` entries.

    Either bound may be ``None`` if only one side is attested. Empty list
    when no usable timespans are present.
    """
    out: list[dict] = []
    for ts in raw or []:
        if not isinstance(ts, dict):
            continue
        start_in = (ts.get("start") or {}).get("in")
        end_in = (ts.get("end") or {}).get("in")
        if not isinstance(start_in, int):
            start_in = None
        if not isinstance(end_in, int):
            end_in = None
        if start_in is None and end_in is None:
            continue
        out.append({"start": start_in, "end": end_in})
    return out


def _collapse_timespans(src: dict) -> list[dict]:
    """Collapse nested timespan ranges into a single overall range.

    Timespans live nested under ``toponyms[].timespans``, ``geometries[].timespans``,
    and ``relations[].timespans`` (see schemas/places.json). Each timespan has
    ``start.in`` and ``end.in`` integer year fields. We take the global
    min(start.in) and max(end.in) across all three sources to give callers a
    single "active from–to" summary.

    Returns ``[{"start": <int>, "end": <int>}]`` or ``[]`` if no timespans
    are present. Either bound may be ``None`` if only one side is attested.
    """
    starts: list[int] = []
    ends: list[int] = []
    for parent_key in ("toponyms", "geometries", "relations"):
        for entry in src.get(parent_key) or []:
            if not isinstance(entry, dict):
                continue
            for ts in entry.get("timespans") or []:
                if not isinstance(ts, dict):
                    continue
                start_in = (ts.get("start") or {}).get("in")
                end_in = (ts.get("end") or {}).get("in")
                if isinstance(start_in, int):
                    starts.append(start_in)
                if isinstance(end_in, int):
                    ends.append(end_in)
    if not starts and not ends:
        return []
    return [{
        "start": min(starts) if starts else None,
        "end": max(ends) if ends else None,
    }]


def _format_geometries(raw_geoms: list[dict]) -> list[dict]:
    """Format geometry objects with full geom + repr_point + timespans."""
    result = []
    for g in raw_geoms:
        entry: dict = {}
        geom_obj = g.get("geom")
        if isinstance(geom_obj, dict) and geom_obj.get("type") and geom_obj.get("coordinates"):
            entry["geom"] = {
                "type": geom_obj["type"],
                "coordinates": geom_obj["coordinates"],
            }
        rp = g.get("repr_point")
        if rp:
            if isinstance(rp, dict):
                entry["repr_point"] = [rp.get("lon", 0), rp.get("lat", 0)]
            elif isinstance(rp, list) and len(rp) == 2:
                entry["repr_point"] = rp
        ts = _normalise_timespans(g.get("timespans") or [])
        if ts:
            entry["timespans"] = ts
        if entry:
            result.append(entry)
    return result


def _format_place_detail(
    src: dict,
    toponyms: list[dict] | None = None,
    requested_fields: set[str] | None = None,
    extra_links: list[dict] | None = None,
) -> PlaceDetail:
    """Convert an ES places _source dict into a PlaceDetail.

    Args:
        src: ``_source`` dict from the places index.
        toponyms: Optional enriched name list from the toponyms index.
        requested_fields: If set, only populate these fields.
        extra_links: Optional links to merge into ``links[]`` (deduped by
            identifier) — e.g. Wikipedia links inherited from a matched wd
            record.
    """
    pid = src.get("place_id", "")

    # Build a (label, lang) → timespans lookup from the place's nested
    # toponyms so timespan data survives whichever name-enrichment path
    # _format_place_detail takes.
    nested_topos = src.get("toponyms") or []
    timespan_lookup: dict[tuple[str, str | None], list[dict]] = {}
    for t in nested_topos:
        if not isinstance(t, dict):
            continue
        label = t.get("label") or ""
        lang = t.get("lang")
        ts = _normalise_timespans(t.get("timespans") or [])
        if label and ts:
            timespan_lookup.setdefault((label, lang), []).extend(ts)

    # Names — prefer toponym enrichment, fall back to nested place data
    names: list[CandidateName] = []
    seen_labels: set[str] = set()
    name_data = toponyms if toponyms is not None else nested_topos
    for t in name_data:
        label = t.get("label", "")
        if label and label not in seen_labels:
            lang = t.get("lang")
            ts = timespan_lookup.get((label, lang)) or []
            names.append(CandidateName(label=label, lang=lang, timespans=ts))
            seen_labels.add(label)

    # Geometries
    raw_geoms = src.get("geometries") or []
    geometries = _format_geometries(raw_geoms)
    repr_point = _extract_repr_point(raw_geoms)

    # Types — carry the resolved AAT concept ids (place#122) so the Atlas popup
    # can link/annotate a type chip with its AAT identifier + scope-note tooltip.
    types = [
        {
            "identifier": t.get("identifier", ""),
            "label": t.get("label", ""),
            "sourceLabel": t.get("sourceLabel", ""),
            "aat_ids": t.get("aat_ids") or [],
        }
        for t in (src.get("types") or [])
    ]

    # Links — the place's own links plus any inherited from matched wd records
    # (merged plainly, deduped by identifier).
    links = [
        {"type": lnk.get("type", ""), "identifier": lnk.get("identifier", "")}
        for lnk in (src.get("links") or [])
    ]
    if extra_links:
        seen_idents = {lnk["identifier"] for lnk in links}
        for lnk in extra_links:
            ident = lnk.get("identifier", "")
            if ident and ident not in seen_idents:
                links.append({"type": lnk.get("type", ""), "identifier": ident})
                seen_idents.add(ident)

    # Descriptions
    descriptions = [
        {"value": d.get("value", ""), "lang": d.get("lang")}
        for d in (src.get("descriptions") or [])
    ]

    # Depictions
    depictions = [
        {"@id": d.get("@id", ""), "title": d.get("title", ""), "license": d.get("license", "")}
        for d in (src.get("depictions") or [])
    ]

    # Relations — include per-relation timespans when present
    relations = [
        {
            "relation_type": r.get("relation_type", ""),
            "related_place_id": r.get("related_place_id", ""),
            "label": r.get("label", ""),
            "timespans": _normalise_timespans(r.get("timespans") or []),
        }
        for r in (src.get("relations") or [])
    ]

    detail = PlaceDetail(
        place_id=pid,
        namespace=src.get("namespace", ""),
        title=src.get("title", ""),
        names=names,
        ccodes=src.get("ccodes") or [],
        types=types,
        geometries=geometries,
        repr_point=repr_point,
        links=links,
        descriptions=descriptions,
        depictions=depictions,
        relations=relations,
        fclasses=src.get("fclasses") or [],
        population=src.get("population"),
        elevation=src.get("elevation"),
        boundary=src.get("boundary"),
        timespans=_collapse_timespans(src),
    )

    # If specific fields were requested, zero out the rest
    if requested_fields is not None:
        all_optional = {
            "names", "ccodes", "types", "geometries", "repr_point",
            "links", "descriptions", "depictions", "relations",
            "fclasses", "population", "elevation",
            "boundary", "timespans",
        }
        for f in all_optional - requested_fields:
            if hasattr(detail, f):
                current = getattr(detail, f)
                setattr(detail, f, [] if isinstance(current, list) else None)

    return detail


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/places", response_model=PlacesResponse)
async def fetch_places(req: PlacesRequest):
    """
    Fetch full place data by namespaced IDs.

    Two-step strategy:

      **Step 1** — Query the ``places`` index with a ``terms`` filter on
      ``place_id`` to retrieve all available fields.

      **Step 2** — Query the ``toponyms`` index with a ``terms`` filter on
      ``attestations`` to enrich name data (label + lang) for each place.

    Returns structured place objects and a list of IDs that were not found.
    """
    auth = es_auth()
    requested_fields = set(req.fields) if req.fields else None

    # Normalise IDs: strip "place:" prefix, track mapping back to caller IDs
    # raw_to_es: caller ID → ES place_id   (e.g. "place:gn:123" → "gn:123")
    raw_to_es = {rid: _strip_prefix(rid) for rid in req.ids}
    es_ids = list(dict.fromkeys(raw_to_es.values()))  # unique, order-preserved

    async with httpx.AsyncClient(timeout=30) as client:
        # ------------------------------------------------------------------
        # Step 1: Fetch places from the places index
        # ------------------------------------------------------------------
        places_body = _build_places_by_id(es_ids)
        places_resp = await client.post(
            f"{ES_BACKEND}/{PLACES_INDEX}/_search",
            json=places_body,
            auth=auth,
            headers=ES_HEADERS,
        )
        places_resp.raise_for_status()
        places_result = places_resp.json()

        raw_hits = places_result.get("hits", {}).get("hits", [])

        # Build source lookup: place_id → _source
        sources: dict[str, dict] = {}
        for hit in raw_hits:
            src = hit.get("_source", {})
            pid = src.get("place_id", "")
            if pid:
                sources[pid] = src

        found_ids = set(sources.keys())
        not_found = [rid for rid in req.ids if raw_to_es[rid] not in found_ids]

        # ------------------------------------------------------------------
        # Step 2: Enrich names from the toponyms index
        # ------------------------------------------------------------------
        place_toponyms: dict[str, list[dict]] = defaultdict(list)

        if found_ids:
            found_list = list(found_ids)
            topo_body = build_toponym_lookup(
                found_list,
                size=max(len(found_list) * 30, 500),
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
                    for pid in (src.get("attestations") or []):
                        if pid in found_ids:
                            place_toponyms[pid].append(
                                {"label": label, "lang": lang}
                            )
            except Exception as e:
                # Non-fatal: places will fall back to nested toponym data
                logger.warning("Toponym enrichment failed (non-fatal): %s", e)

        # ------------------------------------------------------------------
        # Step 2.5: Inherit Wikipedia links from matched Wikidata records
        #
        # A non-wd place that reconciles to Wikidata carries the match as a
        # relation (related_place_id="wd:Q…", relation_type sameAs/exactMatch/
        # closeMatch). The wd doc itself holds the Wikipedia sitelinks as
        # seeAlso links (added 2026-07-06). We fetch those links for the
        # matched wd records and merge them into each place's links[] — no
        # reindex, always in sync with the wd data. Skipped when the caller
        # filtered links out.
        # ------------------------------------------------------------------
        wp_links: dict[str, list[dict]] = {}
        want_links = requested_fields is None or "links" in requested_fields
        if found_ids and want_links:
            wd_targets: set[str] = set()
            for src in sources.values():
                wd_targets.update(_wd_match_targets(src))
            # A matched wd record may already be in this batch — reuse its links.
            for wid in list(wd_targets):
                if wid in sources:
                    wp_links[wid] = _wikipedia_links(sources[wid].get("links"))
                    wd_targets.discard(wid)
            if wd_targets:
                wd_body = _build_places_by_id(
                    list(wd_targets), ["place_id", "links"])
                try:
                    wd_resp = await client.post(
                        f"{ES_BACKEND}/{PLACES_INDEX}/_search",
                        json=wd_body,
                        auth=auth,
                        headers=ES_HEADERS,
                    )
                    wd_resp.raise_for_status()
                    for wh in wd_resp.json().get("hits", {}).get("hits", []):
                        wsrc = wh.get("_source", {})
                        wid = wsrc.get("place_id", "")
                        if wid:
                            wp_links[wid] = _wikipedia_links(wsrc.get("links"))
                except Exception as e:
                    # Non-fatal: places keep their own links, just no inheritance
                    logger.warning(
                        "Wikidata link enrichment failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # Format response — preserve request order, use caller's original IDs
    # ------------------------------------------------------------------
    places: list[PlaceDetail] = []
    for rid in req.ids:
        es_id = raw_to_es[rid]
        if es_id in sources:
            toponyms = place_toponyms.get(es_id) or None
            extra_links: list[dict] = []
            for wid in _wd_match_targets(sources[es_id]):
                extra_links.extend(wp_links.get(wid, []))
            detail = _format_place_detail(
                sources[es_id], toponyms, requested_fields, extra_links or None)
            # If the caller sent a prefixed ID, echo it back so their
            # lookup dict (keyed on the original ID) finds the entry.
            if rid != es_id:
                detail.place_id = rid
            places.append(detail)

    return PlacesResponse(places=places, not_found=not_found)



