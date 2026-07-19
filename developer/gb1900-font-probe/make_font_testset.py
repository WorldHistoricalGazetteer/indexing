"""Phase C (c) — build a HITL font-labelling tool for a stratified sample of REAL map-label crops.

Font ground-truth can only come from a human eye. This samples real discovery labels (stratified so italic/
blackletter/upright are all represented — watercourse- and antiquity-dense labels are included FOR COVERAGE
only; the font label comes from the reviewer, never the text), crops each from z17 tiles, upscales for
legibility, and writes a self-contained HTML page with a per-crop font picker + JSON export/import +
localStorage autosave. The exported font_testset_decisions.json becomes the validation set for (a) onward.

    /vast/ishi/envs/boundary/bin/python make_font_testset.py     # -> font_testset.html
"""
import os, re, glob, json, base64, random, io, numpy as np
import concurrent.futures as cf
from collections import Counter
from PIL import Image
from discrim_test import crop_box

DISC = "/vast/ishi/gb1900/edition/discover"; OUT = f"{DISC}/font_testset.html"
random.seed(42)
WATER = re.compile(r"^(R\.|Afon|Nant)\b|\b(River|Brook|Burn|Beck|Stream|Canal|Lake|Mere|Tarn)\b", re.I)
ANTIQ = re.compile(r"\b(Tumulus|Tumuli|Earthwork|Earthworks|Cairn|Stone Circle|Standing Stone|Site of|Camp|"
                   r"Enclosure|Castle|Barrow|Fort|Moat|Priory|Abbey|Roman)\b", re.I)
N_WATER, N_ANTIQ, N_RAND = 70, 70, 90     # stratified for COVERAGE only

def collect():
    labs = []
    for f in glob.glob(f"{DISC}/labels_*.json"):
        for Lb in json.load(open(f)):
            t = (Lb.get("crowd") or "").strip()
            if t and "box_g" in Lb and len([c for c in t if c.isalnum()]) >= 3:
                labs.append((Lb["box_g"], t))
    return labs

def sample(labs):
    water = [l for l in labs if WATER.search(l[1])]
    antiq = [l for l in labs if ANTIQ.search(l[1])]
    picked = {}
    for pool, n in [(water, N_WATER), (antiq, N_ANTIQ)]:
        random.shuffle(pool)
        for l in pool[:n]: picked[tuple(l[0])] = l
    rest = [l for l in labs if tuple(l[0]) not in picked]
    random.shuffle(rest)
    for l in rest[:N_RAND]: picked[tuple(l[0])] = l
    out = list(picked.values()); random.shuffle(out)
    return out

def crop_b64(job):
    box_g, text = job
    c = crop_box(box_g, pad=8)
    if c is None or c.size < 80: return None
    im = Image.fromarray(c).convert("L")
    im = im.resize((im.width * 3, im.height * 3), Image.LANCZOS)      # upscale for legibility
    buf = io.BytesIO(); im.save(buf, "PNG")
    return dict(text=text, img=base64.b64encode(buf.getvalue()).decode())

HTML = """<!doctype html><html><head><meta charset=utf-8><title>GB1900 font test set</title>
<style>
 :root{{--bg:#f4f1ea;--card:#fffdf7;--ink:#2b2b2b;--line:#d9d2c2;--sel:#2f6f4f}}
 body{{font-family:system-ui,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:16px}}
 header{{position:sticky;top:0;background:var(--bg);padding:10px 4px;border-bottom:1px solid var(--line);z-index:5}}
 h1{{font-size:16px;margin:0 0 6px}} .bar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
 button.act{{padding:6px 12px;border:1px solid var(--line);background:var(--card);border-radius:6px;cursor:pointer}}
 #prog{{font-variant-numeric:tabular-nums;font-weight:600}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;margin-top:12px}}
 .card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;display:flex;flex-direction:column;gap:6px}}
 .imgwrap{{background:#fbfaf5;border:1px solid var(--line);border-radius:4px;min-height:70px;display:flex;align-items:center;justify-content:center;overflow:auto}}
 .imgwrap img{{image-rendering:auto;max-width:100%}}
 .txt{{font-size:13px;color:#555;word-break:break-word}} .idx{{font-size:11px;color:#999}}
 .opts{{display:flex;flex-wrap:wrap;gap:4px}}
 .opt{{font-size:11px;padding:3px 7px;border:1px solid var(--line);border-radius:12px;cursor:pointer;background:#fff}}
 .opt.sel{{background:var(--sel);color:#fff;border-color:var(--sel)}}
 .card.done{{outline:2px solid var(--sel)}}
</style></head><body>
<header><h1>GB1900 font test set — label each crop by its LETTERFORM (not what it means)</h1>
<div class=bar>
 <span id=prog>0/0</span>
 <button class=act onclick=dl()>Download JSON</button>
 <label class=act>Import JSON<input type=file style=display:none onchange=imp(event)></label>
 <span class=txt>upright = plain roman · italic = sloping · blackletter = ornate Gothic · numeral = digits · mixed/unclear as needed</span>
</div></header>
<div class=grid id=grid></div>
<script>
const CROPS = {crops};
const OPTS = ["upright","italic","blackletter","numeral","mixed","unclear"];
let D = JSON.parse(localStorage.getItem("font_testset")||"{{}}");
function save(){{localStorage.setItem("font_testset",JSON.stringify(D));prog()}}
function prog(){{document.getElementById("prog").textContent=Object.keys(D).length+"/"+CROPS.length+" labelled"}}
function pick(i,o){{D[i]=o;save();render()}}
function render(){{
 const g=document.getElementById("grid");g.innerHTML="";
 CROPS.forEach((c,i)=>{{
  const d=document.createElement("div");d.className="card"+(D[i]?" done":"");
  d.innerHTML=`<div class=idx>#${{i}}</div><div class=imgwrap><img src="data:image/png;base64,${{c.img}}"></div>`+
   `<div class=txt>${{c.text.replace(/</g,"&lt;")}}</div>`+
   `<div class=opts>${{OPTS.map(o=>`<span class="opt${{D[i]===o?" sel":""}}" onclick="pick(${{i}},'${{o}}')">${{o}}</span>`).join("")}}</div>`;
  g.appendChild(d);
 }});
}}
function dl(){{
 const out=CROPS.map((c,i)=>({{i,text:c.text,font:D[i]||null}}));
 const b=new Blob([JSON.stringify(out,null,1)],{{type:"application/json"}});
 const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="font_testset_decisions.json";a.click();
}}
function imp(e){{const r=new FileReader();r.onload=()=>{{const j=JSON.parse(r.result);D={{}};j.forEach(x=>{{if(x.font)D[x.i]=x.font}});save();render()}};r.readAsText(e.target.files[0])}}
render();prog();
</script></body></html>"""

def main():
    labs = collect(); print(f"labels available: {len(labs)}", flush=True)
    samp = sample(labs); print(f"sampled: {len(samp)}", flush=True)
    crops = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(crop_b64, samp):
            if r: crops.append(r)
    print(f"cropped: {len(crops)}", flush=True)
    open(OUT, "w").write(HTML.format(crops=json.dumps(crops)))
    print(f"wrote {OUT} ({os.path.getsize(OUT)//1024} KB)", flush=True)

if __name__ == "__main__":
    main()
