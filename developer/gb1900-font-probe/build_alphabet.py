"""Phase C — CO-OCCURRENCE BFS alphabet builder (SG's fan-out; validated by same_letter_test @ 0.91).

A GB1900 label is one uniform font, and the crowd transcript tells us which glyph is which letter. So once
ONE letter of a label is known-font, the label's OTHER letters are the same font — co-occurrence propagates
font-identity, and iterating fans out over the whole alphabet (BFS: every letter attested in a font
co-occurs, in some label, with a letter already known in that font, so it is reached).

  SEED       assign font to labels the TEXT alone pins (italic<-watercourses, upright<-churches,
             blackletter<-antiquity descriptors) — this is the CS category->font map applied wholesale, i.e.
             many CS-grounded seed letters at once. Harvest all their glyphs as REAL-domain templates
             (crossing the CS->real gap only here, corroborated by text).
  PROPAGATE  type every remaining label by SAME-LETTER match to the current alphabet; assign its font only
             when >=2 known letters CONCUR (corroboration) and no strong internal disagreement (mixed-font
             guard: appended Ch./B.M. qualifiers); optional text category-prior as extra corroboration.
             Harvest the assigned label's glyphs -> grows the alphabet to NEW letters. Iterate to convergence.
  REPORT     coverage matrix: independently-attested (letter,case,font) cells vs filled; per-font A-Z reach;
             SEED leave-one-out each round to watch for drift.

Saves alphabet.npz. Matching bucketed by (letter,case). Aligned glyphs cached to aligned_cache.pkl.

    /vast/ishi/envs/boundary/bin/python build_alphabet.py
"""
import os, re, glob, json, math, pickle, numpy as np, cv2
import concurrent.futures as cf
from collections import Counter, defaultdict
from PIL import Image
from discrim_test import norm_glyph, sims_row, crop_box, H, W
from same_letter_test import glyphs_pos, style_of

DISC = "/vast/ishi/gb1900/edition/discover"; OUT = f"{DISC}/alphabet.npz"; CACHE = f"{DISC}/aligned_cache.pkl"
STYLES = ["italic", "blackletter", "upright"]
HIGH = 0.45            # label confidence to assign
CAP = 80              # max templates per (letter,case,style)
GLYPH_MIN = 0.40       # per-glyph match score to count as a voter
MIN_GLYPHS = 4         # a label needs this many aligned glyphs to propagate from
MAX_ROUNDS = 8

# text category-prior font (corroboration only) — extends style_of a little
def font_prior(t): return style_of(t)

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
    attested_indep = set()                            # (L,cap,font) attested via text category alone

    def harvest(idx, font, gen):
        added = 0
        for L, cap, g in data[idx][1]:
            k = (L, cap, font)
            if cap_ct[k] < CAP:
                alpha.append(dict(L=L, cap=cap, style=font, glyph=g, gen=gen)); cap_ct[k] += 1; added += 1
        return added

    # SEED — labels the text alone pins
    for i, (text, gl) in enumerate(data):
        f = font_prior(text)
        if not f: continue
        assigned[i] = f
        for L, cap, g in gl: attested_indep.add((L, cap, f))
        harvest(i, f, 0)
    acc, N, rec = loo_seed(alpha)
    print(f"SEED: {len(assigned)} labels -> {len(alpha)} glyphs; cells={len({(t['L'],t['cap']) for t in alpha})}; "
          f"LOO acc={acc:.3f} (N={N}) {rec}", flush=True)

    # PROPAGATE — co-occurrence BFS to convergence
    for rnd in range(1, MAX_ROUNDS + 1):
        B = build_buckets(alpha); new_assign = 0; new_glyphs = 0; mixed = 0; conflict = 0
        for i, (text, gl) in enumerate(data):
            if i in assigned or len(gl) < MIN_GLYPHS: continue
            winner, conf, voters = type_label(gl, B)
            if not winner or conf < HIGH: continue
            vc = Counter(s for s, _, _ in voters); vw = defaultdict(float)
            for s, sc, m in voters: vw[s] += sc * m
            second = max((s for s in vw if s != winner), key=lambda s: vw[s], default=None)
            if second and vc[second] >= 2 and vw[second] > 0.55 * vw[winner]: mixed += 1; continue   # mixed-font guard
            if vc[winner] < 2: continue                                                              # need corroboration
            pr = font_prior(text)
            if pr and pr != winner: conflict += 1; continue                                          # category veto
            assigned[i] = winner; new_assign += 1; new_glyphs += harvest(i, winner, rnd)
        acc, N, rec = loo_seed(alpha)
        print(f"round {rnd}: +{new_assign} labels (+{new_glyphs} glyphs; {mixed} mixed, {conflict} conflict); "
              f"alphabet={len(alpha)}; cells={len({(t['L'],t['cap']) for t in alpha})}; LOO acc={acc:.3f} {rec}", flush=True)
        if new_assign == 0: break

    # SAVE
    np.savez_compressed(OUT, glyphs=np.array([t["glyph"] for t in alpha], np.uint8),
                        letter=np.array([t["L"] for t in alpha]), cap=np.array([t["cap"] for t in alpha]),
                        style=np.array([t["style"] for t in alpha]), gen=np.array([t["gen"] for t in alpha]))
    # COVERAGE REPORT
    filled = {(t["L"], t["cap"], t["style"]) for t in alpha}
    print(f"\nsaved {OUT}: {len(alpha)} glyphs, {len(assigned)}/{len(data)} labels assigned "
          f"({len(assigned)/len(data)*100:.0f}%)")
    print(f"independently-attested cells (text category): {len(attested_indep)}; of those filled: "
          f"{len(attested_indep & filled)} ({len(attested_indep & filled)/max(1,len(attested_indep))*100:.0f}%)")
    print(f"cells reached BEYOND text-attestation (via BFS propagation): {len(filled - attested_indep)}")
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
