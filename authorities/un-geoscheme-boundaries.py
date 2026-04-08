# authorities/un-geoscheme-boundaries.py

"""
UN M49 Geoscheme Boundary Generator.

Derives supra-national boundary polygons (continents and subregions) by
unioning the admin_level=2 country boundaries already in the ``boundaries``
ES index.  Also fetches Antarctica from the Overpass API (it's tagged
``boundary=continent`` in OSM, not ``boundary=administrative``).

Produces boundary docs at:
  - admin_level=0: 7 continental macro-regions (Africa, Americas, Asia,
    Europe, Oceania, Antarctica — the 6 UN continents plus Antarctica)
  - admin_level=1: 22 geographical subregions + 2 intermediary regions

The M49 country→region mapping is based on ISO 3166-1 alpha-2 codes,
matching the ``ccodes`` field on existing level-2 boundary docs.

Usage:
    # Derive from production ES:
    python -m authorities.un-geoscheme-boundaries --es-host URL

    # Dry run — report what would be created:
    python -m authorities.un-geoscheme-boundaries --es-host URL --dry-run

    # Write GeoJSON Lines file for mbtiles (append mode):
    python -m authorities.un-geoscheme-boundaries --es-host URL --geojsonl FILE
"""

import argparse
from datetime import datetime

import orjson
import requests
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from shapely.validation import make_valid

from elasticsearch import Elasticsearch, helpers
from processing.helpers import compute_representative_point
from processing.settings import BOUNDARIES_INDEX

# ---------------------------------------------------------------------------
# UN M49 Geoscheme — country code → subregion → continent
#
# Source: https://unstats.un.org/unsd/methodology/m49/
# Codes are ISO 3166-1 alpha-2.
# ---------------------------------------------------------------------------

M49_SUBREGIONS = {
    # ---- AFRICA ----
    'Northern Africa': {
        'continent': 'Africa',
        'wikidata': 'Q27381',
        'ccodes': ['DZ', 'EG', 'LY', 'MA', 'SD', 'TN', 'EH'],
    },
    'Eastern Africa': {
        'continent': 'Africa',
        'wikidata': 'Q27407',
        'ccodes': ['BI', 'KM', 'DJ', 'ER', 'ET', 'KE', 'MG', 'MW', 'MU',
                   'MZ', 'RE', 'RW', 'SC', 'SO', 'SS', 'TZ', 'UG', 'ZM',
                   'ZW', 'YT', 'IO'],
    },
    'Middle Africa': {
        'continent': 'Africa',
        'wikidata': 'Q27433',
        'ccodes': ['AO', 'CM', 'CF', 'TD', 'CG', 'CD', 'GQ', 'GA', 'ST'],
    },
    'Southern Africa': {
        'continent': 'Africa',
        'wikidata': 'Q27394',
        'ccodes': ['BW', 'SZ', 'LS', 'NA', 'ZA'],
    },
    'Western Africa': {
        'continent': 'Africa',
        'wikidata': 'Q4412',
        'ccodes': ['BJ', 'BF', 'CV', 'CI', 'GM', 'GH', 'GN', 'GW', 'LR',
                   'ML', 'MR', 'NE', 'NG', 'SN', 'SL', 'TG', 'SH'],
    },

    # ---- AMERICAS ----
    'Caribbean': {
        'continent': 'Americas',
        'wikidata': 'Q664609',
        'ccodes': ['AI', 'AG', 'AW', 'BS', 'BB', 'BQ', 'VG', 'KY', 'CU',
                   'CW', 'DM', 'DO', 'GD', 'GP', 'HT', 'JM', 'MQ', 'MS',
                   'PR', 'BL', 'KN', 'LC', 'MF', 'VC', 'SX', 'TT', 'TC',
                   'VI'],
    },
    'Central America': {
        'continent': 'Americas',
        'wikidata': 'Q27611',
        'ccodes': ['BZ', 'CR', 'SV', 'GT', 'HN', 'MX', 'NI', 'PA'],
    },
    'South America': {
        'continent': 'Americas',
        'wikidata': 'Q18',
        'ccodes': ['AR', 'BO', 'BR', 'CL', 'CO', 'EC', 'FK', 'GF', 'GY',
                   'PY', 'PE', 'SR', 'UY', 'VE'],
    },
    'Northern America': {
        'continent': 'Americas',
        'wikidata': 'Q2017699',
        'ccodes': ['BM', 'CA', 'GL', 'PM', 'US'],
    },

    # ---- ASIA ----
    'Central Asia': {
        'continent': 'Asia',
        'wikidata': 'Q27275',
        'ccodes': ['KZ', 'KG', 'TJ', 'TM', 'UZ'],
    },
    'Eastern Asia': {
        'continent': 'Asia',
        'wikidata': 'Q27231',
        'ccodes': ['CN', 'HK', 'MO', 'JP', 'MN', 'KP', 'KR', 'TW'],
    },
    'South-eastern Asia': {
        'continent': 'Asia',
        'wikidata': 'Q11708',
        'ccodes': ['BN', 'KH', 'ID', 'LA', 'MY', 'MM', 'PH', 'SG', 'TH',
                   'TL', 'VN'],
    },
    'Southern Asia': {
        'continent': 'Asia',
        'wikidata': 'Q771405',
        'ccodes': ['AF', 'BD', 'BT', 'IN', 'IR', 'MV', 'NP', 'PK', 'LK'],
    },
    'Western Asia': {
        'continent': 'Asia',
        'wikidata': 'Q27293',
        'ccodes': ['AM', 'AZ', 'BH', 'CY', 'GE', 'IQ', 'IL', 'JO', 'KW',
                   'LB', 'OM', 'PS', 'QA', 'SA', 'SY', 'TR', 'AE', 'YE'],
    },

    # ---- EUROPE ----
    'Eastern Europe': {
        'continent': 'Europe',
        'wikidata': 'Q27468',
        'ccodes': ['BY', 'BG', 'CZ', 'HU', 'PL', 'MD', 'RO', 'RU', 'SK',
                   'UA'],
    },
    'Northern Europe': {
        'continent': 'Europe',
        'wikidata': 'Q27479',
        'ccodes': ['AX', 'DK', 'EE', 'FO', 'FI', 'GG', 'IS', 'IE', 'IM',
                   'JE', 'LV', 'LT', 'NO', 'SJ', 'SE', 'GB'],
    },
    'Southern Europe': {
        'continent': 'Europe',
        'wikidata': 'Q27449',
        'ccodes': ['AL', 'AD', 'BA', 'HR', 'GI', 'GR', 'VA', 'IT', 'MT',
                   'ME', 'MK', 'PT', 'SM', 'RS', 'SI', 'ES'],
    },
    'Western Europe': {
        'continent': 'Europe',
        'wikidata': 'Q27496',
        'ccodes': ['AT', 'BE', 'FR', 'DE', 'LI', 'LU', 'MC', 'NL', 'CH'],
    },

    # ---- OCEANIA ----
    'Australia and New Zealand': {
        'continent': 'Oceania',
        'wikidata': 'Q45256',
        'ccodes': ['AU', 'NZ', 'NF'],
    },
    'Melanesia': {
        'continent': 'Oceania',
        'wikidata': 'Q37394',
        'ccodes': ['FJ', 'NC', 'PG', 'SB', 'VU'],
    },
    'Micronesia': {
        'continent': 'Oceania',
        'wikidata': 'Q3359409',
        'ccodes': ['GU', 'KI', 'MH', 'FM', 'NR', 'MP', 'PW'],
    },
    'Polynesia': {
        'continent': 'Oceania',
        'wikidata': 'Q35942',
        'ccodes': ['AS', 'CK', 'PF', 'NU', 'PN', 'WS', 'TK', 'TO', 'TV',
                   'WF'],
    },
}

# Intermediary regions (unions of subregions — overlap with child subregions)
M49_INTERMEDIARY = {
    'Sub-Saharan Africa': {
        'continent': 'Africa',
        'wikidata': 'Q132959',
        'subregions': ['Eastern Africa', 'Middle Africa', 'Southern Africa',
                       'Western Africa'],
    },
    'Latin America and the Caribbean': {
        'continent': 'Americas',
        'wikidata': 'Q12585',
        'subregions': ['Caribbean', 'Central America', 'South America'],
    },
}

# Continental macro-regions (derived from subregions)
M49_CONTINENTS = {
    'Africa':   {'wikidata': 'Q15',   'ccodes_label': 'AF'},
    'Americas': {'wikidata': 'Q828',  'ccodes_label': None},
    'Asia':     {'wikidata': 'Q48',   'ccodes_label': 'AS'},
    'Europe':   {'wikidata': 'Q46',   'ccodes_label': 'EU'},
    'Oceania':  {'wikidata': 'Q538',  'ccodes_label': 'OC'},
}

# Antarctica — fetched separately from Overpass (boundary=continent)
ANTARCTICA = {
    'name': 'Antarctica',
    'wikidata': 'Q51',
    'osm_relation': 2186646,
    'ccodes': ['AQ'],
}

OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
OVERPASS_FALLBACK = 'https://overpass.kumi.systems/api/interpreter'

ADMIN_LEVEL_MINZOOM = {
    0: 0,   # continents — always visible
    1: 2,   # subregions — from zoom 2
}


# ---------------------------------------------------------------------------
# Fetch country geometries from ES
# ---------------------------------------------------------------------------

def fetch_country_geometries(es, index=BOUNDARIES_INDEX):
    """
    Fetch all admin_level=2 boundaries from ES, keyed by country code.

    Returns:
        dict mapping ISO alpha-2 code → Shapely geometry
    """
    print("Fetching admin_level=2 boundaries from ES ...")
    query = {
        'query': {'term': {'admin_level': 2}},
        '_source': ['ccodes', 'geom', 'boundary_id', 'name'],
        'size': 500,
    }

    geometries = {}  # cc → list of Shapely geoms (some countries have multiple relations)
    count = 0

    # Use scroll for potentially large result sets
    resp = es.search(index=index, body=query, scroll='5m')
    scroll_id = resp['_scroll_id']

    while True:
        hits = resp['hits']['hits']
        if not hits:
            break
        for hit in hits:
            src = hit['_source']
            ccodes = src.get('ccodes', [])
            geom_data = src.get('geom')
            if not geom_data or not ccodes:
                continue
            try:
                geom = shape(geom_data)
                if not geom.is_valid:
                    geom = make_valid(geom)
                if geom.is_empty:
                    continue
                for cc in ccodes:
                    cc = cc.upper()
                    geometries.setdefault(cc, []).append(geom)
                count += 1
            except Exception:
                continue
        resp = es.scroll(scroll_id=scroll_id, scroll='5m')

    try:
        es.clear_scroll(scroll_id=scroll_id)
    except Exception:
        pass

    # Union multiple relations per country
    result = {}
    for cc, geoms in geometries.items():
        if len(geoms) == 1:
            result[cc] = geoms[0]
        else:
            result[cc] = unary_union(geoms)

    print("  %d country boundaries fetched (%d unique ccodes)" %
          (count, len(result)))
    return result


# ---------------------------------------------------------------------------
# Antarctica from Overpass
# ---------------------------------------------------------------------------

def fetch_antarctica():
    """
    Fetch Antarctica's polygon from the Overpass API.

    Returns Shapely geometry, or None on failure.
    """
    rel_id = ANTARCTICA['osm_relation']  # 2186646
    query = (
        '[out:json][timeout:120];'
        'relation(%d);'
        'out geom;' % rel_id
    )

    for endpoint in [OVERPASS_URL, OVERPASS_FALLBACK]:
        print(f"  Fetching Antarctica (r{rel_id}) from {endpoint} ...")
        try:
            resp = requests.post(
                endpoint,
                data={'data': query},
                timeout=180,
                headers={'User-Agent': 'WHG-Geoscheme/1.0'},
            )
            resp.raise_for_status()

            # Cache raw response for debugging
            data = resp.json()
            elements = data.get('elements', [])
            if not elements:
                print("    No elements returned")
                continue

            # Use osm2geojson to assemble the multipolygon
            import osm2geojson
            geojson = osm2geojson.json2geojson(data)
            features = geojson.get('features', [])
            if not features:
                print("    osm2geojson produced no features")
                continue

            geom = shape(features[0]['geometry'])
            if not geom.is_valid:
                geom = make_valid(geom)
            if geom.is_empty:
                print("    Empty geometry after validation")
                continue

            print("    ✓ Antarctica: %s (%.0f km² approx)" %
                  (geom.geom_type, geom.area * 12321))  # rough deg²→km²
            return geom

        except Exception as e:
            print("    ✗ Failed: %s" % e)
            continue

    print("    ✗ Could not fetch Antarctica from any Overpass endpoint")
    return None


# ---------------------------------------------------------------------------
# Build boundary documents
# ---------------------------------------------------------------------------

def build_boundary_doc(name, admin_level, geom, namespace='m49',
                       wikidata_id=None, ccodes=None):
    """Build a boundary ES doc from a Shapely geometry."""
    boundary_id = "%s:%s" % (namespace, name.lower().replace(' ', '_')
                             .replace('-', '_'))
    full_geom = mapping(geom)

    doc = {
        'boundary_id': boundary_id,
        'namespace': namespace,
        'name': name,
        'source': 'm49',
        'admin_level': admin_level,
        'indexed_at': datetime.now().isoformat(),
        'geom': full_geom,
    }

    # Convex hull
    try:
        hull = geom.convex_hull.simplify(0).buffer(0)
        if hull.is_empty or not hull.is_valid:
            hull = geom.envelope
    except Exception:
        hull = geom.envelope

    if hull and not hull.is_empty:
        doc['hull'] = mapping(hull)
        hb = hull.bounds
        doc['bounds'] = [round(hb[0], 6), round(hb[1], 6),
                         round(hb[2], 6), round(hb[3], 6)]

    rep_point = compute_representative_point(full_geom)
    if rep_point:
        doc['repr_point'] = rep_point

    if wikidata_id:
        doc['wikidata_id'] = wikidata_id

    if ccodes:
        doc['ccodes'] = ccodes

    return doc


def geojsonl_feature(doc):
    """Build a GeoJSON Feature dict for GeoJSON Lines output."""
    props = {
        'id': doc['boundary_id'],
        'name': doc['name'],
        'admin_level': doc['admin_level'],
        'namespace': doc['namespace'],
        'source': doc.get('source', doc['namespace']),
    }
    minzoom = ADMIN_LEVEL_MINZOOM.get(doc['admin_level'], 0)
    if minzoom > 0:
        props['tippecanoe:minzoom'] = minzoom
    if 'ccodes' in doc:
        props['ccodes'] = ','.join(doc['ccodes'])
    if 'wikidata_id' in doc:
        props['wikidata_id'] = doc['wikidata_id']
    return {
        'type': 'Feature',
        'properties': props,
        'geometry': doc['geom'],
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def generate_geoscheme(es_host, dry_run=False, geojsonl_path=None):
    """
    Generate UN M49 geoscheme boundaries from existing level-2 data in ES.
    """
    print("=" * 70)
    print("UN M49 GEOSCHEME BOUNDARY GENERATION")
    print("=" * 70)

    es = Elasticsearch(es_host, request_timeout=120, max_retries=5,
                       retry_on_timeout=True)

    # Step 1: Fetch country geometries
    country_geoms = fetch_country_geometries(es)
    if not country_geoms:
        print("\nERROR: No level-2 boundaries found. Cannot derive geoscheme.")
        return []

    all_docs = []
    missing_codes = set()

    # Step 2: Build subregion geometries (admin_level=1)
    print("\nBuilding subregions (admin_level=1) ...")
    for name, info in M49_SUBREGIONS.items():
        geoms = []
        found_codes = []
        for cc in info['ccodes']:
            if cc in country_geoms:
                geoms.append(country_geoms[cc])
                found_codes.append(cc)
            else:
                missing_codes.add(cc)

        if not geoms:
            print("  ✗ %s: no country geometries found" % name)
            continue

        union = unary_union(geoms)
        if not union.is_valid:
            union = make_valid(union)

        doc = build_boundary_doc(
            name, admin_level=1, geom=union,
            wikidata_id=info.get('wikidata'),
            ccodes=found_codes,
        )
        all_docs.append(doc)
        print("  ✓ %s: %d/%d countries" %
              (name, len(found_codes), len(info['ccodes'])))

    # Step 3: Build intermediary regions (admin_level=1)
    print("\nBuilding intermediary regions (admin_level=1) ...")
    for name, info in M49_INTERMEDIARY.items():
        # Union the constituent subregions' country codes
        all_codes = []
        for sub_name in info['subregions']:
            sub = M49_SUBREGIONS.get(sub_name, {})
            all_codes.extend(sub.get('ccodes', []))

        geoms = []
        found_codes = []
        for cc in all_codes:
            if cc in country_geoms:
                geoms.append(country_geoms[cc])
                found_codes.append(cc)

        if not geoms:
            print("  ✗ %s: no geometries" % name)
            continue

        union = unary_union(geoms)
        if not union.is_valid:
            union = make_valid(union)

        doc = build_boundary_doc(
            name, admin_level=1, geom=union,
            wikidata_id=info.get('wikidata'),
            ccodes=sorted(set(found_codes)),
        )
        all_docs.append(doc)
        print("  ✓ %s: %d countries" % (name, len(set(found_codes))))

    # Step 4: Build continent geometries (admin_level=0)
    print("\nBuilding continents (admin_level=0) ...")

    # Collect all ccodes per continent from subregions
    continent_codes = {}
    for name, info in M49_SUBREGIONS.items():
        cont = info['continent']
        continent_codes.setdefault(cont, set()).update(info['ccodes'])

    for name, info in M49_CONTINENTS.items():
        codes = continent_codes.get(name, set())
        geoms = []
        found_codes = []
        for cc in codes:
            if cc in country_geoms:
                geoms.append(country_geoms[cc])
                found_codes.append(cc)

        if not geoms:
            print("  ✗ %s: no geometries" % name)
            continue

        union = unary_union(geoms)
        if not union.is_valid:
            union = make_valid(union)

        doc = build_boundary_doc(
            name, admin_level=0, geom=union,
            wikidata_id=info.get('wikidata'),
            ccodes=sorted(set(found_codes)),
        )
        all_docs.append(doc)
        print("  ✓ %s: %d countries" % (name, len(set(found_codes))))

    # Step 5: Antarctica (from Overpass)
    print("\nFetching Antarctica ...")
    antarctica_geom = fetch_antarctica()
    if antarctica_geom:
        doc = build_boundary_doc(
            'Antarctica', admin_level=0, geom=antarctica_geom,
            namespace='osm',
            wikidata_id=ANTARCTICA['wikidata'],
            ccodes=ANTARCTICA['ccodes'],
        )
        # Override boundary_id to use the OSM relation ID
        doc['boundary_id'] = f"osm:r{ANTARCTICA['osm_relation']}"
        all_docs.append(doc)
        print("  ✓ Antarctica added")
    else:
        print("  ✗ Antarctica skipped (Overpass unavailable)")

    # Report
    if missing_codes:
        print("\nWARNING: %d country codes not found in boundaries index:" %
              len(missing_codes))
        print("  %s" % ', '.join(sorted(missing_codes)))

    print("\n--- Summary ---")
    level_counts = {}
    for d in all_docs:
        lvl = d['admin_level']
        level_counts[lvl] = level_counts.get(lvl, 0) + 1
    for lvl in sorted(level_counts):
        print("  admin_level=%d: %d" % (lvl, level_counts[lvl]))
    print("  Total: %d documents" % len(all_docs))

    if dry_run:
        print("\nDRY RUN — skipping indexing.")
        for d in all_docs:
            print("  %s: %s (level %d)" %
                  (d['boundary_id'], d['name'], d['admin_level']))
    else:
        # Index
        print("\nIndexing %d docs into %s ..." % (len(all_docs), BOUNDARIES_INDEX))
        actions = [
            {'_index': BOUNDARIES_INDEX, '_id': doc['boundary_id'],
             '_source': doc}
            for doc in all_docs
        ]

        success = 0
        failed = 0
        for ok, info in helpers.parallel_bulk(
            es, actions, thread_count=2, raise_on_error=False,
        ):
            if ok:
                success += 1
            else:
                failed += 1
                if failed <= 5:
                    print("  Bulk error: %s" % str(info))

        print("  ✓ Indexed: %d" % success)
        if failed:
            print("  ✗ Failed: %d" % failed)

    # Write GeoJSON Lines if requested
    if geojsonl_path:
        print("\nWriting GeoJSON Lines to %s ..." % geojsonl_path)
        with open(geojsonl_path, 'ab') as f:
            for doc in all_docs:
                feature = geojsonl_feature(doc)
                f.write(orjson.dumps(feature))
                f.write(b'\n')
        print("  ✓ %d features written" % len(all_docs))

    return all_docs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate UN M49 geoscheme boundaries from existing "
                    "admin_level=2 data in ES",
    )
    parser.add_argument(
        '--es-host', required=True,
        help='Elasticsearch URL (e.g. http://localhost:9201)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Report what would be generated without indexing',
    )
    parser.add_argument(
        '--geojsonl',
        help='Append GeoJSON Lines features to this file (for mbtiles)',
    )
    args = parser.parse_args()

    generate_geoscheme(
        es_host=args.es_host,
        dry_run=args.dry_run,
        geojsonl_path=args.geojsonl,
    )


if __name__ == '__main__':
    main()






