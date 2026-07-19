"""Phase C — PURE SPOT-AND-FAN alphabet builder (SG's method; validated by same_letter_test @ 0.91).

Font is NEVER inferred from a label's text-category (that would bake a semantic assumption into the ground
truth — poisonous if a river name were ever set upright). Font comes SOLELY from the OS Characteristic-Sheet
letter, matched VISUALLY, then fanned by co-occurrence. The crowd transcript is used ONLY for letter-identity
(which glyph is a "C") — never to imply font.

  SEED   spot the CS style-sheet capitals (italic<-"NAVIGABLE RIVERS", upright<-"BAYS", blackletter<-"Norman"
         /"Saxon") in real glyphs by SAME-LETTER visual match (crowd text gives the letter). Keep the most
         confident spots per font -> assign those labels that font, harvest their glyphs as REAL-domain
         templates. This crosses the CS->real gap once, at the seed (HITL-verifiable montage emitted).
  FAN    type every remaining label by SAME-LETTER match to the current REAL alphabet; assign its font only
         when >=2 known letters CONCUR and there's no strong internal disagreement (mixed-font guard for
         appended Ch./B.M. qualifiers). Harvest -> grows to NEW letters. Iterate to convergence.
  REPORT per-font A-Z reach; typeable letters; convergence. NO text-category anywhere.

Saves alphabet.npz + seed_montage.png. Matching bucketed by (letter,case). Aligned glyphs cached.

    /vast/ishi/envs/boundary/bin/python build_alphabet.py
"""
import os, re, glob, json, math, pickle, numpy as np, cv2
import concurrent.futures as cf
from collections import Counter, defaultdict
from PIL import Image
from discrim_test import norm_glyph, sims_row, crop_box, glyphs_of, H, W
from same_letter_test import glyphs_pos

HERE = os.path.dirname(os.path.abspath(__file__)); REF = os.path.join(HERE, "reference")
DISC = "/vast/ishi/gb1900/edition/discover"; OUT = f"{DISC}/alphabet.npz"; CACHE = f"{DISC}/aligned_cache.pkl"
STYLES = ["italic", "blackletter", "upright"]
HIGH = 0.45            # label confidence to assign
CAP = 80              # max templates per (letter,case,style)
GLYPH_MIN = 0.40       # per-glyph match score to count as a voter
MIN_GLYPHS = 4         # a label needs this many aligned glyphs to propagate from
MAX_ROUNDS = 8
SEED_PER_FONT = 45     # keep the N most-confident CS spots per font as seeds
SEED_MARGIN = 0.03     # a CS spot must prefer its font over the next by this margin

# CS style-sheet specimens -> (exemplar, its text, font). Short all-caps words segment most reliably.
CS_SRC = [("ex_canals_word", "CANALS", "italic"),
          ("ex_navigable_rivers_word", "NAVIGABLE RIVERS", "italic"),
          ("ex_bays_word", "BAYS", "upright"),
          ("ex_harbours_word", "HARBOURS", "upright"),
          ("ex_antiq_norman", "Norman", "blackletter"),
          ("ex_antiq_saxon", "Prehistoric or Saxon", "blackletter")]

def force_split(gray, K):
    """split a CLEAN specimen into EXACTLY K letter blocks by cutting at the K-1 deepest, well-separated
    column-ink valleys (robust where letters over-split on serifs or touch in blackletter). -> K rasters."""
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    prof = (ink > 0).sum(0).astype(float)
    nz = np.where(prof > prof.max() * 0.02)[0]
    if len(nz) < K: return []
    a, b = int(nz[0]), int(nz[-1]); seg_prof = prof[a:b + 1]; Wd = len(seg_prof)
    sm = np.convolve(seg_prof, np.ones(3) / 3, "same")
    minsep = max(3, int(Wd / K * 0.40))
    cand = sorted([x for x in range(1, Wd - 1) if sm[x] <= sm[x - 1] and sm[x] <= sm[x + 1]], key=lambda x: sm[x])
    cuts = []
    for x in cand:
        if all(abs(x - c) >= minsep for c in cuts): cuts.append(x)
        if len(cuts) == K - 1: break
    bounds = [0] + sorted(cuts) + [Wd]
    out = []
    for i in range(len(bounds) - 1):
        g = norm_glyph(ink[:, a + bounds[i]:a + bounds[i + 1]] > 0)
        if g is not None: out.append(g)
    return out

def cs_seed_templates():
    """extract letter-labelled glyphs from the CS specimens -> {(letter,cap): {font: [glyph,...]}}."""
    bank = defaultdict(lambda: defaultdict(list)); report = []
    for key, text, font in CS_SRC:
        p = f"{REF}/{key}.jpg"
        if not os.path.exists(p): continue
        gray = np.asarray(Image.open(p).convert("L"), np.uint8)
        letters = [c for c in text if c.isalpha()]
        gs = force_split(gray, len(letters))
        report.append(f"{key}: {len(gs)} blocks vs {len(letters)} letters")
        if len(gs) != len(letters): continue
        for i, g in enumerate(gs):
            bank[(letters[i].upper(), letters[i].isupper())][font].append(g)
    return bank, report

# ---------- alignment ----------
def aligned_glyphs(crop, text):
    gs = glyphs_pos(crop); letters = [c for c in text if c.isalnum()]
    if not gs or not letters: return None
    if len(gs) == len(letters):
        return [(letters[i].upper(), letters[i].isupper(), gs[i]) for i in range(len(letters))]
    return None                       # only CLEAN 1:1 labels drive the alphabet (letters must be reliable)

# ---------- bucketed same-letter matcher ----------
def build_buckets(alpha):
    by = defaultdict(list)
    for t in alpha: by[(t["L"], t["cap"])].append((t["style"], t["glyph"]))
    B = {}
    for k, items in by.items():
        M = np.array([g.astype(np.float32).ravel() for _, g in items], np.float32)
        M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)
        B[k] = (np.array([s for s, _ in items]), M)
    return B

def match_glyph(g, L, cap, B):
    key = (L, cap)
    if key not in B: return None, 0.0, 0.0
    styles_arr, Mn = B[key]
    r = sims_row(g, Mn); uniq = set(styles_arr.tolist())
    best = {s: float(r[styles_arr == s].max()) for s in uniq}
    rk = sorted(best.items(), key=lambda kv: -kv[1])
    return rk[0][0], rk[0][1], rk[0][1] - (rk[1][1] if len(rk) > 1 else rk[0][1])

def type_label(glyphs, B):
    """-> (winner, conf, voters[(style,score,margin)]).  voters = confident multi-style glyph matches."""
    voters = []
    for L, cap, g in glyphs:
        s, sc, m = match_glyph(g, L, cap, B)
        if s and sc >= GLYPH_MIN and m > 0: voters.append((s, sc, m))
    if not voters: return None, 0.0, voters
    w = defaultdict(float)
    for s, sc, m in voters: w[s] += sc * m
    winner = max(w, key=w.get)
    support = w[winner] / (sum(w.values()) + 1e-9)
    meansc = np.mean([sc for s, sc, m in voters if s == winner])
    return winner, float(support * meansc), voters

# ---------- data ----------
def load_and_align():
    if os.path.exists(CACHE):
        return pickle.load(open(CACHE, "rb"))
    labs = []
    for f in glob.glob(f"{DISC}/labels_*.json"):
        for Lb in json.load(open(f)):
            t = (Lb.get("crowd") or "").strip()
            if t and "box_g" in Lb: labs.append((Lb["box_g"], t))
    def work(job):
        box_g, text = job
        crop = crop_box(box_g)
        if crop is None: return None
        gl = aligned_glyphs(crop, text)
        return (text, gl) if gl else None
    out = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(work, labs):
            if r: out.append(r)
    pickle.dump(out, open(CACHE, "wb"))
    return out

def loo_seed(alpha):
    seed = [t for t in alpha if t["gen"] == 0 and t["cap"]]
    if len(seed) < 10: return 0.0, 0, {}
    keys = np.array([t["L"] for t in seed]); styles = np.array([t["style"] for t in seed])
    Mn = np.array([t["glyph"].astype(np.float32).ravel() for t in seed], np.float32); Mn /= (np.linalg.norm(Mn, axis=1, keepdims=True) + 1e-6)
    conf = Counter(); tot = Counter()
    for i in range(len(seed)):
        same = (keys == keys[i]) & (np.arange(len(seed)) != i)
        if len(set(styles[same])) < 2: continue
        r = np.where(same, sims_row(seed[i]["glyph"], Mn), -2.0)
        best = {s: float(r[(styles == s) & same].max()) for s in set(styles[same])}
        pred = max(best, key=best.get); conf[(styles[i], pred)] += 1; tot[styles[i]] += 1
    N = sum(tot.values())
    return sum(conf[(s, s)] for s in STYLES) / max(1, N), N, {s: round(conf[(s, s)] / max(1, tot[s]), 2) for s in STYLES}

def main():
    data = load_and_align()
    print(f"clean-aligned labels: {len(data)}", flush=True)
    alpha = []; cap_ct = Counter(); assigned = {}     # label_idx -> font

    def harvest(idx, font, gen):
        added = 0
        for L, cap, g in data[idx][1]:
            k = (L, cap, font)
            if cap_ct[k] < CAP:
                alpha.append(dict(L=L, cap=cap, style=font, glyph=g, gen=gen)); cap_ct[k] += 1; added += 1
        return added

    # SEED — spot the CS style-sheet capitals in real glyphs (same-letter, visual). Font ONLY from the CS.
    csb, csrep = cs_seed_templates()
    print("CS specimen segmentation:", csrep, flush=True)
    csmat = {}
    for (L, cap), fonts in csb.items():
        rows, fl = [], []
        for f, gs in fonts.items():
            for g in gs: rows.append(g.astype(np.float32).ravel()); fl.append(f)
        M = np.array(rows, np.float32); M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)
        csmat[(L, cap)] = (np.array(fl), M)
    cand = []                                          # (confidence, label_idx, font, score)
    for i, (text, gl) in enumerate(data):
        for L, cap, g in gl:
            if not cap or (L, cap) not in csmat: continue
            fl, M = csmat[(L, cap)]
            r = sims_row(g, M); best = {f: float(r[fl == f].max()) for f in set(fl.tolist())}
            rk = sorted(best.items(), key=lambda kv: -kv[1])
            margin = rk[0][1] - (rk[1][1] if len(rk) > 1 else 0.0)
            if len(rk) >= 2 and margin < SEED_MARGIN: continue     # ambiguous across fonts -> not a seed
            cand.append(((margin if len(rk) >= 2 else rk[0][1]), i, rk[0][0], rk[0][1]))
    seed_ct = defaultdict(int)
    for font in STYLES:
        for conf, i, f, score in sorted([c for c in cand if c[2] == font], key=lambda c: -c[0])[:SEED_PER_FONT]:
            if i in assigned: continue
            assigned[i] = f; harvest(i, f, 0); seed_ct[f] += 1
    print(f"SEED (CS-spot): {len(assigned)} labels -> {len(alpha)} glyphs; "
          f"per-font {{ {', '.join(f'{f}:{seed_ct[f]}' for f in STYLES)} }}", flush=True)
    # HITL montage: the capital glyphs harvested as each font's seed
    for font in STYLES:
        gl_caps = [g for i, f in assigned.items() if f == font for (L, cap, g) in data[i][1] if cap][:60]
        if not gl_caps: continue
        cols = 12; rows_ = (len(gl_caps) + cols - 1) // cols
        canvas = np.full((rows_ * (H + 2), cols * (W + 2)), 240, np.uint8)
        for j, g in enumerate(gl_caps):
            r0, c0 = (j // cols) * (H + 2), (j % cols) * (W + 2)
            canvas[r0:r0 + H, c0:c0 + W] = (1 - g.astype(np.uint8)) * 255
        Image.fromarray(canvas).save(f"{DISC}/seed_{font}.png")
    print(f"seed montages -> {DISC}/seed_{{italic,blackletter,upright}}.png (HITL verify)", flush=True)

    # PROPAGATE — co-occurrence BFS to convergence
    for rnd in range(1, MAX_ROUNDS + 1):
        B = build_buckets(alpha); new_assign = 0; new_glyphs = 0; mixed = 0
        for i, (text, gl) in enumerate(data):
            if i in assigned or len(gl) < MIN_GLYPHS: continue
            winner, conf, voters = type_label(gl, B)
            if not winner or conf < HIGH: continue
            vc = Counter(s for s, _, _ in voters); vw = defaultdict(float)
            for s, sc, m in voters: vw[s] += sc * m
            second = max((s for s in vw if s != winner), key=lambda s: vw[s], default=None)
            if second and vc[second] >= 2 and vw[second] > 0.55 * vw[winner]: mixed += 1; continue   # mixed-font guard
            if vc[winner] < 2: continue                                                              # need >=2 known letters to concur
            assigned[i] = winner; new_assign += 1; new_glyphs += harvest(i, winner, rnd)
        print(f"round {rnd}: +{new_assign} labels (+{new_glyphs} glyphs; {mixed} mixed); "
              f"alphabet={len(alpha)}; cells={len({(t['L'],t['cap']) for t in alpha})}", flush=True)
        if new_assign == 0: break

    # SAVE
    np.savez_compressed(OUT, glyphs=np.array([t["glyph"] for t in alpha], np.uint8),
                        letter=np.array([t["L"] for t in alpha]), cap=np.array([t["cap"] for t in alpha]),
                        style=np.array([t["style"] for t in alpha]), gen=np.array([t["gen"] for t in alpha]))
    # COVERAGE REPORT
    print(f"\nsaved {OUT}: {len(alpha)} glyphs, {len(assigned)}/{len(data)} labels assigned "
          f"({len(assigned)/len(data)*100:.0f}%)")
    print("per-font alphabet reach (distinct CAPITAL letters, /26):")
    for f in STYLES:
        caps = sorted({t["L"] for t in alpha if t["style"] == f and t["cap"] and t["L"].isalpha()})
        two = sorted({t["L"] for t in alpha if t["cap"] and t["L"].isalpha() and
                      len({u["style"] for u in alpha if u["L"] == t["L"] and u["cap"]}) >= 2 and
                      any(u["style"] == f and u["L"] == t["L"] and u["cap"] for u in alpha)})
        print(f"  {f:11s} {len(caps):>2d}/26  {''.join(caps)}")
    cells = defaultdict(set)
    for t in alpha:
        if t["cap"] and t["L"].isalpha(): cells[t["L"]].add(t["style"])
    typeable = sorted(L for L, v in cells.items() if len(v) >= 2)
    missing = sorted(set("ABCDEFGHIJKLMNOPQRSTUVWXYZ") - set(typeable))
    print(f"typeable capital letters (>=2 fonts): {len(typeable)}/26  [{''.join(typeable)}]  MISSING: {''.join(missing)}")

if __name__ == "__main__":
    main()
