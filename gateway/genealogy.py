# gateway/genealogy.py
"""POST /api/genealogy/place: resolve a genealogy-style place string to candidates.

The matching model cannot use hierarchy as comma-separated text. It needs RESOLVED
identifiers: a country code and/or a container place id passed as scope. This endpoint
does that work for a caller that only has the service's export string:

    "Ballymena, County Antrim, Ireland"   (service: ancestry, treealive, gedcom, ...)
      1. split, order and label the components            (place_context.parse_place)
      2. recognise the country (alias table, then the index)
      3. resolve each admin level, broad to narrow, each INSIDE the previous container
      4. search the locality down a RELAXATION LADDER:
            container + country  ->  country  ->  unscoped
         and report which rung answered.

Why a ladder and not a hard filter: family-tree data is full of the user's own mistakes
("Brussels, Germany"). A hard country filter turns such a mistake into a wrong answer or
no answer; relaxing one rung at a time finds the real place and SAYS it had to relax
(``scope.relaxed``, ``candidate.country_matches``). A bare name with no context cannot be
placed by anyone: it is searched unscoped and the response says so, it is not treated as
a failure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import place_context as pc
from .es_helpers import name_resemblance
from .result_cache import TTLCache

logger = logging.getLogger("gateway.genealogy")
router = APIRouter(prefix="/api/genealogy", tags=["genealogy"])

MAX_PLACES = 25            # each place costs ~3-6 index lookups; keep a request bounded
STRONG_CONFIDENCE = 85.0   # an exact or head-word name match: stop climbing the ladder here
ACCEPT_CONFIDENCE = 30.0   # the documented name-quality floor (see /reconcile `confidence`)
CONCURRENCY = 4

# Resolutions of a (kind, text, scope) are stable for a long time and repeat massively across
# a family tree ("Antrim" appears on thousands of lines). One hour, per worker.
_resolved = TTLCache(4096, 3600.0)
_inflight: dict = {}


async def _once(key, work):
    """Cache + SINGLE-FLIGHT: concurrent identical lookups (a batch runs several places at once, and a
    family tree repeats "County Antrim" on every line) share one in-flight request instead of racing
    the cache and each asking the index."""
    cached = _resolved.get(key)
    if cached is not None:
        return cached
    fut = _inflight.get(key)
    if fut is None:
        async def run():
            value = await work()
            _resolved.put(key, value)
            return value
        fut = asyncio.ensure_future(run())
        _inflight[key] = fut
        fut.add_done_callback(lambda _f, k=key: _inflight.pop(k, None))
    return await fut


class GenealogyRequest(BaseModel):
    service: str = Field("generic", description="Service profile: see GET /api/genealogy/services.")
    place: Optional[str] = Field(None, description="One place string.")
    places: Optional[list[str]] = Field(None, description=f"A batch of place strings (at most {MAX_PLACES}).")
    form: Optional[str] = Field(None, description="GEDCOM header PLAC.FORM, e.g. 'City, County, State, Country'. "
                                                   "Declares the order and the level of each component.")
    order: Optional[Literal["small_to_large", "large_to_small", "auto"]] = Field(
        None, description="Override the profile's component order. 'auto' infers it from where the country sits.")
    separator: Optional[str] = Field(None, min_length=1, max_length=3, description="Override the separator (default ',').")
    mode: str = Field("fuzzy", description="Name matching for the locality: fuzzy | exact | starts | in. "
                                           "(familysearch mode is not accepted here.)")
    size: int = Field(10, ge=1, le=50, description="Candidates per place.")
    country_hint: Optional[str] = Field(None, min_length=2, max_length=2,
                                        description="ISO 3166-1 alpha-2 country to assume when the string has none.")
    start_year: Optional[int] = Field(None, description="Temporal filter start year.")
    end_year: Optional[int] = Field(None, description="Temporal filter end year.")
    relax: bool = Field(True, description="Walk the relaxation ladder. false = stop at the most specific rung.")
    parse_only: bool = Field(False, description="Return the parse and the resolved context, skip the locality search.")
    namespaces: Optional[list[str]] = Field(None, description="Restrict to these source namespaces.")


# ----------------------------------------------------------------------------- index lookups


async def _lookup(**kw):
    """One reconcile search through the real pipeline (so scope, licence and result cache apply)."""
    from . import reconcile as rc
    return await rc.reconcile_search(rc.ReconcileRequest(**kw))


def _is_area(hit) -> bool:
    return any(getattr(g, "is_area", False) for g in (hit.geometries or []))


def _is_country_type(hit) -> bool:
    return any(str(t.get("identifier", "")).startswith("A.PCL") for t in (hit.types or []))


async def resolve_country(comp: pc.Component, *, hint: str | None) -> dict:
    """{"code", "source", "place_id"?, "name"?} or {"code": None, "source": "none"}."""
    if comp is not None and comp.country:
        return {"code": comp.country, "source": "alias", "component": comp.norm}
    if comp is not None and comp.norm:
        async def work():
            res = await _lookup(query=comp.norm, mode="exactci", fclasses=["A"], size=10)
            hit = next((h for h in res.hits if _is_country_type(h) and h.ccodes), None)
            return ({"code": hit.ccodes[0], "source": "index", "place_id": hit.place_id,
                     "name": hit.title, "component": comp.norm} if hit else {"code": None})
        got = await _once(("country", pc.fold(comp.norm)), work)
        if got.get("code"):
            return dict(got)
    if hint:
        return {"code": hint.upper(), "source": "hint"}
    return {"code": None, "source": "none"}


async def resolve_container(comp: pc.Component, *, country: str | None, parent_id: str | None) -> dict | None:
    """Resolve one admin-level component to a place usable as ``contained_in``.

    AREAL places are preferred (osm / ukhc / ohm carry polygons). GeoNames carries admin units as
    POINTS, which the gateway can still use (it borrows a co-referent's boundary, or fails closed),
    so a GeoNames admin place is accepted as a fallback and flagged ``areal: false``. Nothing here
    filters on feature class in the first pass: that filter exists only on GeoNames records.
    """
    raw, stripped = comp.norm, pc.strip_admin_words(comp.norm)
    texts = list(dict.fromkeys(t for t in (raw, stripped) if t))
    key = ("container", tuple(pc.fold(t) for t in texts), country, parent_id)
    async def work():

        def base(text: str, mode: str, under: str | None, **extra) -> dict:
            kw: dict[str, Any] = dict(query=text, mode=mode, size=20, **extra)
            if under:
                kw["ccodes"] = [under]
            if parent_id:
                kw["contained_in"] = [parent_id]
            return kw

        found = None
        # Under the stated country first; then anywhere: the user's country can be the mistake
        # ("County Antrim, Ireland": Antrim is in Northern Ireland), and the container then still resolves.
        for under in ([country, None] if country else [None]):
            for text in texts:                               # 1. an areal place, exact name
                res = await _lookup(**base(text, "exactci", under))
                hit = next((h for h in res.hits if _is_area(h) and name_resemblance(text, h.title) >= 1.0), None)
                if hit:
                    found = {"place_id": hit.place_id, "name": hit.title, "component": comp.norm,
                             "via": "exactci", "areal": True, "outside_stated_country": under is None and bool(country)}
                    break
            if not found:                                    # 2. a GeoNames admin place (a point)
                for text in texts:
                    res = await _lookup(**base(text, "exactci", under, fclasses=["A"], namespaces=["gn"]))
                    hit = next((h for h in res.hits if name_resemblance(text, h.title) >= 1.0), None)
                    if hit:
                        found = {"place_id": hit.place_id, "name": hit.title, "component": comp.norm,
                                 "via": "exactci-gn", "areal": False,
                                 "outside_stated_country": under is None and bool(country)}
                        break
            if found:
                break
        if not found:                                        # 3. spelling slack, areal only, stated country
            for text in texts:
                res = await _lookup(**base(text, "lexfuzzy", country))
                hit = next((h for h in res.hits if _is_area(h) and name_resemblance(text, h.title) >= 0.85), None)
                if hit:
                    found = {"place_id": hit.place_id, "name": hit.title, "component": comp.norm,
                             "via": "lexfuzzy", "areal": True, "outside_stated_country": False}
                    break
        return found or {}

    got = await _once(key, work)
    return got or None


# ----------------------------------------------------------------------------- one place


def _comp_dict(c: pc.Component) -> dict:
    return {"raw": c.raw, "norm": c.norm, "level": c.level, "blank": c.blank,
            "country": c.country, "constituent": c.constituent}


async def resolve_place(text: str, req: GenealogyRequest, profile: pc.Profile) -> dict:
    parsed = pc.parse_place(text, profile=profile, separator=req.separator, order=req.order, form=req.form)
    comps = parsed.filled
    out: dict[str, Any] = {
        "input": text,
        "parsed": {"order_applied": parsed.order_applied, "order_source": parsed.order_source,
                   "separator": parsed.separator, "form_used": parsed.form_used, "notes": parsed.notes,
                   "components": [_comp_dict(c) for c in parsed.components]},
    }
    if not comps:
        out.update(status="empty", context={}, scope={"rung": None, "relaxed": False, "attempts": []}, candidates=[])
        return out

    locality = comps[0]
    rest = comps[1:]
    country_comp = None
    if rest:
        last = rest[-1]
        if last.level == "country" or last.country:
            country_comp = last
        elif last.level is None and not any(c.level for c in rest):
            # no FORM and not a known alias: the country end is probably a country the alias table
            # does not know; let the index decide
            country_comp = last
    admin = [c for c in rest if c is not country_comp]

    country = await resolve_country(country_comp, hint=None) if country_comp is not None \
        else {"code": None, "source": "none"}
    if country.get("source") == "none" and country_comp is not None:
        # the candidate "country" component did not resolve: it is just another admin level
        admin = [c for c in rest]
        country_comp = None
    if country.get("source") == "none" and req.country_hint:
        country = {"code": req.country_hint.upper(), "source": "hint"}
    # a constituent country ("England") is both the country and an admin area; keep it as context too
    if country_comp is not None and country_comp.constituent and country_comp not in admin:
        admin = admin + [country_comp]

    containers: list[dict] = []
    unresolved: list[str] = []
    parent_id = None
    for comp in reversed(admin):                      # broad -> narrow
        got = await resolve_container(comp, country=country.get("code"), parent_id=parent_id)
        if got:
            containers.append({**got, "level": comp.level})
            parent_id = got["place_id"]
        else:
            unresolved.append(comp.norm)
    out["context"] = {"country": country, "containers": containers, "unresolved": unresolved}

    # --- ladder
    rungs: list[tuple[str, dict]] = []
    base: dict[str, Any] = {}
    if req.start_year is not None:
        base["start_year"] = req.start_year
    if req.end_year is not None:
        base["end_year"] = req.end_year
    if req.namespaces:
        base["namespaces"] = req.namespaces
    if containers:
        # A resolved container implies its own country, so it is NOT combined with the stated one:
        # the stated country may be the user's mistake, and the two would then exclude everything.
        rungs.append(("container", {"contained_in": [containers[-1]["place_id"]]}))
    if country.get("code"):
        rungs.append(("country", {"ccodes": [country["code"]]}))
    rungs.append(("none", {}))

    if req.parse_only:
        out.update(status="parsed", scope={"rung": None, "relaxed": False, "attempts": []}, candidates=[])
        return out

    attempts: list[dict] = []
    tried: list[tuple[str, Any, float]] = []     # (rung, response, top confidence or 100 when unmeasured)
    chosen = None
    for name, scope in rungs:
        res = await _lookup(query=locality.norm, mode=req.mode, size=req.size, **base, **scope)
        top = res.hits[0] if res.hits else None
        conf = getattr(top, "confidence", None) if top else None
        strong = bool(top) and (conf is None or conf >= STRONG_CONFIDENCE)
        attempts.append({"rung": name, "applied": scope, "hits": len(res.hits),
                         "top_confidence": conf, "accepted": strong})
        if top:
            tried.append((name, res, 100.0 if conf is None else conf))
        if strong:
            chosen = (name, res)
            break
        if not req.relax:
            break
    status = "ok"
    if chosen is None and tried:
        # No rung gave a STRONG name match. Take the best-scoring rung (ties go to the more specific
        # one, which came first): a loose match inside the user's stated place must not beat an exact
        # match just outside it, and equally a weak match anywhere is flagged, not promoted.
        best = max(tried, key=lambda t: (round(t[2], 1), -[r[0] for r in rungs].index(t[0])))
        chosen = (best[0], best[1])
        status = "ok" if best[2] >= ACCEPT_CONFIDENCE else "low_confidence"
    elif chosen is None:
        status = "no_match"

    cands: list[dict] = []
    rung = None
    if chosen:
        rung, res = chosen
        code = country.get("code")
        for h in res.hits:
            d = h.model_dump()
            d["country_matches"] = (None if not code else (code in (h.ccodes or [])))
            cands.append(d)
    out.update(status=status, candidates=cands, scope={
        "rung": rung, "relaxed": bool(rung) and rung != rungs[0][0], "attempts": attempts,
        "note": ("no country or region in the string: searched unscoped, a bare name cannot be placed "
                 "without one") if len(rungs) == 1 else None})
    return out


# ----------------------------------------------------------------------------- endpoints


@router.get("/services")
async def services():
    return {"services": [{"name": p.name, "separator": p.separator, "order": p.order,
                          "basis": p.basis, "note": p.note} for p in pc.PROFILES.values()]}


@router.post("/place")
async def place(req: GenealogyRequest):
    if req.mode.lower() == "familysearch":
        raise HTTPException(422, "familysearch mode is a /api/reconcile mode; use fuzzy | exact | starts | in here")
    profile = pc.PROFILES.get(req.service.lower())
    if profile is None:
        raise HTTPException(422, f"unknown service {req.service!r}; known: {sorted(pc.PROFILES)} "
                                 "(use 'generic' for an unlisted service)")
    texts = list(req.places or []) + ([req.place] if req.place else [])
    if not texts:
        raise HTTPException(422, "supply `place` or `places`")
    if len(texts) > MAX_PLACES:
        raise HTTPException(422, f"at most {MAX_PLACES} places per request; got {len(texts)}")
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(t: str):
        async with sem:
            try:
                return await resolve_place(t, req, profile)
            except HTTPException:
                raise
            except Exception as exc:            # one bad string must not sink the batch
                logger.warning("genealogy: %r failed: %s", t, exc, exc_info=True)
                return {"input": t, "status": "error", "error": type(exc).__name__, "candidates": []}

    results = await asyncio.gather(*[one(t) for t in texts])
    return {"service": profile.name, "profile": {"separator": req.separator or profile.separator,
                                                 "order": req.order or profile.order, "basis": profile.basis},
            "results": results}
