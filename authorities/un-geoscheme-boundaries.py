# authorities/un-geoscheme-boundaries.py

"""
UN M49 Geoscheme Boundary Generator (staged-only).

Derives supra-national boundary polygons (continents and subregions) by
unioning level-2 country boundaries from the **staged ``un`` extract**.
Also fetches Antarctica from the Overpass API (it's tagged
``boundary=continent`` in OSM, not present in the un dataset).

Produces place docs at:
  - boundary="0": continental macro-regions (Africa, Americas, Asia,
    Europe, Oceania, Antarctica)
  - boundary="1": geographical subregions + intermediary regions

Output: ``{STAGED_BASE_DIR}/osm/extract/places.jsonl`` (appended).

The M49 records use ``osm:m49_*`` (and ``osm:r<rel_id>`` for Antarctica)
place IDs because they're conceptually OSM-style admin boundaries; this
keeps tile-bucket and namespace-filter logic working unchanged.

**Run order constraints:**

1. Stage ``un`` first (``WHG_STAGING_MODE=1 python -m authorities.un-countries``).
2. Then run this script. It appends ~25 docs to
   ``staged/osm/extract/places.jsonl`` (creating that file if osm hasn't
   been staged yet).
3. ``osm`` extract may run **before or after** this script — but **never
   concurrently**, since both append to the same file.

Re-running this script duplicates its records (``write_staged_place_doc``
appends without dedup). Clean up old ``osm:m49_*`` records from
``osm/extract/places.jsonl`` before re-running, or simply re-stage osm
from scratch.

Usage::

    WHG_STAGING_MODE=1 python -m authorities.un-geoscheme-boundaries
    WHG_STAGING_MODE=1 python -m authorities.un-geoscheme-boundaries --dry-run
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import requests
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from shapely.validation import make_valid

from processing.osm_boundary_geometry import split_at_antimeridian
from processing.helpers import (
    enrich_geometry,
    compute_h3_fields,
    select_h3_cover_geometry,
    write_staged_place_doc,
)
from processing.settings import STAGED_BASE_DIR

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

UN_NAMESPACE = 'un'
OUTPUT_NAMESPACE = 'osm'


# ---------------------------------------------------------------------------
# Read country geometries from staged un
# ---------------------------------------------------------------------------

def _staged_un_jsonl_path() -> Path:
    """Return the path to the staged un extract JSONL.

    Reads from ``extract/`` (the one path un goes through — un has no
    boundary or h3 stages of its own that overwrite this file at JSONL
    level for our purposes). Reading JSONL specifically (not parquet)
    keeps ``geometries[].hull`` intact — the parquet sidecar drops hull
    via ``staged_parquet.strip_hull_for_parquet``.
    """
    return Path(STAGED_BASE_DIR) / UN_NAMESPACE / "extract" / "places.jsonl"


def fetch_country_geometries():
    """Read all un docs from the staged extract and key them by ccode.

    Returns ``{cc_iso2: shapely_geometry}``. Each un doc carries
    ``ccodes=[<ISO_A2>]`` and one geometry under ``geometries[0]``.
    The full polygon would normally come from the geom_store via
    ``geom_ref``; if the consolidated geom_store isn't available
    (current state of the rebuild — see the cross-cutting issues note
    in execution.md), we fall back to the staged ``hull`` field, which
    is a closed polygon representing each country's outer boundary.
    Hull is good enough for unioning into M49 super-regions.
    """
    jsonl = _staged_un_jsonl_path()
    if not jsonl.exists():
        raise FileNotFoundError(
            f"Staged un extract not found at {jsonl}. "
            "Run ``WHG_STAGING_MODE=1 python -m authorities.un-countries`` first."
        )

    print(f"Reading un records from staged JSONL: {jsonl}")

    geometries: dict[str, list] = {}
    skipped_no_geom = 0
    skipped_no_ccodes = 0
    docs_seen = 0

    with jsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            docs_seen += 1
            ccodes = doc.get("ccodes") or []
            geom_list = doc.get("geometries") or []
            if not ccodes:
                skipped_no_ccodes += 1
                continue
            if not geom_list:
                skipped_no_geom += 1
                continue

            entry = geom_list[0] if isinstance(geom_list[0], dict) else None
            if not entry:
                skipped_no_geom += 1
                continue

            # Prefer the staged hull (always a Polygon/MultiPolygon for un
            # countries, written by ``enrich_geometry`` with ``geom_key``).
            hull = entry.get("hull")
            if not hull:
                skipped_no_geom += 1
                continue

            try:
                geom = shape(hull)
                if not geom.is_valid:
                    geom = make_valid(geom)
                if geom.is_empty:
                    continue
            except Exception:
                continue

            for cc in ccodes:
                cc = cc.upper()
                geometries.setdefault(cc, []).append(geom)

    # Union per-ccode (some countries have multiple un docs in edge cases)
    result = {}
    for cc, geoms in geometries.items():
        if len(geoms) == 1:
            result[cc] = geoms[0]
        else:
            try:
                result[cc] = unary_union(geoms)
            except Exception:
                result[cc] = geoms[0]

    print(
        f"  {docs_seen} un docs scanned; {len(result)} unique ccodes with usable geometry "
        f"(skipped: no_geom={skipped_no_geom}, no_ccodes={skipped_no_ccodes})"
    )
    return result


# ---------------------------------------------------------------------------
# Antarctica from Overpass
# ---------------------------------------------------------------------------

def fetch_antarctica():
    """Fetch Antarctica's polygon from the Overpass API."""
    rel_id = ANTARCTICA['osm_relation']
    query = (
        '[out:json][timeout:120];'
        f'relation({rel_id});'
        'out geom;'
    )

    for endpoint in (OVERPASS_URL, OVERPASS_FALLBACK):
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
    return f"{OUTPUT_NAMESPACE}:m49_{slug}"


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
        'namespace': OUTPUT_NAMESPACE,
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
        h3_geom = select_h3_cover_geometry(geom_entry, raw_geom)
        h3c, h3cover = compute_h3_fields(rp['lon'], rp['lat'], h3_geom)
        if h3c:
            geom_entry['h3_centroid'] = h3c
            geom_entry['h3_cover'] = h3cover

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

    if missing:
        print(f"\n  WARNING: {len(missing)} M49 country codes not in staged un:")
        print(f"    {', '.join(sorted(missing))}")
    else:
        print(f"  ✓ All {len(all_needed)} M49 country codes found")

    if 'AQ' not in found:
        print("  NOTE: Antarctica (AQ) will be fetched from Overpass separately")

    return missing


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def generate_geoscheme(dry_run=False):
    """Generate UN M49 geoscheme docs from staged un, write to staged osm."""
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    print("=" * 70)
    print("UN M49 GEOSCHEME BOUNDARY GENERATION (staged)")
    print("=" * 70)

    country_geoms = fetch_country_geometries()
    if not country_geoms:
        print("\nERROR: No country geometries loaded from staged un.")
        return []

    _verify_country_coverage(country_geoms)

    all_docs = []

    # The M49 polygons need to land in the geom store so tile generation can
    # render them. They're written under a dedicated ``osm_geoscheme`` staging
    # file so they don't race with the per-shard osm boundary writers — keys
    # remain ``osm:m49_*_0`` / ``osm:r{antarctica_id}_0`` and resolve from
    # the consolidated index regardless of which staging file held them.
    writer_ctx = (
        GeomStoreWriter(GEOM_STORE_STAGING_DIR, "osm_geoscheme")
        if not dry_run else None
    )

    def _close_writer():
        configure_module_writer(None)
        if writer_ctx is not None:
            writer_ctx.close()

    if writer_ctx is not None:
        configure_module_writer(writer_ctx)

    try:
        # Subregions (boundary="1")
        print("\nBuilding subregions (boundary='1') ...")
        for name, info in M49_SUBREGIONS.items():
            geoms = [country_geoms[cc] for cc in info['ccodes'] if cc in country_geoms]
            found_codes = [cc for cc in info['ccodes'] if cc in country_geoms]
            if not geoms:
                print(f"  ✗ {name}: no country geometries found")
                continue
            # M49 regions whose member countries include antimeridian-crossers
            # (Kiribati, Russia, USA-Aleutians, Fiji, NZ Chathams, ...) produce
            # unions that wrap from -180 to +180 — and tippecanoe then renders
            # them across the entire globe at low zoom. We must split EACH
            # member at +/-180 *before* unioning; otherwise unary_union folds
            # the wrap into a single CW-wound globe-spanning polygon that the
            # antimeridian library can no longer disentangle. Splitting after
            # union (defensive second pass) catches any residual wrap.
            # make_valid AFTER split — splitting can produce zero-area sliver
            # rings along the dateline that crash unary_union with a
            # TopologyException side-location-conflict. make_valid heals them.
            split_members = [
                make_valid(split_at_antimeridian(g)) for g in geoms
            ]
            union = split_at_antimeridian(make_valid(unary_union(split_members)))
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
            # M49 regions whose member countries include antimeridian-crossers
            # (Kiribati, Russia, USA-Aleutians, Fiji, NZ Chathams, ...) produce
            # unions that wrap from -180 to +180 — and tippecanoe then renders
            # them across the entire globe at low zoom. We must split EACH
            # member at +/-180 *before* unioning; otherwise unary_union folds
            # the wrap into a single CW-wound globe-spanning polygon that the
            # antimeridian library can no longer disentangle. Splitting after
            # union (defensive second pass) catches any residual wrap.
            # make_valid AFTER split — splitting can produce zero-area sliver
            # rings along the dateline that crash unary_union with a
            # TopologyException side-location-conflict. make_valid heals them.
            split_members = [
                make_valid(split_at_antimeridian(g)) for g in geoms
            ]
            union = split_at_antimeridian(make_valid(unary_union(split_members)))
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
            # M49 regions whose member countries include antimeridian-crossers
            # (Kiribati, Russia, USA-Aleutians, Fiji, NZ Chathams, ...) produce
            # unions that wrap from -180 to +180 — and tippecanoe then renders
            # them across the entire globe at low zoom. We must split EACH
            # member at +/-180 *before* unioning; otherwise unary_union folds
            # the wrap into a single CW-wound globe-spanning polygon that the
            # antimeridian library can no longer disentangle. Splitting after
            # union (defensive second pass) catches any residual wrap.
            # make_valid AFTER split — splitting can produce zero-area sliver
            # rings along the dateline that crash unary_union with a
            # TopologyException side-location-conflict. make_valid heals them.
            split_members = [
                make_valid(split_at_antimeridian(g)) for g in geoms
            ]
            union = split_at_antimeridian(make_valid(unary_union(split_members)))
            doc = build_geoscheme_place_doc(
                name, '0', union, info.get('wikidata'), found_codes)
            if doc:
                all_docs.append(doc)
                print(f"  ✓ {name}: {len(found_codes)} countries")

        # Antarctica
        print("\nFetching Antarctica ...")
        antarctica_geom = fetch_antarctica()
        if antarctica_geom:
            antarctica_geom = split_at_antimeridian(antarctica_geom)
            doc = build_geoscheme_place_doc(
                'Antarctica', '0', antarctica_geom,
                ANTARCTICA['wikidata'], ANTARCTICA['ccodes'])
            if doc:
                doc['place_id'] = f"{OUTPUT_NAMESPACE}:r{ANTARCTICA['osm_relation']}"
                all_docs.append(doc)
                print("  ✓ Antarctica added")
        else:
            print("  ✗ Antarctica skipped (Overpass unavailable)")
    finally:
        _close_writer()

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
        print("\nDRY RUN — skipping staged write.")
        for d in all_docs:
            print(f"  {d['place_id']}: {d['title']} (boundary={d['boundary']})")
    else:
        out_path = (
            Path(STAGED_BASE_DIR) / OUTPUT_NAMESPACE / "extract" / "places.jsonl"
        )
        print(f"\nWriting {len(all_docs)} docs (append) → {out_path}")
        for doc in all_docs:
            write_staged_place_doc(namespace=OUTPUT_NAMESPACE, doc=doc)
        print(f"  ✓ Appended {len(all_docs)} M49 docs")

    return all_docs


def main():
    parser = argparse.ArgumentParser(
        description="Generate UN M49 geoscheme boundaries from staged un",
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Print derived docs without staging them')
    args = parser.parse_args()

    generate_geoscheme(dry_run=args.dry_run)


if __name__ == '__main__':
    main()
