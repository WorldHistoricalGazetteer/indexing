"""BIGCAPS letters as connected components, grouped into labels by collinearity.

This does not use Hi-SAM boxes, MapReader boxes or the recogniser. Admin lettering is LETTER-SPACED by design
— that spacing is why MapReader's word spotter never fired on it — so on a cleaned sheet each capital is its
own connected component and needs no segmentation guesswork at all. That is the opposite of the small kerned
mixed-case text, where splitting a word into letters meant inferring boundaries that the ink does not mark,
and where the per-letter alphabet consequently came out as smears.

Earlier evidence against per-letter work does not apply here and should not be cited as though it did: the
4.7% ink-gap figure was measured on MapReader's small words, and the 8.6% figure counted vertical projection
runs on uncleaned crops with a threshold a six-letter label could not reach. Neither tested this.

Grouping is by COLLINEARITY on a STRAIGHT line — BIGCAPS carry no curvature. That makes the residual a much
sharper test than a curve fit would be: a quadratic can absorb three arbitrary points and accept a group of
unrelated ink, whereas a straight fit with a tight residual accepts only what genuinely sits on one baseline.
A label set on two or three lines is recovered afterwards, by merging groups that are parallel, equal in
height and stacked within a couple of letter-heights.

    python bigcaps_components.py --tag sheet_ENG_218_NW --bbox -1.5875 53.7823 -1.514 53.8115
"""
import argparse, base64, io, json, math, os, sys
from collections import Counter
import numpy as np
import cv2

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_font_testset_v2 import _get_tile

N17 = 1 << 17


def flat_field(g, k=101):
    """Divide out paper tone. Engraved sheets vary in exposure across a scan, so a single global threshold
    keeps ink in one corner and loses it in another."""
    bg = cv2.medianBlur(g, k)
    bg = np.maximum(bg, 1)
    out = (g.astype(np.float32) / bg.astype(np.float32)) * 200.0
    return np.clip(out, 0, 255).astype(np.uint8)


def components(bw, min_h, max_h, min_fill=0.12, max_fill=0.92, min_ar=0.12, max_ar=2.2):
    """Candidate letters: connected components the right size and shape to be a capital.

    The filters are all shape, never position, so nothing here presumes where a label is. Long thin runs
    (boundaries, railways, contours) fail the aspect test; hatching blocks fail the fill test.
    """
    n, lab, stats, cent = cv2.connectedComponentsWithStats((bw > 0).astype(np.uint8), 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (min_h <= h <= max_h):
            continue
        ar = w / max(1.0, h)
        if not (min_ar <= ar <= max_ar):
            continue
        fill = area / max(1.0, w * h)
        if not (min_fill <= fill <= max_fill):
            continue
        out.append(dict(x=int(x), y=int(y), w=int(w), h=int(h), area=int(area),
                        cx=float(cent[i][0]), cy=float(cent[i][1]), idx=int(i)))
    return out, lab


def bridge_runs(runs, fit, h_tol=0.32, max_k=3, pitch_tol=0.22, ang_tol=6.0, stats=None):
    """Rejoin runs that one dropped letter tore apart.

    A letter lost to the class map or to a shape filter leaves a hole, and in letter-spaced admin lettering
    a hole is wide: at a pitch of ~3x the letter height, one missing capital puts the survivors ~6 heights
    apart, far beyond any join threshold that is safe to apply blindly. MID and LETO were separated exactly
    so, by a lost D.

    Raising the threshold is the wrong remedy — it would let unrelated ink in at the same time. What makes
    the join safe here is the letter-spacing itself: if two runs are two halves of one label, the distance
    between their facing letters must be a whole number of that label's own pitch, because the gap is made
    of missing letters. So the test is not "near enough" but "an integer number of letters apart", and the
    merged run must still fit one straight baseline. Both runs' own pitch is used where both have one, so
    the estimate does not come from the side being tested.
    """
    runs = sorted(runs, key=lambda r: r["x0"])
    changed = True
    while changed:
        changed = False
        for i in range(len(runs)):
            if runs[i] is None:
                continue
            for j in range(len(runs)):
                if i == j or runs[j] is None:
                    continue
                a, b = runs[i], runs[j]
                if a["x1"] > b["x0"]:                       # a must be the left-hand run
                    continue
                if b["x0"] - a["x1"] > (max_k + 0.5) * 4.0 * a["h"]:
                    break                                   # sorted by x0: everything further is further
                hm = max(a["h"], b["h"])
                if abs(a["h"] - b["h"]) / hm > h_tol:
                    continue
                pitches = [r["pitch"] for r in (a, b) if r["pitch"] > 0]
                if not pitches:
                    continue
                pitch = float(np.mean(pitches))
                k = (b["x0"] - a["x1"]) / pitch             # facing letters, centre to centre
                if k < 1.5 or k > max_k + 0.5:              # 1 pitch is a plain neighbour, not a hole
                    continue
                if abs(k - round(k)) > pitch_tol:           # the hole is not a whole number of letters
                    continue
                if a["n"] > 1 and b["n"] > 1 and abs(
                        math.degrees(math.atan(a["slope"]) - math.atan(b["slope"]))) > ang_tol:
                    continue
                m = fit(a["members"] + b["members"])        # and it must still be one straight baseline
                if m is None:
                    continue
                runs[i], runs[j] = m, None
                if stats is not None:
                    stats["bridged over a dropped letter"] += 1
                changed = True
                break
    return [r for r in runs if r is not None]


def group_letters(cs, max_gap_mult=3.2, h_tol=0.32, max_resid=0.16, min_len=3,
                  max_k=3, pitch_tol=0.22, stats=None):
    """Chain components into labels, then keep the chains that lie on one straight baseline.

    Two components join if they are close relative to their height and of similar height — a label's letters
    are one size, and the spacing scales with the size. The chain is then fitted with a straight line; a real
    label's letters sit on it within a fraction of their own height, incidental ink does not.
    """
    if not cs:
        return []
    P = np.array([[c["cx"], c["cy"]] for c in cs])
    H = np.array([c["h"] for c in cs], float)
    parent = list(range(len(cs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    order = np.argsort(P[:, 0])
    for oi in range(len(order)):
        i = order[oi]
        for oj in range(oi + 1, len(order)):
            j = order[oj]
            dx = P[j, 0] - P[i, 0]
            hmax = max(H[i], H[j])
            if dx > max_gap_mult * hmax:
                break
            if abs(H[i] - H[j]) / hmax > h_tol:
                continue
            if abs(P[j, 1] - P[i, 1]) > 1.1 * hmax:      # same line, not the one above
                continue
            if math.hypot(dx, P[j, 1] - P[i, 1]) > max_gap_mult * hmax:
                continue
            union(i, j)

    groups = {}
    for i in range(len(cs)):
        groups.setdefault(find(i), []).append(i)

    def fit(members):
        """Describe a run of components, or None if they do not sit on one straight baseline."""
        members = sorted(members, key=lambda i: P[i, 0])
        xs, ys = P[members, 0], P[members, 1]
        h = float(np.median(H[members]))
        if len(members) == 1:
            return dict(members=members, h=h, resid=0.0, slope=0.0, x0=float(xs[0]), x1=float(xs[0]),
                        ymid=float(ys[0]), n=1, pitch=0.0, spacing=0.0, spacing_cv=0.0)
        try:
            co = np.polyfit(xs, ys, 1)          # straight only: BIGCAPS are not set on a curve
            resid = float(np.sqrt(np.mean((np.polyval(co, xs) - ys) ** 2)))
        except Exception:
            return None
        if resid > max_resid * h:
            return None
        gaps = np.diff(xs)
        return dict(members=members, h=h, resid=round(resid / h, 3),
                    slope=float(co[0]), x0=float(xs.min()), x1=float(xs.max()),
                    ymid=float(np.median(ys)), n=len(members),
                    pitch=float(np.median(gaps)),
                    spacing=round(float(np.median(gaps) / h), 2),
                    spacing_cv=round(float(np.std(gaps) / max(1e-6, np.mean(gaps))), 2)
                    if len(gaps) > 1 else 0.0)

    runs = [r for r in (fit(m) for m in groups.values()) if r]
    runs = bridge_runs(runs, fit, h_tol=h_tol, max_k=max_k, pitch_tol=pitch_tol, stats=stats)
    out = [r for r in runs if r["n"] >= min_len]
    # A multi-line label is several parallel groups of equal height, stacked. Merged after the line fit
    # rather than before it, so the straight-line residual stays a strict test on each line.
    out.sort(key=lambda g: (g["ymid"], g["x0"]))
    used, merged = set(), []
    for i, g in enumerate(out):
        if i in used:
            continue
        stack = [g]
        used.add(i)
        for j in range(i + 1, len(out)):
            if j in used:
                continue
            o = out[j]
            hm = max(g["h"], o["h"])
            if abs(g["h"] - o["h"]) / hm > 0.28:
                continue
            if abs(o["ymid"] - stack[-1]["ymid"]) > 2.4 * hm:
                continue
            if abs(math.degrees(math.atan(o["slope"]) - math.atan(g["slope"]))) > 8:
                continue
            if min(g["x1"], o["x1"]) - max(g["x0"], o["x0"]) < -1.5 * hm:   # must overlap horizontally
                continue
            stack.append(o)
            used.add(j)
        if len(stack) > 1:
            m = dict(stack[0])
            m["members"] = [k for st in stack for k in st["members"]]
            m["n"] = sum(st["n"] for st in stack)
            m["lines"] = len(stack)
            merged.append(m)
        else:
            g["lines"] = 1
            merged.append(g)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("W", "S", "E", "N"))
    ap.add_argument("--win", type=int, default=8, help="window side in tiles; overlap keeps labels whole")
    ap.add_argument("--overlap", type=int, default=2)
    ap.add_argument("--min-h", type=int, default=20, help="px; below this it is ordinary lettering")
    ap.add_argument("--max-h", type=int, default=260)
    ap.add_argument("--min-letters", type=int, default=3)
    ap.add_argument("--labels-png", default=None,
                    help="per-pixel class map from rf_clean.py apply --out-labels. A component is kept only "
                         "if most of its ink is classified TEXT. Filtering components beats erasing pixels: "
                         "erasure can eat a letter, whereas a mis-scored component is merely dropped")
    ap.add_argument("--min-text", type=float, default=0.5)
    ap.add_argument("--out", default="bigcaps_groups.json")
    ap.add_argument("--out-glyphs", default=None,
                    help="npz of height-normalised letter rasters, for overlay clustering")
    ap.add_argument("--glyph-h", type=int, default=40, help="cap height each group is scaled to")
    ap.add_argument("--canvas-h", type=int, default=56)
    ap.add_argument("--canvas-w", type=int, default=64)
    ap.add_argument("--qc", default="bigcaps_groups_qc.html")
    ap.add_argument("--qc-n", type=int, default=250)
    a = ap.parse_args()

    def lat_px(lat):
        return (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256

    w, s, e, n = a.bbox
    tx0 = int(((w + 180.0) / 360.0 * N17 * 256) // 256)
    tx1 = int(((e + 180.0) / 360.0 * N17 * 256) // 256)
    ty0, ty1 = int(lat_px(n) // 256), int(lat_px(s) // 256)
    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    step = a.win - a.overlap
    print(f"{a.tag}: {nx}x{ny} tiles", flush=True)

    LABMAP = None
    if a.labels_png:
        LABMAP = cv2.imread(a.labels_png, cv2.IMREAD_GRAYSCALE)
        print(f"class map {a.labels_png}: {None if LABMAP is None else LABMAP.shape}", flush=True)
    TEXT_CLASS = 1                     # index of "text" in rf_clean.CLASSES

    # PASS 1 — collect components in SHEET coordinates.
    #
    # The windowing is a memory device, nothing more, so it must not be visible in the result. Grouping
    # inside each window and then discarding whichever copy of a label arrived second cannot work: a label
    # straddling a window edge is not seen twice, it is seen in halves, and two halves with different
    # centres are not duplicates of each other — that is how MOOR and OOR, and MIDDLETON MOOR and LET/MID/
    # MOOR/ENCL, both survived into the same output. Components are therefore accumulated first and grouped
    # once, over the whole sheet.
    #
    # A component clipped by a window edge is dropped there rather than kept in part: the overlap (512px)
    # is wider than max_h, so it is present whole in the neighbouring window. Only the sheet's own border
    # is exempt, there being no neighbour to recover it from.
    pool, seen, stats = {}, set(), Counter()
    for i in range(0, nx, step):
        for j in range(0, ny, step):
            wx0, wy0 = tx0 + i, ty0 + j
            canvas = np.full((a.win * 256, a.win * 256), 255, np.uint8)
            hit = 0
            for u in range(a.win):
                for v in range(a.win):
                    t = _get_tile(wx0 + u, wy0 + v)
                    if t is not None:
                        canvas[v * 256:(v + 1) * 256, u * 256:(u + 1) * 256] = t
                        hit += 1
            if hit == 0:
                continue
            g = flat_field(canvas)
            _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            cs, lab = components(bw, a.min_h, a.max_h)
            stats["components"] += len(cs)
            side = a.win * 256
            # sheet-pixel origin of this window, and of the sheet itself
            ox, oy = wx0 * 256, wy0 * 256
            sx0, sy0, sx1, sy1 = tx0 * 256, ty0 * 256, (tx1 + 1) * 256, (ty1 + 1) * 256
            for c in cs:
                gx0, gy0 = ox + c["x"], oy + c["y"]
                gx1, gy1 = gx0 + c["w"], gy0 + c["h"]
                clipped = ((c["x"] == 0 and gx0 > sx0) or (c["y"] == 0 and gy0 > sy0)
                           or (c["x"] + c["w"] >= side and gx1 < sx1)
                           or (c["y"] + c["h"] >= side and gy1 < sy1))
                if clipped:
                    stats["clipped at window edge"] += 1
                    continue
                key = (gx0, gy0, c["w"], c["h"])       # identical pixels in every window that sees it
                if key in seen:
                    stats["duplicate across windows"] += 1
                    continue
                if LABMAP is not None:
                    # the class map covers the whole sheet from the sheet origin
                    ys, xs = np.where(lab[c["y"]:c["y"] + c["h"], c["x"]:c["x"] + c["w"]] == c["idx"])
                    ly_, lx_ = ys + gy0 - sy0, xs + gx0 - sx0
                    m = ((ly_ >= 0) & (ly_ < LABMAP.shape[0]) & (lx_ >= 0) & (lx_ < LABMAP.shape[1]))
                    if not m.any():
                        continue
                    frac = float((LABMAP[ly_[m], lx_[m]] == TEXT_CLASS).mean())
                    if frac < a.min_text:
                        stats["rejected as not text"] += 1
                        continue
                    c["text_frac"] = round(frac, 2)
                seen.add(key)
                sub = (lab[c["y"]:c["y"] + c["h"], c["x"]:c["x"] + c["w"]] == c["idx"])
                pool[key] = dict(x=gx0, y=gy0, w=c["w"], h=c["h"], area=c["area"],
                                 cx=ox + c["cx"], cy=oy + c["cy"], text_frac=c.get("text_frac"),
                                 png=base64.b64encode(
                                     cv2.imencode(".png", (255 - sub.astype(np.uint8) * 255))[1]).decode())

    # PASS 2 — group once, over the whole sheet, so a label is whole however the windows fell.
    cs = list(pool.values())
    groups = []
    for gr in group_letters(cs, min_len=a.min_letters, stats=stats):
        mem = [cs[k] for k in gr["members"]]
        groups.append(dict(tag=a.tag,
                           gcx=round(float(np.mean([m["cx"] for m in mem])), 1),
                           gcy=round(float(np.mean([m["cy"] for m in mem])), 1),
                           bbox=[min(m["x"] for m in mem), min(m["y"] for m in mem),
                                 max(m["x"] + m["w"] for m in mem), max(m["y"] + m["h"] for m in mem)],
                           h=round(gr["h"], 1), n=gr["n"], resid=gr["resid"],
                           lines=gr.get("lines", 1), spacing=gr["spacing"], spacing_cv=gr["spacing_cv"],
                           letters=[{k: m[k] for k in ("x", "y", "w", "h", "png")} for m in mem]))
    stats["groups"] = len(groups)
    print(f"  {stats['components']} raw components "
          f"({stats['duplicate across windows']} seen again in an overlap, "
          f"{stats['clipped at window edge']} clipped at a window edge and left to the neighbour, "
          f"{stats['rejected as not text']} rejected as not text) "
          f"-> {len(cs)} distinct, {stats['groups']} groups "
          f"({stats['bridged over a dropped letter']} runs rejoined across a dropped letter)", flush=True)
    if groups:
        hh = np.array([g["h"] for g in groups])
        nn = np.array([g["n"] for g in groups])
        sp = np.array([g["spacing"] for g in groups])
        print(f"  letter height: median {np.median(hh):.0f}px  p90 {np.percentile(hh,90):.0f}px")
        print(f"  letters per group: median {np.median(nn):.0f}  max {nn.max()}")
        print(f"  spacing (gap/height): median {np.median(sp):.2f}  "
              f"— letter-spaced admin lettering sits high, ordinary words near 1.0")
    json.dump(dict(groups=[{k: v for k, v in g.items() if k != "letters"} for g in groups]),
              open(a.out, "w"), indent=1)
    print(f"wrote {a.out}")

    if a.out_glyphs:
        # Scaled by the GROUP's cap height, not each component's own: within one label the letters are set
        # at one size, so the group height is the true reference and an O's overshoot or a Q's tail stays
        # visible as the difference it is. Normalising each letter to its own bbox would flatten exactly
        # the proportions a face is recognised by.
        G, meta = [], []
        for gi, g in enumerate(groups):
            sc = a.glyph_h / max(1.0, g["h"])
            for li, l in enumerate(g["letters"]):
                m = cv2.imdecode(np.frombuffer(base64.b64decode(l["png"]), np.uint8),
                                 cv2.IMREAD_GRAYSCALE) < 128
                gh, gw = max(1, int(round(m.shape[0] * sc))), max(1, int(round(m.shape[1] * sc)))
                if gh > a.canvas_h or gw > a.canvas_w:
                    continue                              # taller/wider than the frame: not a capital
                r = cv2.resize(m.astype(np.uint8) * 255, (gw, gh), interpolation=cv2.INTER_AREA)
                cv_ = np.zeros((a.canvas_h, a.canvas_w), np.uint8)
                oy_, ox_ = (a.canvas_h - gh) // 2, (a.canvas_w - gw) // 2
                cv_[oy_:oy_ + gh, ox_:ox_ + gw] = r
                G.append(cv_ > 127)
                meta.append((gi, li, g["tag"], l["x"], l["y"], l["w"], l["h"], g["h"]))
        np.savez_compressed(a.out_glyphs, glyphs=np.array(G, bool),
                            group=np.array([m[0] for m in meta], np.int32),
                            slot=np.array([m[1] for m in meta], np.int32),
                            tag=np.array([m[2] for m in meta]),
                            xywh=np.array([m[3:7] for m in meta], np.int32),
                            group_h=np.array([m[7] for m in meta], np.float32))
        print(f"wrote {a.out_glyphs}: {len(G)} glyphs at {a.canvas_h}x{a.canvas_w}, "
              f"cap height {a.glyph_h}px")

    groups.sort(key=lambda g: -g["h"])
    open(a.qc, "w").write(QC.replace("__DATA__", json.dumps(dict(items=groups[: a.qc_n]))))
    print(f"wrote {a.qc} ({os.path.getsize(a.qc)/1e6:.2f} MB)")
    print("COMPDONE", flush=True)


QC = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · BIGCAPS components</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;z-index:9;
        display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 header input{width:72px} header b{margin-right:4px}
 .sp{flex:1}
 .it{background:#fff;border:1px solid #ddd;border-radius:6px;margin:8px 12px;padding:8px 10px}
 .it.ok{border-color:#3a7d44;box-shadow:inset 4px 0 #3a7d44}
 .it.no{border-color:#b03030;opacity:.45}
 .row{display:flex;gap:6px;align-items:flex-end;flex-wrap:wrap;min-height:60px}
 .row img{background:#fff;border:1px solid #eee;image-rendering:pixelated;height:56px;cursor:pointer}
 .row img.x{opacity:.25;border-color:#b03030}
 .m{font-size:11px;color:#666;margin-top:5px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .m button{font:11px system-ui;padding:1px 8px;cursor:pointer}
 .tx{font:13px system-ui;width:15em;padding:2px 4px}
 a{color:#36c}
</style>
<header><b>BIGCAPS components</b> — each row is one collinear group, each image one connected component.
 <span>hide h&lt;<input type=number id=minh value=0></span>
 <span><label><input type=checkbox id=only> only undecided</label></span>
 <span class=sp></span><span id=st></span>
 <button onclick=dl()>download decisions</button></header>
<div id=w></div>
<script>
const D=__DATA__, K='bigcaps_qc_'+(D.items[0]||{}).tag;
let V=JSON.parse(localStorage.getItem(K)||'{}');
const id=i=>i.tag+'@'+i.bbox.join('_');
// A group is only useful as alphabet material if the transcription is known, so the accept step asks for
// it. Rejecting a single letter is separate: a group is often right except for one blob of map furniture.
function draw(){
 const mh=+document.getElementById('minh').value||0, only=document.getElementById('only').checked;
 let shown=0;
 document.getElementById('w').innerHTML=D.items.filter(i=>i.h>=mh&&!(only&&V[id(i)])).map((i,k)=>{
  const v=V[id(i)]||{}; shown++;
  return `<div class="it ${v.ok===true?'ok':v.ok===false?'no':''}" data-i="${D.items.indexOf(i)}">
   <div class=row>${i.letters.map((l,j)=>
     `<img class="${(v.drop||[]).includes(j)?'x':''}" data-j="${j}"
       src="data:image/png;base64,${l.png}" title="click: not a letter">`).join('')}</div>
   <div class=m>
    <input class=tx placeholder="transcription" value="${v.text||''}">
    <button data-a=y>✓ label</button><button data-a=n>✗ reject</button>
    <span>${i.n} letters · height ${i.h}px · spacing ${i.spacing}&times;h (cv ${i.spacing_cv})
     · residual ${i.resid}&times;h · ${i.lines} line(s)</span>
    <a href="#" data-a=loc>sheet px ${i.bbox[0]},${i.bbox[1]}</a>
   </div></div>`;}).join('');
 stat(shown);
}
function stat(shown){
 document.getElementById('st').textContent=(shown!==undefined?shown+' shown · ':'')
   +Object.values(V).filter(v=>v.ok).length+' accepted, '
   +Object.values(V).filter(v=>v.ok===false).length+' rejected';
}
const save=()=>localStorage.setItem(K,JSON.stringify(V));
document.getElementById('w').addEventListener('click',e=>{
 const it=e.target.closest('.it'); if(!it) return;
 const i=D.items[+it.dataset.i], k=id(i);
 // A click that lands on none of the controls must leave no trace: recording an empty decision here
 // would make an untouched group indistinguishable from a considered one when the file is read back.
 if(e.target.dataset.a==='loc'){ e.preventDefault();
  navigator.clipboard&&navigator.clipboard.writeText(i.bbox.join(' ')); e.target.textContent='copied';
  return;
 }
 if(e.target.tagName==='IMG'){
  V[k]=V[k]||{};
  const j=+e.target.dataset.j, d=new Set(V[k].drop||[]);
  d.has(j)?d.delete(j):d.add(j); V[k].drop=[...d]; e.target.classList.toggle('x');
 } else if(e.target.dataset.a==='y'||e.target.dataset.a==='n'){
  V[k]=V[k]||{};
  V[k].ok=e.target.dataset.a==='y';
  V[k].text=it.querySelector('.tx').value.trim().toUpperCase();
  it.className='it '+(V[k].ok?'ok':'no');
 } else return;
 save(); stat();
});
document.getElementById('w').addEventListener('change',e=>{
 if(!e.target.classList.contains('tx')) return;
 const it=e.target.closest('.it'), i=D.items[+it.dataset.i], k=id(i);
 const t=e.target.value.trim().toUpperCase();
 if(!t&&!V[k]) return;
 V[k]=V[k]||{}; V[k].text=t; save();
});
document.getElementById('minh').onchange=draw; document.getElementById('only').onchange=draw;
function dl(){const b=new Blob([JSON.stringify(V,null,1)],{type:'application/json'}),
 a=document.createElement('a'); a.href=URL.createObjectURL(b);
 a.download='bigcaps_decisions_'+(D.items[0]||{}).tag+'.json'; a.click();}
draw();
</script>
"""

if __name__ == "__main__":
    main()
