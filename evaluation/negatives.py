"""Matched negatives, and the two ways a negative set silently ruins a benchmark.

FAILURE 1 — THE EASY NEGATIVE. Draw a negative uniformly from the corpus and it
is almost always a Latin name of a different length in a different script from
the query. A model that has learned only "which script is this" then separates
the classes at AUC ~0.99, and nothing in the output says the number is about
script detection. `discrimination.check_negative_matching` refuses such a set;
this module is what builds one that passes it, by matching every negative to its
positive on **candidate script** and **candidate length band**.

FAILURE 2 — THE NEGATIVE THAT IS ACTUALLY A MATCH. A name drawn at random can be
a genuine name of the query's place under a different `place_id`: the corpus has
51.2M places across 27 authorities and co-reference between them is the whole
reason the hard-link overlay exists. Such a pair is scored as a false positive
when the model gets it RIGHT, so the model is penalised for being correct and
the error is invisible — the AUC is simply lower than it should be, which reads
as a weaker model rather than as a broken test set.

The exclusion is therefore over the query place's whole co-reference closure,
not just the place itself:

    forbidden(P) = names(P) ∪ names(Q) for every Q linked to P by
                   sameAs / exactMatch / closeMatch in hard_links.sqlite

`distinct` assertions are deliberately NOT followed: a `distinct` edge says two
places are NOT the same, so its endpoint is a legitimate negative — and an
unusually good one, since somebody thought the two were confusable enough to be
worth denying.

⚠ The closure is one hop. Two places linked only through a third are not
excluded. The count of one-hop exclusions is reported so the residual is
visible; deepening it is a decision to take with a measurement, not a default.
"""
from __future__ import annotations

import random
import sqlite3
from collections import Counter, defaultdict

from evaluation.discrimination import Pair, length_band

#: Relations meaning "the same place". `distinct` is excluded on purpose — see
#: the module docstring.
SAME_PLACE_RELATIONS = ("sameAs", "exactMatch", "closeMatch")


class HardLinks:
    """One-hop co-reference lookup over the read-only hard-link overlay."""

    def __init__(self, db_path: str):
        # Read-only URI: this overlay is rebuilt by a Slurm job and is live for
        # the gateway; a benchmark must not be able to take a write lock on it.
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = None

    def neighbours(self, place_id: str) -> set[str]:
        q = ("SELECT place_b FROM hard_link_assertions "
             "WHERE place_a = ? AND relation_type IN (?, ?, ?) "
             "UNION SELECT place_a FROM hard_link_assertions "
             "WHERE place_b = ? AND relation_type IN (?, ?, ?)")
        args = (place_id, *SAME_PLACE_RELATIONS, place_id, *SAME_PLACE_RELATIONS)
        return {r[0] for r in self._conn.execute(q, args)}


class HaystackIndex:
    """The haystack, bucketed by (script, length band) so a matched draw is O(1).

    Built once. A linear scan per negative would be 100k x 1M comparisons and
    would push the whole build into the hours where people start sampling less.
    """

    def __init__(self, docs: list[dict]):
        self._buckets: dict[tuple, list[dict]] = defaultdict(list)
        for d in docs:
            name = d.get("name")
            if not name:
                continue
            self._buckets[(d.get("script", "OTHER"), length_band(name))].append(d)

    def bucket_size(self, script: str, name: str) -> int:
        return len(self._buckets.get((script, length_band(name)), ()))

    def draw(self, script: str, like_name: str, forbidden: set[str],
             rng: random.Random, *, tries: int = 40) -> dict | None:
        """A candidate of the same script and length band, not in `forbidden`.

        Returns None rather than relaxing the match. Relaxing is how an easy
        negative gets in: a fallback to "any script" would quietly reintroduce
        precisely the shortcut `check_negative_matching` exists to detect, and
        it would do so only for the rarest script pairs — the ones the whole
        benchmark is for.
        """
        pool = self._buckets.get((script, length_band(like_name)))
        if not pool:
            return None
        for _ in range(tries):
            cand = pool[rng.randrange(len(pool))]
            if cand["name"] not in forbidden:
                return cand
        return None


class ExclusionImpossible(AssertionError):
    """A positive carries no place, so no co-reference exclusion can be computed.

    Raised rather than proceeding with an empty forbidden set. An external
    positive pack (LHPN historic forms, a transcription set) has no `place_id`,
    and `forbidden.get(place_id, set())` would return empty and read exactly
    like "this place has no co-referents" — the absent input treated as
    nothing-to-do. The caller must supply an exclusion for those pairs, or pass
    `allow_unanchored=True` to state deliberately that they are going in
    without one, in which case the count is reported.
    """


def build_negatives(positives, haystack: HaystackIndex, forbidden_for,
                    rng: random.Random, *,
                    allow_unanchored: bool = False) -> tuple[list[Pair], dict]:
    """One matched negative per positive, plus a census of what could not be matched.

    `forbidden_for(positive) -> set[str]` supplies the co-reference closure's
    names for that positive's place.

    The census is not decoration. A script pair whose negatives could not be
    drawn is a script pair with no AUC, and it must be visible as such rather
    than appearing as a smaller sample of the same measurement.
    """
    unanchored = [p for p in positives if not getattr(p, "has_place", True)]
    if unanchored and not allow_unanchored:
        raise ExclusionImpossible(
            f"{len(unanchored):,} of {len(positives):,} positives carry no "
            f"place_id, so the co-reference exclusion cannot be computed for "
            f"them and every negative drawn against them would be unfiltered. "
            f"First example: {unanchored[0].query!r} ~ {unanchored[0].partner!r} "
            f"(source {unanchored[0].source!r}). Supply an exclusion for these "
            f"pairs, or pass allow_unanchored=True to accept it deliberately — "
            f"the census then reports how many went in without one.")

    pairs: list[Pair] = []
    unmatched: dict[str, int] = defaultdict(int)
    matched: dict[str, int] = defaultdict(int)
    for pos in positives:
        key = "→".join(pos.script_pair)
        pairs.append(Pair(query=pos.query, query_lang=pos.query_lang,
                          query_script=pos.query_script,
                          candidate=pos.partner, candidate_lang=pos.partner_lang,
                          candidate_script=pos.partner_script, label=1))
        cand = haystack.draw(pos.partner_script, pos.partner,
                             forbidden_for(pos), rng)
        if cand is None:
            unmatched[key] += 1
            continue
        matched[key] += 1
        pairs.append(Pair(query=pos.query, query_lang=pos.query_lang,
                          query_script=pos.query_script,
                          candidate=cand["name"],
                          candidate_lang=cand.get("lang", "und"),
                          candidate_script=pos.partner_script, label=0))
    census = {
        "positives": len(positives),
        # Not a footnote: these are the pairs whose negatives were drawn with NO
        # co-reference exclusion, so a "negative" among them may be a genuine
        # name of the query's place and the model is penalised for being right.
        #
        # Broken down by source, because this population stopped being
        # homogeneous. It used to mean one thing — "no place_id" — and under the
        # GOTW ingest spec it will hold at least three: a row that never had an
        # anchor, a row whose anchor was REJECTED by a specialist and whose
        # typed correction could not be re-resolved with corroboration, and a
        # row that re-resolved ambiguously. The middle group are corrections,
        # i.e. the HIGHER-quality answers, so a single scalar would average the
        # best rows in the pack together with the ones nobody could place. One
        # count is exactly the "two populations readable as one" failure this
        # census exists to prevent.
        "unanchored_no_exclusion": len(unanchored),
        "unanchored_by_source": dict(
            sorted(Counter(p.source for p in unanchored).items())),
        "negatives": sum(matched.values()),
        "unmatched_positives": sum(unmatched.values()),
        "matched_by_script_pair": dict(sorted(matched.items())),
        "unmatched_by_script_pair": dict(sorted(unmatched.items())),
    }
    return pairs, census


def drop_unpaired(pairs: list[Pair]) -> tuple[list[Pair], int]:
    """Remove positives whose negative could not be drawn.

    Leaving them in would make the class balance vary by script pair, and a
    per-cell AUC computed over a cell whose positives outnumber its negatives
    3:1 is not comparable with one that is balanced. Returns the count removed
    so the denominator change is stated, not absorbed.
    """
    by_query: dict[tuple, list[Pair]] = defaultdict(list)
    for p in pairs:
        by_query[(p.query, p.query_script, p.candidate_script)].append(p)
    kept, dropped = [], 0
    for group in by_query.values():
        if any(p.label == 1 for p in group) and any(p.label == 0 for p in group):
            kept.extend(group)
        else:
            dropped += len(group)
    return kept, dropped
