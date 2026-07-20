"""Codify the OS Characteristic Sheet font taxonomy — the class list the multi-font discriminator targets and
the spot-and-fan alphabet builder seeds from. Joins the labelling table (per-row data-* + embedded exemplar
crop), reference/cap_heights.json, the AAT crosswalk, and the EXTERNAL civic signal each category encodes
(so admin-area / market-town / parliamentary records can weight the best-three visual guesses).

    python build_font_taxonomy.py   # -> font_taxonomy.json  (+ reference/exemplars/<key>.jpg crops)
"""
import base64, json, os, re, html

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = f"{HERE}/characteristic_sheet_table.html"
CAPH = f"{HERE}/reference/cap_heights.json"
EXDIR = f"{HERE}/reference/exemplars"
OUT = f"{HERE}/font_taxonomy.json"

# What independent records tell us about a name in this category (drives the textual/external re-weighting).
# None = no civic gazetteer signal (physical/■descriptive faces disambiguated by text co-occurrence only).
EXTERNAL = {
    "county_names": "admin:county", "div_counties": "admin:county_division", "hundreds": "admin:hundred",
    "liberties": "admin:liberty", "parishes_ancient": "admin:ancient_parish",
    "civil_parishes": "admin:civil_parish_or_township", "div_townships": "admin:township_division",
    "subdiv_townships": "admin:township_subdivision", "boroughs_parly": "civic:parliamentary_borough",
    "boroughs_muni": "civic:municipal_borough", "towns_general": "settlement:town",
    "town_districts": "admin:town_district", "div_ridings": "admin:riding",
    "poor_law_unions": "admin:poor_law_union", "urban_sanitary": "admin:urban_sanitary_district",
    "cities_members": "civic:parliamentary_city", "cities_nomembers": "civic:city",
    "wards": "admin:ward", "market_towns": "civic:market_town", "other_towns": "settlement:town",
    "parly_div_counties": "admin:parliamentary_division", "county_boroughs": "civic:county_borough",
    "extra_parochial": "admin:extra_parochial", "turnpike_trusts": "infra:turnpike_trust",
    "parish_churches_villages": "settlement:village", "chapelries": "religion:chapelry",
    "other_villages": "settlement:village", "parks_demesnes": "estate:park",
    "gentlemens_seats": "estate:seat", "market_towns2": "civic:market_town",
}

def slug(k):
    return re.sub(r"^ex_", "", k or "")

def main():
    t = open(TABLE).read()
    caph = {c["key"]: c for c in json.load(open(CAPH))} if os.path.exists(CAPH) else {}
    os.makedirs(EXDIR, exist_ok=True)
    rows = re.findall(r"<tr[^>]*data-key=.*?</tr>", t, re.S)
    tax = []
    for r in rows:
        attr = dict(re.findall(r'data-(\w+)="([^"]*)"', r))
        key = attr.get("key")
        if not key: continue
        # save exemplar crop
        m = re.search(rf'data-exkey="{re.escape(key)}"\s+src="data:image/jpeg;base64,([^"]+)"', r)
        expath = None
        if m:
            expath = f"reference/exemplars/{slug(key)}.jpg"
            with open(f"{HERE}/{expath}", "wb") as f: f.write(base64.b64decode(m.group(1)))
        ch = caph.get(key, {})
        s = slug(key)
        tax.append({
            "id": len(tax) + 1, "key": s, "label": html.unescape(attr.get("label", "")),
            "base_style": attr.get("style"), "caps": attr.get("caps") == "1",
            "size_variable": bool(ch.get("size_variable")) or attr.get("size") == "1",
            "cap_h_px": ch.get("cap_h_native_px"), "regime": attr.get("regime"),
            "external": EXTERNAL.get(s), "exemplar": expath})
    json.dump(tax, open(OUT, "w"), ensure_ascii=False, indent=1)
    # summary
    from collections import Counter
    print(f"{len(tax)} categories -> {OUT}")
    print("base styles:", dict(Counter(x["base_style"] for x in tax)))
    print("caps:", sum(1 for x in tax if x["caps"]), "| size-variable:", sum(1 for x in tax if x["size_variable"]))
    print("with external civic signal:", sum(1 for x in tax if x["external"]))
    print("exemplar crops saved:", sum(1 for x in tax if x["exemplar"]))

if __name__ == "__main__":
    main()
