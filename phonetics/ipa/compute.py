#!/usr/bin/env python3
"""
Worker: compute one shard's IPA, write one Parquet file, touch nothing else.

Every input row produces exactly one output row. A name that fails, returns
empty, or comes back identical to its input still gets a row -- with a status
saying which. Dropping them would make the shard's row count a lie and leave
the next run unable to tell "attempted and hopeless" from "never attempted".

An output identical to the input is recorded as echoed_input rather than ok: a
mode that passes text through produces a plausible string that would train
something on nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CHARSIU_BATCH = 64


def _load_shard_rows(work_dir: Path, lang: str, script: str,
                     part: int, n_parts: int) -> List[tuple]:
    """Read one cell's rows out of the partitioned work set.

    DuckDB PERCENT-ENCODES partition directory names, so 'lang=1510/' is
    written as 'lang=1510%2F'. Formatting the path naively finds nothing, and
    finding nothing looks exactly like an empty cell rather than an error --
    431 lang values in this corpus need the encoding.
    """
    import duckdb
    from phonetics.ipa.routes import partition_path_component
    part_dir = (work_dir / partition_path_component("lang", lang or "")
                / partition_path_component("script", script))
    if not part_dir.exists():
        return []
    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT toponym_id, name, name_sha FROM read_parquet('{part_dir}/*.parquet')
        ORDER BY toponym_id
    """).fetchall()
    if n_parts > 1:
        rows = [r for i, r in enumerate(rows) if i % n_parts == part]
    return rows


def compute_shard(plan_path: Path, shard_id: str, out_dir: Path) -> Dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    plan = json.loads(Path(plan_path).read_text())
    shard = next((s for s in plan["shards"] if s["shard_id"] == shard_id), None)
    if shard is None:
        raise SystemExit(f"shard_id not in plan: {shard_id}")
    now = datetime.now(timezone.utc)
    work_dir = Path(plan["work_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{shard_id}.parquet"

    lang, script = shard["lang"], shard["script"]
    rows = _load_shard_rows(work_dir, lang, script,
                            shard.get("part", 0), shard.get("n_parts", 1))

    # A cell the plan says has rows must not silently read back as empty. That
    # is what a path-encoding mistake looks like from here, and an empty shard
    # merges cleanly while quietly dropping a whole language.
    if shard.get("rows", 0) > 0 and not rows:
        raise SystemExit(
            f"shard {shard_id}: plan expects {shard['rows']:,} rows for "
            f"lang={lang!r} script={script!r} but the work partition read back "
            f"empty -- check the partition path encoding, do not merge this run"
        )

    recs = {k: [] for k in ("toponym_id", "name_sha", "lang", "script", "ipa",
                            "backend", "mode", "status", "error", "run_id",
                            "computed_at")}

    def emit(tid, sha, ipa, status, error=None):
        recs["toponym_id"].append(tid)
        recs["name_sha"].append(sha)
        recs["lang"].append(lang or None)
        recs["script"].append(script)
        recs["ipa"].append(ipa)
        recs["backend"].append(shard["backend"])
        recs["mode"].append(shard["mode"])
        recs["status"].append(status)
        recs["error"].append(error)
        recs["run_id"].append(plan["run_id"])
        recs["computed_at"].append(now)

    t0 = time.time()
    if shard["terminal"]:
        # No backend can handle this cell. Record the verdict for every row so
        # the next run does not reopen it.
        for tid, name, sha in rows:
            emit(tid, sha, None, shard["status"])
    else:
        backend, mode = shard["backend"], shard["mode"]
        from phonetics.ipa import backends as B
        if backend == "epitran":
            try:
                eng = B.EpitranBackend(mode)
            except Exception as e:
                for tid, name, sha in rows:
                    emit(tid, sha, None, "failed", f"mode load: {type(e).__name__}: {e}"[:200])
                eng = None
            if eng is not None:
                for tid, name, sha in rows:
                    try:
                        out = eng.transliterate(name)
                    except Exception as e:
                        emit(tid, sha, None, "failed", f"{type(e).__name__}: {e}"[:200]); continue
                    if not out:
                        emit(tid, sha, None, "empty_output")
                    elif out == name:
                        emit(tid, sha, out, "echoed_input")
                    else:
                        emit(tid, sha, out, "ok")
        elif backend == "charsiu":
            try:
                eng = B.CharsiuBackend()
            except Exception as e:
                for tid, name, sha in rows:
                    emit(tid, sha, None, "failed", f"model load: {type(e).__name__}: {e}"[:200])
                eng = None
            if eng is not None:
                for i in range(0, len(rows), CHARSIU_BATCH):
                    chunk = rows[i:i + CHARSIU_BATCH]
                    names = [r[1] for r in chunk]
                    try:
                        outs = eng.transliterate_batch(names, mode)
                    except Exception as e:
                        for tid, name, sha in chunk:
                            emit(tid, sha, None, "failed", f"{type(e).__name__}: {e}"[:200])
                        continue
                    for (tid, name, sha), out in zip(chunk, outs):
                        if not out:
                            emit(tid, sha, None, "empty_output")
                        elif out == name:
                            emit(tid, sha, out, "echoed_input")
                        else:
                            emit(tid, sha, out, "ok")
        elif backend == "phonikud":
            try:
                eng = B.PhonikudBackend()
            except Exception as e:
                for tid, name, sha in rows:
                    emit(tid, sha, None, "failed", f"load: {type(e).__name__}: {e}"[:200])
                eng = None
            if eng is not None:
                for tid, name, sha in rows:
                    try:
                        out = eng.transliterate(name)
                    except Exception as e:
                        emit(tid, sha, None, "failed", f"{type(e).__name__}: {e}"[:200]); continue
                    if not out:
                        emit(tid, sha, None, "empty_output")
                    elif out == name:
                        emit(tid, sha, out, "echoed_input")
                    else:
                        emit(tid, sha, out, "ok")
        else:
            for tid, name, sha in rows:
                emit(tid, sha, None, "failed", f"unknown backend {backend}")

    schema = pa.schema([
        ("toponym_id", pa.string()), ("name_sha", pa.string()),
        ("lang", pa.string()), ("script", pa.string()), ("ipa", pa.string()),
        ("backend", pa.string()), ("mode", pa.string()),
        ("status", pa.string()), ("error", pa.string()),
        ("run_id", pa.string()), ("computed_at", pa.timestamp("us", tz="UTC")),
    ])
    pq.write_table(pa.table(recs, schema=schema), out_path, compression="zstd")

    from collections import Counter
    by_status = dict(Counter(recs["status"]))
    result = {"shard_id": shard_id, "rows_in": len(rows),
              "rows_out": len(recs["toponym_id"]), "by_status": by_status,
              "seconds": round(time.time() - t0, 1), "path": str(out_path)}
    logger.info(json.dumps(result))
    # Every input row must produce exactly one output row.
    if len(rows) != len(recs["toponym_id"]):
        raise SystemExit(f"row count mismatch: in={len(rows)} out={len(recs['toponym_id'])}")
    return result


def _select_shards(plan: dict, backends: Optional[str], terminal_only: bool,
                   compute_only: bool) -> List[dict]:
    shards = plan["shards"]
    if terminal_only:
        shards = [s for s in shards if s["terminal"]]
    if compute_only:
        shards = [s for s in shards if not s["terminal"]]
    if backends:
        want = {b.strip() for b in backends.split(",")}
        shards = [s for s in shards if s.get("backend") in want]
    return shards


def main():
    ap = argparse.ArgumentParser(description="Compute IPA shards")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--shard-id")
    ap.add_argument("--shard-index", type=int,
                    help="index into the SELECTED shard list (Slurm arrays)")
    ap.add_argument("--stride", type=int, default=1,
                    help="with --shard-index, also do every Nth shard after it. "
                         "Lets 3,539 trivial passthrough shards run as ~40 "
                         "array tasks instead of 3,539.")
    ap.add_argument("--backends", help="comma-separated filter, e.g. 'epitran'")
    ap.add_argument("--terminal-only", action="store_true")
    ap.add_argument("--compute-only", action="store_true")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--skip-existing", action="store_true",
                    help="leave shards whose Parquet already exists; makes a "
                         "requeued or re-submitted array cheap to resume")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    plan = json.loads(Path(a.plan).read_text())
    out_dir = Path(a.out_dir)

    if a.shard_id:
        todo = [a.shard_id]
    else:
        if a.shard_index is None:
            raise SystemExit("need --shard-id or --shard-index")
        sel = _select_shards(plan, a.backends, a.terminal_only, a.compute_only)
        todo = [s["shard_id"] for s in sel[a.shard_index::max(1, a.stride)]]

    results, skipped = [], 0
    for sid in todo:
        if a.skip_existing and (out_dir / f"{sid}.parquet").exists():
            skipped += 1
            continue
        results.append(compute_shard(Path(a.plan), sid, out_dir))
    summary = {
        "shards_selected": len(todo), "shards_computed": len(results),
        "shards_skipped_existing": skipped,
        "rows_out": sum(r["rows_out"] for r in results),
        "seconds": round(sum(r["seconds"] for r in results), 1),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
