#!/usr/bin/env python
"""Sharded GB-STAMP cropper for Slurm fan-out (parallelises the single-VM cropper).

Once the tile fetch is COMPLETE (all tiles cached on /vast), cropping is embarrassingly
parallel — each pin's crop is independent. This runs as one Slurm array task per shard:
worker k crops the pins with ``int(pin_id,16) % nshards == k`` **whose crop doesn't already
exist** (so it skips whatever the original VM cropper already did — crops persist and are
uniquely named per pin), writing per-shard batch manifests ``batch_s{k}_NNNN.jsonl`` (matches
the VLM workers' ``batch_*.jsonl`` glob; no collision with the VM cropper's ``batch_NNNN``).

Coordination-free: shards are disjoint by pin_id, dedup via crop-file existence, no shared
state. Idempotent + resumable (re-run skips existing crops).

  python -m processing.gb1900.crop_shard --pins /vast/…/national_typed.jsonl \
      --batch-dir /vast/…/batches --crops /vast/…/crops/national \
      --shard $SLURM_ARRAY_TASK_ID --nshards 12 --batch-size 4000 --zoom 16
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

from processing.gb1900.tiles import stitch_crop


def _text(rec) -> str:
    t = rec.get("text")
    return (t.get("value", "") if isinstance(t, dict) else t) or ""


def run(a) -> None:
    batch_dir = Path(a.batch_dir); batch_dir.mkdir(parents=True, exist_ok=True)
    crops = Path(a.crops); crops.mkdir(parents=True, exist_ok=True)
    n_batch = len(list(batch_dir.glob(f"batch_s{a.shard}_*.jsonl")))
    buf: list[dict] = []
    total = cropped = skipped = 0

    def emit() -> None:
        nonlocal n_batch, cropped
        if not buf:
            return
        n_batch += 1
        man = batch_dir / f"batch_s{a.shard}_{n_batch:04d}.jsonl"
        tmp = batch_dir / f"batch_s{a.shard}_{n_batch:04d}.jsonl.tmp"
        with open(tmp, "w", encoding="utf-8") as mf:
            saved = 0
            for rec in buf:
                pid = rec["pin_id"]; cp = crops / f"gb_{pid}.png"
                if cp.exists() and cp.stat().st_size > 0:
                    continue                                   # already cropped elsewhere
                img, meta = stitch_crop(rec["lat"], rec["lon"], _text(rec), a.zoom)
                if img is None:
                    continue
                img.save(cp); saved += 1; cropped += 1
                mf.write(json.dumps({
                    "place_id": rec.get("place_id", f"gb:{pid}"), "pin_id": pid,
                    "text": rec.get("text"), "lon": rec["lon"], "lat": rec["lat"],
                    "crop_path": str(cp), "crop": meta}, ensure_ascii=False) + "\n")
        if saved:
            tmp.replace(man)
            print(f"[crop s{a.shard}] wrote {man.name}: {saved:,} crops "
                  f"(cropped {cropped:,}, skipped {skipped:,})", flush=True)
        else:
            tmp.unlink(missing_ok=True); n_batch -= 1
        buf.clear()

    for line in open(a.pins, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        pid = rec.get("pin_id")
        lat, lon = rec.get("lat"), rec.get("lon")
        if pid is None or lat is None or lon is None:
            continue
        try:
            if int(pid, 16) % a.nshards != a.shard:            # not my shard
                continue
        except ValueError:
            if hash(pid) % a.nshards != a.shard:
                continue
        total += 1
        cp = crops / f"gb_{pid}.png"
        if cp.exists() and cp.stat().st_size > 0:
            skipped += 1
            continue
        buf.append(rec)
        if len(buf) >= a.batch_size:
            emit()
    emit()
    print(f"[crop s{a.shard}] DONE: shard pins {total:,}, cropped {cropped:,}, "
          f"already-had {skipped:,}", flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pins", required=True)
    p.add_argument("--batch-dir", required=True)
    p.add_argument("--crops", required=True)
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--nshards", type=int, required=True)
    p.add_argument("--batch-size", type=int, default=4000)
    p.add_argument("--zoom", type=int, default=16)
    run(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
