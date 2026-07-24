"""Does GB1900 actually PIN the distinctive-font categories? The single fact that decides Phase A's shape.

Pin-prompted localisation inherits GB1900's own coverage: whatever the volunteers never pinned, we can never
crop. The categories that carry the font signal (admin caps, antiquities, water) are exactly the ones that are
hard to READ, and the same difficulty that made MapReader's word spotter skip them could have made crowd
transcribers skip them. If so, an AMG sweep regains a primary role for completeness.

This measures PREVALENCE — how much material each category has in the gazetteer, corpus-wide and spatially
spread. It deliberately does NOT claim to measure RECALL against what is printed on the sheets; that needs an
AMG sweep as ground truth on sample sheets and is a separate job. Prevalence is still decisive for the paper,
because the paper needs 30-50 verified examples per signature, not exhaustive coverage.

Categories come from the existing lexicons (make_alphabet_ui.LEX, make_font_testset_v2 ANTIQ/WATER) so the weak
labels used here are the same ones the labelling bootstrap will use — no second, divergent vocabulary.

    python pin_category_coverage.py --out /vast/ishi/gb1900/probe/font/pin_category_coverage.json
"""
import argparse, json, os, re, sys, numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_pin_index import load_pins

# Same hints as make_alphabet_ui.LEX, grouped to the OS Characteristic-Sheet families that differ in TYPEFACE
# rather than to individual faces — the signature is what the descriptor has to separate.
CATS = [
    ("water",       r"\b(River|Brook|Burn|Beck|Nant|Afon|Stream|Canal|Navigation|Well|Pool|Mere|Lake|Ford|Water|Dyke|Reservoir|Loch|Llyn)\b"),
    ("antiquities", r"\b(Tumul\w*|Cairn|Barrow|Earthwork|Camp|Cross|Castle|Priory|Abbey|Moat|Tower|Roman|Site of|Stone Circle|Fort)\b"),
    ("woods",       r"\b(Wood|Copse|Plantation|Covert|Spinney|Forest)\b"),
    ("seats",       r"\b(Hall|Lodge|House|Ho\.|Grange|Court|Manor|Villa)\b"),
    ("churches",    r"\b(Church|Chapel|Ch\.|Vicarage|Rectory)\b"),
    ("works",       r"\b(Mill|Works|Factory|Colliery|Foundry|Quarry|Pit|Brick\w*)\b"),
    ("stations",    r"\b(Station|Sta\.|Junction|Signal Box)\b"),
    ("hills",       r"\b(Hill|Down|Fell|Tor|Ridge|Beacon|Moor|Common|Marsh|Fen|Heath)\b"),
    ("roads",       r"\b(Road|Street|Lane|Rd\.|St\.|Avenue|Terrace)\b"),
    ("boundaries",  r"\b(Boro|Bory|Par|Parish|Co\.? Const|Div|Ward|U\.? ?D|R\.? ?D|C\.? ?P|Bdy|Boundary|Detached)\b"),
]
COMPILED = [(name, re.compile(pat, re.I)) for name, pat in CATS]
# Letter-spaced ALLCAPS is the ADMIN signature on the six-inch sheets, and it is not lexical — a county or
# parish name is just a proper noun. Shape is the only corpus-wide handle we have on it without a gazetteer join.
CAPS = re.compile(r"^[A-Z][A-Z\s\.\'&-]{3,}$")
# ...but raw ALLCAPS is swamped by the map's abbreviation furniture (F. P. = footpath, S. P. = signal post,
# B. M. = bench mark), which is set in small caps, not the spaced admin face. Require every token to be a real
# word: alphabetic, 3+ letters. That leaves proper-noun caps — where parish/county/town names actually live.
CAPS_WORDS = re.compile(r"^[A-Z][A-Z\s'&-]+$")


def is_admin_shape(t):
    if not CAPS_WORDS.match(t):
        return False
    toks = [w for w in re.split(r"[\s&-]+", t) if w]
    return len(toks) > 0 and all(len(w.strip("'")) >= 3 for w in toks) and len(t) >= 5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pins", default="/vast/ishi/gb1900/pins_z17.npz")
    ap.add_argument("--admin-names", default="/vast/ishi/gb1900/probe/font/admin_names.json",
                    help="known administrative unit names (from fetch_vob_admin_names.py), if present")
    ap.add_argument("--out", default="/vast/ishi/gb1900/probe/font/pin_category_coverage.json")
    ap.add_argument("--examples", type=int, default=25)
    a = ap.parse_args()

    P = load_pins(a.pins)
    txt = P["text"].astype(str)
    n = len(txt)
    # ~10 km cells: how widely a category is spread decides whether a stratified sample can span the country
    # rather than over-sampling one dense city.
    cell = (P["gx"] // 13000).astype(np.int64) * 100000 + (P["gy"] // 13000).astype(np.int64)
    print(f"{n} pins", flush=True)

    admin_hits = None
    if os.path.exists(a.admin_names):
        try:
            names = json.load(open(a.admin_names))
            if isinstance(names, dict):
                names = list(names.keys())
            NAMES = {str(x).strip().upper() for x in names if str(x).strip()}
            admin_hits = np.array([t.strip().upper() in NAMES for t in txt])
            print(f"admin gazetteer: {len(NAMES)} names", flush=True)
        except Exception as e:
            print(f"admin names unusable ({e}) — skipping that column", flush=True)

    rows = {}
    for name, rx in COMPILED:
        m = np.array([bool(rx.search(t)) for t in txt])
        caps = m & np.array([bool(CAPS.match(t)) for t in txt])
        rows[name] = dict(
            pins=int(m.sum()), share=round(float(m.mean()), 4),
            allcaps_pins=int(caps.sum()), cells_10km=int(len(np.unique(cell[m]))),
            examples=[str(t) for t in txt[m][:: max(1, int(m.sum()) // a.examples)][:a.examples]],
        )
        print(f"  {name:12s} {rows[name]['pins']:>8d} pins  ({rows[name]['share']:.2%})  "
              f"allcaps {rows[name]['allcaps_pins']:>7d}  spread {rows[name]['cells_10km']} cells", flush=True)

    capsm = np.array([bool(CAPS.match(t)) for t in txt])
    lexical = np.zeros(n, bool)
    for _, rx in COMPILED:
        lexical |= np.array([bool(rx.search(t)) for t in txt])
    # ALLCAPS proper nouns with no descriptive word in them — the residue where admin/place-name labels
    # concentrate. Abbreviation furniture is excluded by is_admin_shape, not by the lexicons.
    admin_shape = np.array([is_admin_shape(t) for t in txt]) & ~lexical
    rows["_allcaps_nonlexical"] = dict(
        pins=int(admin_shape.sum()), share=round(float(admin_shape.mean()), 4),
        cells_10km=int(len(np.unique(cell[admin_shape]))),
        examples=[str(t) for t in txt[admin_shape][:: max(1, int(admin_shape.sum()) // a.examples)][:a.examples]],
    )
    print(f"  {'ADMIN-shape':12s} {rows['_allcaps_nonlexical']['pins']:>8d} pins "
          f"({rows['_allcaps_nonlexical']['share']:.2%})  spread {rows['_allcaps_nonlexical']['cells_10km']} cells",
          flush=True)
    if admin_hits is not None:
        rows["_admin_gazetteer_match"] = dict(
            pins=int(admin_hits.sum()), share=round(float(admin_hits.mean()), 4),
            cells_10km=int(len(np.unique(cell[admin_hits]))),
            examples=[str(t) for t in txt[admin_hits][:: max(1, int(admin_hits.sum()) // a.examples)][:a.examples]],
        )
        print(f"  {'ADMIN-gaz':12s} {rows['_admin_gazetteer_match']['pins']:>8d} pins", flush=True)

    out = dict(total_pins=n, allcaps_pins=int(capsm.sum()), lexical_pins=int(lexical.sum()), categories=rows)
    json.dump(out, open(a.out, "w"), indent=2, ensure_ascii=False)
    print(f"wrote {a.out}", flush=True)
    print("COVERAGEDONE", flush=True)


if __name__ == "__main__":
    main()
