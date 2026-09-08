#!/usr/bin/env python3
"""
Cross-encoder-style re-ranking of the retrieval pool — measured, not deployed.

⚠ SCOPE: this produces numbers for a decision. It changes no gateway code.

WHAT IT EXPLOITS. v7 is good at judging a PAIR (discrimination AUC 0.9324) and
poor at ordering a LIST (R@10 0.2940 against R@200 0.4766). Everything between
those two figures is ordering loss inside a pool that already contains the right
answer, and a second pass over the pool can recover it without retraining or
reindexing.

🛑 THE CAP IS STRUCTURAL AND MUST TRAVEL WITH EVERY NUMBER HERE. R@200 is 0.4766
for v7 and 0.4768 for romanised Levenshtein — agreeing to 0.0002. Two methods
with completely different failure modes miss the SAME ~52% of partners, so those
are not in the pool for any reranker to find. A reranker competes for the 48%
that is there. "+62% relative" is a large gain over a pool missing half the right
answers, and quoting it without that denominator is the error this file exists to
prevent.

⚠ WEIGHTS ARE FITTED ON A DEV SPLIT AND REPORTED ON A HELD-OUT TEST SPLIT.
Choosing a blend by looking at the number it produces, then quoting that number,
is fitting to the test set. The split is by query, so a query's pool cannot
appear in both halves.

🛑 AND IT IS EVALUATED AGAINST THE HARD NEGATIVES, not only positives. A
reranker admits more pairs to high ranks BY DESIGN, so a positives-only
measurement cannot show its cost. The old corpus negatives are 98.22% trivially
separable and would report any reranker as free — which is exactly what they
reported for accent folding, wrongly.

🛑 BUT THE HARD-NEGATIVE SET IS ADVERSARIAL TO THE LEXICAL COMPONENT BY
CONSTRUCTION, AND THE THREE-WAY TABLE MUST NOT BE READ WITHOUT THIS.
`mine_hard_negatives` SELECTS pairs at lexical similarity >= 0.80 (the sampled
rows sit at 1.000). A scorer cannot discriminate on an axis the sampling frame
holds constant, so `lexical_only` measuring AUC ~0.50 here is a property of the
CORPUS, not of lexical matching — the same shape as
[[corpus_property_as_model_property]]. Two consequences, both directional:

  * The blend's AUC is a FLOOR, not an estimate. It is dragged toward chance by
    a component this set is built to defeat. Its real-world precision cost is at
    most what is shown, and probably less.
  * v7's own AUC here (0.7687) is NOT comparable to its published 0.9324 — those
    are different negative sets. It is comparable to the other two rows, which
    is the only reason the table exists.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import numpy as np

POOL = 200


def _load(p: Path):
    with open(p, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh]


def fold(s: str) -> str:
    d = unicodedata.normalize("NFD", s)
    return "".join(c for c in d if not unicodedata.combining(c)).lower().strip()


def stratum_of(qs: str, cs: str) -> str:
    if qs == "LATIN" and cs != "LATIN":
        return "latin_q_nonlatin_c"
    if qs != "LATIN" and cs == "LATIN":
        return "nonlatin_q_latin_c"
    if qs == "LATIN" and cs == "LATIN":
        return "both_latin"
    return "both_nonlatin"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--hard-negatives", required=True)
    ap.add_argument("--model-dir", default="/vast/ishi/elastic/hf")
    ap.add_argument("--queries", type=int, default=6000)
    ap.add_argument("--pool", type=int, default=POOL)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    corpus = Path(a.corpus)
    rng = random.Random(a.seed)
    hay = _load(corpus / "haystack.jsonl")
    pos = _load(corpus / "positives.jsonl")
    hay_names = [d["name"] for d in hay]
    hay_langs = [d.get("lang") or "und" for d in hay]
    name_to_idx: dict[str, int] = {}
    for i, n in enumerate(hay_names):
        name_to_idx.setdefault(n, i)

    # Only queries whose partner is actually in the haystack; a missing partner
    # is an unanswerable question, not a miss.
    usable = [p for p in pos if p["partner"] in name_to_idx]
    print(f"positives: {len(pos):,}   partner present: {len(usable):,}", flush=True)
    rng.shuffle(usable)
    usable = usable[:a.queries]
    half = len(usable) // 2
    dev, test = usable[:half], usable[half:]
    print(f"dev {len(dev):,} / test {len(test):,}  (split BY QUERY)", flush=True)

    sys.path.insert(0, str(Path(a.model_dir).resolve()))
    from processing.device import resolve_device
    from inference import SymphonymModel
    from evaluation.retrieval import embed_names
    device = resolve_device("auto", purpose="reranker")
    model = SymphonymModel(model_dir=Path(a.model_dir), device=device)

    print(f"embedding {len(hay_names):,} haystack names on {device}…", flush=True)
    H = embed_names(model, hay_names, hay_langs)
    H = H / np.maximum(np.linalg.norm(H, axis=1, keepdims=True), 1e-9)

    def pools_for(rows):
        qn = [r["query"] for r in rows]
        ql = [r.get("query_lang") or "und" for r in rows]
        Q = embed_names(model, qn, ql)
        Q = Q / np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-9)
        out = []
        for i in range(0, len(rows), 256):
            sims = Q[i:i + 256] @ H.T
            idx = np.argpartition(-sims, a.pool, axis=1)[:, :a.pool]
            for j in range(idx.shape[0]):
                r = rows[i + j]
                cand = idx[j]
                cs = sims[j, cand]
                order = np.argsort(-cs)
                out.append({"row": r, "cand": cand[order], "cos": cs[order],
                            "target": name_to_idx[r["partner"]]})
        return out

    from rapidfuzz.distance import Levenshtein
    from anyascii import anyascii

    def lex(q: str, c: str) -> float:
        a_, b_ = anyascii(q).lower(), anyascii(c).lower()
        if not a_ or not b_:
            return 0.0
        return 1.0 - Levenshtein.distance(a_, b_) / max(len(a_), len(b_))

    def rerank_rank(entry, w: float):
        """Rank of the true partner after blending cosine with lexical."""
        q = entry["row"]["query"]
        lx = np.array([lex(q, hay_names[c]) for c in entry["cand"]], dtype=np.float32)
        blended = (1.0 - w) * entry["cos"] + w * lx
        order = np.argsort(-blended)
        ranked = entry["cand"][order]
        hit = np.nonzero(ranked == entry["target"])[0]
        return int(hit[0]) + 1 if len(hit) else None

    def base_rank(entry):
        hit = np.nonzero(entry["cand"] == entry["target"])[0]
        return int(hit[0]) + 1 if len(hit) else None

    print("building dev pools…", flush=True)
    devp = pools_for(dev)
    print("building test pools…", flush=True)
    testp = pools_for(test)

    def r_at(ranks, k):
        return sum(1 for r in ranks if r is not None and r <= k) / max(len(ranks), 1)

    # --- fit the blend on DEV only ---
    print("\nweight sweep on DEV (never on test):", flush=True)
    best_w, best = 0.0, -1.0
    for w in [i / 10 for i in range(11)]:
        rk = [rerank_rank(e, w) for e in devp]
        v = r_at(rk, 10)
        print(f"   w={w:.1f}  dev R@10={v:.4f}")
        if v > best:
            best, best_w = v, w
    print(f"   chosen w={best_w:.1f}")

    # --- report on TEST ---
    base = [base_rank(e) for e in testp]
    rr = [rerank_rank(e, best_w) for e in testp]
    lexonly = [rerank_rank(e, 1.0) for e in testp]
    rep = {"weight": best_w, "pool": a.pool, "n_test": len(testp),
           "test": {}}
    for label, rk in (("v7_pool_order", base), ("lexical_only", lexonly),
                      ("reranked", rr)):
        rep["test"][label] = {f"recall@{k}": round(r_at(rk, k), 4)
                              for k in (1, 5, 10, 50, a.pool)}
    print(f"\nTEST (n={len(testp):,}), pool={a.pool}:")
    print(f"{'method':<18}{'R@1':>9}{'R@5':>9}{'R@10':>9}{'R@50':>9}{'R@'+str(a.pool):>9}")
    for label in ("v7_pool_order", "lexical_only", "reranked"):
        d = rep["test"][label]
        print(f"{label:<18}" + "".join(f"{d[f'recall@{k}']:>9.4f}"
                                       for k in (1, 5, 10, 50, a.pool)))

    # per stratum
    print("\nper stratum (R@10):")
    strat = defaultdict(lambda: {"base": [], "rr": []})
    for e, b, r in zip(testp, base, rr):
        k = stratum_of(e["row"]["query_script"], e["row"]["partner_script"])
        strat[k]["base"].append(b); strat[k]["rr"].append(r)
    rep["by_stratum"] = {}
    for k, v in sorted(strat.items(), key=lambda kv: -len(kv[1]["base"])):
        b10, r10 = r_at(v["base"], 10), r_at(v["rr"], 10)
        rep["by_stratum"][k] = {"n": len(v["base"]), "base_r10": round(b10, 4),
                                "reranked_r10": round(r10, 4),
                                "delta": round(r10 - b10, 4)}
        print(f"   {k:<22}n={len(v['base']):>6,}  {b10:.4f} -> {r10:.4f}  "
              f"{r10-b10:+.4f}")

    # --- precision against the HARD negatives, same scoring function ---
    print("\nprecision against hard negatives (same blend):", flush=True)
    hn = []
    with open(a.hard_negatives, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i % 97 == 0:          # deterministic thinning; the set is 3.78M
                hn.append(json.loads(line))
    print(f"   sampled {len(hn):,} hard negatives", flush=True)

    qn = [d["query"] for d in hn]; cn = [d["candidate"] for d in hn]
    QN = embed_names(model, qn, [d.get("query_cc") or "und" for d in hn])
    CN = embed_names(model, cn, [d.get("candidate_cc") or "und" for d in hn])
    QN = QN / np.maximum(np.linalg.norm(QN, axis=1, keepdims=True), 1e-9)
    CN = CN / np.maximum(np.linalg.norm(CN, axis=1, keepdims=True), 1e-9)
    cos_n = np.einsum("ij,ij->i", QN, CN)
    lex_n = np.array([lex(q, c) for q, c in zip(qn, cn)], dtype=np.float32)
    neg_score = (1.0 - best_w) * cos_n + best_w * lex_n

    # Positive scores from the true partner of each test query.
    pq = [e["row"]["query"] for e in testp]
    pc = [e["row"]["partner"] for e in testp]
    PQ = embed_names(model, pq, [e["row"].get("query_lang") or "und" for e in testp])
    PC = embed_names(model, pc, [e["row"].get("partner_lang") or "und" for e in testp])
    PQ = PQ / np.maximum(np.linalg.norm(PQ, axis=1, keepdims=True), 1e-9)
    PC = PC / np.maximum(np.linalg.norm(PC, axis=1, keepdims=True), 1e-9)
    pos_score = ((1.0 - best_w) * np.einsum("ij,ij->i", PQ, PC)
                 + best_w * np.array([lex(q, c) for q, c in zip(pq, pc)],
                                     dtype=np.float32))

    # ⚠ AUC ON THE HARD NEGATIVES IS MEANINGLESS WITHOUT THE SAME MEASUREMENT
    # FOR v7 ALONE. Comparing the blend's AUC here against v7's published 0.9324
    # would compare across two different negative sets -- the published figure
    # is over negatives that are 98.22% trivially separable. The only honest
    # comparison is w=0 versus the chosen w, on THESE pairs.
    from sklearn.metrics import roc_auc_score, average_precision_score
    labels = np.r_[np.ones(len(pos_score)), np.zeros(len(neg_score))]

    pos_cos = np.einsum("ij,ij->i", PQ, PC)
    variants = {}
    for name, w in (("v7_cosine_only", 0.0), ("lexical_only", 1.0),
                    (f"blend_w{best_w:.1f}", best_w)):
        ns = (1.0 - w) * cos_n + w * lex_n
        ps = ((1.0 - w) * pos_cos
              + w * np.array([lex(q, c) for q, c in zip(pq, pc)], dtype=np.float32))
        variants[name] = {
            "auc": round(float(roc_auc_score(labels, np.r_[ps, ns])), 4),
            "ap": round(float(average_precision_score(labels, np.r_[ps, ns])), 4),
            "pos_mean": round(float(ps.mean()), 4),
            "neg_mean": round(float(ns.mean()), 4),
            "neg_p99": round(float(np.percentile(ns, 99)), 4),
        }
    rep["hard_negative_variants"] = variants
    print("\n   SAME hard negatives, three scorers — the only fair comparison:")
    print(f"   {'scorer':<20}{'AUC':>9}{'AP':>9}{'pos mean':>11}{'neg mean':>11}")
    for k, v in variants.items():
        print(f"   {k:<20}{v['auc']:>9.4f}{v['ap']:>9.4f}"
              f"{v['pos_mean']:>11.4f}{v['neg_mean']:>11.4f}")

    scores = np.r_[pos_score, neg_score]
    rep["hard_negative_eval"] = {
        "n_pos": int(len(pos_score)), "n_neg": int(len(neg_score)),
        "auc": round(float(roc_auc_score(labels, scores)), 4),
        "ap": round(float(average_precision_score(labels, scores)), 4),
        "pos_mean": round(float(pos_score.mean()), 4),
        "neg_mean": round(float(neg_score.mean()), 4),
        "neg_p99": round(float(np.percentile(neg_score, 99)), 4),
        "neg_max": round(float(neg_score.max()), 4),
    }
    h = rep["hard_negative_eval"]
    print(f"   AUC {h['auc']}   AP {h['ap']}")
    print(f"   positive mean {h['pos_mean']}   hard-negative mean {h['neg_mean']}")
    print(f"   hard-negative p99 {h['neg_p99']}   max {h['neg_max']}")
    for thr in (0.5, 0.6, 0.7, 0.8, 0.9):
        tp = int((pos_score >= thr).sum()); fp = int((neg_score >= thr).sum())
        prec = tp / (tp + fp) if (tp + fp) else None
        print(f"   thr {thr:.1f}: TP {tp:>6,}  FP {fp:>6,}  "
              f"precision {prec if prec is None else round(prec,4)}")

    Path(a.out).write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    print(f"\n-> {a.out}")
    print("\n⚠ R@200 is the CAP: ~52% of partners are outside the pool for any "
          "method,\n  so every gain above is over the 48% that is reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
