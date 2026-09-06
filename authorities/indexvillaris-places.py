# processing/indexvillaris-places.py
"""
Index Index Villaris (1680) historical place data.
"""
import json, os, sys
from pathlib import Path
from datetime import datetime
from processing.helpers import (
    enrich_geometry,
    compute_h3_fields,
    select_h3_cover_geometry,
    write_staged_place_doc,
)
from processing.settings import DATA_DIR, AUTHORITIES
from processing.temporal import attested_at, normalise_timespans, source_release_year

#: Publication year of Index Villaris — the year its records attest.
IV_YEAR = 1680


def _digitisation_year():
    """Year the digital edition on disk attests for its modern cross-references.

    The modern-name toponym used to carry a literal ``2000``–``2025`` lifespan,
    which both invented a start the source never states and went stale on the
    next fetch. What the edition actually attests is "this is the place's name
    as of the edition" — one derived year, not a fabricated range.
    """
    return source_release_year(Path(DATA_DIR) / 'authorities' / 'iv', label='iv')


#: Attestation for the 1680 gazetteer's own names, and for its modern
#: cross-references. Both say "alive at Y", not "existed only in Y".
IV_TIMESPANS = attested_at(IV_YEAR)
MODERN_TIMESPANS = attested_at(_digitisation_year())

NAMESPACE = "iv"
IV_CONFIG = next((auth for auth in AUTHORITIES if auth['namespace'] == 'iv'), None)


def process_iv_entry(entry, namespace='iv'):
    """Process Index Villaris entry."""
    if 'properties' in entry:
        props = entry.get('properties', {})
        geometry = entry.get('geometry')
        if geometry and geometry.get('type') == 'GeometryCollection':
            geometries = geometry.get('geometries', [])
            point_geom = None
            for geom in geometries:
                if geom.get('type') == 'Point':
                    if geom.get('certainty') == 'certain':
                        point_geom = geom
                        break
                    elif point_geom is None:
                        point_geom = geom
            geometry = point_geom
    else:
        props = entry
        geometry = None
        if 'coordinates' in props:
            coords = props['coordinates']
            if isinstance(coords, list) and len(coords) == 2:
                geometry = {'type': 'Point', 'coordinates': coords}
        elif 'longitude' in props and 'latitude' in props:
            try:
                geometry = {'type': 'Point', 'coordinates': [float(props['longitude']), float(props['latitude'])]}
            except:
                pass

    iv_id = props.get('id', props.get('iv_id', ''))
    historical_name = props.get('name', props.get('historical_name', props.get('title', '')))

    if not historical_name and 'names' in entry:
        names = entry['names']
        if names and len(names) > 0:
            historical_name = names[0].get('toponym', '')

    if not iv_id or not historical_name:
        return None

    place_id = f"{namespace}:{iv_id.replace('IV_', '')}"

    toponyms = []
    seen_lsts = set()

    if 'names' in entry and entry['names']:
        for name_obj in entry['names']:
            toponym = name_obj.get('toponym', '')
            if not toponym: continue
            lst = f"{toponym}@en"
            if lst in seen_lsts: continue

            when = name_obj.get('when', {})
            # The source's own LPF timespans already carry earliest/latest.
            # This used to read those bounds out and re-flatten them into
            # `in`/`in` — discarding the source's uncertainty and asserting an
            # exact one-off existence in its place (place#164). Pass them
            # through; `normalise_timespans` only coerces string years to int.
            source_timespans = normalise_timespans(when.get('timespans', []))

            toponyms.append({
                'toponym_id': lst,
                'timespans': source_timespans or IV_TIMESPANS,
            })
            seen_lsts.add(lst)

    if not toponyms:
        lst = f"{historical_name}@en"
        toponyms.append({'toponym_id': lst, 'timespans': IV_TIMESPANS})
        seen_lsts.add(lst)

    modern_name = props.get('modern_name', props.get('modern', ''))
    if modern_name and modern_name != historical_name:
        lst = f"{modern_name}@en"
        if lst not in seen_lsts:
            toponyms.append({'toponym_id': lst, 'timespans': MODERN_TIMESPANS})
            seen_lsts.add(lst)

    if 'alternative_names' in props:
        alt_names = props['alternative_names']
        if isinstance(alt_names, str): alt_names = [alt_names]
        for alt_name in alt_names:
            if alt_name and alt_name not in [historical_name, modern_name]:
                lst = f"{alt_name}@en"
                if lst not in seen_lsts:
                    toponyms.append({'toponym_id': lst, 'timespans': IV_TIMESPANS})
                    seen_lsts.add(lst)

    # Index Villaris is a 1680 gazetteer: it attests these places were
    # alive in 1680, not that they existed ONLY in 1680 (place#164).
    geom_entry = enrich_geometry(geometry, timespans=IV_TIMESPANS) if geometry else None

    place_doc = {
        'place_id': place_id,
        'title': historical_name,
        'toponyms': toponyms,
        'geometries': [geom_entry] if geom_entry else [],
        'ccodes': ['GB']
    }
    if geom_entry and geom_entry.get('repr_point'):
        rp = geom_entry['repr_point']
        h3_geom = select_h3_cover_geometry(geom_entry, geometry)
        h3c, h3cover = compute_h3_fields(rp['lon'], rp['lat'], h3_geom)
        if h3c:
            geom_entry['h3_centroid'] = h3c
            geom_entry['h3_cover'] = h3cover

    types = []
    if 'types' in entry and entry['types']:
        for type_obj in entry['types']:
            identifier = type_obj.get('identifier', '')
            label = type_obj.get('label', '')
            type_map = {
                'wd:Q18511725': 'market-town',
                'wd:Q486972': 'settlement',
                'wd:Q515': 'city',
                'wd:Q532': 'village'
            }
            type_id = type_map.get(identifier, 'settlement')
            types.append({'identifier': type_id, 'label': 'indexvillaris', 'sourceLabel': label})

    if not types:
        if 'market_day' in props and props['market_day']:
            types.append({'identifier': 'market-town', 'label': 'indexvillaris', 'sourceLabel': 'market town (1680)'})
        else:
            types.append({'identifier': 'settlement', 'label': 'indexvillaris', 'sourceLabel': 'village/town (1680)'})

    place_doc['types'] = types

    relations = []
    if 'links' in entry and entry['links']:
        for link in entry['links']:
            link_type = link.get('type', 'closeMatch')
            identifier = link.get('identifier', '')
            if not identifier: continue
            if ':' in identifier:
                parts = identifier.split(':', 1)
                namespace_map = {'GB1900': 'gb', 'osm': 'osm', 'wd': 'wd'}
                source_ns = parts[0]
                target_ns = namespace_map.get(source_ns, source_ns.lower())
                relation_to = f"{target_ns}:{parts[1]}"
                rel_type = 'exactMatch' if link_type == 'exactMatch' else 'closeMatch'
                certainty = 1.0 if link_type == 'exactMatch' else 0.9
                relations.append({
                    'relation_type': rel_type,
                    'related_place_id': relation_to,
                    'label': f"{target_ns.upper()} Match"
                })

    if 'gb1900_id' in props and props['gb1900_id']:
        relations.append({
            'relation_type': 'sameAs',
            'related_place_id': f"gb:{props['gb1900_id']}",
            'label': 'GB1900'
        })

    if 'osm_id' in props and props['osm_id']:
        relations.append({
            'relation_type': 'sameAs',
            'related_place_id': f"osm:{props['osm_id']}",
            'label': 'OpenStreetMap'
        })

    if 'wikidata_id' in props and props['wikidata_id']:
        wd_id = props['wikidata_id']
        if not wd_id.startswith('Q'): wd_id = f"Q{wd_id}"
        relations.append({
            'relation_type': 'sameAs',
            'related_place_id': f"wd:{wd_id}",
            'label': 'Wikidata'
        })

    if relations: place_doc['relations'] = relations
    if 'county' in props and props['county']: place_doc['historical_county'] = props['county']
    if 'parish' in props and props['parish']: place_doc['parish'] = props['parish']
    if 'market_day' in props and props['market_day']: place_doc['market_day'] = props['market_day']
    if 'confidence' in props:
        try:
            place_doc['location_confidence'] = float(props['confidence'])
        except:
            pass

    return place_doc


def stage_iv_file(json_file):
    """Stage Index Villaris JSON file to {STAGED_BASE_DIR}/iv/extract/places.jsonl."""
    print(f"Processing: {json_file}")
    if not os.path.exists(json_file):
        standard_path = Path(DATA_DIR) / 'authorities' / 'iv' / Path(json_file).name
        if standard_path.exists():
            json_file = standard_path
        else:
            print(f"ERROR: Not found: {json_file}")
            return

    places_count = 0
    skipped = 0
    no_coords = 0

    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"ERROR: {e}")
        return

    entries = []
    if isinstance(data, dict):
        if data.get('type') == 'FeatureCollection':
            entries = data.get('features', [])
        elif 'entries' in data:
            entries = data['entries']
        elif 'places' in data:
            entries = data['places']
        else:
            for key in ['data', 'items', 'records']:
                if key in data:
                    entries = data[key]
                    break
    elif isinstance(data, list):
        entries = data

    if not entries:
        print("ERROR: No entries found")
        return

    print(f"Found {len(entries)} entries")
    start_time = datetime.now()

    for i, entry in enumerate(entries):
        if (i + 1) % 500 == 0:
            elapsed = (datetime.now() - start_time).seconds
            rate = i / elapsed if elapsed > 0 else 0
            print(f"\r  {i + 1}/{len(entries)} ({rate:.1f}/s) - staged: {places_count}", end='', flush=True)

        try:
            place_doc = process_iv_entry(entry)
            if not place_doc:
                if 'name' in entry or (
                        isinstance(entry, dict) and 'properties' in entry and 'name' in entry['properties']):
                    no_coords += 1
                else:
                    skipped += 1
                continue

            write_staged_place_doc(namespace=NAMESPACE, doc=place_doc)
            places_count += 1
        except Exception as e:
            print(f"  ERROR {i}: {e}")
            skipped += 1
            continue

    elapsed = (datetime.now() - start_time).seconds
    print(f"\n{'=' * 80}")
    print(f"INDEX VILLARIS STAGING COMPLETE")
    print(f"{'=' * 80}")
    print(f"Time: {elapsed}s")
    print(f"Staged: {places_count:,}")
    print(f"No coords: {no_coords:,}")
    print(f"Skipped: {skipped:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Stage Index Villaris')
    parser.add_argument('--file', help='Path to JSON file')
    args = parser.parse_args()

    if args.file:
        json_file = args.file
    else:
        iv_files = IV_CONFIG.get('files', [])
        if not iv_files:
            print("ERROR: No files configured")
            sys.exit(1)
        file_url = iv_files[0]['url']
        filename = Path(file_url).name or 'IV-GB1900-OSM-WD.lp.json'
        json_file = Path(DATA_DIR) / 'authorities' / 'iv' / filename

    print(f"Index Villaris (STAGING)")
    print(f"File: {json_file}\n")
    stage_iv_file(str(json_file))