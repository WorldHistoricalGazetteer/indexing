import os, sys, json, time
# limit threads BEFORE importing torch
NTHREADS = int(os.environ.get("WORKER_THREADS", "4"))
for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[v] = str(NTHREADS)
import pandas as pd
from shapely import wkt
import torch
torch.set_num_threads(NTHREADS)
import region_common as rc

WID = int(sys.argv[1])
NW = int(sys.argv[2])
INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
SCORE_MIN = 0.4

from mapreader import MapTextRunner
runner = MapTextRunner(
    pd.DataFrame(),
    cfg_file=INST + "/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
    weights_file=INST + "/weights/rumsey-finetune.pth",
    device="cpu")

os.makedirs(rc.MOSAIC_DIR, exist_ok=True)
os.makedirs(rc.BASE + "/boxes", exist_ok=True)
outp = rc.BASE + "/boxes/worker%d.jsonl" % WID
mosaics = [m for k, m in enumerate(rc.all_mosaics()) if k % NW == WID]
print("[w%d] assigned %d mosaics, threads=%d" % (WID, len(mosaics), NTHREADS), flush=True)

nboxes = 0
with open(outp, "w") as out:
    for idx, (i, j) in enumerate(mosaics):
        t = time.time()
        img, tx0, ty0, missing = rc.build_mosaic(i, j)
        mfile = rc.MOSAIC_DIR + "/m_%02d_%02d.png" % (i, j)
        img.save(mfile)
        gx0, gy0 = tx0 * 256, ty0 * 256
        # fresh runner state per image not needed; we filter by image_id
        df = runner.run_on_image(mfile, return_dataframe=True)
        df = df[df["image_id"].astype(str) == os.path.basename(mfile)].reset_index(drop=True)
        df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0.0)
        df = df[df["score"] >= SCORE_MIN]
        for _, r in df.iterrows():
            g0 = r["pixel_geometry"]
            poly = wkt.loads(g0) if isinstance(g0, str) else g0
            c = poly.centroid
            # local px -> global px
            gpoly = [[float(x) + gx0, float(y) + gy0] for x, y in poly.exterior.coords]
            gcx, gcy = c.x + gx0, c.y + gy0
            lon, lat = rc.global_px_to_lonlat(gcx, gcy)
            tile_x = int(gcx // 256); tile_y = int(gcy // 256)
            rec = dict(text=str(r["text"]), score=round(float(r["score"]), 4),
                       gpoly=gpoly, gcx=round(gcx, 2), gcy=round(gcy, 2),
                       lon=round(lon, 6), lat=round(lat, 6),
                       tile_x=tile_x, tile_y=tile_y,
                       mfile=mfile, lcx=round(c.x, 1), lcy=round(c.y, 1),
                       mi=i, mj=j)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            nboxes += 1
        out.flush()
        print("[w%d] %d/%d mosaic(%d,%d) miss=%d boxes+=%d %.1fs" % (
            WID, idx + 1, len(mosaics), i, j, missing, len(df), time.time() - t), flush=True)
print("[w%d] DONE boxes=%d -> %s" % (WID, nboxes, outp), flush=True)
