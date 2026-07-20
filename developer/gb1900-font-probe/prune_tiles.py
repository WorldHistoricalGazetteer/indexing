"""Delete the z17 tiles for a batch of centres after it has been spotted, reclaiming /vast. Mirrors
prefetch_tiles.py's need-set so exactly the batch's tiles go. Neighbouring batches re-fetch only the 2-tile
seam (centres are spatially sorted), so peak tile storage stays ~one batch — the discipline that keeps the
full-GB spot from ever crowding prod ES off the shared /vast ([[vast_capacity_and_crop_fragments]]).

    python prune_tiles.py cov/batch.txt --r 8
"""
import argparse, math, os

N17 = 2 ** 17; TILES = "/vast/ishi/gb1900/tiles17"

def lonlat_to_px(lon, lat):
    x = (lon + 180) / 360 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("centres"); ap.add_argument("--r", type=int, default=8)
    a = ap.parse_args()
    dead = 0; cols = set()
    for line in open(a.centres):
        if not line.strip(): continue
        lon, lat, tag = line.split()[:3]
        cx, cy = lonlat_to_px(float(lon), float(lat)); ctx, cty = int(cx // 256), int(cy // 256)
        for tx in range(ctx - a.r, ctx + a.r + 1):
            cols.add(tx)
            for ty in range(cty - a.r, cty + a.r + 1):
                p = f"{TILES}/{tx}/{ty}.png"
                try: os.remove(p); dead += 1
                except OSError: pass
    for tx in cols:                                  # drop now-empty column dirs
        d = f"{TILES}/{tx}"
        try: os.rmdir(d)
        except OSError: pass
    print(f"PRUNEDONE removed {dead} tiles")

if __name__ == "__main__":
    main()
