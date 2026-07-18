"""z17 full-corpus campaign — ONE geographic batch (a z16 latitude band):
  1. select the band's GB1900 pins;
  2. fetch ONLY the z17 tiles those labels need (deduped — sparse coverage, not the full bbox);
  3. crop each label at z17 + font-classify (CRNN style) and merge with the text typer (type_assign)
     -> top-3 (type, prob); ADDITIVE (never drops labels);
  4. tar the band's z17 tiles to /ix1, drop from /vast; mark the band done (resumable).
Classify-only pass 1 (spotter rescan = pass 2). Run per band via Slurm; idempotent.
    python -m processing.gb1900.z17_batch --ymin 21416 --ymax 21543 --out /vast/.../types_z17
"""
import argparse, os, json, math, io, time, tarfile, urllib.request, numpy as np
import concurrent.futures as cf
from collections import Counter
import sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from type_assign import assign_types

# font-classification lives in the font-probe dir (CRNN + sheet-grounded classifier)
FONT_DIR = "/vast/ishi/gb1900/probe/font"
sys.path.insert(0, FONT_DIR)
MODEL_DIR = f"{FONT_DIR}/out_z17"          # crnn_z17.pt + vocab.json
CLF_PATH = f"{FONT_DIR}/out_ground/font_clf.joblib"   # sheet-grounded fusion classifier

NT = "/vast/ishi/gb1900/edition/national_typed.jsonl"
TILES17 = "/vast/ishi/gb1900/tiles17"
IX1_ARCHIVE = "/ix1/ishi/gb1900_tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
N17 = 2 ** 17
CROP_W, CROP_H = 300, 110    # z17 px window per label (generous; label offset from crowd point)

def z16y(lat):
    return int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * (2 ** 16))
def px17(lon, lat):
    x = (lon + 180) / 360 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y
def tiles_for(xp, yp):
    x0, y0 = int(xp - CROP_W / 2), int(yp - CROP_H / 2)
    return {(tx, ty) for tx in range(x0 // 256, (x0 + CROP_W) // 256 + 1)
                     for ty in range(y0 // 256, (y0 + CROP_H) // 256 + 1)}

def fetch_one(t):
    x, y = t; p = f"{TILES17}/{x}/{y}.png"
    if os.path.exists(p) and os.path.getsize(p) > 500: return "skip"
    os.makedirs(f"{TILES17}/{x}", exist_ok=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(S3.format(x=x, y=y), headers={"User-Agent": "whg-z17"}), timeout=30) as r:
            data = r.read()
        if len(data) < 400: return "404"
        open(p, "wb").write(data); return "got"
    except Exception:
        return "fail"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ymin", type=int, required=True); ap.add_argument("--ymax", type=int, required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--names", default=None)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--font", action="store_true", help="EXPERIMENTAL: merge z17 font-classification into "
                    "types. OFF by default — validated to over-fire rare classes on crowd-point crops "
                    "(see plan-gb1900-typing.md §15 RESULT); reliable font-typing needs spotter boxes (pass-2).")
    ap.add_argument("--force", action="store_true", help="re-run even if band_*.done exists")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True); os.makedirs(IX1_ARCHIVE, exist_ok=True)
    band = f"{a.ymin}_{a.ymax}"
    donef = os.path.join(a.out, f"band_{band}.done")
    if os.path.exists(donef) and not a.force:
        print("band already done:", band, flush=True); return
    names = set(json.load(open(a.names)).get("names", [])) if a.names else None

    # 1. band pins + needed tiles
    pins = []; need = set(); t0 = time.time()
    for line in open(NT):
        try: d = json.loads(line)
        except Exception: continue
        lon, lat = d.get("lon"), d.get("lat")
        if lon is None or lat is None: continue
        if not (a.ymin <= z16y(lat) < a.ymax): continue
        xp, yp = px17(lon, lat)
        tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
        pins.append((d.get("pin_id"), tv, d.get("tier0_rule"), d.get("allcaps"), lon, lat))
        need |= tiles_for(xp, yp)
    # EDGE HANDLING: need = union of each pin's CROP-WINDOW tiles, so boundary labels pull tiles
    # from adjacent bands automatically (no clipping). Boundary tiles may be re-fetched by neighbours.
    print(f"band {band}: {len(pins)} pins, {len(need)} z17 tiles needed ({time.time()-t0:.0f}s)", flush=True)

    # 2. fetch needed tiles
    c = Counter(); tofetch = list(need)
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(fetch_one, tofetch)):
            c[r] += 1
            if i % 20000 == 0: print(f"  fetch {i}/{len(tofetch)} {dict(c)} ({time.time()-t0:.0f}s)", flush=True)
    print(f"fetch done {dict(c)} ({time.time()-t0:.0f}s)", flush=True)

    # 3. type each label: text typer + z17 font-classify (high-precision additive merge).
    #    Tiles are on /vast now (pre-drop) so we crop each label's z17 window, CRNN-embed, classify
    #    its OS typographic style (sheet-grounded), and merge reliable styles into the text top-3.
    ft = None
    if a.font:
        try:
            import data as DATA, crnn_data as CD
            from font_type import FontTyper, merge
            ft = FontTyper(MODEL_DIR, CLF_PATH, dev="cpu")
            print(f"font classifier loaded ({ft.classes}) ({time.time()-t0:.0f}s)", flush=True)
        except Exception as e:
            print(f"!! font classifier unavailable ({e}); text-only", flush=True); ft = None

    outp = os.path.join(a.out, f"band_{band}.jsonl"); n = 0; top = Counter(); fonts = Counter()
    BATCH = 256

    def flush(buf, w):
        # buf: list of (pid, tv, text_dist, lon, lat). Crop+classify those with a valid z17 crop.
        crops, texts, idx = [], [], []
        if ft is not None:
            for k, (pid, tv, td, lon, lat) in enumerate(buf):
                try: crop = DATA.crop_point(lon, lat, [TILES17])
                except Exception: crop = None
                if crop is not None:
                    crops.append(CD._to_h32(crop)); texts.append(tv); idx.append(k)
        preds = ft.classify(crops, texts) if (ft is not None and crops) else []
        pm = {idx[j]: preds[j] for j in range(len(preds))}
        for k, (pid, tv, td, lon, lat) in enumerate(buf):
            types = td
            if k in pm:
                st, cfd = pm[k]
                m = merge(td, st, cfd)
                if m != td: fonts[st] += 1
                types = m
            w.write(json.dumps({"pin_id": pid, "text": tv, "types": [[kk, pp] for kk, pp in types]}, ensure_ascii=False) + "\n")
            top[types[0][0]] += 1

    with open(outp, "w") as w:
        buf = []
        for pid, tv, rule, ac, lon, lat in pins:
            td = [[kk, pp] for kk, pp in assign_types(tv, rule, ac, names)]
            buf.append((pid, tv, td, lon, lat)); n += 1
            if len(buf) >= BATCH:
                flush(buf, w); buf = []
        flush(buf, w)
    print(f"typed {n} labels; top {top.most_common(6)}; font-merges {dict(fonts)} ({time.time()-t0:.0f}s)", flush=True)

    # 4. tar the band's z17 tiles to /ix1, then drop from /vast
    tarp = os.path.join(IX1_ARCHIVE, f"band_{band}.tar")
    fetched_tiles = [(x, y) for (x, y) in need if os.path.exists(f"{TILES17}/{x}/{y}.png")]
    with tarfile.open(tarp, "w") as tar:
        for x, y in fetched_tiles:
            tar.add(f"{TILES17}/{x}/{y}.png", arcname=f"{x}/{y}.png")
    print(f"tarred {len(fetched_tiles)} tiles -> {tarp} ({os.path.getsize(tarp)//1024//1024}MB)", flush=True)
    for x, y in fetched_tiles:
        try: os.remove(f"{TILES17}/{x}/{y}.png")
        except OSError: pass
    open(donef, "w").write(json.dumps(dict(pins=len(pins), tiles=len(fetched_tiles), tar=tarp)))
    print(f"BAND DONE {band} ({time.time()-t0:.0f}s)", flush=True)

if __name__ == "__main__":
    main()
