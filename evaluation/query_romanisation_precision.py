#!/usr/bin/env python3
"""
The precision cost of query-side romanisation — the half the scoping pass owed.

⚠ The scoping run (d33acaa) measured RECALL over positives only. A recall-only
figure is not decidable: any widening of a matcher raises recall, and the
question is what it admits alongside. This runs the same rule over the corpus's
NEGATIVES, which are script-pair and length-band matched to the positives
(`check_negative_matching`, tolerance 0.05), so a per-stratum comparison is
fair rather than a comparison of differently-shaped sets.

SCORED AS THE GATEWAY SCORES, so the numbers mean what the production tiers
mean: an exact lexical hit earns LEXICAL_EXACT_BOOST (2.5), a near hit earns up
to LEXICAL_FUZZY_BOOST (0.75) above LEXICAL_FUZZY_FLOOR (0.5).

🛑 REPORTED PER STRATUM AND NEVER AVERAGED. The change buys 25.73% on one
stratum and 2.26% on another; if it costs precision unevenly, a corpus-wide
mean would hide a stratum where it is a net loss. A change that helps one
stratum and harms another is a per-stratum decision, not a yes/no.

🛑 AND DIACRITIC FOLDS ARE SPLIT OUT. All 896 gains in the near-identity control
were macron/accent folds (`Fāshān`->`fashan`). Those are a far narrower widening
than full transliteration, so they may be precision-safe — and therefore
shippable — even if full query romanisation is not. Folding them into one
number would forfeit that option.
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

LEXICAL_EXACT_BOOST = 2.5
LEXICAL_FUZZY_BOOST = 0.75
LEXICAL_FUZZY_FLOOR = 0.5


def norm(s: str) -> str:
    return " ".join(s.lower().split())


def diacritic_fold(s: str) -> str:
    """Accent/macron stripping ONLY — no transliteration. The narrow widening."""
    d = unicodedata.normalize("NFD", s)
    return "".join(c for c in d if not unicodedata.combining(c)).lower().strip()


def lexical_score(q: str, cands: list[str]) -> float:
    """The gateway's tier shape: exact earns the exact boost, a near miss earns
    up to the fuzzy boost scaled by how much it really resembles."""
    best = 0.0
    for c in cands:
        if not c:
            continue
        if norm(q) == norm(c):
            return LEXICAL_EXACT_BOOST
        r = SequenceMatcher(None, norm(q), norm(c)).ratio()
        if r >= LEXICAL_FUZZY_FLOOR:
            best = max(best, LEXICAL_FUZZY_BOOST * r)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--threshold", type=float, default=LEXICAL_FUZZY_BOOST * 0.85,
                    help="score at or above which a pair counts as MATCHED")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from anyascii import anyascii

    rows = []
    with open(Path(a.corpus) / "pairs.jsonl", encoding="utf-8") as fh:
        for line in fh:
            rows.append(json.loads(line))

    S = defaultdict(lambda: defaultdict(int))
    for d in rows:
        q, c = d["query"], d["candidate"]
        qs, cs = d["query_script"], d["candidate_script"]
        lab = int(d["label"])
        key = ("latin_q_nonlatin_c" if qs == "LATIN" and cs != "LATIN" else
               "nonlatin_q_latin_c" if qs != "LATIN" and cs == "LATIN" else
               "both_latin" if qs == "LATIN" else "both_nonlatin")
        s = S[key]
        s["pos" if lab else "neg"] += 1

        cands = [c]
        if cs != "LATIN":
            r = anyascii(c).lower().strip()
            if r and r != c.lower():
                cands.append(r)

        base = lexical_score(q, cands)
        full = max(base, lexical_score(anyascii(q).lower().strip(), cands))
        dia = max(base, lexical_score(diacritic_fold(q), cands))

        for name, sc in (("base", base), ("full", full), ("dia", dia)):
            if sc >= a.threshold:
                s[f"{name}_{'tp' if lab else 'fp'}"] += 1

    def pr(s, k):
        tp, fp = s.get(f"{k}_tp", 0), s.get(f"{k}_fp", 0)
        prec = tp / (tp + fp) if (tp + fp) else None
        rec = tp / s["pos"] if s["pos"] else None
        return tp, fp, prec, rec

    rep = {"threshold": a.threshold, "strata": {}}
    print(f"{'stratum':<22}{'variant':<7}{'TP':>7}{'FP':>7}{'prec':>8}{'recall':>8}"
          f"{'d-prec':>9}{'d-recall':>10}")
    for k, s in sorted(S.items(), key=lambda kv: -(kv[1]["pos"] + kv[1]["neg"])):
        out = {"n_pos": s["pos"], "n_neg": s["neg"]}
        b = pr(s, "base")
        for variant, label in (("base", "base"), ("full", "full-rom"), ("dia", "diacritic")):
            tp, fp, prec, rec = pr(s, variant)
            dp = (prec - b[2]) if (prec is not None and b[2] is not None) else None
            dr = (rec - b[3]) if (rec is not None and b[3] is not None) else None
            out[variant] = {"tp": tp, "fp": fp,
                            "precision": round(prec, 4) if prec is not None else None,
                            "recall": round(rec, 4) if rec is not None else None,
                            "d_precision": round(dp, 4) if dp is not None else None,
                            "d_recall": round(dr, 4) if dr is not None else None}
            print(f"{k if variant=='base' else '':<22}{label:<7}{tp:>7,}{fp:>7,}"
                  f"{(f'{prec:.4f}' if prec is not None else '--'):>8}"
                  f"{(f'{rec:.4f}' if rec is not None else '--'):>8}"
                  f"{(f'{dp:+.4f}' if dp else '     —'):>9}"
                  f"{(f'{dr:+.4f}' if dr else '     —'):>10}")
        rep["strata"][k] = out
        print()

    Path(a.out).write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
