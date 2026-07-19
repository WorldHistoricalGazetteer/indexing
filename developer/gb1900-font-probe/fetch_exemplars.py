"""Fetch/slice a TIGHT per-category exemplar for every OS Characteristic Sheet category, and build a
labelled contact sheet to verify the crops before assembling the verification table (GB-STAMP Phase A).

- Admin categories -> the single boundary-MARK capital letter (native-res IIIF region crop).
- Text categories  -> the transcribed example line (sliced from the existing block crops, no re-fetch).
Outputs reference/ex_<key>.jpg for each, plus reference/ex_contact.jpg (grid montage with labels).
    python fetch_exemplars.py
"""
import os, io, math, json, urllib.request, numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi

HERE = os.path.dirname(os.path.abspath(__file__)); REF = os.path.join(HERE, "reference")
I1897 = "12807%2F128076792"; I1923 = "12807%2F128076894"
ID = {I1897: "12807/128076792", I1923: "12807/128076894"}   # decoded ids for the manifest/modal
IIIF = "https://map-view.nls.uk/iiif/{id}/{x},{y},{w},{h}/{sw},/0/native.jpg"
# native (x,y,w,h) of the pre-cropped block images (all 1897 sheet)
BLOCK = {"cs_settlement.jpg": (1860, 1500, 1000, 520), "cs_water.jpg": (1500, 2180, 1300, 360),
         "cs_antiq.jpg": (2430, 2660, 900, 300)}

def fetch(x, y, w, h, sw=None, sheet=I1897):
    sw = sw or w
    url = IIIF.format(id=sheet, x=x, y=y, w=w, h=h, sw=sw)
    req = urllib.request.Request(url, headers={"User-Agent": "whg-cs"})
    return Image.open(io.BytesIO(urllib.request.urlopen(req, timeout=30).read())).convert("RGB")

def autocrop(im, pad=11, thr_frac=0.07):
    """Tighten to the ink bounding box. Returns (cropped_im, box) where box=(l,t,r,b) in im pixels.
    A LOW density threshold keeps faint serif tops/tails so the FULL glyph height survives."""
    g = np.asarray(im.convert("L"), np.float32); ink = g < 125
    if ink.sum() < 15: return im, (0, 0, im.width, im.height)
    cols = ink.sum(0).astype(float); rows = ink.sum(1).astype(float)
    cthr = max(2.0, thr_frac * cols.max()); rthr = max(2.0, thr_frac * rows.max())
    cx = np.where(cols >= cthr)[0]; ry = np.where(rows >= rthr)[0]
    if len(cx) == 0 or len(ry) == 0: return im, (0, 0, im.width, im.height)
    H, W = ink.shape
    box = (max(0, int(cx[0]) - pad), max(0, int(ry[0]) - pad),
           min(W, int(cx[-1]) + pad + 1), min(H, int(ry[-1]) + pad + 1))
    return im.crop(box), box

def mask_leaders(im, dot_frac=0.42, gap_mult=2.4, min_run=4):
    """Paint out DOTTED LEADER lines (the '.....' joining a label to its letter). A leader is a run of
    >=min_run small ink blobs at a common y with small, regular x-gaps — distinct from text periods
    (few, word-spaced) and from glyph strokes (large). Safe to run on every exemplar."""
    g = np.asarray(im.convert("L"), np.float32); ink = g < 128
    lbl, n = ndi.label(ink)
    if n < min_run: return im
    comps = []
    for i, s in enumerate(ndi.find_objects(lbl)):
        if not s: continue
        h = s[0].stop - s[0].start; w = s[1].stop - s[1].start
        comps.append((i + 1, h, w, (s[0].start + s[0].stop) / 2.0, (s[1].start + s[1].stop) / 2.0))
    if not comps: return im
    maxh = max(c[1] for c in comps)
    small = [c for c in comps if c[1] <= dot_frac * maxh and c[2] <= dot_frac * maxh]
    remove = set()
    for c in small:                                    # dots sharing c's y-band, left-to-right
        band = sorted((d for d in small if abs(d[3] - c[3]) <= 0.35 * maxh), key=lambda d: d[4])
        run = [band[0]]
        for d in band[1:]:
            if d[4] - run[-1][4] <= gap_mult * max(2, run[-1][2]): run.append(d)
            else:
                if len(run) >= min_run: remove.update(x[0] for x in run)
                run = [d]
        if len(run) >= min_run: remove.update(x[0] for x in run)
    if not remove: return im
    arr = np.array(im.convert("RGB")); arr[np.isin(lbl, list(remove))] = 255
    return Image.fromarray(arr)

def crop_letter(im, pad_frac=0.26, thr=128, close=7):
    """For a single boundary-mark LETTER: LARGEST connected ink blob (strokes joined by a close), crop to
    its bbox + generous margin. Returns (cropped_im, box). Full letter, no stray symbol / leaders."""
    g = np.asarray(im.convert("L"), np.float32); ink = g < thr
    if ink.sum() < 20: return im, (0, 0, im.width, im.height)
    ink = ndi.binary_closing(ink, structure=np.ones((close, close)))
    lbl, n = ndi.label(ink)
    if n == 0: return im, (0, 0, im.width, im.height)
    sizes = ndi.sum(np.ones_like(lbl, np.float32), lbl, index=range(1, n + 1))
    big = int(np.argmax(sizes)) + 1
    ys, xs = np.where(lbl == big)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    pad = int(pad_frac * (y1 - y0 + 1)) + 3; H, W = ink.shape
    box = (max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad + 1), min(H, y1 + pad + 1))
    return im.crop(box), box

# ---- ADMIN single-letter exemplars: native crop boxes (generous, native res) ----
# key, transcribed label, letter, (x,y,w,h), style, caps_only, date_regime, note
ADMIN = [
 ("county_names","County Names","C",(1475,1425,185,170),"caps",True,"both",""),
 ("hundreds","Hundreds","H",(1478,1658,185,168),"caps",True,"pre1879",""),
 ("liberties","Liberties","L",(1478,1860,185,150),"italic",True,"both",""),
 ("parishes_ancient","Parishes (Mother or Ancient)","P",(1495,2000,150,130),"caps",True,"pre1879",""),
 ("civil_parishes","Civil Parishes or Townships","T",(1495,2175,150,130),"caps",True,"both",""),
 ("div_townships","Divisions of Townships","T",(1480,2330,175,130),"italic",True,"pre1879","bold italic"),
 ("subdiv_townships","Subdivisions of Do.","T",(1480,2430,175,120),"italic",True,"pre1879",""),
 ("boroughs_parl","Boroughs (Parliamentary)","B",(1495,2552,150,98),"caps",True,"both",""),
 ("boroughs_munic","Boroughs (Municipal)","B",(1488,2644,165,108),"caps",True,"both",""),
 ("towns_generally","Towns, generally","B",(1480,2750,175,120),"italic",True,"both",""),
 ("town_districts","Town Districts","D",(1480,2855,175,130),"italic",True,"recent","'more recent maps' (>=1879)"),
 ("div_counties","Divisions of Counties (Ridings)","R",(3805,1448,222,212),"caps",True,"pre1879",""),
 ("poor_law_unions","Poor Law Unions","R",(3810,1736,158,152),"caps",True,"both","old Yorks/Lancs = Registrars Districts"),
 ("urban_sanitary","Urban Sanitary Districts","R",(3872,1896,100,104),"caps",True,"both",""),
 ("cities_mp","Cities returning Members","C",(3822,2058,158,138),"caps",True,"pre1879","bold"),
 ("cities_nomp","Cities not returning Members","C",(3822,2194,158,138),"caps",True,"pre1879",""),
 ("wards","Wards","W",(3822,2360,158,134),"caps",True,"both",""),
 ("market_towns","Market Towns","B",(3818,2515,160,124),"italic",True,"pre1879",""),
 ("other_towns","Other Towns","B",(3816,2658,162,112),"italic",True,"pre1879",""),
 ("parl_div_counties","Parliamentary Division of Counties","P",(3806,2782,168,168),"caps",True,"recent","'more recent maps' (>=1879)"),
 ("county_boroughs","County Boroughs","E",(3812,2928,150,160),"caps",True,"recent","'more recent maps' (>=1879)"),
]

# ---- TEXT exemplars: slice from existing block crops (block file, native crop the block was taken at,
#      and the fractional y-band [y0,y1] (and optional x-band) of the line within the block image) ----
# block native boxes: settlement=(1860,1500,1000,520 -> img 600x312); water=(1500,2180,1300,360 -> 900x249);
# antiq=(2430,2660,900,300 -> 900x300); railways=(1640,2800,1260,230 -> 900x164)
TEXT = [
 # key, label, block, (y0,y1) frac, (x0,x1) frac, style, caps_only, date_regime, note
 ("parish_churches","Parish Churches & Villages","cs_settlement.jpg",(0.00,0.16),(0,1),"upright",False,"both",""),
 ("chapelries","Chapelries. Other Churches","cs_settlement.jpg",(0.15,0.30),(0.205,1),"italic",False,"pre1879",""),
 ("other_villages","Other Villages","cs_settlement.jpg",(0.30,0.44),(0,1),"italic",False,"both",""),
 ("parks_word","Parks","cs_settlement.jpg",(0.44,0.585),(0.12,0.395),"caps",True,"both","bold caps"),
 ("demesnes_word","Demesnes","cs_settlement.jpg",(0.44,0.585),(0.50,0.94),"caps",True,"both","bold caps"),
 ("gentlemens_seats","Gentlemens Seats","cs_settlement.jpg",(0.585,0.71),(0,1),"italic",False,"both",""),
 ("manufactories","Manufactories. Mines. Farms. Locks","cs_settlement.jpg",(0.71,0.85),(0,1),"italic",False,"both",""),
 ("workhouses","Workhouses","cs_settlement.jpg",(0.845,1.0),(0.30,0.72),"upright",False,"both",""),
 ("bays_word","Bays","cs_water.jpg",(0.08,0.34),(0.185,0.52),"caps",True,"both","UPRIGHT caps (NOT italic)"),
 ("harbours_word","Harbours","cs_water.jpg",(0.08,0.34),(0.63,0.995),"caps",True,"both","UPRIGHT caps (NOT italic)"),
 ("navigable_rivers_word","Navigable Rivers","cs_water.jpg",(0.34,0.58),(0.10,0.78),"italic",True,"both","italic CAPS"),
 ("small_rivers","Small Rivers & Brooks","cs_water.jpg",(0.58,0.80),(0.16,0.98),"italic",False,"both","italic title-case"),
 ("antiq_roman","Antiquities: Roman","cs_antiq.jpg",(0.03,0.25),(0.315,0.70),"caps",True,"both","SAME face as road names"),
 ("antiq_saxon","Antiquities: Pre-historic or Saxon","cs_antiq.jpg",(0.255,0.42),(0.30,0.80),"blackletter",False,"both",""),
 ("antiq_norman","Antiquities: Norman","cs_antiq.jpg",(0.42,0.63),(0.28,0.47),"blackletter",False,"both","Old-English blackletter; excl. 'or'"),
 ("antiq_subsequent","Antiquities: Subsequent","cs_antiq.jpg",(0.42,0.63),(0.49,0.87),"italic",False,"both","excl. 'or'"),
]

# ---- extra text exemplars needing their own native fetch (not in existing blocks) ----
# key,label,(x,y,w,h),sw,style,caps_only,date_regime,note
EXTRA = [
 ("bogs_moors_word","Bogs, Moors",(1972,2518,408,66),408,"caps",True,"pre1879","* SIZE-VARIABLE; excl. † + 'and'"),
 ("forests_word","Forests",(2496,2516,300,66),300,"caps",True,"pre1879","* SIZE-VARIABLE"),
 ("woods_copses","Woods and Copses",(2138,2608,478,70),478,"upright",False,"both",""),
 ("ranges_hills","Ranges of Hills",(1748,2658,362,56),362,"caps",True,"both","* SIZE-VARIABLE (header)"),
 ("extra_parochial","Extra Parochial",(1745,2842,608,92),560,"caps",True,"pre1879","excl. †"),
 ("turnpike_trusts","Turnpike Trusts",(2435,2842,678,92),540,"italic",True,"pre1879","bold italic caps; excl. †"),
 ("railways_passenger","Railways (Passenger)",(1720,2946,708,92),540,"upright",False,"both","RAILWAYS upright caps + (Passenger) italic"),
 ("railways_mineral","Railways (Mineral)",(2465,2946,672,92),540,"italic",False,"both","RAILWAYS italic caps + (Mineral) italic"),
 ("principal_stations","Principal Stations",(1500,3062,860,95),580,"upright",False,"both",""),
 ("other_stations","Other Stations",(2560,3062,780,95),520,"italic",False,"both",""),
]

# ---- pending crops (SG review) + 1923 sheet: (key, (x,y,w,h), sw, sheet, pad) ----
PEND = [
 ("county_bridges_word",(1735,2072,508,46),508,I1897,6),    # 'County Bridges' (upright)
 ("trust_bridges_word",(2262,2072,545,46),500,I1897,6),     # 'Trust Bridges and Others' (italic)
 ("isolated_houses_word",(2218,2142,352,50),352,I1897,7),   # 'Isolated Houses' (italic)
 ("canals_word",(2640,2298,470,95),440,I1897,10),           # 'CANALS' (cut off in the water block)
 ("contour_numeral",(3852,2632,200,64),200,I1923,5),        # '...200' contour numeral (shared font w/ spot-heights + B.M.)
]

def slice_block(block, yb, xb):
    im = Image.open(os.path.join(REF, block)).convert("RGB"); W, H = im.size
    l, t, r, b = int(xb[0] * W), int(yb[0] * H), int(xb[1] * W), int(yb[1] * H)
    bnx, bny, bnw, bnh = BLOCK[block]; sx, sy = bnw / W, bnh / H
    return im.crop((l, t, r, b)), (bnx + l * sx, bny + t * sy, (r - l) * sx, (b - t) * sy)

def native_of(src, img_size, box):
    """src=(nx,ny,nw,nh) native region of `im`; box=(l,t,r,b) crop in im px -> native [x,y,w,h]."""
    nx, ny, nw, nh = src; W, H = img_size; sx, sy = nw / W, nh / H
    return [round(nx + box[0] * sx), round(ny + box[1] * sy), round((box[2] - box[0]) * sx), round((box[3] - box[1]) * sy)]

def main():
    saved = []; manifest = {}
    def record(key, sheet, src, img_size, box):
        x, y, w, h = native_of(src, img_size, box)
        manifest[f"ex_{key}"] = {"id": ID[sheet], "x": x, "y": y, "w": w, "h": h}
    for key, label, letter, box, *_ in ADMIN:
        try:
            img = mask_leaders(fetch(*box)); crop, cb = crop_letter(img); crop.save(os.path.join(REF, f"ex_{key}.jpg"))
            record(key, I1897, box, img.size, cb); saved.append((key, label + f"  [{letter}]"))
        except Exception as e:
            print("ADMIN ERR", key, e)
    for key, label, block, yb, xb, *_ in TEXT:
        try:
            sub, snat = slice_block(block, yb, xb); sub = mask_leaders(sub); crop, cb = autocrop(sub, pad=6)
            crop.save(os.path.join(REF, f"ex_{key}.jpg")); record(key, I1897, snat, sub.size, cb); saved.append((key, label))
        except Exception as e:
            print("TEXT ERR", key, e)
    for key, label, box, sw, *_ in EXTRA:
        try:
            img = mask_leaders(fetch(*box, sw=sw)); crop, cb = autocrop(img, pad=8); crop.save(os.path.join(REF, f"ex_{key}.jpg"))
            record(key, I1897, box, img.size, cb); saved.append((key, label))
        except Exception as e:
            print("EXTRA ERR", key, e)
    for key, box, sw, sheet, pad in PEND:
        try:
            img = mask_leaders(fetch(*box, sw=sw, sheet=sheet)); crop, cb = autocrop(img, pad=pad); crop.save(os.path.join(REF, f"ex_{key}.jpg"))
            record(key, sheet, box, img.size, cb); saved.append((key, key))
        except Exception as e:
            print("PEND ERR", key, e)
    json.dump(manifest, open(os.path.join(REF, "ex_manifest.json"), "w"), indent=1)

    # contact sheet: grid of all exemplars, each scaled to a cell with its label
    cols = 5; cw, ch = 300, 150; pad = 6; lab_h = 26
    rows = math.ceil(len(saved) / cols)
    sheet = Image.new("RGB", (cols*(cw+pad)+pad, rows*(ch+lab_h+pad)+pad), (245, 243, 238))
    d = ImageDraw.Draw(sheet)
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except Exception: font = ImageFont.load_default()
    for i, (key, label) in enumerate(saved):
        r, c = divmod(i, cols); x0 = pad + c*(cw+pad); y0 = pad + r*(ch+lab_h+pad)
        im = Image.open(os.path.join(REF, f"ex_{key}.jpg")).convert("RGB")
        im.thumbnail((cw, ch)); cell = Image.new("RGB", (cw, ch), (255, 255, 255))
        cell.paste(im, ((cw-im.width)//2, (ch-im.height)//2)); sheet.paste(cell, (x0, y0))
        d.rectangle([x0, y0, x0+cw, y0+ch], outline=(180, 175, 165))
        d.text((x0+3, y0+ch+4), f"{i+1}. {label[:44]}", fill=(30, 25, 20), font=font)
    sheet.save(os.path.join(REF, "ex_contact.jpg"))
    print(f"saved {len(saved)} exemplars; contact sheet reference/ex_contact.jpg ({sheet.size})")

def regen_from(path):
    """Regenerate the exemplars the human ADJUSTED in the crop modal (cs_decisions.json). Each adjusted
    box is re-fetched and AUTO-TRIMMED to the ink (SG: leave a loose margin, we trim for exactness).
    Updates reference/ex_manifest.json in place so the table + modal reflect the new bounds."""
    data = json.load(open(path)); rowsd = data.get("decisions", data) if isinstance(data, dict) else data
    mpath = os.path.join(REF, "ex_manifest.json")
    manifest = json.load(open(mpath)) if os.path.exists(mpath) else {}
    nreg = 0
    for row in rowsd:
        for cr in (row.get("crops") or []):
            if not cr.get("adjusted"): continue
            key = cr["key"]; enc = cr["id"].replace("/", "%2F")
            x, y, w, h = int(cr["x"]), int(cr["y"]), int(cr["w"]), int(cr["h"])
            sw = min(w, int((250000.0 * w / max(1, h)) ** 0.5)) or w
            img = mask_leaders(fetch(x, y, w, h, sw=sw, sheet=enc))
            crop, cb = autocrop(img, pad=3)                 # mask leaders + trim the white margin to the ink
            crop.save(os.path.join(REF, f"{key}.jpg"))
            nx, ny, nw, nh = native_of((x, y, w, h), img.size, cb)
            manifest[key] = {"id": cr["id"], "x": nx, "y": ny, "w": nw, "h": nh}; nreg += 1
    json.dump(manifest, open(mpath, "w"), indent=1)
    print(f"regenerated {nreg} adjusted exemplars (auto-trimmed to ink); updated ex_manifest.json")

def remask_files():
    """Mask dotted leaders on the EXISTING exemplar files IN PLACE (no re-fetch, so user crop adjustments
    are preserved), re-trim, and update ex_manifest.json bounds."""
    mpath = os.path.join(REF, "ex_manifest.json"); manifest = json.load(open(mpath)); nchg = 0
    for key, c in list(manifest.items()):
        p = os.path.join(REF, f"{key}.jpg")
        if not os.path.exists(p): continue
        im = Image.open(p).convert("RGB"); masked = mask_leaders(im)
        if masked is im: continue                              # no leader run found
        crop, cb = autocrop(masked, pad=3); crop.save(p)
        W, H = im.size; sx, sy = c["w"] / W, c["h"] / H
        manifest[key] = {"id": c["id"], "x": round(c["x"] + cb[0] * sx), "y": round(c["y"] + cb[1] * sy),
                         "w": round((cb[2] - cb[0]) * sx), "h": round((cb[3] - cb[1]) * sy)}; nchg += 1
    json.dump(manifest, open(mpath, "w"), indent=1)
    print(f"masked leaders on {nchg} exemplars (in place); updated ex_manifest.json")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-decisions", help="cs_decisions.json with adjusted crops -> regenerate those (trimmed)")
    ap.add_argument("--remask", action="store_true", help="mask dotted leaders on existing files in place")
    a = ap.parse_args()
    if a.from_decisions: regen_from(a.from_decisions)
    elif a.remask: remask_files()
    else: main()
