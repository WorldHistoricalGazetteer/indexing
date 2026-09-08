#!/usr/bin/env python3
"""
Where does the right answer actually sit? The full rank CDF, not two R@k points.

WHY THIS SUPERSEDES AN R@k SWEEP. Every query in this corpus has its partner IN
the haystack -- `usable` filters to exactly that -- so the partner always has a
finite rank and R@k necessarily reaches 1.0 at k = |haystack|. "52% of partners
are outside the top 200" therefore never meant they were unreachable; it meant
their rank is > 200 and we had never looked at where. Reporting the rank
DISTRIBUTION answers at every k at once, and it cannot be read as a ceiling that
isn't there.

🛑 THE TWO RETRIEVERS ARE SCORED INDEPENDENTLY OVER THE WHOLE HAYSTACK. This is
not `reranker.py` re-ordering v7's pool; a re-order can never rescue a partner
v7 did not retrieve, so it could not answer this question even in principle.
Both v7 cosine and romanised normalised-Levenshtein rank all 1,053,229 names
per query.

WHAT THE SHAPES MEAN, decided before the numbers arrive:

  * both curves flat past k=200  -> pool size is not the constraint, the
    EMBEDDING is; the v8 retrain's success criterion is R@200, not R@10.
  * either curve climbing steeply -> that method already retrieves a large part
    of the missing 52% and we are discarding it with k=200; a configuration
    change, available immediately.
  * the two DIVERGING at large k -> they stop failing on the same pairs, which
    contradicts the R@200 agreement to 0.0002 and must be explained before
    anything is built on either.

⚠ Per stratum as well as overall. `both_nonlatin` is n~405 and is the stratum
with most reason to behave differently and least data to say so; its numbers
carry that n wherever they are quoted.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import unicodedata
from pathlib import Path

import numpy as np

KS = (1, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 50000)
PCTS = (25, 50, 75, 90, 95, 99)


def _load(p: Path):
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


def script_of(s: str) -> str:
    for ch in s:
        if ch.isalpha():
            try:
                nm = unicodedata.name(ch)
            except ValueError:
                continue
            return nm.split()[0]
    return "UNKNOWN"


def stratum_of(qs: str, cs: str) -> str:
    ql, cl = qs == "LATIN", cs == "LATIN"
    if ql and cl:
        return "both_latin"
    if ql:
        return "latin_q_nonlatin_c"
    if cl:
        return "nonlatin_q_latin_c"
    return "both_nonlatin"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--model-dir", default="/vast/ishi/elastic/hf")
    ap.add_argument("--queries", type=int, default=6000,
                    help="same value as reranker.py, so the split matches")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=128)
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

    usable = [p for p in pos if p["partner"] in name_to_idx]
    # ⚠ SAME shuffle, seed, and slice as reranker.py, so "the held-out test
    # split" means the same 3,000 queries there and here. Any divergence in
    # these four lines silently compares different populations.
    rng.shuffle(usable)
    usable = usable[:a.queries]
    test = usable[len(usable) // 2:]
    print(f"positives {len(pos):,} · partner present {len([p for p in pos if p['partner'] in name_to_idx]):,}"
          f" · TEST {len(test):,} (same split as reranker.py)", flush=True)
    print(f"haystack {len(hay_names):,} names — rank is over ALL of them", flush=True)

    targets = np.array([name_to_idx[r["partner"]] for r in test], dtype=np.int64)
    strata = [stratum_of(script_of(r["query"]), script_of(r["partner"])) for r in test]

    # ---------- v7 ----------
    sys.path.insert(0, str(Path(a.model_dir).resolve()))
    from processing.device import resolve_device
    from inference import SymphonymModel
    from evaluation.retrieval import embed_names
    device = resolve_device("auto", purpose="recall_ceiling")
    model = SymphonymModel(model_dir=Path(a.model_dir), device=device)

    print(f"embedding haystack on {device}…", flush=True)
    H = embed_names(model, hay_names, hay_langs)
    H = H / np.maximum(np.linalg.norm(H, axis=1, keepdims=True), 1e-9)
    Q = embed_names(model, [r["query"] for r in test],
                    [r.get("query_lang") or "und" for r in test])
    Q = Q / np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-9)

    v7_rank = np.zeros(len(test), dtype=np.int64)
    for i in range(0, len(test), a.chunk):
        sims = Q[i:i + a.chunk] @ H.T
        tgt = sims[np.arange(sims.shape[0]), targets[i:i + a.chunk]][:, None]
        # rank = 1 + how many score strictly higher. No sort of 1.05M needed.
        v7_rank[i:i + a.chunk] = 1 + (sims > tgt).sum(axis=1)
        if i % (a.chunk * 8) == 0:
            print(f"   v7 {i:,}/{len(test):,}", flush=True)
    del H, Q

    # ---------- romanised Levenshtein, over the SAME whole haystack ----------
    from rapidfuzz import process
    from rapidfuzz.distance import Levenshtein
    from anyascii import anyascii

    print("romanising haystack…", flush=True)
    hay_rom = [anyascii(n).lower() for n in hay_names]
    q_rom = [anyascii(r["query"]).lower() for r in test]

    lev_rank = np.zeros(len(test), dtype=np.int64)
    for i in range(0, len(test), a.chunk):
        M = process.cdist(q_rom[i:i + a.chunk], hay_rom,
                          scorer=Levenshtein.normalized_similarity,
                          dtype=np.float32, workers=-1)
        tgt = M[np.arange(M.shape[0]), targets[i:i + a.chunk]][:, None]
        lev_rank[i:i + a.chunk] = 1 + (M > tgt).sum(axis=1)
        if i % (a.chunk * 8) == 0:
            print(f"   lev {i:,}/{len(test):,}", flush=True)

    # ---------- report ----------
    def curve(ranks: np.ndarray) -> dict:
        return {f"recall@{k}": round(float((ranks <= k).mean()), 4) for k in KS}

    def pcts(ranks: np.ndarray) -> dict:
        return {f"p{p}": int(np.percentile(ranks, p)) for p in PCTS}

    rep = {"n_test": len(test), "haystack": len(hay_names),
           "overall": {}, "percentiles": {}, "strata": {}}
    for label, ranks in (("v7_cosine", v7_rank), ("levenshtein_romanised", lev_rank)):
        rep["overall"][label] = curve(ranks)
        rep["percentiles"][label] = pcts(ranks)

    print(f"\nRANK CDF over the FULL haystack, TEST n={len(test):,}")
    hdr = "".join(f"{'R@'+str(k):>9}" for k in KS)
    print(f"{'retriever':<24}{hdr}")
    for label, ranks in (("v7_cosine", v7_rank), ("levenshtein_romanised", lev_rank)):
        print(f"{label:<24}" + "".join(f"{(ranks <= k).mean():>9.4f}" for k in KS))

    print("\nrank percentiles (where the partner ACTUALLY sits):")
    print(f"{'retriever':<24}" + "".join(f"{'p'+str(p):>10}" for p in PCTS))
    for label, ranks in (("v7_cosine", v7_rank), ("levenshtein_romanised", lev_rank)):
        print(f"{label:<24}" + "".join(f"{int(np.percentile(ranks, p)):>10,}" for p in PCTS))

    # 🛑 The agreement claim, tested rather than assumed: do the two miss the
    # SAME pairs? An identical R@200 is equally consistent with disjoint misses.
    for k in (200, 1000, 5000):
        a_hit, b_hit = v7_rank <= k, lev_rank <= k
        both = int((a_hit & b_hit).sum())
        only_a = int((a_hit & ~b_hit).sum())
        only_b = int((~a_hit & b_hit).sum())
        neither = int((~a_hit & ~b_hit).sum())
        union = both + only_a + only_b
        print(f"\nat k={k:,}:  both {both:,}  v7-only {only_a:,}  lev-only {only_b:,}"
              f"  neither {neither:,}")
        print(f"   UNION (an oracle picking the better of the two) = {union/len(test):.4f}"
              f"   vs best single {max(a_hit.mean(), b_hit.mean()):.4f}")
        rep.setdefault("overlap", {})[f"k{k}"] = {
            "both": both, "v7_only": only_a, "lev_only": only_b,
            "neither": neither, "union_recall": round(union / len(test), 4)}

    print("\nper stratum:")
    for st in sorted(set(strata)):
        m = np.array([s == st for s in strata])
        n = int(m.sum())
        row = {"n": n}
        print(f"   {st:<22} n={n:>6,}")
        for label, ranks in (("v7_cosine", v7_rank), ("levenshtein_romanised", lev_rank)):
            sub = ranks[m]
            row[label] = {**curve(sub), **pcts(sub)}
            print(f"      {label:<22}" +
                  "".join(f"{'R@'+str(k)}={(sub <= k).mean():.3f} " for k in (10, 200, 1000, 5000)) +
                  f" p50={int(np.percentile(sub, 50)):,}")
        rep["strata"][st] = row

    Path(a.out).write_text(json.dumps(rep, indent=2))
    print(f"\n-> {a.out}")
    print("\n✅ Every partner IS in the haystack, so these curves reach 1.0 at "
          f"k={len(hay_names):,} by construction. Nothing here is a ceiling; it is "
          "a statement about WHERE the answer sits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
