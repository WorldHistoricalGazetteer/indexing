"""GB-STAMP typing from the RELIABLE signals (option A — see developer/plan-gb1900-typing.md §0b).

Fine font-style typing plateaued (~0.25 on the upright/italic axis), so we type from the signals
that DO work, at an honest confidence tier:
  1. tier-0 text rules (abbrev / keyword / numeric) already type ~59% -> high confidence.
  2. residual proper-names -> coarse type from SIZE x CASE x (confident) style, medium/low confidence.
  3. font-style is a LOW-CONFIDENCE enrichment, applied only when the classifier is confident.

`assign_type(rec)` returns {os_kind, size_band, confidence, source} for a label record carrying
{tier0_rule, text, cap_height_m, allcaps, style?, style_conf?}. os_kind is a coarse OS lettering
kind (mapped to AAT downstream); it is NOT a claim of fine font identity.
"""
from __future__ import annotations

# size bands (ground cap-height, metres) — natural gaps seen in the HITL data (§0b)
SIZE_BANDS = [(0, 30, "small"), (30, 55, "medium"), (55, 1e9, "large")]

# OS abbreviations that are unambiguous type signals (checked-transcription route only)
ABBREV_KIND = {
    "F.P.": "footpath", "B.M.": "benchmark", "S.P.": "signpost", "W": "well", "P": "pump",
    "Ch.": "church", "Sch.": "school", "P.O.": "post-office", "Sp.": "spring", "Fm.": "farm",
    "Ho.": "house", "Mon.": "monument", "P.H.": "public-house",
}

def size_band(cap_h_m):
    if cap_h_m is None:
        return None
    for lo, hi, name in SIZE_BANDS:
        if lo <= cap_h_m < hi:
            return name
    return None

def _norm_abbrev(t):
    return (t or "").strip().rstrip(".").upper()

def assign_type(rec, style_conf_min=0.75):
    """rec: {tier0_rule, text, cap_height_m, allcaps, style?, style_conf?} -> dict."""
    text = (rec.get("text") or "").strip()
    rule = rec.get("tier0_rule")
    band = size_band(rec.get("cap_height_m"))
    style, sconf = rec.get("style"), rec.get("style_conf", 0.0)

    # 1. checked-abbreviation route (highest confidence)
    key = None
    for k in ABBREV_KIND:
        if _norm_abbrev(text) == _norm_abbrev(k):
            key = k; break
    if rule == "abbrev" and key:
        return dict(os_kind=ABBREV_KIND[key], size_band=band, confidence="high", source="abbrev-rule")
    if rule == "numeric" or (text and text.replace(".", "").replace(",", "").isdigit()):
        return dict(os_kind="numeric", size_band=band, confidence="high", source="numeric")
    if rule == "keyword":
        return dict(os_kind="keyword-typed", size_band=band, confidence="high", source="keyword-rule")

    # 2. road/street by text suffix (text is decisive for roads)
    up = text.upper()
    if any(up.endswith(s) or (" " + s) in (" " + up) for s in ("ROAD", "STREET", "LANE", "TERRACE", "AVENUE")):
        return dict(os_kind="road", size_band=band, confidence="high", source="road-suffix")

    # 3. residual proper-name -> coarse from confident style + size/case
    if style and sconf >= style_conf_min and style in ("serif_italic", "slab_italic"):
        # italic serif/slab on OS six-inch = water / physical features
        return dict(os_kind="water-or-feature", size_band=band, confidence="medium",
                    source=f"style:{style}", style_conf=round(sconf, 2))
    if rec.get("allcaps") and band in ("large", "medium"):
        return dict(os_kind="admin-or-place-caps", size_band=band, confidence="low", source="case+size")

    # 4. unresolved residual — carry size/case for later
    return dict(os_kind="settlement-or-unknown", size_band=band, confidence="low",
                source="residual", allcaps=bool(rec.get("allcaps")))
