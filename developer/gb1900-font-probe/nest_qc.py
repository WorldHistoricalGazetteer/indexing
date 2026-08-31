"""Stage C — put the nest-matched labels in front of a human, pre-filled with the face the gazetteer implies.

Split from stage B because the two need different machines: the containment query needs prod ES, which listens
on localhost on the Pitt VM, while cropping needs the tile cache, which is on CRC. Neither host has both.

Every row arrives with a face already chosen, so the reviewer's job is confirmation rather than search — but
the pre-fill is the gazetteer's opinion, not evidence. Rows whose name matched at two levels at once are
flagged rather than resolved, and rows drawn from a `weak` or `proxy` level say so on the row, because a
municipal borough inferred from a flat `local-government-district` type is a different quality of claim from a
parish matched in Kain & Oliver.

    python nest_qc.py --matches labels/nest_matches.json --qc nest_qc.html
"""
import argparse, base64, io, json, os, sys
from collections import Counter
import numpy as np

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_font_testset_v2 import derotate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", default="labels/nest_matches.json")
    ap.add_argument("--inventory", default="labels/face_inventory.json")
    ap.add_argument("--n", type=int, default=400,
                    help="rows on the page. Ordered to put the RARE faces first — the common ones are already "
                         "well sampled and reviewing more of them buys nothing")
    ap.add_argument("--qc", default="nest_qc.html")
    a = ap.parse_args()

    M = json.load(open(a.matches))["matches"]
    FACES = list(json.load(open(a.inventory))["faces"])
    print(f"{len(M)} nest matches", flush=True)

    # Rarest face first, then the most confident match within each face. The whole point of this route is the
    # faces that have no samples; a page sorted by confidence alone would fill with counties.
    per = Counter(m["face"] for m in M)
    M.sort(key=lambda m: (per[m["face"]], -m["match"]["sim"]))
    M = M[: a.n]

    items, miss = [], 0
    for m in M:
        crop = derotate({"gpoly": m["gpoly"]})
        if crop is None or crop.size < 200:
            miss += 1
            continue
        from PIL import Image
        im = Image.fromarray(crop)
        if im.height > 120:
            im = im.resize((max(1, int(im.width * 120 / im.height)), 120), Image.LANCZOS)
        b = io.BytesIO()
        im.save(b, "PNG")
        items.append(dict(gcx=m["gcx"], gcy=m["gcy"], lon=m["lon"], lat=m["lat"], gpoly=m["gpoly"],
                          sheet=m.get("sheet"), cap=m.get("cap"), text=m.get("text"),
                          face=m["face"], designation=m["designation"], trust=m["trust"],
                          matched=m["match"]["title"], ns=m["match"]["namespace"],
                          sim=m["match"]["sim"], ambiguous=m["ambiguous"],
                          ambiguous_with=m.get("ambiguous_with", []),
                          img=base64.b64encode(b.getvalue()).decode()))
    print(f"{len(items)} rows ({miss} with no usable crop)")
    print("faces on the page:", dict(Counter(i["face"] for i in items)))
    open(a.qc, "w").write(QC.replace("__DATA__", json.dumps(dict(items=items, faces=FACES))))
    print(f"wrote {a.qc} ({os.path.getsize(a.qc)/1e6:.2f} MB)")
    print("NESTQCDONE", flush=True)


QC = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · nest-matched admin labels</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;display:flex;gap:14px;
        align-items:center;flex-wrap:wrap;z-index:9}
 button{background:#2a7;color:#fff;border:0;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:13px}
 .it{background:#fff;border:1px solid #ddd;border-radius:6px;margin:8px 12px;padding:8px 10px;
     display:flex;gap:14px;align-items:center}
 .it.ok{background:#f2fbf6;border-color:#8ccdae}
 .it.rej{background:#fbf2f2;border-color:#e0a0a0;opacity:.55}
 .it img{image-rendering:pixelated;max-width:60%;background:#fff;border:1px solid #eee}
 .m{font-size:11px;color:#666;margin-top:4px}
 .ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 select{font:13px system-ui;padding:3px;max-width:230px}
 .bd{border:1px solid #bbb;border-radius:12px;padding:3px 10px;cursor:pointer;font-size:12px;background:#f7f7f7}
 .bd.yes{border-color:#2a7;background:#eafaf1;font-weight:600}
 .bd.no{border-color:#c88;background:#fbf0f0}
 .bd.sel{background:#2a7;color:#fff;border-color:#2a7}
 .tag{font-size:10px;border-radius:8px;padding:1px 7px;margin-left:6px}
 .t-strong{background:#e6f4ea;color:#1a6b3a}
 .t-proxy{background:#fdf3e0;color:#8a5a00}
 .t-weak{background:#fdeaea;color:#a33}
 .amb{background:#efe6fb;color:#63c}
</style>
<header><b>nest-matched admin labels</b>
 <span class=m style=color:#ccc>face pre-filled from the CONTAINING gazetteer unit — a prior, not an answer</span>
 <label><input type=checkbox id=ha onchange=render()> hide ambiguous</label>
 <label><input type=checkbox id=hd onchange=render()> hide decided</label>
 <button onclick=exportJSON()>Export decisions</button>
 <span id=s></span></header>
<div id=w></div>
<script>
const D=__DATA__;
const dec={};
const key=i=>`${i.gcx},${i.gcy}`;
function set(i,face){ const k=key(i);
 if(face===null){dec[k]={reject:true};}
 else if(!face){delete dec[k];}
 else if(dec[k]&&dec[k].face===face){delete dec[k];}
 else {dec[k]={face};}
 render(); }
function render(){
 const ha=document.getElementById('ha').checked, hd=document.getElementById('hd').checked;
 const its=D.items.filter(i=>!(ha&&i.ambiguous)&&!(hd&&dec[key(i)]));
 document.getElementById('s').textContent=`${its.length} shown · ${Object.keys(dec).length} decided`;
 document.getElementById('w').innerHTML=its.map(i=>{
  const n=D.items.indexOf(i), d=dec[key(i)]||{};
  const opts=D.faces.map(f=>`<option${d.face==f?' selected':''}>${f}</option>`).join('');
  const amb=i.ambiguous?`<span class="tag amb">same name at ${i.ambiguous_with.join(', ')}</span>`:'';
  return `<div class="it${d.face?' ok':''}${d.reject?' rej':''}">
   <img src="data:image/png;base64,${i.img}">
   <div>
    <div class=ctl>
      <span class="bd yes${d.face==i.face?' sel':''}" onclick="set(D.items[${n}],'${i.face}')">${i.face}</span>
      <select onchange="set(D.items[${n}], this.value)">
        <option value="">— different face —</option>${opts}</select>
      <span class="bd no" onclick="set(D.items[${n}], null)">not this label</span>
    </div>
    <div class=m>read &ldquo;${(i.text||'').replace(/</g,'&lt;')}&rdquo; · matched <b>${i.matched}</b>
      (${i.ns}, sim ${i.sim}) · ${i.designation}
      <span class="tag t-${i.trust}">${i.trust}</span>${amb}</div>
    <div class=m>${i.sheet} · cap ${i.cap}px</div>
   </div></div>`;}).join('');
}
function exportJSON(){
 const out=[];
 D.items.forEach(i=>{ const d=dec[key(i)]; if(!d) return;
   out.push({gcx:i.gcx,gcy:i.gcy,lon:i.lon,lat:i.lat,gpoly:i.gpoly,sheet:i.sheet,cap:i.cap,
             text:"",read:i.text,matched:i.matched,ns:i.ns,
             face:d.face||null,reject:!!d.reject,source:"nest-match"}); });
 if(!out.length){alert('Nothing decided yet.');return;}
 const blob=new Blob([JSON.stringify({decisions:out},null,1)],{type:'application/json'});
 const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
 a.download='nest_decisions.json'; a.click();
}
render();
</script>
"""

if __name__ == "__main__":
    main()
