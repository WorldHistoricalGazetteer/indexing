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
