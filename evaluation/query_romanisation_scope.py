#!/usr/bin/env python3
"""
Costed sketch: what would romanising the QUERY at search time buy?

⚠ SCOPING, NOT PROPOSING. This measures a payoff and estimates a cost so the
decision can be made with numbers. It changes nothing.

THE GAP. `name_romanized` is stored only for non-Latin names, and the gateway
romanises nothing at query time. So two strata currently have ZERO lexical
reach and neither is helped by populating the field:

    non-Latin query -> Latin candidate   the candidate has no romanised form
                                         (romanize_for_search returns None for
                                         Latin), and the raw names share no
                                         characters
    non-Latin -> non-Latin               only the candidate is romanised, so
                                         two different non-Latin scripts are
                                         never bridged

Romanising the query would address BOTH, by different routes: against a Latin
candidate it matches the RAW name; against a non-Latin candidate it matches the
STORED romanised form. Measured separately, because they are different
mechanisms with different failure modes.

🛑 EXACT AND NEAR REPORTED APART, as in 535089d. A large share of this corpus's
cross-script positives are transliteration pairs, so an exact romanised match
partly measures how the test data was built. The near figure is the defensible
one.

CONTROL: the Latin-query strata must be ~unchanged, because romanising Latin
text is near-identity. A large movement there means the measurement is picking
up something other than the capability.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path


def norm(s: str) -> str:
    return " ".join(s.lower().split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--near", type=float, default=0.85)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from anyascii import anyascii

    pairs = []
    with open(Path(a.corpus) / "pairs.jsonl", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if int(d["label"]) == 1:
                pairs.append(d)

    S = defaultdict(lambda: {"n": 0, "today": 0, "exact": 0, "near": 0})
    for d in pairs:
        q, c = d["query"], d["candidate"]
        qs, cs = d["query_script"], d["candidate_script"]
        key = ("latin_q_nonlatin_c" if qs == "LATIN" and cs != "LATIN" else
               "nonlatin_q_latin_c" if qs != "LATIN" and cs == "LATIN" else
               "both_latin" if qs == "LATIN" else "both_nonlatin")
        s = S[key]
        s["n"] += 1

        # What is reachable TODAY: raw name, plus the stored romanised form of
        # a non-Latin candidate (i.e. name_romanized once populated).
        cands = [c]
        if cs != "LATIN":
            r = anyascii(c).lower().strip()
            if r and r != c.lower():
                cands.append(r)
        if any(norm(q) == norm(x)
               or SequenceMatcher(None, norm(q), norm(x)).ratio() >= a.near
               for x in cands):
            s["today"] += 1
            continue

        # TREATMENT: romanise the query too.
        rq = anyascii(q).lower().strip()
        if not rq:
            continue
        if any(norm(rq) == norm(x) for x in cands):
            s["exact"] += 1
        elif any(SequenceMatcher(None, norm(rq), norm(x)).ratio() >= a.near
                 for x in cands):
            s["near"] += 1

    rep = {"corpus": a.corpus, "near": a.near, "strata": {}}
    print(f"{'stratum':<24}{'n':>8}{'reach today':>13}{'+exact':>9}{'+near':>8}"
          f"{'new reach':>11}")
    tot_new = tot_n = 0
    for k, s in sorted(S.items(), key=lambda kv: -kv[1]["n"]):
        new = s["exact"] + s["near"]
        s["new_reach_pct"] = round(100.0 * new / s["n"], 2) if s["n"] else None
        s["today_pct"] = round(100.0 * s["today"] / s["n"], 2) if s["n"] else None
        rep["strata"][k] = s
        tot_new += new
        tot_n += s["n"]
        print(f"{k:<24}{s['n']:>8,}{s['today']:>13,}{s['exact']:>9,}"
              f"{s['near']:>8,}{s['new_reach_pct']:>10}%")
    rep["corpus_new_reach_pct"] = round(100.0 * tot_new / tot_n, 2)
    print(f"\ncorpus-wide additional positives reached: {tot_new:,} of "
          f"{tot_n:,} = {rep['corpus_new_reach_pct']}%")

    bl = rep["strata"].get("both_latin")
    lq = rep["strata"].get("latin_q_nonlatin_c", {})
    print(f"\nCONTROL latin_q_nonlatin_c new reach {lq.get('new_reach_pct')}% "
          f"— romanising a Latin query is near-identity, so this should be ~0")
    if not bl or bl["n"] == 0:
        print("CONTROL both_latin: VACUOUS — 0 pairs in this corpus, proves nothing")
        rep["control_both_latin"] = "vacuous"

    Path(a.out).write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    print(f"\n-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
