"""Phase C — GLYPH-TEMPLATE DISCRIMINATION TEST (SG's exemplar-matching approach).

Can a canonical Characteristic-Sheet capital, matched as a GLYPH SHAPE, discriminate its own letterform
(italic / blackletter / upright) from the others? The CRNN recognition embed can't (it's trained to be
font-INVARIANT); glyph shape IS the style. Two parts, the second built to confront the domain gap that sank
the earlier synthetic->real attempt:

  PART 1  clean-domain ceiling  — leave-one-exemplar-out over CS glyphs. Necessary condition: do the
          canonical letterforms separate at all under this matcher, with unambiguous ground truth?
  PART 2  domain-gap probe      — match the CS templates against the most UNAMBIGUOUSLY-styled real map
          labels (italic watercourses; blackletter antiquities) and see if the ranking survives to tiles.

    /vast/ishi/envs/boundary/bin/python discrim_test.py
"""
import os, re, glob, json, math, numpy as np, cv2
import concurrent.futures as cf
from collections import Counter, defaultdict
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__)); REF = os.path.join(HERE, "reference")
TILES = "/vast/ishi/gb1900/tiles17"; DISC = "/vast/ishi/gb1900/edition/discover"; N17 = 2 ** 17

# CS exemplars grouped by APPROVED letterform (Phase A). Only the cleanest, most-distinctive sources per class.
STYLE_SRC = {
    "italic":      ["ex_small_rivers", "ex_other_villages", "ex_gentlemens_seats", "ex_navigable_rivers_word", "ex_towns_generally"],
    "blackletter": ["ex_antiq_saxon", "ex_antiq_norman"],
    "upright":     ["ex_parish_churches", "ex_workhouses", "ex_woods_copses", "ex_bays_word", "ex_county_bridges_word"],
}
H, W = 36, 44   # canonical glyph canvas (height-normalised; slant survives in aspect/lean)

def norm_glyph(sub):
    """binary glyph bbox -> height-normalised, centroid-centred H x W binary (preserves italic lean)."""
    ys, xs = np.where(sub > 0)
    if len(ys) < 8: return None
    sub = sub[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    sc = (H - 4) / sub.shape[0]; nw = max(3, int(round(sub.shape[1] * sc)))
    g = cv2.resize(sub.astype(np.uint8) * 255, (nw, H - 4), cv2.INTER_AREA) > 60
    canvas = np.zeros((H, W), bool)
    gw = min(nw, W - 2); g = g[:, :gw]
    cx = int(np.round(np.where(g.any(0))[0].mean())) if g.any() else gw // 2
    x0 = max(0, min(W - gw, W // 2 - cx)); canvas[2:2 + (H - 4), x0:x0 + gw] = g
    return canvas

def glyphs_of(gray):
    """Otsu ink -> letter-sized connected components -> normalised glyphs."""
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, lbl, st, _ = cv2.connectedComponentsWithStats(ink, 8)
    hs = [st[i, 3] for i in range(1, n) if st[i, 3] >= 6]
    if not hs: return []
    mh = np.median(hs); out = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if h < 0.55 * mh or h > 2.2 * mh or area < 10: continue      # letter-sized only
        if w > 3.2 * h: continue                                     # rules / long thin
        g = norm_glyph((lbl[y:y + h, x:x + w] == i))
        if g is not None: out.append(g)
    return out

SHIFTS = [(-2, 0), (-1, 0), (0, 0), (1, 0), (2, 0), (0, -2), (0, 2)]
def _mat(bank):
    """flatten a bank {style:[(key,glyph)]} to unit-norm rows + parallel style/key arrays."""
    styles, keys, vecs = [], [], []
    for s in bank:
        for k, g in bank[s]:
            styles.append(s); keys.append(k); vecs.append(g.astype(np.float32).ravel())
    M = np.array(vecs, np.float32)
    Mn = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)
    return np.array(styles), np.array(keys), Mn
def _shifted(g):
    rows = []
    for dx, dy in SHIFTS:
        b = np.roll(np.roll(g, dx, 1), dy, 0).astype(np.float32).ravel()
        rows.append(b / (np.linalg.norm(b) + 1e-6))
    return np.array(rows, np.float32)                       # (S, HW)
def sims_row(g, Mn):
    return (_shifted(g) @ Mn.T).max(0)                      # (Nb,) best-over-shift cosine to every bank glyph

def load_cs():
    bank = defaultdict(list)                     # style -> list of (exemplar_key, glyph)
    for style, keys in STYLE_SRC.items():
        for k in keys:
            p = f"{REF}/{k}.jpg"
            if not os.path.exists(p): continue
            gray = np.asarray(Image.open(p).convert("L"), np.uint8)
            for g in glyphs_of(gray): bank[style].append((k, g))
    return bank

def nn_style(g, styles_arr, Mn, mask=None):
    """1-NN style: cosine of shifted test glyph to every (optionally masked) bank row; per-style max."""
    r = sims_row(g, Mn)
    if mask is not None: r = np.where(mask, r, -2.0)
    best = {}
    for s in np.unique(styles_arr):
        sel = styles_arr == s
        best[s] = float(r[sel].max()) if sel.any() else -2.0
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    return ranked[0][0], ranked[0][1], ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0)

def part1(bank):
    print("\n=== PART 1: clean-domain ceiling (leave-one-exemplar-out over CS glyphs) ===")
    styles_arr, keys_arr, Mn = _mat(bank)
    rasters = [g for s in bank for _, g in bank[s]]
    stset = list(dict.fromkeys(styles_arr.tolist()))
    counts = {s: int((styles_arr == s).sum()) for s in stset}
    print("CS glyphs per style:", counts)
    CAP = min(counts.values())                   # balance banks so density doesn't bias 1-NN
    base = np.zeros(len(styles_arr), bool)
    for s in stset: base[np.where(styles_arr == s)[0][:CAP]] = True
    conf = Counter(); tot = Counter()
    for held_s in stset:
        for hk in dict.fromkeys(keys_arr[styles_arr == held_s].tolist()):
            sel = (styles_arr == held_s) & (keys_arr == hk)
            mask = base.copy(); mask[sel] = False           # leave this exemplar's glyphs out
            for ti in np.where(sel)[0]:
                pred, _, _ = nn_style(rasters[ti], styles_arr, Mn, mask)
                conf[(held_s, pred)] += 1; tot[held_s] += 1
    acc = sum(conf[(s, s)] for s in stset) / max(1, sum(tot.values()))
    print(f"overall glyph accuracy: {acc:.3f}  (N={sum(tot.values())})")
    print(f"{'true':12s}" + "".join(f"{s[:5]:>7s}" for s in stset) + "   recall")
    for s in stset:
        print(f"  {s:10s}" + "".join(f"{conf[(s,d)]:>7d}" for d in stset) + f"   {conf[(s,s)]/max(1,tot[s]):.2f}")
    return acc

# ---- PART 2: real map instances ----
WATER = re.compile(r"^(R\.|Afon|Nant)\b|\b(River|Brook|Burn|Beck)\b", re.I)
NOTWATER = re.compile(r"\b(Farm|Ho|House|Cottage|Wood|Hall|Fm|Mill|Green|Lane|Bank|Bridge|Field|Moor|Hill)\b", re.I)
ANTIQ = re.compile(r"\b(Tumulus|Tumuli|Earthwork|Earthworks|Cairn|Stone Circle|Standing Stone|Site of|Camp|Enclosure)\b", re.I)
CHURCH = re.compile(r"\b(Church|Chapel|Ch\.?)$|\bChurch\b", re.I)

def tile(tx, ty):
    p = f"{TILES}/{tx}/{ty}.png"
    if os.path.exists(p) and os.path.getsize(p) > 500:
        try: return np.asarray(Image.open(p).convert("L"), np.uint8)
        except Exception: return None
    return None

def crop_box(box_g, pad=3):
    x0, y0, x1, y1 = box_g; x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
    tx0, tx1, ty0, ty1 = x0 // 256, x1 // 256, y0 // 256, y1 // 256
    cv = np.full(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), 255, np.uint8); ok = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            t = tile(tx, ty)
            if t is not None: cv[(ty - ty0) * 256:(ty - ty0) * 256 + 256, (tx - tx0) * 256:(tx - tx0) * 256 + 256] = t; ok = True
    if not ok: return None
    L, U = x0 - tx0 * 256, y0 - ty0 * 256
    c = cv[U:U + (y1 - y0), L:L + (x1 - x0)]
    return c if c.size > 60 else None

def part2(bank):
    print("\n=== PART 2: domain-gap probe (CS templates vs UNAMBIGUOUS real map labels) ===")
    styles_arr, keys_arr, Mn = _mat(bank)
    groups = {"italic-water": [], "blackletter-antiq": [], "upright-church": []}
    for f in glob.glob(f"{DISC}/labels_*.json"):
        for Lb in json.load(open(f)):
            t = (Lb.get("crowd") or "").strip()
            if not t or "box_g" not in Lb: continue
            if WATER.search(t) and not NOTWATER.search(t): groups["italic-water"].append(Lb)
            elif ANTIQ.search(t): groups["blackletter-antiq"].append(Lb)
            elif CHURCH.search(t): groups["upright-church"].append(Lb)
    for grp in groups: groups[grp] = groups[grp][:120]
    print("unambiguous instances:", {k: len(v) for k, v in groups.items()})

    def classify(Lb):
        c = crop_box(Lb["box_g"])
        if c is None: return None
        gs = glyphs_of(c)
        if not gs: return None
        votes = Counter(); sc = []
        for g in gs:
            pred, s, _ = nn_style(g, styles_arr, Mn)
            if pred and s > 0.30: votes[pred] += 1; sc.append(s)
        if not votes: return None
        return votes.most_common(1)[0][0], np.mean(sc), len(gs)

    for grp, expect in [("italic-water", "italic"), ("blackletter-antiq", "blackletter"), ("upright-church", "upright")]:
        res = []
        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            for r in ex.map(classify, groups[grp]):
                if r: res.append(r)
        if not res: print(f"  {grp:20s} no glyphs"); continue
        dist = Counter(r[0] for r in res); n = len(res)
        hit = dist.get(expect, 0) / n
        print(f"  {grp:20s} (expect {expect:11s}) N={n:3d}  correct={hit:.2f}  dist={dict(dist)}  mean-score={np.mean([r[1] for r in res]):.2f}")

def main():
    bank = load_cs()
    a = part1(bank)
    if a < 0.5:
        print("\n=> PART 1 below chance-ish: canonical letterforms do NOT separate under glyph-matching. STOP.")
    part2(bank)

if __name__ == "__main__":
    main()
