"""Build the Characteristic Sheet VERIFICATION TABLE (GB-STAMP Phase A) for human review.

Self-contained HTML: the OS 1897 + c.1923 Characteristic Sheet font taxonomy (NLS, CC-BY), grounded on
high-res IIIF crops (reference/cs_*.jpg), tabulated for the font classifier. Groups the OS categories by
DISTINGUISHABLE typographic STYLE (the training classes), with per-style exemplar crops + metadata flags
(caps-only, size-variable, date-regime) for a human to verify before anything is trained on it.

    python build_cs_table.py   ->  characteristic_sheet_table.html
"""
import base64, os, json, html

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, "reference")

def b64(fn):
    p = os.path.join(REF, fn)
    if not os.path.exists(p): return None
    return "data:image/jpeg;base64," + base64.b64encode(open(p, "rb").read()).decode()

# ---- the sheet-grounded STYLE taxonomy (training classes) ----
# each: key, human label, the OS categories it covers, exemplar crop, and notes/flags.
STYLES = [
    dict(key="admin_caps", label="Administrative — serif CAPS",
         crop="cs_admincaps_top.jpg", crop2="cs_admincaps_mid.jpg",
         covers="County Names, Hundreds†, Divisions of Counties/Ridings†, Poor Law Unions, Urban Sanitary "
                "Districts, Liberties, Parishes (Mother/Ancient)†, Civil Parishes/Townships, Divisions & "
                "Subdivisions of Townships†, Boroughs (Parl./Munic.), Cities returning Members†/not†, Wards, "
                "Market Towns†, Other Towns†, Towns generally, Town Districts‡, Parliamentary Div. of "
                "Counties‡, County Boroughs‡, Extra Parochial†, Turnpike Trusts†",
         note="Size encodes administrative rank (fixed per rank). The single letter (C/H/R/L/P/T/B/W/D/E) is "
              "the boundary MARK on the line, not the label font — the label font is caps. Level is "
              "resolved by SIZE + TEXT, not font. Many are † (pre-1879) or ‡ (recent maps only).",
         style_class="caps"),
    dict(key="settlement_upright", label="Settlement / building — roman UPRIGHT",
         crop="cs_settlement.jpg",
         covers="Parish Churches & Villages, Workhouses, Isolated Houses, County/Trust Bridges, Towns generally",
         note="The default place-name face. Upright vs italic is only ~3° of slant — NOT reliably separable "
              "from font alone (validated ~0.71 ceiling); disambiguated by word-semantics + size.",
         style_class="upright"),
    dict(key="settlement_italic", label="Minor settlement / industry — roman ITALIC",
         crop="cs_settlement.jpg",
         covers="Chapelries & Other Churches†, Other Villages, Gentlemens Seats, Manufactories/Mines/Farms/Locks",
         note="Italic distinguishes minor/subordinate places from principal ones. Mild slant; see caveat above.",
         style_class="upright"),
    dict(key="parks_caps", label="Parks & Demesnes — bold CAPS",
         crop="cs_settlement.jpg",
         covers="Parks and Demesnes",
         note="Bold caps; visually near the admin caps class.", style_class="caps"),
    dict(key="water_italic", label="Water — ITALIC",
         crop="cs_water.jpg",
         covers="Navigable Rivers & Canals (italic caps), Small Rivers & Brooks (italic l/c)",
         note="A PRONOUNCED cursive italic — much more slanted than settlement italic, hence the one serif "
              "class that re-grounds to usable separability (0.72 vs 0.14). Railway-Mineral shares this "
              "italic → split by TEXT. Bays & Harbours are UPRIGHT caps, not italic.",
         style_class="italic"),
    dict(key="land_caps_sizevar", label="Bogs / Moors / Forests — CAPS, size-variable*",
         crop="cs_water.jpg",
         covers="Bogs, Moors and Forests†",
         note="* SIZE varies with extent/importance (one of only TWO size-variable groups). Subordinate parts "
              "engraved in Roman print or stump character per extent.", style_class="caps"),
    dict(key="hills_sizevar", label="Ranges of Hills — size-variable*",
         crop="cs_hills.jpg",
         covers="Ranges of Hills (separate parts / single features)",
         note="* The other size-variable group. All OTHER categories have FIXED canonical size → size is a "
              "reliable discriminator everywhere except here and Bogs/Moors/Forests.", style_class="upright"),
    dict(key="woods", label="Woods & Copses",
         crop=None, covers="Woods and Copses", note="Capture crop pending.", style_class="upright"),
    dict(key="antiquity_roman", label="Antiquities: ROMAN — serif caps",
         crop="cs_antiq.jpg",
         covers="Antiquities (Roman)  —  SAME FACE AS ROAD NAMES",
         note="Roman antiquities and road names share this serif-caps face → font alone can't separate them; "
              "text/context must.", style_class="caps"),
    dict(key="antiquity_saxon", label="Antiquities: PRE-HISTORIC or SAXON — blackletter",
         crop="cs_antiq.jpg", covers="Antiquities (Pre-historic or Saxon)",
         note="Gothic/blackletter. Hard to separate from the Norman blackletter (the middle pair).",
         style_class="blackletter"),
    dict(key="antiquity_norman", label="Antiquities: NORMAN — blackletter (Old English)",
         crop="cs_antiq.jpg", covers="Antiquities (Norman)",
         note="A heavier Old-English blackletter. Middle pair with Saxon — expect confusion.",
         style_class="blackletter"),
    dict(key="antiquity_subsequent", label="Antiquities: SUBSEQUENT — italic",
         crop="cs_antiq.jpg", covers="Antiquities (Subsequent)",
         note="Fourth antiquity font (italic serif). Distinct from the two blackletters and from Roman caps.",
         style_class="italic"),
    dict(key="railway_passenger", label="Railways (Passenger) + Stations — upright",
         crop="cs_railways.jpg", covers="Railways (Passenger), Principal Stations, Other Stations",
         style_class="upright", note="Upright."),
    dict(key="railway_mineral", label="Railways (Mineral) — italic",
         crop="cs_railways.jpg", covers="Railways (Mineral)",
         note="Italic — shares the water italic face; split from water by TEXT.", style_class="italic"),
    dict(key="numeral", label="Contour altitudes / spot heights — NUMERAL",
         crop="cs1923_altitudes.jpg", covers="Contour altitudes (…200), spot heights (7·)",
         note="From the c.1923 sheet. Handled well by the text typer (pure digits).", style_class="numeral"),
    dict(key="benchmark", label="Bench Mark — B.M.",
         crop="cs1923_altitudes.jpg", covers="B.M. on Buildings, Walls, Milestones &c. (Bench Marks)",
         note="From the c.1923 sheet. Feeds the abbreviation lexicon (B.M.).", style_class="caps"),
]

# collapse to the 5 visually-distinct classes the classifier actually predicts
CLASS_COLOR = {"caps": "#5b7", "upright": "#69c", "italic": "#c85", "blackletter": "#a6a", "numeral": "#999"}

# ---- boundary / abbreviation examples (feed the TEXT lexicon, NOT the font classifier) ----
BOUNDARY = [
    ("C.S.", "Centre of Stream"), ("C.R.", "Centre of Road"), ("C.F.", "Centre of Fence"),
    ("S.D.", "Side of Drain"), ("R.H.", "Root of Hedge"), ("F.C.", "Face of Cap"),
    ("S.F.", "Side of Fence"), ("F.W.", "Face of Wall"), ("4′R.H.", "4 feet from Root of Hedge"),
    ("T.C.", "Top of Cap"), ("C.W.", "Centre of Wall"),
    ("Co. Boro. Bdy.", "County Borough Boundary (c.1923 sheet)"),
    ("M.S", "Mile Stone (c.1923; single period ambiguous)"), ("B.M.", "Bench Mark"),
]

def crop_cell(fn, fn2=None):
    imgs = ""
    for f in (fn, fn2):
        if f:
            d = b64(f)
            if d: imgs += f'<img src="{d}" alt="{html.escape(f)}">'
    return imgs or '<span class="pending">crop pending</span>'

def main():
    rows = ""
    for s in STYLES:
        cc = s["style_class"]; col = CLASS_COLOR[cc]
        rows += f"""<tr>
          <td class="crop">{crop_cell(s.get('crop'), s.get('crop2'))}</td>
          <td><b>{html.escape(s['label'])}</b>
              <span class="pill" style="background:{col}">{cc}</span>
              <div class="note">{html.escape(s['note']) if s.get('note') else ''}</div></td>
          <td class="covers">{html.escape(s['covers'])}</td>
          <td class="verify"><label><input type="checkbox"> ok</label><textarea placeholder="correction / note"></textarea></td>
        </tr>"""

    brows = "".join(f"<tr><td class=abbr>{html.escape(a)}</td><td>{html.escape(m)}</td></tr>" for a, m in BOUNDARY)

    strips = "".join(f'<figure><img src="{b64(f)}"><figcaption>{f}</figcaption></figure>'
                     for f in ["cs_900.jpg","cs_1800.jpg","cs_2700.jpg","cs_3600.jpg","cs_4500.jpg",
                               "cs_footnotes.jpg","cs1923_overview.jpg"] if b64(f))

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OS Characteristic Sheet — GB-STAMP font taxonomy (verification)</title>
<style>
  :root {{ --bg:#faf8f3; --fg:#221; --mut:#6b6459; --line:#e2ddd2; --card:#fff; --accent:#8a5a2b; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#17150f; --fg:#e8e2d5; --mut:#a49b88; --line:#332f26; --card:#201d16; --accent:#d8a15e; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }}
  header {{ padding:28px 32px 14px; border-bottom:2px solid var(--accent); }}
  h1 {{ margin:0 0 4px; font-size:23px; }}
  h2 {{ margin:34px 32px 10px; font-size:18px; border-left:3px solid var(--accent); padding-left:10px; }}
  .sub {{ color:var(--mut); font-size:13.5px; max-width:62em; }}
  .facts {{ margin:14px 32px; display:flex; gap:14px; flex-wrap:wrap; }}
  .fact {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:9px 13px; font-size:13px; }}
  .fact b {{ color:var(--accent); }}
  table {{ border-collapse:collapse; margin:0 32px; width:calc(100% - 64px); background:var(--card); }}
  th,td {{ border:1px solid var(--line); padding:9px 11px; text-align:left; vertical-align:top; }}
  th {{ background:color-mix(in srgb, var(--accent) 12%, transparent); font-size:12.5px; text-transform:uppercase; letter-spacing:.04em; }}
  td.crop {{ width:280px; }}
  td.crop img {{ max-width:270px; display:block; margin:3px 0; border:1px solid var(--line); border-radius:3px; background:#fff; }}
  .covers {{ font-size:13px; color:var(--mut); max-width:22em; }}
  .note {{ font-size:12px; color:var(--mut); margin-top:5px; }}
  .pill {{ color:#fff; border-radius:10px; padding:1px 8px; font-size:11px; margin-left:6px; vertical-align:middle; }}
  .verify {{ width:150px; }}
  .verify textarea {{ width:100%; height:40px; margin-top:5px; font:12px sans-serif; border:1px solid var(--line); border-radius:4px; background:var(--bg); color:var(--fg); }}
  .abbr {{ font-family:ui-monospace,monospace; font-weight:600; white-space:nowrap; }}
  figure {{ margin:10px 32px; }}
  figure img {{ max-width:100%; border:1px solid var(--line); border-radius:4px; background:#fff; }}
  figcaption {{ font-size:11.5px; color:var(--mut); }}
  .btable {{ width:auto; }} .btable td {{ font-size:13px; }}
</style></head><body>
<header>
  <h1>OS Characteristic Sheet → GB-STAMP font taxonomy <span style="font-weight:400;color:var(--mut)">(human verification)</span></h1>
  <div class="sub">Source: OS <i>“Examples for the Characters of the Writing on the Engraved Six-Inch Ordnance Maps of Great Britain”</i>,
  1897 (NLS <a href="https://maps.nls.uk/view/128076792">view/128076792</a>) + c.1923 “Conventional Signs &amp; Writing”, Plate IV
  (NLS <a href="https://maps.nls.uk/view/128076894">view/128076894</a>). CC-BY, National Library of Scotland. Crops via IIIF.
  The classifier predicts a typographic <b>STYLE</b> (right-hand pill = the 5 visually-distinct classes it actually
  separates); a later fusion maps (style × text × size × date) → feature-type. <b>Please verify each row and note corrections.</b></div>
  <div class="facts">
    <div class="fact"><b>†</b> = characters only on six-inch maps published <b>before 1879</b> (per the sheet's own footnote)</div>
    <div class="fact"><b>‡</b> = “on the more recent maps” (added categories)</div>
    <div class="fact"><b>*</b> = size varies with extent — ONLY Bogs/Moors/Forests + Hills; all others fixed</div>
    <div class="fact">GB1900 (EDITION 2, 16,450 sheets): <b>99.7% published ≥1879</b> (95.6% ≥1897); only <b>0.3% pre-1879</b>.
      Plan: build the <b>≥1879</b> classifier first, then <b>adapt a &lt;1879 classifier from it</b> (the † letterforms genuinely differ).</div>
  </div>
</header>

<h2>Font-style taxonomy (training classes)</h2>
<table>
 <tr><th>Exemplar (Characteristic Sheet)</th><th>Style class → OS meaning</th><th>OS categories covered</th><th>Verify</th></tr>
 {rows}
</table>

<h2>Boundary &amp; abbreviation labels → TEXT lexicon (not the font classifier)</h2>
<div class="sub" style="margin:0 32px 8px">From the 1897 boundary specimens + c.1923 sheet. These are abbreviation-lexicon items (weighted
distributions where ambiguous, e.g. M.S), tabulated separately from the a–z NLS abbreviation pages.</div>
<table class="btable"><tr><th>Abbrev</th><th>Meaning</th></tr>{brows}</table>

<h2>Provenance strips (full-sheet context)</h2>
{strips}
</body></html>"""
    out = os.path.join(HERE, "characteristic_sheet_table.html")
    open(out, "w").write(doc)
    print("WROTE", out, f"({len(doc)//1024} KB)")

if __name__ == "__main__":
    main()
