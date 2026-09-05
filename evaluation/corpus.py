"""Build the retrieval corpus: a proportional haystack and query-balanced positives.

THE SHAPE, and the reason for it. The haystack is sampled **proportional to the
live script distribution** and the queries are balanced across script pairs.
Balancing the haystack instead would make recall@k a number about a corpus that
does not exist — distractor density is what makes retrieval hard, and it has to
mirror production or the figure is not the operational one. Balancing the
queries costs nothing and is the only way a script pair holding 0.03% of the
corpus gets measured at all.

    haystack   1,000,000 toponyms, per-script proportional to the exact
               `terms` agg over the live index (below), sampled by seeded
               `random_score`
    injected   the specific positive partners that did not land in the
               proportional draw. Declared and COUNTED SEPARATELY —
               "1,000,000 + N injected" — never folded into the headline,
               because an injected partner is a document the sample did not
               choose and the reader is entitled to know how many there were.
    queries    balanced across script pairs, over-sampling minority pairs

POSITIVES COME FROM SHARED `place_id` AND ARE NOT STRATIFIED. Every automatic
labeller available is contaminated: Epitran → IPA → PanPhon is *v7's own
teacher*, so selecting "transliteration-like" pairs by Epitran distance selects
for what v7 already encodes and penalises a v8 that moves away from PanPhon;
CharsiuG2P is the same front end for zh/ko/yue/gan/wuu; edit distance on
romanised forms would hand the Levenshtein baseline labels it generated itself.
So the headline is unstratified per-script-pair recall over all shared-place
cross-script pairs. Exonyms are included: they are the harder case, so including
them makes the number conservative, which is the right direction for a benchmark
whose job is to be able to say v7 is adequate.

`lang_variant` IS NOT USED AS A LABELLER EITHER — it is kept as a VALIDATION
set. A name tagged `-Latn` beside a same-place name in its native script is a
transliteration by the source's own assertion, independent of Symphonym, of
Epitran and of edit distance. There are only ~50k of them, far too few to label
a 1M benchmark, but exactly enough to measure how far any proposed labeller (or
a hand-labelled sample) agrees with an uncontaminated signal before anyone
trusts one at scale.

⚠ WHAT COULD STILL MAKE THIS CORPUS A MEASUREMENT OF SOMETHING ELSE. Wikidata
carries machine-generated multilingual labels, so a single `wd` place can emit
dozens of cross-script "positives" that are all transliterations of one string.
If `wd` dominates, the benchmark measures Wikidata label transliteration. The
namespace mix of the positive set is therefore reported, and positives are
capped per place — a cap, not a filter, so the count that was capped is visible.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

#: Exact per-script counts of the live `toponyms` index, measured 5 Sep 2026 by
#: a `terms` agg with `sum_other_doc_count == 0` whose buckets sum to
#: 72,703,777 — the index total — so the distribution is complete, not a sample.
#: Re-measure rather than trust this after any re-ingest; `script_distribution`
#: below does that, and `SCRIPT_COUNTS` is only the recorded value.
SCRIPT_COUNTS = {
    "LATIN": 60_145_513, "CYRILLIC": 4_234_862, "CJK": 3_240_684,
    "ARABIC": 2_251_220, "HANGUL": 416_894, "OTHER": 395_409,
    "KATAKANA": 358_111, "THAI": 261_989, "GREEK": 245_549,
    "DEVANAGARI": 184_963, "ARMENIAN": 165_304, "HEBREW": 162_324,
    "HIRAGANA": 153_929, "BENGALI": 125_118, "GEORGIAN": 112_180,
    "MALAYALAM": 73_902, "TAMIL": 54_908, "TELUGU": 53_434,
    "KANNADA": 45_743, "GUJARATI": 21_741,
}
TOPONYM_TOTAL = 72_703_777

#: Scripts a Latin-alphabet romanisation cannot be checked against by eye, and
#: which the whole exercise is for. Not used to filter — used to report.
NON_LATIN = frozenset(SCRIPT_COUNTS) - {"LATIN", "OTHER"}


def script_distribution(es, index: str = "toponyms") -> dict:
    """Re-measure the distribution, and REFUSE it if it is not complete.

    A `terms` agg silently truncates: `sum_other_doc_count > 0` means buckets
    were dropped, and a proportional sample built on truncated buckets
    over-represents whatever survived. Checking that the buckets sum to the
    index total is the cheap way to know the frame is the whole frame.
    """
    res = es.search(index=index, size=0, track_total_hits=True,
                    aggs={"s": {"terms": {"field": "script", "size": 100}}})
    total = res["hits"]["total"]["value"]
    agg = res["aggregations"]["s"]
    counts = {b["key"]: b["doc_count"] for b in agg["buckets"]}
    if agg["sum_other_doc_count"]:
        raise SystemExit(
            f"ABORT: script agg dropped {agg['sum_other_doc_count']:,} documents "
            f"into sum_other_doc_count — the buckets are not the whole frame and "
            f"a proportional sample built on them would be biased toward "
            f"whichever scripts survived. Raise `size`.")
    if sum(counts.values()) != total:
        raise SystemExit(
            f"ABORT: script buckets sum to {sum(counts.values()):,} against an "
            f"index total of {total:,}. Some documents carry no `script` and "
            f"would be unreachable by per-script sampling.")
    return counts


def proportional_quota(counts: dict, n: int, *, floor: int = 2_000) -> dict:
    """How many to draw per script, proportional but with a floor.

    A pure proportional draw of 1M gives GUJARATI 299 documents — enough to be a
    distractor set and not enough for any per-script-pair figure to have a
    denominator worth printing. The floor lifts the smallest scripts and the
    excess is taken back from the largest, so the total is exactly `n` and the
    departure from proportionality is confined to scripts that were negligible
    anyway. The quota is returned in full so the report can state it rather than
    describe the sample as 'proportional' and leave the floor implicit.
    """
    total = sum(counts.values())
    raw = {k: n * v / total for k, v in counts.items()}
    quota = {k: max(floor, int(round(v))) for k, v in raw.items()}
    # Loop until the total is EXACTLY n. A single proportional pass leaves a
    # rounding remainder (1,029 documents at n=1e6), and a quota that does not
    # sum to its own stated size is the kind of small discrepancy that later
    # gets explained as sampling noise.
    while True:
        overshoot = sum(quota.values()) - n
        if overshoot == 0:
            break
        big = [k for k in sorted(quota, key=lambda k: -quota[k]) if quota[k] > floor]
        if not big:
            raise ValueError(f"n={n} is too small for floor={floor} across "
                             f"{len(quota)} scripts")
        if overshoot > 0:
            pool = sum(quota[k] for k in big)
            for k in big:
                if overshoot <= 0:
                    break
                take = min(quota[k] - floor, overshoot,
                           max(1, int(overshoot * quota[k] / pool)))
                quota[k] -= take
                overshoot -= take
        else:
            quota[big[0]] -= overshoot          # overshoot < 0: give it back
    return quota


def sample_script(es, script: str, n: int, seed: int, index: str = "toponyms",
                  page: int = 5_000, throttle: float = 0.0) -> list[dict]:
    """Draw `n` random toponyms of one script, deterministically for a seed.

    `random_score` with an explicit seed AND field is reproducible, and paging
    with `search_after` over `[_score, _id]` is stable because the score is a
    pure function of the seed and the document. A plain `from`/`size` walk over
    a random ordering is not — it re-randomises per request.
    """
    out, after = [], None
    while len(out) < n:
        body = {
            "size": min(page, n - len(out)),
            "track_total_hits": False,
            "_source": ["toponym_id", "name", "lang", "lang_variant", "script"],
            "query": {"function_score": {
                "query": {"bool": {"filter": [{"term": {"script": script}}]}},
                "random_score": {"seed": seed, "field": "_seq_no"},
                "boost_mode": "replace"}},
            "sort": [{"_score": "desc"}, {"_doc": "asc"}],
        }
        if after is not None:
            body["search_after"] = after
        res = es.search(index=index, body=body)
        hits = res["hits"]["hits"]
        if not hits:
            break                      # the script has fewer docs than asked
        for h in hits:
            out.append(h["_source"])
        after = hits[-1]["sort"]
        if throttle:
            time.sleep(throttle)
    return out


# ---------------------------------------------------------------------------
# Positives: cross-script names of the same place
# ---------------------------------------------------------------------------

def _split_toponym_id(tid: str) -> tuple[str, str]:
    """``"London@en"`` → ``("London", "en")``.

    Split on the LAST ``@``: a toponym id is ``{name}@{lang}`` and a name may
    itself contain ``@``. Splitting on the first would silently truncate those
    names and, worse, produce a plausible-looking shorter name rather than an
    error.
    """
    name, sep, lang = tid.rpartition("@")
    return (name, lang) if sep else (tid, "und")


@dataclass(frozen=True)
class Positive:
    place_id: str
    namespace: str
    query: str
    query_lang: str
    query_script: str
    partner: str
    partner_lang: str
    partner_script: str

    @property
    def script_pair(self) -> tuple[str, str]:
        return (self.query_script, self.partner_script)


def cross_script_pairs(place: dict, *, max_per_place: int,
                       rng: random.Random) -> tuple[list[Positive], int]:
    """Every cross-script name pair of one place, capped, plus how many were cut.

    Returns ``(pairs, n_dropped)``. The drop count is returned rather than
    discarded because the cap is the one place this construction can silently
    change what is being measured: Wikidata emits dozens of machine-generated
    labels per place, so an uncapped set is dominated by a handful of places and
    a capped set with no drop count looks identical to a naturally small one.
    """
    from phonetics.tokenise import detect_script

    place_id = place.get("place_id") or ""
    namespace = place_id.split(":", 1)[0]
    seen, entries = set(), []
    for t in place.get("toponyms") or []:
        tid = t.get("toponym_id")
        if not tid:
            continue
        name, lang = _split_toponym_id(tid)
        name = name.strip()
        # Deduplicate on the NAME, not the id: `wd` carries the identical Latin
        # string under twenty language tags, and counting those as twenty
        # distinct names would make one label look like a rich multilingual
        # place and would put the same string on both sides of a "pair".
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append((name, lang, detect_script(name)))

    pairs = [Positive(place_id, namespace, a, al, asc, b, bl, bsc)
             for i, (a, al, asc) in enumerate(entries)
             for (b, bl, bsc) in entries[i + 1:]
             if asc != bsc]
    if len(pairs) <= max_per_place:
        return pairs, 0
    dropped = len(pairs) - max_per_place
    return rng.sample(pairs, max_per_place), dropped


def summarise_positives(pairs: list[Positive]) -> dict:
    """The mix, because the mix is how this corpus stops measuring what it means to.

    If one namespace supplies most of the positives then the headline is a
    statement about that authority's label conventions. Reported, never averaged
    away.
    """
    ns = Counter(p.namespace for p in pairs)
    sp = Counter("→".join(sorted(p.script_pair)) for p in pairs)
    places = len({p.place_id for p in pairs})
    top_ns, top_n = (ns.most_common(1) or [("", 0)])[0]
    return {
        "n_pairs": len(pairs),
        "n_places": places,
        "pairs_per_place": round(len(pairs) / places, 2) if places else 0.0,
        "by_namespace": dict(ns.most_common()),
        "by_script_pair": dict(sp.most_common()),
        "dominant_namespace": top_ns,
        "dominant_namespace_share": round(top_n / len(pairs), 4) if pairs else 0.0,
        "n_cross_script_pairs_involving_latin": sum(
            v for k, v in sp.items() if "LATIN" in k.split("→")),
    }
