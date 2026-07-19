"""Phase B step-1 test — does MapReader resolve COUNTY-name lettering (huge, widely spaced)? Parish-level
is already evidenced by the region output (HANWOOD/MEOLE/MAGNA at h~40-58 z16-px). Here we run the spotter
on mosaics centred on transcribed county-name labels and report detections (text, score, box height in
z16 px) + save a boxed visualization so a human can see whether the big lettering is captured (whole or
as letter fragments — either is usable for SIZE, since a fragment's height = the font cap-height)."""
import os, sys, json, numpy as np
import pandas as pd
from shapely import wkt
from PIL import Image, ImageDraw
sys.path.insert(0, "/vast/ishi/gb1900/probe/mapreader_text")
from tiling import build_mosaic, lonlat_to_tile

INST = "/vast/ishi/gb1900/probe/mapreader_text/install"
OUT = "/vast/ishi/gb1900/probe/mapreader_text/county_test"
os.makedirs(OUT, exist_ok=True)
# transcribed county-name labels + a couple of parish-name controls (known to work)
SITES = [("cheshire_a", -3.0719, 52.98679, "county"), ("cheshire_b", -2.14949, 53.42156, "county"),
         ("merionethshire", -3.991, 53.02736, "county")]
GRID = 6                      # 6x6 z16 tiles = 1536 px (county lettering is large)

from mapreader import MapTextRunner
runner = MapTextRunner(pd.DataFrame(),
    cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
    weights_file=f"{INST}/weights/rumsey-finetune.pth", device="cpu")

def minor(poly):
    p = np.array(poly.exterior.coords); p = p - p.mean(0)
    _, _, vt = np.linalg.svd(p, full_matrices=False); pr = p @ vt.T
    return pr[:, 1].max() - pr[:, 1].min(), pr[:, 0].max() - pr[:, 0].min()

for name, lon, lat, kind in SITES:
    try:
        img, x0, y0, bbox, missing = build_mosaic(lon, lat, grid=GRID)
    except Exception as e:
        print(f"=== {name}: mosaic build failed: {e} ===", flush=True); continue
    p = f"{OUT}/{name}.png"; img.save(p)
    df = runner.run_on_image(p, return_dataframe=True)
    df = df[df["image_id"].astype(str) == os.path.basename(p)].reset_index(drop=True)
    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0.0)
    df = df[df["score"] >= 0.3].reset_index(drop=True)
    fx, fy = lonlat_to_tile(lon, lat); cx = (fx - x0) * 256; cy = (fy - y0) * 256
    draw = ImageDraw.Draw(img); dets = []
    for _, r in df.iterrows():
        g0 = r["pixel_geometry"]; poly = wkt.loads(g0) if isinstance(g0, str) else g0
        h, w = minor(poly); c = poly.centroid
        d2 = ((c.x - cx) ** 2 + (c.y - cy) ** 2) ** 0.5
        draw.polygon([(x, y) for x, y in poly.exterior.coords], outline=(230, 0, 0))
        dets.append((round(float(r["score"]), 2), round(h, 1), round(w, 1), round(d2), str(r["text"])))
    draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], outline=(0, 90, 255), width=3)   # crowd-label centre
    img.save(f"{OUT}/{name}_boxes.png")
    dets.sort(key=lambda d: d[3])                    # nearest the label centre first
    print(f"=== {name} ({lon},{lat}) {kind} missing={missing} detections={len(dets)} ===", flush=True)
    print("  (score, box-h z16px, box-w, dist-to-label-px, text) — nearest first:", flush=True)
    for sc, h, w, dd, t in dets[:14]:
        print(f"   score={sc} h={h} w={w} dist={dd}px  {t!r}", flush=True)
print("DONE -> county_test/*_boxes.png", flush=True)
