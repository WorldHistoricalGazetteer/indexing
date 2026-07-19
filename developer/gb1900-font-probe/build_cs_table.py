"""Build the Characteristic Sheet VERIFICATION TABLE (GB-STAMP Phase A) for human review.

Self-contained HTML: EVERY OS 1897 + c.1923 Characteristic Sheet category as its OWN row, with its OWN
tight exemplar (single boundary-mark LETTER for admin; the transcribed example line for the rest — cropped
by fetch_exemplars.py -> reference/ex_*.jpg), the OS's transcribed label, the 5-class visual style, and
the flags the classifier needs: caps-only (all-capital writing — NOT "single letter"), size-variable,
and date-regime († = the shown form is pre-1879; ‡ = "on the more recent maps" i.e. >=1879).

    python build_cs_table.py   ->  characteristic_sheet_table.html
"""
import base64, os, html

HERE = os.path.dirname(os.path.abspath(__file__)); REF = os.path.join(HERE, "reference")

def b64(fn):
    p = os.path.join(REF, fn)
    return "data:image/jpeg;base64," + base64.b64encode(open(p, "rb").read()).decode() if os.path.exists(p) else None

# style class -> colour (the 5 visually-distinct classes the classifier actually predicts)
COL = {"caps": "#4f8a5b", "upright": "#3f6fa8", "italic": "#c07a2b", "blackletter": "#8a4fa0", "numeral": "#777"}

# section, then rows: (key/exemplar, transcribed label, letterform, style, caps_only, size_var, regime, note)
#   regime: "any" (invariant) | "pre1879" († shown form is <1879) | "recent" (‡ >=1879, 'more recent maps')
SECTIONS = [
 ("Administrative units — exemplar is the single boundary-MARK capital (label writing is in capitals)", [
  ("ex_county_names","County Names","C","caps",True,False,"any",""),
  ("ex_hundreds","Hundreds","H","caps",True,False,"pre1879",""),
  ("ex_liberties","Liberties","L","italic",True,False,"any",""),
  ("ex_parishes_ancient","Parishes (Mother or Ancient)","P","caps",True,False,"pre1879",""),
  ("ex_civil_parishes","Civil Parishes or Townships","T","caps",True,False,"any",""),
  ("ex_div_townships","Divisions of Townships","T","italic",True,False,"pre1879","bold italic mark"),
  ("ex_subdiv_townships","Subdivisions of Do.","T","italic",True,False,"pre1879",""),
  ("ex_boroughs_parl","Boroughs (Parliamentary)","B","caps",True,False,"any",""),
  ("ex_boroughs_munic","Boroughs (Municipal)","B","caps",True,False,"any",""),
  ("ex_towns_generally","Towns, generally","B","italic",True,False,"any",""),
  ("ex_town_districts","Town Districts","D","italic",True,False,"recent",""),
  ("ex_div_counties","Divisions of Counties (Ridings)","R","caps",True,False,"pre1879",""),
  ("ex_poor_law_unions","Poor Law Unions","R","caps",True,False,"any","old Yorks/Lancs maps = Registrars Districts"),
  ("ex_urban_sanitary","Urban Sanitary Districts","R","caps",True,False,"any",""),
  ("ex_cities_mp","Cities returning Members","C","caps",True,False,"pre1879","bold mark"),
  ("ex_cities_nomp","Cities not returning Members","C","caps",True,False,"pre1879",""),
  ("ex_wards","Wards","W","caps",True,False,"any",""),
  ("ex_market_towns","Market Towns","B","italic",True,False,"pre1879",""),
  ("ex_other_towns","Other Towns","B","italic",True,False,"pre1879",""),
  ("ex_parl_div_counties","Parliamentary Division of Counties","P","caps",True,False,"recent",""),
  ("ex_county_boroughs","County Boroughs","E","caps",True,False,"recent",""),
  ("ex_extra_parochial","Extra Parochial","","caps",True,False,"pre1879",""),
  ("ex_turnpike_trusts","Turnpike Trusts","","italic",True,False,"pre1879","bold italic caps"),
 ]),
 ("Settlement & buildings", [
  ("ex_parish_churches","Parish Churches & Villages","","upright",False,False,"any",""),
  ("ex_chapelries","Chapelries. Other Churches","","italic",False,False,"pre1879",""),
  ("ex_other_villages","Other Villages","","italic",False,False,"any",""),
  ("ex_parks_demesnes","Parks and Demesnes","","caps",True,False,"any","bold caps ('and' l/c)"),
  ("ex_gentlemens_seats","Gentlemens Seats","","italic",False,False,"any",""),
  ("ex_manufactories","Manufactories. Mines. Farms. Locks","","italic",False,False,"any",""),
  ("ex_workhouses","Workhouses","","upright",False,False,"any",""),
  ("","County Bridges. Trust Bridges & Others. Isolated Houses","","upright",False,False,"any","crop pending"),
 ]),
 ("Water — three DISTINCT classes (not all italic)", [
  ("ex_bays_harbours","Bays and Harbours","","caps",True,False,"any","UPRIGHT caps — NOT italic"),
  ("ex_navigable_rivers","Navigable Rivers and Canals","","italic",True,False,"any","italic CAPS"),
  ("ex_small_rivers","Small Rivers & Brooks","","italic",False,False,"any","italic lower-case"),
 ]),
 ("Land & relief", [
  ("ex_bogs_moors","Bogs, Moors and Forests","","caps",True,True,"pre1879","* size varies with extent"),
  ("ex_woods_copses","Woods and Copses","","upright",False,False,"any",""),
  ("ex_ranges_hills","Ranges of Hills","","caps",True,True,"any","* size varies with extent"),
 ]),
 ("Antiquities — FOUR distinct fonts (middle two hard to separate)", [
  ("ex_antiq_roman","Antiquities: Roman","","caps",True,False,"any","SAME face as ROAD names"),
  ("ex_antiq_saxon","Antiquities: Pre-historic or Saxon","","blackletter",False,False,"any",""),
  ("ex_antiq_norman","Antiquities: Norman","","blackletter",False,False,"any","Old-English blackletter"),
  ("ex_antiq_subsequent","Antiquities: Subsequent","","italic",False,False,"any",""),
 ]),
 ("Railways & stations", [
  ("ex_railways_passenger","Railways (Passenger)","","upright",False,False,"any",""),
  ("ex_railways_mineral","Railways (Mineral)","","italic",False,False,"any","shares the water italic"),
  ("ex_principal_stations","Principal Stations","","upright",False,False,"any",""),
  ("ex_other_stations","Other Stations","","upright",False,False,"any",""),
 ]),
 ("Heights & bench marks (c.1923 sheet)", [
  ("","Contour altitudes (…200) / spot heights (7·)","","numeral",False,False,"any","numeral font — text typer handles"),
  ("","Bench Mark (B.M. on buildings, walls, milestones)","","caps",False,False,"any","→ abbreviation lexicon"),
 ]),
]

REGIME = {"any": ('<span class="rg any">any (invariant)</span>'),
          "pre1879": ('<span class="rg pre">† &lt;1879 form</span>'),
          "recent": ('<span class="rg rec">‡ ≥1879 (recent maps)</span>')}

BOUNDARY = [("C.S.","Centre of Stream"),("C.R.","Centre of Road"),("C.F.","Centre of Fence"),
    ("S.D.","Side of Drain"),("R.H.","Root of Hedge"),("F.C.","Face of Cap"),("S.F.","Side of Fence"),
    ("F.W.","Face of Wall"),("4′R.H.","4 feet from Root of Hedge"),("T.C.","Top of Cap"),("C.W.","Centre of Wall"),
    ("Co. Boro. Bdy.","County Borough Boundary (c.1923)"),("M.S","Mile Stone (c.1923; single period ambiguous)"),
    ("B.M.","Bench Mark")]

def main():
    n = 0; body = ""
    for title, rows in SECTIONS:
        body += f'<tr class="sec"><td colspan="6">{html.escape(title)}</td></tr>'
        for key, label, letter, style, caps_only, size_var, regime, note in rows:
            n += 1
            d = b64(key + ".jpg") if key else None
            img = f'<img src="{d}" alt="{html.escape(label)}">' if d else '<span class="pending">crop pending</span>'
            lf = f'<b class="lf">{html.escape(letter)}</b>' if letter else ''
            flags = ""
            if caps_only: flags += '<span class="fl caps">CAPS</span>'
            if size_var: flags += '<span class="fl sz">size*</span>'
            body += f"""<tr>
              <td class="num">{n}</td>
              <td class="crop">{img}</td>
              <td class="cat"><b>{html.escape(label)}</b> {lf}
                  <div class="note">{html.escape(note)}</div></td>
              <td><span class="pill" style="background:{COL[style]}">{style}</span><div class="flags">{flags}</div></td>
              <td class="reg">{REGIME[regime]}</td>
              <td class="verify"><label><input type=checkbox> ok</label><textarea placeholder="correction"></textarea></td>
            </tr>"""

    brows = "".join(f"<tr><td class=abbr>{html.escape(a)}</td><td>{html.escape(m)}</td></tr>" for a, m in BOUNDARY)
    strips = "".join(f'<figure><img src="{b64(f)}"><figcaption>{f}</figcaption></figure>'
                     for f in ["cs_900.jpg","cs_1800.jpg","cs_2700.jpg","cs_3600.jpg","cs_4500.jpg",
                               "cs_footnotes.jpg","cs1923_overview.jpg"] if b64(f))

    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>OS Characteristic Sheet — GB-STAMP font taxonomy (verification)</title>
<style>
 :root {{ --bg:#faf8f3; --fg:#221e18; --mut:#6b6459; --line:#e4dfd4; --card:#fff; --accent:#8a5a2b; }}
 @media (prefers-color-scheme:dark) {{ :root {{ --bg:#16140f; --fg:#e9e3d6; --mut:#a8a08d; --line:#332f26; --card:#1f1c15; --accent:#d8a15e; }} }}
 :root[data-theme=light] {{ --bg:#faf8f3; --fg:#221e18; --mut:#6b6459; --line:#e4dfd4; --card:#fff; --accent:#8a5a2b; }}
 :root[data-theme=dark] {{ --bg:#16140f; --fg:#e9e3d6; --mut:#a8a08d; --line:#332f26; --card:#1f1c15; --accent:#d8a15e; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }}
 header {{ padding:26px 30px 12px; border-bottom:2px solid var(--accent); }}
 h1 {{ margin:0 0 5px; font-size:22px; }}
 .sub {{ color:var(--mut); font-size:13.5px; max-width:64em; }}
 .legend {{ margin:13px 30px; display:flex; gap:11px; flex-wrap:wrap; }}
 .legend div {{ background:var(--card); border:1px solid var(--line); border-radius:7px; padding:7px 11px; font-size:12.5px; }}
 .legend b {{ color:var(--accent); }}
 .wrap {{ overflow-x:auto; }}
 table {{ border-collapse:collapse; margin:0 30px; width:calc(100% - 60px); background:var(--card); }}
 th,td {{ border:1px solid var(--line); padding:8px 10px; text-align:left; vertical-align:middle; }}
 th {{ background:color-mix(in srgb,var(--accent) 12%,transparent); font-size:12px; text-transform:uppercase; letter-spacing:.04em; position:sticky; top:0; }}
 tr.sec td {{ background:color-mix(in srgb,var(--accent) 8%,transparent); font-weight:600; font-size:13.5px; }}
 td.num {{ color:var(--mut); font-variant-numeric:tabular-nums; width:34px; }}
 td.crop {{ width:250px; }}
 td.crop img {{ max-width:240px; max-height:82px; display:block; border:1px solid var(--line); border-radius:3px; background:#fff; }}
 .cat b {{ font-size:14.5px; }} .lf {{ color:var(--accent); font-size:17px; margin-left:5px; }}
 .note {{ font-size:12px; color:var(--mut); margin-top:2px; }}
 .pill {{ color:#fff; border-radius:10px; padding:2px 9px; font-size:11.5px; }}
 .flags {{ margin-top:5px; }}
 .fl {{ font-size:10.5px; border-radius:4px; padding:1px 5px; margin-right:3px; border:1px solid var(--line); }}
 .fl.caps {{ background:color-mix(in srgb,#4f8a5b 22%,transparent); }} .fl.sz {{ background:color-mix(in srgb,#c07a2b 22%,transparent); }}
 .rg {{ font-size:11.5px; }} .rg.pre {{ color:#b8532b; }} .rg.rec {{ color:#2b7ab8; }} .rg.any {{ color:var(--mut); }}
 .verify {{ width:140px; }}
 .verify textarea {{ width:100%; height:34px; margin-top:4px; font:12px sans-serif; border:1px solid var(--line); border-radius:4px; background:var(--bg); color:var(--fg); }}
 .pending {{ color:var(--mut); font-style:italic; font-size:12px; }}
 .abbr {{ font-family:ui-monospace,monospace; font-weight:600; white-space:nowrap; }}
 h2 {{ margin:32px 30px 8px; font-size:17px; border-left:3px solid var(--accent); padding-left:9px; }}
 .btable {{ width:auto; }} .btable td {{ font-size:13px; }}
 figure {{ margin:9px 30px; }} figure img {{ max-width:100%; border:1px solid var(--line); border-radius:4px; background:#fff; }}
 figcaption {{ font-size:11.5px; color:var(--mut); }}
</style></head><body>
<header>
 <h1>OS Characteristic Sheet → GB-STAMP font taxonomy <span style="font-weight:400;color:var(--mut)">(human verification)</span></h1>
 <div class="sub">Every OS category as its own row with its own exemplar. Source: OS 1897 “Examples for the Characters…”
 (NLS <a href="https://maps.nls.uk/view/128076792">view/128076792</a>) + c.1923 Plate IV
 (NLS <a href="https://maps.nls.uk/view/128076894">view/128076894</a>), CC-BY NLS, via IIIF. The classifier predicts a
 typographic <b>STYLE</b> (the coloured pill = the 5 visually-separable classes); a later fusion maps
 (style × text × size × date-regime) → feature-type. <b>Please tick each row or note a correction.</b></div>
</header>
<div class="legend">
 <div><b>CAPS</b> = the writing is in capitals (applies to multi-word examples too, not just single letters)</div>
 <div><b>size*</b> = size varies with extent — ONLY Bogs/Moors/Forests + Hills; every other category is fixed size</div>
 <div><b>†</b> = shown letterform only on maps published <b>before 1879</b> (sheet footnote)</div>
 <div><b>‡</b> = “on the more recent maps” = <b>≥1879</b></div>
 <div>GB1900 = EDITION 2: <b>99.7% ≥1879</b>, 0.3% pre-1879 → build the <b>≥1879</b> classifier first, then adapt <b>&lt;1879</b> from it</div>
</div>
<div class="wrap"><table>
 <tr><th>#</th><th>Exemplar</th><th>OS category (transcribed)</th><th>Style class</th><th>Date-regime</th><th>Verify</th></tr>
 {body}
</table></div>

<h2>Boundary &amp; abbreviation labels → TEXT lexicon (not the font classifier)</h2>
<div class="sub" style="margin:0 30px 6px">1897 boundary specimens + c.1923 sheet — abbreviation-lexicon items (weighted where ambiguous, e.g. M.S), separate from the a–z NLS abbreviation pages.</div>
<table class="btable"><tr><th>Abbrev</th><th>Meaning</th></tr>{brows}</table>

<h2>Provenance strips (full-sheet context)</h2>
{strips}
</body></html>"""
    open(os.path.join(HERE, "characteristic_sheet_table.html"), "w").write(doc)
    print(f"WROTE characteristic_sheet_table.html — {n} category rows ({len(doc)//1024} KB)")

if __name__ == "__main__":
    main()
