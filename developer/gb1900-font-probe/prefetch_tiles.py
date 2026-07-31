"""CPU-only, HANG-PROOF z17 tile prefetcher — warms the /vast tile cache so GPU spotting reads locally (no S3,
no hang). The inline get_tile (and the old urllib prefetcher) hung in I/O-wait on dead-but-open S3 connections
because urllib's timeout doesn't fire there; here `requests` with explicit (connect, read) timeouts ALWAYS
fires, plus retry/backoff. Tiles shared between overlapping regions are de-duped into one set. Sharded for a
Slurm array; resumable (skips cached tiles). Run on htc (outbound net, no GPU wasted):

    sbatch -M htc --account=ishi --array=0-3 --export=ALL,CENTRES=centres_english.txt prefetch.sbatch
"""
import argparse, os, math, time
import concurrent.futures as cf
import requests

N17 = 2 ** 17
TILES = os.environ.get("FCTILES") or "/vast/ishi/gb1900/tiles17"
IX1 = "/ix1/ishi/gb1900/tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
SESS = requests.Session()

def lonlat_to_tile(lon, lat):
    x = (lon + 180) / 360 * N17
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17
    return int(x), int(y)

def fetch(t):
    tx, ty = t
    for base in (TILES, IX1):                                       # already cached anywhere?
        p = f"{base}/{tx}/{ty}.png"
        if os.path.exists(p) and os.path.getsize(p) > 500: return 0
    d = f"{TILES}/{tx}"; os.makedirs(d, exist_ok=True); p = f"{d}/{ty}.png"
    for k in range(4):
        try:
            r = SESS.get(S3.format(x=tx, y=ty), timeout=(5, 20), headers={"User-Agent": "whg-prefetch"})
            if r.status_code in (403, 404): return -1               # genuinely absent
            if r.status_code == 200 and len(r.content) > 400:
                tmp = p + f".tmp{os.getpid()}"; open(tmp, "wb").write(r.content); os.replace(tmp, p); return 1
            return -1
        except Exception:
            time.sleep(1.5 * (k + 1))                               # 1.5→6s backoff (S3 503 / transient hang)
    return -2                                                       # failed -> left uncached, retried next run

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--centres", default=os.environ.get("CENTRES", "centres_english.txt"))
    ap.add_argument("--r", type=int, default=8); ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--shard", type=int, default=int(os.environ.get("SLURM_ARRAY_TASK_ID", 0)))
    ap.add_argument("--nshards", type=int, default=int(os.environ.get("SLURM_ARRAY_TASK_COUNT", 1)))
    a = ap.parse_args()
    need = set()
    for line in open(a.centres):
        p = line.split()
        if len(p) < 3: continue
        ctx, cty = lonlat_to_tile(float(p[0]), float(p[1]))
        for tx in range(ctx - a.r, ctx + a.r + 1):
            for ty in range(cty - a.r, cty + a.r + 1): need.add((tx, ty))
    mine = sorted(t for t in need if (hash(t) % a.nshards) == a.shard)   # this shard's tiles
    print(f"shard {a.shard}/{a.nshards}: {len(mine)} of {len(need)} unique tiles, {a.workers} workers", flush=True)
    t0 = time.time(); c = {1: 0, 0: 0, -1: 0, -2: 0}
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, res in enumerate(ex.map(fetch, mine)):
            c[res] += 1
            if i % 2000 == 0 and i:
                rate = (time.time() - t0) / i
                print(f"  {i}/{len(mine)}  fetched {c[1]} cached {c[0]} absent {c[-1]} FAIL {c[-2]}"
                      f"  ({rate*1000:.0f}ms/tile, ETA {rate*(len(mine)-i)/60:.0f} min)", flush=True)
    print(f"shard {a.shard} DONE: fetched {c[1]}, cached {c[0]}, absent {c[-1]}, FAIL {c[-2]} in {(time.time()-t0)/60:.0f} min", flush=True)

if __name__ == "__main__":
    main()
