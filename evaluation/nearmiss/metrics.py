"""Pure scoring for the near-miss evaluation. No I/O, no Elasticsearch.

One *item* is a query with a known answer. One *result* is what the pipeline returned for it under one
condition (unscoped, or scoped to the item's country): ``[(place_id, confidence), ...]`` best first.

Reported per stratum, never pooled across strata that mean different things:
  hit@k       the right place is in the top k. "Right" has THREE readings, all reported:
              ``cc`` (the headline) a place bearing the original name IN THE SAMPLED PLACE'S COUNTRY;
              ``any``      any place that bears the original name exactly (a name shared by many places
                           is not a miss because we returned a namesake);
              ``specific`` the exact sampled place (the strict reading; meaningful mainly when scoped).
  mrr         mean reciprocal rank of the first ``any`` hit.
  precision@t among items whose top answer has confidence >= t, the share whose top answer is right,
              with ``coverage@t`` = the share of ALL items that reach t. These two travel together: a
              precision without its coverage is the number that changes its subject (see
              a-number-that-changed-its-subject). This is the data an ACCEPT RULE is calibrated on.
  false_accept@t   for NONSENSE items (no right answer exists): the share whose top confidence >= t.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Sequence

# Confidence measures NAME MATCH, and a mis-spelt query rarely scores above ~40 (measured 2026-11-13: almost nothing
# reaches 50), so the informative thresholds are low. Report fine steps; pick the five to publish from the data.
THRESHOLDS = (10, 20, 30, 40, 50, 70, 85)
KS = (1, 3, 10)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials (never reported without n)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def rank_of(result: Sequence[tuple[str, float]], wanted: Iterable[str]) -> int | None:
    """0-based rank of the first result whose place_id is in ``wanted``; None if absent."""
    w = set(wanted)
    for i, (pid, _conf) in enumerate(result):
        if pid in w:
            return i
    return None


def score_item(item: dict, result: Sequence[tuple[str, float]]) -> dict:
    """Per-item outcome used by ``aggregate``."""
    top_conf = result[0][1] if result else 0.0
    if item.get("kind") == "nonsense":
        return {"nonsense": True, "top_conf": top_conf, "answered": bool(result)}
    r_cc = rank_of(result, item.get("truth_cc") or item["truth_pids"])
    r_any = rank_of(result, item["truth_pids"])
    r_spec = rank_of(result, [item["place_id"]])
    # HEADLINE is the right name in the right country (``cc``): a namesake in another country is not the
    # answer a family historian wants. ``any`` (a namesake anywhere) is kept because it is the looser
    # reading the earlier harness used, so figures can be compared with it.
    return {"nonsense": False, "rank_cc": r_cc, "rank_any": r_any, "rank_specific": r_spec, "top_conf": top_conf,
            "top1_right": r_cc == 0, "answered": bool(result)}


def _rate(vals: list[bool]) -> dict:
    n = len(vals)
    k = sum(vals)
    lo, hi = wilson(k, n)
    return {"n": n, "k": k, "rate": round(k / n, 4) if n else None, "ci95": [round(lo, 4), round(hi, 4)]}


def aggregate(outcomes: list[dict]) -> dict:
    """Metrics for one stratum from per-item outcomes (misspellings and nonsense are separated)."""
    real = [o for o in outcomes if not o["nonsense"]]
    junk = [o for o in outcomes if o["nonsense"]]
    out: dict = {"n": len(real)}
    for k in KS:
        out[f"hit@{k}"] = _rate([o["rank_cc"] is not None and o["rank_cc"] < k for o in real])
    out["any_namesake@10"] = _rate([o["rank_any"] is not None and o["rank_any"] < 10 for o in real])
    out["specific@10"] = _rate([o["rank_specific"] is not None and o["rank_specific"] < 10 for o in real])
    out["mrr"] = round(sum(1 / (o["rank_cc"] + 1) for o in real if o["rank_cc"] is not None) / len(real), 4) if real else None
    out["no_answer"] = _rate([not o["answered"] for o in real])
    prec = {}
    for t in THRESHOLDS:
        reach = [o for o in real if o["top_conf"] >= t]
        prec[str(t)] = {"coverage": _rate([o["top_conf"] >= t for o in real]),
                        "precision": _rate([o["top1_right"] for o in reach])}
    out["accept"] = prec
    if junk:
        out["false_accept"] = {str(t): _rate([o["top_conf"] >= t for o in junk]) for t in THRESHOLDS}
    return out


def stratify(items: list[dict], outcomes: list[dict], keys: dict[str, callable]) -> dict:
    """``keys`` maps a stratum-family name to a function item -> label (or list of labels).
    Returns {family: {label: aggregate}} plus {"ALL": aggregate}. Labelled families overlap by design
    (an item has one length bucket AND several error classes); they are never summed."""
    res: dict = {"ALL": aggregate(outcomes)}
    for fam, fn in keys.items():
        buckets: dict[str, list[dict]] = defaultdict(list)
        for it, oc in zip(items, outcomes):
            labels = fn(it)
            for lab in (labels if isinstance(labels, (list, tuple, set)) else [labels]):
                buckets[str(lab)].append(oc)
        res[fam] = {lab: aggregate(v) for lab, v in sorted(buckets.items())}
    return res


def length_bucket(name: str) -> str:
    n = len(name)
    return "<=5" if n <= 5 else "6-8" if n <= 8 else "9-12" if n <= 12 else "13+"


STANDARD_KEYS = {
    "length": lambda it: length_bucket(it["original"]),
    "error_class": lambda it: it.get("classes") or ["nonsense"],
    "edit_distance": lambda it: str(min(it.get("edit_distance", 0), 4)) if it.get("kind") != "nonsense" else "n/a",
    "ambiguity": lambda it: ("unique" if len(it.get("truth_pids", [])) <= 1 else "shared<=5" if len(it["truth_pids"]) <= 5 else "shared>5"),
    "country": lambda it: it.get("ccode") or "-",
    "words": lambda it: "multi" if " " in it["original"].strip() else "single",
}

STANDARD_KEYS["first_two_letters"] = lambda it: (
    "n/a" if it.get("kind") == "nonsense"
    else "unchanged" if it["query"][:2].casefold() == it["original"][:2].casefold() else "changed")
