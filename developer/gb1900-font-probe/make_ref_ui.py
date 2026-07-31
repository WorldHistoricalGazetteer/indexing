"""Inspection UI for the font reference. For every face — the SINGLE-CHARACTER-reference admin marks first —
show side by side: the Characteristic-Sheet exemplar, the INITIAL SPOTS (the real labels the seed step
assigned via the single mark-letter, with which letter matched and its score), and the FANNED alphabet
(labels the co-occurrence fan later assigned). Lets a human judge whether the single-letter seed actually
found that face or just grabbed same-letter words.  Reads assignments.jsonl (from build_alphabet_multi
DUMP_ASSIGN=1).  ->  ref_ui.html
"""
import base64, io, json, os
from collections import defaultdict
from PIL import Image

HERE = "/vast/ishi/gb1900/probe/font"; OUT = "/vast/ishi/gb1900/edition/discover"
TAX = {x["key"]: x for x in json.load(open(f"{HERE}/font_taxonomy.json"))}

def exb64(key):
    p = f"{HERE}/{TAX.get(key, {}).get('exemplar', '')}"
    if not os.path.exists(p): return ""
    im = Image.open(p).convert("L"); im.thumbnail((150, 90))
    bio = io.BytesIO(); im.save(bio, "PNG"); return base64.b64encode(bio.getvalue()).decode()

# Split gen-0 seeds by PROVENANCE: name-lookup seeds (genuine admin names, no mark-letter) vs mark-letter
# spots (a single reference glyph matched). Conflating them under one "mark-letter" header is misleading —
# the lookup words legitimately contain no mark-letter.
lookup = defaultdict(list); mark = defaultdict(list); fanned = defaultdict(list)
for line in open(f"{OUT}/assignments.jsonl"):
    a = json.loads(line)
    if a["gen"] != 0: fanned[a["font"]].append(a)
    elif a.get("seed_letter") == "lookup": lookup[a["font"]].append(a)
    else: mark[a["font"]].append(a)
for f in mark: mark[f].sort(key=lambda a: -(a.get("seed_score") or 0))

fonts = sorted(set(lookup) | set(mark) | set(fanned),
               key=lambda f: (0 if (TAX.get(f, {}).get("seed_letters") == 1) else 1, f))
data = [{"font": f, "style": f"{TAX.get(f,{}).get('base_style','')}{'CAPS' if TAX.get(f,{}).get('caps') else ''}",
         "single": TAX.get(f, {}).get("seed_letters") == 1, "ex": exb64(f),
         "lookup": lookup.get(f, [])[:60], "mark": mark.get(f, [])[:60], "fan": fanned.get(f, [])[:60],
         "nlook": len(lookup.get(f, [])), "nmark": len(mark.get(f, [])), "nfan": len(fanned.get(f, []))}
        for f in fonts]

HTML = """<!doctype html><meta charset=utf-8><title>GB-STAMP — font reference inspection</title>
<style>
 body{font-family:system-ui,sans-serif;margin:16px;background:#f6f3ec;color:#222}
 h1{font-size:19px} .muted{color:#777;font-size:12px}
 .bar{position:sticky;top:0;background:#f6f3ec;padding:6px 0;border-bottom:1px solid #ddd;z-index:5}
 .bar label{font-size:13px;margin-right:12px}
 .font{background:#fff;border:1px solid #dcd6c8;border-radius:8px;padding:10px 12px;margin:10px 0}
 .font.single{border-color:#c0392b}
 .fh{display:flex;align-items:center;gap:12px;margin-bottom:6px}
 .fh b{font-size:15px} .fh img{height:52px;border:1px solid #eee;background:#fff}
 .tag{font-size:11px;color:#a5322e;border:1px solid #e2b6b0;border-radius:10px;padding:1px 7px}
 .sec{font-size:12px;color:#555;margin:6px 0 3px;font-weight:600}
 .snips{display:flex;flex-wrap:wrap;gap:10px}
 .s{border:1px solid #e6e0d2;border-radius:4px;padding:4px;background:#fff;text-align:center}
 .s img{display:block;height:60px;background:#fff;cursor:zoom-in}
 .s .t{font-size:12px;color:#333;max-width:200px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
 .s .c{font-size:10px;color:#999}
 #lb{display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:100;align-items:center;justify-content:center;cursor:zoom-out}
 #lb img{max-width:94vw;max-height:86vh;background:#fff;padding:10px}
 .empty{color:#c0392b;font-size:12px}
</style>
<h1>Font reference inspection <span class=muted>— single-character-reference faces first (red) · click any crop to enlarge</span></h1>
<div class=bar><label><input type=checkbox id=only> single-character faces only</label>
 <span class=muted id=stat></span></div>
<div id=grid></div>
<div id=lb onclick="this.style.display='none'"><img id=lbi></div>
<script>
const D=__DATA__;
document.body.addEventListener('click',e=>{if(e.target.tagName==='IMG'&&e.target.closest('.s')){document.getElementById('lbi').src=e.target.src;document.getElementById('lb').style.display='flex';}});
function grid(arr,kind){
 if(!arr.length) return '<div class=empty>none</div>';
 return '<div class=snips>'+arr.map(a=>{
   let c = kind==='mark' ? ('spot '+a.seed_letter+' @'+(a.seed_score||0)) : kind==='lookup' ? 'name-lookup' : ('gen'+a.gen);
   if(a.cap_h) c += ' · '+Math.round(a.cap_h)+'px';
   return `<div class=s><img src="data:image/png;base64,${a.crop}"><div class=t title="${(a.text||'').replace(/"/g,'&quot;')}">${(a.text||'').replace(/</g,'&lt;')}</div><div class=c>${c}</div></div>`;
 }).join('')+'</div>';
}
function render(){
 const only=document.getElementById('only').checked;
 const g=document.getElementById('grid');g.innerHTML='';
 for(const d of D){ if(only&&!d.single) continue;
  const el=document.createElement('div');el.className='font'+(d.single?' single':'');
  el.innerHTML=`<div class=fh>${d.ex?`<img src="data:image/png;base64,${d.ex}">`:''}<b>${d.font}</b> <span class=muted>${d.style}</span>${d.single?' <span class=tag>single-char seed</span>':''} <span class=muted>· ${d.nlook} name-lookup, ${d.nmark} mark-letter, ${d.nfan} fanned</span></div>`+
   `<div class=sec>NAME-LOOKUP SEEDS (genuine admin names — no mark-letter):</div>${grid(d.lookup,'lookup')}`+
   `<div class=sec>MARK-LETTER SPOTS (seeded by the single mark-letter):</div>${grid(d.mark,'mark')}`+
   `<div class=sec>FANNED (co-occurrence):</div>${grid(d.fan,'fan')}`;
  g.appendChild(el);
 }
}
document.getElementById('only').addEventListener('change',render);
render();
document.getElementById('stat').textContent=D.length+' faces · '+D.filter(d=>d.single).length+' single-character';
</script>"""
open(f"{OUT}/ref_ui.html", "w").write(HTML.replace("__DATA__", json.dumps(data)))
print(f"wrote {OUT}/ref_ui.html ({len(data)} faces, {sum(d['nlook'] for d in data)} name-lookup, "
      f"{sum(d['nmark'] for d in data)} mark-letter, {sum(d['nfan'] for d in data)} fanned)")
