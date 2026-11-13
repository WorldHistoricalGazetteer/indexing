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


def load(split: str, path: Path = DATA, controls: int = 0) -> list[dict]:
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if split == "all" or r["split"] == split]
    if controls:
        # REGRESSION GUARD: the same places with their CORRECT spelling. A change that helps mis-spellings
        # must not cost exact queries. Generated, not stored, so the frozen set is unchanged.
        base = [r for r in rows if r["kind"] == "misspelling" and not r["targeted"] and not r["collides"]][:controls]
        rows = rows + [{**r, "id": "c" + r["id"][1:], "kind": "control", "query": r["original"], "classes": ["none"],
                        "edit_distance": 0} for r in base]
    return rows


# -- variants. Production is the baseline; every variant is a reversible in-process patch, nothing is deployed. ----
def _clause(prefix_length=2, fuzziness="AUTO"):
    """The production near-miss text clause with its two fuzzy parameters exposed."""
    def build(query: str, mode: str = "fuzzy") -> dict:
        from gateway import es_helpers as eh
        if mode != "fuzzy":
            return eh.build_toponym_text_clause(query, mode)
        return {"bool": {"should": [
            {"multi_match": {"query": query, "fields": ["name^3", "name_romanized^2"], "type": "best_fields",
                             "fuzziness": fuzziness, "prefix_length": prefix_length}},
            {"match": {"name.prefix": {"query": query}}},
            {"term": {"name.raw": {"value": query.lower(), "boost": 5}}}]}}
    return build


VARIANT_SPECS = {
    "prod": {},
    "ngram_fuzzy_old": {"ngram_old": True},
    "boost100": {"boost": 1.0},
    "boost125": {"boost": 1.25},
    "boost140": {"boost": 1.4},
    "boost125_floor60": {"boost": 1.25, "floor": 0.6},
    "p1": {"prefix_length": 1},
    "p0": {"prefix_length": 0},
    "p1_boost125": {"prefix_length": 1, "boost": 1.25},
    "p0_boost125": {"prefix_length": 0, "boost": 1.25},
    "p1_boost125_floor60": {"prefix_length": 1, "boost": 1.25, "floor": 0.6},
    "p1_boost115": {"prefix_length": 1, "boost": 1.15},
    "boost110": {"boost": 1.1}, "boost115": {"boost": 1.15}, "boost135": {"boost": 1.35},
    "boost125_floor40": {"boost": 1.25, "floor": 0.4},
    "boost125_floor70": {"boost": 1.25, "floor": 0.7},
}


def apply_variant(name: str):
    """Patch the live module for ``name``; return an undo function."""
    from gateway import es_helpers as eh
    spec = VARIANT_SPECS[name]
    saved = {k: getattr(eh, k) for k in ("LEXICAL_FUZZY_BOOST", "LEXICAL_FUZZY_FLOOR", "NEAR_MISS_FUZZY_NGRAM", "build_near_miss_clause")}
    if "boost" in spec:
        eh.LEXICAL_FUZZY_BOOST = spec["boost"]
    if "floor" in spec:
        eh.LEXICAL_FUZZY_FLOOR = spec["floor"]
    if spec.get("ngram_old"):
        eh.NEAR_MISS_FUZZY_NGRAM = True
    if "prefix_length" in spec or "fuzziness" in spec:
        eh.build_near_miss_clause = _clause(spec.get("prefix_length", 2), spec.get("fuzziness", "AUTO"))

    def undo():
        for k, v in saved.items():
            setattr(eh, k, v)
    return undo


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
    blocks = {"main": lambda i: i["kind"] != "control" and (i["kind"] == "nonsense" or (not i["targeted"] and not i["collides"])),
              "control": lambda i: i["kind"] == "control",
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
    ap.add_argument("--controls", type=int, default=0, help="also run N correctly-spelt controls (regression guard)")
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--out", default="")
    ap.add_argument("--rescore", default="", help="recompute the report from a saved --out file; no Elasticsearch")
    a = ap.parse_args()
    if a.rescore:
        saved = json.loads(Path(a.rescore).read_text())
        items = load(saved["meta"]["split"], Path(a.data), controls=saved["meta"].get("controls", 0))[: saved["meta"]["items"]]
        by_id = {s["id"]: s for s in saved["items"]}
        unsc = [[tuple(h) for h in by_id[i["id"]]["unscoped"]] if by_id[i["id"]]["unscoped"] is not None else None for i in items]
        scop = [[tuple(h) for h in by_id[i["id"]]["scoped"]] if by_id[i["id"]]["scoped"] is not None else None for i in items]
        saved["report"] = report(items, unsc, scop)
        Path(a.rescore).write_text(json.dumps(saved))
        print("rescored", a.rescore)
        return
    from gateway.threads import cap_cpu_threads
    cap_cpu_threads()
    items = load(a.split, Path(a.data), controls=a.controls)
    if a.limit:
        items = items[: a.limit]
    undo = apply_variant(a.variant)
    t0 = time.time()
    unscoped, scoped, lat, errors = asyncio.run(run(items, a.concurrency))
    undo()
    rep = report(items, unscoped, scoped)
    lat.sort()
    meta = {"variant": a.variant, "split": a.split, "controls": a.controls, "items": len(items), "errors": errors, "seconds": round(time.time() - t0),
            "latency_ms": {"median": round(statistics.median(lat)), "p95": round(lat[int(.95 * len(lat)) - 1])} if lat else {}}
    print(json.dumps(meta))
    for cond in ("unscoped", "scoped"):
        for blk in ("main", "control"):
            if blk in rep[cond]:
                print(summary_line(f"{cond}/{blk}", rep[cond][blk]["ALL"]))
    if a.out:
        per_item = [{"id": it["id"], "unscoped": u, "scoped": s} for it, u, s in zip(items, unscoped, scoped)]
        Path(a.out).write_text(json.dumps({"meta": meta, "report": rep, "items": per_item}))


if __name__ == "__main__":
    main()
