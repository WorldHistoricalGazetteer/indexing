"""Pre-fetch z17 tiles for a list of sheet centres INTO the /vast cache, on a host with network (pitt).
GPU compute nodes cannot reach S3, so tiles must be cached first; then GPU spotting reads them locally.
Tiles are small (~5 MB per 17x17 region) and are the only thing persisted — no crop snippets (keeps /vast
within the discipline of [[vast_capacity_and_crop_fragments]]).

    python prefetch_tiles.py centres_full.txt --r 8 --workers 64
"""
import argparse, os, io, math, urllib.request
import concurrent.futures as cf

N17 = 2 ** 17; TILES = os.environ.get("FCTILES") or "/vast/ishi/gb1900/tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"

def lonlat_to_px(lon, lat):
    x = (lon + 180) / 360 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y

def fetch(t):
    tx, ty = t; p = f"{TILES}/{tx}/{ty}.png"
    if os.path.exists(p) and os.path.getsize(p) > 500: return 0
    os.makedirs(f"{TILES}/{tx}", exist_ok=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-prefetch"}), timeout=30) as r:
            data = r.read()
        if len(data) > 400: open(p, "wb").write(data); return 1
    except Exception: pass
    return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("centres"); ap.add_argument("--r", type=int, default=8); ap.add_argument("--workers", type=int, default=64)
    a = ap.parse_args()
    need = set()
    for line in open(a.centres):
        if not line.strip(): continue
        lon, lat, tag = line.split()[:3]
        cx, cy = lonlat_to_px(float(lon), float(lat)); ctx, cty = int(cx // 256), int(cy // 256)
        for tx in range(ctx - a.r, ctx + a.r + 1):
            for ty in range(cty - a.r, cty + a.r + 1):
                need.add((tx, ty))
    print(f"tiles needed: {len(need)}", flush=True)
    fetched = 0
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, got in enumerate(ex.map(fetch, need)):
            fetched += got
            if i % 5000 == 0: print(f"  {i}/{len(need)} fetched+={fetched}", flush=True)
    print(f"PREFETCHDONE fetched {fetched} new tiles", flush=True)

if __name__ == "__main__":
    main()
