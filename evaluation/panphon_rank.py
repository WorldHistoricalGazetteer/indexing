#!/usr/bin/env python3
"""
Re-measure PanPhon192's effective rank at scale — the most load-bearing
unverified number in plan-symphonym-v8.md.

WHY IT NEEDED REDOING
---------------------
v8's finding 2 is a causal chain: PanPhon input rank 4.37 of 192 -> a teacher
fitted to it -> a student distilled to rank 10.83 of 128 -> a retrain is
justified. The STUDENT's 10.83 was measured twice, on two implementations, and
shown stable from 6k to 1.05M names. The TEACHER's 4.37 was measured once, on
3,000 toponyms, and never again -- the `panphon_features` column has been NULL
in every surviving store since. The IPA store built on 6 Sep is the first time
the input to recompute it has existed.

WHAT THIS DOES DIFFERENTLY
--------------------------
1. A CURVE, not a point. Log-spaced sizes, because the whole reason to redo it
   is that the student's rank was shown stable across scale and the teacher's
   was not. 4.37 holding at 3M confirms finding 2 on its weakest leg; 4.37
   climbing to 12 means v8's justification needs restating.

2. 🛑 A POSITIVE CONTROL FIRST, and it gates everything else. The same
   estimator is run over the v7 STUDENT embeddings, whose rank is known to be
   ~10.83 from two independent implementations. If it does not reproduce that,
   the estimator is wrong and any PanPhon number it produces is uninterpretable
   -- so this reports the control's failure and computes no PanPhon rank at all.
   A rank measurement with no control is a number with nothing behind it.

3. THE SPECTRUM, not just the scalar. "The v7 spectrum does not taper, it falls
   off a cliff" is a different claim from its participation ratio, and the
   plan's language rests on it.

4. STRATIFIED BY SCRIPT. IPA coverage is script-skewed and Latin dominates, so
   a corpus-wide figure could be an average over populations with different
   geometry -- which would be its own finding.

Uses the SHIPPED implementations throughout: `IPAConverter.to_embedding` for the
8-bin pooling (the vector positives were clustered in) and
`evaluation.geometry.measure_geometry` for the estimator that produced 10.83.
Reimplementing either would make the control meaningless.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

sys.path.insert(0, "/vast/ishi/elastic")

STORE = "/vast/ishi/ipa-v8/store/ipa.duckdb"
V7_PARQUET = "/vast/ishi/models/phonetic/data/v7/embeddings_v7.parquet"

_conv = None


def _worker_init():
    global _conv
    from phonetics.extraction.rebuild_toponyms_index import IPAConverter
    _conv = IPAConverter()
    _conv._check_panphon()


def _pool_batch(ipas: List[str]) -> List[Optional[List[float]]]:
    global _conv
    out = []
    for s in ipas:
        try:
            out.append(_conv.to_embedding(s))
        except Exception:
            out.append(None)
    return out


def panphon_vectors(ipas: List[str], workers: int) -> np.ndarray:
    """8-bin pooled 192-d vectors, via the shipped pooling."""
    from multiprocessing import Pool
    chunk = max(200, len(ipas) // (workers * 8) or 1)
    batches = [ipas[i:i + chunk] for i in range(0, len(ipas), chunk)]
    vecs = []
    with Pool(workers, initializer=_worker_init) as pool:
        for res in pool.imap(_pool_batch, batches):
            vecs.extend(v for v in res if v is not None)
    return np.asarray(vecs, dtype=np.float32)


def spectrum(V: np.ndarray) -> dict:
    """Full singular spectrum, by the same Gram route measure_geometry uses --
    eigenvalues of V.T @ V are the squared singular values exactly, and the
    D x D reduction is what keeps a 3M x 192 corpus off the LAPACK workspace."""
    V = np.asarray(V, dtype=np.float32)
    norms = np.linalg.norm(V, axis=1, keepdims=True)
    V = V / np.where(norms == 0, 1.0, norms)
    gram = (V.T @ V).astype(np.float64)
    ev = np.clip(np.linalg.eigvalsh(gram)[::-1], 0.0, None)
    s = np.sqrt(ev)
    idx = [0, 4, 9, 19, 49, 99, len(s) - 1]
    return {
        "sigma": {f"s{i+1}": float(s[i]) for i in idx if i < len(s)},
        "sigma_ratio_to_first": {
            f"s{i+1}/s1": float(s[i] / s[0]) if s[0] > 0 else None
            for i in idx if i < len(s)},
        "var_explained_top": {
            f"top{k}": float(ev[:k].sum() / ev.sum())
            for k in (1, 5, 10, 20, 50) if k <= len(ev)},
    }


def load_v7(n: int, seed: int = 0) -> np.ndarray:
    """Sample v7 student embeddings. Deterministic modulus sampling, NOT a
    LIMIT: the parquet is ordered and a head sample measures the head."""
    import duckdb
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='40GB'")
    total = con.execute(
        f"SELECT count(*) FROM read_parquet('{V7_PARQUET}')").fetchone()[0]
    k = max(1, total // max(n, 1))
    rows = con.execute(f"""
        SELECT embedding FROM read_parquet('{V7_PARQUET}')
        WHERE hash(doc_id) % {k} = 0 AND embedding IS NOT NULL
        LIMIT {n}
    """).fetchall()
    return np.asarray([r[0] for r in rows], dtype=np.float32)


def load_ipa(n: int, script: Optional[str] = None,
             non_latin: bool = False) -> tuple:
    import duckdb
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='40GB'")
    con.execute(f"ATTACH '{STORE}' AS st (READ_ONLY)")
    where = ["status = 'ok'", "ipa IS NOT NULL", "ipa <> ''"]
    if script:
        where.append(f"script = '{script}'")
    if non_latin:
        where.append("script <> 'LATIN'")
    w = " AND ".join(where)
    total = con.execute(f"SELECT count(*) FROM st.ipa WHERE {w}").fetchone()[0]
    if total == 0:
        return [], 0
    k = max(1, total // max(n, 1))
    rows = con.execute(f"""
        SELECT ipa FROM st.ipa
        WHERE {w} AND hash(toponym_id) % {k} = 0
        LIMIT {n}
    """).fetchall()
    return [r[0] for r in rows], total


def measure(V: np.ndarray, label: str, knn_k: int) -> dict:
    from evaluation.geometry import measure_geometry
    rep = measure_geometry(V, knn_k=knn_k)
    out = json.loads(rep.to_json())
    out["label"] = label
    out["spectrum"] = spectrum(V)
    print(f"  {label:<28} n={V.shape[0]:>9,} d={V.shape[1]:>4} "
          f"eff_rank={rep.effective_rank:8.3f}  "
          f"s20/s1={rep.sigma20_over_1:.6f}  var_top10={rep.var_top10:.4f}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="3000,30000,300000,3000000")
    ap.add_argument("--control-n", type=int, default=300_000)
    ap.add_argument("--workers", type=int,
                    default=int(os.environ.get("SLURM_CPUS_PER_TASK", 8)))
    ap.add_argument("--knn-k", type=int, default=200)
    ap.add_argument("--out", required=True)
    ap.add_argument("--control-only", action="store_true")
    a = ap.parse_args()

    report = {"control": None, "panphon": [], "by_script": []}

    # ---- 1. CONTROL. Everything downstream is gated on this. ----
    print("== POSITIVE CONTROL: v7 student embeddings, expect eff_rank ~10.83 of 128 ==")
    t0 = time.time()
    V7 = load_v7(a.control_n)
    print(f"  loaded {V7.shape} in {time.time()-t0:.1f}s")
    ctrl = measure(V7, "v7_student_128d", a.knn_k)
    report["control"] = ctrl
    got = ctrl["effective_rank"]
    # Two independent implementations put this at 10.83; allow generous slack
    # for the different corpus, and still fail loudly outside it.
    ok = 9.0 <= got <= 13.0
    report["control_passed"] = bool(ok)
    report["control_expected"] = "10.83 (+/- tolerance 9.0-13.0)"
    print(f"  CONTROL {'PASSED' if ok else 'FAILED'}: got {got:.3f}, expected ~10.83")
    del V7

    if not ok:
        Path(a.out).write_text(json.dumps(report, indent=2))
        print("\n🛑 CONTROL FAILED — the estimator does not reproduce a known "
              "value, so any PanPhon rank it produced would be uninterpretable. "
              "Computing none. Fix the estimator or the input, then rerun.")
        print("->", a.out)
        sys.exit(2)
    if a.control_only:
        Path(a.out).write_text(json.dumps(report, indent=2))
        print("->", a.out); return

    # ---- 2. PanPhon rank vs corpus size ----
    print("\n== PanPhon192 effective rank vs sample size (plan records 4.37 at n=3,000) ==")
    for n in [int(x) for x in a.sizes.split(",")]:
        t0 = time.time()
        ipas, avail = load_ipa(n)
        if len(ipas) <= a.knn_k:
            print(f"  n={n}: only {len(ipas)} rows available, skipping")
            continue
        V = panphon_vectors(ipas, a.workers)
        print(f"  [{n:,}] {len(ipas):,} ipa -> {V.shape} in {time.time()-t0:.1f}s "
              f"(of {avail:,} available)")
        report["panphon"].append(measure(V, f"panphon192_n{n}", a.knn_k))
        del V

    # ---- 3. Stratify by script ----
    print("\n== PanPhon192 by script (is the corpus figure an average over "
          "different geometries?) ==")
    for label, kwargs in [("LATIN", {"script": "LATIN"}),
                          ("non-LATIN", {"non_latin": True}),
                          ("CJK", {"script": "CJK"}),
                          ("CYRILLIC", {"script": "CYRILLIC"}),
                          ("ARABIC", {"script": "ARABIC"})]:
        ipas, avail = load_ipa(300_000, **kwargs)
        if len(ipas) <= a.knn_k:
            print(f"  {label}: only {len(ipas)} rows, skipping")
            continue
        V = panphon_vectors(ipas, a.workers)
        r = measure(V, f"panphon192_{label}", a.knn_k)
        r["rows_available"] = avail
        report["by_script"].append(r)
        del V

    Path(a.out).write_text(json.dumps(report, indent=2))
    print("\n->", a.out)


if __name__ == "__main__":
    main()
