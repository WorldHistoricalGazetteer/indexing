"""Classify the font of spotter boxes (the validated hybrid: SSL encoder + same-letter kNN vs the human
reference). Emits {lon,lat,gcx,gcy,text,font,conf,fonts,nchar,score,gpoly} — `fonts` is the full ranked
top-3 [font,certainty] shortlist. Feeds the GB-STAMP fusion (downstream keeps only conf>=gate).

Two entry points:
  classify_boxes(boxes)  — in-process, model loaded once and cached; called by spot_sheet.py --classify so a
                           region is classified WHILE its tiles are still on /vast (no reload).
  python font_classify.py — batch/backfill CLI over boxes_*.jsonl (uses fetch-on-miss in make_font_testset_v2
                           when tiles have already been cleaned; set FCTILES to an isolated cache).
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, glob, json, numpy as np, torch
import concurrent.futures as cf
from collections import Counter, defaultdict
from ssl_pretrain import Enc
from ssl_eval import cased, harvest, SETS, STYLES
from make_font_testset_v2 import derotate

SPOT = "/vast/ishi/gb1900/edition/spot"
ENC = os.environ.get("ENC", f"{SPOT}/encoder_full.pt")

_CLF = None
def load_classifier():
    """Load the SSL encoder + embed the human-reference glyphs once; cached for the process lifetime."""
    global _CLF
    if _CLF is not None: return _CLF
    net = Enc(); net.load_state_dict(torch.load(ENC, map_location="cpu")); net.eval()
    ref = []
    for bf, df in SETS: ref += harvest(bf, df)
    rg = [(L, cap, g, f) for f, gl in ref for (L, cap, g) in gl]
    rl = np.array([c[0] for c in rg]); rc = np.array([c[1] for c in rg]); rf = np.array([c[3] for c in rg])
    with torch.no_grad():
        RX = np.stack([c[2] for c in rg]).astype(np.float32)[:, None] / 255.0
        RZ = net(torch.tensor((RX - 0.8) / 0.3)).numpy()
    _CLF = (net, rl, rc, rf, RZ)
    return _CLF

def classify_boxes(boxes, clf=None):
    """Return a font record per classifiable box. `boxes` is any iterable of spotter-box dicts."""
    net, rl, rc, rf, RZ = clf or load_classifier()
    boxes = [r for r in boxes if r.get("score", 0) >= 0.55 and len([c for c in r["text"] if c.isalnum()]) >= 2]
    def prep(r):
        patch = derotate(r)
        if patch is None: return None
        gl = cased(patch, r["text"])
        return (r, gl) if gl else None
    prepped = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for res in ex.map(prep, boxes):
            if res: prepped.append(res)
    out = []; B = 512
    for i in range(0, len(prepped), B):
        chunk = prepped[i:i + B]
        allg = [(bi, L, cap, g) for bi, (r, gl) in enumerate(chunk) for (L, cap, g) in gl]
        if not allg: continue
        with torch.no_grad():
            X = np.stack([a[3] for a in allg]).astype(np.float32)[:, None] / 255.0
            Z = net(torch.tensor((X - 0.8) / 0.3)).numpy()
        per = defaultdict(lambda: defaultdict(float))
        for k, (bi, L, cap, g) in enumerate(allg):
            same = (rl == L) & (rc == cap)
            if not same.any() or len(set(rf[same])) < 2: continue
            sim = np.where(same, RZ @ Z[k], -2.0)
            best = sorted(((s, float(sim[(rf == s) & same].max())) for s in set(rf[same])), key=lambda kv: -kv[1])
            per[bi][best[0][0]] += best[0][1] - (best[1][1] if len(best) > 1 else 0)
        for bi, (r, gl) in enumerate(chunk):
            w = per.get(bi)
            if not w: continue
            tot = sum(w.values()) + 1e-9
            ranked = sorted(((fnt, round(float(sc / tot), 3)) for fnt, sc in w.items()), key=lambda kv: -kv[1])[:3]
            pred, conf = ranked[0]                          # winner; `fonts` keeps the full ranked shortlist
            out.append(dict(lon=r["lon"], lat=r["lat"], gcx=r["gcx"], gcy=r["gcy"], text=r["text"],
                            font=pred, conf=conf, fonts=ranked, nchar=len(gl),
                            score=r.get("score"), gpoly=r.get("gpoly")))
    return out

def main():
    OUT = os.environ.get("FONT_OUT", f"{SPOT}/boxes_font.jsonl")
    clf = load_classifier()
    print(f"reference glyphs: {len(clf[1])} fonts {dict(Counter(clf[3].tolist()))}", flush=True)
    boxes = []
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        if os.path.basename(f).startswith("boxes_font"): continue    # never re-read our own output
        for line in open(f):
            boxes.append(json.loads(line))
    print(f"boxes to classify: {len(boxes)}", flush=True)
    recs = classify_boxes(boxes, clf)
    with open(OUT, "w") as fout:
        for r in recs: fout.write(json.dumps(r) + "\n")
    print(f"FONTCLASSIFYDONE wrote {len(recs)} -> {OUT}; font dist {dict(Counter(r['font'] for r in recs))}", flush=True)

if __name__ == "__main__":
    main()
