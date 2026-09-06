"""Gate 3 as a CURVE — R@k for every k a reranker could be given, not four points.

    python -m evaluation.rank_curve --corpus DIR --model-dir hf \
        [--vectors haystack_v7.npy] [--vectors-out haystack_v7.npy] \
        [--ks 1,5,10,20,50,100,200,500,1000] --out-dir DIR

THE QUESTION THIS EXISTS TO ANSWER. §8 measured that v7 wins discrimination
(AUC 0.9324) and loses retrieval (R@10 0.294 vs levenshtein_romanised's 0.323),
and proposed ONE mechanism for both: a space too dense to order a 200-deep pool.
If that is the whole story, a cross-encoder reranker over the top-k converts v7's
pairwise strength into ranking strength with no retrain. But a reranker can only
reorder what retrieval hands it, so its ceiling is R@pool and its headroom is
R@pool - R@k. Those are the numbers, and neither is a model property alone:
`R@200 - R@10` is the entire prize, and if it is small the deficit is RECALL and
reranking cannot help by construction.

WHY THIS IS A SWEEP AND NOT A RE-RUN. `ranks_from_scores` already computes the
full rank of the partner and only then truncates to `pool`, so every k in the
curve is one arithmetic pass over ranks that were computed anyway. Truncation is
therefore thrown-away information, and §8 threw it away four times. This module
computes ranks ONCE with `pool=None` and writes them to `ranks.jsonl`, after
which any k — and any stratification — is post-processing forever.

THE PRE-REGISTERED CHECK, which is the point of running it this way. §8's corpus,
seed and query budget are known, so four cells of the answer are known BEFORE the
run: R@{1,10,100,200} for symphonym_v7 and the two romanised string baselines.
`--expect` asserts them. A sweep that cannot reproduce the four points it
brackets is not a finer measurement of the same thing, and would otherwise look
exactly like one. Failing that check is the intended outcome when something has
moved; passing it is what licenses the k values nobody has seen.

STRATIFY BEFORE AVERAGING — 35.1% of CJK<->LATIN positives are byte-identical
after romanisation (§8.3), which makes edit distance a NEAR-ORACLE there and a
corpus-wide mean a statement about label provenance rather than about matching.
Every curve is therefore emitted three ways: overall (reported, never quoted
alone), by script pair, and split LATIN-involving / non-Latin<->non-Latin. Each
cell carries its own n and its own hit count, so no rate appears without the
denominator that produced it.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

#: The k values a reranker could plausibly be handed. 200 is the gateway's KNN
#: pool and therefore the operational ceiling; the values above it are here to
#: say whether the partner is *near* the pool or nowhere, which is the difference
#: between "widen k" and "the retrieval stage is the wrong lever".
DEFAULT_KS = (1, 5, 10, 20, 50, 100, 200, 500, 1000)

#: §8, 5 September 2026, corpus 20260905T2000Z, seed 0, 40 queries per script
#: pair. Asserted, not assumed — see the module docstring.
EXPECTED_V8_SECTION_8 = {
    "symphonym_v7":          {"recall@1": 0.0662, "recall@10": 0.2942,
                              "recall@100": 0.4359, "recall@200": 0.4766},
    "levenshtein_romanised": {"recall@1": 0.0729, "recall@10": 0.3230,
                              "recall@100": 0.4414, "recall@200": 0.4768},
    "jaro_winkler_romanised": {"recall@1": 0.0776, "recall@10": 0.3146,
                               "recall@100": 0.4195, "recall@200": 0.4491},
}
#: §8 reports "8,713 queries" without saying at what budget. `--queries-per-pair`
#: defaults to 40 in `run_benchmark`, and 40 yields 4,843 — so §8 was run with an
#: explicit non-default that its own write-up does not record. Recovered by
#: sweeping the budget against the corpus's script-pair histogram: 100 gives
#: exactly 8,713, and no other value is close (80 -> 7,500; 120 -> 9,830).
#: ⚠ The default here is therefore 100, deliberately diverging from
#: `run_benchmark`'s, because matching §8 is this module's whole purpose.
EXPECTED_QUERIES = 8713
EXPECTED_HAYSTACK = 1053229


class ReplicationFailure(AssertionError):
    """A pre-registered §8 cell did not reproduce, so the new k values are unsafe."""


def _load_jsonl(path: Path):
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def stratum_of(query_script: str, partner_script: str) -> str:
    """LATIN-involving vs non-Latin<->non-Latin.

    The split exists because romanisation is the baseline's cheat: where one side
    of the pair is a Latin label that was itself produced by transliterating the
    other, edit distance is scoring transliteration against transliteration. That
    can only happen when LATIN is one of the two scripts, so this is the boundary
    the confound respects, and averaging across it is what hides it.
    """
    return "latin_involving" if "LATIN" in (query_script, partner_script) else "non_latin_pair"


def curve(ranks, ks=DEFAULT_KS) -> dict:
    """R@k for every k, each with its numerator and denominator, plus MRR.

    Ranks here are FULL ranks over the whole haystack (never None), so a miss at
    k is a statement about ordering at that depth and nothing else — unlike the
    pooled form, where None conflates "ranked 201st" with "ranked 900,000th".
    """
    n = len(ranks)
    if not n:
        return {"n": 0}
    out = {"n": n}
    for k in ks:
        hits = sum(1 for r in ranks if r <= k)
        out[f"recall@{k}"] = hits / n
        out[f"hits@{k}"] = hits
    out["mrr"] = sum(1.0 / r for r in ranks) / n
    srt = sorted(ranks)
    out["median_rank"] = srt[n // 2]
    out["p90_rank"] = srt[min(n - 1, int(0.9 * n))]
    # The prize a reranker is competing for: what a PERFECT reordering of the
    # top-200 would add to R@10, and what no reordering can ever reach.
    out["rerank_headroom@10_from_200"] = out["recall@200"] - out["recall@10"] \
        if "recall@200" in out and "recall@10" in out else None
    return out


def curves_by(groups: dict[str, list[int]], ks=DEFAULT_KS) -> dict:
    return {k: curve(v, ks) for k, v in sorted(groups.items())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--model-dir", default="hf")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--threads", type=int, default=16,
                    help="torch CPU threads; 16 is the measured peak (§5.13), "
                         "and 48 is 4x WORSE than 16 on a 48-core node")
    ap.add_argument("--queries-per-pair", type=int, default=100,
                    help="must match the §8 run for the replication check to mean "
                         "anything. 100 — NOT run_benchmark's documented default of "
                         "40, which yields 4,843 queries against §8's 8,713")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--ks", default=",".join(str(k) for k in DEFAULT_KS))
    ap.add_argument("--vectors", help="pre-computed haystack vectors (.npy)")
    ap.add_argument("--vectors-out", help="write the haystack vectors here for reuse")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--scorers", default="symphonym_v7,levenshtein_romanised,jaro_winkler_romanised")
    ap.add_argument("--no-expect", action="store_true",
                    help="skip the pre-registered §8 replication check (say why, in writing)")
    ap.add_argument("--expect-tol", type=float, default=0.002,
                    help="absolute tolerance on each pre-registered cell")
    args = ap.parse_args()

    ks = tuple(int(x) for x in args.ks.split(","))
    corpus = Path(args.corpus)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    want = [s.strip() for s in args.scorers.split(",") if s.strip()]

    import torch
    torch.set_num_threads(args.threads)

    from evaluation.corpus import Positive
    from evaluation.retrieval import (balanced_query_sample, check_partners_present,
                                      embed_names, ranks_from_scores)

    manifest = json.loads((corpus / "manifest.json").read_text())
    report: dict = {
        "corpus": str(corpus),
        "corpus_built_at": manifest.get("built_at"),
        "corpus_seed": manifest.get("seed"),
        "corpus_manifest_pairs_written": manifest.get("pairs_written"),
        "seed": args.seed, "queries_per_pair": args.queries_per_pair,
        "ks": list(ks), "threads": args.threads,
    }

    hay = _load_jsonl(corpus / "haystack.jsonl")
    hay_names = [d["name"] for d in hay]
    hay_langs = [d.get("lang") or "und" for d in hay]
    positives = [Positive(**d) for d in _load_jsonl(corpus / "positives.jsonl")]
    del hay
    print(f"[curve] haystack {len(hay_names):,}  positives {len(positives):,}", flush=True)

    rng = random.Random(args.seed)
    queries = balanced_query_sample(positives, args.queries_per_pair, rng)
    check_partners_present(queries, set(hay_names))
    print(f"[curve] queries {len(queries):,}", flush=True)

    # A corpus or a sample that is not §8's makes every comparison below a
    # comparison with something else. Say so here rather than in the analysis.
    report["sample_matches_section_8"] = (len(queries) == EXPECTED_QUERIES
                                          and len(hay_names) == EXPECTED_HAYSTACK)
    report["n_queries"], report["n_haystack"] = len(queries), len(hay_names)
    if not report["sample_matches_section_8"]:
        print(f"[curve] ⚠ sample differs from §8 "
              f"(queries {len(queries)} vs {EXPECTED_QUERIES}, "
              f"haystack {len(hay_names)} vs {EXPECTED_HAYSTACK})", flush=True)

    # Which haystack row is "the" partner — identical rule to run_benchmark, so
    # the two are measuring the same target and not two different ones.
    by_name_lang, first = {}, {}
    for i, (n, l) in enumerate(zip(hay_names, hay_langs)):
        by_name_lang.setdefault((n, l), i)
        first.setdefault(n, i)
    target = np.asarray([by_name_lang.get((q.partner, q.partner_lang), first[q.partner])
                         for q in queries])

    all_ranks: dict[str, list[int]] = {}

    if "symphonym_v7" in want:
        sys.path.insert(0, str(Path(args.model_dir).resolve()))
        from processing.device import resolve_device
        from inference import SymphonymModel
        device = resolve_device(args.device, purpose="rank-curve")
        report["device"] = str(device)
        model = SymphonymModel(model_dir=Path(args.model_dir), device=device)

        if args.vectors and Path(args.vectors).exists():
            V = np.load(args.vectors)
            print(f"[curve] loaded haystack vectors {V.shape}", flush=True)
        else:
            t0 = time.time()
            V = embed_names(model, hay_names, hay_langs)
            dt = time.time() - t0
            print(f"[curve] embedded haystack in {dt:.0f}s "
                  f"({len(hay_names)/max(dt,1e-9):,.0f} names/s)", flush=True)
        if args.vectors_out and not Path(args.vectors_out).exists():
            np.save(args.vectors_out, V)
            print(f"[curve] haystack vectors → {args.vectors_out}", flush=True)
        Q = embed_names(model, [q.query for q in queries], [q.query_lang for q in queries])

        t0 = time.time()
        r: list[int] = []
        for i in range(0, len(Q), 256):          # 256 x 1.05M float32 = 1.1 GB
            sims = Q[i:i + 256] @ V.T
            r.extend(ranks_from_scores(sims, target[i:i + 256], pool=None))
        all_ranks["symphonym_v7"] = r
        print(f"[curve] symphonym_v7 ranked in {time.time() - t0:.0f}s", flush=True)
        del V

    string_scorers = []
    if "levenshtein_romanised" in want or "jaro_winkler_romanised" in want:
        from anyascii import anyascii
        from rapidfuzz import process
        from rapidfuzz.distance import JaroWinkler, Levenshtein
        rom_hay = [anyascii(n).strip().lower() for n in hay_names]
        rom_q = [anyascii(q.query).strip().lower() for q in queries]
        if "levenshtein_romanised" in want:
            string_scorers.append(("levenshtein_romanised", Levenshtein.normalized_similarity,
                                   rom_q, rom_hay))
        if "jaro_winkler_romanised" in want:
            string_scorers.append(("jaro_winkler_romanised", JaroWinkler.similarity,
                                   rom_q, rom_hay))
    for name, scorer, qn, hn in string_scorers:
        t0 = time.time()
        r = []
        for i in range(0, len(qn), 256):
            sims = process.cdist(qn[i:i + 256], hn, scorer=scorer,
                                 workers=-1, dtype=np.float32)
            r.extend(ranks_from_scores(sims, target[i:i + 256], pool=None))
        all_ranks[name] = r
        print(f"[curve] {name} ranked in {time.time() - t0:.0f}s", flush=True)

    # ---- durable per-query ranks: the artefact §8 did not keep ---------------
    with (out_dir / "ranks.jsonl").open("w", encoding="utf-8") as fh:
        for i, q in enumerate(queries):
            fh.write(json.dumps({
                "place_id": q.place_id, "namespace": q.namespace,
                "query": q.query, "query_lang": q.query_lang,
                "query_script": q.query_script,
                "partner": q.partner, "partner_lang": q.partner_lang,
                "partner_script": q.partner_script,
                "script_pair": "→".join(q.script_pair),
                "stratum": stratum_of(q.query_script, q.partner_script),
                "ranks": {s: all_ranks[s][i] for s in all_ranks},
            }, ensure_ascii=False) + "\n")
    print(f"[curve] per-query ranks → {out_dir / 'ranks.jsonl'}", flush=True)

    # ---- curves -------------------------------------------------------------
    res: dict = {}
    for s, r in all_ranks.items():
        by_pair, by_strat = defaultdict(list), defaultdict(list)
        for q, rank in zip(queries, r):
            by_pair["→".join(q.script_pair)].append(rank)
            by_strat[stratum_of(q.query_script, q.partner_script)].append(rank)
        res[s] = {"overall": curve(r, ks),
                  "by_stratum": curves_by(by_strat, ks),
                  "by_script_pair": curves_by(by_pair, ks)}
    report["curves"] = res

    # ---- the pre-registered check ------------------------------------------
    checks = []
    if not args.no_expect:
        for s, cells in EXPECTED_V8_SECTION_8.items():
            if s not in res:
                continue
            for cell, expected in cells.items():
                got = res[s]["overall"].get(cell)
                ok = got is not None and abs(got - expected) <= args.expect_tol
                checks.append({"scorer": s, "cell": cell, "expected": expected,
                               "got": got, "ok": ok})
        report["replication_check"] = {
            "tolerance": args.expect_tol, "cells": checks,
            "passed": all(c["ok"] for c in checks), "n_cells": len(checks)}
        for c in checks:
            got = "absent" if c["got"] is None else f"{c['got']:.4f}"
            print(f"[curve] {'ok  ' if c['ok'] else 'FAIL'} {c['scorer']:24s} "
                  f"{c['cell']:12s} expected {c['expected']:.4f} got {got}", flush=True)

    out = out_dir / "rank_curve.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"[curve] → {out}", flush=True)

    if checks and not all(c["ok"] for c in checks):
        bad = [f"{c['scorer']}/{c['cell']} expected {c['expected']} got {c['got']}"
               for c in checks if not c["ok"]]
        raise ReplicationFailure(
            f"{len(bad)} of {len(checks)} pre-registered §8 cells did not reproduce "
            f"within {args.expect_tol}: " + "; ".join(bad) +
            ". The curve is written but the k values it adds cannot be read as "
            "refining §8, because this run and §8 are not measuring the same thing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
