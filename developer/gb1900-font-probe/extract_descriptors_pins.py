"""Descriptor bank over the pin-prompted detections — the Hi-SAM-convention replacement for the 113k bank.

Same instrument as before (frozen MapReader ViTAEv2 backbone, tight de-rotated crop -> 512² -> concat
stage3/4/5 mean-pool, 896-d) and the same output field names as `extract_descriptors.py`, so
`build_label_ui.load_bank` reads it with only a path change. What differs is the CROP: the extent comes from
Hi-SAM's LINE mask, which is the production crop unit and the convention Phase B measured (NEXT-PHASE.md §1.3).
The legacy `desc/` bank is MapReader-convention and must not be mixed with this one — a descriptor space that
straddles two crop conventions lets the classifier key on the convention instead of the font.

Carries the weak signature from the transcript so the labelling UI can seed itself; see `weak_sig` for why
that is a sampling prior only and never ground truth.

No crop pixels are persisted — only the 896-d descriptor (/vast is shared with prod ES).

    sbatch --array=0-7 extract_descriptors_pins.sbatch
"""
import sys, os
sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glob, json, time, numpy as np, torch, cv2
import concurrent.futures as cf
import pandas as pd
from make_font_testset_v2 import derotate            # assemble tiles (/vast,/ix1; no fetch) + minAreaRect de-rotate
from weak_sig import weak_sig

PINS = "/vast/ishi/gb1900/edition/pins"
OUT = "/vast/ishi/gb1900/edition/pins/desc"
SAFE = 512
BATCH = 16
shard = int(os.environ.get("SLURM_ARRAY_TASK_ID", 0))
nshard = int(os.environ.get("SLURM_ARRAY_TASK_COUNT", 1))
os.makedirs(OUT, exist_ok=True)

from mapreader import MapTextRunner

INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
dev = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[shard {shard}/{nshard}] device {dev}; loading spotter…", flush=True)
runner = MapTextRunner(pd.DataFrame(),
                       cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
                       weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
pred = runner.predictor
model = pred.model
model.eval()
input_format = pred.input_format
feat = {}


def _hook(m, i, o):
    feat["o"] = o


tgt_name, tgt = max([(n, m) for n, m in model.named_modules() if n.endswith("backbone")],
                    key=lambda nm: nm[0].count("."))
tgt.register_forward_hook(_hook)


def crop512(r):
    poly = r.get("line_gpoly") or r.get("gpoly")     # line = the label; word only if the line mask was empty
    if not poly:
        return None
    patch = derotate({"gpoly": poly})
    if patch is None or patch.size < 80:
        return None
    m = max(patch.shape[:2])
    sq = np.full((m, m), 255, np.uint8)
    sq[:patch.shape[0], :patch.shape[1]] = patch
    g = cv2.resize(sq, (SAFE, SAFE), interpolation=cv2.INTER_AREA)
    return np.repeat(g[:, :, None], 3, 2)            # gray -> 3ch (OS maps ~monochrome; matches the probe)


def backbone_batch(imgs):
    inputs = []
    for im in imgs:
        arr = im[:, :, ::-1] if input_format == "BGR" else im
        t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).to(dev)
        inputs.append({"image": t, "height": SAFE, "width": SAFE})
    feat.clear()
    try:
        with torch.no_grad():
            model(inputs)
    except Exception:
        pass
    o = feat.get("o", {})
    per = []
    for k in sorted(o) if isinstance(o, dict) else []:
        v = o[k]
        if hasattr(v, "tensors"):
            v = v.tensors
        if torch.is_tensor(v) and v.dim() == 4:
            per.append(v.mean(dim=(2, 3)).float().cpu().numpy())
    return np.concatenate(per, 1) if per else None


# ---- gather this shard's words: region files assigned whole, for tile locality ----
files = sorted(glob.glob(f"{PINS}/pins_*.jsonl"))
words = []
for fi, f in enumerate(files):
    if fi % nshard != shard:
        continue
    for line in open(f):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if sum(c.isalnum() for c in r.get("text", "")) < 2:
            continue
        words.append(r)
print(f"[shard {shard}] {len(files)} region files, {len(words)} labels assigned", flush=True)

rows = []
miss = 0
t0 = time.time()


def process_chunk(chunk):
    global miss
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        crops = list(ex.map(crop512, chunk))
    keep = [(r, c) for r, c in zip(chunk, crops) if c is not None]
    miss += len(chunk) - len(keep)
    for i in range(0, len(keep), BATCH):
        sub = keep[i:i + BATCH]
        D = backbone_batch([c for _, c in sub])
        if D is None:
            continue
        for (r, _), d in zip(sub, D):
            ws, rule = weak_sig(r.get("text", ""))
            rows.append((r["gcx"], r["gcy"], r["lon"], r["lat"], r["text"], r.get("pin_id", ""),
                         float(r.get("score", 0)), ws or "", rule or "", d.astype(np.float16)))


CHUNK = 256
for i in range(0, len(words), CHUNK):
    process_chunk(words[i:i + CHUNK])
    if i and i % 5120 == 0:
        rate = (time.time() - t0) / i
        print(f"[shard {shard}] {i}/{len(words)}  kept {len(rows)} miss {miss}  "
              f"{rate*1000:.0f}ms/word ETA {rate*(len(words)-i)/60:.0f}m", flush=True)

if rows:
    np.savez_compressed(
        f"{OUT}/shard_{shard:02d}.npz",
        desc=np.stack([x[9] for x in rows]),
        gcx=np.array([x[0] for x in rows], np.float64), gcy=np.array([x[1] for x in rows], np.float64),
        lon=np.array([x[2] for x in rows], np.float32), lat=np.array([x[3] for x in rows], np.float32),
        text=np.array([x[4] for x in rows], object), pin_id=np.array([x[5] for x in rows], object),
        score=np.array([x[6] for x in rows], np.float32),
        weak_sig=np.array([x[7] for x in rows], object), weak_rule=np.array([x[8] for x in rows], object))
    nweak = sum(1 for x in rows if x[7])
    print(f"[shard {shard}] wrote {len(rows)} descriptors (miss {miss}, "
          f"{miss/max(1,len(words))*100:.0f}%; weak-labelled {nweak}) -> {OUT}/shard_{shard:02d}.npz", flush=True)
else:
    print(f"[shard {shard}] no descriptors (all {miss} tile-missed?)", flush=True)
print(f"[shard {shard}] EXTRACTDONE {(time.time()-t0)/60:.1f}m", flush=True)
