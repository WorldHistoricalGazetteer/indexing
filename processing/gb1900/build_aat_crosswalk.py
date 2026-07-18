"""Build a CANDIDATE AAT crosswalk for GB-STAMP type tokens (publishing prerequisite, option c).
Queries the WHG prod ES `types` index (Getty AAT, synced; ~59k concepts) for each token's search
terms. CANDIDATE-level for human curation — not asserted. Run ON pitt (localhost:9201).
    python build_aat_crosswalk.py --out gb_stamp_aat_crosswalk.json
"""
import argparse, json, time, urllib.request, urllib.parse

ES = "http://localhost:9201/types/_search"
PW = open("/ix1/ishi/es/config/elastic.password").read().strip()
import base64
AUTH = "Basic " + base64.b64encode(f"elastic:{PW}".encode()).decode()

TOKEN_TERMS = {
    "footpath": ["footpaths"], "footbridge": ["footbridges", "pedestrian bridges"], "bridge": ["bridges (built works)"],
    "bridle_road": ["bridle paths"], "well": ["wells (water sources)"], "pump": ["water pumps", "pumps"],
    "spring": ["springs (bodies of water)"], "signpost": ["signposts"], "guidepost": ["guideposts"],
    "benchmark": ["benchmarks (survey markers)"], "milestone": ["milestones"], "milepost": ["mileposts"],
    "boundary_stone": ["boundary markers", "boundary stones"], "boundary_post": ["boundary markers"],
    "boundary": ["boundaries (legal concept)"], "boundary_feature": ["boundaries (legal concept)"],
    "church": ["churches (buildings)"], "chapel": ["chapels (rooms or structures)"], "clergy_house": ["clergy houses"],
    "vicarage": ["vicarages"], "rectory": ["rectories"], "school": ["schools (buildings)"], "college": ["college buildings"],
    "mill": ["mills (grinding facilities)"], "farm": ["farms"], "smithy": ["blacksmith shops"], "works": ["factories"],
    "brewery": ["breweries (buildings)"], "gasworks": ["gasworks"], "brickworks": ["brickyards"], "limekiln": ["lime kilns"],
    "colliery": ["coal mines"], "quarry_or_mine": ["quarries", "mines (extraction sites)"], "quarry": ["quarries"],
    "public_house": ["public houses (taverns)", "taverns (public accommodations)"], "hotel": ["hotels"],
    "post_office": ["post offices"], "letter_box": ["mailboxes"], "telephone": ["telephone booths"],
    "water_feature": ["bodies of water"], "water_tap": ["faucets"], "waterworks": ["waterworks"], "sewage": ["sewage systems"],
    "fire_hydrant": ["fire hydrants"], "tidal_coastal": ["tidal flats"], "flood_land": ["floodplains"],
    "antiquity": ["archaeological sites"], "monument": ["monuments"], "settlement": ["settlements (built complexes)"],
    "admin_or_parish": ["civil parishes", "parishes"], "county_admin": ["counties (political divisions)"],
    "building_or_feature": ["buildings (structures)"], "road": ["roads"], "railway": ["railroads"],
    "tramway": ["streetcar lines"], "station": ["railroad stations"], "signal_box": ["signal towers (railroad structures)"],
    "railway_building": ["railroad buildings"], "aqueduct": ["aqueducts (bridges)"], "viaduct": ["viaducts"],
    "tunnel": ["tunnels"], "pier": ["piers"], "jetty": ["jetties"], "breakwater": ["breakwaters"],
    "landing_stage": ["quays"], "ferry": ["ferries (facilities)"], "wharf": ["wharves"], "boathouse": ["boathouses"],
    "coastguard": ["coast guard stations"], "lifeboat_station": ["lifeboat stations"], "named_landcover": ["woodlands"],
    "tree": ["trees"], "fold": ["animal pens"], "kennels": ["kennels"], "pheasantry": ["game farms"],
    "rifle_range": ["rifle ranges"], "recreation_ground": ["playing fields"], "pavilion": ["pavilions (garden structures)"],
    "reading_room": ["reading rooms"], "almshouses": ["almshouses"], "workhouse": ["workhouses"], "hospital": ["hospitals"],
    "cemetery": ["cemeteries"], "barracks": ["barracks"], "beacon": ["beacons"], "flagstaff": ["flagpoles"],
    "cave": ["caves"], "natural_feature": ["natural landscapes"], "landform": ["landforms"],
    "stone_marker": ["markers (object genre)"], "pound": ["animal pens"], "icehouse": ["ice houses"],
    "pole": ["poles (structural elements)"], "gate": ["gates (door assemblies)"], "stile": ["stiles"],
    "mooring_post": ["bollards"], "spot_height_or_value": ["elevation (spatial attribute)"],
    "parks_demesnes": ["parks"], "gentlemens_seat": ["country houses"], "bays_harbours": ["harbors"],
    "bogs_moors_forests": ["moors"], "hills": ["hills"], "market_town": ["market towns"], "city": ["cities"],
    "borough": ["boroughs"],
}

def search(term):
    body = json.dumps({"size": 4, "query": {"bool": {
        "should": [{"match_phrase": {"term": {"query": term, "boost": 5}}},
                   {"match": {"term": term}}, {"match": {"term_full": term}}, {"match": {"labels": term}}]}},
        "_source": ["aat_id", "term", "is_place_type"]}).encode()
    req = urllib.request.Request(ES, data=body, headers={"Content-Type": "application/json", "Authorization": AUTH})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            hits = json.load(r)["hits"]["hits"]
        return [{"aat_id": h["_source"]["aat_id"], "aat_term": h["_source"].get("term"),
                 "is_place_type": h["_source"].get("is_place_type"), "score": round(h["_score"], 1)} for h in hits]
    except Exception as e:
        print("  ERR", term, e, flush=True); return []

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True); a = ap.parse_args()
    xw = {}
    for i, (tok, terms) in enumerate(sorted(TOKEN_TERMS.items())):
        cands = []
        for term in terms:
            for c in search(term):
                c["matched"] = term; cands.append(c)
        # prefer exact term match, then place-type, then score
        def rank(c):
            exact = any(c["aat_term"] and c["aat_term"].lower().startswith(t.lower().split(" (")[0]) for t in terms)
            return (exact, bool(c.get("is_place_type")), c["score"])
        cands.sort(key=rank, reverse=True)
        best = cands[0] if cands else None
        xw[tok] = {"best": best, "candidates": cands[:5], "search_terms": terms}
        print(f"[{i+1}/{len(TOKEN_TERMS)}] {tok}: {best.get('aat_id') if best else None} {best.get('aat_term') if best else ''}", flush=True)
    json.dump(xw, open(a.out, "w"), indent=1, ensure_ascii=False)
    got = sum(1 for v in xw.values() if v["best"])
    print(f"WROTE {a.out}: {got}/{len(xw)} tokens with a candidate AAT id", flush=True)

if __name__ == "__main__":
    main()
