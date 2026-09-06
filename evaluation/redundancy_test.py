#!/usr/bin/env python3
"""
Does near-duplicate redundancy explain the index's low PanPhon rank?

THE HYPOTHESIS, and a hole in it that has to be checked first
-------------------------------------------------------------
Proposed: the corpus-wide PanPhon rank (3.12) is low partly because the index
carries huge near-duplicate redundancy, while MEHDIE (7.25) is curated distinct
places. Lower diversity concentrates variance into fewer directions.

⚠ But the participation ratio is INVARIANT TO UNIFORM REPLICATION. Duplicating
every vector k times scales the Gram matrix by k, scales every eigenvalue by k,
and leaves the normalised spectrum -- and therefore the participation ratio --
exactly unchanged. So "there are duplicates" cannot by itself be the mechanism.
If redundancy matters at all it must be NON-UNIFORM: some phonetic shapes
over-represented relative to others.

That distinction is the whole test, and C1 below checks the invariance
empirically rather than trusting the algebra.

THE CONFOUND THAT MUST BE HELD
------------------------------
MEHDIE is 55% Arabic / 44% Hebrew; the index sample is ~80% Latin. Comparing
them directly confounds redundancy with script. So the real comparison is
WITHIN script: index-Arabic vs MEHDIE-Arabic, index-Hebrew vs MEHDIE-Hebrew,
each deduplicated and at matched n.

PREDICTIONS, registered before running:
  If redundancy explains the gap -> deduplicating the index sample raises its
    rank substantially, toward MEHDIE's, and the within-script gap closes.
  If it does not -> the index stays near its raw value after dedup, the gap
    persists, and the low rank is a property of what the index CONTAINS rather
    than of how often it repeats it.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, "/vast/ishi/ipa-v8/code")
sys.path.insert(0, "/vast/ishi/elastic")

from evaluation.geometry import measure_geometry          # noqa: E402
from evaluation.panphon_rank import panphon_vectors, load_ipa, spectrum  # noqa: E402

TESTSETS = Path("/vast/ishi/ipa-v8/mehdie-testsets")


def rank_of(V: np.ndarray, knn_k: int = 200) -> float:
    k = min(knn_k, max(5, len(V) // 4))
    if len(V) <= k + 1:
        return float("nan")
    return measure_geometry(V, knn_k=k).effective_rank


def near_dedup(V: np.ndarray, decimals: int = 1) -> np.ndarray:
    """Collapse vectors identical to `decimals` places — a coarse but explicit
    stand-in for 'phonetically near-identical'."""
    if len(V) == 0:
        return V
    _, idx = np.unique(np.round(V, decimals), axis=0, return_index=True)
    return V[np.sort(idx)]


def mehdie_titles():
    seen, titles = set(), []
    for f in sorted(TESTSETS.glob("*/*.tsv")):
        if f.name == "em.tsv" or f.name in seen:
            continue
        seen.add(f.name)
        with open(f, encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                t = (row.get("title") or "").strip()
                if t:
                    titles.append(t)
    return titles


def mehdie_ipa_by_script(workers):
    from phonetics.utils.script_detection import detect_script
    from phonetics.extraction.rebuild_toponyms_index import IPAConverter
    conv = IPAConverter()
    out = {}
    raw = mehdie_titles()
    for name in sorted(set(raw)):
        try:
            sc, _ = detect_script(name)
        except Exception:
            continue
        lang = {"HEBREW": "he", "ARABIC": "ar", "LATIN": "en"}.get(sc.name)
        if not lang:
            continue
        try:
            ipa = conv.to_ipa(name, lang, sc)
        except Exception:
            ipa = None
        if ipa:
            out.setdefault(sc.name, []).append(ipa)
    return out, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--index-n", type=int, default=300_000)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rep = {"controls": {}, "duplication": [], "within_script": []}

    # ---------- CONTROLS ----------
    print("== CONTROLS ==")
    rng = np.random.default_rng(0)

    # C0: isotropic Gaussian must give participation ratio ~= dimension. If it
    # does not, the estimator is wrong and nothing below is interpretable.
    G = rng.normal(size=(20000, 192)).astype(np.float32)
    c0 = rank_of(G)
    print(f"  C0 isotropic gaussian (expect ~192)          : {c0:8.2f}")
    rep["controls"]["gaussian_192"] = c0

    # C1: THE LOAD-BEARING ONE. Uniform replication must not change the
    # participation ratio. If it does, the hypothesis could be about plain
    # duplicates after all -- and the algebra above would be wrong.
    base = panphon_vectors(load_ipa(20000)[0], a.workers)
    r_base = rank_of(base)
    tripled = np.repeat(base, 3, axis=0)
    r_trip = rank_of(tripled)
    print(f"  C1 index sample n={len(base):,}                  : {r_base:8.4f}")
    print(f"  C1 same sample, every row x3 (expect identical): {r_trip:8.4f}"
          f"   delta={r_trip - r_base:+.6f}")
    rep["controls"]["replication_invariance"] = {
        "base": r_base, "tripled": r_trip, "delta": r_trip - r_base}

    # ---------- HOW REDUNDANT IS EACH CORPUS? ----------
    print("\n== duplication rates (is the premise even true?) ==")
    idx_ipa, _ = load_ipa(a.index_n)
    m_by_script, m_raw = mehdie_ipa_by_script(a.workers)
    m_all = [x for v in m_by_script.values() for x in v]
    for label, lst in [("index", idx_ipa), ("mehdie", m_all)]:
        c = Counter(lst)
        dup_rows = len(lst) - len(c)
        top = c.most_common(3)
        d = {"label": label, "n": len(lst), "distinct": len(c),
             "duplicate_rows": dup_rows,
             "duplicate_pct": round(100.0 * dup_rows / len(lst), 3) if lst else None,
             "most_common": [[k, v] for k, v in top]}
        rep["duplication"].append(d)
        print(f"  {label:<8} n={len(lst):>8,} distinct={len(c):>8,} "
              f"dup={d['duplicate_pct']}%  top={top[:2]}")

    # ---------- THE TEST, WITHIN SCRIPT ----------
    print("\n== rank before/after dedup, WITHIN script (confound held) ==")
    print(f"  {'stratum':<22}{'n_raw':>9}{'raw':>9}{'n_exact':>9}{'exact':>9}"
          f"{'n_near':>9}{'near':>9}")
    for script in ("ARABIC", "HEBREW"):
        for src in ("index", "mehdie"):
            if src == "index":
                ipas, _ = load_ipa(a.index_n, script=script)
            else:
                ipas = m_by_script.get(script, [])
            if len(ipas) < 500:
                print(f"  {src}/{script}: {len(ipas)} rows, skipping")
                continue
            V = panphon_vectors(ipas, a.workers)
            r_raw = rank_of(V)
            Ve = panphon_vectors(sorted(set(ipas)), a.workers)
            r_exact = rank_of(Ve)
            Vn = near_dedup(Ve, decimals=1)
            r_near = rank_of(Vn)
            row = {"source": src, "script": script,
                   "n_raw": len(V), "rank_raw": r_raw,
                   "n_exact_dedup": len(Ve), "rank_exact_dedup": r_exact,
                   "n_near_dedup": int(len(Vn)), "rank_near_dedup": r_near,
                   "spectrum_raw": spectrum(V)}
            rep["within_script"].append(row)
            print(f"  {src+'/'+script:<22}{len(V):>9,}{r_raw:>9.3f}"
                  f"{len(Ve):>9,}{r_exact:>9.3f}{len(Vn):>9,}{r_near:>9.3f}")

    Path(a.out).write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    print("\n->", a.out)


if __name__ == "__main__":
    main()
