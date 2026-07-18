"""Harvest REAL (crop -> transcript) pairs from spotter boxes for CRNN training (B').
Crops are paper-flattened + resized to H=32 (variable width). Labels are the spotter's own text.
Also carries cap_height_m (size) so the SAME pool aligns with the HITL anchor ids for eval.
"""
import glob, json, math, numpy as np
from scipy import ndimage as ndi
import data as DATA

H = 32

def cap_h_m(gpoly):
    ys = [p[1] for p in gpoly]
    yy = (min(ys) + max(ys)) / 2 / 256.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / (2**16)))))
    return (max(ys) - min(ys)) * 40075016.686 * math.cos(math.radians(lat)) / (2**24)

def _to_h32(crop01):
    if crop01.shape[0] != H:
        crop01 = ndi.zoom(crop01, (H / crop01.shape[0], H / crop01.shape[0]), order=1)
    return np.clip(crop01, 0, 1).astype(np.float32)

def clean_text(t):
    return (t or "").strip()

def harvest(boxes_glob, tile_dirs, nmax=100000, min_len=1, max_len=24, rng=None, scale=1):
    """-> list of dict(img[H,W] 0..1, text, cap_h_m). scale=2 crops z16 boxes from z17 tiles."""
    boxes = []
    for f in glob.glob(boxes_glob):
        for line in open(f):
            line = line.strip()
            if line: boxes.append(json.loads(line))
    if rng is not None:
        rng.shuffle(boxes)
    out = []
    for b in boxes:
        if len(out) >= nmax: break
        t = clean_text(b.get("text"))
        if not (min_len <= len(t) <= max_len):
            continue
        c = DATA.crop_box(b["gpoly"], tile_dirs, scale=scale)
        if c is None:
            continue
        out.append(dict(img=_to_h32(c), text=t, cap_h_m=cap_h_m(b["gpoly"]) * scale, style=None))
    return out

import math as _math, re as _re
# high-precision blackletter antiquity terms (verified overwhelmingly blackletter in the z17 montage);
# deliberately excludes Church/Castle/Roman/Cross/Abbey (mixed serif/road) to keep the auto-label clean
_ANTIQ = _re.compile(r"\b(Tumulus|Tumuli|Cairn|Cairns|Camp|Earthwork|Earthworks|Barrow|Barrows|"
                     r"Motte|Cist|Enclosure|Entrenchment)\b", _re.I)
def _z16blk(lon, lat):
    x = int((lon + 180) / 360 * (2**16))
    y = int((1 - _math.log(_math.tan(_math.radians(lat)) + 1 / _math.cos(_math.radians(lat))) / _math.pi) / 2 * (2**16))
    return x // 8, y // 8

def auto_style(text):
    up = text.upper(); al = [c for c in text if c.isalpha()]
    if text.replace(".", "").replace(",", "").isdigit(): return "numeral"
    if any(up.endswith(s) or (" " + s) in (" " + up) for s in ("ROAD", "STREET", "LANE", "TERRACE", "AVENUE")):
        return "road_caps"                                    # road wins over antiquity ("ROMAN ROAD")
    if _ANTIQ.search(text): return "blackletter"
    if up == text and len(al) >= 4 and " " in text.strip(): return "caps_spaced"
    return None

def harvest_crowd(nt_path, blocks, tile_dirs, nmax=8000, min_len=1, max_len=24):
    """crop crowd labels (national_typed) within `blocks` at z17 (window crop). Returns items with
    img,text,style(auto),cap_h_m(None). For the rare-font (antiquity/urban) blocks."""
    blocks = set(blocks); out = []
    for line in open(nt_path):
        if len(out) >= nmax: break
        try: d = json.loads(line)
        except Exception: continue
        tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
        lon, lat = d.get("lon"), d.get("lat")
        if not (tv and lon and lat): continue
        tv = tv.strip()
        if not (min_len <= len(tv) <= max_len): continue
        if _z16blk(lon, lat) not in blocks: continue
        c = DATA.crop_point(lon, lat, tile_dirs)
        if c is None: continue
        out.append(dict(img=_to_h32(c), text=tv, style=auto_style(tv), cap_h_m=None))
    return out

def build_vocab(items):
    chars = sorted({ch for it in items for ch in it["text"]})
    stoi = {c: i + 1 for i, c in enumerate(chars)}   # 0 = CTC blank
    itos = {i + 1: c for i, c in enumerate(chars)}
    return stoi, itos

def collate(batch, stoi, pad=1.0):
    seqs = [[stoi[c] for c in it["text"] if c in stoi] for it in batch]
    tlens = [len(s) for s in seqs]
    Wimg = max(it["img"].shape[1] for it in batch)
    W = max(Wimg, 4 * (max(tlens) + 2), 16)                 # ensure T = W//4-1 >= target length
    X = np.full((len(batch), 1, H, W), pad, np.float32)
    for i, it in enumerate(batch):
        w = it["img"].shape[1]
        X[i, 0, :, :w] = it["img"]
    X = (X - X.mean(axis=(2, 3), keepdims=True)) / (X.std(axis=(2, 3), keepdims=True) + 1e-5)
    targets = [c for s in seqs for c in s]
    inp_len = W // 4 - 1
    return X, np.array(targets, np.int64), np.array(tlens, np.int64), inp_len
