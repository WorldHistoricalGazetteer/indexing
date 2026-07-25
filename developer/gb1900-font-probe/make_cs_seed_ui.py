"""Annotate letters on the Characteristic-Sheet specimens, to seed faces that have no map samples yet.

Ten of the seventeen inventory faces hold no glyphs at all, and several of those are the rare admin faces that
may take a very long time to encounter on real sheets. The CS engraves a specimen for every category, so it can
seed them directly — and a CS specimen is the *canonical* letterform, cleaner than any map crop will ever be,
which for template matching is an advantage rather than a compromise.

Each `reference/ex_*.jpg` is one CS category. Categories are mapped to inventory faces through the `os`
designations, so several specimens can seed one face — which is the point of the whole signature scheme. The
mapping is offered as a pre-selection, not imposed: every image carries a face dropdown, because a filename is
weaker evidence than a human eye.

Draw a box round each letter and type the character. Export writes the boxes; `extract_cs_glyphs.py` cuts them
to the same 44x36 normalisation as the map-derived library so the two are directly comparable.

    python make_cs_seed_ui.py --out admin_probe/cs_seed_ui.html
"""
import argparse, base64, glob, io, json, os, re
from PIL import Image

# filename stem -> the OS designation it depicts. The stems are terse, so this is written out rather than
# guessed; anything unlisted still appears in the UI with no pre-selected face.
STEM_OS = {
    "antiq_norman": "Antiquities (Norman)",
    "antiq_roman": "Antiquities (Roman)",
    "antiq_saxon": "Antiquities (Pre-historic or Saxon)",
    "antiq_subsequent": "Antiquities (post-Norman)",
    "bays_harbours": "Bays and Harbours", "bays_word": "Bays and Harbours",
    "harbours_word": "Bays and Harbours",
    "bogs_moors": "Bogs, Moors and Forests", "bogs_moors_word": "Bogs, Moors and Forests",
    "forests_word": "Bogs, Moors and Forests",
    "boroughs_munic": "Boroughs (Municipal)",
    "boroughs_parl": "Boroughs (Parliamentary)",
    "canals_word": "Navigable Rivers and Canals",
    "navigable_rivers": "Navigable Rivers and Canals",
    "navigable_rivers_word": "Navigable Rivers and Canals",
    "chapelries": "Chapelries. Other Churches",
    "cities_mp": "Cities returning Members",
    "cities_nomp": "Cities not returning Members",
    "civil_parishes": "Civil Parishes or Townships",
    "county_boroughs": "County Boroughs",
    "county_bridges": "County Bridges", "county_bridges_word": "County Bridges",
    "county_names": "County Names",
    "demesnes_word": "Parks and Demesnes", "parks_demesnes": "Parks and Demesnes",
    "parks_word": "Parks and Demesnes",
    "div_counties": "Divisions of Counties (Ridings)",
    "div_townships": "Divisions of Townships",
    "subdiv_townships": "Subdivisions of Townships",
    "extra_parochial": "Extra Parochial",
    "gentlemens_seats": "Gentlemens Seats",
    "hundreds": "Hundreds",
    "isolated_houses_word": "Isolated Houses",
    "liberties": "Liberties",
    "manufactories": "Manufactories. Mines. Farms. Locks.",
    "market_towns": "Market Towns",
    "other_stations": "Chapelries. Other Churches",
    "other_towns": "Other Towns",
    "other_villages": "Other Villages",
    "parish_churches": "Parish Churches, & Villages",
    "parishes_ancient": "Parishes (Mother or Ancient)",
    "parl_div_counties": "Parliamentary Divisions of Counties",
    "poor_law_unions": "Poor Law Unions",
    "principal_stations": "Railways (Passenger)",
    "railways_mineral": "Railways (Mineral)",
    "railways_passenger": "Railways (Passenger)",
    "ranges_hills": "Ranges of Hills",
    "small_rivers": "Small Rivers & Brooks",
    "town_districts": "Town Districts",
    "towns_generally": "Towns, generally",
    "trust_bridges_word": "Trust Bridges and Others",
    "turnpike_trusts": "Turnpike Trusts",
    "urban_sanitary": "Urban Sanitary Districts",
    "wards": "Wards",
    "woods_copses": "Woods and Copses",
    "workhouses": "Workhouses",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="reference")
    ap.add_argument("--inventory", default="labels/face_inventory.json")
    ap.add_argument("--out", default="admin_probe/cs_seed_ui.html")
    ap.add_argument("--min-side", type=int, default=24, help="skip crops too small to hold a letter")
    a = ap.parse_args()

    inv = json.load(open(a.inventory))["faces"]
    os_to_face = {}
    for face, v in inv.items():
        for des in (v.get("os") or "").split("|"):
            if des.strip():
                os_to_face[des.strip()] = face

    specs, unmapped = [], []
    for p in sorted(glob.glob(f"{a.dir}/ex_*.jpg")):
        stem = re.sub(r"^ex_", "", os.path.basename(p)[:-4])
        im = Image.open(p).convert("L")
        if min(im.size) < a.min_side:
            continue
        des = STEM_OS.get(stem, "")
        face = os_to_face.get(des, "")
        if not face:
            unmapped.append(stem)
        b = io.BytesIO()
        im.save(b, "PNG")
        specs.append(dict(stem=stem, w=im.width, h=im.height, os=des, face=face,
                          img=base64.b64encode(b.getvalue()).decode()))

    empty = [f for f in inv]
    print(f"{len(specs)} CS specimens, {len(specs)-len(unmapped)} mapped to a face")
    if unmapped:
        print(f"  no face for: {', '.join(sorted(unmapped))}")
    per_face = {}
    for s in specs:
        per_face.setdefault(s["face"] or "(unassigned)", []).append(s["stem"])
    for f in list(inv) + ["(unassigned)"]:
        n = len(per_face.get(f, []))
        print(f"  {f:28s} {n:>2d} specimen(s)" + ("" if n else "   << nothing to seed from"))

    data = json.dumps(dict(specs=specs, faces=list(inv), order=list(inv)))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "w").write(HTML.replace("__DATA__", data))
    print(f"\nwrote {a.out} ({os.path.getsize(a.out)/1e6:.2f} MB)")
    print("CSSEEDUIDONE")


HTML = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · seed letters from the Characteristic Sheet</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee;color:#1a1a1a}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;z-index:99;
        display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 header b{font-size:15px}
 button{background:#2a7;color:#fff;border:0;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:13px}
 button.alt{background:#555}
 #wrap{padding:12px}
 .sp{background:#fff;border:1px solid #ddd;border-radius:6px;margin-bottom:12px;padding:8px 10px}
 .sp h3{margin:0 0 6px;font-size:13px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .sp h3 .k{font-weight:700} .sp h3 .m{color:#777;font-weight:400;font-size:11px}
 .stack{position:relative;display:inline-block;line-height:0;cursor:crosshair;border:1px solid #eee}
 .stack canvas{position:absolute;left:0;top:0}
 .done{opacity:.55}
 .bx{font-size:11px;color:#333;margin-top:4px;display:flex;gap:6px;flex-wrap:wrap}
 .bx span{background:#eef;border:1px solid #ccd;border-radius:4px;padding:1px 5px;cursor:pointer}
 .bx span:hover{background:#fdd}
 select{font:12px system-ui;padding:3px}
 #hint{opacity:.85;font-size:12px}
</style>
<header>
 <b>GB-STAMP · seed letters from the Characteristic Sheet</b>
 <label>zoom <input type=range id=zoom min=1 max=8 value=4></label>
 <label><input type=checkbox id=hd onchange=render()> hide specimens already boxed</label>
 <button onclick=exportJSON()>Export letter boxes</button>
 <span id=hint>drag a box round ONE letter, then press its key. Click a chip to delete it.</span>
 <span id=stat></span>
</header>
<div id=wrap></div>
<script>
const D=__DATA__;
const boxes={};                 // stem -> [{x,y,w,h,ch}] in IMAGE pixels
const face={};                  // stem -> face (pre-selected from the OS designation, editable)
D.specs.forEach(s=>{ boxes[s.stem]=[]; face[s.stem]=s.face; });
let pend=null;                  // box awaiting its character

function Z(){ return +document.getElementById('zoom').value; }
function stat(){
  const n=Object.values(boxes).reduce((a,b)=>a+b.length,0);
  const f=new Set(Object.entries(boxes).filter(([k,v])=>v.length).map(([k])=>face[k]||'?'));
  document.getElementById('stat').textContent=`${n} letters boxed across ${f.size} face(s)`;
}
function render(){
  const hide=document.getElementById('hd').checked;
  document.getElementById('wrap').innerHTML = D.specs.filter(s=>!(hide&&boxes[s.stem].length)).map(s=>{
    const z=Z();
    return `<div class="sp${boxes[s.stem].length?' done':''}">
      <h3><span class=k>${s.stem}</span>
        <span class=m>${s.os||'(no OS designation)'} · ${s.w}&times;${s.h}</span>
        face: <select onchange="face['${s.stem}']=this.value">
          <option value=""${face[s.stem]?'':' selected'}>—</option>
          ${D.faces.map(f=>`<option${f==face[s.stem]?' selected':''}>${f}</option>`).join('')}
        </select></h3>
      <div class=stack id="st_${s.stem}" style="width:${s.w*z}px;height:${s.h*z}px">
        <img src="data:image/png;base64,${s.img}" width="${s.w*z}" height="${s.h*z}"
             style="image-rendering:pixelated">
        <canvas id="cv_${s.stem}" width="${s.w*z}" height="${s.h*z}"></canvas>
      </div>
      <div class=bx>${boxes[s.stem].map((b,i)=>
          `<span onclick="delBox('${s.stem}',${i})">${b.ch||'?'} &times;</span>`).join('')}</div>
    </div>`;
  }).join('');
  D.specs.forEach(s=>{ const c=document.getElementById('cv_'+s.stem); if(c) wire(s,c); });
  stat();
}
function draw(s,cv){
  const z=Z(), ctx=cv.getContext('2d');
  ctx.clearRect(0,0,cv.width,cv.height);
  ctx.lineWidth=2;
  boxes[s.stem].forEach(b=>{
    ctx.strokeStyle='#d02020'; ctx.strokeRect(b.x*z,b.y*z,b.w*z,b.h*z);
    if(b.ch){ ctx.fillStyle='#d02020'; ctx.font='bold 14px system-ui';
              ctx.fillText(b.ch,b.x*z+2,b.y*z-3); }
  });
}
function wire(s,cv){
  const z=Z(); let start=null;
  cv.onmousedown=e=>{ const r=cv.getBoundingClientRect();
    start=[(e.clientX-r.left)/z,(e.clientY-r.top)/z]; e.preventDefault(); };
  cv.onmousemove=e=>{ if(!start)return; const r=cv.getBoundingClientRect();
    const x=(e.clientX-r.left)/z, y=(e.clientY-r.top)/z;
    draw(s,cv); const ctx=cv.getContext('2d');
    ctx.strokeStyle='#2a7'; ctx.lineWidth=2;
    ctx.strokeRect(Math.min(start[0],x)*z,Math.min(start[1],y)*z,
                   Math.abs(x-start[0])*z,Math.abs(y-start[1])*z); };
  cv.onmouseup=e=>{ if(!start)return; const r=cv.getBoundingClientRect();
    const x=(e.clientX-r.left)/z, y=(e.clientY-r.top)/z;
    const b={x:Math.min(start[0],x),y:Math.min(start[1],y),
             w:Math.abs(x-start[0]),h:Math.abs(y-start[1]),ch:''};
    start=null;
    if(b.w<2||b.h<2) return;                    // a click, not a box
    boxes[s.stem].push(b); pend=[s.stem,boxes[s.stem].length-1];
    draw(s,cv); document.getElementById('hint').textContent='now press the letter key for that box';
  };
  draw(s,cv);
}
window.addEventListener('keydown',e=>{
  if(!pend) return;
  if(e.key==='Escape'){ const [st,i]=pend; boxes[st].splice(i,1); pend=null; render(); return; }
  if(e.key.length!==1) return;
  const [st,i]=pend; boxes[st][i].ch=e.key; pend=null;
  document.getElementById('hint').textContent='drag a box round ONE letter, then press its key.';
  render();
});
function delBox(stem,i){ boxes[stem].splice(i,1); render(); }
function exportJSON(){
  const out=[];
  D.specs.forEach(s=>{
    const bs=boxes[s.stem].filter(b=>b.ch);
    if(bs.length) out.push({stem:s.stem, face:face[s.stem]||'', os:s.os, boxes:bs});
  });
  if(!out.length){ alert('No letters boxed yet.'); return; }
  const bad=out.filter(o=>!o.face);
  if(bad.length && !confirm(`${bad.length} specimen(s) have no face assigned; export anyway?`)) return;
  const blob=new Blob([JSON.stringify({specimens:out},null,1)],{type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download='cs_letter_boxes.json'; a.click();
}
document.getElementById('zoom').oninput=render;
render();
</script>
"""

if __name__ == "__main__":
    main()
