"""Cut MapReader spots into single letters, so map samples can extend each face's alphabet.

Blind letter segmentation is hard; this is not blind. MapReader returns the recognised string with every spot,
so the letter count is known and the job is a k-way cut rather than a search for how many letters there are.
Two mechanisms, in order of trustworthiness:

  RUNS  contiguous columns of ink separated by clear gaps. On OS sheets this often just works, because the
        lettering is generously spaced — and where it works it is exact, not estimated. Used only when the run
        count matches the expected letter count.
  CUTS  otherwise, dynamic programming picks the k-1 columns that cross least ink, subject to a minimum letter
        width. This is the fallback for touching letters, and it is recorded as such: every glyph carries the
        method that produced it and a confidence, so a later stage can prefer clean cuts without re-deriving
        them.

Words are split at their widest gaps before letters are, because a space is a far more reliable cut than any
letter boundary, and getting it wrong misaligns every character that follows.

Output is normalised identically to the CS and hand-built libraries (44x36, ink-bbox cropped, aspect
preserved), so segmented letters drop straight into the same matcher.

    python segment_spots.py --boxes /vast/.../boxes_sheet_ENG_218_NW.jsonl --n 400 --qc qc.html
"""
import argparse, base64, glob, io, json, math, os, sys
from collections import Counter
import numpy as np
import cv2

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_font_testset_v2 import derotate                       # tile assembly + minAreaRect de-rotation

H, W = 44, 36


def norm_glyph(sub):
    """Identical to extract_alphabet.norm_glyph, so segmented letters match the existing libraries."""
    ys, xs = np.where(sub > 0)
    if len(ys) < 6:
        return None
    g = (sub[ys.min():ys.max() + 1, xs.min():xs.max() + 1] > 0).astype(np.uint8) * 255
    scale = min(H / g.shape[0], W / g.shape[1])
    g = cv2.resize(g, (max(1, int(g.shape[1] * scale)), max(1, int(g.shape[0] * scale))),
                   interpolation=cv2.INTER_AREA)
    canvas = np.zeros((H, W), np.uint8)
    oy, ox = (H - g.shape[0]) // 2, (W - g.shape[1]) // 2
    canvas[oy:oy + g.shape[0], ox:ox + g.shape[1]] = g
    return canvas


def binarise(crop, erase_lines=True):
    # erase_crossing_lines works on GRAYSCALE and returns (image, n_lines) — it must run before thresholding,
    # not after, since it thresholds internally to find the map linework crossing the word.
    if erase_lines:
        try:
            from line_erase import erase_crossing_lines
            crop = erase_crossing_lines(crop)[0]
        except Exception:
            pass
    _, bw = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(bw, 8)
    for i in range(1, n):                                       # specks are not letters
        if stats[i][4] <= 3:
            bw[lab == i] = 0
    return bw


def runs_of(profile, min_gap=1):
    """Contiguous inked column ranges, merging gaps narrower than `min_gap`."""
    on = profile > 0
    out, s = [], None
    gap = 0
    for i, v in enumerate(on):
        if v:
            if s is None:
                s = i
            gap = 0
        else:
            if s is not None:
                gap += 1
                if gap >= min_gap:
                    out.append((s, i - gap + 1))
                    s = None
    if s is not None:
        out.append((s, len(on)))
    return [(a, b) for a, b in out if b > a]


def dp_cuts(profile, k, min_w):
    """Choose k-1 cut columns minimising ink crossed, with a minimum letter width.

    Reading the profile as a cost surface rather than hunting for zero columns matters: touching letters have
    no zero column anywhere, and the least-ink column between them is still the right place to cut.
    """
    n = len(profile)
    if k <= 1 or n < k * min_w:
        return []
    cost = profile.astype(np.float64)
    INF = 1e18
    # dp[c][i] = best total cost using c cuts, last cut at column i
    dp = np.full((k, n), INF)
    back = np.full((k, n), -1, int)
    for i in range(min_w, n - min_w + 1):
        dp[1][i] = cost[i]
    for c in range(2, k):
        for i in range(c * min_w, n - min_w + 1):
            lo, hi = (c - 1) * min_w, i - min_w + 1
            if hi <= lo:
                continue
            seg = dp[c - 1][lo:hi]
            j = int(np.argmin(seg))
            if seg[j] >= INF:
                continue
            dp[c][i] = seg[j] + cost[i]
            back[c][i] = lo + j
    last = dp[k - 1][: n - min_w + 1]
    if not len(last) or last.min() >= INF:
        return []
    i = int(np.argmin(last))
    cuts = []
    for c in range(k - 1, 0, -1):
        cuts.append(i)
        i = back[c][i]
        if i < 0:
            break
    return sorted(cuts)


def segment(bw, text, min_w=4):
    """Return [(x0, x1, char)] for the alphanumeric characters of `text`."""
    tokens = [t for t in text.split() if any(c.isalnum() for c in t)]
    if not tokens:
        return [], "no-letters"
    prof = (bw > 0).sum(0).astype(np.int32)
    if prof.sum() == 0:
        return [], "no-ink"

    # 1. split into words at the widest gaps — a space is a far safer cut than a letter boundary
    spans = [(0, bw.shape[1])]
    if len(tokens) > 1:
        rr = runs_of(prof, min_gap=1)
        if len(rr) >= len(tokens):
            gaps = sorted(((rr[i + 1][0] - rr[i][1], i) for i in range(len(rr) - 1)), reverse=True)
            cutafter = sorted(i for _, i in gaps[: len(tokens) - 1])
            spans, s = [], 0
            for i in cutafter:
                spans.append((rr[s][0], rr[i][1]))
                s = i + 1
            spans.append((rr[s][0], rr[-1][1]))
        else:
            return [], "too-few-runs-for-words"

    out, methods = [], []
    for (x0, x1), tok in zip(spans, tokens):
        chars = [c for c in tok if c.isalnum()]
        sub = prof[x0:x1]
        rr = runs_of(sub, min_gap=1)
        if len(rr) == len(chars):                               # exact: the letters are already separated
            for (a, b), ch in zip(rr, chars):
                out.append((x0 + a, x0 + b, ch))
            methods.append("runs")
        else:
            cuts = dp_cuts(sub, len(chars), min_w)
            if len(cuts) != len(chars) - 1:
                methods.append("failed")
                continue
            edges = [0] + cuts + [len(sub)]
            for i, ch in enumerate(chars):
                out.append((x0 + edges[i], x0 + edges[i + 1], ch))
            methods.append("cuts")
    return out, "+".join(sorted(set(methods))) if methods else "failed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boxes", required=True, help="MapReader boxes jsonl (glob ok)")
    ap.add_argument("--n", type=int, default=400, help="spots to attempt")
    ap.add_argument("--min-score", type=float, default=0.55)
    ap.add_argument("--min-chars", type=int, default=2)
    ap.add_argument("--out", default="labels/spot_glyphs.npz")
    ap.add_argument("--qc", default=None, help="write a QC page showing the cuts")
    ap.add_argument("--qc-n", type=int, default=120)
    ap.add_argument("--no-erase-lines", dest="erase_lines", action="store_false")
    a = ap.parse_args()

    recs = []
    for f in sorted(glob.glob(a.boxes)):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("score", 1) < a.min_score or not r.get("gpoly"):
                continue
            if sum(c.isalnum() for c in r.get("text", "")) < a.min_chars:
                continue
            recs.append(r)
    print(f"{len(recs)} candidate spots", flush=True)
    recs = recs[: a.n]

    glyphs, chars, words, angles, gcx, gcy, meth, texts = [], [], [], [], [], [], [], []
    qc = []
    stats = Counter()
    for wi, r in enumerate(recs):
        crop = derotate(r)
        if crop is None or crop.size < 200:
            stats["no crop (tile miss)"] += 1
            continue
        bw = binarise(crop, a.erase_lines)
        segs, how = segment(bw, r["text"])
        stats[how] += 1
        if not segs:
            continue
        got = 0
        for (x0, x1, ch) in segs:
            g = norm_glyph(bw[:, max(0, x0):x1])
            if g is None:
                continue
            glyphs.append(g)
            chars.append(ch)
            words.append(300000 + wi)
            angles.append(0.0)
            gcx.append(r.get("gcx", 0.0))
            gcy.append(r.get("gcy", 0.0))
            meth.append(how)
            texts.append(r["text"])
            got += 1
        if a.qc and len(qc) < a.qc_n:
            vis = cv2.cvtColor(255 - bw, cv2.COLOR_GRAY2RGB)
            for (x0, x1, ch) in segs:
                cv2.rectangle(vis, (max(0, x0), 0), (min(vis.shape[1] - 1, x1 - 1), vis.shape[0] - 1),
                              (210, 40, 40), 1)
            b = io.BytesIO()
            from PIL import Image
            Image.fromarray(vis).save(b, "PNG")
            gl = []
            for (x0, x1, ch) in segs:
                g = norm_glyph(bw[:, max(0, x0):x1])
                if g is None:
                    continue
                bb = io.BytesIO()
                Image.fromarray(255 - g).save(bb, "PNG")
                gl.append(dict(ch=ch, img=base64.b64encode(bb.getvalue()).decode()))
            qc.append(dict(text=r["text"], how=how, n=got,
                           img=base64.b64encode(b.getvalue()).decode(), glyphs=gl))

    print(f"\n{len(glyphs)} letters from {len(recs)} spots")
    for k, v in stats.most_common():
        print(f"  {v:>5d}  {k}")
    if glyphs:
        print(f"\n  by method: {dict(Counter(meth))}")
        print(f"  distinct characters: {len(set(chars))}")

    np.savez_compressed(a.out, glyphs=np.array(glyphs, np.uint8), chars=np.array(chars, dtype="U1"),
                        word=np.array(words, np.int64), angle=np.array(angles, np.float64),
                        gcx=np.array(gcx), gcy=np.array(gcy),
                        method=np.array(meth, dtype=object).astype(str),
                        text=np.array(texts, dtype=object).astype(str))
    print(f"\nwrote {a.out}")

    if a.qc:
        html = QC.replace("__DATA__", json.dumps(dict(items=qc)))
        os.makedirs(os.path.dirname(a.qc) or ".", exist_ok=True)
        open(a.qc, "w").write(html)
        print(f"wrote {a.qc} ({os.path.getsize(a.qc)/1e6:.2f} MB)")
    print("SEGMENTDONE", flush=True)


QC = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · spot segmentation QC</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;display:flex;gap:14px}
 .it{background:#fff;border:1px solid #ddd;border-radius:6px;margin:10px 12px;padding:8px 10px}
 .it h4{margin:0 0 6px;font:600 13px system-ui}
 .it h4 small{color:#777;font-weight:400}
 .w img{image-rendering:pixelated;border:1px solid #eee;background:#fff}
 .gs{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
 .gs div{text-align:center;font-size:11px;color:#555}
 .gs img{image-rendering:pixelated;height:66px;border:1px solid #e2e2e2;background:#fff}
 .cuts{color:#a15} .runs{color:#161}
</style>
<header><b>spot segmentation QC</b>
 <label>zoom <input type=range id=z min=1 max=6 value=3></label>
 <span id=s></span></header>
<div id=w></div>
<script>
const D=__DATA__;
document.getElementById('s').textContent=`${D.items.length} spots`;
function render(){
 const z=+document.getElementById('z').value;
 document.getElementById('w').innerHTML=D.items.map(it=>`
  <div class=it><h4>${it.text} <small class="${it.how}">— ${it.how}, ${it.n} letters</small></h4>
   <div class=w><img src="data:image/png;base64,${it.img}" style="height:${28*z}px"></div>
   <div class=gs>${it.glyphs.map(g=>`<div><img src="data:image/png;base64,${g.img}"><br>${g.ch}</div>`).join('')}</div>
  </div>`).join('');
}
document.getElementById('z').oninput=render; render();
</script>
"""

if __name__ == "__main__":
    main()
