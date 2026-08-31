"""Step 1 of the backbone-typing pipeline: extract the word-level MapReader-backbone font-style descriptor
(tight de-rotated word crop -> 512² -> frozen ViTAEv2 -> concat stage3-5 global-pool, the 0.42-sig winner)
for every banked spotter word, so the labelling UI + readout have a descriptor bank. NO crop pixels are
persisted (only the 896-d descriptor) — /vast must not fill (ES shares it).

Parallel Slurm ARRAY: shard the boxes across tasks; per-region files sharded whole (tile locality), the big
boxes_font.jsonl sharded by line (balance). Tiles read from the /vast+/ix1 cache with NO network fetch (a
cleaned-tile word is simply skipped — avoids the urllib fetch-on-dead-connection hang); most tiles are hot
from the just-run spot. Backbone forwards are BATCHED (16 words) for throughput. Writes one parquet shard.

    sbatch --array=0-7 extract_descriptors.sbatch
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, glob, json, time, numpy as np, torch, cv2
import concurrent.futures as cf
import pandas as pd
from make_font_testset_v2 import derotate          # assemble tiles (/vast,/ix1; no fetch) + minAreaRect de-rotate

SPOT = "/vast/ishi/gb1900/edition/spot"; OUT = f"{SPOT}/desc"; SAFE = 512; BATCH = 16
shard = int(os.environ.get("SLURM_ARRAY_TASK_ID", 0))
nshard = int(os.environ.get("SLURM_ARRAY_TASK_COUNT", 1))
os.makedirs(OUT, exist_ok=True)

from mapreader import MapTextRunner
INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
dev = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[shard {shard}/{nshard}] device {dev}; loading spotter…", flush=True)
runner = MapTextRunner(pd.DataFrame(), cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
    weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
pred = runner.predictor; model = pred.model; model.eval(); input_format = pred.input_format
feat = {}
def _hook(m, i, o): feat["o"] = o
tgt_name, tgt = max([(n, m) for n, m in model.named_modules() if n.endswith("backbone")], key=lambda nm: nm[0].count("."))
tgt.register_forward_hook(_hook)

def crop512(r):
    patch = derotate(r)                                        # grayscale, de-rotated tight word (or None on tile miss)
    if patch is None or patch.size < 80: return None
    m = max(patch.shape[:2]); sq = np.full((m, m), 255, np.uint8); sq[:patch.shape[0], :patch.shape[1]] = patch
    g = cv2.resize(sq, (SAFE, SAFE), interpolation=cv2.INTER_AREA)
    return np.repeat(g[:, :, None], 3, 2)                      # gray -> 3ch (OS maps ~monochrome; matches probe)

def backbone_batch(imgs):
    inputs = []
    for im in imgs:
        arr = im[:, :, ::-1] if input_format == "BGR" else im
        t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).to(dev)
        inputs.append({"image": t, "height": SAFE, "width": SAFE})
    feat.clear()
    try:
        with torch.no_grad(): model(inputs)
    except Exception: pass
    o = feat.get("o", {}); per = []
    for k in sorted(o) if isinstance(o, dict) else []:
        v = o[k]
        if hasattr(v, "tensors"): v = v.tensors
        if torch.is_tensor(v) and v.dim() == 4: per.append(v.mean(dim=(2, 3)).float().cpu().numpy())   # [B,C]
    return np.concatenate(per, 1) if per else None             # [B,896]

# ---- gather this shard's words ----
files = sorted(glob.glob(f"{SPOT}/boxes_gb_*.jsonl")) + [f"{SPOT}/boxes_font.jsonl"]
words = []
for fi, f in enumerate(files):
    big = f.endswith("boxes_font.jsonl")
    if not big and fi % nshard != shard: continue             # region file -> assigned whole (tile locality)
    if not os.path.exists(f): continue
    for li, line in enumerate(open(f)):
        if big and li % nshard != shard: continue             # big file -> by line (balance)
        try: r = json.loads(line)
        except Exception: continue
        if r.get("score", 0) < 0.55 or sum(c.isalnum() for c in r["text"]) < 2: continue
        words.append(r)
cap = int(os.environ.get("WORDCAP", 0))
if cap: words = words[:cap]
print(f"[shard {shard}] {len(words)} classifiable words assigned{' (CAPPED)' if cap else ''}", flush=True)

# ---- crop (threaded) + backbone (batched) ----
rows = []; miss = 0; t0 = time.time()
def process_chunk(chunk):
    global miss
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        crops = list(ex.map(crop512, chunk))
    keep = [(r, c) for r, c in zip(chunk, crops) if c is not None]
    miss += len(chunk) - len(keep)
    for i in range(0, len(keep), BATCH):
        sub = keep[i:i + BATCH]
        D = backbone_batch([c for _, c in sub])
        if D is None: continue
        for (r, _), d in zip(sub, D):
            rows.append((r["gcx"], r["gcy"], r["lon"], r["lat"], r["text"], r.get("font", ""),
                         float(r.get("score", 0)), d.astype(np.float16)))

CHUNK = 256
for i in range(0, len(words), CHUNK):
    process_chunk(words[i:i + CHUNK])
    if i and i % 5120 == 0:
        r = (time.time() - t0) / i
        print(f"[shard {shard}] {i}/{len(words)}  kept {len(rows)} miss {miss}  {r*1000:.0f}ms/word ETA {r*(len(words)-i)/60:.0f}m", flush=True)

if rows:
    np.savez_compressed(f"{OUT}/shard_{shard:02d}.npz",
        desc=np.stack([x[7] for x in rows]),                  # [N,896] float16
        gcx=np.array([x[0] for x in rows], np.float64), gcy=np.array([x[1] for x in rows], np.float64),
        lon=np.array([x[2] for x in rows], np.float32), lat=np.array([x[3] for x in rows], np.float32),
        text=np.array([x[4] for x in rows], object), font_crude=np.array([x[5] for x in rows], object),
        score=np.array([x[6] for x in rows], np.float32))
    print(f"[shard {shard}] wrote {len(rows)} descriptors (miss {miss}, {miss/max(1,len(words))*100:.0f}%) -> {OUT}/shard_{shard:02d}.npz", flush=True)
else:
    print(f"[shard {shard}] no descriptors (all {miss} tile-missed?)", flush=True)
print(f"[shard {shard}] EXTRACTDONE {(time.time()-t0)/60:.1f}m", flush=True)
