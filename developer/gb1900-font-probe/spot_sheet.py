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

N17 = 2 ** 17; TILES = "/vast/ishi/gb1900/tiles17"; IX1 = "/ix1/ishi/gb1900/tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
OUT = "/vast/ishi/gb1900/edition/spot"; os.makedirs(OUT, exist_ok=True)
SCORE_MIN = 0.4

def lonlat_to_px(lon, lat):
    x = (lon + 180) / 360 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y
def px_to_lonlat(px, py):
    lon = px / (N17 * 256) * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / (N17 * 256)))))
    return lon, lat

def get_tile(tx, ty):
    p = f"{TILES}/{tx}/{ty}.png"
    if os.path.exists(p) and os.path.getsize(p) > 500:
        try: return Image.open(p).convert("RGB")
        except Exception: pass
    os.makedirs(f"{TILES}/{tx}", exist_ok=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-spot"}), timeout=30) as r:
            data = r.read()
        if len(data) > 400:
            open(p, "wb").write(data); return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception: pass
    return None

def mosaic(mx0, my0, M):
    canvas = Image.new("RGB", (M * 256, M * 256), (255, 255, 255)); miss = 0
    for i in range(M):
        for j in range(M):
            t = get_tile(mx0 + i, my0 + j)
            if t is not None: canvas.paste(t, (i * 256, j * 256))
            else: miss += 1
    return canvas, miss

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lon", type=float, required=True); ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--tag", required=True); ap.add_argument("--r", type=int, default=8, help="tile radius")
    ap.add_argument("--mos", type=int, default=8); ap.add_argument("--overlap", type=int, default=1)
    ap.add_argument("--cleanup", action="store_true", help="delete this region's z17 tiles after spotting")
    ap.add_argument("--classify", action="store_true", help="font-classify this region's boxes before cleanup (tiles still hot)")
    a = ap.parse_args(); t0 = time.time()
    cxp, cyp = lonlat_to_px(a.lon, a.lat); ctx, cty = int(cxp // 256), int(cyp // 256)
    tx0, ty0 = ctx - a.r, cty - a.r; side = 2 * a.r + 1
    step = a.mos - a.overlap
    origins = [(tx0 + i, ty0 + j) for i in range(0, side, step) for j in range(0, side, step)]
    print(f"{a.tag}: region {side}x{side} tiles, {len(origins)} mosaics of {a.mos*256}px", flush=True)

    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"{a.tag}: spotter device={dev}", flush=True)
    runner = MapTextRunner(pd.DataFrame(),
        cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
        weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
    mdir = f"{OUT}/mosaics_{a.tag}"; os.makedirs(mdir, exist_ok=True)
    boxes = {}                                     # dedup key -> record
    for k, (mx0, my0) in enumerate(origins):
        img, miss = mosaic(mx0, my0, a.mos)
        mf = f"{mdir}/m_{mx0}_{my0}.png"; img.save(mf); gx0, gy0 = mx0 * 256, my0 * 256
        df = runner.run_on_image(mf, return_dataframe=True)
        if df is None or len(df) == 0 or "score" not in df.columns:   # blank/uncached mosaic -> 0 boxes
            os.remove(mf); print(f"  [{a.tag}] mosaic {k+1}/{len(origins)} empty (miss={miss})", flush=True); continue
        df = df[df["image_id"].astype(str) == os.path.basename(mf)].reset_index(drop=True)
        df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0.0)
        df = df[df["score"] >= SCORE_MIN]
        for _, r in df.iterrows():
            g0 = r["pixel_geometry"]; poly = wkt.loads(g0) if isinstance(g0, str) else g0; c = poly.centroid
            gcx, gcy = c.x + gx0, c.y + gy0
            key = (round(gcx / 8), round(gcy / 8), str(r["text"]).lower())
            if key in boxes and boxes[key]["score"] >= float(r["score"]): continue
            lon, lat = px_to_lonlat(gcx, gcy)
            boxes[key] = dict(text=str(r["text"]), score=round(float(r["score"]), 4),
                              gpoly=[[round(float(x) + gx0, 1), round(float(y) + gy0, 1)] for x, y in poly.exterior.coords],
                              gcx=round(gcx, 1), gcy=round(gcy, 1), lon=round(lon, 6), lat=round(lat, 6))
        os.remove(mf)
        print(f"  [{a.tag}] mosaic {k+1}/{len(origins)} miss={miss} boxes={len(boxes)} ({time.time()-t0:.0f}s)", flush=True)
    outp = f"{OUT}/boxes_{a.tag}.jsonl"
    with open(outp, "w") as f:
        for rec in boxes.values(): f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try: os.rmdir(mdir)
    except OSError: pass
    if a.classify:                                 # classify WHILE tiles are still hot (no reload later)
        try:
            from font_classify import classify_boxes
            recs = classify_boxes(list(boxes.values()))
            with open(f"{OUT}/boxes_font_{a.tag}.jsonl", "w") as cf_:
                for rec in recs: cf_.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"[{a.tag}] classified {len(recs)} boxes", flush=True)
        except Exception as e:
            print(f"[{a.tag}] classify FAILED: {e}", flush=True)
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
        print(f"[{a.tag}] archived {moved} tiles to /ix1", flush=True)
    print(f"[{a.tag}] DONE {len(boxes)} boxes -> {outp} ({time.time()-t0:.0f}s)", flush=True)

if __name__ == "__main__":
    main()
