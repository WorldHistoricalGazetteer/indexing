# gateway/extend.py
"""
Data extension endpoint for the WHG API gateway.

Implements ``POST /api/extend`` — the OpenRefine "data extension" flow.
After reconciliation returns namespaced place IDs (e.g. ``wd:Q16202``),
the Django app forwards CRC-sourced IDs here to fetch property values
(names, geometry, country codes, types, etc.) in the OpenRefine extend
response format.

The endpoint accepts a list of entity IDs and property definitions,
looks up the records via the places/toponyms entity service, and returns
property values wrapped in the ``{"str": ...}`` format that OpenRefine
expects.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

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
    extract_place_geoms as _extract_geojson_geoms,
    extract_repr_point as _extract_repr_point_raw,
)

logger = logging.getLogger("gateway.extend")

router = APIRouter(prefix="/api", tags=["Extend"])

# Reconciliation API convention: entity IDs are prefixed with their type.
_ENTITY_PREFIX = "place:"


def _strip_prefix(raw_id: str) -> str:
    """Strip the ``place:`` entity-type prefix if present."""
    return raw_id[len(_ENTITY_PREFIX):] if raw_id.startswith(_ENTITY_PREFIX) else raw_id


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ExtendProperty(BaseModel):
    id: str = Field(..., description="Property identifier, e.g. 'whg:names_canonical'")


class ExtendRequest(BaseModel):
    ids: list[str] = Field(
        ..., min_length=1, max_length=500,
        description="Entity IDs with the 'place:' prefix (e.g. 'place:wd:Q16202')",
    )
    properties: list[ExtendProperty] = Field(
        ..., min_length=1,
        description="Property definitions to extract",
    )


class ExtendResponse(BaseModel):
    rows: dict[str, dict[str, list[dict]]] = Field(
        default_factory=dict,
        description="Property values keyed by entity ID, then property ID",
    )
    meta: list[dict[str, str]] = Field(
        default_factory=list,
        description="Metadata for each requested property",
    )


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

# GeoNames feature class codes → human-readable labels
_FCLASS_LABELS: dict[str, tuple[str, str]] = {
    "A": ("Administrative areas", "https://www.geonames.org/export/codes.html"),
    "H": ("Hydrographic features", "https://www.geonames.org/export/codes.html"),
    "L": ("Area features", "https://www.geonames.org/export/codes.html"),
    "P": ("Populated places", "https://www.geonames.org/export/codes.html"),
    "R": ("Road / railroad features", "https://www.geonames.org/export/codes.html"),
    "S": ("Spot features (buildings, farms)", "https://www.geonames.org/export/codes.html"),
    "T": ("Hypsographic features", "https://www.geonames.org/export/codes.html"),
    "U": ("Undersea features", "https://www.geonames.org/export/codes.html"),
    "V": ("Vegetation features", "https://www.geonames.org/export/codes.html"),
}


def _country_label(code: str) -> str:
    """Look up an ISO 3166-1 alpha-2 country code label via pycountry."""
    try:
        import pycountry
        country = pycountry.countries.get(alpha_2=code.upper())
        return country.name if country else code
    except Exception:
        return code


# ---------------------------------------------------------------------------
# Value wrapping (OpenRefine extend format)
# ---------------------------------------------------------------------------

def _wrap_value(val: Any) -> list[dict]:
    """
    Wrap a Python value in the OpenRefine extend cell format.

    Rules (from the spec):
      None             → []
      "string"         → [{"str": "string"}]
      ["a", "b"]       → [{"str": "a"}, {"str": "b"}]
      [dict1, dict2]   → [{"str": json(dict1)}, {"str": json(dict2)}]
      {dict}           → [{"str": json(dict)}]
    """
    if val is None:
        return []
    if isinstance(val, str):
        return [{"str": val}]
    if isinstance(val, (int, float)):
        return [{"str": str(val)}]
    if isinstance(val, dict):
        return [{"str": json.dumps(val, ensure_ascii=False)}]
    if isinstance(val, list):
        if not val:
            return []
        wrapped = []
        for item in val:
            if isinstance(item, str):
                wrapped.append({"str": item})
            elif isinstance(item, (dict, list)):
                wrapped.append({"str": json.dumps(item, ensure_ascii=False)})
            else:
                wrapped.append({"str": str(item)})
        return wrapped
    return [{"str": str(val)}]


# ---------------------------------------------------------------------------
# Geometry helpers — shared implementations live in es_helpers
# (imported above as _extract_geojson_geoms / _extract_repr_point_raw).
# ---------------------------------------------------------------------------


def _geojson_to_wkt(geojson: dict) -> str | None:
    """Convert a GeoJSON geometry dict to WKT via Shapely."""
    try:
        from shapely.geometry import shape as shapely_shape
        return shapely_shape(geojson).wkt
    except Exception:
        return None


def _geojson_to_bbox(geojson: dict) -> str | None:
    """Compute bounding box string 'minx, miny, maxx, maxy' from GeoJSON."""
    try:
        from shapely.geometry import shape as shapely_shape
        bounds = shapely_shape(geojson).bounds  # (minx, miny, maxx, maxy)
        return f"{bounds[0]}, {bounds[1]}, {bounds[2]}, {bounds[3]}"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Timespan helpers
# ---------------------------------------------------------------------------

def _extract_timespans(src: dict) -> list[dict]:
    """
    Collect all timespans from the nested toponyms and geometries.

    Returns a deduplicated list of ``{"begin": ..., "end": ...}`` dicts.
    """
    seen: set[tuple] = set()
    spans: list[dict] = []
    for field in ("toponyms", "geometries", "relations"):
        for obj in src.get(field, []):
            for ts in obj.get("timespans", []):
                begin = ts.get("start", {}).get("in")
                end = ts.get("end", {}).get("in")
                key = (begin, end)
                if key not in seen:
                    seen.add(key)
                    spans.append({"begin": begin, "end": end})
    return spans


# ---------------------------------------------------------------------------
# Property extractors
#
# Each extractor receives the place _source dict and the enriched names
# list, and returns a Python value that will be wrapped by _wrap_value.
# ---------------------------------------------------------------------------

def _extract_property(
    prop_id: str,
    src: dict,
    names: list[dict],
) -> Any:
    """
    Extract a single property value from a place record.

    Args:
        prop_id: The ``whg:`` property identifier.
        src: The ES ``_source`` dict from the places index.
        names: Enriched name list from the toponyms index
            (``[{"label": ..., "lang": ...}, ...]``).

    Returns:
        A Python value (str, list, dict, or None) ready for ``_wrap_value``.
    """
    title = src.get("title", "")

    match prop_id:
        # --- Identity ---
        case "whg:id_short":
            return src.get("place_id")

        case "whg:id_object":
            pid = src.get("place_id", "")
            return {"id": pid, "label": title} if pid else None

        # --- Names ---
        case "whg:names_canonical":
            return title or None

        case "whg:names_array":
            # Title prepended if not already present
            result: list[dict] = []
            seen: set[str] = set()
            if title:
                result.append({"toponym": title})
                seen.add(title)
            for n in names:
                label = n.get("label", "")
                if label and label not in seen:
                    result.append({"toponym": label})
                    seen.add(label)
            return result or None

        case "whg:names_summary":
            result_strs: list[str] = []
            seen_strs: set[str] = set()
            if title:
                result_strs.append(title)
                seen_strs.add(title)
            for n in names:
                label = n.get("label", "")
                if label and label not in seen_strs:
                    result_strs.append(label)
                    seen_strs.add(label)
            return result_strs or None

        # --- Geometry ---
        case "whg:geometry_geojson":
            geoms = _extract_geojson_geoms(src)
            # Each geometry dict is individually JSON-serialised by _wrap_value
            return geoms or None

        case "whg:geometry_wkt":
            geoms = _extract_geojson_geoms(src)
            wkts = [w for g in geoms if (w := _geojson_to_wkt(g))]
            return wkts or None

        case "whg:geometry_centroid":
            rp = _extract_repr_point_raw(src)
            if rp and len(rp) == 2:
                # "lat, lon" as specified (note: repr_point is [lon, lat])
                return [f"{rp[1]}, {rp[0]}"]
            return None

        case "whg:geometry_bbox":
            geoms = _extract_geojson_geoms(src)
            bboxes = [b for g in geoms if (b := _geojson_to_bbox(g))]
            return bboxes or None

        # --- Countries ---
        case "whg:countries_codes":
            ccodes = src.get("ccodes", [])
            return ccodes or None

        case "whg:countries_objects":
            ccodes = src.get("ccodes", [])
            return [
                {"code": c, "label": _country_label(c)} for c in ccodes
            ] or None

        # --- Types ---
        case "whg:types_objects":
            types = src.get("types", [])
            return [
                {
                    "identifier": t.get("identifier", ""),
                    "label": t.get("label", ""),
                    "sourceLabel": t.get("sourceLabel", ""),
                }
                for t in types
            ] or None

        # --- Feature classes ---
        case "whg:classes_codes":
            fclasses = src.get("fclasses", [])
            return fclasses or None

        case "whg:classes_objects":
            fclasses = src.get("fclasses", [])
            return [
                {
                    "code": fc,
                    "label": _FCLASS_LABELS.get(fc, ("Unknown", ""))[0],
                    "reference": _FCLASS_LABELS.get(fc, ("", ""))[1],
                }
                for fc in fclasses
            ] or None

        # --- Temporal ---
        case "whg:temporal_objects":
            spans = _extract_timespans(src)
            return spans or None

        case "whg:temporal_years":
            spans = _extract_timespans(src)
            years: list[str] = []
            for s in spans:
                begin = s.get("begin")
                end = s.get("end")
                if begin is not None or end is not None:
                    b = str(begin) if begin is not None else "?"
                    e = str(end) if end is not None else "?"
                    years.append(f"{b}-{e}")
            return years or None

        # --- Unrecognised property ---
        case _:
            return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_places_by_id(ids: list[str]) -> dict:
    """Build an ES terms query to fetch places by place_id (all fields)."""
    return {
        "size": len(ids),
        "query": {"terms": {"place_id": ids}},
        "_source": True,
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/extend", response_model=ExtendResponse)
async def extend(req: ExtendRequest):
    """
    Data extension endpoint for the OpenRefine reconciliation flow.

    Accepts entity IDs (with ``place:`` prefix) and property definitions,
    looks up records from the places and toponyms indexes, and returns
    property values in the OpenRefine extend response format.

    Example request::

        {
            "ids": ["place:wd:Q16202", "place:gn:745044"],
            "properties": [
                {"id": "whg:names_canonical"},
                {"id": "whg:geometry_geojson"},
                {"id": "whg:countries_codes"}
            ]
        }
    """
    auth = es_auth()
    prop_ids = [p.id for p in req.properties]

    # Map caller IDs → ES place_ids (strip "place:" prefix)
    raw_to_es: dict[str, str] = {rid: _strip_prefix(rid) for rid in req.ids}
    es_ids = list(dict.fromkeys(raw_to_es.values()))  # unique, order-preserved

    # Source lookup: es_place_id → _source
    sources: dict[str, dict] = {}
    # Toponym enrichment: es_place_id → [{"label": ..., "lang": ...}, ...]
    place_toponyms: dict[str, list[dict]] = defaultdict(list)

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

        for hit in places_resp.json().get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            pid = src.get("place_id", "")
            if pid:
                sources[pid] = src

        found_ids = set(sources.keys())

        # ------------------------------------------------------------------
        # Step 2: Enrich names from the toponyms index (if name properties
        # are requested)
        # ------------------------------------------------------------------
        name_props = {
            "whg:names_canonical", "whg:names_array", "whg:names_summary",
        }
        if found_ids and name_props & set(prop_ids):
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
                for th in topo_resp.json().get("hits", {}).get("hits", []):
                    tsrc = th.get("_source", {})
                    label = tsrc.get("name", "")
                    lang = tsrc.get("lang")
                    for pid in tsrc.get("attestations", []):
                        if pid in found_ids:
                            place_toponyms[pid].append(
                                {"label": label, "lang": lang}
                            )
            except Exception as e:
                logger.warning("Toponym enrichment failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # Build response rows
    # ------------------------------------------------------------------
    rows: dict[str, dict[str, list[dict]]] = {}

    for raw_id in req.ids:
        es_id = raw_to_es[raw_id]
        src = sources.get(es_id, {})
        names = place_toponyms.get(es_id, [])

        row: dict[str, list[dict]] = {}
        for prop_id in prop_ids:
            if src:
                val = _extract_property(prop_id, src, names)
                row[prop_id] = _wrap_value(val)
            else:
                # Record not found — return empty for all properties
                row[prop_id] = []
        rows[raw_id] = row

    # Meta block
    meta = [{"id": p, "name": p} for p in prop_ids]

    return ExtendResponse(rows=rows, meta=meta)

