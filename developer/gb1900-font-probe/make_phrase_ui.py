"""HITL UI to check/adjust the letter segmentation of the multi-letter Characteristic-Sheet specimens (the
word/phrase crops), the counterpart to the single-capital review. For each crop you see it enlarged (padded
so descenders aren't clipped) with its letter-boundary cuts overlaid; click to add a cut, click a cut to
remove, and correct the letters. Italic faces get SLANTED cuts with a per-font angle control so the
separators follow the letter slope. Export phrase_seeds.json = [{key, text, angle, cuts:[normalised x]}] which
build_alphabet_multi uses (shear by -angle, then cut) instead of the auto force_split.

    python make_phrase_ui.py   # -> phrase_ui.html  (open locally)
"""
import base64, json, os
HERE = os.path.dirname(os.path.abspath(__file__))
tax = json.load(open(f"{HERE}/font_taxonomy.json"))
FAILED = {"parish_churches", "manufactories", "trust_bridges_word", "isolated_houses_word", "antiq_saxon"}

cards = []
for x in tax:
    if not x.get("exemplar") or (x.get("seed_letters") or 0) < 2: continue
    p = f"{HERE}/{x['exemplar']}"
    if not os.path.exists(p): continue
    b64 = base64.b64encode(open(p, "rb").read()).decode()
    italic = "italic" in (x.get("base_style") or "")
    cards.append(dict(key=x["key"], label=x["label"], text=x.get("seed_text", ""),
                      style=f"{x['base_style']}{'CAPS' if x['caps'] else ''}", img=b64,
                      italic=italic, angle=(14 if italic else 0), failed=x["key"] in FAILED))

HTML = """<!doctype html><meta charset=utf-8><title>GB-STAMP — phrase seed segmentation</title>
<style>
 body{font-family:system-ui,sans-serif;margin:18px;background:#f6f3ec;color:#222}
 h1{font-size:19px} .muted{color:#777;font-size:13px}
 .bar{position:sticky;top:0;background:#f6f3ec;padding:8px 0;border-bottom:1px solid #ddd;z-index:5}
 button{font-size:14px;padding:6px 12px;margin-right:8px;cursor:pointer}
 .card{background:#fff;border:1px solid #dcd6c8;border-radius:8px;padding:12px 14px;margin:12px 0;box-shadow:0 1px 4px rgba(0,0,0,.06)}
 .card.fail{border-color:#c0392b;background:#fff6f5}
 .hd{display:flex;align-items:baseline;gap:10px;margin-bottom:6px}
 .hd b{font-size:15px} .badge{font-size:11px;color:#a5322e}
 canvas{border:1px solid #bbb;cursor:crosshair;display:block;background:#fff;max-width:100%}
 .row{display:flex;align-items:center;gap:10px;margin-top:8px;flex-wrap:wrap}
 input[type=text]{font-size:15px;padding:4px 8px;font-family:ui-monospace,monospace;width:340px}
 input[type=range]{width:150px} .ang{font-variant-numeric:tabular-nums;width:38px;display:inline-block}
 .seg{font-variant-numeric:tabular-nums} .ok{color:#2a7d2a} .bad{color:#c0392b}
</style>
<h1>Phrase seed segmentation <span class=muted>— click to add a letter cut · click a cut to remove · italic faces get a slant control</span></h1>
<div class=bar>
 <button onclick=save()>Save</button><button onclick=dl()>Download phrase_seeds.json</button>
 <span class=muted id=prog></span>
</div>
<div id=grid></div>
<script>
const DATA=__DATA__;
let S=JSON.parse(localStorage.getItem("gbstamp_phrase")||"{}");
const SCALE=3, HIT=7, PADF=0.35;
function letters(t){return (t||"").split("").filter(c=>/[A-Za-z0-9]/.test(c));}
function st(d){if(!S[d.key])S[d.key]={cuts:[],text:d.text,angle:d.angle};if(S[d.key].angle==null)S[d.key].angle=d.angle;return S[d.key];}
function geom(cv,d){const padTop=d._ih*SCALE*PADF;return {W:cv.width,padTop,yc:padTop+d._ih*SCALE/2,tan:Math.tan((st(d).angle||0)*Math.PI/180)};}
function xAt(cv,d,xnorm,y){const g=geom(cv,d);return xnorm*g.W+g.tan*(g.yc-y);}
function draw(cv,d){
 const ctx=cv.getContext("2d");ctx.clearRect(0,0,cv.width,cv.height);
 const g=geom(cv,d);ctx.drawImage(d._img,0,g.padTop,cv.width,d._ih*SCALE);
 ctx.strokeStyle="#d33";ctx.lineWidth=1.5;
 (st(d).cuts||[]).forEach(x=>{ctx.beginPath();ctx.moveTo(xAt(cv,d,x,0),0);ctx.lineTo(xAt(cv,d,x,cv.height),cv.height);ctx.stroke();});
}
function segInfo(d){const s=st(d);return {nseg:(s.cuts||[]).length+1,nl:letters(s.text).length};}
function prog(){let done=0;DATA.forEach(d=>{const s=segInfo(d);if(s.nseg===s.nl&&(st(d).cuts||[]).length>0)done++;});
 document.getElementById("prog").textContent=done+" / "+DATA.length+" fonts segmented";}
function save(){localStorage.setItem("gbstamp_phrase",JSON.stringify(S));prog();}
function render(){
 const g=document.getElementById("grid");g.innerHTML="";
 DATA.forEach(d=>{
  const card=document.createElement("div");card.className="card"+(d.failed?" fail":"");
  const img=new Image();d._img=img;
  card.innerHTML=`<div class=hd><b>${d.key}</b> <span class=muted>${d.label}</span> <span class=badge>${d.style}</span>${d.failed?' <span class=bad>auto-split FAILED</span>':''}</div>`;
  const cv=document.createElement("canvas");card.appendChild(cv);
  const row=document.createElement("div");row.className="row";
  const inp=document.createElement("input");inp.type="text";inp.value=st(d).text;
  const seg=document.createElement("span");seg.className="seg";
  const slant=document.createElement("span");
  slant.innerHTML=`slant <input type=range min=-30 max=30 value=${st(d).angle}> <span class=ang>${st(d).angle}\\u00b0</span>`;
  const rng=slant.querySelector("input"),angv=slant.querySelector(".ang");
  const rst=document.createElement("button");rst.textContent="clear cuts";
  row.append(Object.assign(document.createElement("span"),{textContent:"letters:"}),inp,seg,slant,rst);
  card.appendChild(row);g.appendChild(card);
  function upd(){const s=segInfo(d);seg.innerHTML=`segments <b class=${s.nseg===s.nl?'ok':'bad'}>${s.nseg}</b> vs letters <b>${s.nl}</b>`;prog();}
  img.onload=()=>{
   d._ih=img.naturalHeight;cv.width=img.naturalWidth*SCALE;cv.height=img.naturalHeight*SCALE*(1+2*PADF);
   if(!st(d).cuts||!st(d).cuts.length){const nl=letters(inp.value).length,c=[];for(let i=1;i<nl;i++)c.push(i/nl);st(d).cuts=c;}
   draw(cv,d);upd();
  };
  img.src="data:image/jpeg;base64,"+d.img;
  cv.addEventListener("click",e=>{
   const r=cv.getBoundingClientRect(),sx=cv.width/r.width,sy=cv.height/r.height;
   const cx=(e.clientX-r.left)*sx,cy=(e.clientY-r.top)*sy;const cuts=st(d).cuts;
   const near=cuts.findIndex(x=>Math.abs(cx-xAt(cv,d,x,cy))<HIT*2);
   if(near>=0)cuts.splice(near,1);
   else{const gg=geom(cv,d);cuts.push((cx-gg.tan*(gg.yc-cy))/gg.W);}
   cuts.sort((a,b)=>a-b);st(d).cuts=cuts;draw(cv,d);upd();save();
  });
  inp.addEventListener("input",()=>{st(d).text=inp.value;upd();save();});
  rng.addEventListener("input",()=>{st(d).angle=+rng.value;angv.textContent=rng.value+"\\u00b0";draw(cv,d);save();});
  rst.addEventListener("click",()=>{st(d).cuts=[];draw(cv,d);upd();save();});
 });
 prog();
}
function dl(){
 const out=DATA.map(d=>({key:d.key,text:st(d).text,angle:st(d).angle||0,
   cuts:(st(d).cuts||[]).map(x=>+x.toFixed(4))}));
 const b=new Blob([JSON.stringify(out,null,1)],{type:"application/json"});
 const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="phrase_seeds.json";a.click();
}
render();
</script>"""
out = f"{HERE}/phrase_ui.html"
open(out, "w").write(HTML.replace("__DATA__", json.dumps(cards)))
print(f"wrote {out} with {len(cards)} phrase crops ({sum(c['italic'] for c in cards)} italic w/ slant control)")
