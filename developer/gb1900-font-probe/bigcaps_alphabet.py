"""Cluster extracted BIGCAPS letters by overlay similarity into a per-(face, letter) alphabet.

The premise is that an engraved sheet is not handwriting: the same letter in the same face is struck from
the same punch every time, so two instances should overlay almost exactly. If that holds, clusters of
overlaid glyphs ARE the (face, letter) cells of the alphabet, and a human names each cluster once instead
of labelling every glyph. If it does not hold — if clusters mix letters, or one letter scatters across many
clusters — that is the finding, and it is visible in the QC page rather than buried in an accuracy number.

Distance is 1 - IoU of the two binary rasters, maximised over a small shift, so a pixel of centring error
is not read as a difference in letterform. Linkage is AVERAGE, not single: single linkage would chain
through the near-misses that inevitably exist between similar capitals (C/G, O/Q, E/F) and collapse the
alphabet into one blob.

    python bigcaps_alphabet.py --glyphs bigcaps_glyphs_*.npz --max-dist 0.28
"""
import argparse, base64, glob, io, json, os
import numpy as np
import cv2
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import squareform


def load(patterns):
    G, grp, tag, meta = [], [], [], []
    off = 0
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            z = np.load(path, allow_pickle=True)
            g = z["glyphs"]
            G.append(g)
            grp.append(z["group"].astype(np.int64) + off)
            tag.append(z["tag"])
            meta.append(z["xywh"])
            off += int(z["group"].max()) + 1 if len(z["group"]) else 0
            print(f"  {path}: {len(g)} glyphs")
    if not G:
        raise SystemExit("no glyphs found")
    return (np.concatenate(G), np.concatenate(grp), np.concatenate(tag), np.concatenate(meta))


def iou_matrix(G, shifts=2, step=1):
    """Pairwise IoU, maximised over a shift of the second glyph.

    Done as matrix products rather than per-pair loops: the intersection of two binary rasters is their dot
    product, so one matmul gives every pair at once for a given shift.
    """
    n, H, W = G.shape
    A = G.reshape(n, -1).astype(np.float32)
    area = A.sum(1)
    best = np.zeros((n, n), np.float32)
    offs = [(dy, dx) for dy in range(-shifts, shifts + 1, step)
            for dx in range(-shifts, shifts + 1, step)]
    for dy, dx in offs:
        B = np.roll(np.roll(G, dy, axis=1), dx, axis=2).reshape(n, -1).astype(np.float32)
        inter = A @ B.T
        union = area[:, None] + area[None, :] - inter
        np.maximum(best, inter / np.maximum(union, 1.0), out=best)
    best = np.maximum(best, best.T)          # a shift that helps one direction is symmetric in effect
    np.fill_diagonal(best, 1.0)
    return best


def png(mask):
    return base64.b64encode(cv2.imencode(".png", (255 - mask.astype(np.uint8) * 255))[1]).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glyphs", nargs="+", default=["bigcaps_glyphs_*.npz"])
    ap.add_argument("--max-dist", type=float, default=0.28,
                    help="average-linkage cut; 1-IoU, so 0.28 means members overlay to ~72%")
    ap.add_argument("--shift", type=int, default=2, help="px of centring slack allowed in the overlay")
    ap.add_argument("--min-size", type=int, default=2, help="clusters smaller than this are 'singletons'")
    ap.add_argument("--out", default="bigcaps_alphabet.json")
    ap.add_argument("--qc", default="bigcaps_alphabet_qc.html")
    ap.add_argument("--qc-max", type=int, default=60, help="glyphs shown per cluster")
    a = ap.parse_args()

    G, grp, tag, xywh = load(a.glyphs)
    print(f"{len(G)} glyphs, {len(set(grp.tolist()))} groups, canvas {G.shape[1]}x{G.shape[2]}")

    S = iou_matrix(G, shifts=a.shift)
    D = np.clip(1.0 - S, 0, None)
    np.fill_diagonal(D, 0.0)
    Z = linkage(squareform(D, checks=False), method="average")
    lab = fcluster(Z, t=a.max_dist, criterion="distance")
    print(f"cut at {a.max_dist}: {lab.max()} clusters")

    order = [(c, np.where(lab == c)[0]) for c in range(1, lab.max() + 1)]
    order.sort(key=lambda t: -len(t[1]))
    kept = [(c, ix) for c, ix in order if len(ix) >= a.min_size]
    single = sum(len(ix) for c, ix in order if len(ix) < a.min_size)
    print(f"  {len(kept)} clusters with >={a.min_size} members, "
          f"covering {sum(len(ix) for _, ix in kept)} glyphs; {single} left as singletons")
    sizes = np.array([len(ix) for _, ix in kept])
    if len(sizes):
        print(f"  cluster size: median {np.median(sizes):.0f}  max {sizes.max()}")
        # A cluster is only useful as an alphabet cell if its members really do overlay. The mean
        # within-cluster IoU says how tight it is; a loose cluster is a warning, not a result.
        tight = [float(S[np.ix_(ix, ix)][np.triu_indices(len(ix), 1)].mean()) for _, ix in kept]
        print(f"  within-cluster overlay: median {np.median(tight):.3f}  "
              f"worst {np.min(tight):.3f}  ({(np.array(tight) >= 0.8).mean()*100:.0f}% at or above 0.80)")

    items = []
    for c, ix in kept:
        sub = S[np.ix_(ix, ix)]
        medoid = ix[int(np.argmax(sub.sum(1)))]
        show = ix[np.argsort(-S[medoid, ix])][: a.qc_max]
        items.append(dict(cid=int(c), n=int(len(ix)),
                          tight=round(float(sub[np.triu_indices(len(ix), 1)].mean()), 3)
                          if len(ix) > 1 else 1.0,
                          groups=int(len(set(grp[ix].tolist()))),
                          # Which labels this cluster's letters came from. Letters set in the same label
                          # are necessarily in the same face, so this is the evidence that turns a set of
                          # letterform clusters into faces — without anyone having to judge a face by eye.
                          labels=sorted(set(int(v) for v in grp[ix])),
                          mean=png(G[ix].mean(0) > 0.5),
                          glyphs=[png(G[i]) for i in show]))
    json.dump(dict(max_dist=a.max_dist,
                   clusters=[{k: v for k, v in it.items() if k not in ("mean", "glyphs")}
                             for it in items]), open(a.out, "w"), indent=1)
    print(f"wrote {a.out}")
    open(a.qc, "w").write(QC.replace("__DATA__", json.dumps(dict(items=items))))
    print(f"wrote {a.qc} ({os.path.getsize(a.qc)/1e6:.2f} MB)")
    print("ALPHADONE", flush=True)


QC = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · alphabet clusters</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;z-index:9;
        display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 .sp{flex:1} header input{width:60px}
 .cl{background:#fff;border:1px solid #ddd;border-radius:6px;margin:8px 12px;padding:8px 10px;
     display:flex;gap:12px;align-items:flex-start}
 .cl.done{border-color:#3a7d44;box-shadow:inset 4px 0 #3a7d44}
 .cl.no{opacity:.4}
 .mean{flex:0 0 auto;text-align:center}
 .mean img{height:64px;image-rendering:pixelated;border:1px solid #ccc;background:#fff}
 .ltr{font:20px system-ui;width:2.4em;text-align:center;margin-top:4px;padding:2px}
 .body{flex:1}
 .gs{display:flex;gap:4px;flex-wrap:wrap}
 .gs img{height:44px;image-rendering:pixelated;border:1px solid #eee;background:#fff}
 .m{font-size:11px;color:#666;margin-top:5px}
 .m button{font:11px system-ui;padding:1px 8px;cursor:pointer;margin-left:8px}
</style>
<header><b>alphabet clusters</b> — the left image is the cluster's overlay; if it is a crisp letter the
 cluster is one letterform, if it is a smear it is a mixture.
 <span>hide overlay &lt;<input type=number id=mint value=0 step=0.05></span>
 <span class=sp></span><span id=st></span><button onclick=dl()>download</button></header>
<div id=w></div>
<script>
const D=__DATA__, K='bigcaps_alphabet';
let V=JSON.parse(localStorage.getItem(K)||'{}');
function draw(){
 const mt=+document.getElementById('mint').value||0;
 document.getElementById('w').innerHTML=D.items.filter(i=>i.tight>=mt).map(i=>{
  const v=V[i.cid]||{};
  return `<div class="cl ${v.ltr?'done':''} ${v.mixed?'no':''}" data-c="${i.cid}">
   <div class=mean><img src="data:image/png;base64,${i.mean}"><br>
    <input class=ltr maxlength=2 value="${v.ltr||''}" placeholder="?"></div>
   <div class=body><div class=gs>${i.glyphs.map(g=>
     `<img src="data:image/png;base64,${g}">`).join('')}</div>
    <div class=m>cluster ${i.cid} · ${i.n} glyphs from ${i.groups} label(s)
     · overlay ${i.tight}<button data-a=mix>mixture, not one letter</button></div></div></div>`;
  }).join('');
 stat();
}
function stat(){
 document.getElementById('st').textContent=
  Object.values(V).filter(v=>v.ltr).length+' named, '
  +Object.values(V).filter(v=>v.mixed).length+' marked mixed';
}
const save=()=>localStorage.setItem(K,JSON.stringify(V));
document.getElementById('w').addEventListener('input',e=>{
 if(!e.target.classList.contains('ltr'))return;
 const c=e.target.closest('.cl').dataset.c;
 V[c]=V[c]||{}; V[c].ltr=e.target.value.toUpperCase();
 e.target.closest('.cl').classList.toggle('done',!!V[c].ltr);
 save(); stat();
});
document.getElementById('w').addEventListener('click',e=>{
 if(e.target.dataset.a!=='mix')return;
 const el=e.target.closest('.cl'), c=el.dataset.c;
 V[c]=V[c]||{}; V[c].mixed=!V[c].mixed; el.classList.toggle('no',V[c].mixed);
 save(); stat();
});
document.getElementById('mint').onchange=draw;
function dl(){const b=new Blob([JSON.stringify(V,null,1)],{type:'application/json'}),
 a=document.createElement('a'); a.href=URL.createObjectURL(b);
 a.download='bigcaps_alphabet_names.json'; a.click();}
draw();
</script>
"""

if __name__ == "__main__":
    main()
