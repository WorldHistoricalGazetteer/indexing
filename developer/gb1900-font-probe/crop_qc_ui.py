"""Render the anchor crop columns to a page, so a crop bug can never again hide behind a plausible number.

The whole Phase-B comparison rests on three crops of the same anchor differing only in which polygon defined
them. That is exactly the kind of thing that looks fine in a table while being wrong in the image — as it was:
every crop was a map region, and the number still came out near its expected value.

    python crop_qc_ui.py --npz anchor_crops_hisam.npz --out anchor_crops_qc.html
"""
import argparse, base64, io, json, os, sys
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weak_sig import is_numeral


def b64(a, maxh=90):
    im = Image.fromarray(np.asarray(a, np.uint8)).convert("L")
    if im.height > maxh:
        im = im.resize((max(1, int(im.width * maxh / im.height)), maxh), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="/vast/ishi/gb1900/edition/spot/anchor_crops_hisam.npz")
    ap.add_argument("--out", default="/vast/ishi/gb1900/edition/clean/anchor_crops_qc.html")
    ap.add_argument("--n", type=int, default=80)
    ap.add_argument("--inventory", default="labels/face_inventory.json")
    ap.add_argument("--keep-numerals", dest="skip_numerals", action="store_false")
    a = ap.parse_args()
    d = np.load(a.npz, allow_pickle=True)
    sigs = d["sigs"].astype(str)
    texts = d["texts"].astype(str) if "texts" in d.files else np.array([""] * len(sigs))
    gcx = d["gcx"] if "gcx" in d.files else np.zeros(len(sigs))
    gcy = d["gcy"] if "gcy" in d.files else np.zeros(len(sigs))
    items = []
    skipped_num = 0
    for i in range(min(a.n, len(sigs))):
        # Numeral anchors are out of inventory scope, so reviewing their crops decides nothing.
        if a.skip_numerals and (is_numeral(texts[i]) or sigs[i].startswith("numeral")):
            skipped_num += 1
            continue
        items.append(dict(i=int(i), sig=sigs[i], text=texts[i],
                          gcx=float(gcx[i]), gcy=float(gcy[i]),
                          mr=b64(d["mr"][i]), word=b64(d["word"][i]), line=b64(d["line"][i])))
    # Offer the INVENTORY faces, not the legacy signatures the anchors were labelled with. The anchors
    # predate this inventory: their scheme had no way to say prehistoric/Saxon versus Norman, so both sit
    # under blackletter-solid-fancy and the descriptor cannot separate what its training data never
    # distinguished. Re-labelling anchors here is what migrates the classifier to the new label space.
    faces = list(json.load(open(a.inventory))["faces"]) if os.path.exists(a.inventory) else []
    # An ACCEPT has to resolve to an inventory face to be worth anything, so the legacy-signature mapping
    # travels with the page. Where a legacy signature covers two faces (blackletter) accept cannot resolve it,
    # and the page says so rather than silently picking one.
    from propose_faces import SIG_FACE
    legacy = sorted(set(sigs.tolist()))
    html = HTML.replace("__DATA__", json.dumps(dict(items=items, n=len(sigs),
                                                    sigs=faces or legacy, legacy=legacy,
                                                    sigface={k: v for k, v in SIG_FACE.items()})))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "w").write(html)
    print(f"{len(sigs)} anchors ({skipped_num} numeral, skipped); wrote {a.out} ({os.path.getsize(a.out)/1e6:.2f} MB)")
    print("CROPQCUIDONE")


HTML = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · anchor crop QC</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;display:flex;gap:14px;
        align-items:center;flex-wrap:wrap;z-index:9}
 button{background:#2a7;color:#fff;border:0;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:13px}
 table{border-collapse:collapse;margin:10px} td,th{border:1px solid #ddd;padding:4px 6px;background:#fff;
   vertical-align:middle} th{background:#efece8;font-size:11px;position:sticky;top:40px}
 img{image-rendering:pixelated;background:#fff;max-width:380px}
 .t{font-size:11px;color:#555;max-width:150px;word-break:break-word}
 tr.rej td{background:#fbf2f2;opacity:.55} tr.fix td{background:#f2fbf6}
 tr.acc td{background:#f4faf4}
 .bd.ok.sel{background:#2a7;border-color:#2a7}
 .fx{font-size:10px;color:#777;margin-top:3px;max-width:170px}
 .hs{display:none}   /* Hi-SAM columns hidden: its masks clip words, so they showed nothing useful */
 .bd{border:1px solid #bbb;border-radius:12px;padding:2px 9px;cursor:pointer;font-size:11px;background:#f7f7f7}
 .bd.sel{background:#c33;color:#fff;border-color:#c33}
 select{font:11px system-ui;max-width:180px}
</style>
<header><b>anchor crop QC</b> — <span id=s></span>
 <label><input type=checkbox id=ho onchange=render()> only legacy blackletter</label>
 <label><input type=checkbox id=hd onchange=render()> hide reviewed</label>
 <button onclick=exportJSON()>Export corrections</button></header>
<table><thead><tr><th>text</th><th>signature</th><th>crop</th><th class=hs>Hi-SAM word</th>
 <th class=hs>Hi-SAM line</th><th>verdict</th></tr></thead><tbody id=b></tbody></table>
<script>
const D=__DATA__;
const dec={};   // row index -> {reject:true} | {sig:"..."}
function acc(i){ if(dec[i]&&dec[i].accept) delete dec[i]; else dec[i]={accept:true}; render(); }
function rej(i){ if(dec[i]&&dec[i].reject) delete dec[i]; else dec[i]={reject:true}; render(); }
function fix(i,s){ if(!s) delete dec[i]; else dec[i]={sig:s}; render(); }
function faceOf(it,d){                       // what an ACCEPT would mean, in inventory terms
  if(d.sig) return d.sig;
  const f=D.sigface[it.sig]||[];
  return f.length==1 ? f[0] : (f.length>1 ? f.join(' OR ') : '(no inventory face)');
}
function render(){
 const hd=document.getElementById('hd').checked, ho=document.getElementById('ho').checked;
 const its=D.items.filter(i=>!(hd&&dec[i.i]) && !(ho&&i.sig.indexOf('blackletter')<0));
 const nd=Object.keys(dec).length;
 document.getElementById('s').textContent =
   `${its.length} shown · ${nd}/${D.n} reviewed (${D.n-nd} untouched)`;
 document.getElementById('b').innerHTML=its.map(i=>{
  const d=dec[i.i]||{};
  const opts=D.sigs.map(s=>`<option${d.sig==s?' selected':''}>${s}</option>`).join('');
  return `<tr class="${d.reject?'rej':(d.sig?'fix':(d.accept?'acc':''))}">
   <td class=t>${i.text}</td><td class=t>${d.sig||i.sig}${d.sig?' <b>(fixed)</b>':''}</td>
   <td><img src="data:image/png;base64,${i.mr}"></td>
   <td class=hs><img src="data:image/png;base64,${i.word}"></td>
   <td class=hs><img src="data:image/png;base64,${i.line}"></td>
   <td><span class="bd ok${d.accept?' sel':''}" onclick="acc(${i.i})">accept</span>
       <span class="bd${d.reject?' sel':''}" onclick="rej(${i.i})">reject</span><br>
       <select onchange="fix(${i.i}, this.value)">
         <option value="">— assign inventory face —</option>${opts}</select>
       <div class=fx>${faceOf(i,d)}</div></td></tr>`;}).join('');
}
function exportJSON(){
 const out=[];
 D.items.forEach(i=>{ const d=dec[i.i]; if(!d) return;
   const f=(D.sigface[i.sig]||[]);
   out.push({gcx:i.gcx, gcy:i.gcy, text:i.text, was:i.sig,
             action:d.reject?'reject':(d.sig?'correct':'accept'),
             face:d.sig||(d.accept&&f.length==1?f[0]:null),
             unresolved:!!(d.accept&&f.length!=1)}); });
 if(!out.length){alert('Nothing marked yet.');return;}
 const blob=new Blob([JSON.stringify({corrections:out},null,1)],{type:'application/json'});
 const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
 a.download='anchor_corrections.json'; a.click();
}
render();
</script>
"""

if __name__ == "__main__":
    main()
