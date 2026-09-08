#!/usr/bin/env python3
"""Does the SHIPPED blend realise the +0.146 the 2x2 says is available?

⚠ MEASUREMENT ONLY. This simulates gateway scoring inside the evaluation
harness. It imports nothing from `gateway/` and changes nothing there; several
sessions share that tree and `origin/main` deploys to the running gateway.

🛑 TIE CONVENTION: PESSIMISTIC throughout — `retrieval.ranks_from_scores`,
`(row > t).sum() + (row == t).sum()`. Stated because an optimistic convention
is what made this module's earlier lexical figures overstate the baseline by up
to 0.055, and the blend below is FULL of ties: the exact tier is a flat 2.5, so
every exactly-spelled candidate ties with every other.

WHAT IS REPLICATED FROM THE SHIPPED SOURCE (constants and formulae, read from
`gateway/es_helpers.py`, not guessed):

    exact      LEXICAL_EXACT_BOOST x weight            2.5   flat, case-folded
    near-miss  LEXICAL_FUZZY_BOOST x resemblance x w   0.75  difflib ratio,
                                                            autojunk off,
                                                            floor 0.5
    phonetic   pass-normalised KNN score               <=1.0 cos / top-cos
    total      SUM, not max                            <=4.25

⚠ WHAT IS A PROXY, AND THE RESULT MUST NOT BE READ AS IF IT WERE NOT:

  * The near-miss tier RETRIEVES via an ES `multi_match` (`name^3,
    name_romanized^2, name.prefix`, `fuzziness: AUTO`, size 200). Here that
    retrieval is romanised-Levenshtein top-200. The SCORING is the shipped
    formula; the CANDIDATE SET is a stand-in, so a name ES would surface and
    rapidfuzz would not is invisible to this.
  * The phonetic tier's normalisation divides by the pass's own top score
    (`collect_place_ids(normalise=True)`); replicated, but on cosines rather
    than ES `_score`.
  * One name per candidate: no `attestations` fan-out, so this ranks NAMES the
    way the harness does rather than place_ids the way the gateway does.

🛑 WHAT THE HEADLINE NUMBER MEANS. The blend's candidate pool here is the UNION
of its three retrieval passes — which is, by construction, the oracle union of
the 2x2. So `oracle_union - blend` is NOT retrieval loss. It is purely the cost
of the TIER ORDERING: partners that were retrieved and then ranked below the
cut. That is the quantity asked for, and it is the only one this design can
measure.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

# Read from gateway/es_helpers.py, not invented. Kept as literals so this file
# has no import path into the gateway package.
LEXICAL_EXACT_BOOST = 2.5      # es_helpers.py:513
LEXICAL_FUZZY_BOOST = 0.75     # es_helpers.py:634
LEXICAL_FUZZY_FLOOR = 0.5      # es_helpers.py:641
PHONETIC_CEILING = 1.0
KNN_K = 200                    # build_phonetic_knn default
KNN_SIMILARITY = 0.7           # build_phonetic_knn default
LEX_POOL = 200                 # build_lexical_fuzzy_query size

KS = (1, 5, 10, 20, 50, 100, 200)


def _load(p: Path):
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


def script_of(s: str) -> str:
    for ch in s:
        if ch.isalpha():
            try:
                return unicodedata.name(ch).split()[0]
            except ValueError:
                continue
    return "UNKNOWN"


def scripts_for(row: dict) -> tuple[str, str]:
    qs, ps = row.get("query_script"), row.get("partner_script")
    return (qs, ps) if qs and ps else (script_of(row["query"]),
                                       script_of(row["partner"]))


def resemblance(a: str, b: str) -> float:
    """`name_resemblance` verbatim: difflib ratio, casefolded, autojunk off."""
    left, right = (a or "").strip().casefold(), (b or "").strip().casefold()
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--model-dir", default="/vast/ishi/elastic/hf")
    ap.add_argument("--balanced", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=64)
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
    groups = defaultdict(list)
    for r in usable:
        groups[scripts_for(r)].append(r)
    test = []
    for _, items in sorted(groups.items()):
        test.extend(items if len(items) <= a.balanced
                    else rng.sample(items, a.balanced))
    rng.shuffle(test)
    print(f"BALANCED {a.balanced}/pair over {len(groups)} pairs -> {len(test):,}",
          flush=True)
    if len(test) != 8713:
        print(f"🛑 §8 is 8,713 at 100/pair; this is {len(test):,} — NOT §8's "
              f"population, do not compare the numbers below to its anchors.",
              flush=True)
    else:
        print("   ✅ EXACTLY §8's 8,713 — population reproduced.", flush=True)

    targets = np.array([name_to_idx[r["partner"]] for r in test], dtype=np.int64)

    sys.path.insert(0, str(Path(a.model_dir).resolve()))
    from processing.device import resolve_device
    from inference import SymphonymModel
    from evaluation.retrieval import embed_names, ranks_from_scores
    device = resolve_device("auto", purpose="blend_realisation")
    model = SymphonymModel(model_dir=Path(a.model_dir), device=device)

    print(f"embedding {len(hay_names):,} haystack names on {device}…", flush=True)
    H = embed_names(model, hay_names, hay_langs)
    H = H / np.maximum(np.linalg.norm(H, axis=1, keepdims=True), 1e-9)
    Q = embed_names(model, [r["query"] for r in test],
                    [r.get("query_lang") or "und" for r in test])
    Q = Q / np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-9)

    from rapidfuzz import process
    from rapidfuzz.distance import Levenshtein
    from anyascii import anyascii
    hay_rom = [anyascii(n).lower() for n in hay_names]
    hay_fold = [n.strip().casefold() for n in hay_names]
    fold_index: dict[str, list[int]] = defaultdict(list)
    for i, f in enumerate(hay_fold):
        fold_index[f].append(i)

    n = len(test)
    blend_rank = np.zeros(n, dtype=np.int64)
    v7_rank = np.zeros(n, dtype=np.int64)
    lev_rank = np.zeros(n, dtype=np.int64)
    # which 2x2 cell each query's partner falls in, at k=200
    cell = np.empty(n, dtype=object)
    tier_of_partner = np.empty(n, dtype=object)

    for i in range(0, n, a.chunk):
        rows = test[i:i + a.chunk]
        sims = Q[i:i + len(rows)] @ H.T
        M = process.cdist([anyascii(r["query"]).lower() for r in rows], hay_rom,
                          scorer=Levenshtein.normalized_similarity,
                          dtype=np.float32, workers=-1)
        v7_rank[i:i + len(rows)] = np.asarray(
            ranks_from_scores(sims, targets[i:i + len(rows)], pool=None),
            dtype=np.int64)
        lev_rank[i:i + len(rows)] = np.asarray(
            ranks_from_scores(M, targets[i:i + len(rows)], pool=None),
            dtype=np.int64)

        for j, r in enumerate(rows):
            q = r["query"]
            tgt = targets[i + j]
            # ---- the three retrieval passes, as the gateway runs them -------
            knn = np.argpartition(-sims[j], KNN_K)[:KNN_K]
            knn = knn[sims[j][knn] >= KNN_SIMILARITY]
            lex = np.argpartition(-M[j], LEX_POOL)[:LEX_POOL]
            exact = np.array(fold_index.get(q.strip().casefold(), []),
                             dtype=np.int64)
            pool = np.unique(np.concatenate(
                [knn, lex, exact]) if len(exact) else np.concatenate([knn, lex]))

            # ---- the three tiers, SUMMED (es_helpers: added, not maxed) -----
            score = np.zeros(len(pool), dtype=np.float64)
            pos_in_pool = {int(c): k for k, c in enumerate(pool)}
            top_cos = float(sims[j][knn].max()) if len(knn) else 0.0
            for c in knn:
                if top_cos > 0:
                    score[pos_in_pool[int(c)]] += min(
                        PHONETIC_CEILING, float(sims[j][c]) / top_cos)
            for c in exact:
                score[pos_in_pool[int(c)]] += LEXICAL_EXACT_BOOST
            qf = q.strip().casefold()
            for c in pool:
                rr = resemblance(qf, hay_names[int(c)])
                if rr >= LEXICAL_FUZZY_FLOOR:
                    score[pos_in_pool[int(c)]] += LEXICAL_FUZZY_BOOST * rr

            if tgt in pos_in_pool:
                t = score[pos_in_pool[int(tgt)]]
                blend_rank[i + j] = int((score > t).sum() + (score == t).sum())
                # which tier carried the partner, for (b)
                in_knn = tgt in set(int(x) for x in knn)
                in_exact = tgt in set(int(x) for x in exact)
                rr = resemblance(qf, hay_names[int(tgt)])
                tier_of_partner[i + j] = ("exact" if in_exact else
                                          "near-miss" if rr >= LEXICAL_FUZZY_FLOOR
                                          else "phonetic-only" if in_knn else "?")
            else:
                blend_rank[i + j] = 10 ** 9      # not retrieved at all
                tier_of_partner[i + j] = "not-retrieved"

            v_hit = int(v7_rank[i + j]) <= 200
            l_hit = int(lev_rank[i + j]) <= 200
            cell[i + j] = ("both" if v_hit and l_hit else
                           "v7-only" if v_hit else
                           "lev-only" if l_hit else "neither")
        if i % (a.chunk * 16) == 0:
            print(f"   {i:,}/{n:,}", flush=True)

    def r_at(ranks, k):
        return float((ranks <= k).mean())

    union200 = np.array([c != "neither" for c in cell])
    rep = {"n": n, "tie_convention": "pessimistic (retrieval.ranks_from_scores)"}
    print(f"\nR@k — §8 population, n={n:,}, PESSIMISTIC ties")
    print(f"{'method':<24}" + "".join(f"{'R@'+str(k):>9}" for k in KS))
    for label, ranks in (("v7_cosine", v7_rank),
                         ("levenshtein_romanised", lev_rank),
                         ("SHIPPED BLEND (sim)", blend_rank)):
        rep[label] = {f"recall@{k}": round(r_at(ranks, k), 4) for k in KS}
        print(f"{label:<24}" + "".join(f"{r_at(ranks, k):>9.4f}" for k in KS))
    print(f"{'ORACLE union @200':<24}" + " " * (9 * (len(KS) - 1)) +
          f"{union200.mean():>9.4f}")
    rep["oracle_union@200"] = round(float(union200.mean()), 4)

    gap = float(union200.mean()) - r_at(blend_rank, 200)
    print(f"\n🛑 ORDERING COST at k=200: oracle union {union200.mean():.4f} - "
          f"blend {r_at(blend_rank, 200):.4f} = {gap:+.4f}")
    print("   (the pool is the union by construction, so this is NOT retrieval "
          "loss — it is partners retrieved and then ranked below the cut)")
    rep["ordering_cost@200"] = round(gap, 4)

    print("\n(b) blend MISSES at k=10, by 2x2 cell of origin:")
    miss10 = blend_rank > 10
    for c in ("both", "v7-only", "lev-only", "neither"):
        m = np.array([x == c for x in cell])
        tot = int(m.sum())
        if not tot:
            print(f"   {c:<10} n=0 — EMPTY, tests nothing")
            continue
        print(f"   {c:<10} n={tot:>6,}   missed at k=10: {int((m & miss10).sum()):>6,}"
              f"  ({(m & miss10).sum() / tot:6.2%})")
        rep.setdefault("miss@10_by_cell", {})[c] = {
            "n": tot, "missed": int((m & miss10).sum())}

    print("\n    which tier carried the partner (all queries):")
    tc = defaultdict(int)
    for t in tier_of_partner:
        tc[t] += 1
    for t, k in sorted(tc.items(), key=lambda x: -x[1]):
        print(f"      {t:<16} {k:>6,}  {k / n:6.2%}")
    rep["partner_tier"] = dict(tc)

    Path(a.out).write_text(json.dumps(rep, indent=2))
    print(f"\n-> {a.out}")
    print("\n⚠ The near-miss CANDIDATE SET is a rapidfuzz stand-in for ES "
          "multi_match/fuzziness AUTO. Scoring is the shipped formula; retrieval "
          "is not. A name ES would surface and rapidfuzz would not is invisible "
          "here, so the blend figure is an UPPER bound on nothing and a lower "
          "bound on nothing — it is the shipped ORDERING over a proxy pool.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
