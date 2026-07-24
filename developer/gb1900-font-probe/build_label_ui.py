"""Step 2 — ACTIVE-LEARNING labelling UI over the backbone-descriptor bank, targeting the 16 typographic
SIGNATURES (base_style·fill·decor), NOT the 49 fine faces. Rationale (confirmed on real data 2026-07-23): the
OS used one generic serif at overlapping sizes for a whole family of small descriptive features, so within a
serif signature the faces are typographically indistinguishable (font AND cap-height overlap: county_bridges
36px == woods_copses 36px). No font/size method can split them — feature-type there comes from text/gazetteer/
symbol downstream. So the classifier target IS the signature, which is what's eye- and backbone-distinguishable.

Round 1 (no pool labels) = farthest-point DIVERSE cold-start. Rounds 2+ fold labels/pool_labels.json in as
anchors (each carries `sig`) and rank the bank by UNCERTAINTY (novelty far-from-anchor + ambiguity split-between-
signatures), FPS-diversified, with the nearest-anchor PROPOSED signature per card. Re-crops selected words from
the /vast tile cache (red box = detected extent). Emits self-contained HTML; labels export to pool_labels.json.

GPU-free; run on CRC htc.   python build_label_ui.py --n 300
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import os, glob, json, base64, io, argparse, numpy as np
from collections import defaultdict, Counter
from PIL import Image, ImageDraw
from make_font_testset_v2 import derotate

HERE = "/vast/ishi/gb1900/probe/font"; SPOT = "/vast/ishi/gb1900/edition/spot"
TAX = json.load(open(f"{HERE}/font_taxonomy.json"))
def sigof(f): return "·".join(str(f.get(x)) for x in ("base_style", "fill", "decor"))
FACE_SIG = {f["key"]: sigof(f) for f in TAX}
MIN_ANCHORS = 15
def key(gcx, gcy): return (round(float(gcx), 1), round(float(gcy), 1))

# 16 signatures with plain-English recognition cues (fill: solid | none=hollow/outline | diagonal/horizontal=hatched)
SIG_DESC = {
 "italic·solid·serif":       "Italic serif — water & generic small descriptive (rivers, seats, churches, bridges…). Big italic bucket.",
 "upright·solid·serif":      "Upright serif, solid — generic descriptive (churches, bridges, woods, stations…).",
 "upright·solid·plain":      "Upright plain/sans (no serifs), solid — Roman antiquities, wards, passenger railways.",
 "blackletter·solid·fancy":  "Blackletter / Gothic (Old-English) — antiquities (Saxon, Norman, medieval sites).",
 "italic·solid·plain":       "Italic plain figures/letters, solid — boundary numerals & markers, mineral railways.",
 "numeral·solid·plain":      "Upright figures — contour / spot heights, bench marks.",
 "upright·none·fancy":       "Upright ORNATE outline caps — county names (top-level admin).",
 "upright·solid·fancy":      "Upright ORNATE serif, solid — county boroughs (admin).",
 "upright·diagonal·serif":   "Upright serif, DIAGONAL hatch fill — hundreds, cities w/o MP (admin).",
 "upright·horizontal·serif": "Upright serif, HORIZONTAL hatch fill — municipal boroughs, divisional counties (admin).",
 "upright·none·serif":       "Upright serif OUTLINE (hollow letters) — ancient parishes, workhouses (admin).",
 "upright·none·plain":       "Upright plain OUTLINE (hollow) — urban sanitary districts (admin).",
 "upright·diagonal·plain":   "Upright plain, DIAGONAL hatch — poor-law unions (admin).",
 "italic·horizontal·serif":  "Italic serif, HORIZONTAL hatch fill — divisional / other towns (admin).",
 "italic·none·serif":        "Italic serif OUTLINE (hollow) — sub-div townships / town districts (admin).",
 "italic·diagonal·serif":    "Italic serif, DIAGONAL hatch fill — liberties (admin).",
}
def _ex_datauri(rel):
    p = f"{HERE}/{rel}" if rel else None
    if p and os.path.exists(p): return "data:image/jpeg;base64," + base64.b64encode(open(p, "rb").read()).decode()
    return None
def sig_exemplar(s):                                          # a member face's exemplar image for signature s
    for f in TAX:
        if FACE_SIG[f["key"]] == s and f.get("exemplar"):
            u = _ex_datauri(f["exemplar"])
            if u: return u
    return None

def load_bank(pattern=f"{SPOT}/desc/shard_*.npz"):
    ds, meta = [], defaultdict(list)
    for s in sorted(glob.glob(pattern)):
        d = np.load(s, allow_pickle=True); ds.append(d["desc"].astype(np.float32))
        for k in ("gcx", "gcy", "lon", "lat", "text", "score"): meta[k].append(d[k])
        # weak transcript-derived signature: present in the pin bank, absent from the legacy one
        meta["weak"].append(d["weak_sig"] if "weak_sig" in d.files else np.array([""] * len(d["desc"]), object))
    X = np.concatenate(ds)
    for k in meta: meta[k] = np.concatenate(meta[k])
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    return X, meta

def fps(X, cand, n, seed_idx):
    sel = [seed_idx]; picked = X[seed_idx][None]
    cand = np.array([c for c in cand if c != seed_idx])
    while len(sel) < min(n, len(cand) + 1) and len(cand):
        mind = 1 - (X[cand] @ picked.T).max(1); j = int(np.argmax(mind)); nxt = cand[j]
        sel.append(nxt); picked = np.vstack([picked, X[nxt]]); cand = np.delete(cand, j)
    return sel

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--mode", default="auto", choices=["auto", "uncertainty", "grow", "novelty", "weak"],
                    help="grow = nearest neighbours of rare sigs (KNOWN DEAD END: yields near-duplicates); "
                         "novelty = words FARTHEST from all anchors (discovery); "
                         "weak = transcript-seeded, the bootstrap round — see --weak-sig")
    ap.add_argument("--weak-sig", default=None,
                    help="restrict `weak` mode to one signature, e.g. 'upright·solid·serif' (the weak axis)")
    ap.add_argument("--target", type=int, default=20, help="grow rare sigs up to this many anchors")
    ap.add_argument("--out", default=f"{SPOT}/label_ui.html")
    # Crop-convention switches. Defaults keep the legacy MapReader-box path working unchanged; point them at
    # the pin corpus for the Hi-SAM convention. NEVER mix the two banks — a descriptor space straddling two
    # crop conventions lets the classifier key on the convention instead of the font (NEXT-PHASE.md §1.2).
    ap.add_argument("--bank", default=f"{SPOT}/desc/shard_*.npz")
    ap.add_argument("--boxes", default=f"{SPOT}/boxes_*.jsonl", help="source records to re-crop cards from")
    ap.add_argument("--poly-field", default="gpoly", help="'line_gpoly' for the pin corpus (the label extent)")
    ap.add_argument("--pool", default=f"{HERE}/labels/pool_labels.json", help="labels for THIS convention")
    # 0.55 is a MapReader DETECTION CONFIDENCE. The pin bank's `score` is Hi-SAM's predicted mask IoU, which
    # tops out around 0.42 and answers a different question entirely (we prompted at a known label, so there
    # was no detection decision to be confident about). Pass 0 for the pin bank — quality control there comes
    # from the on_ink / mask-area flags, not from this number.
    ap.add_argument("--min-score", type=float, default=0.55)
    ap.add_argument("--anchors-npz", default=None,
                    help="descriptors+sigs to use as anchors directly (e.g. anchor_desc_hisam.npz), for when "
                         "the labelled anchors live outside the sampled regions and so aren't in the bank")
    a = ap.parse_args()
    X, meta = load_bank(a.bank); N = len(X); print(f"bank: {N} words ({a.bank})", flush=True)

    done = set(); Ad = []; Asig = []
    plab = a.pool
    if os.path.exists(plab):
        lab = json.load(open(plab)); idx = {key(meta["gcx"][i], meta["gcy"][i]): i for i in range(N)}
        for l in lab:
            done.add(key(l["gcx"], l["gcy"])); i = idx.get(key(l["gcx"], l["gcy"]))
            s = l.get("sig") or (FACE_SIG.get(l["face"]) if l.get("face") else None)   # sig-native, face fallback
            if i is not None and s: Ad.append(X[i]); Asig.append(s)
    if a.anchors_npz and os.path.exists(a.anchors_npz):
        d = np.load(a.anchors_npz, allow_pickle=True)
        key_desc = "desc_line" if "desc_line" in d.files else "desc"      # line crops = the production convention
        E = d[key_desc].astype(np.float32)
        E /= (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)            # bank rows are unit-norm; match them
        Ad.extend(list(E)); Asig.extend([str(s) for s in d["sigs"]])
        print(f"+{len(E)} anchors from {os.path.basename(a.anchors_npz)}:{key_desc}", flush=True)
    Ad = np.array(Ad) if Ad else np.zeros((0, X.shape[1]), np.float32); Asig = np.array(Asig)
    have = len(Ad) >= MIN_ANCHORS
    print(f"{len(Ad)} anchors, {len(done)} done -> {'UNCERTAINTY' if have else 'COLD-START'} round", flush=True)
    if have:                                                  # readout: are the labelled signatures separable?
        An = Ad / (np.linalg.norm(Ad, axis=1, keepdims=True) + 1e-9); S = An @ An.T; np.fill_diagonal(S, -2)
        perc = defaultdict(lambda: [0, 0])
        ok = 0
        for i in range(len(An)):
            nn = np.argsort(-S[i])[:5]; pred = Counter(Asig[j] for j in nn).most_common(1)[0][0]
            ok += (pred == Asig[i]); perc[Asig[i]][1] += 1; perc[Asig[i]][0] += (pred == Asig[i])
        maj = max(Counter(Asig).values()) / len(Asig)
        print(f"READOUT LOO kNN(5): sig acc {ok/len(An):.3f} on {len(An)} anchors / {len(set(Asig))} sigs (majority {maj:.2f})", flush=True)
        for s in sorted(perc, key=lambda s: -perc[s][1]):
            print(f"   {perc[s][0]:>3}/{perc[s][1]:<3} {perc[s][0]/perc[s][1]:.2f}  {s}", flush=True)

    score = meta["score"].astype(float)
    cand = [i for i in range(N) if score[i] >= a.min_score and key(meta["gcx"][i], meta["gcy"][i]) not in done]
    props_for = {}; selu = {}
    if a.mode == "weak":
        # THE BOOTSTRAP ROUND. Candidates are chosen by what the TRANSCRIPT implies, not by where they sit in
        # descriptor space — which is the whole point: `grow` mode picks a rare signature's nearest neighbours
        # and gets near-duplicates of what is already labelled, whereas the lexicon supplies genuinely varied
        # words from all over the country. The weak signature is only the PROPOSED answer on the card; the
        # human confirms or corrects it, and nothing is reported from an unconfirmed label.
        weak = meta["weak"].astype(str)
        pool = [i for i in cand if weak[i] and (not a.weak_sig or weak[i] == a.weak_sig)]
        if not pool:
            raise SystemExit(f"no weakly-labelled candidates{' for ' + a.weak_sig if a.weak_sig else ''} — "
                             f"is this the pin bank? (--bank)")
        sel = fps(X, pool, a.n, int(pool[0]))            # spread across descriptor space, not the top-N nearest
        props_for = {i: [weak[i]] for i in sel}
        selu = {i: 0.0 for i in sel}
        if have:
            # Show the descriptor's own opinion next to the lexicon's. Where they disagree is the informative
            # card — either the lexicon mis-typed the word, or this is the confusion the round exists to fix.
            sl = sorted(set(Asig)); cols = {s: np.where(Asig == s)[0] for s in sl}
            sims = X[np.array(sel)] @ Ad.T
            cls = np.stack([sims[:, cols[s]].max(1) for s in sl], 1)
            for n_, i in enumerate(sel):
                near = [sl[k] for k in np.argsort(-cls[n_])[:2]]
                props_for[i] = [weak[i]] + [s for s in near if s != weak[i]]
                selu[i] = round(float(near[0] != weak[i]), 3)   # 1.0 = descriptor disagrees with the lexicon
            print(f"  descriptor disagrees with the lexicon on {int(sum(selu.values()))}/{len(sel)} cards",
                  flush=True)
        got = Counter(weak[i] for i in sel)
        print(f"WEAK: {len(pool)} candidates -> {len(sel)} cards {dict(got)}", flush=True)
    elif have:
        sl = sorted(set(Asig)); cols = {s: np.where(Asig == s)[0] for s in sl}
        cA = np.array(cand); sims = X[cA] @ Ad.T
        cls = np.stack([sims[:, cols[s]].max(1) for s in sl], 1)
        ordr = np.argsort(-cls, 1)
        if a.mode == "grow":
            rare = [s for s in sl if len(cols[s]) < a.target]
            quota = max(8, a.n // max(1, len(rare)))
            chosen = {}
            for s in rare:                                     # nearest unlabelled pool words to each rare sig
                si = sl.index(s); taken = 0
                for j in np.argsort(-cls[:, si]):
                    ci = int(cA[j])
                    if ci in chosen: continue
                    chosen[ci] = [s] + [sl[k] for k in ordr[j] if sl[k] != s][:2]   # proposed: this rare sig first
                    taken += 1
                    if taken >= quota: break
            sel = list(chosen.keys()); props_for = chosen; selu = {i: 0.0 for i in sel}
            print(f"GROW: {len(rare)} rare sigs {[f'{s}({len(cols[s])})' for s in rare]}, quota {quota}/sig -> {len(sel)} cards", flush=True)
        elif a.mode == "novelty":
            top1 = sims.max(1)                                 # max sim to ANY anchor; low = novel/unexplored
            novel = cA[np.argsort(top1)[:a.n * 5]]             # the most-novel pool words
            sel = fps(X, list(novel), a.n, int(novel[0]))     # spread across different novel regions
            cpos = {int(cA[j]): j for j in range(len(cA))}
            props_for = {i: [sl[k] for k in ordr[cpos[i], :3]] for i in sel}   # nearest sigs (low-conf reference)
            selu = {i: round(float(1 - top1[cpos[i]]), 3) for i in sel}        # novelty score (higher = newer)
            ns = [float(top1[cpos[i]]) for i in sel]
            print(f"NOVELTY: {len(sel)} farthest-from-anchor words, max-sim {min(ns):.2f}..{max(ns):.2f}", flush=True)
        else:
            top1 = sims.max(1); p = np.exp(cls * 8); p /= p.sum(1, keepdims=True)
            conf = p.max(1); marg = np.take_along_axis(p, ordr[:, :1], 1)[:, 0] - np.take_along_axis(p, ordr[:, 1:2], 1)[:, 0]
            unc = 0.5 * (1 - top1) + 0.25 * (1 - conf) + 0.25 * (1 - marg)
            top = cA[np.argsort(-unc)[:a.n * 4]]; sel = fps(X, list(top), a.n, int(top[0]))
            u_by = {int(cA[j]): float(unc[j]) for j in range(len(cA))}
            prop_by = {int(cA[j]): [sl[k] for k in ordr[j, :3]] for j in range(len(cA))}
            props_for = {i: prop_by.get(i, []) for i in sel}; selu = {i: u_by.get(i, 0.0) for i in sel}
    else:
        sub = cand[:: max(1, len(cand) // 6000)]; seed = max(cand, key=lambda i: score[i])
        sel = fps(X, sub if seed in sub else [seed] + sub, a.n, seed); selu = {i: 0.0 for i in sel}
    print(f"selected {len(sel)} words", flush=True)

    want = {key(meta["gcx"][i], meta["gcy"][i]) for i in sel}; rec = {}
    for f in glob.glob(a.boxes):
        for line in open(f):
            try: r = json.loads(line)
            except Exception: continue
            k = key(r["gcx"], r["gcy"])
            if k in want and k not in rec: rec[k] = r
        if len(rec) >= len(want): break

    cards = []
    for i in sel:
        k = key(meta["gcx"][i], meta["gcy"][i]); r = rec.get(k)
        if r is None: continue
        poly = r.get(a.poly_field) or r.get("gpoly")     # crop the SAME extent the descriptor was taken from
        patch = derotate({"gpoly": poly}) if poly else None
        if patch is None or patch.size < 80: continue
        H, W = patch.shape[:2]; im = Image.fromarray(patch).convert("RGB")
        ImageDraw.Draw(im).rectangle([4, 4, W - 5, H - 5], outline=(214, 40, 40), width=1)
        if max(H, W) < 240: im = im.resize((W * 2, H * 2), Image.NEAREST)
        buf = io.BytesIO(); im.save(buf, "PNG")
        cards.append(dict(gcx=float(meta["gcx"][i]), gcy=float(meta["gcy"][i]), text=str(meta["text"][i]),
                          lon=float(meta["lon"][i]), lat=float(meta["lat"][i]), unc=round(selu.get(i, 0.0), 3),
                          img=base64.b64encode(buf.getvalue()).decode(), proposed=props_for.get(i, [])))
    print(f"cropped {len(cards)} cards", flush=True)
    write_html(cards, a.out, have)
    print(f"wrote {a.out}\nLABELUIDONE", flush=True)

def write_html(cards, out, have):
    sigs = [dict(sig=s, desc=d, ex=sig_exemplar(s)) for s, d in SIG_DESC.items()]
    data = json.dumps(dict(cards=cards, sigs=sigs, mode=("uncertainty" if have else "cold-start")))
    html = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP signature labelling</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee;color:#1a1a1a}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px;z-index:9;display:flex;gap:16px;align-items:center}
 header b{font-size:15px} #stat{opacity:.85}
 .wrap{display:grid;grid-template-columns:1fr 380px}
 #cards{padding:12px;display:flex;flex-direction:column;gap:9px}
 .card{background:#fff;border:1px solid #ddd;border-radius:6px;padding:9px;display:flex;gap:14px;align-items:center}
 .card.done{opacity:.5} .card.act{outline:2px solid #2a7}
 .card img.wd{max-height:60px;image-rendering:pixelated;background:#fbfbf8;border:1px solid #eee;cursor:pointer}
 .meta{min-width:150px;display:flex;flex-direction:column;gap:3px} .meta .t{font-weight:600;font-size:15px}
 .meta .u{color:#a15;font-size:11px} .meta .d{color:#2a7;font-weight:600;font-size:11px} .meta .x{color:#96690a;font-weight:600}
 .acts{display:flex;gap:4px;margin-top:2px} .mini{background:#ded;color:#164;padding:2px 6px;font-size:11px;border-radius:4px;border:0;cursor:pointer}
 .mini.sk{background:#eedcc0;color:#7a5410} .mini:hover{filter:brightness(.94)}
 .props{display:flex;gap:6px;flex-wrap:wrap;flex:1}
 .fc{border:1px solid #cbb;border-radius:5px;padding:2px 7px;cursor:pointer;background:#faf7f4;display:flex;gap:5px;align-items:center;font-size:11px}
 .fc:hover{background:#eafaef} .fc.sel{background:#2a7;color:#fff;border-color:#2a7} .fc img{height:24px;background:#fff} .fc.propose{outline:2px solid #d80}
 #picker{position:sticky;top:38px;height:calc(100vh - 38px);overflow:auto;background:#efe9e2;padding:10px;border-left:1px solid #ccc}
 .pf{display:flex;gap:8px;align-items:center;padding:5px;border-radius:5px;cursor:pointer;margin-bottom:3px;border:1px solid #0000}
 .pf:hover{background:#dff} .pf img{height:34px;background:#fff;border:1px solid #ddd} .pf.act{background:#2a7;color:#fff}
 .pf .sg{font-weight:600} .pf small{opacity:.85}
 button{background:#2a7;color:#fff;border:0;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:13px}
 .hidden{display:none}
 #modal{position:fixed;inset:0;background:rgba(20,18,16,.85);display:none;align-items:center;justify-content:center;z-index:99;cursor:zoom-out}
 #modal.show{display:flex} #modal img{max-width:94vw;max-height:80vh;image-rendering:pixelated;background:#fbfbf8;border:3px solid #fff}
 #modal .cap{position:fixed;bottom:18px;left:0;right:0;text-align:center;color:#f4f2ee;font-size:16px;font-weight:600}
</style>
<header><b>GB-STAMP · signature labelling</b><span id=stat></span>
 <label><input type=checkbox id=hide> hide done</label>
 <button onclick=exportJSON()>Export labels JSON</button></header>
<div class=wrap><div id=cards></div>
<div id=picker><h4 id=ptitle>Click a card, then a signature</h4><div id=pflist></div></div></div>
<div id=modal onclick="this.classList.remove('show')"><img id=modimg><div class=cap id=modcap></div></div>
<script>
const D=__DATA__; let active=null; const labels={}; const SKIP="__skip__";
const sbk={}; D.sigs.forEach(s=>sbk[s.sig]=s);
function chip(s,cls){const f=sbk[s]||{sig:s,desc:''};return `<div class="fc ${cls||''}" data-s="${s}" title="${f.desc}">`+
  (f.ex?`<img src="${f.ex}">`:'')+`<span>${s}</span></div>`;}
function render(){const c=document.getElementById('cards');c.innerHTML='';
  D.cards.forEach((cd,i)=>{const lab=labels[i];const done=lab!=null;const div=document.createElement('div');
    div.className='card'+(done?' done':'')+(document.getElementById('hide').checked&&done?' hidden':'')+(active==i?' act':'');
    div.dataset.i=i;
    div.innerHTML=`<div class=meta><div class=t>${cd.text||'·'}</div>`+(cd.unc?`<div class=u>unc ${cd.unc}</div>`:'')+
      (done&&lab!=SKIP?`<div class=d>✓ ${lab}</div>`:'')+(lab==SKIP?`<div class=x>✗ can't tell</div>`:'')+
      `<div class=acts><button class=mini onclick="event.stopPropagation();zoom(${i})">🔍 enlarge</button>`+
      `<button class=mini onclick="event.stopPropagation();clearRow(${i})">clear</button>`+
      `<button class="mini sk" onclick="event.stopPropagation();skipRow(${i})">can't tell</button></div></div>`+
      `<img class=wd title="click to select this row · 🔍 to enlarge" src="data:image/png;base64,${cd.img}">`+
      `<div class=props>`+(cd.proposed.length?cd.proposed.map((s,j)=>chip(s,(j==0?'propose ':'')+(lab==s?'sel':''))).join(''):
         '<small style="color:#999">cold-start — pick a signature →</small>')+`</div>`;
    div.onclick=e=>{selectCard(i);const ch=e.target.closest('.fc');if(ch)assign(i,ch.dataset.s);};
    c.appendChild(div);});
  const nl=Object.values(labels).filter(v=>v!=SKIP).length,ns=Object.values(labels).filter(v=>v==SKIP).length;
  document.getElementById('stat').textContent=`${D.mode} · ${nl} labelled · ${ns} can't-tell · ${D.cards.length} total`;}
function selectCard(i){active=i;
  document.getElementById('ptitle').textContent=`${D.cards[i].text||'word'} — assign signature`;
  document.getElementById('pflist').innerHTML=D.sigs.map(f=>`<div class="pf ${labels[i]==f.sig?'act':''}" onclick="assign(${i},'${f.sig}')">`+
    (f.ex?`<img src="${f.ex}">`:'<span style=width:34px></span>')+`<div><div class=sg>${f.sig}</div><small>${f.desc}</small></div></div>`).join('');
  render();}
function assign(i,s){labels[i]=s;render();}
function clearRow(i){delete labels[i];render();}
function skipRow(i){labels[i]=SKIP;render();}
function zoom(i){document.getElementById('modimg').src="data:image/png;base64,"+D.cards[i].img;
  document.getElementById('modcap').textContent=D.cards[i].text||'';document.getElementById('modal').classList.add('show');}
document.addEventListener('keydown',e=>{if(e.key=='Escape')document.getElementById('modal').classList.remove('show');});
document.getElementById('hide').onchange=render;
function exportJSON(){const out=Object.keys(labels).map(i=>({gcx:D.cards[i].gcx,gcy:D.cards[i].gcy,
  lon:D.cards[i].lon,lat:D.cards[i].lat,text:D.cards[i].text,sig:labels[i]==SKIP?null:labels[i]}));
  const b=new Blob([JSON.stringify(out,null,1)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='pool_labels_round.json';a.click();}
render();
</script>"""
    open(out, "w").write(html.replace("__DATA__", data))

if __name__ == "__main__":
    main()
