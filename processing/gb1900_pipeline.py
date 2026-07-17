#!/usr/bin/env python
"""GB1900 STAMP edition — concurrent cropper (runs on the pitt VM, NOT a login node).

While the national NLS tile fetch runs (a separate, polite, single fetch on pitt),
this loop overlaps the crop stage with it: it repeatedly finds residual pins whose
covering tiles are ALL already cached on /vast, crops them (marker crops), and
writes fixed-size **batch manifests** (batch_NNNN.jsonl) that the VLM step consumes.

Runs entirely on pitt (long processes OK there). It never submits Slurm — VLM
batches are submitted separately by one-shot `sbatch` at checkpoints (never a
standing driver on a login node). Resumable via a processed-state file.

  python -m processing.gb1900_pipeline \
      --residual /vast/ishi/gb1900/edition/national_residual.jsonl \
      --batch-dir /vast/ishi/gb1900/edition/batches \
      --crops /vast/ishi/gb1900/crops/national \
      --fetch-log /vast/ishi/gb1900/edition/national_fetch.log \
      --batch-size 15000 --zoom 16 --poll 300
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from processing.gb1900_tiles import covering_tiles, tile_path, stitch_crop


def _all_tiles_cached(lat: float, lon: float, z: int, pad: int = 1) -> bool:
    for (tz, tx, ty) in covering_tiles(lat, lon, z, pad):
        p = tile_path(tz, tx, ty)
        if not (p.exists() and p.stat().st_size > 0):
            return False
    return True


def _fetch_done(fetch_log: Path) -> bool:
    if not fetch_log.exists():
        return False
    try:
        tail = fetch_log.read_text(errors="replace")[-2000:]
    except Exception:
        return False
    return "[fetch] fetched" in tail and "already-cached" in tail


def run(args) -> None:
    residual = [json.loads(l) for l in open(args.residual, encoding="utf-8")]
    batch_dir = Path(args.batch_dir); batch_dir.mkdir(parents=True, exist_ok=True)
    crops = Path(args.crops); crops.mkdir(parents=True, exist_ok=True)
    state = batch_dir / "processed.txt"
    processed: set[str] = set()
    if state.exists():
        processed = {l.strip() for l in open(state) if l.strip()}
    # batch counter = existing batch files
    n_batch = len(list(batch_dir.glob("batch_*.jsonl")))
    fetch_log = Path(args.fetch_log) if args.fetch_log else None
    z, pad, bs = args.zoom, args.pad, args.batch_size
    print(f"[pipeline] residual={len(residual):,} already-processed={len(processed):,} "
          f"batches-so-far={n_batch}")

    def emit(pins: list[dict]) -> None:
        nonlocal n_batch
        if not pins:
            return
        n_batch += 1
        man = batch_dir / f"batch_{n_batch:04d}.jsonl"
        with open(man, "w", encoding="utf-8") as mf, open(state, "a") as sf:
            saved = 0
            for rec in pins:
                img, meta = stitch_crop(
                    rec["lat"], rec["lon"],
                    (rec.get("text", {}) or {}).get("value", "")
                    if isinstance(rec.get("text"), dict) else (rec.get("text") or ""),
                    z)
                pid = rec["pin_id"]
                sf.write(pid + "\n")
                processed.add(pid)
                if img is None:
                    continue
                cp = crops / f"gb_{pid}.png"
                img.save(cp)
                saved += 1
                mf.write(json.dumps({
                    "place_id": rec.get("place_id", f"gb:{pid}"), "pin_id": pid,
                    "text": rec.get("text"), "lon": rec["lon"], "lat": rec["lat"],
                    "crop_path": str(cp), "crop": meta}, ensure_ascii=False) + "\n")
        print(f"[pipeline] wrote {man.name}: {saved:,} crops "
              f"({len(processed):,}/{len(residual):,} residual done)")

    while True:
        ready: list[dict] = []
        remaining = 0
        for rec in residual:
            if rec["pin_id"] in processed:
                continue
            remaining += 1
            if _all_tiles_cached(rec["lat"], rec["lon"], z, pad):
                ready.append(rec)
                if len(ready) >= bs:
                    emit(ready); ready = []
        done = fetch_log is not None and _fetch_done(fetch_log)
        if ready and (done or len(ready) >= bs // 4):
            emit(ready); ready = []
        remaining = sum(1 for rec in residual if rec["pin_id"] not in processed)
        if remaining == 0:
            print("[pipeline] all residual cropped — done.")
            (batch_dir / "DONE").write_text("all cropped\n"); break
        if done:
            # fetch finished but some pins never got tiles (missing upstream) — stop.
            print(f"[pipeline] fetch done; {remaining:,} pins had no tiles (skipped).")
            (batch_dir / "DONE").write_text(f"fetch done; {remaining} unfetchable\n"); break
        print(f"[pipeline] {remaining:,} residual pending tiles; sleeping {args.poll}s")
        time.sleep(args.poll)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--residual", required=True)
    p.add_argument("--batch-dir", required=True)
    p.add_argument("--crops", required=True)
    p.add_argument("--fetch-log", help="national fetch log — used to detect fetch completion")
    p.add_argument("--batch-size", type=int, default=15000)
    p.add_argument("--zoom", type=int, default=16)
    p.add_argument("--pad", type=int, default=1)
    p.add_argument("--poll", type=int, default=300)
    run(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
