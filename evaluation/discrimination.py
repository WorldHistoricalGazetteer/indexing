"""Gate 2 — positives AND matched negatives, reported as AUC and AP, never a rate.

WHAT WENT WRONG BEFORE. The standing pairs test asserted that known matches
score high, and never asked what non-matches score. It reported "100% of pairs
clear 0.65" and that number is compatible with a model that returns 1.0 for
every input. A pass rate over positives alone cannot fail; the only question it
answers is whether the positives were positive.

THE SECOND TRAP, which is subtler and is what most of this module is about:
adding negatives is not enough if the negatives are EASY. Draw a negative
uniformly from the corpus and it is overwhelmingly a Latin name of a different
length in a different script from the query — so a model that has learned
nothing but "which script is this" separates the classes almost perfectly, and
AUC 0.97 means the encoder can tell Arabic from Thai. Negatives must therefore
be matched to their positive on

    script pair   — same (query script, candidate script) as the positive
    length band   — candidate length within the same bucket as the positive's

and `check_negative_matching` FAILS the run when they are not, rather than
reporting an AUC that is really a script classifier's score.

THE THIRD TRAP: coverage. A baseline that cannot score a pair (double-metaphone
on Arabic) must be excluded from that pair's ROC, not scored 0. Every result
here carries `n_scored of N` and the AUC is over the scored subset — which is
also why AUC alone is not enough to compare two scorers whose coverage differs,
and why coverage is printed on the same line, always.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

import numpy as np

#: Candidate-length buckets, in characters. Matching exactly on length would
#: make most positives unmatchable; these are wide enough to fill and narrow
#: enough that length alone is not a giveaway.
LENGTH_BANDS = ((0, 4), (5, 7), (8, 11), (12, 17), (18, 25), (26, 10_000))


def length_band(name: str) -> tuple[int, int]:
    n = len(name)
    for lo, hi in LENGTH_BANDS:
        if lo <= n <= hi:
            return (lo, hi)
    raise AssertionError("LENGTH_BANDS must cover every length")


@dataclass
class Pair:
    query: str
    query_lang: str
    query_script: str
    candidate: str
    candidate_lang: str
    candidate_script: str
    label: int                      # 1 positive, 0 negative
    stratum: str = ""               # e.g. "transliteration" / "exonym"

    @property
    def script_pair(self) -> tuple[str, str]:
        return (self.query_script, self.candidate_script)


@dataclass
class DiscriminationResult:
    scorer: str
    n_pairs: int
    n_scored: int
    n_positive: int
    n_negative: int
    auc: float | None
    average_precision: float | None
    positive_mean: float
    negative_mean: float
    by_script_pair: dict = field(default_factory=dict)
    by_stratum: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.n_scored / self.n_pairs if self.n_pairs else 0.0

    def line(self) -> str:
        auc = "n/a" if self.auc is None else f"{self.auc:.4f}"
        ap = "n/a" if self.average_precision is None else f"{self.average_precision:.4f}"
        return (f"{self.scorer:<28} AUC {auc:>7}  AP {ap:>7}  "
                f"pos_mean {self.positive_mean:.3f}  neg_mean {self.negative_mean:.3f}  "
                f"scored {self.n_scored:,} of {self.n_pairs:,} "
                f"({self.coverage:.1%}; {self.n_positive:,}+ / {self.n_negative:,}-)")

    def to_json(self) -> str:
        return json.dumps(asdict(self) | {"coverage": self.coverage}, indent=2)


class NegativesNotMatched(AssertionError):
    """The negative set does not mirror the positives, so the AUC would be a
    measurement of script/length detection rather than of phonetic matching."""


def check_negative_matching(pairs: list[Pair], *, tolerance: float = 0.05) -> None:
    """Fail unless negatives mirror positives on script pair and length band.

    `tolerance` is the largest allowed absolute difference in any cell's share
    of its class. It is deliberately strict: the whole value of matched
    negatives is that the two distributions are the same, and a "roughly
    similar" negative set reintroduces exactly the shortcut it exists to remove.
    """
    pos = [p for p in pairs if p.label == 1]
    neg = [p for p in pairs if p.label == 0]
    if not pos or not neg:
        raise NegativesNotMatched(
            f"need both classes; got {len(pos)} positive and {len(neg)} negative. "
            f"A single-class set has no AUC — this is the failure the old pairs "
            f"test hid by only ever holding positives.")

    problems = []
    for name, key in (("script pair", lambda p: p.script_pair),
                      ("length band", lambda p: length_band(p.candidate))):
        cp = Counter(key(p) for p in pos)
        cn = Counter(key(p) for p in neg)
        for cell in set(cp) | set(cn):
            sp, sn = cp[cell] / len(pos), cn[cell] / len(neg)
            if abs(sp - sn) > tolerance:
                problems.append(
                    f"{name} {cell}: {sp:.1%} of positives vs {sn:.1%} of "
                    f"negatives (n={cp[cell]}/{cn[cell]})")
    if problems:
        raise NegativesNotMatched(
            "negatives are not matched to positives, so AUC would measure "
            "script/length detection and not phonetic matching:\n  "
            + "\n  ".join(sorted(problems)))


def _auc_ap(scores: np.ndarray, labels: np.ndarray) -> tuple[float | None, float | None]:
    """AUC and average precision, or (None, None) when a class is missing.

    Returning None rather than 0.5 matters: 0.5 is a legitimate value meaning
    'no better than chance', and a degenerate set that silently reports it is
    indistinguishable from a genuinely useless model.
    """
    if labels.min() == labels.max():
        return None, None
    from sklearn.metrics import average_precision_score, roc_auc_score
    return (float(roc_auc_score(labels, scores)),
            float(average_precision_score(labels, scores)))


def evaluate_pairs(pairs: list[Pair], scorer, name: str,
                   *, require_matched: bool = True) -> DiscriminationResult:
    """Score every pair and report AUC/AP overall, per script pair, per stratum.

    `scorer(pair) -> baselines.Scored`-shaped object (``.score``, ``.covered``)
    or a plain float. Uncovered pairs are EXCLUDED, not zeroed.
    """
    if require_matched:
        check_negative_matching(pairs)

    notes = []
    scores, labels, kept = [], [], []
    for p in pairs:
        s = scorer(p)
        if hasattr(s, "covered"):
            if not s.covered:
                continue
            v = float(s.score)
        else:
            v = float(s)
        scores.append(v)
        labels.append(p.label)
        kept.append(p)
    S, L = np.asarray(scores), np.asarray(labels)
    if len(S) == 0:
        return DiscriminationResult(
            scorer=name, n_pairs=len(pairs), n_scored=0, n_positive=0,
            n_negative=0, auc=None, average_precision=None,
            positive_mean=float("nan"), negative_mean=float("nan"),
            notes=["scorer covered no pair at all"])

    auc, ap = _auc_ap(S, L)
    if auc is None:
        notes.append("one class vanished after dropping uncovered pairs — AUC "
                     "is undefined and is reported as n/a, not as 0.5")

    def _cell(sub_idx):
        s, l = S[sub_idx], L[sub_idx]
        a, p_ = _auc_ap(s, l)
        return {"n": int(len(s)), "n_positive": int(l.sum()),
                "n_negative": int((1 - l).sum()), "auc": a, "average_precision": p_,
                "positive_mean": float(s[l == 1].mean()) if l.sum() else None,
                "negative_mean": float(s[l == 0].mean()) if (1 - l).sum() else None}

    by_sp, by_st = defaultdict(list), defaultdict(list)
    for i, p in enumerate(kept):
        by_sp["→".join(p.script_pair)].append(i)
        if p.stratum:
            by_st[p.stratum].append(i)

    return DiscriminationResult(
        scorer=name, n_pairs=len(pairs), n_scored=len(S),
        n_positive=int(L.sum()), n_negative=int((1 - L).sum()),
        auc=auc, average_precision=ap,
        positive_mean=float(S[L == 1].mean()) if L.sum() else float("nan"),
        negative_mean=float(S[L == 0].mean()) if (1 - L).sum() else float("nan"),
        by_script_pair={k: _cell(np.asarray(v)) for k, v in sorted(by_sp.items())},
        by_stratum={k: _cell(np.asarray(v)) for k, v in sorted(by_st.items())},
        notes=notes)
