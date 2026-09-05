"""Run gates 2 and 3 over a built corpus: discrimination, then retrieval.

    python -m evaluation.run_benchmark --corpus DIR --vectors haystack_v7.npy \
        [--queries-per-pair 40] [--out report.json]

Every scorer meets the SAME queries and the SAME haystack. Symphonym could
afford far more queries than edit distance can — one matmul against a million
vectors versus a million string comparisons — and spending that headroom would
produce two numbers that are not comparable. The query budget is therefore set
by the slowest scorer and printed with the result.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import numpy as np


def _load_jsonl(path: Path):
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--vectors", help="pre-computed haystack vectors (.npy)")
    ap.add_argument("--model-dir", default="hf")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--queries-per-pair", type=int, default=40,
                    help="query balancing: at most this many per script pair")
    ap.add_argument("--pool", type=int, default=200, help="rank pool; the gateway's k")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out")
    ap.add_argument("--skip-baselines", action="store_true")
    args = ap.parse_args()

    from evaluation.baselines import BASELINES
    from evaluation.corpus import Positive
    from evaluation.discrimination import Pair, evaluate_pairs
    from evaluation.retrieval import (
        balanced_query_sample, by_script_pair, check_partners_present,
        embed_names, rank_by_embedding, rank_by_string, recall_at_k)

    corpus = Path(args.corpus)
    rng = random.Random(args.seed)
    report: dict = {"corpus": str(corpus), "seed": args.seed}

    # ---------------- gate 2: discrimination -------------------------------
    pairs = [Pair(**d) for d in _load_jsonl(corpus / "pairs.jsonl")]
    print(f"[bench] discrimination over {len(pairs):,} pairs "
          f"({sum(p.label for p in pairs):,} positive)", flush=True)

    sys.path.insert(0, str(Path(args.model_dir).resolve()))
    from processing.device import resolve_device
    from inference import SymphonymModel
    device = resolve_device(args.device, purpose="benchmark")
    model = SymphonymModel(model_dir=Path(args.model_dir), device=device)

    # One embedding pass over every distinct name in the pair set, then cosine
    # by lookup. Embedding per pair would embed each query once per candidate.
    distinct = sorted({(p.query, p.query_lang) for p in pairs}
                      | {(p.candidate, p.candidate_lang) for p in pairs})
    P = embed_names(model, [n for n, _ in distinct], [l for _, l in distinct])
    pos = {nl: i for i, nl in enumerate(distinct)}

    def symphonym_pair(p: Pair) -> float:
        return float(P[pos[(p.query, p.query_lang)]] @ P[pos[(p.candidate, p.candidate_lang)]])

    disc = {}
    scorers = {"symphonym_v7": symphonym_pair}
    if not args.skip_baselines:
        scorers |= {k: (lambda p, f=f: f(p.query, p.candidate))
                    for k, f in BASELINES.items()}
    for name, fn in scorers.items():
        t0 = time.time()
        res = evaluate_pairs(pairs, fn, name)
        print(f"[bench] {res.line()}   ({time.time() - t0:.0f}s)", flush=True)
        disc[name] = json.loads(res.to_json())
    report["discrimination"] = disc

    # ---------------- gate 3: retrieval ------------------------------------
    hay = _load_jsonl(corpus / "haystack.jsonl")
    hay_names = [d["name"] for d in hay]
    hay_langs = [d.get("lang") or "und" for d in hay]
    positives = [Positive(**d) for d in _load_jsonl(corpus / "positives.jsonl")]

    queries = balanced_query_sample(positives, args.queries_per_pair, rng)
    print(f"[bench] retrieval: {len(queries):,} queries (<= {args.queries_per_pair} "
          f"per script pair) against {len(hay_names):,} haystack names", flush=True)
    check_partners_present(queries, set(hay_names))

    first = {}
    for i, n in enumerate(hay_names):
        first.setdefault(n, i)
    target = np.asarray([first[q.partner] for q in queries])

    V = (np.load(args.vectors) if args.vectors
         else embed_names(model, hay_names, hay_langs))
    Q = embed_names(model, [q.query for q in queries], [q.query_lang for q in queries])

    t0 = time.time()
    ranks = rank_by_embedding(Q, V, target, pool=args.pool)
    print(f"[bench] symphonym_v7 retrieval in {time.time() - t0:.0f}s", flush=True)
    retr = {"symphonym_v7": {"overall": recall_at_k(ranks),
                             "by_script_pair": by_script_pair(queries, ranks)}}

    if not args.skip_baselines:
        from anyascii import anyascii
        from rapidfuzz.distance import JaroWinkler, Levenshtein
        rom_hay = [anyascii(n).strip().lower() for n in hay_names]
        rom_q = [anyascii(q.query).strip().lower() for q in queries]
        for name, scorer, qn, hn in (
                ("levenshtein_raw", Levenshtein.normalized_similarity,
                 [q.query for q in queries], hay_names),
                ("levenshtein_romanised", Levenshtein.normalized_similarity,
                 rom_q, rom_hay),
                ("jaro_winkler_romanised", JaroWinkler.similarity, rom_q, rom_hay)):
            t0 = time.time()
            r = rank_by_string(qn, hn, target, scorer, pool=args.pool)
            print(f"[bench] {name} retrieval in {time.time() - t0:.0f}s", flush=True)
            retr[name] = {"overall": recall_at_k(r),
                          "by_script_pair": by_script_pair(queries, r)}

    report["retrieval"] = retr | {"_queries": len(queries),
                                  "_haystack": len(hay_names),
                                  "_pool": args.pool}
    out = Path(args.out) if args.out else corpus / "benchmark.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"[bench] → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
