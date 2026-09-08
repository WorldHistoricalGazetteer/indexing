#!/usr/bin/env python3
"""
Repopulate `panphon_features` from the CORRECTED `ipa` — replaced and extended.

🛑 THIS IS NOT A REPAIR AND MUST NEVER BE REPORTED AS ONE. The backfill took
`panphon_features` from 34,141,080 to 424,520, and `update_es index` derives the
shipped `panphon_embedding` FROM that column, so the next index build would
write the field on ~1.2% of documents and pass every automated check. Recomputing
does not restore what was there:

  * +~16 M rows that NEVER had the field, because `ipa` coverage rose from
    34.1 M to 50.2 M;
  * DIFFERENT VALUES on the overlap, because the IPA itself changed — which is
    the entire point of having recomputed it.

Anyone diffing the promoted 34.1 M against the next generation's ~50.2 M will
otherwise conclude something broke. **Replaced and extended, never restored.**

ONE IMPLEMENTATION, NOT A REIMPLEMENTATION. The derivation is
`IPAConverter.to_features` from `rebuild_toponyms_index` — the same call the
original pipeline made — packed the same way (`struct.pack('<N>f')`, N×24
float32, 24 articulatory features per segment). A second copy here would be a
second thing to keep in step with the consumer, and the consumer
(`_embedding_from_packed_features`) unpacks by that exact layout.

⚠ BENCHMARK AND RUN SHARE THE CODE PATH ON PURPOSE. A rate measured by a
lookalike loop is a rate for the lookalike. `features_for_ipa` is called by both,
so the benchmark's throughput is the throughput that will actually happen.

⚠ AND ROWS WITH `ipa` WILL NOT ALL YIELD FEATURES. `word_fts` returns nothing
for an IPA string PanPhon cannot segment, so the post-run count is <= the `ipa`
count, not equal to it. The benchmark measures that fraction rather than
assuming it, because assuming it is how a shortfall gets read as a defect later.
"""

from __future__ import annotations

import argparse
import json
import struct
import time
from pathlib import Path
from typing import Optional

FEATURES_PER_SEGMENT = 24


def features_for_ipa(converter, ipa: str) -> Optional[bytes]:
    """IPA -> the packed N x 24 float32 blob, exactly as the pipeline stores it.

    Returns None when PanPhon cannot segment the string; the caller must treat
    that as a legitimate outcome, not an error.
    """
    feats = converter.to_features(ipa)
    if not feats:
        return None
    if len(feats) % FEATURES_PER_SEGMENT != 0:
        # 🛑 The consumer unpacks strictly by this layout and returns None on a
        # ragged blob, so a ragged blob is a silent field loss downstream, not a
        # tolerable oddity. Refuse to write one.
        return None
    return struct.pack(f"{len(feats)}f", *feats)


def benchmark(inventory_db: str, sample: int, seed: int, out: Optional[str]) -> int:
    import duckdb
    from phonetics.extraction.rebuild_toponyms_index import IPAConverter

    # ⚠ READ-ONLY. A read-write attach to a missing path CREATES an empty
    # database, and a benchmark over an empty sample reports a magnificent rate.
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{inventory_db}' AS inv (READ_ONLY)")
    except Exception as exc:
        raise SystemExit(f"cannot attach {inventory_db!r} read-only: {exc}") from exc

    total_ipa = con.execute(
        "SELECT count(*) FROM inv.toponyms WHERE ipa IS NOT NULL AND ipa <> ''"
    ).fetchone()[0]
    if total_ipa == 0:
        raise SystemExit("0 rows carry ipa — refusing to benchmark nothing")

    # A UNIFORM sample of the real population: word_fts cost scales with IPA
    # length, and length varies by script, so a head/tail slice of an ordered
    # table would measure one script's rate and call it the corpus rate.
    rows = con.execute(f"""
        SELECT ipa FROM inv.toponyms
        WHERE ipa IS NOT NULL AND ipa <> ''
        USING SAMPLE {sample} ROWS (reservoir, {seed})
    """).fetchall()
    con.close()
    ipas = [r[0] for r in rows]
    print(f"inventory rows with ipa : {total_ipa:,}")
    print(f"uniform sample          : {len(ipas):,}", flush=True)

    conv = IPAConverter()
    conv.to_features("a")                       # pay the FeatureTable load once
    t0 = time.perf_counter()
    ok = ragged_or_empty = 0
    nbytes = 0
    for ipa in ipas:
        blob = features_for_ipa(conv, ipa)
        if blob is None:
            ragged_or_empty += 1
        else:
            ok += 1
            nbytes += len(blob)
    dt = time.perf_counter() - t0

    rate = len(ipas) / dt
    mean_blob = nbytes / ok if ok else 0.0
    yield_frac = ok / len(ipas)
    proj_rows = total_ipa * yield_frac
    proj_bytes = total_ipa * yield_frac * mean_blob
    core_hours = (total_ipa / rate) / 3600.0

    print(f"\nelapsed {dt:.1f}s  ->  {rate:,.0f} rows/s on ONE core")
    print(f"features produced       : {ok:,}  ({yield_frac:.4%} of sampled ipa rows)")
    print(f"no features             : {ragged_or_empty:,}  (PanPhon could not segment)")
    print(f"mean blob               : {mean_blob:,.0f} bytes")
    print(f"\nPROJECTED over {total_ipa:,} ipa rows:")
    print(f"  panphon_features rows : {proj_rows:,.0f}   "
          f"(NOT {total_ipa:,} — the shortfall is PanPhon, not a defect)")
    print(f"  blob storage          : {proj_bytes / 1e9:,.1f} GB")
    print(f"  single-core time      : {core_hours:,.1f} core-hours")
    for c in (16, 32, 64):
        print(f"    on {c:>2} cores         : {core_hours / c:,.2f} h wall (perfect scaling)")
    print("\n⚠ Wall-clock figures assume perfect scaling and exclude the read of "
          "the ipa column and the write of the shards. Treat them as a floor.")
    print("🛑 This is REPLACED AND EXTENDED, not restored: different values on the "
          "34.1 M overlap because the IPA changed, plus rows that never had the field.")

    rep = {"total_ipa_rows": total_ipa, "sampled": len(ipas),
           "rows_per_sec_single_core": round(rate, 1),
           "yield_fraction": round(yield_frac, 6),
           "mean_blob_bytes": round(mean_blob, 1),
           "projected_feature_rows": int(proj_rows),
           "projected_blob_gb": round(proj_bytes / 1e9, 2),
           "single_core_hours": round(core_hours, 2)}
    if out:
        Path(out).write_text(json.dumps(rep, indent=2))
        print(f"\n-> {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("benchmark", help="measure the rate on the real code path")
    b.add_argument("--inventory-db", required=True)
    b.add_argument("--sample", type=int, default=200_000)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--out")
    a = ap.parse_args()
    if a.cmd == "benchmark":
        return benchmark(a.inventory_db, a.sample, a.seed, a.out)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
