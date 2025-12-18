# processing/wikidata-places.py

"""
Index Wikidata places data into Elasticsearch.

Updated to use temporal scoping design where temporal data lives with places,
not with toponyms. All Wikidata names are current (2025) data.
"""

import gzip
import json
import sys
import os

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)

GEOSHAPE_REFS_FILE = os.path.join(DATA_DIR, "wikidata", "wikidata_geoshape_refs.jsonl")


def stream_wikidata(file_path):
    """
    Generator yielding Wikidata entities from compressed JSON dump.
    """
    with gzip.open(file_path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line == '[' or line == ']':
                continue
            if line.endswith(','):
                line = line[:-1]
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"JSON decode error: {e}", file=sys.stderr)
                continue


def is_geographic_entity(entity):
    """
    Check if a Wikidata entity represents a geographic place.
    """
    if 'claims' not in entity:
        return False

    # Check P31 (instance of)
    if 'P31' in entity['claims']:
        for claim in entity['claims']['P31']:
            if 'mainsnak' in claim and 'datavalue' in claim['mainsnak']:
                qid = claim['mainsnak']['datavalue'].get('value', {}).get('id', '')
                # Exclude humans, websites, etc.
                if qid in ['Q5', 'Q35127', 'Q4167836', 'Q13442814']:
                    return False
                # Include geographic entities
                if qid in ['Q82794', 'Q515', 'Q486972', 'Q532', 'Q7275', 'Q6256',
                           'Q23442', 'Q8502', 'Q4022', 'Q23397', 'Q34763', 'Q33837']:
                    return True

    # Check if it has coordinates (P625) - strong indicator of geographic entity
    if 'P625' in entity['claims']:
        return True

    # Check if it has geoshape data (P3896) - also indicates geographic entity
    if 'P3896' in entity['claims']:
        return True

    return False


def extract_labels(entity):
    """
    Extract labels from all languages.
    """
    labels = {}
    if 'labels' in entity:
        for lang, label_obj in entity['labels'].items():
            labels[lang] = label_obj['value']
    return labels


def extract_coordinates(entity):
    """
    Extract coordinates from P625 (coordinate location).
    """
    if 'claims' not in entity or 'P625' not in entity['claims']:
        return None

    for claim in entity['claims']['P625']:
        if 'mainsnak' in claim and 'datavalue' in claim['mainsnak']:
            coords = claim['mainsnak']['datavalue'].get('value', {})
            if 'latitude' in coords and 'longitude' in coords:
                return [coords['longitude'], coords['latitude']]

    return None


def extract_geoshape_ref(entity):
    """
    Extract geoshape reference from P3896.
    Returns the file name (e.g., 'France.map') or None.
    """
    if 'claims' not in entity or 'P3896' not in entity['claims']:
        return None

    for claim in entity['claims']['P3896']:
        if 'mainsnak' in claim and 'datavalue' in claim['mainsnak']:
            value = claim['mainsnak']['datavalue'].get('value', {})
            if isinstance(value, str) and value.startswith('Data:'):
                # Return only the filename after "Data:"
                return value[5:]

    return None


def extract_coordinates_and_geoshape(entity):
    """
    Extract point coordinates and geoshape reference.
    Returns: locations array, geoshape_ref string.
    """
    locations = []

    # Get point coordinates from P625
    coords = extract_coordinates(entity)
    if coords:
        location = {
            'geometry': {
                'type': 'Point',
                'coordinates': coords
            },
            'rep_point': {
                'lon': coords[0],
                'lat': coords[1]
            }
        }
        locations.append(location)

    # Get geoshape reference from P3896
    geoshape_ref = extract_geoshape_ref(entity)

    return locations if locations else None, geoshape_ref


def extract_country_codes(entity):
    """
    Extract country codes from P297 (ISO 3166-1 alpha-2 code).
    """
    ccodes = []
    if 'claims' not in entity or 'P297' not in entity['claims']:
        return ccodes

    for claim in entity['claims']['P297']:
        if 'mainsnak' in claim and 'datavalue' in claim['mainsnak']:
            code = claim['mainsnak']['datavalue'].get('value', '')
            if code:
                ccodes.append(code)

    return ccodes


def extract_elevation(entity):
    """
    Extract elevation from P2044 (elevation above sea level).
    """
    if 'claims' not in entity or 'P2044' not in entity['claims']:
        return None

    for claim in entity['claims']['P2044']:
        if 'mainsnak' in claim and 'datavalue' in claim['mainsnak']:
            value = claim['mainsnak']['datavalue'].get('value', {})
            if isinstance(value, dict) and 'amount' in value:
                try:
                    return int(float(value['amount']))
                except (ValueError, TypeError):
                    pass

    return None


def extract_types(entity):
    """
    Extract type information from P31 (instance of).
    """
    types = []
    if 'claims' not in entity or 'P31' not in entity['claims']:
        return types

    for claim in entity['claims']['P31']:
        if 'mainsnak' in claim and 'datavalue' in claim['mainsnak']:
            value = claim['mainsnak']['datavalue'].get('value', {})
            if isinstance(value, dict) and 'id' in value:
                qid = value['id']
                types.append({
                    'identifier': qid,
                    'label': 'wikidata',
                    'sourceLabel': qid
                })

    return types


def extract_geonames_id(entity):
    """
    Extract Geonames ID from P1566.
    """
    if 'claims' not in entity or 'P1566' not in entity['claims']:
        return None

    for claim in entity['claims']['P1566']:
        if 'mainsnak' in claim and 'datavalue' in claim['mainsnak']:
            gn_id = claim['mainsnak']['datavalue'].get('value', '')
            if gn_id:
                return gn_id

    return None


def create_place_doc(entity):
    """
    Create a place document from a Wikidata entity.

    Uses new temporal scoping design:
    - toponyms: nested array with timespan for each toponym

    Returns: doc dict, geoshape_ref string.
    """
    qid = entity.get('id')
    if not qid:
        return None, None

    labels = extract_labels(entity)
    label = labels.get('en', labels.get('mul', qid))

    # Build toponyms array with temporal scoping
    toponyms = []
    seen_lsts = set()  # Deduplicate

    # Add labels (one per language)
    if 'labels' in entity:
        for lang, label_obj in entity['labels'].items():
            name = label_obj['value']
            lst = f"{name}@{lang}"

            if lst not in seen_lsts:
                # Wikidata is current data - use 2025
                toponyms.append({
                    'toponym_id': lst,
                    'timespan': {
                        'start': {'in': 2025},
                        'end': {'in': 2025}
                    }
                })
                seen_lsts.add(lst)

    # Add aliases (multiple per language)
    if 'aliases' in entity:
        for lang, alias_list in entity['aliases'].items():
            for alias_obj in alias_list:
                name = alias_obj['value']
                lst = f"{name}@{lang}"

                if lst not in seen_lsts:
                    # Aliases also get current year scope
                    toponyms.append({
                        'toponym_id': lst,
                        'timespan': {
                            'start': {'in': 2025},
                            'end': {'in': 2025}
                        }
                    })
                    seen_lsts.add(lst)

    doc = {
        'place_id': f"wd:{qid}",
        'label': label,
        'toponyms': toponyms,
        'source': 'wikidata'
    }

    # Extract coordinates and geoshape reference
    locations, geoshape_ref = extract_coordinates_and_geoshape(entity)
    if locations:
        doc['locations'] = locations

    # Add country codes
    ccodes = extract_country_codes(entity)
    if ccodes:
        doc['ccodes'] = ccodes

    # Add elevation
    elevation = extract_elevation(entity)
    if elevation is not None:
        doc['elevation'] = elevation

    # Add types
    types = extract_types(entity)
    if types:
        doc['types'] = types

    # Add relation to Geonames if present
    relations = []
    gn_id = extract_geonames_id(entity)
    if gn_id:
        relations.append({
            'relationType': 'sameAs',
            'relationTo': f"gn:{gn_id}",
            'source': 'wikidata',
            'method': 'curated',
            'certainty': 1.0
        })

    if relations:
        doc['relations'] = relations

    # Only index if it has at least one spatial attribute (coords or geoshape_ref)
    if not locations and not geoshape_ref:
        return None, None

    return doc, geoshape_ref


def index_wikidata(file_path, places_index, geoshape_refs_file):
    """
    Process Wikidata dump, index places, and save geoshape references.

    Uses simplified schema:
    - toponyms: nested array with toponym_id and timespan

    NOTE: This script does NOT index to the toponyms index. The toponyms index
    is populated by a separate deduplication step that runs after ALL authorities
    are ingested.
    """
    place_batch = []

    place_count = 0
    processed = 0
    skipped = 0
    geoshape_count = 0

    print("Starting Wikidata processing...")
    print(f"Saving geoshape references to: {geoshape_refs_file}")
    print("Note: Using simplified schema")
    print("  - toponyms: nested array with toponym_id and timespan")

    # Ensure directory exists
    os.makedirs(os.path.dirname(geoshape_refs_file), exist_ok=True)

    # Open the geoshape file for writing
    with open(geoshape_refs_file, 'w') as refs_f:
        for entity in stream_wikidata(file_path):
            processed += 1

            # Progress update every 100k entities
            if processed % 100000 == 0:
                sys.stdout.write(
                    f"\rProcessed {processed:,} entities... (places: {place_count:,}, geoshapes: {geoshape_count:,}, skipped: {skipped:,})")
                sys.stdout.flush()

            # Check if it's a geographic entity
            if not is_geographic_entity(entity):
                skipped += 1
                continue

            try:
                # Create place document and get geoshape reference
                place_doc, geoshape_ref = create_place_doc(entity)

                if not place_doc:
                    skipped += 1
                    continue

                place_id = place_doc['place_id']
                qid = entity['id']

                # Save Geoshape Reference
                if geoshape_ref:
                    ref_doc = {
                        'qid': qid,
                        'geoshape_ref': geoshape_ref
                    }
                    refs_f.write(json.dumps(ref_doc) + '\n')
                    geoshape_count += 1

                # Add to place batch
                place_batch.append({
                    '_index': places_index,
                    '_id': place_id,
                    '_source': place_doc
                })

                # Bulk index places
                if len(place_batch) >= BATCH_SIZE:
                    success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
                    place_count += success
                    place_batch = []

            except Exception as e:
                print(f"\nError processing entity {entity.get('id', 'unknown')}: {str(e)}", file=sys.stderr)
                continue

        # Index remaining batch
        if place_batch:
            success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
            place_count += success

    print(f"\n\nIndexing complete!")
    print(f"Total entities processed: {processed:,}")
    print(f"Places indexed: {place_count:,}")
    print(f"Geoshape references saved: {geoshape_count:,}")
    print(f"Skipped (non-geographic): {skipped:,}")


if __name__ == "__main__":
    WIKIDATA_FILE = f"{DATA_DIR}/wikidata/latest-all/latest-all.json.gz"
    PLACES_INDEX = "places"

    print(f"Starting to index Wikidata from {WIKIDATA_FILE}")
    print(f"Target index: {PLACES_INDEX}")
    print(f"Saving geoshape references to: {GEOSHAPE_REFS_FILE}\n")

    index_wikidata(WIKIDATA_FILE, PLACES_INDEX, GEOSHAPE_REFS_FILE)
    create_checkpoint_snapshot(es, "wikidata_places")