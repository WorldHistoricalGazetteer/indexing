#!/usr/bin/env python3
"""
Which backend, if any, converts a given (lang, script) to IPA.

WHY THIS IS NOT A HAND-WRITTEN DICT
-----------------------------------
It used to be. `EPITRAN_LANG_MAP` in rebuild_toponyms_index.py lists 45
(lang, script) pairs. The environment has 254 Epitran modes installed across
203 ISO-639-3 codes -- 115 of them custom tables built by this project and
installed by scripts/install_epitran_extensions.sh. Measured on the 72.7M
corpus (6 Sep 2026), the gap between those two numbers is 15,823,375 toponyms
(21.76%) that no backend was ever asked to handle, in 215 (lang, script) cells,
all 215 of which produce real non-echo IPA when actually called.

A hand-written list silently loses capability every time a mode is added. So
routes are DERIVED from what is installed, and the hand-maintained part is only
the exceptions: neural overrides, and labels we decline to trust.

THE QUARANTINE IS NOT A BLOCKLIST OF LANGUAGES
----------------------------------------------
Wikidata carries one label per Wikipedia edition. Lsjbot mass-generated place
articles worldwide in Cebuano, Waray, Swedish, Minangkabau and Volapuk, so a
`ceb` label on an Austrian mountain records which wiki has an article, not the
language of the name. Measured on the corpus:

    lang    toponyms   from wd   name also under another lang
    ceb    2,786,505     98.4%    82.6%
    sv     1,825,578     94.4%    82.7%   <- already routed today
    nan      341,121     89.0%    73.1%
    sh       279,332     97.4%    80.6%
    mul      209,667     99.9%    88.6%
    war      161,194     91.5%    95.3%
    vo       141,241     89.7%    95.5%
    min      112,829     98.0%    99.1%

Running ceb-Latn phonology over "Navas del Marques" yields well-formed,
confident, wrong IPA. The mode is not broken -- it is being asked the wrong
question, which is why the mode-level verification cannot see this.

Quarantined cells are RECORDED with status "quarantined", never silently
dropped: a row exists for every toponym so that coverage always has a
denominator and a re-run never retries them blindly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Set, Tuple

# Script enum name -> the script subtag Epitran uses in its mode filenames.
SCRIPT_TAG: Dict[str, str] = {
    "LATIN": "Latn", "CYRILLIC": "Cyrl", "GREEK": "Grek", "ARABIC": "Arab",
    "HEBREW": "Hebr", "DEVANAGARI": "Deva", "BENGALI": "Beng",
    "TAMIL": "Taml", "TELUGU": "Telu", "MALAYALAM": "Mlym",
    "KANNADA": "Knda", "GUJARATI": "Gujr", "THAI": "Thai",
    "GEORGIAN": "Geor", "ARMENIAN": "Armn", "HANGUL": "Hang",
    "CJK": "Hans", "HIRAGANA": "Hrgn", "KATAKANA": "Ktkn",
    # ⚠ THE FIX. This table had 19 entries against `Script`'s 20, and the
    # missing one was the catch-all: `SCRIPT_TAG.get("OTHER")` returned None, so
    # `if iso3 and tag` at the bottom of `resolve` could never be true and every
    # one of these writing systems returned `no_route` with a correctly-derived
    # iso3 in hand. `mya-Mymr`, `pan-Guru`, `bod-Tibt`, `sin-Sinh`, `khm-Khmr`
    # and `sat-Olck` were hand-written, installed, and unreachable.
    #
    # A tag here is a FACT about the writing system, not a claim that a route
    # exists — `mode in self.modes` still gates that — so MONGOLIAN and
    # CANADIAN_ABORIGINAL are listed despite having no mode: they resolve to
    # `no_route` either way, and naming them keeps this table a description of
    # Unicode rather than of our current coverage.
    "MYANMAR": "Mymr",
    "GURMUKHI": "Guru",
    "TIBETAN": "Tibt",
    "SINHALA": "Sinh",
    "KHMER": "Khmr",
    "OL_CHIKI": "Olck",
    "TIFINAGH": "Tfng",
    "ETHIOPIC": "Ethi",
    "ORIYA": "Orya",
    "LAO": "Laoo",
    "MONGOLIAN": "Mong",
    "CANADIAN_ABORIGINAL": "Cans",
    "BOPOMOFO": "Bopo",
    "THAANA": "Thaa",
    "NKO": "Nkoo",
    "SYRIAC": "Syrc",
    "COPTIC": "Copt",
}

BACKEND_EPITRAN = "epitran"
BACKEND_CHARSIU = "charsiu"
BACKEND_PHONIKUD = "phonikud"

# Neural routes, which take precedence over any Epitran mode of the same name.
# ja+CJK is here because it was MISSING everywhere: to_ipa routed ja only for
# HIRAGANA and KATAKANA, so 465,177 Kanji toponyms fell through to the Epitran
# default branch, found no ('ja', CJK) entry and returned None. Verified
# against the shipped helper on 6 Sep: 12 of 12 sampled Kanji names -> None,
# while the Katakana control returned IPA.
NEURAL_ROUTES: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("he", "HEBREW"): (BACKEND_PHONIKUD, "he"),
    ("zh", "CJK"): (BACKEND_CHARSIU, "cmn"),
    ("gan", "CJK"): (BACKEND_CHARSIU, "cmn"),
    ("wuu", "CJK"): (BACKEND_CHARSIU, "cmn"),
    ("yue", "CJK"): (BACKEND_CHARSIU, "yue"),
    ("ko", "HANGUL"): (BACKEND_CHARSIU, "kor"),
    ("ko", "CJK"): (BACKEND_CHARSIU, "kor"),
    ("ja", "CJK"): (BACKEND_CHARSIU, "jpn"),
}

# Script-first routes that must beat the neural table (Kana is Epitran's, and
# CharsiuG2P only handles Kanji -- this was a v6 bugfix, preserved here).
SCRIPT_FIRST: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("ja", "HIRAGANA"): (BACKEND_EPITRAN, "jpn-Hrgn"),
    ("ja", "KATAKANA"): (BACKEND_EPITRAN, "jpn-Ktkn"),
}

# Language tags whose value is a Wikipedia edition, not a claim about the name.
# See the module docstring for the measurements behind each.
QUARANTINED_LANGS: Set[str] = {"ceb", "war", "min", "vo", "mul"}

# Tags that are not languages at all -- found in the live corpus.
NON_LANGUAGE_TAGS: Set[str] = {"genitive", "ar1", "lauc"}

# ISO 639-2 codes that mean "no usable language", as opposed to a language we
# happen not to support. They must resolve to `no_lang`, NOT `no_route`.
#
# WHY THE DISTINCTION IS LOAD-BEARING. The two statuses address different
# future work: `no_lang` is the queue for language identification, `no_route`
# is the queue for adding a G2P backend. `und` is ISO 639-2 for UNDETERMINED --
# semantically identical to an empty tag, and no Epitran mode will ever be
# written for it. Filing it under `no_route` would put 1.4M rows in the
# backend-work queue that no backend can ever serve, and take them out of the
# language-identification queue that is precisely where they belong.
#
# `mul` is handled separately in QUARANTINED_LANGS for a different reason (a
# Wikidata edition label, not a linguistic claim).
#
# 🛑 THIS GUARD IS NOT EVIDENCE THAT THE CASE OCCURS. Do not derive a forecast
# from it. The tgn fix (`lang or "und"`) re-keys `Name@` -> `Name@und` in the
# **places** index ONLY; the toponyms inventory normalises `und` back to None
# at rebuild_toponyms_index.py:935 before the id is built, so it holds `Name@`
# and this router never sees an `und` from that path. The two indices
# legitimately differ in shape and neither is wrong.
#
# An earlier note here sized ~1,398,790 rows as a re-keyed inventory
# population, and a `no_lang` forecast of 18,543,146 -> ~19,941,936 was drawn
# from it. Both are WITHDRAWN: those rows were always present as `Name@`, so
# there is no new inventory population and no growth. The guard remains as
# defensive correctness for an inventory built by some other path.
#
# ⚠ ALIGNED WITH AN EXISTING CONVENTION, not invented here. The toponyms build
# already canonicalises these away at rebuild_toponyms_index.py:935:
#     if lang and lang.lower() in ('und', 'zxx', 'mis', 'null', 'none'):
#         lang = None
# so an inventory produced by that path never presents them to this router at
# all. `null`/`none` are included anyway because an inventory built by any
# OTHER path could carry them, and without them they would fall through to
# `no_route` -- the wrong queue by the same argument that puts `und` in
# `no_lang`. Matching the set exactly means the two places cannot disagree
# about what counts as "no language".
UNDETERMINED_TAGS: Set[str] = {"und", "mis", "zxx", "null", "none"}

_MODE_RE = re.compile(r"^([a-z]{3})-([A-Za-z]+)$")

# Epitran implements some languages in CODE, with no CSV map to glob. English
# is the big one: there is no eng-Latn.csv, yet Epitran('eng-Latn') works via
# flite/lex_lookup. Deriving routes from the CSV directory alone therefore
# DROPS English -- ~8M toponyms, the single largest cell in the corpus. Caught
# by the end-to-end test, which reported 120 of 120 English rows as no_route.
#
# Anything added here is a claim that the mode loads on the target host. Run
# `python -m phonetics.ipa.preflight` to test that claim before a large run;
# the worker also records status="failed" for a mode that will not load, so a
# wrong entry is recorded rather than silent.
CODE_BACKED_MODES: Set[str] = {"eng-Latn"}


@dataclass(frozen=True)
class Route:
    backend: str
    mode: str          # epitran mode name, or charsiu/phonikud language tag
    reason: str        # why this route, for the audit trail


def installed_epitran_modes() -> Set[str]:
    """Mode names available in the INSTALLED epitran: the CSV maps on disk,
    plus the modes Epitran implements in code (see CODE_BACKED_MODES)."""
    import epitran
    from pathlib import Path
    d = Path(epitran.__file__).parent / "data" / "map"
    csv_modes = {p.stem for p in d.glob("*.csv") if _MODE_RE.match(p.stem)}
    return csv_modes | CODE_BACKED_MODES


def build_iso1_to_iso3() -> Dict[str, str]:
    try:
        import pycountry
    except ImportError:  # pragma: no cover
        return {}
    return {
        l.alpha_2: l.alpha_3 for l in pycountry.languages
        if getattr(l, "alpha_2", None) and getattr(l, "alpha_3", None)
    }


def normalise_lang(lang: Optional[str]) -> str:
    """Base subtag, lowercased. '' when there is nothing usable."""
    if not lang:
        return ""
    return lang.strip().split("-")[0].split("_")[0].split(":")[0].lower()


class RouteTable:
    """Resolves (lang, script) -> Route | None, deriving Epitran routes from
    the installed mode set."""

    def __init__(self, modes: Optional[Set[str]] = None,
                 quarantine: Optional[Set[str]] = None,
                 allow_quarantined: bool = False):
        self.modes = modes if modes is not None else installed_epitran_modes()
        self.iso1 = build_iso1_to_iso3()
        self.quarantine = QUARANTINED_LANGS if quarantine is None else quarantine
        self.allow_quarantined = allow_quarantined

    def to_iso3(self, lang: str) -> Optional[str]:
        base = normalise_lang(lang)
        if len(base) == 3:
            return base
        return self.iso1.get(base)

    def resolve(self, lang: Optional[str], script: str) -> Tuple[Optional[Route], str]:
        """Returns (route, status). status is one of:
        ok | no_lang | non_language_tag | quarantined | no_route
        """
        base = normalise_lang(lang)
        if not base:
            return None, "no_lang"
        if base in UNDETERMINED_TAGS:
            # An explicit "we do not know" is the same state as no tag at all,
            # and belongs in the same queue.
            return None, "no_lang"
        if base in NON_LANGUAGE_TAGS:
            return None, "non_language_tag"

        key = (base, script)
        if key in SCRIPT_FIRST:
            b, m = SCRIPT_FIRST[key]
            return Route(b, m, "script-first"), "ok"
        if key in NEURAL_ROUTES:
            b, m = NEURAL_ROUTES[key]
            return Route(b, m, "neural"), "ok"

        if base in self.quarantine and not self.allow_quarantined:
            return None, "quarantined"

        iso3, tag = self.to_iso3(base), SCRIPT_TAG.get(script)
        if iso3 and tag:
            mode = f"{iso3}-{tag}"
            if mode in self.modes:
                return Route(BACKEND_EPITRAN, mode, "installed-mode"), "ok"
        return None, "no_route"

    def summary(self) -> Dict[str, int]:
        return {
            "installed_modes": len(self.modes),
            "neural_routes": len(NEURAL_ROUTES),
            "script_first_routes": len(SCRIPT_FIRST),
            "quarantined_langs": len(self.quarantine),
            "allow_quarantined": int(self.allow_quarantined),
        }


def shard_token(value: str) -> str:
    """Filesystem-safe token for a lang/script value used in a shard id.

    The corpus's `lang` field is not a language code in 431 of its values: it
    holds '1510/', '1749:source', '1837-1893', ' Acland St', '20 Sukhumvit'.
    A '/' in a shard id becomes a directory separator and the Parquet write
    fails with FileNotFoundError partway through an array.

    Clean codes ('en', 'ca', 'zh-Hans') pass through UNCHANGED, so shard ids
    stay stable across a re-plan and already-computed shards remain valid. A
    value that needed rewriting gets a short hash of the original appended, so
    two different junk tags cannot collapse onto one id.
    """
    import hashlib
    import re as _re
    if value == "":
        return "NONE"
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", value)
    if safe == value:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:40]}~{digest}"


def partition_path_component(key: str, value: str) -> str:
    """Directory name DuckDB writes for PARTITION_BY, which percent-encodes.

    Reading a partition back by naively formatting f'lang={value}' silently
    misses every non-trivial value -- and a miss looks like an empty shard,
    not an error.
    """
    from urllib.parse import quote
    return f"{key}={quote(value, safe='')}"
