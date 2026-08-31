"""GB-STAMP — interactive per-LETTER alphabet builder. Surfaces gazetteer-confirmed admin labels (harvest) and
word-content-hinted descriptive labels as map snippets; the reviewer selects a row (click or draw), assigns a
FACE from the scrolling full-taxonomy column on the right, CORRECTS the transcription if the spotter got it
wrong, and draws a box round each letter. Exports per-letter glyph labels -> alphabet_labels.json. This breaks
the CNN scarcity ceiling (each word -> ~6 clean per-letter examples) with human segmentation on the actual
44-face taxonomy that has had no direct real-crop supervision.

    /vast/ishi/envs/boundary/bin/python make_alphabet_ui.py --n-admin 90 --n-desc-per 8
Output: /vast/ishi/gb1900/edition/admin/alphabet_ui.html  (annotate in browser -> Download alphabet_labels.json)
"""
import argparse, os, io, re, json, math, time, glob, base64, random, urllib.request
import numpy as np, cv2
from PIL import Image

HERE = "/vast/ishi/gb1900/probe/font"; ADMIN = "/vast/ishi/gb1900/edition/admin"
SPOT = "/vast/ishi/gb1900/edition/spot"
N17 = 2 ** 17; TILES = "/vast/ishi/gb1900/tiles17"; IX1 = "/ix1/ishi/gb1900/tiles17"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"

# Word-content HINTS -> likely DESCRIPTIVE face (valid font_taxonomy keys). Only a suggestion; reviewer confirms.
LEX = [
    (re.compile(r"\b(Canal|Navigation)\b", re.I), "Navigable Rivers and Canals"),
    (re.compile(r"\b(River|Brook|Burn|Beck|Nant|Afon|Stream|Well|Pool|Mere|Lake|Ford|Water|Dyke)\b", re.I), "small_rivers"),
    (re.compile(r"\bRoman\b", re.I), "antiq_roman"),
    (re.compile(r"\b(Tumul|Cairn|Barrow|Earthwork|Camp|Stone|Cross|Site of)\b", re.I), "antiq_saxon"),
    (re.compile(r"\b(Castle|Priory|Abbey|Moat|Tower)\b", re.I), "antiq_subsequent"),
    (re.compile(r"\b(Station|Sta\.|Junction)\b", re.I), "other_stations"),
    (re.compile(r"\b(Church|Chapel|\bCh\b)\b", re.I), "parish_churches"),
    (re.compile(r"\b(Wood|Copse|Plantation|Covert|Spinney)\b", re.I), "woods_copses"),
    (re.compile(r"\b(Hall|Lodge|House|Grange|Court|Manor)\b", re.I), "gentlemens_seats"),
    (re.compile(r"\b(Mill|Works|Factory|Colliery|Foundry|Quarry|Pit)\b", re.I), "manufactories"),
    (re.compile(r"\b(Bog|Moor|Common|Marsh|Fen|Heath|Forest)\b", re.I), "Bogs, Moors and Forests"),
    (re.compile(r"\b(Hill|Down|Fell|Tor|Ridge|Beacon)\b", re.I), "ranges_hills"),
]

def get_tile(tx, ty):
    for base in (TILES, IX1):
        p = f"{base}/{tx}/{ty}.png"
        if os.path.exists(p) and os.path.getsize(p) > 500:
            try: return np.asarray(Image.open(p).convert("L"), np.uint8)
            except Exception: pass
    os.makedirs(f"{TILES}/{tx}", exist_ok=True)
    for k in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-alpha"}), timeout=30) as r:
                d = r.read()
            if len(d) > 400:
                open(f"{TILES}/{tx}/{ty}.png", "wb").write(d); return np.asarray(Image.open(io.BytesIO(d)).convert("L"), np.uint8)
            return None
        except Exception as e:
            if getattr(e, "code", None) in (403, 404): return None
            time.sleep(1.0 * (k + 1))
    return None

def snippet(x0, y0, x1, y1, pad_frac=0.6):
    h = y1 - y0; padx = int((x1 - x0) * 0.15) + 20; pady = int(h * pad_frac) + 15
    X0, Y0, X1, Y1 = int(x0 - padx), int(y0 - pady), int(x1 + padx), int(y1 + pady)
    tx0, ty0, tx1, ty1 = X0 // 256, Y0 // 256, X1 // 256, Y1 // 256
    canvas = np.full(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), 255, np.uint8)
    for i in range(tx1 - tx0 + 1):
        for j in range(ty1 - ty0 + 1):
            t = get_tile(tx0 + i, ty0 + j)
            if t is not None: canvas[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = t
    cx0, cy0 = tx0 * 256, ty0 * 256
    sub = canvas[Y0 - cy0:Y1 - cy0, X0 - cx0:X1 - cx0]
    return sub, (x0 - X0, y0 - Y0, x1 - X0, y1 - Y0)

def make_card(name, x0, y0, x1, y1, suggest, meta):
    sub, (lx0, ly0, lx1, ly1) = snippet(x0, y0, x1, y1)
    if sub.size == 0 or sub.shape[0] < 8: return None
    im = Image.fromarray(sub); scale = min(1.0, 900 / im.width)
    if scale < 1.0: im = im.resize((int(im.width * scale), int(im.height * scale)))
    bio = io.BytesIO(); im.convert("L").save(bio, "PNG")
    return dict(name=name, suggest=suggest, scale=scale,
                guide=[lx0 * scale, ly0 * scale, lx1 * scale, ly1 * scale],
                img=base64.b64encode(bio.getvalue()).decode(), **meta)

def admin_candidates(harvest, n):
    hits = [json.loads(l) for l in open(harvest)]
    hits.sort(key=lambda h: (h.get("nfrag", 1) >= 2, h.get("cap_h", 0)), reverse=True)
    for h in hits[:n]:
        c = make_card(h["name"], h["x0"], h["y0"], h["x1"], h["y1"], (h.get("faces") or [None])[0],
                      dict(cap_h=h.get("cap_h"), nfrag=h.get("nfrag", 1), tag=h["tag"], group="admin"))
        if c: yield c

def descriptive_candidates(n_per):
    picked = {}
    files = sorted(glob.glob(f"{SPOT}/boxes_gb_*.jsonl")); random.Random(7).shuffle(files)
    for f in files:
        if picked and min(len(v) for v in picked.values()) >= n_per and len(picked) >= len(LEX): break
        tag = os.path.basename(f)[6:-6]
        for line in open(f):
            try: r = json.loads(line)
            except Exception: continue
            t = r.get("text", "") or ""
            if r.get("score", 0) < 0.6 or not r.get("gpoly") or len([c for c in t if c.isalnum()]) < 4: continue
            face = next((fc for rx, fc in LEX if rx.search(t)), None)
            if not face or len(picked.get(face, [])) >= n_per: continue
            xs = [p[0] for p in r["gpoly"]]; ys = [p[1] for p in r["gpoly"]]
            c = make_card(t, min(xs), min(ys), max(xs), max(ys), face,
                          dict(cap_h=int(max(ys) - min(ys)), nfrag=1, tag=tag, group="descriptive"))
            if c: picked.setdefault(face, []).append(c)
    for cards in picked.values():
        yield from cards

ROADRX = re.compile(r"\b(STREET|ROAD|LANE|AVENUE|TERRACE|CRESCENT|PARADE|ROW|PLACE|SQUARE|GARDENS|WHARF|QUAY|COURT|WALK|GROVE|DRIVE|CLOSE|BUILDINGS|COTTAGES|VILLAS)\b", re.I)
def _allcaps(t):
    a = [c for c in t if c.isalpha()]; return bool(a) and all(c.isupper() for c in a)

def _tag_lat():
    lat = {}
    for src in (f"{HERE}/centres_all.txt", f"{HERE}/centres_repr.txt"):
        if os.path.exists(src):
            for l in open(src):
                p = l.split()
                if len(p) >= 3: lat.setdefault(p[2], float(p[1]))
    return lat

def large_candidates(minh, n, maxh=300.0, maxlat=56.0):
    # the big single-letter-exemplar admin/town faces ARE the largest ALLCAPS labels; surface them directly (no
    # gazetteer). Restrict to England/Wales/S-Scotland (lat<56 — the far-north giants are SOUND/VOE water names),
    # to admin cap-height scale (<=300 excludes huge water labels), and drop OCR garbage (#-heavy fragments).
    lat = _tag_lat(); rows = []
    for f in glob.glob(f"{SPOT}/boxes_gb_*.jsonl"):
        tag = os.path.basename(f)[6:-6]
        if lat.get(tag, 0) >= maxlat: continue              # skip far-north (water-feature dominated)
        for line in open(f):
            try: r = json.loads(line)
            except Exception: continue
            t = r.get("text", "") or ""
            alnum = [c for c in t if c.isalnum()]
            if r.get("score", 0) < 0.55 or not r.get("gpoly") or len(alnum) < 3: continue
            if len(alnum) / max(1, len(t.replace(" ", ""))) < 0.7: continue   # drop #-heavy garbage
            if not _allcaps(t) or ROADRX.search(t): continue
            xs = [p[0] for p in r["gpoly"]]; ys = [p[1] for p in r["gpoly"]]
            (_, (bw, bh), _) = cv2.minAreaRect(np.array(r["gpoly"], np.float32).reshape(-1, 1, 2))
            caph = min(bw, bh)                               # TRUE cap height (oriented), not axis-aligned bbox height
            aaw, aah = max(xs) - min(xs), max(ys) - min(ys)
            if not (minh <= caph <= maxh): continue
            if aaw < 1.3 * aah: continue                    # horizontal label -> legible snippet (excludes diagonal slivers)
            rows.append((caph, t, min(xs), min(ys), max(xs), max(ys), tag))
    rows.sort(reverse=True)                                  # largest cap height first
    seen = set(); out = []
    for h, t, x0, y0, x1, y1, tag in rows:
        key = (t.upper(), round((x0 + x1) / 2 / 300), round((y0 + y1) / 2 / 300))
        if key in seen: continue
        seen.add(key)
        c = make_card(t, x0, y0, x1, y1, None, dict(cap_h=int(h), nfrag=1, tag=tag, group="large"))
        if c: out.append(c)
        if len(out) >= n: break
    return out

def numeral_candidates(n):
    # contour altitudes / spot heights / bench marks — the numeral·solid·plain signature (0 candidates otherwise).
    lat = _tag_lat(); out = []; files = sorted(glob.glob(f"{SPOT}/boxes_gb_*.jsonl")); random.Random(3).shuffle(files)
    for f in files:
        if len(out) >= n: break
        tag = os.path.basename(f)[6:-6]
        if lat.get(tag, 99) >= 56: continue
        for line in open(f):
            try: r = json.loads(line)
            except Exception: continue
            t = r.get("text", "") or ""; dig = [c for c in t if c.isdigit()]; an = [c for c in t if c.isalnum()]
            if r.get("score", 0) < 0.6 or not r.get("gpoly") or len(dig) < 2 or len(dig) / max(1, len(an)) < 0.8: continue
            xs = [p[0] for p in r["gpoly"]]; ys = [p[1] for p in r["gpoly"]]
            c = make_card(t, min(xs), min(ys), max(xs), max(ys), "contour_numeral",
                          dict(cap_h=int(max(ys) - min(ys)), nfrag=1, tag=tag, group="numeral"))
            if c: out.append(c)
            if len(out) >= n: break
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-admin", type=int, default=90); ap.add_argument("--n-desc-per", type=int, default=8)
    ap.add_argument("--n-large", type=int, default=300); ap.add_argument("--large-minh", type=float, default=42)
    ap.add_argument("--n-num", type=int, default=24)
    ap.add_argument("--harvest", default=f"{ADMIN}/harvest.jsonl")
    a = ap.parse_args()
    def exb64(path):                                         # face exemplar specimen -> b64 (shown in the picker)
        if not path: return None
        p = path if os.path.isabs(path) else os.path.join(HERE, path)
        if not os.path.exists(p): return None
        im = Image.open(p).convert("L"); im.thumbnail((280, 52))
        bio = io.BytesIO(); im.save(bio, "PNG"); return base64.b64encode(bio.getvalue()).decode()
    tax = json.load(open(f"{HERE}/font_taxonomy.json"))
    faces = [{"key": f["key"], "label": f["label"], "caps": bool(f.get("caps")), "style": f["base_style"],
              "fill": f.get("fill"), "decor": f.get("decor"), "ex": exb64(f.get("exemplar")),
              "sig": "·".join(str(x) for x in (f["base_style"], f.get("fill"), f.get("decor")))}   # typographic signature
             for f in tax]   # ALL 48 faces; faces sharing a sig are the same font (classifier target)
    # ORDER: admin + descriptive (unchanged from before) THEN large appended, so existing localStorage
    # annotations (keyed by candidate id) stay valid — the new large candidates just get fresh trailing ids.
    data = (list(admin_candidates(a.harvest, a.n_admin)) + list(descriptive_candidates(a.n_desc_per))
            + list(large_candidates(a.large_minh, a.n_large)) + list(numeral_candidates(a.n_num)))
    # dedup across sources by (text, rounded global centre) so a harvest label isn't repeated as a large one
    seen = set(); uniq = []
    for c in data:
        k = (c["name"].upper(), c["tag"], round(c["cap_h"] / 10))
        if k in seen: continue
        seen.add(k); uniq.append(c)
    data = uniq
    for i, c in enumerate(data): c["id"] = i
    from collections import Counter
    grp = Counter(c["group"] for c in data)
    html = TEMPLATE.replace("__DATA__", json.dumps(data)).replace("__FACES__", json.dumps(faces))
    out = f"{ADMIN}/alphabet_ui.html"; open(out, "w").write(html)
    print(f"wrote {out}: {len(data)} candidates ({dict(grp)}) across {len(set(c['suggest'] for c in data))} seeded faces", flush=True)

TEMPLATE = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP alphabet builder</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f6f3ec;color:#222}
 header{position:sticky;top:0;background:#f6f3ec;padding:8px 14px;border-bottom:1px solid #ccc;z-index:20;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 button{padding:5px 9px;border:1px solid #cbb;border-radius:6px;background:#fffdf7;cursor:pointer}
 .wrap{display:flex;gap:14px;padding:14px;align-items:flex-start}
 .left{flex:1;min-width:0;display:flex;flex-direction:column;gap:12px}
 .right{width:310px;position:sticky;top:60px;max-height:calc(100vh - 84px);overflow-y:auto;background:#fff;border:1px solid #dcd6c8;border-radius:8px;padding:8px}
 .right h3{margin:4px 6px;font-size:12px;color:#555;position:sticky;top:0;background:#fff}
 .faceitem{padding:6px 8px;border-radius:6px;cursor:pointer;font-size:13px;border:1px solid transparent;border-left:5px solid #e2ddd0}
 .faceitem:hover{background:#f2ede2}
 .faceitem .fa{color:#a5322e;font-size:11px}
 .faceitem img{display:block;max-height:38px;max-width:100%;background:#fff;border:1px solid #eee;margin-bottom:3px}
 .faceitem.sel{outline:2px solid #c0392b;outline-offset:-2px}
 .faceitem.caps .lab{font-weight:600}
 .faceitem.cov-low{border-left-color:#e0a020}
 .faceitem.cov-ok{border-left-color:#2e7d32;background:#eef6ec}
 .faceitem .cnt{float:right;font-size:11px;color:#999}
 .faceitem.cov-low .cnt{color:#b5860b;font-weight:600}
 .faceitem.cov-ok .cnt{color:#2e7d32;font-weight:700}
 .card{background:#fff;border:1px solid #dcd6c8;border-radius:8px;padding:10px;cursor:pointer}
 .card.active{outline:3px solid #d33;outline-offset:1px}
 .card.done{border-color:#2e7d32;background:#f5faf3}
 .card.skipped{opacity:.5;background:#f2efe8}
 .card.skipped .facebadge::after{content:" — SKIPPED";color:#c0392b}
 .hd{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
 .trans{font-size:15px;font-weight:600;padding:3px 6px;border:1px solid #cbb;border-radius:5px;width:230px}
 .muted{color:#777;font-size:12px} .facebadge{font-size:13px;color:#1b5e20;font-weight:600}
 canvas{border:1px solid #bbb;cursor:crosshair;background:#fff;max-width:100%}
 .ctrl{font-size:12px;color:#555;margin:4px 0 6px;display:flex;align-items:center;gap:4px;flex-wrap:wrap}
 .ctrl input[type=range]{width:150px;vertical-align:middle}
 .boxes{font-size:12px;color:#444;margin-top:4px}
 .chip{display:inline-block;border:1px solid #d0c0b0;border-radius:10px;padding:1px 8px;margin:2px;background:#faf7f0}
</style>
<header>
 <b>Alphabet builder</b>
 <span class=muted>Select a row (click or draw) → assign its FACE from the right column · fix the transcription if wrong · draw a box per letter</span>
 <button onclick=dl()>⬇ Download alphabet_labels.json</button> <span id=stat class=muted></span>
 <label class=muted style="margin-left:10px"><input type=checkbox id=hidedone onchange=applyHide()> hide completed &amp; skipped</label>
 <span id=covstat style="margin-left:auto;font-size:13px"></span>
</header>
<div class=wrap>
 <div class=left id=grid></div>
 <div class=right id=faces></div>
</div>
<script>
const D=__DATA__, FACES=__FACES__;
const S=JSON.parse(localStorage.getItem("gbstamp_alpha")||"{}");
let active=null;
const TARGET=8;   // words/face for "adequately covered" (same-letter kNN needs ~8-10/face)
function faceCounts(){ const c={}; for(const d of D){ const s=S[d.id]; if(!s||s.skip||!(s.boxes&&s.boxes.length))continue;
  (c[s.face]=c[s.face]||{w:0,g:0}); c[s.face].w++; c[s.face].g+=s.boxes.length; } return c; }
function sigOf(key){ const f=FACES.find(x=>x.key===key); return f?f.sig:key; }
function updateFaceCounts(){ const c=faceCounts();                     // per-face {w,g}
  const sc={}; for(const f of FACES){ const n=c[f.key]||{w:0,g:0}; (sc[f.sig]=sc[f.sig]||{w:0,g:0}); sc[f.sig].w+=n.w; sc[f.sig].g+=n.g; }
  document.querySelectorAll('.faceitem').forEach(it=>{ const s=sigOf(it.dataset.key), t=sc[s]||{w:0,g:0}, own=(c[it.dataset.key]||{w:0}).w;
    const b=it.querySelector('.cnt'); if(b) b.textContent = t.w?(own?`${own}w · Σ${t.w}`:`Σ${t.w}`):'';   // own + signature total
    it.classList.remove('cov-none','cov-low','cov-ok');
    it.classList.add(t.w===0?'cov-none':(t.w<TARGET?'cov-low':'cov-ok')); });                            // colour by SIGNATURE coverage
  const sigs=[...new Set(FACES.map(f=>f.sig))]; let ok=0,par=0,non=0;
  for(const s of sigs){ const w=(sc[s]||{w:0}).w; if(w===0)non++; else if(w<TARGET)par++; else ok++; }
  const cs=document.getElementById('covstat'); if(cs) cs.innerHTML=`<b style="color:#2e7d32">${ok} covered</b> · <b style="color:#b5860b">${par} partial</b> · ${non} none <span class=muted>of ${sigs.length} type-signatures (faces sharing a font share coverage · target ${TARGET})</span>`; }
function st(d){ if(!S[d.id]) S[d.id]={face:(d.suggest||""),text:d.name,boxes:[],skip:false,rot:0,zoom:1}; const s=S[d.id];
  if(s.text==null)s.text=d.name; if(s.rot==null)s.rot=0; if(s.zoom==null)s.zoom=1; return s; }
function chars(d){ return [...st(d).text].filter(c=>/[a-z0-9]/i.test(c)); }
function save(){ localStorage.setItem("gbstamp_alpha",JSON.stringify(S)); stat(); updateFaceCounts(); applyHide(); }
function applyHide(){ const cb=document.getElementById('hidedone'); const hide=cb&&cb.checked;
  for(const d of D){ const card=document.querySelector(`.card[data-id="${d.id}"]`); if(!card)continue; const s=S[d.id];
    const done=s&&!s.skip&&s.boxes&&s.boxes.length, skip=s&&s.skip;
    card.style.display=(hide&&(done||skip)&&d.id!==active)?'none':''; } }   // keep the active row visible even if done
function stat(){ let done=Object.values(S).filter(x=>!x.skip&&x.boxes.length).length,sk=Object.values(S).filter(x=>x.skip).length;
  document.getElementById('stat').textContent=`${done} labelled · ${sk} skipped · ${D.length} total`; }
function faceLabel(k){ const f=FACES.find(x=>x.key===k); return f?f.label:(k||"—"); }
function cv_of(d){ return document.querySelector(`canvas[data-id="${d.id}"]`); }

function renderFaces(){ const el=document.getElementById('faces');
  el.innerHTML='<h3>FACE — click to assign · green=covered, amber=partial</h3>'+FACES.map(f=>
   `<div class="faceitem${f.caps?' caps':''}" data-key="${f.key}"><span class=cnt></span>${f.ex?`<img src="data:image/png;base64,${f.ex}">`:''}<span class=lab>${f.label}</span><div class=fa>${f.style} · fill:${f.fill} · ${f.decor}${f.caps?' · CAPS':''}</div></div>`).join('');
  el.querySelectorAll('.faceitem').forEach(it=>it.onclick=()=>{ if(active==null)return; st(D[active]).face=it.dataset.key; save(); renderCard(D[active]); highlightFace(); });
  updateFaceCounts(); }
function highlightFace(){ const cur=active!=null?st(D[active]).face:null;
  document.querySelectorAll('.faceitem').forEach(it=>it.classList.toggle('sel',it.dataset.key===cur)); }

function setActive(id){ active=id; document.querySelectorAll('.card').forEach(c=>c.classList.toggle('active',+c.dataset.id===id)); highlightFace(); applyHide();
  const it=document.querySelector(`.faceitem[data-key="${st(D[id]).face}"]`); if(it)it.scrollIntoView({block:'nearest'}); }

const IMGCACHE={};
function loadImg(d,cb){ if(IMGCACHE[d.id]){cb(IMGCACHE[d.id]);return;} const im=new Image(); im.onload=()=>{IMGCACHE[d.id]=im;cb(im);}; im.src="data:image/png;base64,"+d.img; }
function rotDims(img,rad){ const w=img.width,h=img.height;
  return {w,h,nw:Math.abs(w*Math.cos(rad))+Math.abs(h*Math.sin(rad)),nh:Math.abs(w*Math.sin(rad))+Math.abs(h*Math.cos(rad))}; }
function draw(cv,d){ const s=st(d); loadImg(d,img=>{ const rad=(s.rot||0)*Math.PI/180; const {w,h,nw,nh}=rotDims(img,rad);
    cv.width=Math.round(nw); cv.height=Math.round(nh); const ctx=cv.getContext('2d');
    ctx.save(); ctx.translate(nw/2,nh/2); ctx.rotate(rad); ctx.drawImage(img,-w/2,-h/2); ctx.restore();
    if(!s.rot){ ctx.strokeStyle="#6aa0d8"; ctx.setLineDash([5,4]); ctx.strokeRect(d.guide[0],d.guide[1],d.guide[2]-d.guide[0],d.guide[3]-d.guide[1]); ctx.setLineDash([]); }
    ctx.strokeStyle="#d33"; ctx.lineWidth=2; ctx.font="14px system-ui"; ctx.fillStyle="#d33";
    s.boxes.forEach(b=>{ ctx.strokeRect(b.x,b.y,b.w,b.h); ctx.fillText(b.char||"?",b.x+2,b.y-3); });
    cv.style.width=Math.round(cv.width*(s.zoom||1))+'px'; }); }
// clean rotated snippet (no box overlays) -> b64, cached in state, used for export so pixels match the boxes' frame
function cacheRotated(d){ const s=st(d); if(!s.rot){ s.rimg=null; return; } loadImg(d,img=>{ const rad=s.rot*Math.PI/180; const {w,h,nw,nh}=rotDims(img,rad);
    const oc=document.createElement('canvas'); oc.width=Math.round(nw); oc.height=Math.round(nh); const c=oc.getContext('2d');
    c.save(); c.translate(nw/2,nh/2); c.rotate(rad); c.drawImage(img,-w/2,-h/2); c.restore();
    s.rimg=oc.toDataURL('image/png').split(',')[1]; save(); }); }

function cardHTML(d){ const s=st(d);
  return `<div class="hd">
     <input class=trans value="${(s.text||'').replace(/"/g,'&quot;')}" data-id=${d.id}>
     <span class=facebadge>face: ${faceLabel(s.face)}</span>
     <span class=muted>${d.group} · cap-h ${d.cap_h} · ${d.tag}${d.suggest?' · hint:'+d.suggest:''}</span>
     <button class=clr data-id=${d.id}>clear boxes</button>
     <button class=skp data-id=${d.id}>${s.skip?'un-skip':'skip'}</button></div>
   <div class=ctrl>↻ rotate <input type=range class=rot min=-90 max=90 step=1 value=${s.rot||0}><b class=rv>${s.rot||0}°</b>
     &nbsp; 🔍 zoom <input type=range class=zoom min=1 max=5 step=0.25 value=${s.zoom||1}><b class=zv>${(s.zoom||1)}×</b>
     &nbsp;<button class=rst>reset</button> <span class=muted>rotating clears boxes — set it first to level a curved label</span></div>
   <canvas data-id=${d.id}></canvas><div class=boxes id=bx${d.id}></div>`; }
function renderCard(d){ const card=document.querySelector(`.card[data-id="${d.id}"]`); if(!card)return; const s=st(d);
  card.classList.toggle('done',!s.skip&&s.boxes.length>0); card.classList.toggle('skipped',!!s.skip);
  const skb=card.querySelector('.skp'); if(skb) skb.textContent=s.skip?'un-skip':'skip';
  card.querySelector('.facebadge').textContent='face: '+faceLabel(s.face); showBoxes(d); }

function render(){ const g=document.getElementById('grid'); g.innerHTML='';
 for(const d of D){ const card=document.createElement('div'); card.className='card'; card.dataset.id=d.id;
   card.innerHTML=cardHTML(d); g.appendChild(card);
   card.onclick=e=>{ if(!e.target.closest('input,button,canvas,.del')) setActive(d.id); };
   const cv=card.querySelector('canvas'); draw(cv,d); bindCanvas(cv,d);
   card.querySelector('.trans').onchange=e=>{ st(d).text=e.target.value; const ch=chars(d);
     st(d).boxes.forEach((b,i)=>b.char=ch[i]||"?"); save(); draw(cv,d); showBoxes(d); };
   card.querySelector('.clr').onclick=()=>{ st(d).boxes=[]; save(); draw(cv,d); renderCard(d); };
   card.querySelector('.skp').onclick=()=>{ st(d).skip=!st(d).skip; save(); renderCard(d); };
   const rotS=card.querySelector('.rot'), zoomS=card.querySelector('.zoom');
   rotS.oninput=e=>{ card.querySelector('.rv').textContent=e.target.value+'°'; };
   rotS.onchange=e=>{ const v=+e.target.value; if(v!==st(d).rot) st(d).boxes=[]; st(d).rot=v; cacheRotated(d); save(); draw(cv,d); showBoxes(d); renderCard(d); };
   zoomS.oninput=e=>{ st(d).zoom=+e.target.value; card.querySelector('.zv').textContent=(+e.target.value)+'×'; draw(cv,d); };
   zoomS.onchange=()=>save();
   card.querySelector('.rst').onclick=()=>{ st(d).rot=0; st(d).zoom=1; st(d).rimg=null; st(d).boxes=[]; save(); render(); setActive(d.id); };
   renderCard(d);
 }
 renderFaces(); stat(); applyHide();
}
function bindCanvas(cv,d){ let sx,sy,drag=false; const s=st(d);
 const pos=e=>{const r=cv.getBoundingClientRect();return[(e.clientX-r.left)*cv.width/r.width,(e.clientY-r.top)*cv.height/r.height];};
 cv.onmousedown=e=>{ setActive(d.id); [sx,sy]=pos(e); drag=true; };
 cv.onmousemove=e=>{ if(!drag)return; const[x,y]=pos(e); draw(cv,d); const ctx=cv.getContext('2d');
   ctx.strokeStyle="#0a0";ctx.lineWidth=2;ctx.strokeRect(Math.min(sx,x),Math.min(sy,y),Math.abs(x-sx),Math.abs(y-sy)); };
 cv.onmouseup=e=>{ if(!drag)return; drag=false; const[x,y]=pos(e); const w=Math.abs(x-sx),h=Math.abs(y-sy); if(w<4||h<4)return;
   const ch=chars(d); s.boxes.push({char:ch[s.boxes.length]||"?",x:Math.min(sx,x),y:Math.min(sy,y),w,h}); save(); draw(cv,d); showBoxes(d); renderCard(d); };
}
function showBoxes(d){ const s=st(d); const el=document.getElementById('bx'+d.id); if(!el)return; const ch=chars(d);
 el.innerHTML=s.boxes.map((b,i)=>`<span class=chip>${b.char} <a href=# data-id=${d.id} data-i=${i} class=del>✕</a></span>`).join('')
   + (s.boxes.length<ch.length?` <span class=muted>next: ${ch[s.boxes.length]}</span>`:' <span class=muted>✓ all letters</span>');
 el.querySelectorAll('.del').forEach(a=>a.onclick=e=>{e.preventDefault();const d2=D[+e.target.dataset.id];st(d2).boxes.splice(+e.target.dataset.i,1);save();
   draw(cv_of(d2),d2); showBoxes(d2); renderCard(d2);}); }
function dl(){ const out=[];
 for(const d of D){ const s=S[d.id]; if(!s||s.skip||!s.boxes.length)continue;
   out.push({name:s.text,face:s.face,tag:d.tag,scale:d.scale,cap_h:d.cap_h,group:d.group,rot:s.rot||0,img:(s.rimg||d.img),
             letters:s.boxes.map(b=>({char:b.char,x:Math.round(b.x),y:Math.round(b.y),w:Math.round(b.w),h:Math.round(b.h)}))}); }
 const b=new Blob([JSON.stringify(out)],{type:"application/json"});const a=document.createElement("a");
 a.href=URL.createObjectURL(b);a.download="alphabet_labels.json";a.click(); }
render();
</script>"""

if __name__ == "__main__":
    main()
