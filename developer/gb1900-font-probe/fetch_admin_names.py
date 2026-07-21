"""Fetch name gazetteers for the OS administrative faces from Wikidata (WDQS SPARQL), to seed the font
reference by LOOKING UP genuine admin labels (all-caps) rather than a single ornate mark-letter. Each OS face
maps to one or more Wikidata classes; we collect their English labels (+ aliases) filtered to Great Britain.

    /vast/ishi/envs/boundary/bin/python fetch_admin_names.py   # -> labels/admin_names.json
"""
import json, os, time, urllib.parse, urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "labels", "admin_names.json")
EP = "https://query.wikidata.org/sparql"
UA = "GB-STAMP/1.0 (World Historical Gazetteer; stephen@docuracy.co.uk)"

# OS face -> Wikidata class Q-ids (instances of, incl. subclasses); most are GB-specific so no country filter.
FACE_CLASSES = {
    "county_names":     ["Q13411591", "Q5127848", "Q13539802"],   # historic county (England / Wales / Scotland)
    "civil_parishes":   ["Q1115575", "Q13539803"],                # civil parish, ancient parish
    "parishes_ancient": ["Q13539803", "Q1115575"],
    "boroughs_munic":   ["Q1907114"],                             # municipal borough
    "boroughs_parl":    ["Q659103"],                              # borough constituency (UK Parliament)
    "county_boroughs":  ["Q1138494"],                             # county borough
    "hundreds":         ["Q217691"],                              # hundred
    "poor_law_unions":  ["Q1509831"],                             # poor law union
    "div_counties":     ["Q1195098"],                             # riding
    "liberties":        ["Q1361029"],                             # liberty (local government)
    "wards":            ["Q4123071"],                             # electoral ward / division
    "town_districts":   ["Q1187811", "Q1852178"],                # urban district, rural district
    "urban_sanitary":   ["Q1852178"],                            # urban district (proxy)
    "market_towns":     ["Q671324"],                             # market town
    "cities_nomp":      ["Q1549591"],                            # city (big city) — proxy
}
# country = UK / GB&Ireland / Kingdom of GB / Kingdom of England / Kingdom of Scotland (historic entities vary)
GB = "VALUES ?ctry { wd:Q145 wd:Q174193 wd:Q161885 wd:Q179876 wd:Q170676 } ?x wdt:P17 ?ctry ."

def query(qids, limit=8000):
    vals = " ".join("wd:" + q for q in qids)
    sparql = f"""SELECT DISTINCT ?name WHERE {{
      VALUES ?cls {{ {vals} }} ?x wdt:P31 ?cls .
      {GB}
      ?x rdfs:label ?name . FILTER(LANG(?name)="en")
    }} LIMIT {limit}"""
    url = EP + "?" + urllib.parse.urlencode({"query": sparql, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/sparql-results+json"})
    d = json.loads(urllib.request.urlopen(req, timeout=90).read())
    return [b["name"]["value"] for b in d["results"]["bindings"]]

def clean(names):
    out = set()
    for n in names:
        # drop qualifiers/parentheticals; keep the bare place name
        base = n.split("(")[0].split(",")[0].strip()
        if 2 <= len(base) <= 40 and any(c.isalpha() for c in base): out.add(base)
    return sorted(out)

def main():
    res = {}
    for face, qids in FACE_CLASSES.items():
        try:
            names = clean(query(qids)); res[face] = names
            print(f"{face:<20} {len(names):>5}  e.g. {names[:4]}", flush=True)
        except Exception as e:
            print(f"{face:<20} ERROR {type(e).__name__} {str(e)[:80]}", flush=True); res[face] = []
        time.sleep(2)
    json.dump(res, open(OUT, "w"), ensure_ascii=False, indent=0)
    print(f"\nwrote {OUT}: {sum(len(v) for v in res.values())} names across {sum(1 for v in res.values() if v)} faces")

if __name__ == "__main__":
    main()
