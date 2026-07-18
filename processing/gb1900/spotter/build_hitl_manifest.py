import os, io, re, json, glob, base64, time
from PIL import Image
from shapely.geometry import Polygon, Point
from shapely.strtree import STRtree
import region_common as rc

BASE = "/vast/ishi/gb1900/probe/mapreader_text"
REGION = BASE + "/region"
HITL = BASE + "/hitl"
GB = "/vast/ishi/gb1900/edition/national_typed.jsonl"
VLM_GLOB = "/vast/ishi/gb1900/edition/vlm/batch_*/shard-0.jsonl"
LETTER = "/vast/ishi/elastic/typesystem/data/gb1900_os_lettering.json"
SHARED_CACHE = "/vast/ishi/gb1900/tiles/16"
REGION_CACHE = REGION + "/tiles"
PAD_FRAC = 0.10
TARGET_H = 64
PER_STYLE = 24

os.makedirs(HITL, exist_ok=True)
lett = json.load(open(LETTER))
STYLE_TOKEN = {k: v for k, v in lett["style_to_type_token"].items() if not k.startswith("_")}
LEGEND = lett["style_legend"]

# ---- 1. load boxes ----
boxes = []
for f in glob.glob(REGION + "/boxes/worker*.jsonl"):
    with open(f) as fh:
        for line in fh:
            line = line.strip()
            if line:
                boxes.append(json.loads(line))
print("boxes:", len(boxes), flush=True)

# ---- 2. GB1900 pins in region bbox -> global px ----
w, s, e, n = rc.region_bbox()
pins = []           # (pin_id, gpx, gpy)
with open(GB) as fh:
    for line in fh:
        try:
            d = json.loads(line)
        except Exception:
            continue
        lon = d.get("lon"); lat = d.get("lat")
        if lon is None or lat is None:
            continue
        if not (w <= lon <= e and s <= lat <= n):
            continue
        pid = d.get("pin_id") or d.get("place_id")
        gpx, gpy = rc.lonlat_to_global_px(lon, lat)
        pins.append((pid, gpx, gpy))
print("pins in region:", len(pins), flush=True)
pts = [Point(p[1], p[2]) for p in pins]
tree = STRtree(pts)

# ---- match: each box -> first pin inside it ----
box_pin = []        # (box, pin_id)
matched_pids = set()
for b in boxes:
    poly = Polygon(b["gpoly"])
    if not poly.is_valid:
        poly = poly.buffer(0)
    for ci in tree.query(poly):
        if poly.contains(pts[int(ci)]):
            pid = pins[int(ci)][0]
            box_pin.append((b, pid))
            matched_pids.add(pid)
            break
print("boxes matched to a pin:", len(box_pin), "unique pins:", len(matched_pids), flush=True)

# ---- 3. VLM os_style lookup for matched pins (scan shards) ----
vlm = {}
t = time.time()
for shard in sorted(glob.glob(VLM_GLOB)):
    with open(shard) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            pid = r.get("pin_id")
            if pid in matched_pids and pid not in vlm:
                v = r.get("vlm") or {}
                st = v.get("os_style")
                if st:
                    vlm[pid] = dict(os_style=st, case=v.get("case"),
                                    size_band=v.get("size_band"), vlm_text=v.get("vlm_text"))
print("vlm os_style found for %d/%d matched pins (%.1fs)" % (len(vlm), len(matched_pids), time.time() - t), flush=True)

# keep only boxes whose pin has an os_style
kept = [(b, pid) for (b, pid) in box_pin if pid in vlm]
print("boxes kept (pin has os_style):", len(kept), flush=True)

# ---- 4. clean tight crop from TILES ----
_tile_cache = {}
def load_tile(tx, ty):
    k = (tx, ty)
    if k in _tile_cache:
        return _tile_cache[k]
    im = None
    for base in (SHARED_CACHE, REGION_CACHE):
        p = os.path.join(base, str(tx), str(ty) + ".png")
        if os.path.exists(p):
            try:
                im = Image.open(p).convert("RGB"); break
            except Exception:
                im = None
    if len(_tile_cache) < 400:
        _tile_cache[k] = im
    return im

def crop_from_tiles(gpoly):
    xs = [p[0] for p in gpoly]; ys = [p[1] for p in gpoly]
    minx, maxx = min(xs), max(xs); miny, maxy = min(ys), max(ys)
    bw = maxx - minx; bh = maxy - miny
    px = bw * PAD_FRAC; py = bh * PAD_FRAC
    minx -= px; maxx += px; miny -= py; maxy += py
    tx0 = int(minx // 256); tx1 = int(maxx // 256)
    ty0 = int(miny // 256); ty1 = int(maxy // 256)
    W = (tx1 - tx0 + 1) * 256; H = (ty1 - ty0 + 1) * 256
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    ok = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            t = load_tile(tx, ty)
            if t is not None:
                canvas.paste(t, ((tx - tx0) * 256, (ty - ty0) * 256)); ok = True
    if not ok:
        return None
    ox, oy = tx0 * 256, ty0 * 256
    L = max(0, int(minx - ox)); U = max(0, int(miny - oy))
    R = min(W, int(maxx - ox)); D = min(H, int(maxy - oy))
    if R - L < 3 or D - U < 3:
        return None
    crop = canvas.crop((L, U, R, D))
    if crop.height != TARGET_H:
        nw = max(1, int(round(crop.width * TARGET_H / crop.height)))
        crop = crop.resize((nw, TARGET_H), Image.LANCZOS)
    buf = io.BytesIO(); crop.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

# ---- group by style, counts, stratified samples ----
from collections import defaultdict
by_style = defaultdict(list)
for b, pid in kept:
    by_style[vlm[pid]["os_style"]].append((b, pid))
counts = {st: len(v) for st, v in by_style.items()}

samples = []
for st in sorted(by_style):
    items = sorted(by_style[st], key=lambda bp: bp[0]["score"])  # spread by score
    if len(items) > PER_STYLE:
        idx = sorted(set(int(round(i * (len(items) - 1) / (PER_STYLE - 1))) for i in range(PER_STYLE)))
        items = [items[i] for i in idx]
    for b, pid in items:
        crop = crop_from_tiles(b["gpoly"])
        if crop is None:
            continue
        v = vlm[pid]
        samples.append(dict(
            pin_id=pid, os_style=st, case=v.get("case"), size_band=v.get("size_band"),
            text=b["text"], crop=crop,
            style_desc=LEGEND.get(st, st),
            mapped_token=STYLE_TOKEN.get(st)))
print("samples with crops:", len(samples), flush=True)

styles_desc = {st: LEGEND.get(st, st) for st in by_style}
manifest = dict(styles=styles_desc, style_to_type_token=STYLE_TOKEN,
                counts=counts, samples=samples)
outp = HITL + "/manifest_clean.json"
json.dump(manifest, open(outp, "w"), ensure_ascii=False)
print("WROTE", outp, os.path.getsize(outp), "bytes", flush=True)
print("counts:", json.dumps(counts), flush=True)
