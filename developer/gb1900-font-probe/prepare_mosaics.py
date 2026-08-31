"""Assemble large RGB z17 mosaics over a geographic SPREAD of spotted regions, so we can run a newer segmenter
(Hi-SAM) on the SAME imagery MapReader processed and see whether it recovers the letter-spaced admin labels /
sparse antiquities MapReader's word-first spotter misses. Each mosaic is R×R tiles centred on a region's word
centroid; MapReader's own boxes (gpoly) are saved alongside (offset to mosaic px) for overlay comparison. Tiles
read from the /vast+/ix1 cache only (no fetch). GPU-free.

    python prepare_mosaics.py --n 8 --r 16
"""
import glob, json, argparse, os, numpy as np, cv2
from PIL import Image

SPOT = "/vast/ishi/gb1900/edition/spot"; OUT = f"{SPOT}/hisam_test"
TILES = "/vast/ishi/gb1900/tiles17"; IX1 = "/ix1/ishi/gb1900/tiles17"

def read_tile(tx, ty):
    for base in (TILES, IX1):
        p = f"{base}/{tx}/{ty}.png"
        if os.path.exists(p):
            try: return np.asarray(Image.open(p).convert("RGB"), np.uint8)
            except Exception: return None
    return None

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=8); ap.add_argument("--r", type=int, default=16)
    a = ap.parse_args(); os.makedirs(OUT, exist_ok=True)
    files = sorted(glob.glob(f"{SPOT}/boxes_gb_*.jsonl"))
    pick = files[:: max(1, len(files) // a.n)][:a.n]                 # geographic spread across the region grid
    print(f"{len(files)} region files, testing {len(pick)}", flush=True)
    for f in pick:
        tag = os.path.basename(f)[6:-6]
        rows = [json.loads(l) for l in open(f)]
        rows = [r for r in rows if r.get("gpoly")]
        if not rows: continue
        cx = np.median([np.mean([p[0] for p in r["gpoly"]]) for r in rows])
        cy = np.median([np.mean([p[1] for p in r["gpoly"]]) for r in rows])
        tx0 = int(cx) // 256 - a.r // 2; ty0 = int(cy) // 256 - a.r // 2
        ox, oy = tx0 * 256, ty0 * 256
        cvx = np.full((a.r * 256, a.r * 256, 3), 255, np.uint8); hit = 0
        for i, tx in enumerate(range(tx0, tx0 + a.r)):
            for j, ty in enumerate(range(ty0, ty0 + a.r)):
                t = read_tile(tx, ty)
                if t is not None: cvx[j * 256:j * 256 + 256, i * 256:i * 256 + 256] = t; hit += 1
        if hit < a.r * a.r * 0.5:                                    # too many missing tiles -> skip
            print(f"  {tag}: only {hit}/{a.r*a.r} tiles cached — skip", flush=True); continue
        # MapReader boxes that fall inside this mosaic, offset to mosaic px
        W = a.r * 256; boxes = []
        for r in rows:
            pts = [[p[0] - ox, p[1] - oy] for p in r["gpoly"]]
            if all(0 <= p[0] < W and 0 <= p[1] < W for p in pts): boxes.append(dict(text=r["text"], poly=pts))
        Image.fromarray(cvx).save(f"{OUT}/mosaic_{tag}.png")
        json.dump(boxes, open(f"{OUT}/mrboxes_{tag}.json", "w"))
        print(f"  {tag}: {a.r}x{a.r} mosaic ({hit} tiles), {len(boxes)} MapReader boxes -> mosaic_{tag}.png", flush=True)
    print("MOSAICSDONE", flush=True)

if __name__ == "__main__":
    main()
