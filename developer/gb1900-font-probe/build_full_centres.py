"""Generate a FULL-COVERAGE set of sheet centres tiling every GB1900 label-bearing area.

Buckets all label coordinates into z17 tile-blocks of STEP tiles a side; a region (spot_sheet r=8 -> 17-tile
side) centred on each populated block covers it with a 2-tile overlap onto neighbours, so the union of all
regions is gap-free over the labels. Output is sorted row-major (by block y, then x) so a batched driver's
consecutive batches are spatially contiguous — neighbouring centres then share tiles WITHIN a batch (dedup on
prefetch) and only the batch-boundary seam is ever re-fetched after per-batch tile cleanup.

    python build_full_centres.py --out centres_all.txt          # from national_typed.jsonl
"""
import argparse, json, math

N17 = 2 ** 17
NT = "/vast/ishi/gb1900/edition/national_typed.jsonl"

def lonlat_to_tile(lon, lat):
    x = (lon + 180) / 360 * N17
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17
    return int(x), int(y)

def tile_to_lonlat(tx, ty):                      # centre of tile (tx+0.5, ty+0.5)
    lon = (tx + 0.5) / N17 * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 0.5) / N17))))
    return lon, lat

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=NT); ap.add_argument("--out", default="centres_all.txt")
    ap.add_argument("--step", type=int, default=15, help="block size in z17 tiles (region side 17 -> 2-tile overlap)")
    a = ap.parse_args()
    blocks = set(); n = 0; nc = 0
    for line in open(a.src):
        try: d = json.loads(line)
        except Exception: continue
        n += 1; lon, lat = d.get("lon"), d.get("lat")
        if lon is None or lat is None: continue
        nc += 1; tx, ty = lonlat_to_tile(lon, lat)
        blocks.add((tx // a.step, ty // a.step))
    print(f"labels {n}; with coords {nc}; populated blocks {len(blocks)}", flush=True)
    off = a.step // 2                             # block-centre tile within the block
    with open(a.out, "w") as f:
        for by, bx in sorted((by, bx) for bx, by in blocks):     # row-major for batch locality
            ctx, cty = bx * a.step + off, by * a.step + off
            lon, lat = tile_to_lonlat(ctx, cty)
            f.write(f"{lon:.5f} {lat:.5f} gb_{bx}_{by}\n")
    print(f"wrote {len(blocks)} centres -> {a.out}", flush=True)

if __name__ == "__main__":
    main()
