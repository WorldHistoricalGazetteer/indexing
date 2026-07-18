import os, re, json, time, math
import pandas as pd
from shapely import wkt
from rapidfuzz import fuzz
from PIL import Image
from tiling import build_mosaic, px_to_lonlat, lonlat_to_tile, Z

BASE="/vast/ishi/gb1900/probe/mapreader_text"
INST=f"{BASE}/install"
GB="/vast/ishi/gb1900/edition/national_typed.jsonl"
GRID=4
DPX=45          # pixel proximity (~65m at z16 UK)
TFUZЗ=65        # fuzzy threshold
SCORE_MIN=0.4
CROP_HALF=60    # ~120px crops
CAP=20          # max crops per direction per tile

SITES=[
 ("tile1_town_shrewsbury", -2.7550, 52.7071, "dense town centre (Shrewsbury)"),
 ("tile2_rural_pembridge", -2.8930, 52.2180, "open rural farmland (Herefordshire)"),
 ("tile3_coast_conwy",     -3.8300, 53.2800, "coastal/estuary (Conwy)"),
 ("tile4_moor_dartmoor",   -3.9600, 50.5700, "upland/moor (Dartmoor)"),
]

def norm(s):
    return re.sub(r"[^a-z0-9]","", (s or "").lower())

def safe(s):
    s=re.sub(r"[^A-Za-z0-9]+","_", (s or "").strip())[:40]
    return s or "blank"

# --- 1. build mosaics, record bboxes ---
mos={}
for name,lon,lat,desc in SITES:
    img,x0,y0,bbox,missing=build_mosaic(lon,lat,grid=GRID)
    fx,fy=lonlat_to_tile(lon,lat)
    p=f"{BASE}/tiles/{name}.png"; img.save(p)
    mos[name]=dict(lon=lon,lat=lat,desc=desc,img=img,x0=x0,y0=y0,bbox=bbox,
                   missing=missing,path=p,center_tile=(int(fx),int(fy)))
    print(f"[mosaic] {name} bbox={[round(b,4) for b in bbox]} missing={missing} center_z16={int(fx)}/{int(fy)}")

# --- 2. one streaming pass over GB1900, bucket points into bboxes ---
def in_bbox(lon,lat,b): return b[0]<=lon<=b[2] and b[1]<=lat<=b[3]
gb={n:[] for n in mos}
t=time.time(); nline=0
with open(GB) as f:
    for line in f:
        nline+=1
        try: d=json.loads(line)
        except: continue
        lon=d.get("lon"); lat=d.get("lat")
        if lon is None or lat is None: continue
        txt=d.get("text") or {}
        val=txt.get("value") if isinstance(txt,dict) else txt
        if not val: continue
        for n,m in mos.items():
            if in_bbox(lon,lat,m["bbox"]):
                gb[n].append((lon,lat,val))
print(f"[gb1900] scanned {nline} lines in {round(time.time()-t,1)}s; per-tile:",{n:len(v) for n,v in gb.items()})

# --- 3. run spotter on each mosaic ---
from mapreader import MapTextRunner
runner=MapTextRunner(pd.DataFrame(),
  cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
  weights_file=f"{INST}/weights/rumsey-finetune.pth", device="cpu")

os.makedirs(f"{BASE}/mismatches",exist_ok=True)
manifest=[]; summary={}
for name,m in mos.items():
    t=time.time()
    df=runner.run_on_image(m["path"], return_dataframe=True)
    df=df[df["image_id"].astype(str)==os.path.basename(m["path"])].reset_index(drop=True)
    df["score"]=pd.to_numeric(df["score"],errors="coerce").fillna(0.0)
    df=df[df["score"]>=SCORE_MIN].reset_index(drop=True)
    spotted=[]
    for _,r in df.iterrows():
        g0=r["pixel_geometry"]; poly=wkt.loads(g0) if isinstance(g0,str) else g0; c=poly.centroid
        lon,lat=px_to_lonlat(c.x,c.y,m["x0"],m["y0"])
        spotted.append(dict(text=str(r["text"]),score=float(r["score"]),
                            px=c.x,py=c.y,lon=lon,lat=lat))
    # GB points -> pixel coords
    gbpts=[]
    for lon,lat,val in gb[name]:
        fx,fy=lonlat_to_tile(lon,lat)
        px=(fx-m["x0"])*256; py=(fy-m["y0"])*256
        gbpts.append(dict(text=val,lon=lon,lat=lat,px=px,py=py))
    # match pairs
    matched_s=set(); matched_g=set(); npairs=0
    for i,s in enumerate(spotted):
        for j,g in enumerate(gbpts):
            dpx=math.hypot(s["px"]-g["px"], s["py"]-g["py"])
            if dpx<=DPX and fuzz.ratio(norm(s["text"]),norm(g["text"]))>=TFUZЗ:
                matched_s.add(i); matched_g.add(j); npairs+=1
    spotted_only=[s for i,s in enumerate(spotted) if i not in matched_s]
    gb_only=[g for j,g in enumerate(gbpts) if j not in matched_g]
    spotted_only.sort(key=lambda s:-s["score"])
    summary[name]=dict(desc=m["desc"],bbox=[round(b,5) for b in m["bbox"]],
        missing_tiles=m["missing"],n_spotted=len(spotted),n_gb1900=len(gbpts),
        n_matches=len(matched_g),n_spotted_only=len(spotted_only),n_gb1900_only=len(gb_only),
        spotted_texts=[s["text"] for s in spotted][:60],
        spotted_only_texts=[s["text"] for s in spotted_only][:40],
        gb1900_texts=[g["text"] for g in gbpts][:60],
        gb1900_only_texts=[g["text"] for g in gb_only][:40])
    # crops for mismatches (capped)
    def crop(cx,cy,fn):
        img=m["img"]; L=max(0,int(cx-CROP_HALF)); U=max(0,int(cy-CROP_HALF))
        R=min(img.width,int(cx+CROP_HALF)); D=min(img.height,int(cy+CROP_HALF))
        img.crop((L,U,R,D)).save(fn)
    for k,s in enumerate(spotted_only[:CAP]):
        fn=f"{BASE}/mismatches/{name}_spotted_only_{k:02d}_{safe(s['text'])}.png"
        crop(s["px"],s["py"],fn)
        manifest.append(dict(tile=name,kind="spotted_only",text=s['text'],score=round(s['score'],3),
            lon=round(s['lon'],6),lat=round(s['lat'],6),file=fn))
    for k,g in enumerate(gb_only[:CAP]):
        if not(0<=g['px']<m['img'].width and 0<=g['py']<m['img'].height): continue
        fn=f"{BASE}/mismatches/{name}_gb1900_only_{k:02d}_{safe(g['text'])}.png"
        crop(g["px"],g["py"],fn)
        manifest.append(dict(tile=name,kind="gb1900_only",text=g['text'],
            lon=round(g['lon'],6),lat=round(g['lat'],6),file=fn))
    print(f"[spot] {name} spotted={len(spotted)} gb={len(gbpts)} match={len(matched_g)} "
          f"s_only={len(spotted_only)} g_only={len(gb_only)} ({round(time.time()-t,1)}s)")

json.dump(summary,open(f"{BASE}/out/summary.json","w"),indent=2,ensure_ascii=False)
json.dump(manifest,open(f"{BASE}/mismatches/manifest.json","w"),indent=2,ensure_ascii=False)
print("DONE. crops:",len(manifest))
