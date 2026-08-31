"""Bring the BIG admin-area labels into the alphabet, from Hi-SAM line detections.

MapReader does not spot them. Its word spotter is trained on ordinary map lettering, and the large
letter-spaced administrative names — county, borough, parish, hundred — either fall below its confidence or
come back as isolated letters. Those labels are precisely where the rare faces live: eleven of the fifteen
inventory faces have no map sample at all, and most of them are only ever set at admin-label size. So the
anchor set cannot be completed from MapReader boxes however long the loop is run.

Hi-SAM's LINE level supplies them. At word level a spaced label fragments into one tiny mask per letter (the
Leeds sheet's word-level AMG output had a median short side of 6px and only 41 detections above 18px, which
read as "this sheet has no large lettering" — an artefact of the level, not the sheet). The line level groups
the letters back into the label.

Two gates, and only two, because AMG carries no transcript: SIZE (the admin faces are large by definition) and
NON-COVERAGE by MapReader (anything MapReader already found is handled by the existing loop, and re-labelling
it here would double-count it in the anchor set). The numeral filter cannot apply — there is no text to test —
which is another reason to keep the size floor high, since contour numbers are small.

These are presented for DIRECT assignment, not for confirmation. The descriptor has anchors for four faces
only, so a proposal here would name one of the four for a spot whose whole purpose is to be a fifth: a
confident wrong answer, and worse than none. It is used instead to ORDER the page, so that like sits with like
and the eye can work down a run of one face rather than jumping between them.

    python bigfont_ui.py --amg /vast/.../amg_line_sheet_ENG_218_NW.jsonl \
                         --mr /vast/.../boxes_sheet_ENG_218_NW.jsonl --qc bigfont_qc.html
"""
import argparse, base64, glob, io, json, os, sys, time
from collections import Counter
import numpy as np
import cv2

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_font_testset_v2 import derotate
from propose_faces import load_backbone, descriptor, SIG_FACE


def shortside(poly):
    """Cap-height proxy: the short side of the minimum-area rectangle, which is what distinguishes an admin
    label from a street name regardless of how long the label is."""
    (_, _), (w, h), _ = cv2.minAreaRect(np.asarray(poly, np.float32))
    return min(w, h)


def bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


CELL = 512                    # px; MapReader boxes are far smaller, so one cell ring always suffices


def index_boxes(mr):
    """Grid index over MapReader boxes, keyed by global-pixel cell.

    At one sheet the linear scan was fine. At forty it is 337k boxes against every detection, which is a
    billion rectangle tests — so the boxes are bucketed and each detection consults only the cells it spans.
    """
    idx = {}
    for b in mr:
        for cx in range(int(b[0]) // CELL, int(b[2]) // CELL + 1):
            for cy in range(int(b[1]) // CELL, int(b[3]) // CELL + 1):
                idx.setdefault((cx, cy), []).append(b)
    return idx


def covered(poly, idx, thresh):
    """Fraction of the detection's bbox area overlapped by MapReader boxes.

    Deliberately bbox-on-bbox rather than polygon intersection: MapReader's boxes are rough, and the question
    is only "has the other spotter already got this label", which does not need a precise area.
    """
    x0, y0, x1, y1 = bbox(poly)
    area = max(1.0, (x1 - x0) * (y1 - y0))
    tot, seen = 0.0, set()
    for cx in range(int(x0) // CELL, int(x1) // CELL + 1):
        for cy in range(int(y0) // CELL, int(y1) // CELL + 1):
            for b in idx.get((cx, cy), ()):
                if id(b) in seen:            # a box spanning two cells must not be counted twice
                    continue
                seen.add(id(b))
                ix0, iy0 = max(x0, b[0]), max(y0, b[1])
                ix1, iy1 = min(x1, b[2]), min(y1, b[3])
                if ix1 > ix0 and iy1 > iy0:
                    tot += (ix1 - ix0) * (iy1 - iy0)
    return min(1.0, tot / area) >= thresh


def farthest_first(D, A, k):
    """Pick k candidates that between them cover the descriptor space, seeded by the existing anchors.

    A labelling budget spent proportionally is a budget spent almost entirely on the faces already known: the
    big-font candidates are dominated by letter-spaced road caps, of which the anchor set holds 260. What is
    scarce is everything else. So each pick is the candidate FURTHEST from everything covered so far — anchors
    included, which is what makes the first picks land on lettering unlike anything yet labelled.

    This selects what to ASK about, never what the answer is. Every pick is still assigned by eye.
    """
    if k >= len(D):
        return list(range(len(D)))
    cov = (D @ A.T).max(axis=1) if len(A) else np.full(len(D), -1.0)
    picked = []
    for _ in range(k):
        i = int(cov.argmin())
        picked.append(i)
        cov = np.maximum(cov, D @ D[i])
    return picked


def chain_order(D):
    """Greedy nearest-neighbour ordering, so visually similar crops are adjacent on the page.

    Not clustering — there is no k to choose and no cluster label to defend. It only asks the page to present
    its items in an order where consecutive rows tend to share a face, which turns fifteen-way dropdown
    hunting into a run of repeats.
    """
    n = len(D)
    if n < 3:
        return list(range(n))
    S = D @ D.T
    np.fill_diagonal(S, -2)
    order, seen = [0], {0}
    while len(order) < n:
        row = S[order[-1]].copy()
        row[list(seen)] = -2
        nxt = int(row.argmax())
        order.append(nxt)
        seen.add(nxt)
    return order


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--amg", required=True, help="glob of LINE-level AMG jsonl")
    ap.add_argument("--mr", default=None, help="glob of MapReader boxes jsonl (coverage gate)")
    ap.add_argument("--anchors", default="/vast/ishi/gb1900/edition/spot/anchor_desc_hisam.npz")
    ap.add_argument("--column", default="desc_mr")
    ap.add_argument("--min-cap", type=float, default=16.0,
                    help="px short side. Admin lettering is large; below this the page fills with the "
                         "ordinary small text MapReader already handles")
    ap.add_argument("--max-cap", type=float, default=400.0, help="above this it is a mask failure, not a label")
    ap.add_argument("--cover", type=float, default=0.5,
                    help="drop a detection whose bbox is at least this covered by MapReader boxes")
    ap.add_argument("--min-score", type=float, default=0.5)
    ap.add_argument("--n", type=int, default=4000, help="candidates to crop and describe")
    ap.add_argument("--qc-n", type=int, default=300,
                    help="how many reach the page. Forty sheets yield thousands, nearly all of them the same "
                         "road-caps the anchor set already holds in quantity")
    ap.add_argument("--inventory", default="labels/face_inventory.json")
    ap.add_argument("--out", default="/vast/ishi/gb1900/edition/spot/bigfont_candidates.jsonl")
    ap.add_argument("--workers", type=int, default=12, help="threads for the crop stage")
    ap.add_argument("--qc", default="bigfont_qc.html")
    a = ap.parse_args()

    inv = json.load(open(a.inventory))
    FACES = list(inv["faces"])

    mr = []
    if a.mr:
        for f in sorted(glob.glob(a.mr)):
            for line in open(f):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("gpoly"):
                    mr.append(bbox(r["gpoly"]))
    print(f"{len(mr)} MapReader boxes for the coverage gate", flush=True)
    midx = index_boxes(mr) if mr else {}

    recs, drop = [], Counter()
    for f in sorted(glob.glob(a.amg)):
        sheet = os.path.basename(f).replace("amg_line_", "").replace(".jsonl", "")
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get("gpoly"):
                drop["no polygon"] += 1
                continue
            if r.get("score", 1.0) < a.min_score:
                drop["low score"] += 1
                continue
            cap = shortside(r["gpoly"])
            if cap < a.min_cap:
                drop[f"short side < {a.min_cap:g}px"] += 1
                continue
            if cap > a.max_cap:
                drop["implausibly large"] += 1
                continue
            if midx and covered(r["gpoly"], midx, a.cover):
                drop["already found by MapReader"] += 1
                continue
            r["cap"] = round(float(cap), 1)
            r["sheet"] = sheet
            recs.append(r)
    recs.sort(key=lambda r: -r["cap"])
    n_gated = len(recs)
    recs = recs[: a.n]
    for k, v in drop.most_common():
        print(f"  dropped {v:>5d}  {k}")
    # Say what the cap discards. A truncated run that reports only its own size reads as full coverage.
    if n_gated > len(recs):
        print(f"  dropped {n_gated - len(recs):>5d}  beyond --n {a.n} (smallest of the {n_gated} gated; "
              f"cap-height floor became {recs[-1]['cap']:g}px)")
    print(f"{len(recs)} big-font candidates", flush=True)
    if not recs:
        print("BIGFONTDONE", flush=True)
        return

    torch, model, input_format, feat, dev = load_backbone()
    z = np.load(a.anchors, allow_pickle=True)
    A = z[a.column].astype(np.float32)
    A /= (np.linalg.norm(A, axis=1, keepdims=True) + 1e-9)
    ASIG = z["sigs"].astype(str)

    # Cropping is the bottleneck, not the descriptor: derotate assembles a tile mosaic and rotates the whole
    # canvas, which for a long diagonal label is millions of pixels. It is disk-read plus OpenCV, both of
    # which release the GIL, so it threads well — while the descriptor must stay serial on the one GPU.
    # derotate itself is untouched: its output is what the existing anchor descriptors were built from, and
    # changing it would silently make the two banks incomparable.
    import concurrent.futures as cf
    t_crop = time.time()
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        crops = list(ex.map(derotate, recs))
    print(f"cropped {sum(c is not None for c in crops)}/{len(recs)} in {time.time()-t_crop:.0f}s", flush=True)

    keep, desc, imgs = [], [], []
    miss = 0
    t0 = time.time()
    for n_done, (r, crop) in enumerate(zip(recs, crops), 1):
        if n_done % 250 == 0:
            rate = n_done / max(1e-9, time.time() - t0)
            print(f"  described {n_done}/{len(recs)} ({rate:.1f}/s, "
                  f"~{(len(recs)-n_done)/max(rate,1e-9)/60:.0f} min left)", flush=True)
        if crop is None or crop.size < 200:
            miss += 1
            continue
        d = descriptor(crop, torch, model, input_format, feat, dev)
        if d is None:
            miss += 1
            continue
        keep.append(r)
        desc.append(d / (np.linalg.norm(d) + 1e-9))
        b = io.BytesIO()
        from PIL import Image
        im = Image.fromarray(crop)
        if im.height > 120:                       # admin labels are long; cap the page weight, not the width
            im = im.resize((max(1, int(im.width * 120 / im.height)), 120), Image.LANCZOS)
        im.save(b, "PNG")
        imgs.append(base64.b64encode(b.getvalue()).decode())
    print(f"{len(keep)} cropped ({miss} with no usable crop)", flush=True)
    if not keep:
        print("BIGFONTDONE", flush=True)
        return

    D = np.array(desc, np.float32)
    if len(D) > a.qc_n:
        sel = sorted(farthest_first(D, A, a.qc_n))
        cov = (D @ A.T).max(axis=1)
        print(f"selected {len(sel)} of {len(D)} by farthest-first coverage "
              f"(novelty: selected median {1-np.median(cov[sel]):.3f} vs all {1-np.median(cov):.3f})",
              flush=True)
        keep = [keep[i] for i in sel]
        imgs = [imgs[i] for i in sel]
        D = D[sel]
    items = []
    for i in chain_order(D):
        r = keep[i]
        sim = A @ D[i]
        near = ASIG[int(sim.argmax())]
        hint = [near] if near in FACES else SIG_FACE.get(near, [])
        items.append(dict(gcx=r.get("gcx"), gcy=r.get("gcy"), lon=r.get("lon"), lat=r.get("lat"),
                          # the polygon travels with the decision: these spots exist in no boxes_*.jsonl, so
                          # the descriptor bank has no other way to re-crop them
                          gpoly=r.get("gpoly"), sheet=r.get("sheet"),
                          cap=r["cap"], score=r.get("score"), img=imgs[i],
                          # A HINT, not a proposal: the anchor set covers four faces, so this can only ever
                          # name one of those four however wrong that is for an admin label.
                          hint=hint[0] if hint else None, hint_sim=round(float(sim.max()), 3)))

    with open(a.out, "w") as fh:
        for it in items:
            fh.write(json.dumps({k: v for k, v in it.items() if k != "img"}, ensure_ascii=False) + "\n")
    print(f"wrote {a.out}")
    open(a.qc, "w").write(QC.replace("__DATA__", json.dumps(dict(items=items, faces=FACES))))
    print(f"wrote {a.qc} ({os.path.getsize(a.qc)/1e6:.2f} MB)")
    print(f"\ncap-height: median {np.median([i['cap'] for i in items]):.0f}px  "
          f"max {max(i['cap'] for i in items):.0f}px")
    print("BIGFONTDONE", flush=True)


QC = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · big-font labels</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;display:flex;gap:14px;
        align-items:center;flex-wrap:wrap;z-index:9}
 button{background:#2a7;color:#fff;border:0;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:13px}
 .it{background:#fff;border:1px solid #ddd;border-radius:6px;margin:8px 12px;padding:8px 10px;
     display:flex;gap:14px;align-items:center}
 .it.done{background:#f2fbf6;border-color:#8ccdae}
 .it.rej{background:#fbf2f2;border-color:#e0a0a0;opacity:.55}
 .it img{image-rendering:pixelated;max-width:70%;background:#fff;border:1px solid #eee}
 .m{font-size:11px;color:#666;margin-top:4px}
 .ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 select{font:13px system-ui;padding:3px;max-width:230px}
 .bd{border:1px solid #bbb;border-radius:12px;padding:3px 10px;cursor:pointer;font-size:12px;background:#f7f7f7}
 .bd:hover{filter:brightness(.95)}
 .bd.same{border-color:#468;background:#eef4fa}
 .bd.no{border-color:#c88;background:#fbf0f0}
 .bd.num{border-color:#a8a;background:#f6f0fa}
</style>
<header><b>big-font labels</b> <span class=m style=color:#ccc>Hi-SAM line detections MapReader missed —
 ordered so similar lettering sits together</span>
 <label><input type=checkbox id=hd onchange=render()> hide decided</label>
 <button onclick=exportJSON()>Export decisions</button>
 <span id=s></span></header>
<div id=w></div>
<script>
const D=__DATA__;
const dec={};
const key=i=>`${i.gcx},${i.gcy}`;
let last=null;
function set(i,face){ const k=key(i);
 /* Bench marks and spot heights arrive here unfiltered — AMG carries no transcript, so the numeral test that
    guards the MapReader loop has nothing to test. They are real labels in a face the inventory does not hold,
    so they get their own sink: calling them "not a label" would record something untrue, and leaving them to
    the dropdown would put numerals into a lettering face. */
 if(face==='__num'){dec[k]={reject:true,reason:'numeral'};}
 else if(face===null){dec[k]={reject:true,reason:'not-a-label'};}
 else if(!face){delete dec[k];}
 else if(dec[k]&&dec[k].face===face){delete dec[k];}
 else {dec[k]={face}; last=face;}
 render(); }
function render(){
 const hd=document.getElementById('hd').checked;
 const its=D.items.filter(i=>!(hd&&dec[key(i)]));
 document.getElementById('s').textContent=`${its.length} shown · ${Object.keys(dec).length} decided`;
 document.getElementById('w').innerHTML=its.map(i=>{
  const n=D.items.indexOf(i), d=dec[key(i)]||{};
  const opts=D.faces.map(f=>`<option${d.face==f?' selected':''}>${f}</option>`).join('');
  /* "same as last" is the whole ergonomic point of the similarity ordering: a run of one face is one click
     per row rather than a fifteen-item dropdown hunt per row. */
  const same=last?`<span class="bd same" onclick="set(D.items[${n}],'${last}')">↑ ${last}</span>`:'';
  return `<div class="it${d.face?' done':''}${d.reject?' rej':''}">
   <img src="data:image/png;base64,${i.img}">
   <div>
    <div class=ctl>
      <select onchange="set(D.items[${n}], this.value)">
        <option value="">— choose face —</option>${opts}</select>
      ${same}
      <span class="bd num" onclick="set(D.items[${n}],'__num')">numeral</span>
      <span class="bd no" onclick="set(D.items[${n}], null)">not a label</span>
    </div>
    <div class=m>${i.sheet} · cap ${i.cap}px · score ${i.score}${i.hint?' · nearest anchor '+i.hint+' ('+i.hint_sim+')':''}</div>
   </div></div>`;}).join('');
}
function exportJSON(){
 const out=[];
 D.items.forEach(i=>{ const d=dec[key(i)]; if(!d) return;
   out.push({gcx:i.gcx,gcy:i.gcy,lon:i.lon,lat:i.lat,gpoly:i.gpoly,sheet:i.sheet,cap:i.cap,text:"",
             face:d.face||null,reject:!!d.reject,reason:d.reason||null,source:"hisam-line"}); });
 if(!out.length){alert('Nothing decided yet.');return;}
 const blob=new Blob([JSON.stringify({decisions:out},null,1)],{type:'application/json'});
 const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
 a.download='bigfont_decisions.json'; a.click();
}
render();
</script>
"""

if __name__ == "__main__":
    main()
