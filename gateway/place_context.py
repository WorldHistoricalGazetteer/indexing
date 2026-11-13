# gateway/place_context.py
"""Pure place-string parsing for genealogy-style input. No Elasticsearch, no I/O.

Genealogy services export a place as ONE comma-delimited string carrying its own
administrative hierarchy ("Ballymena, County Antrim, Ireland"). The matching model
cannot use that text: hierarchy has to arrive as RESOLVED PLACE IDENTIFIERS
(`contained_in`, `countries`), never as commas inside the query. This module does the
first half of that job (split, order, label, recognise the country) and leaves the
second half (resolving each level to an identifier) to ``genealogy.py``.

What is documented and what is assumed
--------------------------------------
Only the GEDCOM convention is taken from a specification: a ``PLAC`` value is a
comma-separated jurisdiction list, and the file header may declare its order with
``PLAC.FORM`` (e.g. ``City, County, State, Country``). Every other service profile is an
ASSUMPTION about that service's export and is marked ``basis="assumed"`` so a consumer
can see it. Nothing here claims knowledge of a vendor's internals; the ``order``,
``separator`` and ``form`` request fields exist so a caller can state the truth, and
``order="auto"`` infers it from where the country sits.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

SMALL_TO_LARGE = "small_to_large"   # "Town, County, Country": the GEDCOM default
LARGE_TO_SMALL = "large_to_small"   # "Country, County, Town": e.g. some East Asian / Hungarian styles
AUTO = "auto"
ORDERS = (SMALL_TO_LARGE, LARGE_TO_SMALL, AUTO)


@dataclass(frozen=True)
class Profile:
    name: str
    separator: str
    order: str
    basis: str            # "gedcom-spec" | "assumed"
    note: str


PROFILES: dict[str, Profile] = {p.name: p for p in (
    Profile("gedcom", ",", SMALL_TO_LARGE, "gedcom-spec",
            "GEDCOM 5.5.1 PLAC: comma-separated jurisdictions, smallest first unless the header's "
            "PLAC.FORM says otherwise. Pass the header value as `form`."),
    Profile("treealive", ",", SMALL_TO_LARGE, "gedcom-spec",
            "TreeAlive ingests GEDCOM files, so GEDCOM PLAC rules apply."),
    Profile("familysearch", ",", SMALL_TO_LARGE, "assumed",
            "Assumed: standardised places read 'City, County, State, Country'."),
    Profile("ancestry", ",", SMALL_TO_LARGE, "assumed",
            "Assumed: comma-delimited, smallest first. Verify against a real export."),
    Profile("myheritage", ",", SMALL_TO_LARGE, "assumed",
            "Assumed GEDCOM-style export. Verify against a real export."),
    Profile("findmypast", ",", SMALL_TO_LARGE, "assumed",
            "Assumed: comma-delimited, smallest first. Verify against a real export."),
    Profile("wikitree", ",", SMALL_TO_LARGE, "assumed",
            "Assumed: 'Town, County, State, Country'."),
    Profile("generic", ",", AUTO, "assumed",
            "Unknown service: comma-delimited, order inferred from where the country sits."),
)}


# --------------------------------------------------------------------------- normalisation

_BRACKETS = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_WS = re.compile(r"\s+")


def clean(text: str) -> str:
    """Trim, drop bracketed qualifiers ("(now Ballymena)"), collapse whitespace."""
    t = unicodedata.normalize("NFKC", text or "")
    t = _BRACKETS.sub(" ", t)
    return _WS.sub(" ", t).strip(" \t.;:")


def fold(text: str) -> str:
    """Case/punctuation-insensitive key for table lookups: 'U.S.A.' -> 'u s a'."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = re.sub(r"[.’'`]", "", t)           # 'St. Mary's' -> 'st marys'
    t = re.sub(r"[^\w\s]", " ", t)
    return _WS.sub(" ", t).strip()


_ADMIN_WORDS = (
    "county", "co", "shire", "state of", "province of", "provincia de", "provincia di", "parish",
    "borough", "district", "department", "departement", "oblast", "kreis", "landkreis", "gemeinde",
    "township", "twp", "region", "commune", "municipality", "city of", "town of", "barony",
)
_ADMIN_RE = re.compile(
    r"^(?:" + "|".join(sorted(map(re.escape, _ADMIN_WORDS), key=len, reverse=True)) + r")\.?\s+|"
    r"\s+(?:" + "|".join(sorted(map(re.escape, _ADMIN_WORDS), key=len, reverse=True)) + r")\.?$",
    re.IGNORECASE)


def strip_admin_words(text: str) -> str:
    """'County Antrim' / 'Antrim County' / 'Co. Antrim' -> 'Antrim'. Returns the input unchanged
    if stripping would leave nothing."""
    out = _ADMIN_RE.sub("", clean(text)).strip()
    return out or clean(text)


# --------------------------------------------------------------------------- countries

# A deliberately small table of the aliases that real genealogy data uses and that an
# index lookup resolves badly (initialisms, "Holland", native-language names, the constituent
# countries of the UK). Every other country is recognised by looking it up in the index.
# value: (ISO 3166-1 alpha-2, constituent)  -- constituent=True means the name is ALSO an admin
# area of that country ("England" is a country code GB *and* a region of it).
_COUNTRIES: dict[str, tuple[str, bool]] = {}


def _add(code: str, *names: str, constituent: bool = False) -> None:
    for n in names:
        _COUNTRIES[fold(n)] = (code, constituent)


_add("US", "USA", "US", "U.S.A.", "United States", "United States of America", "America")
_add("GB", "UK", "U.K.", "United Kingdom", "Great Britain", "Britain", "GB")
for _n in ("England", "Scotland", "Wales", "Northern Ireland"):
    _add("GB", _n, constituent=True)
_add("IE", "Ireland", "Republic of Ireland", "Eire", "Éire")
_add("DE", "Germany", "Deutschland")
_add("NL", "Netherlands", "The Netherlands", "Holland", "Nederland")
_add("FR", "France")
_add("ES", "Spain", "España", "Espana")
_add("IT", "Italy", "Italia")
_add("PT", "Portugal")
_add("BE", "Belgium", "Belgique", "België", "Belgie")
_add("CH", "Switzerland", "Schweiz", "Suisse", "Svizzera")
_add("AT", "Austria", "Österreich", "Osterreich")
_add("SE", "Sweden", "Sverige")
_add("NO", "Norway", "Norge")
_add("DK", "Denmark", "Danmark")
_add("FI", "Finland", "Suomi")
_add("PL", "Poland", "Polska")
_add("CZ", "Czech Republic", "Czechia", "Česko", "Cesko")
_add("SK", "Slovakia")
_add("HU", "Hungary", "Magyarország", "Magyarorszag")
_add("RO", "Romania")
_add("GR", "Greece", "Hellas")
_add("TR", "Turkey", "Türkiye", "Turkiye")
_add("RU", "Russia", "Russian Federation")
_add("UA", "Ukraine")
_add("CA", "Canada")
_add("AU", "Australia")
_add("NZ", "New Zealand")
_add("ZA", "South Africa")
_add("IN", "India")
_add("CN", "China")
_add("JP", "Japan")
_add("MX", "Mexico", "México")
_add("BR", "Brazil", "Brasil")
_add("AR", "Argentina")


def country_of(component: str) -> tuple[str, bool] | None:
    """(ISO2, is_constituent) when the component is a known country alias, else None."""
    return _COUNTRIES.get(fold(component))


# --------------------------------------------------------------------------- GEDCOM PLAC.FORM

_LEVEL_WORDS = {
    "locality": ("city", "town", "village", "locality", "place", "hamlet", "parish", "township",
                 "settlement", "ort", "ville"),
    "admin2": ("county", "district", "borough", "shire", "kreis", "department", "municipality",
               "commune", "barony"),
    "admin1": ("state", "province", "region", "oblast", "land", "prefecture", "canton", "voivodeship"),
    "country": ("country", "nation"),
}


def parse_form(form: str | None, separator: str = ",") -> list[str] | None:
    """GEDCOM ``PLAC.FORM`` ("City, County, State, Country") -> level labels, in FORM order.
    Unknown keywords become "other". None when no form was given."""
    if not form or not form.strip():
        return None
    out = []
    for part in form.split(separator):
        w = fold(part)
        out.append(next((lvl for lvl, words in _LEVEL_WORDS.items() if w in words), "other"))
    return out


# --------------------------------------------------------------------------- parsing


@dataclass
class Component:
    raw: str
    norm: str                      # cleaned text
    level: str | None = None       # locality | admin2 | admin1 | country | other | None (unlabelled)
    blank: bool = False            # an empty slot ("Holy Land, , Ontario, Canada")
    country: str | None = None     # ISO2 when recognised as a country alias
    constituent: bool = False


@dataclass
class ParsedPlace:
    raw: str
    components: list[Component]                      # NARROWEST first, whatever the input order
    order_applied: str                               # small_to_large | large_to_small
    order_source: str                                # explicit | profile | auto | form
    separator: str
    form_used: list[str] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def filled(self) -> list[Component]:
        return [c for c in self.components if not c.blank]


def split_place(text: str, separator: str = ",") -> list[str]:
    """Split keeping blank slots: positions matter when a FORM names the levels."""
    return [p.strip() for p in (text or "").split(separator)]


def parse_place(text: str, *, profile: Profile, separator: str | None = None, order: str | None = None,
                form: str | None = None) -> ParsedPlace:
    sep = separator or profile.separator
    parts = split_place(text, sep)
    comps = [Component(raw=p, norm=clean(p), blank=not clean(p)) for p in parts]
    for c in comps:
        if not c.blank:
            hit = country_of(c.norm)
            if hit:
                c.country, c.constituent = hit

    want = (order or profile.order).lower()
    if want not in ORDERS:
        want = profile.order
    source = "explicit" if order else "profile"
    filled = [c for c in comps if not c.blank]
    notes: list[str] = []
    if want == AUTO:
        if len(filled) >= 2 and filled[-1].country and not filled[0].country:
            want, source = SMALL_TO_LARGE, "auto"
        elif len(filled) >= 2 and filled[0].country and not filled[-1].country:
            want, source = LARGE_TO_SMALL, "auto"
        else:
            want, source = SMALL_TO_LARGE, "auto"
            notes.append("order could not be inferred (no country at either end); assumed small_to_large")

    labels = parse_form(form, sep)
    if labels:
        source = "form" if not order else source
        # FORM lists levels in the same order as the components. GEDCOM marks a missing level with a
        # BLANK slot, so a length mismatch is ambiguous. Anchor both ends (the first component is
        # the first FORM level, the last is the last) and give the middle components the labels
        # nearest the BROAD end: "Provo, Utah, USA" under "City, County, State, Country" reads
        # locality / state / country, not county.
        if len(labels) != len(comps):
            notes.append(f"PLAC.FORM has {len(labels)} levels but the place has {len(comps)} components; "
                         "both ends anchored, middle levels assigned from the broad end")
            n = len(comps)
            if n <= 1:
                labels = labels[:n]
            elif len(labels) > n:
                mid_labels = labels[1:-1]
                k = n - 2
                mid = (mid_labels[-k:] if want == SMALL_TO_LARGE else mid_labels[:k]) if k > 0 else []
                labels = [labels[0]] + mid + [labels[-1]]
            else:
                labels = labels + [None] * (n - len(labels))
        for c, lvl in zip(comps, labels):
            c.level = lvl

    # NARROWEST first, regardless of input order
    narrow_first = comps if want == SMALL_TO_LARGE else list(reversed(comps))
    if not labels:
        # Position-based labelling: only the country end is reliable.
        last_filled = next((c for c in reversed(narrow_first) if not c.blank), None)
        if last_filled is not None and last_filled.country:
            last_filled.level = "country"
    return ParsedPlace(raw=text, components=narrow_first, order_applied=want, order_source=source,
                       separator=sep, form_used=labels, notes=notes)
