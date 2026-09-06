#!/usr/bin/env python3
"""
The per-stratum v7 discrimination baseline, measured before the v8 retrain.

WHY A CORPUS AVERAGE CANNOT SERVE AS THE BASELINE
-------------------------------------------------
What v8 is now mainly for is +15.9M IPA strings concentrated in ~20 languages
that had NO phonetic routing at all under v7. Against 72.7M toponyms that is
invisible in an average: a large gain in a small stratum and no change anywhere
else produce the same corpus number as no gain at all. Without a per-stratum v7
figure there is nothing to compare v8 to, and the acceptance gate silently
degrades to the corpus average it was written to replace.

THREE STRATA, REPORTED SEPARATELY AND NEVER SUMMED
--------------------------------------------------
  GAIN            languages that gain IPA at v8 -- 20 with ZERO at v7, plus the
                  improved set (en, ja, zh)
  CONTAMINATION   `sv` alone, watched for DEGRADATION rather than improvement:
                  v8 trains on ~109k more Swedish, 94.4% of it Wikidata labels
                  whose language tag records a wiki edition rather than the name
  UNCHANGED       everything else, and the only stratum where "no regression"
                  was ever measuring what it claimed

🛑 THE GAIN COLUMN IS A FLOOR, NOT A BASELINE, AND IS LABELLED SO IN THE TABLE.
A v7 AUC over pairs whose languages had zero IPA at v7 training measures a model
on inputs it was never given a signal for. That is not "v7's discrimination on
this stratum" in the sense the UNCHANGED figure is. Under one caption the two
read as commensurable however carefully the prose hedges, so the label goes in
the table.

⚠ Quarantined languages (ceb/war/min/vo/mul) are EXCLUDED from the gate
entirely: v7 had zero IPA for all five, so they can neither gain nor regress,
and letting them fall into another stratum would attribute their behaviour to
whichever language sat opposite them.

⚠ Latin-involving pairs are split from non-Latin<->non-Latin and never
averaged: 35% of CJK<->Latin positives romanise to identical strings, which
makes edit distance a near-oracle there and has already produced one misleading
result in this project.

⚠ A stratum with too few pairs reports its N and says so, rather than
publishing an AUC. Measured on this corpus, 8 of the 20 v7-zero languages hold
under 400 pairs (gl: 138), so per-LANGUAGE figures are not reportable for them
even where the aggregate stratum is.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Dict, List

import numpy as np

# The 20 languages with zero v7 IPA that v8 gains, from the 2,438-stratum table.
GAIN_ZERO = {"ga", "ca", "nb", "ce", "eu", "nan", "ast", "nn", "tt", "eo",
             "arz", "sh", "sl", "cy", "gl", "sk", "oc", "uz", "be", "vec"}
GAIN_IMPROVED = {"en", "ja", "zh"}
CONTAMINATION = {"sv"}
QUARANTINED = {"ceb", "war", "min", "vo", "mul"}

MIN_PAIRS_FOR_AUC = 100
MIN_PAIRS_FOR_TIGHT_AUC = 400


def base_lang(l: str | None) -> str:
    return (l or "").split("-")[0].split("_")[0].lower()


def stratum_of(q: str, c: str) -> str:
    """Exactly one stratum per pair, by precedence. Quarantined FIRST, so a
    ceb<->en pair is excluded rather than credited to en's gain."""
    ls = {base_lang(q), base_lang(c)}
    if ls & QUARANTINED:
        return "excluded_quarantined"
    if ls & CONTAMINATION:
        return "contamination_risk"
    if ls & GAIN_ZERO:
        return "gain_v7zero"
    if ls & GAIN_IMPROVED:
        return "gain_improved"
    return "unchanged"


def script_family(qs: str, cs: str) -> str:
    return "latin_involving" if "LATIN" in (qs, cs) else "non_latin_both"


def auc_ap(labels: List[int], scores: List[float]):
    if len(set(labels)) < 2:
        return None, None
    from sklearn.metrics import average_precision_score, roc_auc_score
    return (float(roc_auc_score(labels, scores)),
            float(average_precision_score(labels, scores)))


def bootstrap_auc_ci(labels, scores, n_boot=200, seed=0):
    """Percentile bootstrap. Reported so a stratum's AUC arrives with the
    precision its pair count actually supports, rather than to 4 decimals
    regardless."""
    if len(set(labels)) < 2 or len(labels) < MIN_PAIRS_FOR_AUC:
        return None
    from sklearn.metrics import roc_auc_score
    rng = np.random.default_rng(seed)
    lab = np.asarray(labels); sc = np.asarray(scores)
    n = len(lab)
    out = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        if len(set(lab[idx].tolist())) < 2:
            continue
        out.append(roc_auc_score(lab[idx], sc[idx]))
    if len(out) < 20:
        return None
    lo, hi = np.percentile(out, [2.5, 97.5])
    return [round(float(lo), 4), round(float(hi), 4)]


def summarise(rows, label, is_floor=False, ci=True) -> dict:
    labels = [r[0] for r in rows]
    scores = [r[1] for r in rows]
    n, npos = len(rows), sum(labels)
    d = {"stratum": label, "n_pairs": n, "n_positive": npos,
         "n_negative": n - npos, "is_floor_not_baseline": is_floor}
    if n < MIN_PAIRS_FOR_AUC or npos == 0 or npos == n:
        d["auc"] = None
        d["average_precision"] = None
        d["verdict"] = (f"INSUFFICIENT: {n} pairs "
                        f"({npos} pos / {n - npos} neg) — reporting N, not an AUC")
        return d
    a, ap = auc_ap(labels, scores)
    d["auc"], d["average_precision"] = round(a, 4), round(ap, 4)
    d["auc_ci95"] = bootstrap_auc_ci(labels, scores) if ci else None
    d["verdict"] = ("ok" if n >= MIN_PAIRS_FOR_TIGHT_AUC
                    else f"wide interval ({n} pairs)")
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--model-dir", default="/vast/ishi/elastic/hf")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    corpus = Path(a.corpus)
    pairs = []
    with open(corpus / "pairs.jsonl", encoding="utf-8") as fh:
        for line in fh:
            pairs.append(json.loads(line))
    for p in pairs:
        p["label"] = int(p["label"])
        p["stratum"] = stratum_of(p["query_lang"], p["candidate_lang"])
        p["family"] = script_family(p["query_script"], p["candidate_script"])
    print(f"loaded {len(pairs):,} pairs", flush=True)

    from collections import Counter
    cnt = Counter(p["stratum"] for p in pairs)
    print("stratum assignment (precedence: quarantined > sv > gain_zero > "
          "gain_improved > unchanged):")
    for k, v in cnt.most_common():
        print(f"   {k:<24} {v:>8,}")

    # ---- v7 embeddings ----
    sys.path.insert(0, str(Path(a.model_dir).resolve()))
    from processing.device import resolve_device
    from inference import SymphonymModel
    from evaluation.retrieval import embed_names
    device = resolve_device(a.device, purpose="stratified-baseline")
    model = SymphonymModel(model_dir=Path(a.model_dir), device=device)
    distinct = sorted({(p["query"], p["query_lang"]) for p in pairs}
                      | {(p["candidate"], p["candidate_lang"]) for p in pairs})
    print(f"embedding {len(distinct):,} distinct (name, lang) on {device}", flush=True)
    t0 = time.time()
    P = embed_names(model, [n for n, _ in distinct], [l for _, l in distinct])
    idx = {nl: i for i, nl in enumerate(distinct)}
    print(f"embedded in {time.time()-t0:.0f}s", flush=True)

    from evaluation.baselines import levenshtein_romanised

    def v7(p):
        return float(P[idx[(p["query"], p["query_lang"])]]
                     @ P[idx[(p["candidate"], p["candidate_lang"])]])

    def lev(p):
        """Returns None when the scorer does not cover the pair.

        `Scored.score` is None for an uncovered pair, and coercing that to 0.0
        would score it as maximally dissimilar -- inventing a confident wrong
        answer where the baseline actually declined to answer, and flattering
        or punishing it depending on the label. discrimination.py excludes
        uncovered pairs for exactly this reason; so does this."""
        s = levenshtein_romanised(p["query"], p["candidate"])
        v = getattr(s, "score", s)
        return None if v is None else float(v)

    report = {"corpus": str(corpus), "device": str(device),
              "stratum_counts": dict(cnt), "scorers": {}}

    for sname, fn in (("symphonym_v7", v7), ("levenshtein_romanised", lev)):
        print(f"\n===== {sname} =====", flush=True)
        raw = [(p["label"], fn(p), p["stratum"], p["family"]) for p in pairs]
        scored = [r for r in raw if r[1] is not None]
        n_uncovered = len(raw) - len(scored)
        if n_uncovered:
            print(f"  {n_uncovered:,} of {len(raw):,} pairs UNCOVERED by this "
                  f"scorer — excluded, not zeroed. Every N below is the "
                  f"covered denominator.", flush=True)
        report.setdefault("uncovered", {})[sname] = n_uncovered
        blocks = []
        # Corpus figure, for continuity with the existing 0.9324 / 0.9002.
        blocks.append(summarise([(l, s) for l, s, _, _ in scored], "CORPUS (all pairs)"))
        for st in ("gain_v7zero", "gain_improved", "contamination_risk",
                   "unchanged", "excluded_quarantined"):
            rows = [(l, s) for l, s, t, _ in scored if t == st]
            floor = st.startswith("gain")
            blocks.append(summarise(rows, st, is_floor=floor))
            # ⚠ Latin-involving and non-Latin<->non-Latin are NEVER averaged.
            for fam in ("latin_involving", "non_latin_both"):
                sub = [(l, s) for l, s, t, f in scored if t == st and f == fam]
                blocks.append(summarise(sub, f"{st} / {fam}", is_floor=floor))
        report["scorers"][sname] = blocks
        print(f"{'stratum':<44}{'N':>9}{'pos':>8}{'AUC':>9}{'AP':>9}  note")
        for b in blocks:
            auc = f"{b['auc']:.4f}" if b["auc"] is not None else "--"
            apv = f"{b['average_precision']:.4f}" if b["average_precision"] is not None else "--"
            tag = " [FLOOR]" if b["is_floor_not_baseline"] and b["auc"] is not None else ""
            print(f"{b['stratum']:<44}{b['n_pairs']:>9,}{b['n_positive']:>8,}"
                  f"{auc:>9}{apv:>9}  {b['verdict']}{tag}")

    Path(a.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
