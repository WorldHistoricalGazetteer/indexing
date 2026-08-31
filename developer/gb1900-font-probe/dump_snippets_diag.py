"""Build a HITL review page of the fan's font assignments, from real map label snippets (de-rotated word
crops — not glyph rasters). Two sections:
  A) REFERENCE — per font, the label snippets the system confidently assigned to it (so a human can
     adjudicate the fan's own reference: are these really that face?).
  B) CONFUSED — for the most-confused face PAIRS, the snippets the system is genuinely torn between
     (top-1/top-2 close), beside each face's Characteristic-Sheet exemplar.

    FCTILES=/vast/ishi/gb1900/fc_tiles /vast/ishi/envs/boundary/bin/python dump_snippets.py  # -> snippets.html
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, json, base64, io, glob, resource, numpy as np
from collections import defaultdict
from PIL import Image
from build_alphabet import build_buckets, match_glyph, GLYPH_MIN, force_split, HIGH
from make_font_testset_v2 import derotate

HERE = "/vast/ishi/gb1900/probe/font"; OUT = "/vast/ishi/gb1900/edition/discover"; SPOT = "/vast/ishi/gb1900/edition/spot"
TAX = {x["key"]: x for x in json.load(open(f"{HERE}/font_taxonomy.json"))}
PERFONT_CAP, PAIR_CAP = 36, 24

d = np.load(f"{OUT}/alphabet_multi.npz", allow_pickle=True)
alpha = [dict(L=str(d["letter"][i]), cap=bool(d["cap"][i]), style=str(d["style"][i]), glyph=d["glyphs"][i])
         for i in range(len(d["glyphs"]))]
B = build_buckets(alpha)

def vote(glyphs):
    w = defaultdict(float); nv = 0
    for L, cap, g in glyphs:
        s, sc, m = match_glyph(g, L, cap, B)
        if s and sc >= GLYPH_MIN and m > 0: w[s] += sc * m; nv += 1
    return w, nv

def b64(patch):
    im = Image.fromarray(patch).convert("L"); h = 40
    im = im.resize((max(1, int(im.width * h / max(1, im.height))), h))
    bio = io.BytesIO(); im.save(bio, "PNG"); return base64.b64encode(bio.getvalue()).decode()

ALLF = set(t["style"] for t in alpha)
perfont = defaultdict(list); pairbox = defaultdict(list); nproc = 0; MAXPROC = 1500
lines = [l for fp in glob.glob(f"{SPOT}/boxes_gb_*.jsonl") for l in open(fp)]
import random; random.seed(0); random.shuffle(lines)          # sample across regions, don't front-load
for line in lines:
    if nproc >= MAXPROC: break
    r = json.loads(line)
    if r.get("score", 0) < 0.55 or len([c for c in r["text"] if c.isalnum()]) < 2 or not r.get("gpoly"): continue
    poly = np.asarray(r["gpoly"], np.float32)          # skip degenerate boxes (an outlier vertex blows up assemble -> OOM)
    bw = float(poly[:, 0].max() - poly[:, 0].min()); bh = float(poly[:, 1].max() - poly[:, 1].min())
    if not (4 < bw < 1200 and 4 < bh < 400): continue
    patch = derotate(r)
    if patch is None: continue
    letters = [c for c in r["text"] if c.isalnum()]
    gs = force_split(patch, len(letters))
    if len(gs) != len(letters): continue
    nproc += 1
    if nproc % 100 == 0:
        full = sum(1 for f in ALLF if len(perfont[f]) >= PERFONT_CAP)
        print(f"  processed {nproc}; full {full}/{len(ALLF)}; RSS={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss//1024}MB", flush=True)
        if full >= len(ALLF) - 4 and sum(len(v) for v in pairbox.values()) > 150: break
    w, nv = vote([(letters[i].upper(), letters[i].isupper(), gs[i]) for i in range(len(letters))])
    if nv < 2 or not w: continue
    rk = sorted(w.items(), key=lambda kv: -kv[1]); tot = sum(w.values()) + 1e-9
    conf = rk[0][1] / tot; top1 = rk[0][0]; top2 = rk[1][0] if len(rk) > 1 else None
    torn = top2 and rk[1][1] > 0.6 * rk[0][1]
    keepf = conf >= HIGH and len(perfont[top1]) < PERFONT_CAP
    keepp = torn and len(pairbox[frozenset([top1, top2])]) < PAIR_CAP
    if not (keepf or keepp): continue
    snip = dict(text=r["text"], conf=round(float(conf), 2), top1=top1, top2=top2, img=b64(patch))
    if keepf: perfont[top1].append(snip)
    if keepp: pairbox[frozenset([top1, top2])].append(snip)
print(f"collected {sum(len(v) for v in perfont.values())} reference snippets, {sum(len(v) for v in pairbox.values())} confused (processed {nproc})", flush=True)

def exb64(key):
    p = f"{HERE}/{TAX.get(key,{}).get('exemplar','')}"
    if not os.path.exists(p): return ""
    im = Image.open(p).convert("L"); im.thumbnail((110, 60))
    bio = io.BytesIO(); im.save(bio, "PNG"); return base64.b64encode(bio.getvalue()).decode()

pairs = sorted(pairbox.items(), key=lambda kv: -len(kv[1]))[:12]
data = {
    "fonts": {f: sorted(perfont[f], key=lambda s: -s["conf"]) for f in sorted(perfont)},
    "meta": {f: {"style": f"{TAX.get(f,{}).get('base_style','')}{'CAPS' if TAX.get(f,{}).get('caps') else ''}",
                 "ex": exb64(f)} for f in perfont},
    "pairs": [{"a": sorted(p)[0], "b": sorted(p)[1], "exa": exb64(sorted(p)[0]), "exb": exb64(sorted(p)[1]),
               "snips": snips} for p, snips in pairs],
}

HTML = """<!doctype html><meta charset=utf-8><title>GB-STAMP — font assignment review</title>
<style>
 body{font-family:system-ui,sans-serif;margin:16px;background:#f6f3ec;color:#222}
 h1{font-size:19px} h2{font-size:15px;margin:18px 0 6px} .muted{color:#777;font-size:12px}
 .tabs{position:sticky;top:0;background:#f6f3ec;padding:6px 0;border-bottom:1px solid #ddd;z-index:5}
 .tabs button{font-size:14px;padding:6px 14px;margin-right:6px;cursor:pointer}
 .font{background:#fff;border:1px solid #dcd6c8;border-radius:8px;padding:8px 10px;margin:8px 0}
 .fh{display:flex;align-items:center;gap:10px;margin-bottom:5px}
 .fh b{font-size:14px} .fh img{height:34px;background:#fff;border:1px solid #eee}
 .snips{display:flex;flex-wrap:wrap;gap:8px}
 .s{border:1px solid #e6e0d2;border-radius:4px;padding:3px;background:#fff;text-align:center}
 .s img{display:block;height:38px;background:#fff} .s .t{font-size:10px;color:#555;max-width:120px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
 .s .c{font-size:9px;color:#999}
 .pair{background:#fff;border:1px solid #d9b3ae;border-radius:8px;padding:10px;margin:10px 0}
 .pair .hd{display:flex;align-items:center;gap:14px;margin-bottom:6px}
 .pair .hd .side{display:flex;align-items:center;gap:6px} .pair .hd img{height:40px;border:1px solid #eee}
 .pair .hd b{color:#a5322e}
 .hidden{display:none}
</style>
<h1>Font assignment review <span class=muted>— A: adjudicate the reference · B: the faces the system can't separate</span></h1>
<div class=tabs>
 <button onclick="show('A')">A · reference by font</button>
 <button onclick="show('B')">B · confused pairs</button>
 <span class=muted id=stat></span>
</div>
<div id=A></div><div id=B class=hidden></div>
<script>
const D=__DATA__;
function grid(snips){return '<div class=snips>'+snips.map(s=>`<div class=s><img src="data:image/png;base64,${s.img}"><div class=t title="${(s.text||'').replace(/"/g,'&quot;')}">${(s.text||'').replace(/</g,'&lt;')}</div><div class=c>${s.conf}${s.top2?' vs '+s.top2:''}</div></div>`).join('')+'</div>';}
function renderA(){
 let h=''; const fs=Object.keys(D.fonts).sort((a,b)=>D.fonts[b].length-D.fonts[a].length);
 for(const f of fs){const m=D.meta[f]||{};
  h+=`<div class=font><div class=fh>${m.ex?`<img src="data:image/png;base64,${m.ex}">`:''}<b>${f}</b> <span class=muted>${m.style||''} · ${D.fonts[f].length} snippets</span></div>${grid(D.fonts[f])}</div>`;}
 document.getElementById('A').innerHTML=h;
}
function renderB(){
 let h='';
 for(const p of D.pairs){
  h+=`<div class=pair><div class=hd><div class=side>${p.exa?`<img src="data:image/png;base64,${p.exa}">`:''}<b>${p.a}</b></div><span class=muted>vs</span><div class=side><b>${p.b}</b>${p.exb?`<img src="data:image/png;base64,${p.exb}">`:''}</div><span class=muted>· ${p.snips.length} torn snippets</span></div>${grid(p.snips)}</div>`;}
 document.getElementById('B').innerHTML=h;
}
function show(t){document.getElementById('A').classList.toggle('hidden',t!='A');document.getElementById('B').classList.toggle('hidden',t!='B');}
renderA();renderB();
document.getElementById('stat').textContent=Object.keys(D.fonts).length+' fonts · '+D.pairs.length+' confused pairs';
</script>"""
open(f"{OUT}/snippets.html", "w").write(HTML.replace("__DATA__", json.dumps(data)))
print(f"wrote {OUT}/snippets.html", flush=True)
