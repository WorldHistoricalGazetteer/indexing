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

🛑 DEPLOYMENT TRAP, PAID FOR ONCE. On CRC the job runs with
`PYTHONPATH=/vast/ishi/ipa-v8/code:/vast/ishi/elastic`, and the first entry holds
a PARTIAL `phonetics` package -- `ipa/`, `utils/`, `training/` and no
`extraction/`. Python resolves `phonetics` in the first entry that has it and
never looks further, so `from phonetics.extraction...` raises ModuleNotFoundError
even though the module is plainly present in the second entry. Three jobs died in
one second each. Fixed by symlinking `code/phonetics/extraction` ->
`elastic/phonetics/extraction`, verified byte-identical to this repo's copy
(sha256 756877ab...), so the derivation benchmarked here IS the shipped one.

⚠ That symlink makes the import path a HYBRID of two trees, and they are not
identical: `phonetics/utils/script_detection.py` differs across repo, code dir
and deployed tree -- three versions. It does not affect this module, because
`to_features` goes straight to PanPhon and never touches script detection. State
that rather than rely on it: anything here that starts calling `to_ipa` inherits
the ambiguity.

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
    #
    # ⚠ `USING SAMPLE n ROWS` RETURNS FEWER THAN n HERE, AND THE REASON MATTERS.
    # Asking for 200,000 returned 136,241. DuckDB binds the sample to the table
    # SCAN, not to the filtered result, so it draws n rows from all 73,479,069
    # and the WHERE clause then removes those without ipa:
    #
    #     200,000 x (50,221,897 / 73,479,069) = 136,697 predicted
    #                                           136,241 observed  (-0.33%,
    #                                           ~2 sigma on a binomial of 208)
    #
    # ✅ The consequence is benign and must be stated rather than assumed: a
    # uniform sample of the whole table, filtered to a subset, is still a
    # UNIFORM sample OF THAT SUBSET. The rate and yield figures stand. What does
    # NOT stand is the sample SIZE — report the count actually measured on, not
    # the one the SQL asks for.
    #
    # ✅ CONFIRMED by an independent synthetic reproduction (session 04): the
    # sample is applied BEFORE the WHERE in the same SELECT, so this is "a
    # sample of the table, filtered" and not "a sample of the filtered table".
    # The fix is one subquery -- FILTER FIRST, THEN SAMPLE:
    #     SELECT * FROM (SELECT ... WHERE pred) USING SAMPLE n ROWS  -- exact n
    # Row sampling is exact; PERCENT sampling is not. And the same shape bites
    # far harder elsewhere: `SELECT DISTINCT x FROM t USING SAMPLE 200000` gives
    # the distinct values OF A 200,000-ROW SAMPLE, silently.
    #
    # ⚠ WHAT THE WRONG FRAME DOES AND DOES NOT MOVE. A RATE is unaffected --
    # yield is a ratio within whatever rows were drawn, so 99.9993% stands.
    # Anything CONDITIONED ON THE PREDICATE carries the wrong frame, and the
    # mean blob size below is in that category: it is a mean over
    # sampled-then-filtered rows, not over sampled-from-filtered rows. Harmless
    # only if blob length does not correlate with ipa-presence, which is likely
    # and is not verified.
    #
    # ⚠ A RESIDUAL OF 456 ROWS IS UNEXPLAINED, AND IT IS ALSO n=1. Measured
    # selectivity 0.683486 predicts 136,697 from 200,000; observed 136,241,
    # short by 456 = -2.19 sigma on a binomial sd of 208. I proposed that the
    # table's ORDERING plus per-morsel reservoir sampling caused it.
    #
    # 🛑 THAT HYPOTHESIS WAS TESTED AND FAILED. Session 04 ran seven
    # arrangements at matched selectivity, ten trials each: front-loaded came
    # out at +2.22 sigma -- the OPPOSITE SIGN -- and no condition reproduced a
    # deficit of this size. If per-morsel sampling were position-biased in the
    # way the story needs, front-loading is exactly where it should show.
    #
    # 🛑 AND THE MORE BASIC ERROR WAS MINE: ONE DRAW IS NOT A RESIDUAL. 04's
    # uniform condition varies by +/-183 between draws, so a single observation
    # 456 low is about two and a half draws from nothing. I computed a sigma
    # against a THEORETICAL binomial and then reasoned as if I had measured a
    # systematic effect. The width of the distribution was never measured.
    #
    # Anyone picking this up: take ten draws on the real table and look at their
    # sd BEFORE anything more elaborate. It likely dissolves the question.
    # Nothing here depends on it -- but still do not reuse a sample drawn this
    # way for a per-script or per-region breakdown.
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

    # 🛑 POSITIVE CONTROL BEFORE MEASURING ANYTHING. An import that resolves is
    # not a derivation that works: this module reaches across a hybrid import
    # path into another tree, and the failure modes there are a missing symbol
    # (loud) and a DIFFERENT implementation of the same name (silent). A rate
    # measured through a wrong `to_features` is a rate for the wrong thing.
    #
    # `word_fts` on a two-segment IPA string must give exactly 2 x 24 float32 =
    # 192 bytes, the layout `_embedding_from_packed_features` unpacks by. If the
    # segment count or the feature width ever moves, every blob this writes is
    # unreadable by the consumer and nothing downstream would say so.
    probe = features_for_ipa(conv, "pa")
    if probe is None or len(probe) % (FEATURES_PER_SEGMENT * 4) != 0:
        raise SystemExit(
            f"to_features control FAILED: /pa/ gave {probe!r}. Expected a blob "
            f"of N x {FEATURES_PER_SEGMENT} float32. Refusing to benchmark a "
            f"derivation that does not agree with the consumer's layout."
        )
    print(f"control: /pa/ -> {len(probe)} bytes "
          f"({len(probe) // 4 // FEATURES_PER_SEGMENT} segments) — layout ok",
          flush=True)
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



BUCKET_EXPR = "abs(hash(toponym_id)) % {n}"


def run_shard(inventory_db: str, out_dir: str, buckets: int, bucket: int,
              temp_dir: str, max_temp: str) -> int:
    """One hash bucket of the inventory -> one Parquet shard. Reads READ-ONLY.

    The bucket expression is shared with `merge` verbatim (BUCKET_EXPR): the
    merge joins bucket k's rows against bucket k's shard, so a divergence
    between the two would not error -- it would join the wrong rows and leave
    the rest NULL. One constant, used twice.
    """
    import duckdb
    from phonetics.extraction.rebuild_toponyms_index import IPAConverter

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"features.b{bucket:04d}.parquet"

    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{inventory_db}' AS inv (READ_ONLY)")
    except Exception as exc:
        raise SystemExit(f"cannot attach {inventory_db!r} read-only: {exc}") from exc
    try:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        con.execute(f"PRAGMA temp_directory='{temp_dir}'")
        con.execute(f"PRAGMA max_temp_directory_size='{max_temp}'")
    except Exception as exc:
        logger_warn(f"could not set DuckDB spill limits: {exc}")

    expr = BUCKET_EXPR.format(n=buckets)
    rows = con.execute(f"""
        SELECT toponym_id, ipa FROM inv.toponyms
        WHERE ipa IS NOT NULL AND ipa <> '' AND {expr} = {bucket}
    """).fetchall()
    con.close()
    n_in = len(rows)
    if n_in == 0:
        # 🛑 An empty bucket is possible only if the hash is degenerate or the
        # bucket index is out of range. Both are bugs, and both would otherwise
        # produce a valid empty shard that the merge accepts.
        raise SystemExit(f"bucket {bucket}/{buckets} selected 0 rows — refusing "
                         f"to write an empty shard that would look complete")

    conv = IPAConverter()
    probe = features_for_ipa(conv, "pa")
    if probe is None or len(probe) % (FEATURES_PER_SEGMENT * 4) != 0:
        raise SystemExit(f"to_features control FAILED: /pa/ gave {probe!r}")

    ids, blobs = [], []
    for tid, ipa in rows:
        b = features_for_ipa(conv, ipa)
        if b is not None:
            ids.append(tid)
            blobs.append(b)

    import pyarrow as pa_
    import pyarrow.parquet as pq_
    tbl = pa_.table({"toponym_id": pa_.array(ids, pa_.string()),
                     "panphon_features": pa_.array(blobs, pa_.binary())})
    tmp = target.with_suffix(".parquet.tmp")
    pq_.write_table(tbl, tmp, compression="zstd")
    tmp.replace(target)                      # atomic: no half-written shard
    print(f"bucket {bucket}/{buckets}: {n_in:,} in -> {len(ids):,} features "
          f"({n_in - len(ids):,} unsegmentable) -> {target}", flush=True)
    return 0


def logger_warn(msg: str) -> None:
    print(f"WARNING: {msg}", flush=True)



def merge(inventory_db: str, shard_glob: str, buckets: int, work_db: str,
          temp_dir: str, max_temp: str, verify_copy: bool, execute: bool) -> int:
    """COPY the inventory, rebuild `toponyms` on the COPY, verify, leave it.

    🛑 THE ORIGINAL IS NEVER OPENED FOR WRITING. That is the whole design. The
    untouched tables — `observed_chars` PK(char,script), `script_stats`
    PK(script), the two 122.5M-row link tables with their NOT NULLs — keep every
    guarantee BY NEVER BEING TOUCHED, which is stronger than reproducing their
    DDL correctly. `cp` cannot silently drop a constraint; a hand-written
    CREATE TABLE can, and a silent schema downgrade is invisible to every count
    check we have.

    ⚠ The only table whose DDL this writes is `toponyms`, and it is the one with
    NO constraints at all — asserted below, not assumed. If that ever stops
    being true this refuses rather than quietly dropping something.

    THIS DOES NOT SWAP. It produces a verified candidate beside the original and
    stops; the rename is a separate, announced step. A merge that also swaps has
    no point at which a human can look at the result.
    """
    import shutil
    import duckdb

    src = Path(inventory_db)
    dst = Path(work_db)
    if not src.is_file():
        raise SystemExit(f"source inventory {src} is not a file")
    if dst.exists():
        raise SystemExit(f"{dst} already exists — refusing to overwrite a "
                         f"candidate that may be someone's in-progress work")
    if not execute:
        print(f"DRY RUN. Would copy {src} -> {dst} "
              f"({src.stat().st_size / 1e9:,.1f} GB) and rebuild toponyms.")
        print("Re-run with --execute.")
        return 0

    # --- 1. copy, then prove the copy ---------------------------------------
    t0 = time.perf_counter()
    print(f"copying {src.stat().st_size / 1e9:,.1f} GB -> {dst} …", flush=True)
    shutil.copy2(src, dst)
    print(f"copied in {time.perf_counter() - t0:,.0f}s", flush=True)
    if src.stat().st_size != dst.stat().st_size:
        raise SystemExit("copy size mismatch — refusing to continue")
    if verify_copy:
        import hashlib

        def sha(p: Path) -> str:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 24), b""):
                    h.update(chunk)
            return h.hexdigest()

        t0 = time.perf_counter()
        a_, b_ = sha(src), sha(dst)
        print(f"sha256 {a_[:16]}… both sides, {time.perf_counter() - t0:,.0f}s",
              flush=True)
        if a_ != b_:
            raise SystemExit(f"COPY IS NOT IDENTICAL: {a_} vs {b_}")

    # --- 2. rebuild toponyms on the COPY ------------------------------------
    con = duckdb.connect(str(dst))
    try:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        con.execute(f"PRAGMA temp_directory='{temp_dir}'")
        con.execute(f"PRAGMA max_temp_directory_size='{max_temp}'")
    except Exception as exc:
        logger_warn(f"could not set DuckDB spill limits: {exc}")

    before = con.execute("""
        SELECT count(*), count(panphon_features) FROM toponyms
    """).fetchone()
    n_constraints = con.execute("""
        SELECT count(*) FROM duckdb_constraints() WHERE table_name = 'toponyms'
    """).fetchone()[0]
    if n_constraints:
        raise SystemExit(
            f"`toponyms` declares {n_constraints} constraint(s). This module's "
            f"premise is that it declares none, and rebuilding the table would "
            f"drop them. Refusing — extend the DDL below first."
        )
    idx_sql = [r[0] for r in con.execute("""
        SELECT sql FROM duckdb_indexes() WHERE table_name = 'toponyms'
    """).fetchall()]
    # 🛑 CAPTURE THE EXPECTATION FROM THE SOURCE, DO NOT HARDCODE IT. Today the
    # file holds 7 indexes and 9 constraints; a hardcoded pair silently stops
    # describing the schema the moment anyone legitimately adds one, and then
    # either blocks a correct merge or — worse, if it were a >= test — passes a
    # lossy one. The only trustworthy expectation is the file's own state one
    # statement ago.
    idx_before, con_before = con.execute("""
        SELECT (SELECT count(*) FROM duckdb_indexes()),
               (SELECT count(*) FROM duckdb_constraints())
    """).fetchone()
    print(f"toponyms before: {before[0]:,} rows, "
          f"{before[1]:,} with features · {len(idx_sql)} indexes", flush=True)

    cols = con.execute("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name = 'toponyms' ORDER BY ordinal_position
    """).fetchall()
    ddl_cols = ", ".join(f'"{c}" {t}' for c, t in cols)
    names = [c for c, _ in cols]
    if "panphon_features" not in names:
        raise SystemExit("no panphon_features column — wrong table?")

    con.execute(f"CREATE TABLE toponyms_new ({ddl_cols})")
    select_cols = ", ".join(
        "f.panphon_features" if c == "panphon_features" else f't."{c}"'
        for c in names)

    # ⚠ BUCKETED INSERT, NOT ONE JOIN. The shard side is ~67 GB of blobs; a
    # single hash join builds that in memory and spills onto a shared volume.
    # Bucket k of the table joins ONLY shard k, so each build side is ~1/N.
    expr = BUCKET_EXPR.format(n=buckets)
    total_from_shards = 0
    for k in range(buckets):
        shard = shard_glob.replace("*", f"b{k:04d}")
        if not Path(shard).is_file():
            raise SystemExit(f"shard {shard} missing — refusing a partial merge")
        n = con.execute(f"""
            INSERT INTO toponyms_new
            SELECT {select_cols}
            FROM toponyms t
            LEFT JOIN read_parquet('{shard}') f USING (toponym_id)
            WHERE {expr} = {k}
        """).fetchall()
        got = con.execute(f"SELECT count(*) FROM read_parquet('{shard}')").fetchone()[0]
        total_from_shards += got
        print(f"  bucket {k + 1}/{buckets}: shard has {got:,} features", flush=True)

    after = con.execute("""
        SELECT count(*), count(panphon_features) FROM toponyms_new
    """).fetchone()
    print(f"toponyms_new: {after[0]:,} rows, {after[1]:,} with features")

    # --- 3. verification that can actually FAIL on what changed --------------
    problems = []
    if after[0] != before[0]:
        problems.append(f"row count {before[0]:,} -> {after[0]:,}")
    if after[1] != total_from_shards:
        problems.append(f"features {after[1]:,} != shard total {total_from_shards:,}")
    if problems:
        raise SystemExit("VERIFICATION FAILED: " + "; ".join(problems) +
                         " — toponyms_new left in place, original untouched")

    con.execute("DROP TABLE toponyms")
    con.execute("ALTER TABLE toponyms_new RENAME TO toponyms")
    for sql in idx_sql:
        print(f"  recreating: {sql}", flush=True)
        con.execute(sql)

    # 🛑 A CTAS drops indexes silently, so count them back rather than trust the
    # loop above. And count constraints across the WHOLE file: the untouched
    # tables should still have all 9, and if they do not, the copy is wrong.
    n_idx, n_con = con.execute("""
        SELECT (SELECT count(*) FROM duckdb_indexes()),
               (SELECT count(*) FROM duckdb_constraints())
    """).fetchone()
    con.execute("CHECKPOINT")
    con.close()
    print(f"\nindexes {n_idx} (source had {idx_before}) · "
          f"constraints {n_con} (source had {con_before})")
    if (n_idx, n_con) != (idx_before, con_before):
        raise SystemExit(
            f"SCHEMA CHANGED: indexes {idx_before} -> {n_idx}, constraints "
            f"{con_before} -> {n_con}. DO NOT SWAP THIS FILE. The rows may be "
            f"perfect and the guarantees are not — which no count check sees."
        )
    print(f"\n✅ candidate ready: {dst}")
    print(f"   original UNTOUCHED: {src}")
    print("🛑 NOT SWAPPED. The rename is a separate announced step.")
    print("⚠ REPLACED AND EXTENDED, not restored: different values on the "
          "34.1M overlap, plus rows that never had the field.")
    return 0


def inspect(inventory_db: str, out: Optional[str]) -> int:
    """Report everything a CTAS + swap must reproduce — and what it cannot.

    🛑 A CTAS LOSES WHAT IT DOES NOT SELECT. `CREATE TABLE t AS SELECT ...`
    reproduces columns and values and silently drops PRIMARY KEY, NOT NULL,
    UNIQUE and every index. The rewritten inventory would hold the right rows
    with fewer guarantees, and nothing downstream would notice until a duplicate
    or a null arrived months later. So the merge must recreate them explicitly —
    which means knowing them, not assuming them.

    ⚠ This exists because writing the merge against a GUESSED schema is the same
    fault as the rest of this campaign: reasoning correctly about a shape the
    store does not have. Run it, read it, then write the merge.
    """
    import duckdb

    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{inventory_db}' AS inv (READ_ONLY)")
    except Exception as exc:
        raise SystemExit(f"cannot attach {inventory_db!r} read-only: {exc}") from exc

    rep: dict = {"database": inventory_db}

    tables = con.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_catalog = 'inv' AND table_schema = 'main'
        ORDER BY table_name
    """).fetchall()
    rep["tables"] = {}
    print(f"tables in {inventory_db}:")
    for (t,) in tables:
        n = con.execute(f'SELECT count(*) FROM inv.main."{t}"').fetchone()[0]
        cols = con.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_catalog='inv' AND table_schema='main' AND table_name=?
            ORDER BY ordinal_position
        """, [t]).fetchall()
        rep["tables"][t] = {"rows": n,
                            "columns": [{"name": c, "type": d, "nullable": nl}
                                        for c, d, nl in cols]}
        print(f"  {t:<24} {n:>14,} rows, {len(cols)} columns")
        for c, d, nl in cols:
            print(f"      {c:<24} {d:<28} {'NULL' if nl == 'YES' else 'NOT NULL'}")

    # The DDL DuckDB itself would emit — the only faithful source for the parts
    # information_schema does not carry.
    for view, key in (("duckdb_tables()", "table_sql"),
                      ("duckdb_indexes()", "indexes"),
                      ("duckdb_constraints()", "constraints")):
        try:
            rows = con.execute(f"SELECT * FROM {view}").fetchdf()
            rows = rows[rows.get("database_name", "inv") == "inv"] if "database_name" in rows else rows
            rep[key] = json.loads(rows.to_json(orient="records"))
            print(f"\n--- {view} ---")
            print(rows.to_string(max_colwidth=110))
        except Exception as exc:
            rep[key] = f"UNAVAILABLE: {exc}"
            print(f"\n--- {view} --- UNAVAILABLE: {exc}")

    # The column the repopulation touches, stated as it is now.
    cur = con.execute("""
        SELECT count(*) AS rows,
               count(ipa) FILTER (ipa <> '') AS with_ipa,
               count(panphon_features) AS with_features
        FROM inv.toponyms
    """).fetchone()
    rep["toponyms_now"] = {"rows": cur[0], "with_ipa": cur[1],
                           "with_features": cur[2]}
    print(f"\ntoponyms: {cur[0]:,} rows · ipa {cur[1]:,} · panphon_features {cur[2]:,}")
    print("⚠ with_features is the POST-BACKFILL figure. The run replaces and "
          "extends it; it does not restore the 34,141,080.")
    con.close()

    if out:
        Path(out).write_text(json.dumps(rep, indent=2, default=str))
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
    i = sub.add_parser("inspect", help="what a CTAS + swap must reproduce")
    i.add_argument("--inventory-db", required=True)
    i.add_argument("--out")
    r = sub.add_parser("run", help="one hash bucket -> one Parquet shard")
    r.add_argument("--inventory-db", required=True)
    r.add_argument("--out-dir", required=True)
    r.add_argument("--buckets", type=int, required=True)
    r.add_argument("--bucket", type=int, required=True)
    r.add_argument("--temp-dir", default="/ix1/ishi/tmp/ipa-duckdb")
    r.add_argument("--max-temp", default="32GB")
    m = sub.add_parser("merge", help="copy the inventory, rebuild toponyms on the copy")
    m.add_argument("--inventory-db", required=True)
    m.add_argument("--shard-glob", required=True,
                   help="path with a literal * where the bucket token goes")
    m.add_argument("--buckets", type=int, required=True)
    m.add_argument("--work-db", required=True, help="the CANDIDATE path (must not exist)")
    m.add_argument("--temp-dir", default="/ix1/ishi/tmp/ipa-duckdb")
    m.add_argument("--max-temp", default="32GB")
    m.add_argument("--no-verify-copy", action="store_true",
                   help="skip the sha256 of both 121GB files (not recommended)")
    m.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    if a.cmd == "benchmark":
        return benchmark(a.inventory_db, a.sample, a.seed, a.out)
    if a.cmd == "inspect":
        return inspect(a.inventory_db, a.out)
    if a.cmd == "run":
        return run_shard(a.inventory_db, a.out_dir, a.buckets, a.bucket,
                         a.temp_dir, a.max_temp)
    if a.cmd == "merge":
        return merge(a.inventory_db, a.shard_glob, a.buckets, a.work_db,
                     a.temp_dir, a.max_temp, not a.no_verify_copy, a.execute)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
