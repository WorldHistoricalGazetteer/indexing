"""HITL UI to check/adjust the letter segmentation of the multi-letter Characteristic-Sheet specimens (the
word/phrase crops), the counterpart to the single-capital review. For each crop you see it enlarged with its
letter-boundary cuts overlaid; click to add a cut, click a cut to remove it, and correct the letters. Export
phrase_seeds.json = [{key, text, cuts:[normalised x]}] which build_alphabet_multi uses instead of the
auto force_split for those fonts.

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
    letters = "".join(c for c in (x.get("seed_text") or "") if c.isalnum())
    cards.append(dict(key=x["key"], label=x["label"], text=x.get("seed_text", ""), nlet=len(letters),
                      style=f"{x['base_style']}{'CAPS' if x['caps'] else ''}", img=b64,
                      failed=x["key"] in FAILED))

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
 .seg{font-variant-numeric:tabular-nums} .ok{color:#2a7d2a} .bad{color:#c0392b}
</style>
<h1>Phrase seed segmentation <span class=muted>— click to add a letter cut · click a cut to remove · drag to nudge</span></h1>
<div class=bar>
 <button onclick=save()>Save</button><button onclick=dl()>Download phrase_seeds.json</button>
 <span class=muted id=prog></span>
</div>
<div id=grid></div>
<script>
const DATA=__DATA__;
let S=JSON.parse(localStorage.getItem("gbstamp_phrase")||"{}");
const SCALE=3, HIT=6;
function letters(t){return (t||"").split("").filter(c=>/[A-Za-z0-9]/.test(c));}
function draw(cv,d){
 const ctx=cv.getContext("2d"); ctx.clearRect(0,0,cv.width,cv.height);
 ctx.drawImage(d._img,0,0,cv.width,cv.height);
 const cuts=(S[d.key]&&S[d.key].cuts)||[];
 ctx.strokeStyle="#d33"; ctx.lineWidth=1.5;
 cuts.forEach(x=>{const px=x*cv.width;ctx.beginPath();ctx.moveTo(px,0);ctx.lineTo(px,cv.height);ctx.stroke();});
}
function segInfo(d){
 const cuts=(S[d.key]&&S[d.key].cuts)||[]; const txt=(S[d.key]&&S[d.key].text!=null)?S[d.key].text:d.text;
 const nseg=cuts.length+1, nl=letters(txt).length;
 return {nseg,nl,ok:nseg===nl};
}
function prog(){
 let done=0; DATA.forEach(d=>{if(segInfo(d).ok&&((S[d.key]&&S[d.key].cuts)||[]).length>0)done++;});
 document.getElementById("prog").textContent=done+" / "+DATA.length+" fonts segmented";
}
function save(){localStorage.setItem("gbstamp_phrase",JSON.stringify(S));prog();}
function render(){
 const g=document.getElementById("grid");g.innerHTML="";
 DATA.forEach(d=>{
  const card=document.createElement("div");card.className="card"+(d.failed?" fail":"");
  const img=new Image();d._img=img;
  card.innerHTML=`<div class=hd><b>${d.key}</b> <span class=muted>${d.label}</span> <span class=badge>${d.style}</span>${d.failed?' <span class=bad>auto-split FAILED</span>':''}</div>`;
  const cv=document.createElement("canvas");card.appendChild(cv);
  const row=document.createElement("div");row.className="row";
  const inp=document.createElement("input");inp.type="text";
  inp.value=(S[d.key]&&S[d.key].text!=null)?S[d.key].text:d.text;
  const seg=document.createElement("span");seg.className="seg";
  const rst=document.createElement("button");rst.textContent="clear cuts";
  row.appendChild(document.createTextNode("letters:"));row.appendChild(inp);row.appendChild(seg);row.appendChild(rst);
  card.appendChild(row);g.appendChild(card);
  img.onload=()=>{
   cv.width=img.naturalWidth*SCALE; cv.height=img.naturalHeight*SCALE;
   if(!S[d.key])S[d.key]={cuts:[],text:d.text};
   // seed with even cuts if none yet
   if(!S[d.key].cuts||!S[d.key].cuts.length){
     const nl=letters(inp.value).length; const c=[];
     for(let i=1;i<nl;i++)c.push(i/nl); S[d.key].cuts=c;
   }
   draw(cv,d); upd();
  };
  img.src="data:image/jpeg;base64,"+d.img;
  function upd(){const s=segInfo(d);seg.innerHTML=`segments <b class=${s.ok?'ok':'bad'}>${s.nseg}</b> vs letters <b>${s.nl}</b>`;prog();}
  cv.addEventListener("click",e=>{
   const r=cv.getBoundingClientRect();const x=(e.clientX-r.left)/r.width;
   const cuts=S[d.key].cuts;const near=cuts.findIndex(c=>Math.abs(c-x)*cv.width<HIT*2);
   if(near>=0)cuts.splice(near,1);else cuts.push(x);cuts.sort((a,b)=>a-b);
   S[d.key].cuts=cuts;draw(cv,d);upd();save();
  });
  inp.addEventListener("input",()=>{S[d.key]=S[d.key]||{cuts:[]};S[d.key].text=inp.value;upd();save();});
  rst.addEventListener("click",()=>{S[d.key].cuts=[];draw(cv,d);upd();save();});
 });
 prog();
}
function dl(){
 const out=DATA.map(d=>({key:d.key,text:(S[d.key]&&S[d.key].text!=null)?S[d.key].text:d.text,
   cuts:((S[d.key]&&S[d.key].cuts)||[]).map(x=>+x.toFixed(4))}));
 const b=new Blob([JSON.stringify(out,null,1)],{type:"application/json"});
 const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="phrase_seeds.json";a.click();
}
render();
</script>"""
out = f"{HERE}/phrase_ui.html"
open(out, "w").write(HTML.replace("__DATA__", json.dumps(cards)))
print(f"wrote {out} with {len(cards)} phrase crops ({sum(c['failed'] for c in cards)} flagged as auto-split failures)")
