"""Sheet-grounded font-style experiment (GB-STAMP re-grounding on the OS 1897 Characteristic Sheet).

The Characteristic Sheet is the OS's OWN generative model of the map typography. Rather than the
275 hand-labelled ad-hoc anchors, we AUTO-LABEL abundant exemplars per SHEET style using crowd text
(the styles the OS documents), CRNN-embed their z17 crops, and refit the fusion classifier.

Coarse VISUAL partition (what the eye/pixels can actually separate), each mapped to sheet meaning:
  italic       -> WATER (navigable rivers/canals + small rivers/brooks are italic on the sheet)
                  [+ railway-mineral is also italic — disambiguated by TEXT, not font]
  blackletter  -> ANTIQUITY (Roman / prehistoric-Saxon / Norman — era split is TEXTUAL)
  caps         -> ADMIN / TOWN (counties, parishes, boroughs... in caps; level via SIZE+TEXT)
  upright      -> SETTLEMENT / building (ordinary roman upright — the default)
  numeral      -> spot heights / benchmarks

KEY TEST: does `italic` (trained on strongly-italic WATER labels) separate from `upright`
(settlement)? The old serif_italic .14 was italic-vs-upright WITHIN settlements (~3 deg slant);
water italic is a proper cursive italic and should be far more separable.

    python font_ground.py --out out_ground --per-class 2000 --workers 48
"""
import argparse, os, json, math, re, time, urllib.request, numpy as np, torch
import concurrent.futures as cf
from collections import Counter
from sklearn.model_selection import cross_val_predict, StratifiedKFold
import data as DATA, crnn_data as CD
from crnn import CRNN
from crnn_eval import crnn_embed
from fusion import textfeats, clf, per_class

NT = "/vast/ishi/gb1900/edition/national_typed.jsonl"
TILES17 = ["/vast/ishi/gb1900/tiles17"]
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
MODEL_DIR = "/vast/ishi/gb1900/probe/font/out_z17"
N17 = 2 ** 17
CROP_W, CROP_H = 280, 96

# ---- sheet-grounded text rules (auto-labels; deliberately HIGH-PRECISION lexicons) ----
WATER = re.compile(r"\b(River|Riv|Brook|Burn|Canal|Stream|Beck|Water|Afon|Nant|Rhyd|Gill|"
                   r"Sike|Lade|Dyke|Cut|Navigation|Reach|Pill|Fleet)\b", re.I)
WATER_SUFFsuf = re.compile(r"(bourne|brook|burn)$", re.I)
# antiquity: blackletter on the sheet — extend the verified-clean CD._ANTIQ with era terms
ANTIQ = re.compile(r"\b(Tumulus|Tumuli|Cairn|Cairns|Camp|Earthwork|Earthworks|Barrow|Barrows|"
                   r"Motte|Cist|Enclosure|Entrenchment|Cross|Cross\s|Tumbeg|Stone\sCircle|"
                   r"Standing\sStone|Hut\sCircle|Souterrain|Broch|Dun|Fort|Roman)\b", re.I)
NUMERAL = re.compile(r"^[\d\s.,]+$")


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
    x, y = t; p = f"{TILES17[0]}/{x}/{y}.png"
    if os.path.exists(p) and os.path.getsize(p) > 500: return "skip"
    os.makedirs(f"{TILES17[0]}/{x}", exist_ok=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(S3.format(x=x, y=y), headers={"User-Agent": "whg-z17"}), timeout=30) as r:
            data = r.read()
        if len(data) < 400: return "404"
        open(p, "wb").write(data); return "got"
    except Exception:
        return "fail"

def label_of(text, admin):
    """Assign a sheet-style auto-label to a crowd label, or None (skip — ambiguous)."""
    t = (text or "").strip()
    if not t or len(t) > 24: return None
    al = [c for c in t if c.isalpha()]
    if NUMERAL.match(t) and any(c.isdigit() for c in t): return "numeral"
    if len(al) < 2: return None                                   # single marks: ambiguous
    if ANTIQ.search(t): return "blackletter"
    if WATER.search(t) or WATER_SUFFsuf.search(t): return "italic"
    # ADMIN caps: allcaps AND a known admin (county/parish) name -> sheet says caps
    up = t.upper()
    if t == up and " " not in t.strip() and t.lower() in admin and len(al) >= 4:
        return "caps"                                            # single-token allcaps admin name
    # UPRIGHT settlement: ordinary mixed-case place name, NOT water/antiquity/allcaps/admin
    if t != up and " " not in t.strip() and len(al) >= 4 and t.lower() not in admin:
        return "upright"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True); ap.add_argument("--per-class", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--admin", default="/vast/ishi/gb1900/probe/font/admin_names.json")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    admin = set(x.lower() for x in json.load(open(a.admin)).get("names", [])) if os.path.exists(a.admin) else set()
    print("admin gazetteer:", len(admin), "names | device:", dev, flush=True)

    # ---- 1. sample exemplars per sheet-style, spread nationwide ----
    want = a.per_class
    buckets = {k: [] for k in ("italic", "blackletter", "caps", "upright", "numeral")}
    t0 = time.time()
    for line in open(NT):
        if all(len(v) >= want for v in buckets.values()): break
        try: d = json.loads(line)
        except Exception: continue
        lon, lat = d.get("lon"), d.get("lat")
        if lon is None or lat is None: continue
        tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
        lab = label_of(tv, admin)
        if lab is None or len(buckets[lab]) >= want: continue
        # nationwide spread: keep only if it advances the bucket (natural file order ~ geographic)
        buckets[lab].append((tv, lon, lat))
    print("sampled:", {k: len(v) for k, v in buckets.items()}, f"({time.time()-t0:.0f}s)", flush=True)

    # ---- 2. fetch needed z17 tiles ----
    items = [(lab, tv, lon, lat) for lab, rows in buckets.items() for (tv, lon, lat) in rows]
    need = set()
    for _, _, lon, lat in items:
        xp, yp = px17(lon, lat); need |= tiles_for(xp, yp)
    print("fetching", len(need), "z17 tiles for training sample...", flush=True)
    c = Counter()
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(fetch_one, list(need))):
            c[r] += 1
            if i % 10000 == 0: print(f"  fetch {i}/{len(need)} {dict(c)} ({time.time()-t0:.0f}s)", flush=True)
    print("fetch done", dict(c), flush=True)

    # ---- 3. crop + CRNN embed ----
    vocab = json.load(open(os.path.join(MODEL_DIR, "vocab.json")))
    net = CRNN(n_class=len(vocab["stoi"]) + 1).to(dev)
    net.load_state_dict(torch.load(os.path.join(MODEL_DIR, "crnn_z17.pt"), map_location=dev)); net.eval()

    imgs, ys, texts = [], [], []
    for lab, tv, lon, lat in items:
        crop = DATA.crop_point(lon, lat, TILES17)
        if crop is None: continue
        imgs.append(CD._to_h32(crop)); ys.append(lab); texts.append(tv)
    ys = np.array(ys)
    print("cropped:", len(imgs), dict(Counter(ys.tolist())), f"({time.time()-t0:.0f}s)", flush=True)
    Zc = crnn_embed(net, imgs, dev)
    # ink-height feature (rough size proxy from the crop: rows with ink; paper=1, ink<1)
    def ink_h(im):
        rows = (im < 0.5).sum(axis=1); on = np.where(rows > 2)[0]
        return math.log(max(1.0, (on[-1] - on[0]) if len(on) else 1.0))
    sz = np.array([[ink_h(im)] for im in imgs])
    tx = np.array([textfeats(t) for t in texts])
    F = np.hstack([Zc, sz, tx])
    np.save(os.path.join(a.out, "F.npy"), F); np.save(os.path.join(a.out, "y.npy"), ys)

    # ---- 4. eval: 5-fold stratified (abundant labels; not LeaveOneOut) ----
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=0)
    pred = cross_val_predict(clf(), F, ys, cv=skf)
    overall = round(float((pred == ys).mean()), 3)
    pc = per_class(ys, pred)
    # confusion for the KEY pair italic vs upright
    conf = {}
    for a_ in ("italic", "upright", "blackletter", "caps", "numeral"):
        row = {b_: int(((ys == a_) & (pred == b_)).sum()) for b_ in ("italic", "upright", "blackletter", "caps", "numeral")}
        conf[a_] = row
    rep = dict(overall=overall, per_class=pc, confusion=conf, n=int(len(ys)),
               sampled={k: len(v) for k, v in buckets.items()})
    json.dump(rep, open(os.path.join(a.out, "ground_report.json"), "w"), indent=2)
    print("=== SHEET-GROUNDED RESULT ===", flush=True)
    print("overall:", overall, flush=True)
    print("per_class:", json.dumps(pc), flush=True)
    print("confusion (row=true):", json.dumps(conf), flush=True)
    # fit final on ALL + save for z17_batch
    import joblib
    model = clf().fit(F, ys)
    joblib.dump({"model": model, "classes": list(model.classes_)}, os.path.join(a.out, "font_clf.joblib"))
    print("WROTE font_clf.joblib + ground_report.json", flush=True)


if __name__ == "__main__":
    main()
