"""Inspect the assembled glyph library — every letter held for every face — and collapse the face inventory.

Two views, because two different questions are being asked of the same 231 glyphs:

  BY FACE    what letters do we hold for this face, and are they clean enough to match against? This is the
             coverage question that decides which faces the overlay matcher can even attempt.
  BY LETTER  the same character across every face, side by side. This is the question the OS Characteristic
             Sheet cannot answer: several of its categories are the SAME typeface at different sizes (the
             Roman-antiquities face is the road-label face; there are generic upright and italic serifs that
             between them carry most labels), and the only way to see that is to put the same letter from each
             face next to each other. Glyphs are size-normalised here, so size differences are removed and what
             remains is letterform — which is exactly the comparison needed.

The merge column exports a map from OS face key to a generic face name, so the inventory can be reduced to
distinct TYPEFACES before any matching is attempted.

    python make_glyph_ui.py --npz labels/alphabet_glyphs.npz --out admin_probe/glyph_ui.html
"""
import argparse, base64, io, json, os
import numpy as np
from PIL import Image


def png_b64(g, scale=3):
    """Glyph as a data URI. Ink is drawn dark on white; nearest-neighbour upscale keeps the pixels honest."""
    im = Image.fromarray(255 - g.astype(np.uint8)).convert("L")
    im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
    b = io.BytesIO()
    im.save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="labels/alphabet_glyphs.npz")
    ap.add_argument("--inventory", default="labels/face_inventory.json")
    ap.add_argument("--out", default="admin_probe/glyph_ui.html")
    ap.add_argument("--scale", type=int, default=3)
    a = ap.parse_args()

    d = np.load(a.npz, allow_pickle=True)
    glyphs = d["glyphs"]
    oids = d["oid"] if "oid" in d.files else np.arange(len(glyphs))
    chars = d["chars"].astype(str)
    faces = d["faces"].astype(str)
    words = d["word"] if "word" in d.files else np.zeros(len(chars), int)
    angles = d["angle"] if "angle" in d.files else np.zeros(len(chars))

    inv, order = {}, []
    if os.path.exists(a.inventory):
        inv = json.load(open(a.inventory)).get("faces", {})
        order = list(inv)

    items = []
    for i in range(len(glyphs)):
        items.append(dict(
            # `i` is the index in THIS npz; `oid` is the index in the ORIGINAL library, which is what the
            # curation spec addresses. Deleting from a curated view must name the original glyph, or the
            # deletion would silently point at a different sample next time the spec is re-applied.
            i=int(i), oid=int(oids[i]), ch=chars[i], face=faces[i], word=int(words[i]),
            angle=round(float(angles[i]), 1),
            img=png_b64(glyphs[i], a.scale),
        ))

    # Every inventory face appears, sample or not: a face nobody has spotted yet is still a face the matcher
    # must account for, and showing it empty is what makes the gap visible.
    per_face = {f: {"letters": set(), "upper": set(), "lower": set(), "n": 0} for f in order}
    for it in items:
        v = per_face.setdefault(it["face"], {"letters": set(), "upper": set(), "lower": set(), "n": 0})
        v["letters"].add(it["ch"])
        v["upper" if it["ch"].isupper() else "lower"].add(it["ch"])
        v["n"] += 1
    seq = order + [f for f in per_face if f not in order]
    summary = {f: dict(n=per_face[f]["n"], letters=len(per_face[f]["letters"]),
                       upper=len(per_face[f]["upper"]), lower=len(per_face[f]["lower"]),
                       os=inv.get(f, {}).get("os", ""), known=f in inv)
               for f in seq}

    withs = [f for f, v in summary.items() if v["n"]]
    print(f"{len(items)} glyphs, {len(withs)}/{len(summary)} inventory faces have samples, "
          f"{len(set(chars))} distinct characters")

    data = json.dumps(dict(items=items, summary=summary, order=seq))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "w").write(HTML.replace("__DATA__", data))
    print(f"wrote {a.out} ({os.path.getsize(a.out)/1e6:.2f} MB)")


HTML = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · glyph library</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee;color:#1a1a1a}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;z-index:9;
        display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 header b{font-size:15px} button{background:#2a7;color:#fff;border:0;border-radius:5px;padding:6px 12px;
   cursor:pointer;font-size:13px} button.alt{background:#555} button.on{background:#d80}
 #wrap{padding:12px}
 .face{background:#fff;border:1px solid #ddd;border-radius:6px;margin-bottom:10px;padding:8px 10px}
 .face h3{margin:0 0 6px;font-size:13px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
 .face h3 .k{font-weight:700} .face h3 .m{color:#777;font-weight:400;font-size:11px}
 .row{display:flex;flex-wrap:wrap;gap:10px}
 .cell{text-align:center} .cell img{image-rendering:pixelated;background:#fff;border:1px solid #e2e2e2}
 .cell{cursor:pointer} .cell.gone{opacity:.28}
 .cell.gone img{outline:2px solid #c00}
 .cell .c{font-size:11px;color:#555;font-weight:600}
 .cell .w{font-size:9px;color:#aaa}
 .lt{background:#fff;border:1px solid #ddd;border-radius:6px;margin-bottom:10px;padding:8px 10px}
 .lt h3{margin:0 0 6px;font-size:15px}
 .grp{display:inline-block;margin:0 12px 10px 0;padding:4px 6px;border:1px solid #eee;border-radius:5px;
      vertical-align:top}
 .grp .fn{font-size:10px;color:#444;max-width:150px;word-break:break-word}
 .merge{margin-top:6px} .merge input{font:12px system-ui;padding:3px 5px;width:420px}
 .face.empty{background:#faf8f5;border-style:dashed}
 .warn{color:#a15;font-size:11px}
</style>
<header>
 <b>GB-STAMP · glyph library</b>
 <button id=bf class=on onclick="setView('face')">by face</button>
 <button id=bl onclick="setView('letter')">by letter (compare faces)</button>
 <label>size <input type=range id=zoom min=1 max=5 value=3></label>
 <label><input type=checkbox id=he onchange=render()> hide empty faces</label>
 <button class=alt onclick=exportDesignations()>Export inventory</button>
 <button class=alt onclick=exportDeletes()>Export deletions</button>
 <span id=dstat style="opacity:.8"></span>
 <span id=stat></span>
</header>
<div id=wrap></div>
<script>
const D=__DATA__; let view='face';
const del=new Set();          // original-library ids marked for deletion
const des={};
Object.keys(D.summary).forEach(f=>des[f]=D.summary[f].os||'');
document.getElementById('stat').textContent =
  `${D.items.length} glyphs · ${Object.keys(D.summary).length} inventory faces · `+
  `${new Set(D.items.map(i=>i.ch)).size} characters`;

function cell(it){
  // Click to mark/unmark. The id shown and exported is the ORIGINAL library index, not this view's index,
  // so a deletion decided in a curated view still names the right sample when the spec is re-applied.
  const gone = del.has(it.oid);
  return `<div class="cell${gone?' gone':''}" onclick="toggle(${it.oid})" title="click to delete / restore">
          <img src="data:image/png;base64,${it.img}" style="height:${zoomPx()}px">
          <div class=c>${it.ch}${gone?' &#10007;':''}</div>
          <div class=w>#${it.oid} w${it.word} ${it.angle}&deg;</div></div>`;
}
function toggle(oid){ del.has(oid)?del.delete(oid):del.add(oid); render(); }
function zoomPx(){ return 22*(+document.getElementById('zoom').value); }

function byFace(){
  return D.order.filter(f=>D.summary[f] && (!hideEmpty() || D.summary[f].n)).map(f=>{
    const s=D.summary[f];
    const its=D.items.filter(i=>i.face==f).sort((a,b)=>a.ch.localeCompare(b.ch)||a.word-b.word);
    let note='';
    if(!s.n) note=`<span class=warn>no samples yet</span>`;
    else if(Math.max(s.upper,s.lower)<5) note=`<span class=warn>thin in both cases</span>`;
    return `<div class="face${s.n?'':' empty'}"><h3><span class=k>${f}</span>
      <span class=m>${s.n} glyphs · ${s.upper} UPPER · ${s.lower} lower</span> ${note}</h3>
      <div class=row>${its.map(cell).join('')}</div>
      <div class=merge>OS designations (pipe-delimited):
        <input class=os value="${(des[f]||'').replace(/"/g,'&quot;')}"
               oninput="des['${f}']=this.value" placeholder="e.g. Parishes (Mother or Ancient)|..."></div>
      </div>`;
  }).join('');
}
function hideEmpty(){ return document.getElementById('he').checked; }
function byLetter(){
  const chars=[...new Set(D.items.map(i=>i.ch))].sort();
  return chars.map(c=>{
    const its=D.items.filter(i=>i.ch==c);
    const faces=[...new Set(its.map(i=>i.face))].sort();
    return `<div class=lt><h3>${c} <span style="font-size:11px;color:#777">${faces.length} faces</span></h3>
      ${faces.map(f=>`<div class=grp><div class=row>
        ${its.filter(i=>i.face==f).map(cell).join('')}</div>
        <div class=fn>${f}</div></div>`).join('')}</div>`;
  }).join('');
}
function render(){
  document.getElementById('wrap').innerHTML = view=='face'?byFace():byLetter();
  document.getElementById('dstat').textContent = del.size?`${del.size} marked for deletion`:'';
}
function exportDeletes(){
  if(!del.size){alert('No glyphs marked. Click a glyph to mark it.');return;}
  const out={}; [...del].sort((a,b)=>a-b).forEach(o=>{
    const it=D.items.find(x=>x.oid==o);
    out[o]=it?`${it.face} '${it.ch}' (word ${it.word})`:'';
  });
  const blob=new Blob([JSON.stringify(out,null,1)],{type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download='glyph_deletions.json'; a.click();
}
function setView(v){ view=v;
  document.getElementById('bf').className = v=='face'?'on':'';
  document.getElementById('bl').className = v=='letter'?'on':'';
  render(); }
document.getElementById('zoom').oninput=render;
function exportDesignations(){
  const faces={}; D.order.forEach(f=>faces[f]={os:(des[f]||'').trim()});
  const blob=new Blob([JSON.stringify({faces},null,1)],{type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download='face_inventory.json'; a.click();
}
render();
</script>
"""

if __name__ == "__main__":
    main()
