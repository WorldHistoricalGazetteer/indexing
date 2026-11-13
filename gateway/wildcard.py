"""Wildcard patterns for place-name search (``mode: "wildcard"`` on /api/search). Pure: no I/O.

Syntax follows FamilySearch's Read Places ``q`` (see ``familysearch.py``): ``*`` matches zero or more
characters, ``?`` exactly one, a backslash escapes either (``\\*`` is a literal asterisk). Matching is
case-insensitive and runs against the lower-cased exact-name keyword, so it covers every name form of a
place in every language and script.

What the gateway refuses, and why. A wildcard at the START of a pattern, or with almost no literal text
before it, makes Elasticsearch walk the whole term dictionary (tens of millions of names): ``*aris``,
``?aris`` and ``p*`` are all of that kind. Rather than time out slowly the request is refused with a
message saying how to write the pattern. The limits (``MIN_LITERAL``, ``MAX_WILDCARDS``) are measured, not
guessed: see ``tests`` and the commit message that set them.
"""
from __future__ import annotations

import re

MIN_LITERAL = 3        # literal characters required BEFORE the first wildcard (the expensive part)
MAX_WILDCARDS = 4      # unescaped * and ? together; more than this is never a name search
MAX_LENGTH = 100

_TOKEN = re.compile(r"\\(.)|([*?])|(.)", re.DOTALL)


def tokens(pattern: str) -> list[tuple[str, str]]:
    """('lit', char) for literal characters (escapes resolved), ('star', '*'), ('one', '?')."""
    out = []
    for m in _TOKEN.finditer(pattern or ""):
        if m.group(1) is not None:
            out.append(("lit", m.group(1)))
        elif m.group(2) is not None:
            out.append(("star" if m.group(2) == "*" else "one", m.group(2)))
        else:
            out.append(("lit", m.group(3)))
    return out


def has_wildcard(pattern: str) -> bool:
    """True when the pattern contains an UNESCAPED ``*`` or ``?``."""
    return any(k in ("star", "one") for k, _ in tokens(pattern))


def check(pattern: str) -> str | None:
    """None when the pattern may be searched, else a message that says how to fix it."""
    p = (pattern or "").strip()
    if len(p) > MAX_LENGTH:
        return f"The pattern is too long ({len(p)} characters; the limit is {MAX_LENGTH})."
    toks = tokens(p)
    wild = [i for i, (k, _) in enumerate(toks) if k != "lit"]
    if not wild:
        return None
    if len(wild) > MAX_WILDCARDS:
        return f"Too many wildcards ({len(wild)}); use at most {MAX_WILDCARDS} of * and ?."
    if wild[0] == 0:
        return ("A pattern cannot start with a wildcard, because that would scan every place name. "
                "Start with at least %d letters, for example par* rather than *aris." % MIN_LITERAL)
    if wild[0] < MIN_LITERAL:
        return (f"Give at least {MIN_LITERAL} letters before the first wildcard "
                f"(for example par*, not {p[:wild[0]]}*), so the search stays selective.")
    if all(k == "star" for k in (toks[i][0] for i in wild)) and all(k != "lit" for k, _ in toks):
        return "That pattern is only wildcards."
    return None


def literal_text(pattern: str) -> str:
    """The pattern's literal characters, lower-cased, wildcards removed."""
    return "".join(c for k, c in tokens(pattern) if k == "lit").casefold()


def to_regex(pattern: str) -> re.Pattern:
    """The pattern as an anchored, case-insensitive regular expression (for tests and local checks)."""
    parts = []
    for k, c in tokens(pattern):
        parts.append(".*" if k == "star" else "." if k == "one" else re.escape(c))
    return re.compile("^" + "".join(parts) + "$", re.IGNORECASE | re.DOTALL)


def score(pattern: str, name: str) -> float:
    """How closely ``name`` is what the pattern describes, 0 to 1: the share of the name the pattern's
    own letters account for. ``par*`` ranks ``Paris`` (0.6) above ``Parisian Gardens`` (0.2). ``?`` counts
    as a matched letter, so ``p?ris`` scores ``Paris`` 1.0. An exact-length match is the best possible."""
    n = len((name or "").strip())
    if n == 0:
        return 0.0
    fixed = sum(1 for k, _ in tokens(pattern) if k in ("lit", "one"))
    return min(1.0, fixed / n)


#: Multiplier when the pattern fits the place's own title rather than only one of its other names.
TITLE_FIT_BONUS = 2.0


def importance_factor(population) -> float:
    """1 + log10(1 + population): a town of 10,000 counts about 5x a place with no recorded population, a
    city of 2 million about 7.3x. Damped on purpose: a closer pattern fit must still be able to win."""
    import math
    try:
        pop = max(0.0, float(population or 0))
    except (TypeError, ValueError):
        pop = 0.0
    return 1.0 + math.log10(1.0 + pop)
