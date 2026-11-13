# gateway/familysearch.py
"""`mode: "familysearch"` for /api/reconcile: FamilySearch "Read Places" query syntax.

https://developers.familysearch.org/main/reference/readplaces

The reconcile `query` carries a FamilySearch `q` string, e.g.

    name:"par*,france" +date:1800/1900 +parentId:un:fra~

which is translated, BEFORE anything is searched, into this gateway's own parameters. The
translation is LEXICAL ONLY: no phonetic (KNN) pass ever runs in this mode, so results are
what the characters of the name say (exact, prefix, wildcard, or edit-distance fuzzy), which
is what FamilySearch's own matching promises. Every parameter that cannot be honoured is
reported in the response's ``familysearch.warnings`` and NOT applied: nothing is silently
dropped and nothing is silently invented.

Mapping (see the user documentation for the full table):

    name:X            exact, case-insensitive               -> mode exactci
    name:X~           edit-distance fuzzy, no phonetics     -> mode lexfuzzy
    name:a*b / a?b    wildcard (never a leading wildcard)   -> mode wildcard
    partialName:X     type-ahead                            -> mode starts
    name:"A,B,C"      A within B within C                   -> A + ccodes / contained_in
    +date:Y or Y/Y    required year window                  -> start_year / end_year
    +typeId:186       required type (known ids only)        -> fclasses / types
    +parentId:ID[~]   required parent                       -> contained_in
    latitude, longitude, distance   proximity               -> lat / lng / radius (km)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from . import place_context as pc

KNOWN_KEYS = {"name", "partialname", "date", "typeid", "typegroupid", "parentid",
              "latitude", "longitude", "distance", "placehint"}

# FamilySearch place-type ids that are documented in the Read Places reference. Anything not
# listed is reported and ignored; callers can also pass a WHG source-type identifier
# ("A.PCLI") or an AAT id ("aat:300008347") as the value.
FS_TYPE_IDS: dict[str, dict] = {
    "186": {"label": "city", "fclasses": ["P"]},
}

_DISPLAY = {"typeid": "typeId", "typegroupid": "typeGroupId", "parentid": "parentId",
            "placehint": "placeHint", "partialname": "partialName"}
_PAIR = re.compile(r"""\s*([+-]?)([A-Za-z]+):""")


@dataclass
class Pair:
    key: str                 # lower-cased
    value: str               # raw, quotes removed, escapes KEPT
    required: bool = False
    excluded: bool = False
    fuzzy: bool = False
    quoted: bool = False


def tokenize(q: str) -> list[Pair]:
    """`name:value` pairs separated by whitespace; values with spaces/commas are double-quoted;
    a trailing `~` means fuzzy (name) or any-descendant (parentId); `+`/`-` prefix = required/exclude."""
    pairs: list[Pair] = []
    pos, n = 0, len(q or "")
    while pos < n:
        while pos < n and q[pos].isspace():
            pos += 1
        if pos >= n:
            break
        m = _PAIR.match(q, pos)
        if not m:
            raise ValueError(f"cannot parse q at position {pos}: expected name:value, found {q[pos:pos+20]!r}")
        op, key = m.group(1), m.group(2)
        pos = m.end()
        quoted = False
        if pos < n and q[pos] == '"':
            quoted = True
            pos += 1
            buf = []
            while pos < n and q[pos] != '"':
                if q[pos] == "\\" and pos + 1 < n:
                    buf.append(q[pos:pos + 2]); pos += 2
                else:
                    buf.append(q[pos]); pos += 1
            if pos >= n:
                raise ValueError("unterminated double quote in q")
            pos += 1
            value = "".join(buf)
        else:
            start = pos
            while pos < n and not q[pos].isspace():
                if q[pos] == "\\" and pos + 1 < n:
                    pos += 2
                else:
                    pos += 1
            value = q[start:pos]
        fuzzy = False
        if value.endswith("~") and not value.endswith("\\~"):
            value, fuzzy = value[:-1], True
        elif quoted and pos < n and q[pos] == "~":
            fuzzy = True
            pos += 1
        pairs.append(Pair(key=key.lower(), value=value, required=op == "+", excluded=op == "-",
                          fuzzy=fuzzy, quoted=quoted))
    return pairs


_UNESC_WILD = re.compile(r"(?<!\\)[*?]")


def has_wildcard(value: str) -> bool:
    return bool(_UNESC_WILD.search(value))


def unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def split_hierarchy(value: str) -> list[str]:
    """Split on UNESCAPED commas: 'par*,france' -> ['par*', 'france']."""
    parts, buf, i = [], [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            buf.append(value[i:i + 2]); i += 2
        elif ch == ",":
            parts.append("".join(buf).strip()); buf = []; i += 1
        else:
            buf.append(ch); i += 1
    parts.append("".join(buf).strip())
    return parts


def parse_years(value: str) -> tuple[int | None, int | None]:
    """GEDCOM X simple date at year granularity: '1823', '1800/1900', '1800/', '/1900'."""
    v = value.strip()
    m = re.fullmatch(r"(-?\d{1,4})?(?:/(-?\d{1,4})?)?", v)
    if not v or not m:
        raise ValueError(f"unsupported date {value!r}; expected YYYY or YYYY/YYYY")
    a, b = m.group(1), m.group(2)
    if "/" not in v:
        return int(a), int(a)
    return (int(a) if a else None), (int(b) if b else None)


def parse_distance(value: str) -> float:
    """'45K' -> 45 km; '10M' or '10' -> miles (FamilySearch's default) -> km."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KkMm]?)", value.strip())
    if not m:
        raise ValueError(f"unsupported distance {value!r}; expected e.g. 45K or 10M")
    n, unit = float(m.group(1)), m.group(2).upper()
    return n if unit == "K" else n * 1.609344


@dataclass
class Mapped:
    query: str | None = None
    mode: str = "exactci"
    parents: list[str] = field(default_factory=list)       # narrow -> broad, as written
    contained_in: list[str] = field(default_factory=list)
    ccodes: list[str] = field(default_factory=list)
    fclasses: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    start_year: int | None = None
    end_year: int | None = None
    lat: float | None = None
    lng: float | None = None
    radius_km: float | None = None
    warnings: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)


def translate(pairs: list[Pair]) -> Mapped:
    """Pure: FamilySearch pairs -> WHG parameters. Raises ValueError for q that FamilySearch
    itself would reject."""
    m = Mapped()
    by: dict[str, list[Pair]] = {}
    for p in pairs:
        if p.key not in KNOWN_KEYS:
            m.warnings.append(f"unknown parameter '{p.key}' ignored")
            m.ignored.append(p.key)
            continue
        by.setdefault(p.key, []).append(p)

    name_p = (by.get("name") or [None])[0]
    part_p = (by.get("partialname") or [None])[0]
    if part_p is not None:
        if name_p is not None:
            m.warnings.append("partialName given: name ignored (as in FamilySearch)")
        value, mode = part_p.value, "starts"
        if has_wildcard(value):
            m.warnings.append("wildcards in partialName are unspecified in FamilySearch; treated as literal text")
        parts = [unescape(value)]
    elif name_p is not None:
        parts = split_hierarchy(name_p.value)
        first = parts[0]
        wild = has_wildcard(first)
        if wild and name_p.fuzzy:
            raise ValueError("`~` is incompatible with unescaped wildcards in name")
        if wild and re.match(r"[*?]", first):
            raise ValueError("a wildcard must not appear at the beginning of the name")
        mode = "wildcard" if wild else ("lexfuzzy" if name_p.fuzzy else "exactci")
        parts = [first if wild else unescape(first)] + [unescape(x) for x in parts[1:]]
    else:
        parts, mode = [], "exactci"

    m.mode = mode
    if parts:
        m.query = parts[0]
        m.parents = [x for x in parts[1:] if x]

    for p in by.get("date", []):
        if p.excluded:
            m.warnings.append("-date is not supported; ignored")
        elif not p.required:
            m.warnings.append("optional date (no '+') only influences ranking in FamilySearch; "
                              "WHG does not rank by date, so it was not applied (use +date to filter)")
        else:
            m.start_year, m.end_year = parse_years(p.value)

    for key in ("typeid", "typegroupid"):
        for p in by.get(key, []):
            if p.excluded or not p.required:
                m.warnings.append(f"{_DISPLAY[key]}: only '+' (required) is supported; "
                                  f"{'exclusion' if p.excluded else 'optional matching'} not applied")
                continue
            v = p.value.strip()
            if key == "typeid" and v in FS_TYPE_IDS:
                m.fclasses += FS_TYPE_IDS[v]["fclasses"]
            elif key == "typeid" and v.lower().startswith("aat:"):
                m.types.append(v.lower())
            elif key == "typeid" and "." in v:
                m.types.append(v)                      # WHG source-type identifier, e.g. A.PCLI
            else:
                m.warnings.append(f"{_DISPLAY[key]}:{v} is not a known FamilySearch type id in the WHG crosswalk; "
                                  "ignored. Pass a WHG type instead (e.g. typeId:A.PCLI or typeId:aat:300008347)")
                m.ignored.append(f"{_DISPLAY[key]}:{v}")

    for p in by.get("parentid", []):
        if p.excluded:
            m.warnings.append("-parentId (exclude places within) is not supported; ignored")
            continue
        if not p.required:
            m.warnings.append("optional parentId only influences ranking in FamilySearch; not applied "
                              "(use +parentId to filter)")
            continue
        pid = p.value.strip()
        pid = pid[len("place:"):] if pid.startswith("place:") else pid
        if not p.fuzzy:
            m.warnings.append("parentId without '~' means DIRECT parents only in FamilySearch; WHG has no direct-parent "
                              "relation, so any containment (descendant) is applied")
        m.contained_in.append(pid)

    lat = (by.get("latitude") or [None])[0]
    lng = (by.get("longitude") or [None])[0]
    dist = (by.get("distance") or [None])[0]
    if lat is not None or lng is not None or dist is not None:
        if lat is None or lng is None or dist is None:
            m.warnings.append("latitude, longitude and distance must all be given for a proximity search; ignored")
        else:
            m.lat, m.lng, m.radius_km = float(lat.value), float(lng.value), parse_distance(dist.value)

    for p in by.get("placehint", []):
        m.warnings.append("placeHint is not supported; ignored")
        m.ignored.append("placeHint")

    if m.query is None and not (m.contained_in or m.lat is not None):
        raise ValueError("q must contain name or partialName (or a +parentId / proximity filter)")
    return m


async def translate_request(req):
    """ReconcileRequest(mode='familysearch') -> (ReconcileRequest with GATEWAY params, info dict)."""
    from . import genealogy as g
    try:
        pairs = tokenize(req.query or "")
        m = translate(pairs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"familysearch q: {exc}")

    ccodes = list(req.ccodes or []) + list(m.ccodes)
    contained = list(req.contained_in or []) + list(m.contained_in)
    resolved: list[dict] = []
    # name:"par*,france": resolve the parents (small -> large as FamilySearch writes them) to a
    # country code and an areal container. Unresolved parents are REPORTED, never guessed.
    if m.parents:
        comps = [pc.Component(raw=x, norm=pc.clean(x)) for x in m.parents]
        for c in comps:
            hit = pc.country_of(c.norm)
            if hit:
                c.country, c.constituent = hit
        country_comp = comps[-1]
        country = await g.resolve_country(country_comp, hint=None)
        admin = comps[:-1]
        if country.get("source") == "none":
            admin, country = comps, {"code": None, "source": "none"}
        if country.get("code"):
            ccodes.append(country["code"])
            resolved.append({"component": country_comp.norm, "kind": "country", **country})
        parent_id = None
        for c in reversed(admin):
            got = await g.resolve_container(c, country=country.get("code"), parent_id=parent_id)
            if got:
                parent_id = got["place_id"]
                resolved.append({"component": c.norm, "kind": "container", **got})
            else:
                m.warnings.append(f"parent '{c.norm}' could not be resolved to a WHG place; not applied")
        if parent_id:
            contained.append(parent_id)

    update: dict[str, Any] = {"query": m.query, "mode": m.mode, "variants": None}
    if ccodes:
        update["ccodes"] = list(dict.fromkeys(ccodes))
    if contained:
        update["contained_in"] = list(dict.fromkeys(contained))
    if m.fclasses:
        update["fclasses"] = list(dict.fromkeys((req.fclasses or []) + m.fclasses))
    if m.types:
        update["types"] = list(dict.fromkeys((req.types or []) + m.types))
    if m.start_year is not None:
        update["start_year"] = m.start_year
    if m.end_year is not None:
        update["end_year"] = m.end_year
    if m.lat is not None:
        update.update(lat=m.lat, lng=m.lng, radius=m.radius_km)
    new = req.model_copy(update=update)
    info = {
        "q": req.query,
        "pairs": [{"key": p.key, "value": p.value, "required": p.required, "excluded": p.excluded,
                   "fuzzy": p.fuzzy} for p in pairs],
        "phonetic": False,
        "mode_applied": m.mode,
        "applied": {k: v for k, v in update.items() if k not in ("variants",)},
        "resolved_parents": resolved,
        "warnings": m.warnings,
        "ignored": m.ignored,
    }
    return new, info
