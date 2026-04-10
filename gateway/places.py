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


def _format_geometries(raw_geoms: list[dict]) -> list[dict]:
    """Format geometry objects with full geom + repr_point."""
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
        if entry:
            result.append(entry)
    return result


def _format_place_detail(
    src: dict,
    toponyms: list[dict] | None = None,
    requested_fields: set[str] | None = None,
) -> PlaceDetail:
    """Convert an ES places _source dict into a PlaceDetail.

    Args:
        src: ``_source`` dict from the places index.
        toponyms: Optional enriched name list from the toponyms index.
        requested_fields: If set, only populate these fields.
    """
    pid = src.get("place_id", "")

    # Names — prefer toponym enrichment, fall back to nested place data
    names: list[CandidateName] = []
    seen_labels: set[str] = set()
    name_data = toponyms if toponyms is not None else src.get("toponyms", [])
    for t in name_data:
        label = t.get("label", "")
        if label and label not in seen_labels:
            names.append(CandidateName(label=label, lang=t.get("lang")))
            seen_labels.add(label)

    # Geometries
    raw_geoms = src.get("geometries", [])
    geometries = _format_geometries(raw_geoms)
    repr_point = _extract_repr_point(raw_geoms)

    # Types
    types = [
        {
            "identifier": t.get("identifier", ""),
            "label": t.get("label", ""),
            "sourceLabel": t.get("sourceLabel", ""),
        }
        for t in src.get("types", [])
    ]

    # Links
    links = [
        {"type": lnk.get("type", ""), "identifier": lnk.get("identifier", "")}
        for lnk in src.get("links", [])
    ]

    # Descriptions
    descriptions = [
        {"value": d.get("value", ""), "lang": d.get("lang")}
        for d in src.get("descriptions", [])
    ]

    # Depictions
    depictions = [
        {"@id": d.get("@id", ""), "title": d.get("title", ""), "license": d.get("license", "")}
        for d in src.get("depictions", [])
    ]

    # Relations
    relations = [
        {
            "relation_type": r.get("relation_type", ""),
            "related_place_id": r.get("related_place_id", ""),
            "label": r.get("label", ""),
        }
        for r in src.get("relations", [])
    ]

    detail = PlaceDetail(
        place_id=pid,
        namespace=src.get("namespace", ""),
        title=src.get("title", ""),
        names=names,
        ccodes=src.get("ccodes", []),
        types=types,
        geometries=geometries,
        repr_point=repr_point,
        links=links,
        descriptions=descriptions,
        depictions=depictions,
        relations=relations,
        fclasses=src.get("fclasses", []),
        population=src.get("population"),
        elevation=src.get("elevation"),
    )

    # If specific fields were requested, zero out the rest
    if requested_fields is not None:
        all_optional = {
            "names", "ccodes", "types", "geometries", "repr_point",
            "links", "descriptions", "depictions", "relations",
            "fclasses", "population", "elevation",
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

    async with httpx.AsyncClient(timeout=30) as client:
        # ------------------------------------------------------------------
        # Step 1: Fetch places from the places index
        # ------------------------------------------------------------------
        places_body = _build_places_by_id(req.ids)
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
        not_found = [pid for pid in req.ids if pid not in found_ids]

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
                    for pid in src.get("attestations", []):
                        if pid in found_ids:
                            place_toponyms[pid].append(
                                {"label": label, "lang": lang}
                            )
            except Exception as e:
                # Non-fatal: places will fall back to nested toponym data
                logger.warning("Toponym enrichment failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # Format response — preserve request order
    # ------------------------------------------------------------------
    places: list[PlaceDetail] = []
    for pid in req.ids:
        if pid in sources:
            toponyms = place_toponyms.get(pid) or None
            places.append(
                _format_place_detail(sources[pid], toponyms, requested_fields)
            )

    return PlacesResponse(places=places, not_found=not_found)



