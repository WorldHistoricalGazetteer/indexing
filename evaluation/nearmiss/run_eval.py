"""Run the near-miss evaluation set through the REAL reconcile pipeline, in-process. Run on the Pitt host.

    PYTHONPATH=/vast/ishi/elastic python -m evaluation.nearmiss.run_eval --split dev --variant prod --out /tmp/r.json

Read-only and light: bounded concurrency, result cache OFF (so a repeated string can never answer for a
variant). Two conditions per misspelling: ``unscoped`` (the string alone) and ``scoped`` (the item's
country as a hard filter, as a caller who has the country supplies it); nonsense is run unscoped only.

Report blocks, never pooled: ``main`` (the random sample), ``targeted`` (diacritic/abbreviation items,
small n), ``colliding`` (the misspelling happens to be a real name elsewhere: ambiguous by construction).
Use ``--split dev`` while tuning. ``--split test`` is for the end: every look at it spends it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ["GATEWAY_RESULT_CACHE_SIZE"] = "0"          # before the gateway is imported
sys.path.insert(0, "/vast/ishi/elastic")

from evaluation.nearmiss import metrics as M            # noqa: E402

DATA = Path(__file__).parent / "data" / "nearmiss_v1.jsonl"


def load(split: str, path: Path = DATA) -> list[dict]:
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return [r for r in rows if split == "all" or r["split"] == split]


# -- variants of the near-miss clause. Production is the baseline; add a variant here to compare it. ------
def _variants():
    from gateway import es_helpers as eh
    prod = eh.build_near_miss_clause

    def ngram_fuzzy_old():                     # the clause before 2026-11-12 (rollback switch)
        eh.NEAR_MISS_FUZZY_NGRAM = True
        return lambda: setattr(eh, "NEAR_MISS_FUZZY_NGRAM", False)

    def restore_prod():
        eh.build_near_miss_clause = prod
        eh.NEAR_MISS_FUZZY_NGRAM = False

    return {"prod": lambda: (restore_prod(), (lambda: None))[1], "ngram_fuzzy_old": ngram_fuzzy_old}


async def run(items: list[dict], concurrency: int) -> tuple[list[dict], list[dict], list[float], int]:
    from gateway import reconcile as rc
    sem = asyncio.Semaphore(concurrency)
    unscoped: list = [None] * len(items)
    scoped: list = [None] * len(items)
    lat: list[float] = []
    errors = 0

    async def one(i: int, it: dict, cond: str):
        nonlocal errors
        kw = {"ccodes": [it["ccode"]]} if cond == "scoped" else {}
        async with sem:
            t = time.perf_counter()
            try:
                res = await rc.reconcile_search(rc.ReconcileRequest(query=it["query"], mode="fuzzy", size=10, **kw))
                hits = [(h.place_id, float(h.confidence or 0.0)) for h in res.hits]
            except Exception as exc:                       # a failed query is an error, never a silent miss
                errors += 1
                hits = None
                print(f"ERROR {it['id']} {cond}: {type(exc).__name__}: {exc}", file=sys.stderr)
            lat.append((time.perf_counter() - t) * 1000)
        (unscoped if cond == "unscoped" else scoped)[i] = hits

    jobs = []
    for i, it in enumerate(items):
        jobs.append(one(i, it, "unscoped"))
        if it["kind"] != "nonsense" and it.get("ccode"):
            jobs.append(one(i, it, "scoped"))
    await asyncio.gather(*jobs)
    return unscoped, scoped, lat, errors


def report(items, unscoped, scoped) -> dict:
    out = {}
    blocks = {"main": lambda i: i["kind"] == "nonsense" or (not i["targeted"] and not i["collides"]),
              "targeted": lambda i: i["targeted"], "colliding": lambda i: i["collides"] and i["kind"] != "nonsense"}
    for cond, results in (("unscoped", unscoped), ("scoped", scoped)):
        out[cond] = {}
        for name, pred in blocks.items():
            sel = [(it, r) for it, r in zip(items, results) if pred(it) and r is not None]
            if not sel:
                continue
            its = [s[0] for s in sel]
            oc = [M.score_item(it, r) for it, r in sel]
            out[cond][name] = M.stratify(its, oc, M.STANDARD_KEYS if name == "main" else {"error_class": M.STANDARD_KEYS["error_class"]})
    return out


def summary_line(label, agg) -> str:
    def r(k): return f'{agg[k]["rate"]:.3f} [{agg[k]["ci95"][0]:.3f}-{agg[k]["ci95"][1]:.3f}]'
    return f'{label:12} n={agg["n"]:4}  hit@1 {r("hit@1")}  hit@3 {r("hit@3")}  hit@10 {r("hit@10")}  mrr {agg["mrr"]:.3f}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    ap.add_argument("--variant", default="prod")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--out", default="")
    ap.add_argument("--rescore", default="", help="recompute the report from a saved --out file; no Elasticsearch")
    a = ap.parse_args()
    if a.rescore:
        saved = json.loads(Path(a.rescore).read_text())
        items = load(saved["meta"]["split"], Path(a.data))[: saved["meta"]["items"]]
        by_id = {s["id"]: s for s in saved["items"]}
        unsc = [[tuple(h) for h in by_id[i["id"]]["unscoped"]] if by_id[i["id"]]["unscoped"] is not None else None for i in items]
        scop = [[tuple(h) for h in by_id[i["id"]]["scoped"]] if by_id[i["id"]]["scoped"] is not None else None for i in items]
        saved["report"] = report(items, unsc, scop)
        Path(a.rescore).write_text(json.dumps(saved))
        print("rescored", a.rescore)
        return
    from gateway.threads import cap_cpu_threads
    cap_cpu_threads()
    items = load(a.split, Path(a.data))
    if a.limit:
        items = items[: a.limit]
    undo = _variants()[a.variant]()
    t0 = time.time()
    unscoped, scoped, lat, errors = asyncio.run(run(items, a.concurrency))
    undo()
    rep = report(items, unscoped, scoped)
    lat.sort()
    meta = {"variant": a.variant, "split": a.split, "items": len(items), "errors": errors, "seconds": round(time.time() - t0),
            "latency_ms": {"median": round(statistics.median(lat)), "p95": round(lat[int(.95 * len(lat)) - 1])} if lat else {}}
    print(json.dumps(meta))
    for cond in ("unscoped", "scoped"):
        if "main" in rep[cond]:
            print(summary_line(f"{cond}/main", rep[cond]["main"]["ALL"]))
    if a.out:
        per_item = [{"id": it["id"], "unscoped": u, "scoped": s} for it, u, s in zip(items, unscoped, scoped)]
        Path(a.out).write_text(json.dumps({"meta": meta, "report": rep, "items": per_item}))


if __name__ == "__main__":
    main()
