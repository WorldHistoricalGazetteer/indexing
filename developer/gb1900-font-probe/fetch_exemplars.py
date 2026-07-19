"""Fetch/slice a TIGHT per-category exemplar for every OS Characteristic Sheet category, and build a
labelled contact sheet to verify the crops before assembling the verification table (GB-STAMP Phase A).

- Admin categories -> the single boundary-MARK capital letter (native-res IIIF region crop).
- Text categories  -> the transcribed example line (sliced from the existing block crops, no re-fetch).
Outputs reference/ex_<key>.jpg for each, plus reference/ex_contact.jpg (grid montage with labels).
    python fetch_exemplars.py
"""
import os, io, math, urllib.request, numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__)); REF = os.path.join(HERE, "reference")
I1897 = "12807%2F128076792"
IIIF = "https://map-view.nls.uk/iiif/{id}/{x},{y},{w},{h}/{sw},/0/native.jpg"

def fetch(x, y, w, h, sw=None):
    sw = sw or w
    url = IIIF.format(id=I1897, x=x, y=y, w=w, h=h, sw=sw)
    req = urllib.request.Request(url, headers={"User-Agent": "whg-cs"})
    return Image.open(io.BytesIO(urllib.request.urlopen(req, timeout=30).read())).convert("RGB")

def autocrop(im, pad=10, thr_frac=0.14):
    """Tighten to the ink bounding box. Density thresholds (fraction of peak row/col ink) drop the
    sparse dotted leader lines + surrounding whitespace, centring the exemplar on its glyphs."""
    g = np.asarray(im.convert("L"), np.float32); ink = g < 125
    if ink.sum() < 15: return im
    cols = ink.sum(0).astype(float); rows = ink.sum(1).astype(float)
    cthr = max(2.0, thr_frac * cols.max()); rthr = max(2.0, thr_frac * rows.max())
    cx = np.where(cols >= cthr)[0]; ry = np.where(rows >= rthr)[0]
    if len(cx) == 0 or len(ry) == 0: return im
    H, W = ink.shape
    return im.crop((max(0, int(cx[0]) - pad), max(0, int(ry[0]) - pad),
                    min(W, int(cx[-1]) + pad + 1), min(H, int(ry[-1]) + pad + 1)))

# ---- ADMIN single-letter exemplars: native crop boxes (generous, native res) ----
# key, transcribed label, letter, (x,y,w,h), style, caps_only, date_regime, note
ADMIN = [
 ("county_names","County Names","C",(1480,1440,175,150),"caps",True,"both",""),
 ("hundreds","Hundreds","H",(1495,1670,150,140),"caps",True,"pre1879",""),
 ("liberties","Liberties","L",(1495,1875,150,120),"italic",True,"both",""),
 ("parishes_ancient","Parishes (Mother or Ancient)","P",(1495,2000,150,130),"caps",True,"pre1879",""),
 ("civil_parishes","Civil Parishes or Townships","T",(1495,2175,150,130),"caps",True,"both",""),
 ("div_townships","Divisions of Townships","T",(1480,2330,175,130),"italic",True,"pre1879","bold italic"),
 ("subdiv_townships","Subdivisions of Do.","T",(1480,2430,175,120),"italic",True,"pre1879",""),
 ("boroughs_parl","Boroughs (Parliamentary)","B",(1495,2555,150,130),"caps",True,"both",""),
 ("boroughs_munic","Boroughs (Municipal)","B",(1495,2655,150,120),"caps",True,"both",""),
 ("towns_generally","Towns, generally","B",(1480,2750,175,120),"italic",True,"both",""),
 ("town_districts","Town Districts","D",(1480,2855,175,130),"italic",True,"recent","'more recent maps' (>=1879)"),
 ("div_counties","Divisions of Counties (Ridings)","R",(3820,1465,205,185),"caps",True,"pre1879",""),
 ("poor_law_unions","Poor Law Unions","R",(3830,1755,165,130),"caps",True,"both","old Yorks/Lancs = Registrars Districts"),
 ("urban_sanitary","Urban Sanitary Districts","R",(3830,1900,165,130),"caps",True,"both",""),
 ("cities_mp","Cities returning Members","C",(3830,2070,165,130),"caps",True,"pre1879","bold"),
 ("cities_nomp","Cities not returning Members","C",(3830,2205,165,130),"caps",True,"pre1879",""),
 ("wards","Wards","W",(3830,2370,165,125),"caps",True,"both",""),
 ("market_towns","Market Towns","B",(3825,2525,170,120),"italic",True,"pre1879",""),
 ("other_towns","Other Towns","B",(3825,2670,170,120),"italic",True,"pre1879",""),
 ("parl_div_counties","Parliamentary Division of Counties","P",(3820,2795,185,155),"caps",True,"recent","'more recent maps' (>=1879)"),
 ("county_boroughs","County Boroughs","E",(3820,2935,185,155),"caps",True,"recent","'more recent maps' (>=1879)"),
]

# ---- TEXT exemplars: slice from existing block crops (block file, native crop the block was taken at,
#      and the fractional y-band [y0,y1] (and optional x-band) of the line within the block image) ----
# block native boxes: settlement=(1860,1500,1000,520 -> img 600x312); water=(1500,2180,1300,360 -> 900x249);
# antiq=(2430,2660,900,300 -> 900x300); railways=(1640,2800,1260,230 -> 900x164)
TEXT = [
 # key, label, block, (y0,y1) frac, (x0,x1) frac, style, caps_only, date_regime, note
 ("parish_churches","Parish Churches & Villages","cs_settlement.jpg",(0.00,0.16),(0,1),"upright",False,"both",""),
 ("chapelries","Chapelries. Other Churches","cs_settlement.jpg",(0.15,0.30),(0,1),"italic",False,"pre1879",""),
 ("other_villages","Other Villages","cs_settlement.jpg",(0.30,0.44),(0,1),"italic",False,"both",""),
 ("parks_demesnes","Parks and Demesnes","cs_settlement.jpg",(0.44,0.585),(0,1),"caps",True,"both","bold caps"),
 ("gentlemens_seats","Gentlemens Seats","cs_settlement.jpg",(0.585,0.71),(0,1),"italic",False,"both",""),
 ("manufactories","Manufactories. Mines. Farms. Locks","cs_settlement.jpg",(0.71,0.85),(0,1),"italic",False,"both",""),
 ("workhouses","Workhouses","cs_settlement.jpg",(0.85,1.0),(0,1),"upright",False,"both",""),
 ("bays_harbours","Bays and Harbours","cs_water.jpg",(0.08,0.34),(0,1),"caps",True,"both","UPRIGHT caps (NOT italic)"),
 ("navigable_rivers","Navigable Rivers and Canals","cs_water.jpg",(0.34,0.58),(0,1),"italic",True,"both","italic CAPS"),
 ("small_rivers","Small Rivers & Brooks","cs_water.jpg",(0.58,0.80),(0.16,0.98),"italic",False,"both","italic lower-case"),
 ("antiq_roman","Antiquities: Roman","cs_antiq.jpg",(0.05,0.27),(0.26,0.74),"caps",True,"both","SAME face as road names"),
 ("antiq_saxon","Antiquities: Pre-historic or Saxon","cs_antiq.jpg",(0.28,0.45),(0.26,0.80),"blackletter",False,"both",""),
 ("antiq_norman","Antiquities: Norman","cs_antiq.jpg",(0.45,0.64),(0.26,0.52),"blackletter",False,"both","Old-English blackletter"),
 ("antiq_subsequent","Antiquities: (Norman) or Subsequent","cs_antiq.jpg",(0.45,0.64),(0.49,0.82),"italic",False,"both",""),
]

# ---- extra text exemplars needing their own native fetch (not in existing blocks) ----
# key,label,(x,y,w,h),sw,style,caps_only,date_regime,note
EXTRA = [
 ("bogs_moors","Bogs, Moors and Forests",(1550,2530,1270,95),640,"caps",True,"pre1879","* SIZE-VARIABLE"),
 ("woods_copses","Woods and Copses",(1690,2620,980,58),600,"upright",False,"both",""),
 ("ranges_hills","Ranges of Hills",(1355,2676,650,82),560,"caps",True,"both","* SIZE-VARIABLE"),
 ("extra_parochial","Extra Parochial",(1710,2842,880,92),580,"caps",True,"pre1879",""),
 ("turnpike_trusts","Turnpike Trusts",(2435,2842,1010,92),620,"italic",True,"pre1879","bold italic caps"),
 ("railways_passenger","Railways (Passenger)",(1720,2946,850,92),560,"upright",False,"both",""),
 ("railways_mineral","Railways (Mineral)",(2465,2946,950,92),600,"italic",False,"both","shares water italic"),
 ("principal_stations","Principal Stations",(1500,3065,810,95),540,"upright",False,"both",""),
 ("other_stations","Other Stations",(2560,3065,760,95),520,"upright",False,"both",""),
]

def slice_block(block, yb, xb):
    im = Image.open(os.path.join(REF, block)).convert("RGB")
    W, H = im.size
    return im.crop((int(xb[0]*W), int(yb[0]*H), int(xb[1]*W), int(yb[1]*H)))

def main():
    saved = []
    for key, label, letter, box, *_ in ADMIN:
        try:
            im = autocrop(fetch(*box), pad=8); im.save(os.path.join(REF, f"ex_{key}.jpg")); saved.append((key, label + f"  [{letter}]"))
        except Exception as e:
            print("ADMIN ERR", key, e)
    for key, label, block, yb, xb, *_ in TEXT:
        try:
            im = autocrop(slice_block(block, yb, xb), pad=6); im.save(os.path.join(REF, f"ex_{key}.jpg")); saved.append((key, label))
        except Exception as e:
            print("TEXT ERR", key, e)
    for key, label, box, sw, *_ in EXTRA:
        try:
            im = autocrop(fetch(*box, sw=sw), pad=8); im.save(os.path.join(REF, f"ex_{key}.jpg")); saved.append((key, label))
        except Exception as e:
            print("EXTRA ERR", key, e)

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

if __name__ == "__main__":
    main()
