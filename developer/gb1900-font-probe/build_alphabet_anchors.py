"""Build a per-face alphabet from the VERIFIED anchors, by aggregation rather than by trusting any one cut.

The composed-word overlay needs a template per (face, character), and the library it would otherwise use is
not fit for the job: Italic-Solid-Serif — one half of the very discrimination the overlay is meant to settle —
holds 33 engraved Characteristic-Sheet letters and not one map letter, and CS templates matched printed map
lettering at 0.176, below chance. Engraved specimens cannot type printed text. The alphabet has to come from
the map.

Segmentation into letters was judged poor when its output was used to classify each letter individually, and
that judgement stands. It is not the same task as this one. Here every cut is one vote among many toward a
median template, so a bad cut shifts a pixel or two rather than producing a wrong answer, and the error
averages out instead of propagating. Two things make that safe:

* `runs` segmentation is preferred over `cuts`. A `runs` split means the letters were already separated by
  their own ink gaps — nothing was inferred. `cuts` means a dynamic-programming guess at where one letter ends,
  and those are recorded separately so a template built mostly from guesses can be identified and distrusted.
* Templates are the MEDIAN over instances, not the mean, so a handful of botched cuts cannot drag the shape.

Faces come from the human-verified anchor set, so a glyph's face is never in doubt — only its boundaries are.

    python build_alphabet_anchors.py --qc alphabet_anchors_qc.html
"""
import argparse, base64, glob, io, json, os, sys
from collections import Counter, defaultdict
import numpy as np
import cv2

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_font_testset_v2 import derotate
from segment_spots import binarise, segment, norm_glyph

SPOT = "/vast/ishi/gb1900/edition/spot"


def key(gx, gy):
    return (round(float(gx), 1), round(float(gy), 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="labels/pool_labels_faced.json")
    ap.add_argument("--boxes", default=f"{SPOT}/boxes_*.jsonl")
    ap.add_argument("--min-inst", type=int, default=3,
                    help="instances a (face, char) needs before it becomes a template. A single instance is "
                         "one segmentation's opinion, which is exactly what this method declines to trust")
    ap.add_argument("--runs-only", action="store_true",
                    help="use only ink-gap-separated letters; the strictest alphabet, and much thinner")
    ap.add_argument("--out", default="labels/alphabet_anchors.npz")
    ap.add_argument("--qc", default="alphabet_anchors_qc.html")
    a = ap.parse_args()

    lab = [l for l in json.load(open(a.labels)) if l.get("face") and l.get("text")]
    want = {key(l["gcx"], l["gcy"]): l for l in lab}
    print(f"{len(lab)} verified anchors carrying a transcript", flush=True)

    boxes = {}
    for f in sorted(glob.glob(a.boxes)):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = key(r["gcx"], r["gcy"])
            if k in want and k not in boxes:
                boxes[k] = r
        if len(boxes) >= len(want):
            break
    print(f"{len(boxes)} matched to MapReader boxes", flush=True)

    glyphs, faces, chars, methods, wordids, insts = [], [], [], [], [], []
    stats = Counter()
    for wid, (k, r) in enumerate(boxes.items()):
        l = want[k]
        crop = derotate(r)
        if crop is None or crop.size < 200:
            stats["no crop"] += 1
            continue
        bw = binarise(crop)
        segs, method = segment(bw, l["text"])
        if not segs:
            stats[f"segment failed: {method}"] += 1
            continue
        if a.runs_only and "runs" not in method:
            stats["not runs-separated"] += 1
            continue
        stats[f"ok ({method})"] += 1
        for x0, x1, ch in segs:
            g = norm_glyph(bw[:, max(0, x0):x1])
            if g is None:
                continue
            glyphs.append(g)
            faces.append(l["face"])
            chars.append(ch)
            methods.append(method)
            wordids.append(wid)
    print(f"\n{len(glyphs)} glyph instances from {len(boxes)} words")
    for kk, v in stats.most_common():
        print(f"  {v:>5d}  {kk}")

    G = np.array(glyphs, np.uint8)
    F = np.array(faces, dtype=object).astype(str)
    C = np.array(chars, dtype=object).astype(str)
    M = np.array(methods, dtype=object).astype(str)
    W = np.array(wordids, np.int64)

    # Templates: median over instances of the same (face, char). The word id travels with every instance so a
    # leave-one-word-out evaluation can rebuild the template with the test word's own glyphs removed —
    # without that, a word would be matched against a template it helped create.
    tf, tc, tmpl, tn, trun = [], [], [], [], []
    for f in sorted(set(F)):
        for c in sorted(set(C[F == f])):
            m = (F == f) & (C == c)
            if m.sum() < a.min_inst:
                continue
            tf.append(f)
            tc.append(c)
            tmpl.append(np.median(G[m], axis=0).astype(np.uint8))
            tn.append(int(m.sum()))
            trun.append(float(np.mean(["runs" in x for x in M[m]])))

    np.savez_compressed(a.out, glyphs=G, faces=F.astype(object), chars=C.astype(object),
                        methods=M.astype(object), word=W,
                        tmpl=np.array(tmpl, np.uint8), tmpl_face=np.array(tf, dtype=object),
                        tmpl_char=np.array(tc, dtype=object), tmpl_n=np.array(tn),
                        tmpl_runs=np.array(trun))
    print(f"\n{len(tmpl)} templates (>= {a.min_inst} instances) -> {a.out}\n")
    print(f"  {'face':26s} {'letters':>7} {'instances':>10}  {'median n':>8}  {'runs share':>10}")
    for f in sorted(set(tf)):
        i = [j for j, x in enumerate(tf) if x == f]
        print(f"  {f:26s} {len(i):>7} {int(sum(tn[j] for j in i)):>10}  "
              f"{int(np.median([tn[j] for j in i])):>8}  {np.mean([trun[j] for j in i]):>10.2f}")
        print(f"      {''.join(sorted(tc[j] for j in i))}")

    if a.qc:
        items = []
        for f in sorted(set(tf)):
            row = []
            for j in sorted([j for j, x in enumerate(tf) if x == f], key=lambda j: tc[j]):
                b = io.BytesIO()
                from PIL import Image
                Image.fromarray(255 - tmpl[j]).save(b, "PNG")
                row.append(dict(ch=tc[j], n=tn[j], runs=round(trun[j], 2),
                                img=base64.b64encode(b.getvalue()).decode()))
            items.append(dict(face=f, glyphs=row))
        open(a.qc, "w").write(QC.replace("__DATA__", json.dumps(dict(faces=items))))
        print(f"\nwrote {a.qc} ({os.path.getsize(a.qc)/1e6:.2f} MB)")
    print("ALPHABETDONE", flush=True)


QC = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · anchor alphabets</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;z-index:9}
 .face{background:#fff;border:1px solid #ddd;border-radius:6px;margin:10px 12px;padding:10px 12px}
 .face h3{margin:0 0 8px;font-size:14px}
 .row{display:flex;flex-wrap:wrap;gap:8px}
 .g{text-align:center;font-size:10px;color:#666}
 .g img{display:block;width:44px;height:54px;image-rendering:pixelated;border:1px solid #eee;background:#fff}
 .g.thin img{border-color:#e0a0a0}
 .g.guess img{border-color:#d8a}
</style>
<header><b>anchor alphabets</b> — median template per (face, letter).
 <span style=color:#f8b>pink border</span> = mostly DP-guessed cuts;
 <span style=color:#f88>red border</span> = fewer than 6 instances</header>
<div id=w></div>
<script>
const D=__DATA__;
document.getElementById('w').innerHTML=D.faces.map(f=>`
 <div class=face><h3>${f.face} <span style="font-weight:400;color:#888">— ${f.glyphs.length} letters</span></h3>
 <div class=row>${f.glyphs.map(g=>`
   <div class="g${g.n<6?' thin':''}${g.runs<0.5?' guess':''}">
     <img src="data:image/png;base64,${g.img}"><div>${g.ch} · ${g.n}</div></div>`).join('')}
 </div></div>`).join('');
</script>
"""

if __name__ == "__main__":
    main()
