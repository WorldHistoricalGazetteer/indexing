"""Primary pass: propose a face for every spot from the backbone descriptor, for a human to confirm.

The point is to populate faces that have no map lettering at all. Eleven of seventeen are seeded only from
engraved Characteristic-Sheet specimens, and CS templates match printed letters below chance, so those faces
need real samples and there is no automatic route to them. What the descriptor CAN do is narrow the search:
it reaches 0.739 maxsim-LOO on whole-word MapReader crops, so it can put a spot in front of a human already
labelled with its most likely face and a confidence, instead of asking them to find candidates by eye.

A proposal is not a label. Nothing enters the alphabet on a descriptor's say-so: if it did, the composed-word
overlay would later be scored against an alphabet built from the descriptor's own decisions, and agreement
between the two would mean nothing. The descriptor proposes, a human disposes.

The anchors carry the OLD six-signature labels, which do not map one-to-one onto the seventeen named faces.
Three map cleanly; `blackletter·solid·fancy` straddles PHS / N. Those are emitted as an explicit either-or rather than silently resolved — a two-way choice
is still most of the work done, and a guess would be worse than useless.

    python propose_faces.py --boxes .../boxes_sheet_ENG_218_NW.jsonl --n 800 --qc proposals_qc.html
"""
import argparse, base64, glob, io, json, os, sys
from collections import Counter, defaultdict
import numpy as np
import cv2

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_font_testset_v2 import derotate
from weak_sig import is_numeral

SAFE = 512
INST = "/vast/ishi/gb1900/probe/mapreader_text/install"

# Old anchor signature -> inventory face(s). An empty list means the signature has no face in the inventory.
SIG_FACE = {
    "upright·solid·plain": ["Upright-Solid-Plain"],
    "italic·solid·plain": ["Italic-Solid-Plain"],
    "upright·solid·serif": ["Upright-Solid-Serif"],
    "italic·solid·serif": ["Italic-Solid-Serif"],
    "blackletter·solid·fancy": ["Blackletter"],
    "numeral·solid·plain": [],
}


def load_backbone():
    import torch
    import pandas as pd
    from mapreader import MapTextRunner
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    runner = MapTextRunner(pd.DataFrame(),
                           cfg_file=f"{INST}/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml",
                           weights_file=f"{INST}/weights/rumsey-finetune.pth", device=dev)
    pred = runner.predictor
    model = pred.model
    model.eval()
    feat = {}
    tgt_name, tgt = max([(n, m) for n, m in model.named_modules() if n.endswith("backbone")],
                        key=lambda nm: nm[0].count("."))
    tgt.register_forward_hook(lambda m, i, o: feat.__setitem__("o", o))
    return torch, model, pred.input_format, feat, dev


def descriptor(crop, torch, model, input_format, feat, dev):
    m = max(crop.shape[:2])
    sq = np.full((m, m), 255, np.uint8)
    sq[:crop.shape[0], :crop.shape[1]] = crop
    g = cv2.resize(sq, (SAFE, SAFE), interpolation=cv2.INTER_AREA)
    im = np.repeat(g[:, :, None], 3, 2)
    arr = im[:, :, ::-1] if input_format == "BGR" else im
    t = torch.as_tensor(np.ascontiguousarray(arr.transpose(2, 0, 1)).astype("float32")).to(dev)
    feat.clear()
    try:
        with torch.no_grad():
            model([{"image": t, "height": SAFE, "width": SAFE}])
    except Exception:
        pass
    o = feat.get("o", {})
    per = []
    for k in sorted(o) if isinstance(o, dict) else []:
        v = o[k]
        if hasattr(v, "tensors"):
            v = v.tensors
        if torch.is_tensor(v) and v.dim() == 4:
            per.append(v[0].mean(dim=(1, 2)).float().cpu().numpy())
    return np.concatenate(per) if per else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boxes", required=True)
    ap.add_argument("--anchors", default="/vast/ishi/gb1900/edition/spot/anchor_desc_hisam.npz")
    ap.add_argument("--column", default="desc_mr",
                    help="anchor crop convention to match against; MapReader boxes scored best (0.739)")
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--min-score", type=float, default=0.55)
    ap.add_argument("--min-chars", type=int, default=3)
    ap.add_argument("--out", default="/vast/ishi/gb1900/edition/spot/face_proposals.jsonl")
    ap.add_argument("--qc", default=None)
    ap.add_argument("--qc-n", type=int, default=200)
    ap.add_argument("--inventory", default="labels/face_inventory.json")
    ap.add_argument("--keep-numerals", dest="skip_numerals", action="store_false",
                    help="propose faces for numeric transcripts too (they have no face in the inventory)")
    ap.add_argument("--drop-sigs", nargs="*", default=["numeral·solid·plain"],
                    help="signatures removed from the anchor set: they cannot then be proposed at all, "
                         "which is the honest treatment of a class that has no face in the inventory")
    a = ap.parse_args()

    z = np.load(a.anchors, allow_pickle=True)
    A = z[a.column].astype(np.float32)
    A /= (np.linalg.norm(A, axis=1, keepdims=True) + 1e-9)
    ASIG = z["sigs"].astype(str)
    if a.drop_sigs:
        keep = ~np.isin(ASIG, a.drop_sigs)
        print(f"dropping {int((~keep).sum())} anchors of {a.drop_sigs}", flush=True)
        A, ASIG = A[keep], ASIG[keep]
    sigs = sorted(set(ASIG))
    cols = {s: np.where(ASIG == s)[0] for s in sigs}
    print(f"{len(A)} anchors over {len(sigs)} signatures ({a.column})", flush=True)
    FACES = list(json.load(open(a.inventory))["faces"]) if os.path.exists(a.inventory) else []

    def class_sims(v):
        sim = A @ v
        return {s: float(sim[cols[s]].max()) for s in sigs}

    def confidence(per):
        """Margin standardised by the spread of the class similarities.

        The raw gap between best and second is ~0.01 on cosine similarities that all sit near 0.9, so it
        cannot separate a confident call from a coin toss. Dividing by the spread of the class scores asks the
        question that matters — is the winner clear RELATIVE to how tightly the classes are packed for this
        particular spot — and gives a number that is comparable between spots.
        """
        vals = np.array(sorted(per.values(), reverse=True))
        sd = float(vals.std())
        return float((vals[0] - vals[1]) / sd) if sd > 1e-9 and len(vals) > 1 else 0.0

    # Calibrate on the anchors themselves, leave-one-out: the same confidence measure applied to data whose
    # true face is known tells the reviewer which proposals can be taken on trust and which need an eye.
    print("\n  calibration (anchors, leave-one-out):", flush=True)
    cal = []
    for i in range(len(A)):
        sim = A @ A[i]
        sim[i] = -2
        per = {s: float(sim[cols[s]].max()) for s in sigs}
        pred = max(per, key=per.get)
        cal.append((confidence(per), pred == ASIG[i]))
    cal.sort(reverse=True)
    conf_arr = np.array([c for c, _ in cal])
    for lo in (0.0, 0.5, 1.0, 1.5, 2.0):
        sel = [ok for c, ok in cal if c >= lo]
        if sel:
            print(f"    confidence >= {lo:.1f}: {len(sel):>4d} anchors, accuracy {np.mean(sel):.3f}", flush=True)

    recs, n_num = [], 0
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
            if a.skip_numerals and is_numeral(r.get("text", "")):
                n_num += 1
                continue
            recs.append(r)
    recs = recs[: a.n]
    print(f"{len(recs)} spots to propose for ({n_num} numeral transcripts skipped)", flush=True)

    torch, model, input_format, feat, dev = load_backbone()
    out, qc = [], []
    miss = 0
    for r in recs:
        crop = derotate(r)
        if crop is None or crop.size < 200:
            miss += 1
            continue
        d = descriptor(crop, torch, model, input_format, feat, dev)
        if d is None:
            miss += 1
            continue
        d = d / (np.linalg.norm(d) + 1e-9)
        per = class_sims(d)
        order = sorted(per, key=per.get, reverse=True)
        top, second = order[0], order[1] if len(order) > 1 else None
        margin = confidence(per)
        # Anchors may be labelled in either vocabulary. Once they carry inventory faces there is nothing to
        # map, and looking them up in the legacy table returns nothing — which reported every proposal as
        # out of inventory. Prefer the label as-is when it already names a face.
        faces = [top] if top in FACES else SIG_FACE.get(top, [])
        rec = dict(text=r["text"], gcx=r.get("gcx"), gcy=r.get("gcy"),
                   lon=r.get("lon"), lat=r.get("lat"), gpoly=r.get("gpoly"),
                   sig=top, sim=round(per[top], 4), margin=round(margin, 3),
                   sims={k: round(v, 4) for k, v in per.items()},
                   faces=faces, ambiguous=len(faces) > 1, in_scope=bool(faces),
                   runner_up=second)
        out.append(rec)
        if a.qc and len(qc) < a.qc_n:
            b = io.BytesIO()
            from PIL import Image
            Image.fromarray(crop).save(b, "PNG")
            ru_faces = ([second] if second in FACES else SIG_FACE.get(second, [])) if second else []
            qc.append(dict(rec, img=base64.b64encode(b.getvalue()).decode(), gpoly=None,
                           runner_faces=ru_faces))

    with open(a.out, "w") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n{len(out)} proposals ({miss} spots with no usable crop)")
    c = Counter(r["sig"] for r in out)
    for s, n in c.most_common():
        # Same resolution the proposals use — reading SIG_FACE directly reported every face-labelled anchor
        # as out of inventory, which was alarming and wrong.
        f = [s] if s in FACES else SIG_FACE.get(s, [])
        tag = " / ".join(f) if f else "NOT IN INVENTORY"
        print(f"  {s:26s} {n:>4d}  -> {tag}")
    amb = sum(r["ambiguous"] for r in out)
    oos = sum(not r["in_scope"] for r in out)
    print(f"\n  unambiguous: {len(out)-amb-oos}   two-way: {amb}   out of scope: {oos}")
    m = np.array([r["margin"] for r in out])
    print(f"  confidence: median {np.median(m):.2f}  p25 {np.percentile(m,25):.2f}  p75 {np.percentile(m,75):.2f}")
    for q in (0.5, 1.0, 1.5, 2.0):
        print(f"    confidence >= {q:.1f}: {int((m>=q).sum())} ({(m>=q).mean():.0%})")
    print(f"\nwrote {a.out}")

    if a.qc:
        qc.sort(key=lambda r: -r["margin"])
        open(a.qc, "w").write(QC.replace("__DATA__", json.dumps(dict(items=qc, faces=FACES))))
        print(f"wrote {a.qc} ({os.path.getsize(a.qc)/1e6:.2f} MB)")
    print("PROPOSEDONE", flush=True)


QC = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · face proposals</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;display:flex;gap:14px;
        align-items:center;flex-wrap:wrap;z-index:9}
 button{background:#2a7;color:#fff;border:0;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:13px}
 button.alt{background:#555}
 .it{background:#fff;border:1px solid #ddd;border-radius:6px;margin:8px 12px;padding:8px 10px;
     display:flex;gap:14px;align-items:center}
 .it.done{background:#f2fbf6;border-color:#8ccdae}
 .it.rej{background:#fbf2f2;border-color:#e0a0a0;opacity:.6}
 .it img{image-rendering:pixelated;max-height:70px;background:#fff;border:1px solid #eee}
 .m{font-size:11px;color:#666}
 /* Badges ARE the control: the winner and the runner-up are the two answers that matter, so accepting
    either is one click. An ambiguous signature contributes one badge PER face, so the two-way split is
    resolved by the same click rather than needing a second decision. */
 .badges{display:flex;gap:6px;flex-wrap:wrap;margin:3px 0}
 .bd{border:1px solid #bbb;border-radius:12px;padding:2px 10px;cursor:pointer;font-size:12px;background:#f7f7f7}
 .bd.win{border-color:#2a7;background:#eafaf1;font-weight:600}
 .bd.run{border-color:#d80;background:#fff6e8}
 .bd.sel{background:#2a7;color:#fff;border-color:#2a7}
 .bd:hover{filter:brightness(.96)}
 select{font:12px system-ui;padding:2px}
 .bar{height:6px;background:#eee;border-radius:3px;width:120px;overflow:hidden}
 .bar i{display:block;height:6px;background:#2a7}
</style>
<header><b>face proposals</b>
 <label>min confidence <input type=range id=mm min=0 max=30 value=0> <span id=mv>0.0</span></label>
 <label><input type=checkbox id=ha onchange=render()> hide two-way</label>
 <label><input type=checkbox id=hd onchange=render()> hide decided</label>
 <button onclick=exportJSON()>Export decisions</button>
 <span id=s></span></header>
<div id=w></div>
<script>
const D=__DATA__;
const dec={};      // key -> {face} or {reject:true}
const key=i=>`${i.gcx},${i.gcy}`;
function set(i,face){ const k=key(i); if(face===null){dec[k]={reject:true};} else if(dec[k]&&dec[k].face===face){delete dec[k];} else {dec[k]={face};} render(); }
function render(){
 const mm=(+document.getElementById('mm').value)/10, ha=document.getElementById('ha').checked,
       hd=document.getElementById('hd').checked;
 document.getElementById('mv').textContent=mm.toFixed(1);
 const its=D.items.filter(i=>i.margin>=mm && !(ha&&i.ambiguous) && !(hd&&dec[key(i)]));
 const n=Object.keys(dec).length;
 document.getElementById('s').textContent=`${its.length} shown · ${n} decided`;
 document.getElementById('w').innerHTML=its.map(i=>{
  const d=dec[key(i)]||{};
  const win=(i.faces||[]).map(f=>`<span class="bd win${d.face==f?' sel':''}" onclick="set(D.items[${D.items.indexOf(i)}],'${f}')">${f}</span>`).join('');
  const run=(i.runner_faces||[]).filter(f=>!(i.faces||[]).includes(f))
      .map(f=>`<span class="bd run${d.face==f?' sel':''}" onclick="set(D.items[${D.items.indexOf(i)}],'${f}')">${f}</span>`).join('');
  const opts=D.faces.map(f=>`<option${d.face==f?' selected':''}>${f}</option>`).join('');
  return `<div class="it${d.face?' done':''}${d.reject?' rej':''}">
   <img src="data:image/png;base64,${i.img}">
   <div>
    <div class=badges>${win}${run}
      <select onchange="set(D.items[${D.items.indexOf(i)}], this.value)">
        <option value="">— other —</option>${opts}</select>
      <span class=bd onclick="set(D.items[${D.items.indexOf(i)}], null)">reject</span>
    </div>
    <div class=m>${i.text} · conf ${i.margin} · winner ${i.sig}${i.runner_up?' · runner-up '+i.runner_up:''}</div>
    <div class=bar><i style="width:${Math.min(100,i.margin*40)}%"></i></div>
   </div></div>`;}).join('');
}
function exportJSON(){
 const out=[];
 D.items.forEach(i=>{ const d=dec[key(i)]; if(!d) return;
   out.push({gcx:i.gcx,gcy:i.gcy,lon:i.lon,lat:i.lat,text:i.text,
             proposed:i.sig,confidence:i.margin,
             face:d.face||null,reject:!!d.reject}); });
 if(!out.length){alert('Nothing decided yet — click a badge.');return;}
 const blob=new Blob([JSON.stringify({decisions:out},null,1)],{type:'application/json'});
 const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
 a.download='face_decisions.json'; a.click();
}
document.getElementById('mm').oninput=render; render();
</script>
"""

if __name__ == "__main__":
    main()
