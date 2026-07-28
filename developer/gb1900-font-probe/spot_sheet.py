"""Phase C localization via MapReader spotter (z17) — the RIGHT instrument (full-word polygons + recognised
text), replacing the fragmenting pure-OpenCV discovery. Tiles a sheet region into overlapping 2048px
mosaics, runs MapTextRunner on each, maps boxes to GLOBAL z17 px + lon/lat, dedups across overlaps, and
writes one box record per word: {text, score, gpoly, gcx/gcy, lon/lat}. These clean crops feed the HITL
font test-set and the same-letter alphabet.

    /home/stg135/.conda/envs/mapreader/bin/python spot_sheet.py --lon -1.78 --lat 51.18 --tag amesbury --r 8
"""
import argparse, os, io, math, json, time, shutil, urllib.request, numpy as np
import pandas as pd
from shapely import wkt
from PIL import Image
from mapreader import MapTextRunner

N17 = 2 ** 17
# SPOT_TILES points the spotter at an alternate tile cache (e.g. a masked/de-noised copy of a sheet built by
# clean_sheet.py) without touching this script. When set, the /ix1 archive fallback is dropped too — otherwise
# a missing processed tile would silently fall back to the ORIGINAL imagery and quietly contaminate the run.
# On-demand fetches are cached to NODE-LOCAL scratch, never to /vast. /vast/ishi is a 1TB project quota
# shared with production ES and this project has driven it to flood-stage read-only before; a cache that
# grows without bound on it is a liability, not a convenience. The durable layer is the /ix1 block store,
# so anything lost when the node goes away costs at most a re-fetch of tiles the corpus does not yet hold.
TILES = (os.environ.get("SPOT_TILES")
         or (os.path.join(os.environ["SLURM_SCRATCH"], "tiles17") if os.environ.get("SLURM_SCRATCH")
             else "/vast/ishi/gb1900/tiles17"))
IX1 = "" if os.environ.get("SPOT_TILES") else "/ix1/ishi/gb1900/tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
# Overridable so a re-spot can write alongside the existing output instead of over it: comparing an old
# and a new pass is the only way to tell an intended change (the added baseline) from an unintended one.
OUT = os.environ.get("SPOT_OUT", "/vast/ishi/gb1900/edition/spot"); os.makedirs(OUT, exist_ok=True)
SCORE_MIN = 0.4

def lonlat_to_px(lon, lat):
    x = (lon + 180) / 360 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y
def px_to_lonlat(px, py):
    lon = px / (N17 * 256) * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / (N17 * 256)))))
    return lon, lat

_STORE_DIR = os.environ.get("TILE_STORE", "/ix1/ishi/gb1900/tilestore")
_STORE_CACHE = {}
_STORE_BLOCK = 64


def store_tile(tx, ty):
    """Read from the per-block corpus on /ix1, if it is there.

    Tried FIRST, ahead of loose files and well ahead of the network: this is the whole point of building it.
    Connections are cached per block because a mosaic's 64 tiles nearly always fall in one or two blocks, so
    the open cost is paid once per region rather than once per tile. A missing block is remembered as None
    so an unbuilt corpus costs one failed lookup per block rather than one per tile.
    """
    key = (tx // _STORE_BLOCK, ty // _STORE_BLOCK)
    con = _STORE_CACHE.get(key, False)
    if con is False:
        import sqlite3
        path = f"{_STORE_DIR}/z17_{key[0]}_{key[1]}.sqlite"
        con = None
        if os.path.exists(path):
            try:
                # Read-write open, not mode=ro: a block still carrying a WAL is invisible to a read-only
                # connection, and a cache that silently reads as empty is worse than one that fails loudly.
                con = sqlite3.connect(path, timeout=30)
                con.execute("SELECT count(*) FROM tile LIMIT 1")
            except Exception:
                con = None
        _STORE_CACHE[key] = con
    if con is None:
        return None
    try:
        row = con.execute("SELECT data FROM tile WHERE tx=? AND ty=?", (tx, ty)).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def get_tile(tx, ty):
    d = store_tile(tx, ty)
    if d:
        try:
            return Image.open(io.BytesIO(d)).convert("RGB")
        except Exception:
            pass
    p = f"{TILES}/{tx}/{ty}.png"
    if os.path.exists(p) and os.path.getsize(p) > 500:
        try: return Image.open(p).convert("RGB")
        except Exception: pass
    ap = f"{IX1}/{tx}/{ty}.png"                          # /ix1 archive (populated by --cleanup); avoids S3 re-fetch
    if os.path.exists(ap) and os.path.getsize(ap) > 500:
        try: return Image.open(ap).convert("RGB")
        except Exception: pass
    if os.environ.get("SPOT_TILES"):
        return None          # processed-tile mode: a miss is a miss. Fetching would pull the ORIGINAL tile
                             # into the processed cache and silently un-do the masking for that tile.
    os.makedirs(f"{TILES}/{tx}", exist_ok=True)
    # Retry with backoff: S3 returns 503 SlowDown under concurrent load (8 GPU tasks × ~289 tiles/region),
    # and the old no-retry get_tile dropped the WHOLE region to 0 boxes on a throttle burst (1018/1200 empties).
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-spot"}), timeout=30) as r:
                data = r.read()
            if len(data) > 400:
                open(p, "wb").write(data); return Image.open(io.BytesIO(data)).convert("RGB")
            return None                                  # small/absent object (ocean tile) — legitimately empty, no retry
        except Exception as e:
            if getattr(e, "code", None) in (403, 404): return None   # genuinely absent — don't retry
            time.sleep(1.5 * (attempt + 1))              # 1.5→6s backoff before next attempt (throttle/timeout)
    return None

def mosaic(mx0, my0, M, workers=16):
    """Fetch the mosaic's tiles CONCURRENTLY.

    Serially, a missing tile costs its whole retry ladder before the next is even attempted, so the cost of
    a bad patch is the SUM of the failures rather than the slowest of them. Re-spotting a starved region
    showed mosaics of 6-9s where every tile was cached against 950s, 755s and 637s where 40, 32 and 27 were
    not — which is also why regions were being killed by their walltime and losing everything. The work is
    network-bound, so threads are the right tool and the retry ladder inside get_tile is unchanged.
    """
    import concurrent.futures as _cf
    canvas = Image.new("RGB", (M * 256, M * 256), (255, 255, 255)); miss = 0
    def one(ij):
        i, j = ij
        return i, j, get_tile(mx0 + i, my0 + j)
    with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, j, t in ex.map(one, [(i, j) for i in range(M) for j in range(M)]):
            if t is not None: canvas.paste(t, (i * 256, j * 256))
            else: miss += 1
    return canvas, miss

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lon", type=float, default=None); ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--tag", default=None); ap.add_argument("--r", type=int, default=8, help="tile radius")
    ap.add_argument("--mos", type=int, default=8); ap.add_argument("--overlap", type=int, default=1)
    ap.add_argument("--cleanup", action="store_true", help="delete this region's z17 tiles after spotting")
    ap.add_argument("--classify", action="store_true", help="font-classify this region's boxes before cleanup (tiles still hot)")
    ap.add_argument("--centres", default=None,
                    help="lon lat tag [n] per line — spot MANY regions in one process. The model costs "
                         "20-40s to load and, now that tiles are local and a mosaic takes ~7s, loading it "
                         "once per region would be a large share of a 35,514-region sweep")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    a = ap.parse_args()
    if not a.centres and (a.lon is None or a.lat is None or not tag):
        ap.error("give --lon/--lat/--tag, or --centres")

    dev = "cuda" if __import__("torch").cuda.is_available() else "cpu"
    runner = MapTextRunner(pd.DataFrame(),
        cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
        weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)

    if not a.centres:
        do_region(runner, a, a.lon, a.lat, tag)
        return
    todo = []
    for line in open(a.centres):
        p_ = line.split()
        if len(p_) >= 3:
            todo.append((float(p_[0]), float(p_[1]), p_[2]))
    mine = [r for i, r in enumerate(todo) if i % a.of == a.shard]
    print(f"shard {a.shard}/{a.of}: {len(mine)} of {len(todo)} regions, model loaded once", flush=True)
    done = 0
    for k, (lon, lat, tag) in enumerate(mine):
        bf = f"{OUT}/boxes_{tag}.jsonl"
        if os.path.exists(bf) and os.path.getsize(bf) > 0:
            continue                                   # resumable, same contract as before
        try:
            do_region(runner, a, lon, lat, tag)
            done += 1
        except Exception as e:
            print(f"FAIL {tag}: {type(e).__name__}: {e}", flush=True)
        if (k + 1) % 25 == 0:
            print(f"  [shard {a.shard}] {k+1}/{len(mine)} seen, {done} spotted "
                  f"({time.time()-T0:.0f}s)", flush=True)
    print(f"SHARDDONE {a.shard}: {done} regions spotted", flush=True)


T0 = time.time()


def do_region(runner, a, lon, lat, tag):
    t0 = time.time(); tile_miss = tile_tot = 0
    cxp, cyp = lonlat_to_px(lon, lat); ctx, cty = int(cxp // 256), int(cyp // 256)
    tx0, ty0 = ctx - a.r, cty - a.r; side = 2 * a.r + 1
    step = a.mos - a.overlap
    origins = [(tx0 + i, ty0 + j) for i in range(0, side, step) for j in range(0, side, step)]
    print(f"{tag}: region {side}x{side} tiles, {len(origins)} mosaics of {a.mos*256}px", flush=True)

    mdir = f"{OUT}/mosaics_{tag}"; os.makedirs(mdir, exist_ok=True)
    boxes = {}                                     # dedup key -> record
    for k, (mx0, my0) in enumerate(origins):
        img, miss = mosaic(mx0, my0, a.mos)
        tile_miss += miss; tile_tot += a.mos * a.mos
        mf = f"{mdir}/m_{mx0}_{my0}.png"; img.save(mf); gx0, gy0 = mx0 * 256, my0 * 256
        df = runner.run_on_image(mf, return_dataframe=True)
        if df is None or len(df) == 0 or "score" not in df.columns:   # blank/uncached mosaic -> 0 boxes
            os.remove(mf); print(f"  [{tag}] mosaic {k+1}/{len(origins)} empty (miss={miss})", flush=True); continue
        df = df[df["image_id"].astype(str) == os.path.basename(mf)].reset_index(drop=True)
        df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0.0)
        df = df[df["score"] >= SCORE_MIN]
        for _, r in df.iterrows():
            g0 = r["pixel_geometry"]; poly = wkt.loads(g0) if isinstance(g0, str) else g0; c = poly.centroid
            gcx, gcy = c.x + gx0, c.y + gy0
            key = (round(gcx / 8), round(gcy / 8), str(r["text"]).lower())
            if key in boxes and boxes[key]["score"] >= float(r["score"]): continue
            lon, lat = px_to_lonlat(gcx, gcy)
            # Keep the model's OWN baseline as well as the outline. MapTextPipeline emits a pixel_line per
            # detection — the text's centre-line — and discarding it meant every downstream stage re-derived
            # direction from the outline's minimum-area rectangle, which is a chord across a curved word
            # rather than its heading.
            gl = None
            l0 = r.get("pixel_line")
            if l0 is not None:
                try:
                    ln = wkt.loads(l0) if isinstance(l0, str) else l0
                    gl = [[round(float(x) + gx0, 1), round(float(y) + gy0, 1)] for x, y in ln.coords]
                except Exception:
                    gl = None
            boxes[key] = dict(text=str(r["text"]), score=round(float(r["score"]), 4),
                              gpoly=[[round(float(x) + gx0, 1), round(float(y) + gy0, 1)] for x, y in poly.exterior.coords],
                              gline=gl,
                              gcx=round(gcx, 1), gcy=round(gcy, 1), lon=round(lon, 6), lat=round(lat, 6))
        os.remove(mf)
        print(f"  [{tag}] mosaic {k+1}/{len(origins)} miss={miss} boxes={len(boxes)} ({time.time()-t0:.0f}s)", flush=True)
    # A region that completed while NLS tile fetches were failing writes a near-empty file that is
    # indistinguishable from genuinely empty terrain, and the resume rule then skips it FOREVER because the
    # file is non-empty. Record what was actually seen, so a starved run can be told from a quiet one.
    json.dump(dict(tag=tag, tiles_missing=tile_miss, tiles_total=tile_tot,
                   miss_frac=round(tile_miss / max(1, tile_tot), 4), boxes=len(boxes)),
              open(f"{OUT}/cover_{tag}.json", "w"))
    outp = f"{OUT}/boxes_{tag}.jsonl"
    with open(outp, "w") as f:
        for rec in boxes.values(): f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try: os.rmdir(mdir)
    except OSError: pass
    if a.classify:                                 # classify WHILE tiles are still hot (no reload later)
        try:
            from font_classify import classify_boxes
            recs = classify_boxes(list(boxes.values()))
            with open(f"{OUT}/boxes_font_{tag}.jsonl", "w") as cf_:
                for rec in recs: cf_.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"[{tag}] classified {len(recs)} boxes", flush=True)
        except Exception as e:
            print(f"[{tag}] classify FAILED: {e}", flush=True)
    if a.cleanup:                                  # free /vast by ARCHIVING this region's tiles to /ix1
        moved = 0
        for tx in range(tx0, tx0 + side):
            for ty in range(ty0, ty0 + side):
                src = f"{TILES}/{tx}/{ty}.png"
                if not os.path.exists(src): continue
                dst = f"{IX1}/{tx}/{ty}.png"
                try:
                    if os.path.exists(dst): os.remove(src)          # already archived by a neighbour
                    else: os.makedirs(f"{IX1}/{tx}", exist_ok=True); shutil.move(src, dst)
                    moved += 1
                except OSError:
                    try: os.remove(src)
                    except OSError: pass
            try: os.rmdir(f"{TILES}/{tx}")
            except OSError: pass
        print(f"[{tag}] archived {moved} tiles to /ix1", flush=True)
    print(f"[{tag}] DONE {len(boxes)} boxes -> {outp} ({time.time()-t0:.0f}s)", flush=True)

if __name__ == "__main__":
    main()
