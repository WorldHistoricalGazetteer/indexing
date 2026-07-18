"""Measure the MapReader-spotter <-> GB1900-crowd transcript gap on the region set.

Two dimensions (see developer/plan-gb1900-typing.md §12):
  NUMBER  — crowd-only omissions (detection miss vs match-radius artifact),
            broken multi-word labels (spotter splits one crowd label into N boxes),
            spotter-only new content.
  TEXT    — for matched pairs, how often the strings agree / diverge.

Runs standalone in the probe dir (imports region_common). No training; CPU only.
    python gap_diagnostic.py            # writes gap_report.json + prints summary
"""
import os, glob, json, re, difflib, numpy as np
from collections import Counter
from scipy.spatial import cKDTree
import region_common as rc

BASE = "/vast/ishi/gb1900/probe/mapreader_text"
GB = "/vast/ishi/gb1900/edition/national_typed.jsonl"
BOXES = BASE + "/region/boxes/worker*.jsonl"
OUT = BASE + "/gap_report.json"

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def ratio(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()

# ---- load crowd pins in region ----
w, s, e, n = rc.region_bbox()
pins = []
with open(GB) as fh:
    for line in fh:
        try: d = json.loads(line)
        except Exception: continue
        lon, lat = d.get("lon"), d.get("lat")
        if lon is None or lat is None or not (w <= lon <= e and s <= lat <= n):
            continue
        t = d.get("text") or {}
        txt = t.get("value") if isinstance(t, dict) else t
        gx, gy = rc.lonlat_to_global_px(lon, lat)
        pins.append(dict(pin_id=d.get("pin_id"), text=txt or "", gx=gx, gy=gy))
print("crowd pins in region:", len(pins), flush=True)

# ---- load spotter boxes ----
boxes = []
for f in glob.glob(BOXES):
    for line in open(f):
        line = line.strip()
        if not line: continue
        b = json.loads(line)
        xs = [p[0] for p in b["gpoly"]]; ys = [p[1] for p in b["gpoly"]]
        boxes.append(dict(text=b.get("text", ""), score=b.get("score", 0),
                          cx=float(np.mean(xs)), cy=float(np.mean(ys)),
                          x0=min(xs), x1=max(xs), y0=min(ys), y1=max(ys)))
print("spotter boxes:", len(boxes), flush=True)

bxy = np.array([[b["cx"], b["cy"]] for b in boxes])
btree = cKDTree(bxy)
pxy = np.array([[p["gx"], p["gy"]] for p in pins])

# ---- 1. recall of crowd pins vs match radius (px; ~1.4 m/px here) ----
recall = {}
for R in [8, 16, 24, 32, 48, 64, 96, 128]:
    hit = btree.query_ball_point(pxy, R)
    recall[R] = round(float(np.mean([1 if h else 0 for h in hit])), 4)
print("recall vs radius:", recall, flush=True)

# ---- 2. text agreement for nearest box within R=48 ----
R_MATCH = 48
dist, idx = btree.query(pxy, k=1)
agree = Counter(); examples_div = []
matched = 0
sims = []
for p, dd, ii in zip(pins, dist, idx):
    if dd > R_MATCH:
        continue
    matched += 1
    sp = boxes[ii]["text"]
    r = ratio(p["text"], sp)
    sims.append(r)
    if r >= 0.999: agree["exact"] += 1
    elif norm(p["text"]) == norm(sp): agree["exact"] += 1
    elif p["text"].lower() == sp.lower(): agree["case_only"] += 1
    elif r >= 0.8: agree["minor"] += 1
    elif r >= 0.5: agree["weak"] += 1
    else:
        agree["divergent"] += 1
        if len(examples_div) < 25:
            examples_div.append(dict(crowd=p["text"], spotter=sp, sim=round(r, 2)))
print("matched (<=%dpx):" % R_MATCH, matched, "agreement:", dict(agree), flush=True)

# ---- 3. broken multi-word: crowd label recoverable by MERGING >=2 spotter boxes ----
multi = [p for p in pins if len(str(p["text"]).split()) >= 2]
recov_single = recov_merge = notfound = 0
examples_merge = []
for p in multi:
    near = btree.query_ball_point([p["gx"], p["gy"]], 90)
    if not near:
        notfound += 1; continue
    cand = sorted((boxes[i] for i in near), key=lambda b: b["cx"])
    # best single box?
    best_single = max((ratio(p["text"], c["text"]) for c in cand), default=0)
    # best contiguous concatenation of >=2 boxes (reading order L->R)
    best_merge = 0; best_join = None
    for a in range(len(cand)):
        acc = cand[a]["text"]
        for bb in range(a + 1, len(cand)):
            acc = acc + " " + cand[bb]["text"]
            rr = ratio(p["text"], acc)
            if rr > best_merge:
                best_merge = rr; best_join = acc
    if best_single >= 0.8:
        recov_single += 1
    elif best_merge >= 0.8:
        recov_merge += 1
        if len(examples_merge) < 25:
            examples_merge.append(dict(crowd=p["text"], merged=best_join, sim=round(best_merge, 2)))
    else:
        notfound += 1
print("multiword crowd labels:", len(multi),
      "| whole-in-one-box:", recov_single,
      "| recovered-by-merge:", recov_merge,
      "| still-missing:", notfound, flush=True)

# ---- 4. spotter-only (no crowd pin within R=48) ----
ptree = cKDTree(pxy)
d2, _ = ptree.query(bxy, k=1)
sonly = [boxes[i] for i in range(len(boxes)) if d2[i] > R_MATCH]
word = sum(1 for b in sonly if re.search(r"[A-Za-z]{3,}", b["text"]))
num = sum(1 for b in sonly if re.fullmatch(r"[\d.,]+", (b["text"] or "").strip() or "x"))
print("spotter-only boxes:", len(sonly), "| word-like:", word, "| numeric:", num, flush=True)

crowd_only = int((dist > R_MATCH).sum())
rep = dict(
    n_crowd=len(pins), n_spotter=len(boxes),
    recall_vs_radius=recall,
    match_radius_px=R_MATCH,
    matched=matched, crowd_only=crowd_only,
    text_agreement=dict(agree),
    text_sim_median=round(float(np.median(sims)), 3) if sims else None,
    multiword_total=len(multi), multiword_whole_box=recov_single,
    multiword_recovered_by_merge=recov_merge, multiword_missing=notfound,
    spotter_only=len(sonly), spotter_only_word=word, spotter_only_numeric=num,
    examples_divergent_text=examples_div,
    examples_broken_multiword=examples_merge,
)
json.dump(rep, open(OUT, "w"), ensure_ascii=False, indent=2)
print("WROTE", OUT, flush=True)
