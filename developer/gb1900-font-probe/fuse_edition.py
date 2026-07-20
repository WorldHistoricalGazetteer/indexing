"""Build the GB-STAMP edition — the CLEAN process only. From the CC0 raw dump we take just each label's
RAW TEXT + COORDINATES (never the legacy type_assign tokens). Feature type is derived fresh from:
  (1) the FONT classification (validated hybrid) — the OS style class, and
  (2) a transparent, OS-grounded high-purity lexicon on the label's own words (the anchor terms).
No legacy types are read or fused. Output: gb_stamp.jsonl.

    python fuse_edition.py --gate 0.8
"""
import argparse, json, math, os, re
from collections import Counter, defaultdict

BASE = "/vast/ishi/gb1900/edition"
NT = f"{BASE}/national_typed.jsonl"           # used ONLY for raw text + lon/lat (CC0 raw dump)
FONT = f"{BASE}/spot/boxes_font.jsonl"
XWALK = "/vast/ishi/gb1900/probe/font/gb_stamp_aat_crosswalk.json"
OUT = f"{BASE}/gb_stamp.jsonl"

# FONT-UNCONDITIONAL lexicon: terms whose type is the same regardless of the OS lettering style.
LEXICON = [
    (r"\b(Tumulus|Tumuli|Cairn|Barrow|Earthwork|Cist|Stone Circle|Standing Stone|Site of)\b", "antiquity"),
    (r"\b(Cromlech|Dolmen|Menhir|Hut Circle|Rath|Dun|Broch|Souterrain|Chambered|Tumbeg|Long Barrow|Round Barrow)\b", "antiquity"),
    (r"\b(Priory|Abbey|Motte)\b", "antiquity"),
    (r"\b(Quay|Harbour|Pier|Dock|Jetty|Slip|Lighthouse)\b", "coastal_feature"),
    (r"^(R\.|Afon|Nant)\b|\b(River|Brook|Burn|Beck|Stream|Rivulet)\b", "river"),
    (r"\b(Canal|Aqueduct|Leat|Lade)\b", "canal"),
    (r"\b(Pool|Mere|Tarn|Lake|Loch|Llyn|Reservoir|Pond)\b", "lake"),
    (r"\b(Spring|Well|Fountain|Spout)\b", "spring"),
    (r"\b(Ford|Weir|Sluice|Lock)\b", "water_feature"),
    (r"\b(Church|Chapel|Cathedral|Minster)\b", "church"),
    (r"\b(Wood|Copse|Plantation|Covert|Shaw|Spinney|Grove)\b", "wood"),
    (r"\b(Quarry|Pit|Mine|Colliery|Shaft)\b", "quarry"),
    (r"\b(Mill|Smithy|Forge|Works|Foundry|Kiln)\b", "mill"),
    (r"\b(Farm|Cottage|House|Hall|Lodge|Grange|Manor|Barn)\b", "building_or_feature"),
    (r"\b(Bridge|Viaduct)\b", "bridge"),
    (r"\b(Hill|Moor|Fell|Down|Common|Heath|Bog)\b", "relief"),
]
LEX = [(re.compile(p, re.I), t) for p, t in LEXICON]
# FONT-CONDITIONED lexicon: SAME word -> DIFFERENT type depending on OS lettering style. This is the core
# of GB-STAMP — blackletter marks the antiquity reading; roman/italic marks the modern feature. Starter set;
# the data-driven (term x font_style -> type) analysis (analyze_term_font.py) will extend/verify it.
FONT_COND = [
    (re.compile(r"\b(Camp|Castle|Fort|Tower|Cross|Moat|Battery|Beacon|Chapel|Stone)\b", re.I),
     {"blackletter": "antiquity"}),   # blackletter reading = antiquity; roman/italic falls through to LEX/None
]
# font style -> fallback OS class (when neither lexicon fires)
FONT_CLASS = {"blackletter": "antiquity", "numeral": "elevation"}

def font_cond_token(text, font_style):
    if not font_style: return None
    for rx, m in FONT_COND:
        if rx.search(text or "") and font_style in m: return m[font_style]
    return None

def lexicon_token(text):
    for rx, tok in LEX:
        if rx.search(text or ""): return tok
    return None

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--gate", type=float, default=0.8); a = ap.parse_args()
    xwalk = json.load(open(XWALK)) if os.path.exists(XWALK) else {}
    def aat(tok):
        e = xwalk.get(tok, {}).get("best") if tok else None
        return (e["aat_id"], e["aat_term"]) if e and e.get("aat_id") else (None, None)

    grid = defaultdict(list); nf = 0
    if os.path.exists(FONT):
        for line in open(FONT):
            r = json.loads(line)
            if r["conf"] < a.gate: continue
            grid[(round(r["lon"], 3), round(r["lat"], 3))].append(r); nf += 1
    print(f"confident font boxes (conf>={a.gate}): {nf}", flush=True)

    def nearest_font(lon, lat):
        best, bd = None, 6e-4
        rl, ra = round(lon, 3), round(lat, 3)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for r in grid.get((round(rl + dx * 1e-3, 3), round(ra + dy * 1e-3, 3)), []):
                    dd = math.hypot(r["lon"] - lon, r["lat"] - lat)
                    if dd < bd: bd, best = dd, r
        return best

    fout = open(OUT, "w"); n = nfont = ntyped = 0; fdist = Counter(); tsrc = Counter()
    for line in open(NT):
        try: d = json.loads(line)
        except Exception: continue
        lon, lat = d.get("lon"), d.get("lat")
        tv = d.get("text"); text = tv.get("value") if isinstance(tv, dict) else tv
        fb = nearest_font(lon, lat) if lon is not None else None
        font_style = fb["font"] if fb else None; font_conf = fb["conf"] if fb else None
        if font_style: nfont += 1; fdist[font_style] += 1
        # CLEAN typing: font-CONDITIONED term first (the GB-STAMP disambiguation), then font-unconditional
        # lexicon, then font-class fallback.
        tok = font_cond_token(text, font_style); src = "font_conditioned" if tok else None
        if not tok:
            tok = lexicon_token(text); src = "lexicon" if tok else None
        if not tok and font_style in FONT_CLASS: tok, src = FONT_CLASS[font_style], "font"
        if tok: ntyped += 1; tsrc[src] += 1
        aid, aterm = aat(tok)
        rec = dict(place_id=d.get("place_id"), text=text, lon=lon, lat=lat,
                   type=tok, type_source=src, font_style=font_style, font_conf=font_conf,
                   aat_id=aid, aat_term=aterm)
        fout.write(json.dumps(rec, ensure_ascii=False) + "\n"); n += 1
        if n % 500000 == 0: print(f"  {n} written; font {nfont}; typed {ntyped}", flush=True)
    fout.close()
    print(f"FUSEDONE {n} records -> {OUT}; font-enriched {nfont} ({nfont/max(1,n)*100:.1f}%); "
          f"typed {ntyped} ({ntyped/max(1,n)*100:.1f}%) src {dict(tsrc)}; font dist {dict(fdist)}", flush=True)

if __name__ == "__main__":
    main()
