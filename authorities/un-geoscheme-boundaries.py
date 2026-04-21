# authorities/un-geoscheme-boundaries.py

"""
UN M49 Geoscheme Boundary Generator.

Derives supra-national boundary polygons (continents and subregions) by
unioning level-2 country boundaries from the ``places`` ES index (filtering
on ``boundary`` field value "2").  Also fetches Antarctica from the Overpass
API (it's tagged ``boundary=continent`` in OSM).

Produces place docs at:
  - boundary="0": 7 continental macro-regions (Africa, Americas, Asia,
    Europe, Oceania, Antarctica)
  - boundary="1": 22 geographical subregions + 2 intermediary regions

Uses ``osm:`` namespace with synthetic deterministic IDs (e.g.
``osm:m49_africa``, ``osm:m49_eastern_africa``).

Usage:
    python -m authorities.un-geoscheme-boundaries --es-host URL
    python -m authorities.un-geoscheme-boundaries --es-host URL --dry-run
"""

import argparse
from datetime import datetime

import requests
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from shapely.validation import make_valid

from elasticsearch import Elasticsearch, helpers
from processing.helpers import enrich_geometry, compute_h3_fields

# ---------------------------------------------------------------------------
# UN M49 Geoscheme — country code → subregion → continent
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

M49_CONTINENTS = {
    'Africa':   {'wikidata': 'Q15',   'ccodes_label': 'AF'},
    'Americas': {'wikidata': 'Q828',  'ccodes_label': None},
    'Asia':     {'wikidata': 'Q48',   'ccodes_label': 'AS'},
    'Europe':   {'wikidata': 'Q46',   'ccodes_label': 'EU'},
    'Oceania':  {'wikidata': 'Q538',  'ccodes_label': 'OC'},
}

ANTARCTICA = {
    'name': 'Antarctica',
    'wikidata': 'Q51',
    'osm_relation': 2186646,
    'ccodes': ['AQ'],
}

OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
OVERPASS_FALLBACK = 'https://overpass.kumi.systems/api/interpreter'


# ---------------------------------------------------------------------------
# Fetch country geometries from the places index
# ---------------------------------------------------------------------------

def fetch_country_geometries(es, places_index='places_*'):
    """
    Fetch all boundary='2' places from the places index, keyed by country code.

    Returns:
        dict mapping ISO alpha-2 code → Shapely geometry
    """
    print("Fetching boundary='2' places from ES ...")
    query = {
        'query': {'term': {'boundary': '2'}},
        '_source': ['ccodes', 'geometries', 'place_id', 'title'],
        'size': 500,
    }

    geometries = {}  # cc → list of Shapely geoms
    count = 0

    resp = es.search(index=places_index, body=query, scroll='5m')
    scroll_id = resp['_scroll_id']

    while True:
        hits = resp['hits']['hits']
        if not hits:
            break
        for hit in hits:
            src = hit['_source']
            ccodes = src.get('ccodes', [])
            geom_list = src.get('geometries', [])
            if not geom_list or not ccodes:
                continue
            geom_data = geom_list[0].get('geom') if geom_list else None
            if not geom_data:
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

    result = {}
    for cc, geoms in geometries.items():
        if len(geoms) == 1:
            result[cc] = geoms[0]
        else:
            result[cc] = unary_union(geoms)

    print(f"  {count} country places fetched ({len(result)} unique ccodes)")
    return result


# ---------------------------------------------------------------------------
# Antarctica from Overpass
# ---------------------------------------------------------------------------

def fetch_antarctica():
    """Fetch Antarctica's polygon from the Overpass API."""
    rel_id = ANTARCTICA['osm_relation']
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

            data = resp.json()
            elements = data.get('elements', [])
            if not elements:
                print("    No elements returned")
                continue

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

            print(f"    ✓ Antarctica: {geom.geom_type}")
            return geom

        except Exception as e:
            print(f"    ✗ Failed: {e}")
            continue

    print("    ✗ Could not fetch Antarctica from any Overpass endpoint")
    return None


# ---------------------------------------------------------------------------
# Build place documents
# ---------------------------------------------------------------------------

def _make_place_id(name):
    """Generate a deterministic osm: namespace place_id for M49 regions."""
    slug = name.lower().replace(' ', '_').replace('-', '_')
    return f"osm:m49_{slug}"


def build_geoscheme_place_doc(name, boundary_value, geom,
                              wikidata_id=None, ccodes=None):
    """Build a places-index doc from a Shapely geometry."""
    place_id = _make_place_id(name)
    raw_geom = mapping(geom)
    geom_entry = enrich_geometry(raw_geom, geom_key=f"{place_id}_0")
    if not geom_entry:
        return None

    doc = {
        'place_id': place_id,
        'namespace': 'osm',
        'title': name,
        'toponyms': [{'toponym_id': f"{name}@en"}],
        'geometries': [geom_entry],
        'types': [{
            'identifier': 'synthetic_backfill',
            'label': 'aat',
            'sourceLabel': 'm49-derived',
        }],
        'boundary': boundary_value,
        'indexed_at': datetime.now().isoformat(),
    }
    if geom_entry.get('repr_point'):
        rp = geom_entry['repr_point']
        h3c, h3cover = compute_h3_fields(rp['lon'], rp['lat'], raw_geom)
        if h3c:
            doc['h3_centroid'] = h3c
            doc['h3_cover'] = h3cover

    if ccodes:
        doc['ccodes'] = ccodes

    if wikidata_id:
        doc['relations'] = [{
            'relation_type': 'sameAs',
            'related_place_id': f"wd:{wikidata_id}",
            'label': 'Wikidata',
        }]

    return doc


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _verify_country_coverage(country_geoms):
    """Check that all M49 country codes are accounted for."""
    all_needed = set()
    for info in M49_SUBREGIONS.values():
        all_needed.update(info['ccodes'])

    found = set(country_geoms.keys())
    missing = all_needed - found

    if 'FR' in missing:
        print("  NOTE: France (FR) not found — may use ISO3166-1=FR tag "
              "instead of ISO3166-1:alpha2=FR")

    if missing:
        print(f"\n  WARNING: {len(missing)} M49 country codes not found:")
        print(f"    {', '.join(sorted(missing))}")
    else:
        print(f"  ✓ All {len(all_needed)} M49 country codes found")

    if 'AQ' not in found:
        print("  NOTE: Antarctica (AQ) will be fetched from Overpass separately")

    return missing


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def generate_geoscheme(es_host, dry_run=False, places_index='places_*',
                       target_index='places'):
    """Generate UN M49 geoscheme place docs from existing boundary='2' data."""
    print("=" * 70)
    print("UN M49 GEOSCHEME BOUNDARY GENERATION")
    print("=" * 70)

    es = Elasticsearch(es_host, request_timeout=120, max_retries=5,
                       retry_on_timeout=True)

    country_geoms = fetch_country_geometries(es, places_index)
    if not country_geoms:
        print("\nERROR: No boundary='2' places found.")
        return []

    _verify_country_coverage(country_geoms)

    all_docs = []

    # Subregions (boundary="1")
    print("\nBuilding subregions (boundary='1') ...")
    for name, info in M49_SUBREGIONS.items():
        geoms = [country_geoms[cc] for cc in info['ccodes'] if cc in country_geoms]
        found_codes = [cc for cc in info['ccodes'] if cc in country_geoms]
        if not geoms:
            print(f"  ✗ {name}: no country geometries found")
            continue
        union = make_valid(unary_union(geoms))
        doc = build_geoscheme_place_doc(
            name, '1', union, info.get('wikidata'), found_codes)
        if doc:
            all_docs.append(doc)
            print(f"  ✓ {name}: {len(found_codes)}/{len(info['ccodes'])} countries")

    # Intermediary regions (boundary="1")
    print("\nBuilding intermediary regions (boundary='1') ...")
    for name, info in M49_INTERMEDIARY.items():
        all_codes = []
        for sub_name in info['subregions']:
            all_codes.extend(M49_SUBREGIONS.get(sub_name, {}).get('ccodes', []))
        geoms = [country_geoms[cc] for cc in all_codes if cc in country_geoms]
        found_codes = sorted(set(cc for cc in all_codes if cc in country_geoms))
        if not geoms:
            print(f"  ✗ {name}: no geometries")
            continue
        union = make_valid(unary_union(geoms))
        doc = build_geoscheme_place_doc(
            name, '1', union, info.get('wikidata'), found_codes)
        if doc:
            all_docs.append(doc)
            print(f"  ✓ {name}: {len(found_codes)} countries")

    # Continents (boundary="0")
    print("\nBuilding continents (boundary='0') ...")
    continent_codes = {}
    for name, info in M49_SUBREGIONS.items():
        continent_codes.setdefault(info['continent'], set()).update(info['ccodes'])
    for name, info in M49_CONTINENTS.items():
        codes = continent_codes.get(name, set())
        geoms = [country_geoms[cc] for cc in codes if cc in country_geoms]
        found_codes = sorted(cc for cc in codes if cc in country_geoms)
        if not geoms:
            print(f"  ✗ {name}: no geometries")
            continue
        union = make_valid(unary_union(geoms))
        doc = build_geoscheme_place_doc(
            name, '0', union, info.get('wikidata'), found_codes)
        if doc:
            all_docs.append(doc)
            print(f"  ✓ {name}: {len(found_codes)} countries")

    # Antarctica
    print("\nFetching Antarctica ...")
    antarctica_geom = fetch_antarctica()
    if antarctica_geom:
        doc = build_geoscheme_place_doc(
            'Antarctica', '0', antarctica_geom,
            ANTARCTICA['wikidata'], ANTARCTICA['ccodes'])
        if doc:
            doc['place_id'] = f"osm:r{ANTARCTICA['osm_relation']}"
            all_docs.append(doc)
            print("  ✓ Antarctica added")
    else:
        print("  ✗ Antarctica skipped (Overpass unavailable)")

    # Summary
    print("\n--- Summary ---")
    level_counts = {}
    for d in all_docs:
        lvl = d['boundary']
        level_counts[lvl] = level_counts.get(lvl, 0) + 1
    for lvl in sorted(level_counts):
        print(f"  boundary='{lvl}': {level_counts[lvl]}")
    print(f"  Total: {len(all_docs)} documents")

    if dry_run:
        print("\nDRY RUN — skipping indexing.")
        for d in all_docs:
            print(f"  {d['place_id']}: {d['title']} (boundary={d['boundary']})")
    else:
        print(f"\nIndexing {len(all_docs)} docs into '{target_index}' ...")
        actions = [
            {'_index': target_index, '_id': doc['place_id'], '_source': doc}
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
                    print(f"  Bulk error: {info}")
        print(f"  ✓ Indexed: {success}")
        if failed:
            print(f"  ✗ Failed: {failed}")

    return all_docs


def main():
    parser = argparse.ArgumentParser(
        description="Generate UN M49 geoscheme boundaries from existing "
                    "boundary='2' places in ES",
    )
    parser.add_argument('--es-host', required=True, help='Elasticsearch URL')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--places-index', default='places_*',
                        help='Source places index pattern')
    parser.add_argument('--target-index', default='places',
                        help='Target index for writing docs')
    args = parser.parse_args()

    generate_geoscheme(
        es_host=args.es_host,
        dry_run=args.dry_run,
        places_index=args.places_index,
        target_index=args.target_index,
    )


if __name__ == '__main__':
    main()

