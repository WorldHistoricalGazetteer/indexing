"""Build the brush-painting UI for training a pixel classifier on map linework (the Ilastik workflow, in-repo).

Hand-written morphology has gone as far as it usefully can on this problem. Hatching is the case that defeats
it: a single hatch stroke is the same size, width and darkness as a letter stroke, so nothing local separates
them and the discriminating information is textural and contextual. That is exactly what a pixel classifier
over a multi-scale filter bank captures — and what a few painted brush strokes are enough to teach it.

Emits a self-contained HTML page holding N crops of a sheet at NATIVE resolution. The reviewer paints a handful
of strokes per class and exports one PNG label mask per crop; `rf_clean.py` trains on those pixels.

Crops are chosen to SPAN the problem rather than sample it evenly: the densest ink, the sparsest, and crops
centred on GB1900 pins so every class the classifier must separate — text especially — is present to paint.

    python make_paint_ui.py --tag sheet_ENG_218_NW --bbox W S E N --n 12 --size 512
"""
import argparse, base64, io, json, math, os, sys
import numpy as np, cv2
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hisam_pins import read_tile, N17
from build_pin_index import load_pins, pins_in_box
from sheet_clean import stitch, flat_field, lat_px

CLASSES = [
    ("paper", "#ffffff", "blank paper / background"),
    ("text", "#1e3cff", "lettering of any size or face"),
    ("line", "#00a000", "roads, contours, boundaries, railway casings, streams"),
    ("hatch", "#ff9800", "the parallel ruling that fills buildings"),
    ("solid", "#d02020", "solid black fill"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("W", "S", "E", "N"))
    ap.add_argument("--n", type=int, default=12, help="number of crops to paint")
    ap.add_argument("--size", type=int, default=512, help="crop side in px")
    ap.add_argument("--pins", default="/vast/ishi/gb1900/pins_z17.npz")
    ap.add_argument("--out", default="/vast/ishi/gb1900/edition/clean/paint_ui.html")
    ap.add_argument("--no-flatten", dest="flatten", action="store_false")
    a = ap.parse_args()

    w, s, e, n = a.bbox
    tx0 = int(((w + 180.0) / 360.0 * N17 * 256) // 256)
    tx1 = int(((e + 180.0) / 360.0 * N17 * 256) // 256)
    ty0, ty1 = int(lat_px(n) // 256), int(lat_px(s) // 256)
    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    rgb, hit = stitch(tx0, ty0, nx, ny)
    print(f"{a.tag}: {nx}x{ny} tiles ({hit} present)", flush=True)
    if a.flatten:
        rgb = flat_field(rgb)
    H, W = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    S = a.size
    # Ink density per candidate window, on a coarse grid.
    dens = cv2.blur((bw > 0).astype(np.float32), (S, S))
    step = S // 2
    cands = []
    for y in range(0, H - S, step):
        for x in range(0, W - S, step):
            cands.append((x, y, float(dens[y + S // 2, x + S // 2])))
    cands.sort(key=lambda c: -c[2])

    P = load_pins(a.pins)
    pin_idx = pins_in_box(P, tx0 * 256, ty0 * 256, (tx0 + nx) * 256, (ty0 + ny) * 256)

    picks = []
    # densest, sparsest-with-ink, and pin-centred — so every class is paintable somewhere
    picks += cands[: max(1, a.n // 3)]
    inked = [c for c in cands if c[2] > 0.02]
    picks += inked[-max(1, a.n // 3):]
    for k in pin_idx[:: max(1, len(pin_idx) // max(1, a.n - len(picks)))][: a.n - len(picks)]:
        x = int(float(P["gx"][k]) - tx0 * 256) - S // 2
        y = int(float(P["gy"][k]) - ty0 * 256) - S // 2
        picks.append((max(0, min(W - S, x)), max(0, min(H - S, y)), 0.0))

    seen, crops = set(), []
    for x, y, d in picks:
        key = (x // step, y // step)
        if key in seen:
            continue
        seen.add(key)
        patch = rgb[y:y + S, x:x + S]
        buf = io.BytesIO()
        Image.fromarray(patch).save(buf, "PNG")
        crops.append(dict(id=len(crops), gx=int(tx0 * 256 + x), gy=int(ty0 * 256 + y), size=S,
                          ink=round(d, 4),
                          img=base64.b64encode(buf.getvalue()).decode()))
        if len(crops) >= a.n:
            break
    print(f"  {len(crops)} crops of {S}px", flush=True)

    data = json.dumps(dict(tag=a.tag, classes=[dict(name=c[0], colour=c[1], hint=c[2]) for c in CLASSES],
                           crops=crops))
    html = HTML.replace("__DATA__", data)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    open(a.out, "w").write(html)
    print(f"wrote {a.out} ({os.path.getsize(a.out)/1e6:.1f} MB)\nPAINTUIDONE", flush=True)


HTML = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · paint linework classes</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee;color:#1a1a1a}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;z-index:9;
        display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 header b{font-size:15px}
 .cls{display:flex;gap:6px;align-items:center;padding:3px 9px;border-radius:5px;cursor:pointer;
      border:2px solid transparent;background:#3a3430}
 .cls.on{border-color:#fff}
 .sw{width:14px;height:14px;border-radius:3px;border:1px solid #0006}
 button{background:#2a7;color:#fff;border:0;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:13px}
 button.alt{background:#555}
 #wrap{padding:12px;display:flex;flex-wrap:wrap;gap:14px}
 .card{background:#fff;border:1px solid #ddd;border-radius:6px;padding:8px}
 .card h4{margin:0 0 6px;font-weight:600;font-size:12px;color:#555}
 .stack{position:relative;line-height:0;cursor:crosshair}
 .stack canvas{position:absolute;left:0;top:0}
 .stack img{position:relative;z-index:0}
 #hint{opacity:.85;font-size:12px}
</style>
<header>
  <b>GB-STAMP · paint linework classes</b>
  <span id=cls></span>
  <label>brush <input type=range id=brush min=2 max=40 value=10></label>
  <label><input type=checkbox id=erase> erase</label>
  <button onclick=undo()>Undo</button>
  <button class=alt onclick=clearAll()>Clear all</button>
  <button onclick=exportJSON()>Export labels</button>
  <span id=hint></span>
</header>
<div id=wrap></div>
<script>
const D=__DATA__;
let active=0, painting=false, strokes=[], lastPt=null;
const wrap=document.getElementById('wrap');

const clsBox=document.getElementById('cls');
D.classes.forEach((c,i)=>{
  const el=document.createElement('span'); el.className='cls'+(i==0?' on':''); el.dataset.i=i;
  el.innerHTML=`<span class=sw style="background:${c.colour}"></span>${c.name}`;
  el.onclick=()=>{active=i;[...clsBox.children].forEach(x=>x.classList.remove('on'));el.classList.add('on');
                  document.getElementById('hint').textContent=c.hint;};
  clsBox.appendChild(el);
});
document.getElementById('hint').textContent=D.classes[0].hint;

D.crops.forEach(cr=>{
  const card=document.createElement('div'); card.className='card';
  card.innerHTML=`<h4>#${cr.id} &nbsp; ${cr.gx},${cr.gy} &nbsp; ink ${cr.ink}</h4>`;
  const stack=document.createElement('div'); stack.className='stack';
  stack.style.width=cr.size+'px'; stack.style.height=cr.size+'px';
  const img=new Image(); img.src='data:image/png;base64,'+cr.img; img.width=cr.size; img.height=cr.size;
  const cv=document.createElement('canvas'); cv.width=cr.size; cv.height=cr.size;
  cv.style.opacity=0.55;
  stack.appendChild(img); stack.appendChild(cv); card.appendChild(stack); wrap.appendChild(card);
  cr._cv=cv; cr._ctx=cv.getContext('2d',{willReadFrequently:true});

  function pt(ev){const r=cv.getBoundingClientRect();return [ev.clientX-r.left, ev.clientY-r.top];}
  function draw(p,q){
    const ctx=cr._ctx, b=+document.getElementById('brush').value;
    ctx.globalCompositeOperation=document.getElementById('erase').checked?'destination-out':'source-over';
    ctx.strokeStyle=D.classes[active].colour; ctx.lineWidth=b; ctx.lineCap='round'; ctx.lineJoin='round';
    ctx.beginPath(); ctx.moveTo(p[0],p[1]); ctx.lineTo(q[0],q[1]); ctx.stroke();
  }
  cv.onmousedown=e=>{painting=true; lastPt=pt(e); strokes.push({cr:cr, snap:cr._cv.toDataURL()}); draw(lastPt,lastPt); e.preventDefault();};
  cv.onmousemove=e=>{if(!painting)return; const p=pt(e); draw(lastPt,p); lastPt=p;};
  window.addEventListener('mouseup',()=>{painting=false;});
});

function undo(){
  const s=strokes.pop(); if(!s)return;
  const im=new Image(); im.onload=()=>{s.cr._ctx.clearRect(0,0,s.cr.size,s.cr.size); s.cr._ctx.drawImage(im,0,0);};
  im.src=s.snap;
}
function clearAll(){ D.crops.forEach(c=>c._ctx.clearRect(0,0,c.size,c.size)); strokes=[]; }

function b64(u8){
  // Chunked: String.fromCharCode.apply blows the argument limit on a 512x512 = 262144-element mask, which
  // would make export fail on exactly the crops worth exporting.
  let s='', C=0x8000;
  for(let i=0;i<u8.length;i+=C) s+=String.fromCharCode.apply(null,u8.subarray(i,i+C));
  return btoa(s);
}
function exportJSON(){
  // Re-quantise the painted RGBA to exact class colours, so anti-aliased brush edges cannot invent a class.
  const pal=D.classes.map(c=>[parseInt(c.colour.slice(1,3),16),parseInt(c.colour.slice(3,5),16),parseInt(c.colour.slice(5,7),16)]);
  const out=[];
  D.crops.forEach(cr=>{
    const d=cr._ctx.getImageData(0,0,cr.size,cr.size), px=d.data;
    const lab=new Uint8Array(cr.size*cr.size); let any=0;
    for(let i=0;i<lab.length;i++){
      const a=px[i*4+3]; if(a<40){lab[i]=255;continue;}
      let best=0,bd=1e9;
      for(let k=0;k<pal.length;k++){
        const dr=px[i*4]-pal[k][0], dg=px[i*4+1]-pal[k][1], db=px[i*4+2]-pal[k][2];
        const dd=dr*dr+dg*dg+db*db; if(dd<bd){bd=dd;best=k;}
      }
      lab[i]=best; any++;
    }
    if(any) out.push({id:cr.id,gx:cr.gx,gy:cr.gy,size:cr.size,labels:b64(lab)});
  });
  if(!out.length){alert('Nothing painted yet.');return;}
  const blob=new Blob([JSON.stringify({tag:D.tag,classes:D.classes.map(c=>c.name),crops:out})],
                      {type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download='paint_labels_'+D.tag+'.json'; a.click();
}
</script>
"""

if __name__ == "__main__":
    main()
