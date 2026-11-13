"""Paired comparison of two saved runs on the SAME items: who gained, who lost, and by how much.

    python -m evaluation.nearmiss.compare dev_prod.json dev_p1.json [--cond unscoped]

A rate difference on independent samples hides what matters for a tuning change: the per-item flips.
For each stratum this reports hit@10 of A and B, the number of items GAINED (miss -> hit) and LOST
(hit -> miss), and an exact two-sided sign-test p-value on the discordant pairs. Run on ``dev``.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from evaluation.nearmiss import metrics as M
from evaluation.nearmiss.run_eval import DATA, load


def sign_test_p(gained: int, lost: int) -> float:
    """Exact two-sided binomial sign test on the discordant pairs (p = 0.5 under 'no change')."""
    n = gained + lost
    if n == 0:
        return 1.0
    k = min(gained, lost)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def _hit(item, hits, k=10) -> bool | None:
    if hits is None:
        return None
    r = M.rank_of([tuple(h) for h in hits], item.get("truth_cc") or item["truth_pids"])
    return r is not None and r < k


def compare(a: dict, b: dict, items: list[dict], cond: str, k: int = 10) -> dict:
    """Overall and per-family flips between runs ``a`` and ``b`` (saved --out dicts)."""
    ia = {s["id"]: s[cond] for s in a["items"]}
    ib = {s["id"]: s[cond] for s in b["items"]}
    rows = []
    for it in items:
        if it["kind"] not in ("misspelling", "control") or it["collides"] or it["targeted"] or it["id"] not in ia or it["id"] not in ib:
            continue
        ha, hb = _hit(it, ia[it["id"]], k), _hit(it, ib[it["id"]], k)
        if ha is None or hb is None:
            continue
        rows.append((it, ha, hb))

    def summ(sel):
        n = len(sel)
        ga = sum(1 for _, x, y in sel if not x and y)
        lo = sum(1 for _, x, y in sel if x and not y)
        return {"n": n, "A": round(sum(x for _, x, _ in sel) / n, 3) if n else None,
                "B": round(sum(y for _, _, y in sel) / n, 3) if n else None, "gained": ga, "lost": lo,
                "p": round(sign_test_p(ga, lo), 4)}

    out = {"ALL": summ(rows)}
    for fam in ("first_two_letters", "error_class", "edit_distance", "length"):
        buckets: dict[str, list] = {}
        for r in rows:
            labs = M.STANDARD_KEYS[fam](r[0])
            for lab in (labs if isinstance(labs, (list, tuple)) else [labs]):
                buckets.setdefault(str(lab), []).append(r)
        out[fam] = {lab: summ(v) for lab, v in sorted(buckets.items()) if len(v) >= 15}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a"); ap.add_argument("b")
    ap.add_argument("--cond", default="unscoped", choices=["unscoped", "scoped"])
    ap.add_argument("--data", default=str(DATA))
    args = ap.parse_args()
    A, B = (json.loads(Path(x).read_text()) for x in (args.a, args.b))
    items = load(A["meta"]["split"], Path(args.data), controls=A["meta"].get("controls", 0))
    res = compare(A, B, items, args.cond)
    print(json.dumps({"A": A["meta"]["variant"], "B": B["meta"]["variant"], "cond": args.cond}))
    for fam, v in res.items():
        if fam == "ALL":
            print(f"ALL  {v}")
        else:
            print(f"-- {fam}")
            for lab, s in v.items():
                print(f"   {lab:12} {s}")


if __name__ == "__main__":
    main()
