"""Tier-2 country fallback and the disputed-claims overlay for ccode resolution.

Two additions to ``ccode_enrichment``, both arising from place#173.

**Tier-2 fallback.** The primary source (geoBoundaries HPSC) is ADM0 at
sovereign-state level, so twelve ISO territories it does not carve out resolve
to *nothing*: ``HK`` (13,060 places), ``SJ`` (10,053), ``TF``, ``GS``, ``JE``,
``MO``, ``PM``, ``MF``, ``SX``, ``CC``, ``BV``, ``HM`` — 32,703 places in total.
BNDA still covers them, so it is retained purely as a fallback tier.

The tiers are kept **strictly separate and consulted in order** rather than
merged into one polygon set. Merging two sources at different resolutions would
put a 232-vertex BNDA outline next to a 73,663-vertex geoBoundaries one along a
shared border, and every disagreement between them becomes a sliver where a
place is claimed by both countries or by neither. Consulting tier 2 only when
tier 1 returned *nothing* makes that impossible: a place either falls inside a
primary polygon and is answered there, or it falls in a hole the primary does
not cover at all.

**Disputed-claims overlay.** geoBoundaries represents only 4 of 13 tested
disputes by overlapping claims (Gilgit-Baltistan, Aksai Chin, Arunachal
Pradesh, Taiwan). For the rest it picks a single claimant, and inconsistently —
*de jure* for Crimea and Northern Cyprus, *de facto* for the Golan, Western
Sahara and the Kurils. Since the primary *does* return an answer for those, no
fallback fires and the narrowing would be invisible.

The overlay therefore runs **in addition to** tier 1: where a place falls inside
a declared disputed territory, every claimant is attested. This replaces
``ccode_enrichment.BNDA_DISPUTED_CLAIMANTS``, a hardcoded table of four disputes
that omits Crimea, the Golan, Taiwan, Northern Cyprus, Abkhazia, South Ossetia
and the Falklands — so those already go unexamined today.

Entries are **data, deliberately**: WHG should not encode a sovereignty position
in a Python literal that nobody revisits. Each carries its evidence and the date
it was decided.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

DISPUTED_CLAIMS_FILE = Path(__file__).with_name("data") / "disputed_claims.json"


def load_disputed_claims(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the disputed-territory overlay; empty list if absent."""
    p = path or DISPUTED_CLAIMS_FILE
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("territories", [])


def claimants_for_point(
    lon: float,
    lat: float,
    territories: Iterable[dict[str, Any]],
) -> list[str]:
    """Every claimant ISO code whose declared territory contains the point.

    Uses the territory's ``bbox`` as a cheap reject before any geometry work;
    most places are nowhere near a disputed zone.
    """
    from shapely.geometry import Point, shape

    point = Point(lon, lat)
    out: list[str] = []
    for territory in territories:
        bbox = territory.get("bbox")
        if bbox and not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        geom = territory.get("_shape")
        if geom is None:
            raw = territory.get("geometry")
            if not raw:
                continue
            geom = shape(raw)
            territory["_shape"] = geom          # cache across calls
        try:
            if geom.contains(point):
                out.extend(territory.get("claimants", []))
        except Exception:
            continue
    return sorted(set(out))


def apply_overlay(
    ccodes: list[str],
    lon: float | None,
    lat: float | None,
    territories: Iterable[dict[str, Any]],
) -> list[str]:
    """Union the primary answer with any disputed claimants for the point.

    Additive by design. The overlay never *removes* a code the source
    returned — asserting that a source is wrong about who administers a
    territory is a different and much larger claim than asserting that more
    than one party claims it.
    """
    if lon is None or lat is None or not territories:
        return ccodes
    extra = claimants_for_point(lon, lat, territories)
    if not extra:
        return ccodes
    return sorted(set(ccodes) | set(extra))


def resolve_tiered(
    primary_fn,
    fallback_fn,
    *,
    lon: float | None = None,
    lat: float | None = None,
    territories: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[str], str]:
    """Resolve against tier 1, falling back to tier 2 only on an empty result.

    Returns ``(ccodes, tier)`` where tier is ``"primary"``, ``"fallback"`` or
    ``"none"`` — recorded so the fallback's usage is measurable rather than
    invisible. A fallback rate that drifts upward means the primary source has
    developed holes, which is exactly the kind of change that otherwise shows
    up only as a slow loss of country codes.
    """
    codes = list(primary_fn() or [])
    tier = "primary" if codes else "none"
    if not codes and fallback_fn is not None:
        codes = list(fallback_fn() or [])
        if codes:
            tier = "fallback"
    if territories:
        codes = apply_overlay(codes, lon, lat, territories)
    return sorted(set(codes)), tier


# ---------------------------------------------------------------------------
# Full-BNDA tier 2 (place#173, 6 August 2026)
# ---------------------------------------------------------------------------

BNDA_SOURCE_FILE = Path(__file__).with_name("data") / "un_bnda_countries.geojson"


def _wrap_lon(lon: float) -> float:
    """BNDA represents the US Aleutians with unwrapped longitudes up to 191."""
    if lon > 180.0:
        return lon - 360.0
    if lon < -180.0:
        return lon + 360.0
    return lon


def _normalize_lons(geom: dict) -> dict:
    """Fold out-of-range longitudes; mirrors authorities/un-countries.py."""
    def _walk(node):
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)):
                return [_wrap_lon(float(node[0]))] + [float(x) for x in node[1:]]
            return [_walk(x) for x in node]
        return node

    if not isinstance(geom, dict) or "coordinates" not in geom:
        return geom
    out = dict(geom)
    out["coordinates"] = _walk(geom.get("coordinates"))
    return out


def load_full_bnda_tier(path: Path | None = None) -> list[tuple[str, Any]]:
    """Return [(ccode, shapely geometry)] for **every** BNDA country.

    Why the whole set rather than only the countries geoBoundaries lacks:

    ``split_by_tier`` puts a country in tier 2 only when *all* its geometries
    are BNDA-sourced, i.e. only the ~18 that geoBoundaries does not carve out.
    So a place just outside geoBoundaries' finer coastline got tier 1 = nothing,
    then tier 2 = a set that does not contain its country either, and ended with
    **no country code at all**. That is not hypothetical: 464 places across
    ``VI`` (33), ``AS`` (96), ``GU`` (27), ``MP`` (28) and ``BQ`` (280) came out
    uncoded on the 5 Aug 2026 corpus run, all of them in territories geoBoundaries
    *does* cover, so the fallback could never fire for them.

    Widening tier 2 to the full BNDA set does **not** reintroduce the
    mixed-resolution sliver problem the tiers exist to prevent. That risk comes
    from merging two polygon sets so a border is described by both at once. Here
    the ordering is unchanged and absolute: tier 2 is consulted only where tier 1
    returned nothing, so there is no tier-1 answer for a tier-2 answer to
    disagree with. A coarser outline is strictly better than no country.

    Loaded from the committed BNDA GeoJSON rather than the `un` documents,
    because ``create_country_place_doc`` *replaces* BNDA's geometry with the
    geoBoundaries one where it has an override — so the `un` doc for ``VI`` no
    longer carries a BNDA polygon at all.
    """
    from shapely.geometry import shape

    p = path or BNDA_SOURCE_FILE
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as fh:
        data = json.load(fh)

    out: list[tuple[str, Any]] = []
    for feat in (data.get("features") or []):
        props = feat.get("properties") or {}
        cc = (props.get("iso2cd") or "").strip().upper()
        geom = feat.get("geometry")
        if not cc or not geom:
            continue
        try:
            shp = shape(_normalize_lons(geom))
        except Exception:
            continue
        if shp is None or shp.is_empty:
            continue
        out.append((cc, shp))
    return out


class BndaFallbackIndex:
    """Spatial index over the full BNDA set, for places tier 1 could not place.

    An STRtree rather than the H3 prefilter tier 1 uses: tier 2 now spans every
    country, it is consulted for a small minority of places, and BNDA outlines
    are coarse enough that a bbox query plus a prepared predicate is both
    simpler and faster than maintaining a second cell index.
    """

    def __init__(self, entries: list[tuple[str, Any]] | None = None):
        from shapely.prepared import prep
        from shapely.strtree import STRtree

        self._entries = entries if entries is not None else load_full_bnda_tier()
        self._geoms = [g for _cc, g in self._entries]
        self._ccodes = [cc for cc, _g in self._entries]
        self._prepared = [prep(g) for g in self._geoms]
        self._tree = STRtree(self._geoms) if self._geoms else None

    def __len__(self) -> int:
        return len(self._entries)

    def ccodes_for(self, place_geom) -> list[str]:
        """Every BNDA country whose polygon intersects ``place_geom``.

        Ordered by descending overlap in the place's own dimension, matching
        ``_filter_by_containment``: an areal place needs areal overlap, a linear
        one linear overlap, so a shared border is not mistaken for containment.
        """
        if self._tree is None or place_geom is None or place_geom.is_empty:
            return []

        is_point = place_geom.geom_type in ("Point", "MultiPoint")
        is_linear = place_geom.geom_type in (
            "LineString", "MultiLineString", "LinearRing")
        place_measure = place_geom.length if is_linear else place_geom.area

        matches: list[tuple[str, float]] = []
        for idx in self._tree.query(place_geom):
            i = int(idx)
            if not self._prepared[i].intersects(place_geom):
                continue
            cc = self._ccodes[i]
            if is_point:
                matches.append((cc, 1.0))
                continue
            # Same covers() fast path as tier 1: no overlay when the country
            # wholly contains the place.
            if self._prepared[i].covers(place_geom):
                if place_measure > 0:
                    matches.append((cc, place_measure))
                continue
            try:
                inter = self._geoms[i].intersection(place_geom)
            except Exception:
                continue
            if inter.is_empty:
                continue
            measure = inter.length if is_linear else inter.area
            if measure > 0:
                matches.append((cc, measure))

        if not matches:
            return []
        if is_point:
            return sorted({cc for cc, _ in matches})
        matches.sort(key=lambda t: t[1], reverse=True)
        seen, ordered = set(), []
        for cc, _ in matches:
            if cc not in seen:
                seen.add(cc)
                ordered.append(cc)
        return ordered
