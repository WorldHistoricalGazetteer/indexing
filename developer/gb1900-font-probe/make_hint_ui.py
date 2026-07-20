"""HITL text-hint UI (v2). Rules key on the WHOLE GB1900 label (so a rule for 'Stone' fires only when the
label is exactly 'Stone'; multi-word tags like 'Chalk Pit' are their own rules), split by detected OS
lettering style. Each spotter word-box is joined back to its GB1900 pin to recover the full label; groups
are ranked, isolated (single-word) vs phrase (multi-word) flagged, with per-style example crops (de-rotated
full label) + counts. Type entry is an AAT SEARCH backed by the resolved vocabulary. Exports
font_hint_rules.json: [{label, isolated, font, match(exact|suffix), aat_id, aat_term}].

    /vast/ishi/envs/boundary/bin/python make_hint_ui.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, glob, json, re, base64, io, math, numpy as np
import concurrent.futures as cf
from collections import defaultdict, Counter
from PIL import Image
from make_font_testset_v2 import derotate

SPOT = "/vast/ishi/gb1900/edition/spot"; OUT = f"{SPOT}/hint_ui.html"
NT = "/vast/ishi/gb1900/edition/national_typed.jsonl"
VOCAB = json.load(open(f"{SPOT.replace('/edition/spot','/probe/font')}/aat_vocab.json")) if os.path.exists("/vast/ishi/gb1900/probe/font/aat_vocab.json") else []
GATE = 0.7; MIN_TOTAL = 6; MAX_GROUPS = 140; CROPS_PER = 3
STYLES = ["italic", "upright", "blackletter", "numeral"]

def norm(t): return re.sub(r"\s+", " ", (t or "").strip()).lower()

def main():
    # 1. font-classified boxes + their geometry (gpoly from raw spotter boxes)
    gpoly = {}
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        if f.endswith("boxes_font.jsonl"): continue
        for line in open(f):
            r = json.loads(line)
            if "gpoly" in r: gpoly[(r["gcx"], r["gcy"])] = r["gpoly"]
    fboxes = []
    for line in open(f"{SPOT}/boxes_font.jsonl"):
        r = json.loads(line)
        if r["conf"] >= GATE: fboxes.append(r)
    box_buckets = {(round(r["lon"], 2), round(r["lat"], 2)) for r in fboxes}
    print(f"font boxes: {len(fboxes)}", flush=True)

    # 2. GB1900 pins near the boxes only (full label text)
    pins = defaultdict(list)                                  # fine grid -> [(text, lon, lat)]
    for line in open(NT):
        try: d = json.loads(line)
        except Exception: continue
        lo, la = d.get("lon"), d.get("lat")
        if lo is None or (round(lo, 2), round(la, 2)) not in box_buckets: continue
        tv = d.get("text"); tx = tv.get("value") if isinstance(tv, dict) else tv
        if tx: pins[(round(lo, 3), round(la, 3))].append((tx, lo, la))
    print(f"nearby pins indexed", flush=True)

    def nearest_pin(lon, lat):
        best, bd = None, 6e-4
        rl, ra = round(lon, 3), round(lat, 3)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (t, lo, la) in pins.get((round(rl + dx * 1e-3, 3), round(ra + dy * 1e-3, 3)), []):
                    dd = math.hypot(lo - lon, la - lat)
                    if dd < bd: bd, best = dd, (t, lo, la)
        return best

    # 3. join boxes -> pins; accumulate per pin: fonts + gpolys
    perpin = defaultdict(lambda: {"fonts": Counter(), "gp": [], "lat": None, "text": None})
    for r in fboxes:
        pin = nearest_pin(r["lon"], r["lat"])
        if not pin: continue
        k = (round(pin[1], 6), round(pin[2], 6))
        p = perpin[k]; p["fonts"][r["font"]] += r["conf"]; p["lat"] = pin[2]; p["text"] = pin[0]
        gp = gpoly.get((r["gcx"], r["gcy"]))
        if gp: p["gp"] += gp
    # 4. group pins by (label, dominant font)
    groups = defaultdict(list)                                # (norm_label, font) -> [pin dict]
    for k, p in perpin.items():
        if not p["fonts"] or not p["gp"]: continue
        font = p["fonts"].most_common(1)[0][0]
        groups[(norm(p["text"]), font)].append(p)
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    # keep top groups but ensure some phrase coverage
    ranked = [g for g in ranked if len(g[1]) >= MIN_TOTAL][:MAX_GROUPS]
    print(f"label×style groups: {len(ranked)}", flush=True)

    def crop(p):
        patch = derotate({"gpoly": p["gp"], "lat": p["lat"]})
        if patch is None or patch.size < 60: return None
        im = Image.fromarray(patch).convert("L"); h = 200
        im = im.resize((max(1, int(im.width * h / im.height)), h), Image.LANCZOS)
        b = io.BytesIO(); im.save(b, "JPEG", quality=90); return base64.b64encode(b.getvalue()).decode()
    # crop examples per group
    jobs = [(gi, p) for gi, (_, ps) in enumerate(ranked) for p in ps[:CROPS_PER]]
    crops = defaultdict(list)
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for (gi, p), b in zip(jobs, ex.map(lambda j: crop(j[1]), jobs)):
            if b: crops[gi].append(b)

    # merge the (label, font) groups into per-LABEL cards with style columns
    bylabel = defaultdict(dict)
    labelmeta = {}
    for gi, ((lab, font), ps) in enumerate(ranked):
        bylabel[lab][font] = {"n": len(ps), "crops": crops.get(gi, [])}
        labelmeta[lab] = {"disp": ps[0]["text"], "isolated": " " not in lab.strip()}
    data = [dict(label=lab, disp=labelmeta[lab]["disp"], isolated=labelmeta[lab]["isolated"],
                 tot=sum(v["n"] for v in st.values()),
                 styles={f: st[f] for f in STYLES if f in st}) for lab, st in bylabel.items()]
    data.sort(key=lambda d: (not d["isolated"], -d["tot"]))

    html = (HTML.replace("__DATA__", json.dumps(data)).replace("__VOCAB__", json.dumps(VOCAB))
                .replace("__STYLES__", json.dumps(STYLES)))
    open(OUT, "w").write(html)
    print(f"wrote {OUT} ({os.path.getsize(OUT)//1024} KB); {len(data)} labels "
          f"({sum(1 for d in data if d['isolated'])} isolated, {sum(1 for d in data if not d['isolated'])} phrases)", flush=True)

HTML = r"""<!doctype html><html><head><meta charset=utf-8><title>GB-STAMP — text-hint review</title>
<style>
 body{font-family:system-ui,sans-serif;background:#f4f1ea;color:#2b2b2b;margin:0;padding:14px}
 header{position:sticky;top:0;background:#f4f1ea;padding:8px 2px;border-bottom:1px solid #d9d2c2;z-index:5}
 h1{font-size:16px;margin:0 0 6px} button,input[type=text]{padding:6px 10px;border:1px solid #d9d2c2;background:#fffdf7;border-radius:6px}
 #prog{font-weight:600} .muted{color:#888;font-size:12px}
 .card{background:#fffdf7;border:1px solid #d9d2c2;border-radius:8px;padding:10px 12px;margin-top:10px}
 .term{font-family:Georgia,serif;font-size:18px;font-weight:700}
 .badge{font-size:11px;padding:2px 7px;border-radius:10px;margin-left:8px}
 .iso{background:#2f6f4f;color:#fff} .phr{background:#8a6d3b;color:#fff}
 .styles{display:flex;flex-wrap:wrap;gap:14px;margin-top:8px}
 .st{border:1px solid #e7e0d0;border-radius:6px;padding:8px;min-width:230px}
 .stname{font-weight:600;font-size:13px;margin-bottom:4px}
 .crops{display:flex;flex-wrap:wrap;gap:5px}
 .crops img{height:64px;background:#fbfaf5;border:1px solid #ddd;border-radius:3px;cursor:zoom-in}
 .st input{width:100%;margin-top:6px;font-size:13px} .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px}
 .filt{margin-left:10px} #lb{position:fixed;inset:0;background:rgba(0,0,0,.82);display:none;align-items:center;justify-content:center;z-index:50;cursor:zoom-out}
 #lb img{max-width:94vw;max-height:90vh;background:#fbfaf5;border:4px solid #fff;border-radius:4px}
</style></head><body>
<div id=lb onclick="this.style.display='none'"><img id=lbimg></div>
<header><h1>GB-STAMP — text-hint review · rules apply to the WHOLE label (isolation) unless it's a phrase</h1>
<div><span id=prog>0</span> rules · <button onclick=dl()>Download rules JSON</button>
 <label style="cursor:pointer">Import<input type=file style=display:none onchange=imp(event)></label>
 <span class=filt><label><input type=checkbox id=fiso checked> isolated</label>
 <label><input type=checkbox id=fphr checked> phrases</label></span>
 <span class=muted>Type an AAT term (autocompletes); leave blank to skip.</span></div></header>
<datalist id=aat></datalist>
<div id=grid></div>
<script>
const DATA=__DATA__, VOCAB=__VOCAB__, STYLES=__STYLES__;
const COL={italic:"#c07a2b",upright:"#3f6fa8",blackletter:"#8a4fa0",numeral:"#777"};
const TERM2ID={}; VOCAB.forEach(v=>TERM2ID[v.term.toLowerCase()]=v.id);
document.getElementById("aat").innerHTML=VOCAB.map(v=>`<option value="${v.term}">`).join("");
let R=JSON.parse(localStorage.getItem("gbstamp_hints2")||"{}");
function save(){localStorage.setItem("gbstamp_hints2",JSON.stringify(R));prog()}
function prog(){document.getElementById("prog").textContent=Object.values(R).reduce((a,o)=>a+Object.values(o).filter(rc=>rc&&rc.t).length,0)}
function set(lab,st,f,v){R[lab]=R[lab]||{}; R[lab][st]=R[lab][st]||{}; R[lab][st][f]=v; if(f=='t'&&!v)delete R[lab][st]; save()}
function render(){
 const iso=document.getElementById("fiso").checked, phr=document.getElementById("fphr").checked;
 const g=document.getElementById("grid");g.innerHTML="";
 DATA.filter(d=>d.isolated?iso:phr).forEach(d=>{
  const cols=Object.entries(d.styles).map(([st,info])=>{
   const rc=(R[d.label]||{})[st]||{}; const cur=rc.t||""; const cm=rc.m||"exact"; const L=d.label.replace(/'/g,"\\'");
   const crops=(info.crops||[]).map(b=>`<img src="data:image/jpeg;base64,${b}">`).join("");
   const msel=`<select onchange="set('${L}','${st}','m',this.value)"><option value=exact ${cm=='exact'?'selected':''}>whole label (isolation)</option><option value=suffix ${cm=='suffix'?'selected':''}>as suffix (…${d.disp})</option></select>`;
   return `<div class=st><div class=stname><span class=dot style="background:${COL[st]}"></span>${st} <span class=muted>×${info.n}</span></div>
     <div class=crops>${crops}</div><input type=text list=aat placeholder="AAT type…" value="${cur.replace(/"/g,'&quot;')}"
       oninput="set('${L}','${st}','t',this.value)">${msel}</div>`;
  }).join("");
  const el=document.createElement("div");el.className="card";
  el.innerHTML=`<span class=term>${(d.disp||"").replace(/</g,"&lt;")}</span>`+
    `<span class="badge ${d.isolated?'iso':'phr'}">${d.isolated?'isolated':'phrase'}</span> <span class=muted>×${d.tot}</span>`+
    `<div class=styles>${cols}</div>`;
  g.appendChild(el);
 });
}
function dl(){const out=[];for(const[lab,o]of Object.entries(R))for(const[st,rc]of Object.entries(o))if(rc&&rc.t){
   out.push({label:lab,isolated:!lab.includes(" "),font:st,match:rc.m||"exact",aat_term:rc.t,aat_id:TERM2ID[rc.t.toLowerCase()]||null});}
 const b=new Blob([JSON.stringify(out,null,1)],{type:"application/json"});const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="font_hint_rules.json";a.click();}
function imp(e){const r=new FileReader();r.onload=()=>{R={};JSON.parse(r.result).forEach(x=>{const l=x.label||x.term;R[l]=R[l]||{};R[l][x.font]={t:x.aat_term||x.type,m:x.match||"exact"}});save();render()};r.readAsText(e.target.files[0])}
document.getElementById("grid").addEventListener("click",e=>{if(e.target.tagName==="IMG"){document.getElementById("lbimg").src=e.target.src;document.getElementById("lb").style.display="flex";}});
document.getElementById("fiso").addEventListener("change",render);
document.getElementById("fphr").addEventListener("change",render);
render();prog();
</script></body></html>"""

if __name__ == "__main__":
    main()
