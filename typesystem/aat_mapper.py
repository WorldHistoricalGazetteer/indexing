# types/aat_mapper.py

"""
Automated AAT mapping augmentation for WHG type vocabulary files.

Reads the JSON files in types/data/ and augments each value entry with
AAT (Art & Architecture Thesaurus) mappings using multiple strategies:

Subcommands:
    static     — Apply curated static mappings for OSM/OHM/GeoNames/Pleiades
    sparql     — Query AAT SPARQL endpoint for label matches
    wikidata   — Bridge Wikidata Q-items → AAT via P1014 (Getty AAT ID)
    validate   — Validate existing AAT IDs against the AAT vocabulary
    report     — Report mapping coverage statistics

Usage:
    python -m typesystem.aat_mapper static
    python -m typesystem.aat_mapper sparql
    python -m typesystem.aat_mapper wikidata
    python -m typesystem.aat_mapper validate
    python -m typesystem.aat_mapper report
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlencode, quote

from typesystem.aat_config import AAT_SPARQL_ENDPOINT, AAT_JSON_API

DATA_DIR = Path(__file__).parent / "data"


# ============================================================================
# I/O Helpers
# ============================================================================

def load_data_file(name):
    """Load a types/data/*.json file."""
    path = DATA_DIR / name
    with open(path) as f:
        return json.load(f)


def save_data_file(name, data):
    """Write a types/data/*.json file (atomic: write to temp, then rename)."""
    path = DATA_DIR / name
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except BaseException:
        os.unlink(tmp_path)
        raise
    print(f"  Saved {path}")


def iter_values(data, namespace):
    """
    Yield (key_path, entry) tuples for all value entries in a data file.
    Handles the different structures of each namespace's JSON.
    """
    if namespace in ("osm", "ohm"):
        # Keyed by tag key (place, natural, etc.) → .values[]
        for tag_key, tag_data in data.items():
            if tag_key.startswith("_"):
                continue
            if not isinstance(tag_data, dict):
                continue
            for entry in tag_data.get("values", []):
                yield f"{tag_key}={entry.get('value', '')}", entry

    elif namespace == "geonames":
        # Keyed by feature class (A, H, P, etc.) → .values[]
        for fclass, fclass_data in data.items():
            if not isinstance(fclass_data, dict):
                continue
            for entry in fclass_data.get("values", []):
                yield f"{fclass}.{entry.get('value', '')}", entry

    elif namespace == "wikidata":
        for entry in data.get("values", []):
            yield entry.get("value", ""), entry

    elif namespace == "pleiades":
        for entry in data.get("values", []):
            yield entry.get("value", ""), entry
        for entry in data.get("deprecated", []):
            yield entry.get("value", ""), entry


def set_aat_mapping(entry, aat_id, aat_term, confidence, source):
    """Set the AAT mapping on a value entry."""
    entry["aat_mapping"] = {
        "aat_id": aat_id,
        "aat_term": aat_term,
        "confidence": confidence,
        "source": source,
    }


# ============================================================================
# SPARQL Helper
# ============================================================================

def sparql_query(query, timeout=60):
    """Execute a SPARQL query against the AAT endpoint. Returns parsed JSON."""
    params = urlencode({"query": query, "format": "json"})
    url = f"{AAT_SPARQL_ENDPOINT}?{params}"
    req = Request(url, headers={
        "User-Agent": "WHG-indexing/1.0 (whgazetteer.org)",
        "Accept": "application/sparql-results+json",
    })
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sparql_label_search(label, limit=10):
    """
    Search AAT for concepts matching a label string.
    Returns list of (aat_id, term, score_hint) tuples.
    """
    # Use lucene full-text search via the GVP ontology
    query = f"""
    PREFIX gvp: <http://vocab.getty.edu/ontology#>
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
    PREFIX luc: <http://www.ontotext.com/owlim/lucene#>
    PREFIX aat: <http://vocab.getty.edu/aat/>

    SELECT ?concept ?term ?scopeNote WHERE {{
      ?concept a gvp:Concept ;
               gvp:prefLabelGVP/gvp:term ?term .
      ?concept luc:term "{label}" .
      OPTIONAL {{
        ?concept skos:scopeNote/rdf:value ?scopeNote .
        FILTER(LANG(?scopeNote) = "en")
      }}
    }}
    LIMIT {limit}
    """
    try:
        result = sparql_query(query)
        matches = []
        for binding in result.get("results", {}).get("bindings", []):
            concept_uri = binding.get("concept", {}).get("value", "")
            term = binding.get("term", {}).get("value", "")
            if "/aat/" in concept_uri:
                aat_id_str = concept_uri.split("/aat/")[-1]
                try:
                    aat_id = int(aat_id_str)
                    matches.append((aat_id, term))
                except ValueError:
                    pass
        return matches
    except Exception as e:
        print(f"    SPARQL query failed for '{label}': {e}")
        return []


def sparql_exact_match(label):
    """
    Search AAT for an exact preferred-label match.
    Returns (aat_id, term) or None.
    """
    escaped = label.replace('"', '\\"')
    query = f"""
    PREFIX gvp: <http://vocab.getty.edu/ontology#>
    PREFIX xl: <http://www.w3.org/2008/05/skos-xl#>

    SELECT ?concept ?term WHERE {{
      ?concept a gvp:Concept ;
               gvp:prefLabelGVP [xl:literalForm ?term] .
      FILTER(LCASE(STR(?term)) = "{escaped.lower()}")
    }}
    LIMIT 5
    """
    try:
        result = sparql_query(query)
        for binding in result.get("results", {}).get("bindings", []):
            concept_uri = binding.get("concept", {}).get("value", "")
            term = binding.get("term", {}).get("value", "")
            if "/aat/" in concept_uri:
                aat_id_str = concept_uri.split("/aat/")[-1]
                try:
                    return int(aat_id_str), term
                except ValueError:
                    pass
    except Exception as e:
        print(f"    SPARQL exact match failed for '{label}': {e}")
    return None


def sparql_label_by_id(aat_id):
    """
    Fetch the preferred English label for an AAT concept by numeric ID.
    Returns the label string, or None.
    """
    query = f"""
    PREFIX gvp: <http://vocab.getty.edu/ontology#>
    PREFIX aat: <http://vocab.getty.edu/aat/>

    SELECT ?term WHERE {{
      aat:{aat_id} gvp:prefLabelGVP/gvp:term ?term .
    }}
    LIMIT 1
    """
    try:
        result = sparql_query(query)
        for binding in result.get("results", {}).get("bindings", []):
            return binding.get("term", {}).get("value", "")
    except Exception as e:
        print(f"    AAT label fetch failed for aat:{aat_id}: {e}")
    return None


# ============================================================================
# ES types-index lookups (replaces SPARQL for label resolution)
# ============================================================================

ES_TYPES_INDEX = "types"


def es_labels_by_ids(es, aat_ids):
    """
    Batch-fetch preferred labels for a collection of AAT IDs from the local
    ES types index.  Returns dict: aat_id (int) → label (str).
    """
    if not aat_ids:
        return {}
    docs = [{"_index": ES_TYPES_INDEX, "_id": f"aat:{aid}"} for aid in aat_ids]
    resp = es.mget(docs=docs, source_includes=["term"])
    labels = {}
    for doc in resp.get("docs", []):
        if doc.get("found"):
            aid = doc["_source"].get("aat_id") or int(doc["_id"].split(":")[1])
            labels[aid] = doc["_source"].get("term", "")
    return labels


def es_label_search(es, label, limit=5):
    """
    Search the local ES types index for AAT concepts matching a label string.

    Two-phase:
      1. Exact match on term.keyword (case-sensitive but fast)
      2. Folded match on term.folded (lowercase + asciifolding)

    Returns list of (aat_id, term, confidence) tuples.
    """
    results = []

    # Phase 1: exact keyword match
    resp = es.search(
        index=ES_TYPES_INDEX,
        size=limit,
        query={"term": {"term.keyword": label}},
        source=["aat_id", "term"],
    )
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        results.append((src["aat_id"], src["term"], "es_exact"))

    if results:
        return results

    # Phase 2: folded text match (case-insensitive, asciifolding)
    resp = es.search(
        index=ES_TYPES_INDEX,
        size=limit,
        query={"match": {"term.folded": {"query": label, "operator": "and"}}},
        source=["aat_id", "term"],
    )
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        results.append((src["aat_id"], src["term"], "es_fuzzy"))

    return results


# ============================================================================
# Wikidata → AAT bridge
# ============================================================================

def fetch_wikidata_aat_mappings(qids):
    """
    Query Wikidata SPARQL for P1014 (Getty AAT ID) values.
    Returns dict: qid → aat_id (int).
    """
    if not qids:
        return {}

    # Batch into groups of 200 for VALUES clause
    results = {}
    batches = [qids[i:i + 200] for i in range(0, len(qids), 200)]

    print(f"  Querying Wikidata SPARQL for P1014 on {len(qids)} Q-items "
          f"({len(batches)} batches) ...")

    for i, batch in enumerate(batches):
        values_str = " ".join(f"wd:{q}" for q in batch)
        query = f"""
        PREFIX wd: <http://www.wikidata.org/entity/>
        PREFIX wdt: <http://www.wikidata.org/prop/direct/>

        SELECT ?item ?aatId WHERE {{
          VALUES ?item {{ {values_str} }}
          ?item wdt:P1014 ?aatId .
        }}
        """
        url = "https://query.wikidata.org/sparql"
        params = urlencode({"query": query, "format": "json"})
        req = Request(f"{url}?{params}", headers={
            "User-Agent": "WHG-indexing/1.0 (whgazetteer.org)",
            "Accept": "application/sparql-results+json",
        })

        try:
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            for binding in data.get("results", {}).get("bindings", []):
                qid = binding["item"]["value"].split("/")[-1]
                aat_id_str = binding["aatId"]["value"]
                try:
                    results[qid] = int(aat_id_str)
                except ValueError:
                    pass

        except Exception as e:
            print(f"    Batch {i + 1} failed: {e}")

        if (i + 1) % 5 == 0:
            print(f"    ... {i + 1}/{len(batches)} batches done")
        time.sleep(1)  # Be polite to Wikidata

    return results


# ============================================================================
# Static mappings — curated lookup tables
# ============================================================================

# OSM/OHM: sourceLabel → (aat_id, aat_term)
# These are authoritative hand-curated mappings.
OSM_OHM_STATIC_MAPPINGS = {
    # ── place=* ──────────────────────────────────────────────────────────
    # Sorted by descending TagInfo count
    "place=hamlet": (300008584, "hamlets"),                         # 2,087K
    # "place=locality": (300008347, "inhabited places"),  ## NB: OSM defines this as "an unpopulated location for which there is no extant feature"            # 1,944K
    "place=village": (300008372, "villages"),                       # 1,741K
    "place=isolated_dwelling": (300005929, "dwellings"),            # 851K
    "place=islet": (300008680, "islands"),                          # 701K
    "place=neighbourhood": (300000745, "suburbs"),                  # 532K  closest AAT
    "place=plot": (300000280, "plots (land)"),                      # 308K
    "place=farm": (300000206, "farms"),                             # 282K
    "place=suburb": (300000745, "suburbs"),                         # 160K
    "place=quarter": (300000745, "suburbs"),                        # 159K
    "place=town": (300008375, "towns"),                             # 117K
    "place=square": (300008072, "open spaces"),                     # 108K
    "place=island": (300008680, "islands"),                         # 100K
    "place=municipality": (300265612, "municipalities"),            # 17K
    "place=city": (300008389, "cities"),                            # 15K
    "place=county": (300000771, "counties"),                        # 9K
    "place=civil_parish": (300000778, "parishes"),                  # 4K
    "place=region": (300182722, "geographic regions"),              # 3K
    "place=archipelago": (300386854, "archipelagos"),               # 3K
    "place=state": (300000776, "boroughs"),                         # 2K  approx
    "place=borough": (300000776, "boroughs"),                       # 590
    "place=country": (300128207, "nations"),                        # 227
    "place=sea": (300008694, "seas"),                               # 148
    "place=continent": (300128176, "continents"),                   # 9
    "place=ocean": (300008687, "oceans"),                           # 5

    # ── natural=* ────────────────────────────────────────────────────────
    "natural=water": (300008680, "bodies of water"),                # 22,600K
    "natural=wood": (300132294, "natural landscapes"),              # 12,233K
    "natural=scrub": (300132294, "natural landscapes"),             # 5,444K
    "natural=wetland": (300008832, "wetlands"),                     # 4,465K
    "natural=peak": (300008795, "peaks (landforms)"),               # 1,094K
    "natural=cliff": (300008773, "cliffs"),                         # 980K
    "natural=heath": (300132294, "natural landscapes"),             # 647K
    "natural=sand": (300008755, "dunes"),                           # 589K
    "natural=spring": (300008697, "springs (bodies of water)"),     # 277K
    "natural=ridge": (300008812, "ridges (landforms)"),             # 246K
    "natural=beach": (300008816, "beaches"),                        # 242K
    "natural=reef": (300132316, "reefs"),                           # 117K
    "natural=glacier": (300008781, "glaciers"),                     # 115K
    "natural=bay": (300132315, "bays (bodies of water)"),           # 89K
    "natural=saddle": (300008815, "saddles (landforms)"),           # 78K
    "natural=cave_entrance": (300008746, "caves"),                  # 71K
    "natural=valley": (300008861, "valleys"),                       # 62K
    "natural=cape": (300008775, "capes (landforms)"),               # 56K
    "natural=volcano": (300132325, "volcanoes"),                    # 10K
    "natural=strait": (300008700, "straits"),                       # 8K
    "natural=hot_spring": (300008700, "hot springs"),               # 7K
    "natural=peninsula": (300008804, "peninsulas"),                 # 5K

    # ── water=* ──────────────────────────────────────────────────────────
    "water=pond": (300008689, "ponds"),                             # 2,293K
    "water=lake": (300008680, "lakes"),                             # 886K  (was: islands — fixed)
    "water=reservoir": (300006191, "reservoirs"),                   # 859K
    "water=river": (300008707, "rivers"),                           # 513K
    "water=canal": (300006075, "canals"),                           # 68K
    "water=stream": (300008699, "streams"),                         # 55K
    "water=oxbow": (300008707, "rivers"),                           # 53K  part of river system
    "water=ditch": (300006176, "ditches"),                          # 40K
    "water=lagoon": (300132315, "bays (bodies of water)"),          # 13K  approx
    "water=moat": (300006295, "moats"),                             # 3K
    "water=harbour": (300008678, "harbors"),                        # 1K

    # ── waterway=* ───────────────────────────────────────────────────────
    "waterway=stream": (300008699, "streams"),                      # 28,877K
    "waterway=ditch": (300006176, "ditches"),                       # 4,581K
    "waterway=river": (300008707, "rivers"),                        # 2,006K
    "waterway=canal": (300006075, "canals"),                        # 956K
    "waterway=dam": (300006079, "dams"),                            # 264K
    "waterway=waterfall": (300132324, "waterfalls"),                # 86K
    "waterway=dock": (300120631, "docks"),                          # 7K

    # ── historic=* ───────────────────────────────────────────────────────
    "historic=memorial": (300006780, "memorials"),                  # 481K
    "historic=archaeological_site": (300000810, "archaeological sites"),  # 223K
    "historic=ruins": (300008057, "ruins"),                         # 178K
    "historic=wayside_shrine": (300007558, "shrines"),              # 163K
    "historic=boundary_stone": (300006971, "boundary markers"),     # 94K
    "historic=building": (300004792, "buildings"),                  # 89K
    "historic=tomb": (300005926, "tombs"),                          # 71K
    "historic=monument": (300006958, "monuments"),                  # 71K
    "historic=castle": (300006891, "castles (fortifications)"),     # 54K
    "historic=manor": (300005828, "manor houses"),                  # 19K
    "historic=mine": (300000195, "mines (extractive industry sites)"),  # 13K
    "historic=church": (300007466, "churches"),                     # 12K
    "historic=fort": (300006909, "forts"),                          # 9K
    "historic=city_gate": (300002779, "city gates"),                # 9K
    "historic=milestone": (300006973, "milestones"),                # 9K
    "historic=wreck": (300188189, "shipwrecks"),                    # 6K
    "historic=monastery": (300005616, "monasteries"),               # 3K
    "historic=farm": (300000206, "farms"),                          # 3K
    "historic=bridge": (300007836, "bridges (built works)"),        # 3K
    "historic=tower": (300004847, "towers (building divisions)"),   # 3K
    "historic=battlefield": (300000824, "battlefields"),            # 2K
    "historic=cemetery": (300000632, "cemeteries"),                 # 2K
    "historic=palace": (300005734, "palaces"),                      # ~1K (not in table; historic)
    "historic=temple": (300007595, "temples"),                      # ~1K
    "historic=aqueduct": (300006165, "aqueducts"),                  # 1K
    "historic=lighthouse": (300007741, "lighthouses"),              # ~1K

    # ── boundary=* ───────────────────────────────────────────────────────
    "boundary=administrative": (300261086, "political administrative bodies"),  # 2,100K
    "boundary=protected_area": (300008069, "national parks"),       # 132K  approx
    "boundary=historic": (300261086, "political administrative bodies"),  # 14K
    "boundary=national_park": (300008069, "national parks"),        # 6K

    # ── landuse=* ────────────────────────────────────────────────────────
    "landuse=farmland": (300000206, "farms"),                       # 11,328K
    "landuse=residential": (300008347, "inhabited places"),         # 10,417K
    "landuse=forest": (300132294, "natural landscapes"),            # 5,847K
    "landuse=meadow": (300132294, "natural landscapes"),            # 5,380K
    "landuse=orchard": (300000206, "farms"),                        # 1,835K
    "landuse=industrial": (300000757, "industrial districts"),      # 1,393K
    "landuse=vineyard": (300000206, "farms"),                       # 847K
    "landuse=cemetery": (300000292, "cemeteries"),                  # 587K
    "landuse=commercial": (300000745, "suburbs"),                   # 572K  approx
    "landuse=quarry": (300000195, "mines (extractive industry sites)"),  # 252K
    "landuse=reservoir": (300006191, "reservoirs"),                 # 197K
    "landuse=recreation_ground": (300008072, "open spaces"),        # 181K
    "landuse=military": (300000810, "archaeological sites"),        # 94K  approx
    "landuse=port": (300120631, "docks"),                           # approx
    "landuse=harbour": (300120580, "ports"),                        # 3K

    # ── amenity=* ────────────────────────────────────────────────────────
    "amenity=place_of_worship": (300007391, "religious buildings"),  # 1,600K
    "amenity=school": (300007165, "schools (buildings)"),            # 1,435K
    "amenity=hospital": (300007145, "hospitals"),                    # 217K
    "amenity=fountain": (300006203, "fountains"),                   # 196K
    "amenity=townhall": (300007237, "town halls"),                  # 172K
    "amenity=grave_yard": (300000360, "burial grounds"),            # 159K
    "amenity=library": (300007150, "libraries (institutions)"),     # 111K
    "amenity=marketplace": (300112348, "marketplaces"),             # 96K
    "amenity=college": (300008331, "colleges"),                     # 71K
    "amenity=university": (300007166, "universities (institutions)"),  # 57K
    "amenity=theatre": (300007117, "theaters (buildings)"),         # 52K
    "amenity=courthouse": (300007425, "courthouses"),               # 31K
    "amenity=monastery": (300000641, "monasteries"),                # 16K
    "amenity=prison": (300007147, "prisons"),                       # 15K

    # ── tourism=* ────────────────────────────────────────────────────────
    "tourism=hotel": (300007166, "hotels"),                         # 446K
    "tourism=attraction": (300006958, "monuments"),                 # 251K  approx
    "tourism=museum": (300005768, "museums (buildings)"),           # 109K
    "tourism=gallery": (300005768, "galleries (buildings)"),        # 21K
    "tourism=zoo": (300005581, "zoos"),                             # 9K
    "tourism=castle": (300006891, "castles (fortifications)"),      # (overlap w/ historic)

    # ── leisure=* ────────────────────────────────────────────────────────
    "leisure=garden": (300008090, "gardens (open spaces)"),         # 1,498K
    "leisure=park": (300008072, "open spaces"),                     # 1,277K
    "leisure=nature_reserve": (300008069, "national parks"),        # 148K  approx
    "leisure=stadium": (300007108, "stadiums"),                     # 52K
    "leisure=marina": (300120471, "marinas"),                       # 31K

    # ── man_made=* ───────────────────────────────────────────────────────
    "man_made=tower": (300004847, "towers (building divisions)"),   # 449K
    "man_made=water_well": (300006209, "wells"),                    # 344K
    "man_made=bridge": (300007836, "bridges (built works)"),        # 311K
    "man_made=works": (300006232, "factories"),                     # 238K
    "man_made=water_tower": (300007880, "water towers"),            # 165K
    "man_made=pier": (300120631, "docks"),                          # 728K  approx
    "man_made=dyke": (300006177, "dikes"),                          # 36K
    "man_made=aqueduct": (300006165, "aqueducts"),                  # (historic overlap)
    "man_made=windmill": (300006281, "windmills"),                  # ~15K
    "man_made=lighthouse": (300007741, "lighthouses"),              # ~15K
    "man_made=watermill": (300006280, "watermills"),                # ~5K
    "man_made=observatory": (300007303, "observatories"),           # ~2K

    # ── military=* ───────────────────────────────────────────────────────
    "military=barracks": (300006871, "barracks"),                   # 16K
    "military=bunker": (300006926, "bunkers"),                      # 107K
    "military=base": (300000455, "military installations"),         # 4K
    "military=airfield": (300007027, "airfields"),                  # 2K
    "military=naval_base": (300000888, "naval bases"),              # 526

    # ── building=* (allowlist only — significant types) ──────────────────
    "building=school": (300007165, "schools (buildings)"),           # 1,307K
    "building=church": (300007466, "churches"),                     # 430K
    "building=university": (300007166, "universities (institutions)"),  # 175K
    "building=hospital": (300007145, "hospitals"),                  # 169K
    "building=hotel": (300007166, "hotels"),                        # 157K
    "building=chapel": (300004590, "chapels"),                      # 118K
    "building=mosque": (300007544, "mosques"),                      # 107K
    "building=government": (300007429, "government buildings"),     # 32K
    "building=temple": (300007595, "temples"),                      # 31K
    "building=stadium": (300007180, "stadiums"),                    # 12K
    "building=tomb": (300005926, "tombs"),                          # 11K
    "building=tower": (300004847, "towers (building divisions)"),   # 7K
    "building=shrine": (300007558, "shrines"),                      # 6K
    "building=military": (300000455, "military installations"),     # 6K
    "building=castle": (300006891, "castles (fortifications)"),     # 4K
    "building=monastery": (300000641, "monasteries"),               # 4K
    "building=cathedral": (300007501, "cathedrals"),                # 3K
    "building=museum": (300005768, "museums (buildings)"),          # 2K
    "building=barracks": (300006691, "barracks"),                   # 2K
    "building=prison": (300343479, "prisons"),                      # 2K
    "building=water_tower": (300007880, "water towers"),            # 2K
    "building=library": (300007145, "libraries"),                   # 2K
    "building=synagogue": (300007590, "synagogues"),                # 2K
    "building=train_station": (300007783, "railroad stations"),     # (overlap)
    "building=theatre": (300007117, "theaters (buildings)"),        # 1K
    "building=pagoda": (300007587, "pagodas"),                      # 1K
    "building=palace": (300005734, "palaces"),                      # 870

    # ── aeroway=* (Tier 3) ───────────────────────────────────────────────
    "aeroway=aerodrome": (300007026, "airports"),                   # 46K

    # ── railway=* (Tier 3) ───────────────────────────────────────────────
    "railway=station": (300007783, "railroad stations"),            # 104K
    "railway=halt": (300007783, "railroad stations"),               # 35K
}

# GeoNames: sourceLabel (e.g. "P.PPL") → (aat_id, aat_term)
GEONAMES_STATIC_MAPPINGS = {
    "P.PPL": (300008347, "inhabited places"),
    "P.PPLA": (300008347, "inhabited places"),
    "P.PPLA2": (300008347, "inhabited places"),
    "P.PPLA3": (300008347, "inhabited places"),
    "P.PPLA4": (300008347, "inhabited places"),
    "P.PPLA5": (300008347, "inhabited places"),
    "P.PPLC": (300008347, "inhabited places"),
    "P.PPLL": (300008347, "inhabited places"),
    "P.PPLR": (300008347, "inhabited places"),
    "P.PPLS": (300008347, "inhabited places"),
    "P.PPLX": (300008347, "inhabited places"),
    "A.ADM1": (300000776, "boroughs"),
    "A.ADM2": (300000771, "counties"),
    "A.ADM3": (300265612, "municipalities"),
    "A.ADM4": (300265612, "municipalities"),
    "A.ADM5": (300265612, "municipalities"),
    "A.ADMD": (300261086, "political administrative bodies"),
    "A.PCLI": (300128207, "nations"),
    "A.PCLD": (300128207, "nations"),
    "A.PCLIX": (300128207, "nations"),
    "A.PCLF": (300128207, "nations"),
    "A.PCLS": (300128207, "nations"),
    "A.TERR": (300182722, "geographic regions"),
    "H.STM": (300008699, "streams"),
    "H.STMS": (300008699, "streams"),
    "H.LK": (300132316, "reefs"),  # TODO: fix — should be lakes
    "H.LKS": (300132316, "reefs"),  # TODO: fix — should be lakes
    "H.RSV": (300006191, "reservoirs"),
    "H.SPNG": (300008697, "springs (bodies of water)"),
    "H.BAY": (300132315, "bays (bodies of water)"),
    "H.OCN": (300008687, "oceans"),
    "H.SEA": (300008694, "seas"),
    "H.CHNL": (300006075, "canals"),
    "H.FLLSW": (300132324, "waterfalls"),
    "H.FLLS": (300132324, "waterfalls"),
    "H.STRT": (300008700, "straits"),
    "T.MT": (300008795, "peaks (landforms)"),
    "T.MTS": (300008795, "peaks (landforms)"),
    "T.PK": (300008795, "peaks (landforms)"),
    "T.VLC": (300132325, "volcanoes"),
    "T.ISL": (300008680, "islands"),
    "T.ISLS": (300008680, "islands"),
    "T.PEN": (300008804, "peninsulas"),
    "T.CAPE": (300008775, "capes (landforms)"),
    "T.CLF": (300008773, "cliffs"),
    "T.VAL": (300008861, "valleys"),
    "T.PLN": (300008831, "plains"),
    "T.PLAT": (300008736, "plateaus"),
    "T.RDG": (300008812, "ridges (landforms)"),
    "T.BCH": (300008816, "beaches"),
    "T.DSRT": (300008764, "deserts"),
    "T.GRGE": (300008783, "gorges"),
    "T.CAVE": (300008746, "caves"),
    "T.GLCR": (300008781, "glaciers"),
    "S.CH": (300007466, "churches"),
    "S.CSTL": (300006891, "castles (fortifications)"),
    "S.FT": (300006909, "forts"),
    "S.MNST": (300005616, "monasteries"),
    "S.TMPL": (300007595, "temples"),
    "S.LIBR": (300007150, "libraries (institutions)"),
    "S.MUS": (300005768, "museums (buildings)"),
    "S.HSP": (300007145, "hospitals"),
    "S.SCH": (300007165, "schools (buildings)"),
    "S.UNIV": (300007166, "universities (institutions)"),
    "S.LTHSE": (300007741, "lighthouses"),
    "S.BDG": (300007836, "bridges (built works)"),
    "S.RSTN": (300007783, "railroad stations"),
    "S.AIRP": (300007844, "airports"),
    "S.CMTY": (300000292, "cemeteries"),
    "S.MN": (300000195, "mines (extractive industry sites)"),
    "S.DAM": (300006079, "dams"),
    "S.PSN": (300007147, "prisons"),
    "L.PRK": (300008072, "open spaces"),
    "L.RESN": (300008069, "national parks"),
    "L.RESW": (300008069, "national parks"),
    "L.AREA": (300182722, "geographic regions"),
    "L.RGN": (300182722, "geographic regions"),
    "L.CONT": (300128176, "continents"),
    "R.RD": (300008217, "roads"),
    "R.RR": (300120693, "transportation structures"),
    "U.RDGU": (300387581, "undersea landforms"),
    "U.TRNU": (300387581, "undersea landforms"),
    "V.FRST": (300132294, "natural landscapes"),
}


# Curated Pleiades place-type → (aat_id, aat_term) for types that lack a
# source-provided ``same_as`` AAT URI. IDs validated against the production
# aat_hierarchy.json by label. ``cmd_static`` applies these to existing
# pleiades.json entries AND appends any that the vocabulary file is missing
# (several types appear on places but not in the built vocab file).
#
# NB: "station" is DELIBERATELY omitted. In the Pleiades vocabulary it is not a
# generic transit station — it denotes a route stopping-place (mansio/mutatio)
# sense and is applied inconsistently, with no clean AAT equivalent. Mapping it
# would misclassify ~1.2k places, so it is left unmapped on purpose.
PLEIADES_STATIC_MAPPINGS = {
    "fort": (300006909, "forts"),
    "fort-group": (300006909, "forts"),
    "province": (300000774, "provinces"),
    "church": (300007466, "churches (buildings)"),
    "settlement-modern": (300008347, "inhabited places"),
    "city-gate": (300005080, "city gates"),
    "bath": (300007353, "public baths"),
    "cemetery": (300266755, "cemeteries"),
    "aqueduct": (300006165, "aqueducts"),
    "temple": (300007595, "temples (buildings)"),
    "dam": (300006084, "dams"),
    "villa": (300005517, "villas"),
    "military-installation-or-camp-temporary": (300164060, "military installations"),
}


# ============================================================================
# Subcommands
# ============================================================================

def cmd_static():
    """Apply curated static mappings to all data files."""
    applied = 0

    # --- OSM ---
    try:
        osm_data = load_data_file("osm.json")
        for source_label, entry in iter_values(osm_data, "osm"):
            if source_label in OSM_OHM_STATIC_MAPPINGS:
                aat_id, aat_term = OSM_OHM_STATIC_MAPPINGS[source_label]
                set_aat_mapping(entry, aat_id, aat_term, "curated", "static_osm")
                applied += 1
        save_data_file("osm.json", osm_data)
        print(f"  OSM: {applied} mappings applied")
    except FileNotFoundError:
        print("  OSM: skipped (osm.json not found)")

    # --- OHM ---
    ohm_applied = 0
    try:
        ohm_data = load_data_file("ohm.json")
        for source_label, entry in iter_values(ohm_data, "ohm"):
            if source_label in OSM_OHM_STATIC_MAPPINGS:
                aat_id, aat_term = OSM_OHM_STATIC_MAPPINGS[source_label]
                set_aat_mapping(entry, aat_id, aat_term, "curated", "static_ohm")
                ohm_applied += 1
        save_data_file("ohm.json", ohm_data)
        print(f"  OHM: {ohm_applied} mappings applied")
    except FileNotFoundError:
        print("  OHM: skipped (ohm.json not found)")

    # --- GeoNames ---
    gn_applied = 0
    try:
        gn_data = load_data_file("geonames.json")
        for source_label, entry in iter_values(gn_data, "geonames"):
            if source_label in GEONAMES_STATIC_MAPPINGS:
                aat_id, aat_term = GEONAMES_STATIC_MAPPINGS[source_label]
                set_aat_mapping(entry, aat_id, aat_term, "curated", "static_geonames")
                gn_applied += 1
        save_data_file("geonames.json", gn_data)
        print(f"  GeoNames: {gn_applied} mappings applied")
    except FileNotFoundError:
        print("  GeoNames: skipped (geonames.json not found)")

    # --- Pleiades ---
    pl_applied = 0
    try:
        pl_data = load_data_file("pleiades.json")
        seen_ids = set()
        for identifier, entry in iter_values(pl_data, "pleiades"):
            seen_ids.add(identifier)
            # Source same_as URIs take priority (authoritative).
            if "aat_id" in entry and "aat_mapping" not in entry:
                set_aat_mapping(
                    entry, entry["aat_id"],
                    entry.get("label", identifier),
                    "authoritative", "pleiades_same_as"
                )
                pl_applied += 1
            # Curated top-up for types without a source same_as mapping.
            elif "aat_mapping" not in entry and identifier in PLEIADES_STATIC_MAPPINGS:
                aat_id, aat_term = PLEIADES_STATIC_MAPPINGS[identifier]
                set_aat_mapping(entry, aat_id, aat_term, "curated", "static_pleiades")
                pl_applied += 1
        # Several curated types appear on places but are absent from the vocab
        # file — append them so the committed dict fully reproduces coverage.
        pl_values = pl_data.setdefault("values", [])
        for identifier, (aat_id, aat_term) in PLEIADES_STATIC_MAPPINGS.items():
            if identifier not in seen_ids:
                new_entry = {"value": identifier, "label": identifier}
                set_aat_mapping(new_entry, aat_id, aat_term, "curated", "static_pleiades")
                pl_values.append(new_entry)
                pl_applied += 1
        save_data_file("pleiades.json", pl_data)
        print(f"  Pleiades: {pl_applied} mappings applied (same_as + curated static)")
    except FileNotFoundError:
        print("  Pleiades: skipped (pleiades.json not found)")

    total = applied + ohm_applied + gn_applied + pl_applied
    print(f"\nTotal static mappings applied: {total}")


def cmd_sparql(es=None):
    """Match unmapped entries against AAT labels (via local ES or remote SPARQL)."""
    mode = "ES types index" if es else "AAT SPARQL"
    print(f"AAT label matching via {mode} ...")
    matched = 0
    attempted = 0

    for filename, namespace in [
        ("osm.json", "osm"),
        ("ohm.json", "ohm"),
        ("geonames.json", "geonames"),
        ("pleiades.json", "pleiades"),
        ("wikidata.json", "wikidata"),
    ]:
        try:
            data = load_data_file(filename)
        except FileNotFoundError:
            continue

        file_matched = 0
        file_attempted = 0
        for source_label, entry in iter_values(data, namespace):
            # Skip already mapped
            if "aat_mapping" in entry:
                continue

            # Determine a search label
            if namespace == "wikidata":
                label = entry.get("label", "")
            elif namespace == "pleiades":
                label = entry.get("label", entry.get("value", ""))
            elif namespace == "geonames":
                label = entry.get("name", entry.get("value", ""))
            else:
                # OSM/OHM: use the tag value itself
                label = entry.get("value", "")

            if not label or len(label) < 3:
                continue

            # Clean up underscores
            clean_label = label.replace("_", " ")
            attempted += 1
            file_attempted += 1

            if es is not None:
                # --- ES lookup (fast, local) ---
                results = es_label_search(es, clean_label)
                if results:
                    aat_id, aat_term, confidence = results[0]
                    set_aat_mapping(entry, aat_id, aat_term, confidence, "aat_es")
                    file_matched += 1
                    matched += 1
            else:
                # --- SPARQL fallback (slow, remote) ---
                result = sparql_exact_match(clean_label)
                if result:
                    aat_id, aat_term = result
                    set_aat_mapping(entry, aat_id, aat_term, "sparql_exact", "aat_sparql")
                    file_matched += 1
                    matched += 1
                else:
                    results = sparql_label_search(clean_label, limit=3)
                    if results:
                        aat_id, aat_term = results[0]
                        set_aat_mapping(entry, aat_id, aat_term, "sparql_fuzzy", "aat_sparql")
                        file_matched += 1
                        matched += 1
                # Rate-limit SPARQL requests
                time.sleep(0.5)

            if attempted % 500 == 0:
                print(f"    ... {attempted} attempted, {matched} matched")

            # Periodic save every 500 entries (protects long runs)
            if file_attempted % 500 == 0 and file_matched > 0:
                save_data_file(filename, data)
                print(f"    (checkpoint: {file_matched} matches saved)")

        if file_matched:
            save_data_file(filename, data)
        print(f"  {filename}: {file_matched} new matches "
              f"({file_attempted} attempted)")

    print(f"\nLabel matching: {matched}/{attempted} entries matched")


def cmd_wikidata(es=None):
    """Bridge Wikidata Q-items → AAT via P1014 property."""
    try:
        data = load_data_file("wikidata.json")
    except FileNotFoundError:
        print("Error: wikidata.json not found")
        return

    # Collect unmapped Q-items
    unmapped = []
    for qid, entry in iter_values(data, "wikidata"):
        if "aat_mapping" not in entry and qid.startswith("Q"):
            unmapped.append((qid, entry))

    if not unmapped:
        print("No unmapped Wikidata types to process")
        return

    qids = [q for q, _ in unmapped]
    aat_map = fetch_wikidata_aat_mappings(qids)

    # Fetch AAT labels for matched IDs
    matched_aat_ids = set(aat_map.values())
    aat_labels = {}
    if matched_aat_ids:
        if es is not None:
            print(f"  Fetching labels for {len(matched_aat_ids)} AAT concepts from ES ...")
            aat_labels = es_labels_by_ids(es, list(matched_aat_ids))
            print(f"    -> {len(aat_labels)} labels found")
        else:
            print(f"  Fetching labels for {len(matched_aat_ids)} AAT concepts via SPARQL ...")
            for i, aat_id in enumerate(matched_aat_ids):
                label = sparql_label_by_id(aat_id)
                if label:
                    aat_labels[aat_id] = label
                if (i + 1) % 50 == 0:
                    print(f"    ... {i + 1}/{len(matched_aat_ids)} labels fetched")
                time.sleep(0.3)

    # Apply mappings
    applied = 0
    for qid, entry in unmapped:
        if qid in aat_map:
            aat_id = aat_map[qid]
            aat_term = aat_labels.get(aat_id, f"aat:{aat_id}")
            set_aat_mapping(entry, aat_id, aat_term, "wikidata_P1014", "wikidata_bridge")
            applied += 1

    save_data_file("wikidata.json", data)
    print(f"\nWikidata → AAT bridge: {applied}/{len(unmapped)} types mapped")


def cmd_validate():
    """Validate existing AAT IDs by checking them against the AAT API."""
    print("Validating AAT IDs ...")
    valid = 0
    invalid = 0
    total = 0

    for filename, namespace in [
        ("osm.json", "osm"),
        ("ohm.json", "ohm"),
        ("geonames.json", "geonames"),
        ("wikidata.json", "wikidata"),
        ("pleiades.json", "pleiades"),
    ]:
        try:
            data = load_data_file(filename)
        except FileNotFoundError:
            continue

        for source_label, entry in iter_values(data, namespace):
            mapping = entry.get("aat_mapping")
            if not mapping:
                continue

            aat_id = mapping.get("aat_id")
            if not aat_id:
                continue

            total += 1
            if total % 50 == 0:
                print(f"  ... checked {total} ({valid} valid, {invalid} invalid)")

            try:
                url = AAT_JSON_API.format(aat_id=aat_id)
                req = Request(url, headers={
                    "User-Agent": "WHG-indexing/1.0",
                    "Accept": "application/json",
                })
                with urlopen(req, timeout=15) as resp:
                    if resp.status == 200:
                        valid += 1
                        mapping["validated"] = True
                    else:
                        invalid += 1
                        mapping["validated"] = False
                        print(f"    INVALID: aat:{aat_id} → HTTP {resp.status}")
            except Exception as e:
                invalid += 1
                mapping["validated"] = False
                print(f"    INVALID: aat:{aat_id} → {e}")

            time.sleep(0.3)

        if total:
            save_data_file(filename, data)

    print(f"\nValidation complete: {valid} valid, {invalid} invalid out of {total}")


def cmd_report():
    """Report AAT mapping coverage across all data files."""
    print("=" * 70)
    print("AAT MAPPING COVERAGE REPORT")
    print("=" * 70)

    grand_total = 0
    grand_mapped = 0

    for filename, namespace in [
        ("osm.json", "osm"),
        ("ohm.json", "ohm"),
        ("geonames.json", "geonames"),
        ("wikidata.json", "wikidata"),
        ("pleiades.json", "pleiades"),
    ]:
        try:
            data = load_data_file(filename)
        except FileNotFoundError:
            continue

        total = 0
        mapped = 0
        by_source = {}
        by_confidence = {}

        for source_label, entry in iter_values(data, namespace):
            total += 1
            mapping = entry.get("aat_mapping")
            if mapping:
                mapped += 1
                src = mapping.get("source", "unknown")
                by_source[src] = by_source.get(src, 0) + 1
                conf = mapping.get("confidence", "unknown")
                by_confidence[conf] = by_confidence.get(conf, 0) + 1

        pct = (mapped / total * 100) if total else 0
        print(f"\n{filename} ({namespace}):")
        print(f"  Total types: {total}")
        print(f"  Mapped: {mapped} ({pct:.1f}%)")
        if by_source:
            print(f"  By source: {json.dumps(by_source, indent=4)}")
        if by_confidence:
            print(f"  By confidence: {json.dumps(by_confidence, indent=4)}")

        grand_total += total
        grand_mapped += mapped

    pct = (grand_mapped / grand_total * 100) if grand_total else 0
    print(f"\n{'=' * 70}")
    print(f"TOTAL: {grand_mapped}/{grand_total} ({pct:.1f}%) mapped across all vocabularies")


# ============================================================================
# CLI
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="AAT mapping augmentation for WHG type vocabulary files"
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("static", help="Apply curated static mappings")

    sp_sparql = subparsers.add_parser("sparql",
        help="Match unmapped entries against AAT labels (ES or SPARQL)")
    sp_sparql.add_argument("--es-host",
        help="ES host with types index (omit to fall back to AAT SPARQL)")

    sp_wd = subparsers.add_parser("wikidata",
        help="Bridge Wikidata → AAT via P1014")
    sp_wd.add_argument("--es-host",
        help="ES host with types index for label lookups (omit for SPARQL)")

    subparsers.add_parser("validate", help="Validate existing AAT IDs")
    subparsers.add_parser("report", help="Report mapping coverage")

    args = parser.parse_args()

    # Create ES client for subcommands that support it
    es = None
    if getattr(args, "es_host", None):
        from typesystem.es_client import create_client
        es = create_client(args.es_host)
        print(f"Using ES types index at {args.es_host}")

    if args.command == "static":
        cmd_static()
    elif args.command == "sparql":
        cmd_sparql(es=es)
    elif args.command == "wikidata":
        cmd_wikidata(es=es)
    elif args.command == "validate":
        cmd_validate()
    elif args.command == "report":
        cmd_report()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

