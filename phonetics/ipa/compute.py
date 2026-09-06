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
    import duckdb
    part_dir = work_dir / f"lang={lang or ''}" / f"script={script}"
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


def main():
    ap = argparse.ArgumentParser(description="Compute one IPA shard")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--shard-id")
    ap.add_argument("--shard-index", type=int,
                    help="index into the plan's shard list (for Slurm arrays)")
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    shard_id = a.shard_id
    if shard_id is None:
        if a.shard_index is None:
            raise SystemExit("need --shard-id or --shard-index")
        plan = json.loads(Path(a.plan).read_text())
        shard_id = plan["shards"][a.shard_index]["shard_id"]
    r = compute_shard(Path(a.plan), shard_id, Path(a.out_dir))
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
