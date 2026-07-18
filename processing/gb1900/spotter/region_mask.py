import os, re, json, glob
from shapely.geometry import Polygon, Point
from shapely.strtree import STRtree
import region_common as rc

BASE = rc.BASE
GB = "/vast/ishi/gb1900/edition/national_typed.jsonl"
MARGIN = 15.0        # px buffer on each box (spec)
ISO_PX = 60.0        # nearest-pin distance for "isolated" conservative yield
NUM_RE = re.compile(r"^[0-9]+([.,][0-9]+)?$")

def n_alpha(s):
    return sum(1 for c in s if c.isalpha())

def classify(t):
    s = t.strip()
    if NUM_RE.match(s):
        return "numeric"
    if n_alpha(s) >= 3:
        return "word"
    return "other"

# ---- load boxes ----
boxes = []
for f in glob.glob(BASE + "/boxes/worker*.jsonl"):
    with open(f) as fh:
        for line in fh:
            line = line.strip()
            if line:
                boxes.append(json.loads(line))
print("boxes loaded:", len(boxes))

# ---- load GB1900 pins in region bbox -> global px ----
w, s, e, n = rc.region_bbox()
pins = []
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
        txt = d.get("text") or {}
        val = txt.get("value") if isinstance(txt, dict) else txt
        gpx, gpy = rc.lonlat_to_global_px(lon, lat)
        pins.append((gpx, gpy, val))
print("gb1900 pins in region:", len(pins))

pin_pts = [Point(p[0], p[1]) for p in pins]
tree = STRtree(pin_pts)

# ---- mask each box ----
counts = dict(total=len(boxes), transcribed=0, untr_numeric=0, untr_word=0,
              untr_other=0, untr_word_isolated=0)
untr_word = []
for b in boxes:
    poly = Polygon(b["gpoly"])
    if not poly.is_valid:
        poly = poly.buffer(0)
    bpoly = poly.buffer(MARGIN)
    cand = tree.query(bpoly)          # indices whose bbox intersects
    hit = False
    for ci in cand:
        if bpoly.contains(pin_pts[int(ci)]):
            hit = True
            break
    cls = classify(b["text"])
    if hit:
        counts["transcribed"] += 1
        continue
    # untranscribed
    if cls == "numeric":
        counts["untr_numeric"] += 1
    elif cls == "word":
        counts["untr_word"] += 1
        # nearest-pin distance for conservative "isolated" flag
        nidx = tree.nearest(Point(b["gcx"], b["gcy"]))
        npt = pin_pts[int(nidx)]
        dist = npt.distance(Point(b["gcx"], b["gcy"]))
        b["_nearest_pin_px"] = round(dist, 1)
        if dist > ISO_PX:
            counts["untr_word_isolated"] += 1
        untr_word.append(b)
    else:
        counts["untr_other"] += 1

counts["untranscribed_total"] = counts["untr_numeric"] + counts["untr_word"] + counts["untr_other"]
counts["pct_word_untr_of_boxes"] = round(100.0 * counts["untr_word"] / max(1, counts["total"]), 2)
counts["pct_word_untr_isolated_of_boxes"] = round(100.0 * counts["untr_word_isolated"] / max(1, counts["total"]), 2)
counts["gb1900_pins"] = len(pins)

# ---- training batch: word-label untranscribed ----
def poly_lonlat(gpoly):
    out = []
    for x, y in gpoly:
        lon, lat = rc.global_px_to_lonlat(x, y)
        out.append([round(lon, 6), round(lat, 6)])
    return out

with open(BASE + "/training_batch.jsonl", "w") as out:
    for b in untr_word:
        rec = dict(lon=b["lon"], lat=b["lat"], text=b["text"], score=b["score"],
                   tile="%d/%d" % (b["tile_x"], b["tile_y"]),
                   bbox_lonlat=poly_lonlat(b["gpoly"]),
                   nearest_pin_px=b.get("_nearest_pin_px"),
                   isolated=bool(b.get("_nearest_pin_px", 0) > ISO_PX))
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")

json.dump(counts, open(BASE + "/stats.json", "w"), indent=2)
print(json.dumps(counts, indent=2))

# ---- 30 adjudication crops spread across score ----
from PIL import Image
ADJ = BASE + "/adjudicate"
os.makedirs(ADJ, exist_ok=True)
for f in glob.glob(ADJ + "/*.png"):
    os.remove(f)
cand = sorted(untr_word, key=lambda b: b["score"])
manifest = []
if cand:
    k = 30
    idxs = sorted(set(int(round(i * (len(cand) - 1) / max(1, k - 1))) for i in range(k)))
    mcache = {}
    for rank, ci in enumerate(idxs):
        b = cand[ci]
        mf = b["mfile"]
        if mf not in mcache:
            mcache[mf] = Image.open(mf).convert("RGB")
        im = mcache[mf]
        cx, cy = b["lcx"], b["lcy"]
        L = max(0, int(cx - 60)); U = max(0, int(cy - 60))
        R = min(im.width, int(cx + 60)); D = min(im.height, int(cy + 60))
        safe = re.sub(r"[^A-Za-z0-9]+", "_", b["text"])[:30] or "blank"
        fn = ADJ + "/adj_%02d_s%03d_%s.png" % (rank, int(b["score"] * 100), safe)
        im.crop((L, U, R, D)).save(fn)
        manifest.append(dict(file=fn, text=b["text"], score=b["score"],
                             lon=b["lon"], lat=b["lat"],
                             tile="%d/%d" % (b["tile_x"], b["tile_y"]),
                             nearest_pin_px=b.get("_nearest_pin_px")))
json.dump(manifest, open(ADJ + "/manifest.json", "w"), indent=2, ensure_ascii=False)
print("adjudication crops:", len(manifest))
