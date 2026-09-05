"""Baselines Symphonym has to beat, and the honest statement of what each can do.

A baseline that cannot in principle score a cross-script pair is not a weak
competitor, it is a *broken* one, and reporting its number beside Symphonym's
without saying so flatters Symphonym for free. So every scorer here declares its
coverage — the fraction of pairs it could score at all — and the report must
carry it. "Levenshtein 0.31" over a set where 98% of pairs share no character is
a statement about the alphabet, not about edit distance.

    levenshtein_raw          Cross-script this is ~0 BY CONSTRUCTION: no shared
                             characters, so the normalised similarity is near
                             zero for a correct pair and a wrong one alike. It
                             is here as a FLOOR, to make visible how much of any
                             baseline's score comes from romanisation rather
                             than from the algorithm.
    levenshtein_romanised    The real competitor. Both sides pushed through
                             `anyascii` first. This is what "v7 beats plain
                             Levenshtein by 3.7pp" has to mean, since raw edit
                             distance cannot rank Arabic against Hebrew at all.
    jaro_winkler_romanised   Same, with a prefix bonus — stronger on names that
                             agree at the head and diverge at the tail, which is
                             most transliteration disagreement.
    double_metaphone         LATIN-ONLY BY CONSTRUCTION. `doublemetaphone` of a
                             non-Latin string returns ('', ''), and two empty
                             codes compare EQUAL — a silent 1.0 on every
                             cross-script pair, which would look like a perfect
                             baseline. Guarded: an empty code scores None
                             (uncovered), never 1.0. On romanised input it has
                             coverage but inherits every romanisation error.

⚠ `anyascii` gives MANDARIN readings for Han characters, so a Japanese
place-name romanises to its Chinese reading and the romanised baselines are
wrong on ja-CJK in a way that is not the baseline's fault. Counted separately
rather than averaged in.
"""
from __future__ import annotations

from dataclasses import dataclass

from anyascii import anyascii
from metaphone import doublemetaphone
from rapidfuzz.distance import JaroWinkler, Levenshtein


@dataclass
class Scored:
    """A score plus whether the scorer could actually see the pair.

    `covered=False` is NOT `score=0.0`. Collapsing the two is how an
    inapplicable metric becomes a confident wrong answer.
    """
    score: float | None
    covered: bool

    @property
    def value(self) -> float:
        return self.score if self.covered and self.score is not None else 0.0


def _rom(s: str) -> str:
    return anyascii(s).strip().lower()


def levenshtein_raw(a: str, b: str) -> Scored:
    return Scored(Levenshtein.normalized_similarity(a, b), True)


def levenshtein_romanised(a: str, b: str) -> Scored:
    ra, rb = _rom(a), _rom(b)
    if not ra or not rb:
        return Scored(None, False)      # anyascii dropped the string entirely
    return Scored(Levenshtein.normalized_similarity(ra, rb), True)


def jaro_winkler_romanised(a: str, b: str) -> Scored:
    ra, rb = _rom(a), _rom(b)
    if not ra or not rb:
        return Scored(None, False)
    return Scored(JaroWinkler.similarity(ra, rb), True)


def double_metaphone(a: str, b: str, *, romanise: bool = False) -> Scored:
    """Agreement between double-metaphone codes, or *uncovered* if either is empty.

    The guard is the whole point. `doublemetaphone('لندن')` is `('', '')`, and
    `'' == ''`, so an unguarded implementation reports a perfect match for every
    pair of non-Latin names — a baseline that beats everything by scoring
    nothing.
    """
    sa, sb = (_rom(a), _rom(b)) if romanise else (a, b)
    ca, cb = doublemetaphone(sa), doublemetaphone(sb)
    codes_a = [c for c in ca if c]
    codes_b = [c for c in cb if c]
    if not codes_a or not codes_b:
        return Scored(None, False)
    if set(codes_a) & set(codes_b):
        return Scored(1.0, True)
    # Not a binary metric only: a graded fallback lets the ROC curve have more
    # than two points, without which AUC is barely meaningful.
    best = max(Levenshtein.normalized_similarity(x, y)
               for x in codes_a for y in codes_b)
    return Scored(best, True)


def double_metaphone_romanised(a: str, b: str) -> Scored:
    return double_metaphone(a, b, romanise=True)


#: Name → scorer. The order is the order they should be reported in: floor
#: first, so a reader sees what the alphabet contributes before what the
#: algorithm does.
BASELINES = {
    "levenshtein_raw": levenshtein_raw,
    "levenshtein_romanised": levenshtein_romanised,
    "jaro_winkler_romanised": jaro_winkler_romanised,
    "double_metaphone": double_metaphone,
    "double_metaphone_romanised": double_metaphone_romanised,
}
