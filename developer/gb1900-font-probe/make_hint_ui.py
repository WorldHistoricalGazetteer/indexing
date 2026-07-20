"""Build the HITL text-hinting UI: for each common label word, show — per detected OS lettering style —
example crops + counts, so the reviewer can APPROVE/EDIT the feature type that word implies IN THAT STYLE
(the font-conditioned rules, e.g. 'Camp' in blackletter = antiquity, in roman = a modern place). Exports
font_hint_rules.json -> feeds fuse_edition.FONT_COND / LEXICON. Self-contained HTML, like the other HITL tools.

    /vast/ishi/envs/boundary/bin/python make_hint_ui.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, glob, json, re, base64, io, numpy as np
import concurrent.futures as cf
from collections import defaultdict, Counter
from PIL import Image
from make_font_testset_v2 import derotate

SPOT = "/vast/ishi/gb1900/edition/spot"; OUT = f"{SPOT}/hint_ui.html"
GATE = 0.7; MIN_TOTAL = 8; MAX_TERMS = 90; CROPS_PER = 3
STYLES = ["italic", "upright", "blackletter", "numeral"]
VOCAB = ["antiquity", "river", "canal", "lake", "spring", "water_feature", "coastal_feature", "church",
         "wood", "quarry", "mill", "building_or_feature", "bridge", "relief", "settlement", "road",
         "elevation", "boundary", "(ignore)"]

def norm(t): return re.sub(r"[^a-z]", "", (t or "").lower())

def main():
    # gpoly lookup from raw spotter boxes (boxes_font lacks geometry)
    gpoly = {}
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        if f.endswith("boxes_font.jsonl"): continue
        for line in open(f):
            r = json.loads(line)
            if "gpoly" in r: gpoly[(r["gcx"], r["gcy"])] = r["gpoly"]
    # group font-classified boxes by (term, style)
    by = defaultdict(lambda: defaultdict(list)); raw = {}
    for line in open(f"{SPOT}/boxes_font.jsonl"):
        r = json.loads(line)
        if r["conf"] < GATE: continue
        w = norm(r["text"])
        if len(w) < 3: continue
        by[w][r["font"]].append(r); raw.setdefault(w, r["text"].strip())
    terms = sorted(((w, sum(len(v) for v in fs.values()), fs) for w, fs in by.items() if sum(len(v) for v in fs.values()) >= MIN_TOTAL),
                   key=lambda x: -x[1])[:MAX_TERMS]
    print(f"terms: {len(terms)}", flush=True)

    def crop_b64(r):
        gp = gpoly.get((r["gcx"], r["gcy"]))
        if not gp: return None
        patch = derotate({"gpoly": gp, "lat": r["lat"]})
        if patch is None or patch.size < 60: return None
        im = Image.fromarray(patch).convert("L"); h = 40; im = im.resize((max(1, int(im.width * h / im.height)), h), Image.LANCZOS)
        b = io.BytesIO(); im.save(b, "JPEG", quality=82); return base64.b64encode(b.getvalue()).decode()

    cards = []
    jobs = []
    for w, tot, fs in terms:
        for st in STYLES:
            for r in fs.get(st, [])[:CROPS_PER]: jobs.append((w, st, r))
    imgs = defaultdict(lambda: defaultdict(list))
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for (w, st, r), b in zip(jobs, ex.map(lambda j: crop_b64(j[2]), jobs)):
            if b: imgs[w][st].append(b)
    data = []
    for w, tot, fs in terms:
        data.append(dict(term=raw[w], key=w, total=tot,
                         styles={st: {"n": len(fs.get(st, [])), "crops": imgs[w].get(st, [])} for st in STYLES if fs.get(st)}))

    html = HTML.replace("__DATA__", json.dumps(data)).replace("__VOCAB__", json.dumps(VOCAB)).replace("__STYLES__", json.dumps(STYLES))
    open(OUT, "w").write(html)
    print(f"wrote {OUT} ({os.path.getsize(OUT)//1024} KB), {len(data)} terms", flush=True)

HTML = r"""<!doctype html><html><head><meta charset=utf-8><title>GB-STAMP — text-hint review</title>
<style>
 body{font-family:system-ui,sans-serif;background:#f4f1ea;color:#2b2b2b;margin:0;padding:14px}
 header{position:sticky;top:0;background:#f4f1ea;padding:8px 2px;border-bottom:1px solid #d9d2c2;z-index:5}
 h1{font-size:16px;margin:0 0 6px} button{padding:6px 12px;border:1px solid #d9d2c2;background:#fffdf7;border-radius:6px;cursor:pointer}
 #prog{font-weight:600;font-variant-numeric:tabular-nums}
 .card{background:#fffdf7;border:1px solid #d9d2c2;border-radius:8px;padding:10px;margin-top:10px}
 .term{font-family:Georgia,serif;font-size:17px;font-weight:700} .muted{color:#888;font-size:12px}
 .styles{display:flex;flex-wrap:wrap;gap:14px;margin-top:8px}
 .st{border:1px solid #e7e0d0;border-radius:6px;padding:8px;min-width:210px}
 .stname{font-weight:600;font-size:13px;margin-bottom:4px}
 .crops img{height:36px;margin:2px;background:#fbfaf5;border:1px solid #eee;border-radius:3px}
 select{font-size:13px;padding:3px;margin-top:5px;width:100%}
 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px}
</style></head><body>
<header><h1>GB-STAMP — text-hint review: what does each word mean <em>in each lettering style</em>?</h1>
<div><span id=prog>0</span> rules set · <button onclick=dl()>Download rules JSON</button>
 <label style="cursor:pointer">Import<input type=file style=display:none onchange=imp(event)></label>
 <span class=muted>Set the feature type each word implies in each style; leave blank to skip. Blackletter usually = antiquity.</span></div></header>
<div id=grid></div>
<script>
const DATA=__DATA__, VOCAB=__VOCAB__, STYLES=__STYLES__;
const COL={italic:"#c07a2b",upright:"#3f6fa8",blackletter:"#8a4fa0",numeral:"#777"};
let R=JSON.parse(localStorage.getItem("gbstamp_hints")||"{}");
function save(){localStorage.setItem("gbstamp_hints",JSON.stringify(R));prog()}
function prog(){document.getElementById("prog").textContent=Object.values(R).reduce((a,o)=>a+Object.values(o).filter(Boolean).length,0)}
function set(k,st,v){R[k]=R[k]||{};R[k][st]=v;save()}
function render(){
 const g=document.getElementById("grid");g.innerHTML="";
 DATA.forEach(d=>{
  const cols=Object.entries(d.styles).map(([st,info])=>{
   const cur=(R[d.key]||{})[st]||"";
   const opts=['<option value="">— type —</option>'].concat(VOCAB.map(v=>`<option ${v===cur?"selected":""}>${v}</option>`)).join("");
   const crops=(info.crops||[]).map(b=>`<img src="data:image/jpeg;base64,${b}">`).join("");
   return `<div class=st><div class=stname><span class=dot style="background:${COL[st]}"></span>${st} <span class=muted>×${info.n}</span></div>
     <div class=crops>${crops}</div><select onchange="set('${d.key}','${st}',this.value)">${opts}</select></div>`;
  }).join("");
  const el=document.createElement("div");el.className="card";
  el.innerHTML=`<span class=term>${d.term.replace(/</g,"&lt;")}</span> <span class=muted>×${d.total}</span><div class=styles>${cols}</div>`;
  g.appendChild(el);
 });
}
function dl(){const out=[];for(const[k,o]of Object.entries(R))for(const[st,ty]of Object.entries(o))if(ty)out.push({term:k,font:st,type:ty});
 const b=new Blob([JSON.stringify(out,null,1)],{type:"application/json"});const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="font_hint_rules.json";a.click();}
function imp(e){const r=new FileReader();r.onload=()=>{R={};JSON.parse(r.result).forEach(x=>{R[x.term]=R[x.term]||{};R[x.term][x.font]=x.type});save();render()};r.readAsText(e.target.files[0])}
render();prog();
</script></body></html>"""

if __name__ == "__main__":
    main()
