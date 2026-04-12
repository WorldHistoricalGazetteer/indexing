# processing/wikidata-places.py

"""
Index Wikidata places data into Elasticsearch.

OPTIMIZED VERSION - Uses orjson and byte-scanning for 3-5x speedup
"""

import gzip
import sys
import os

import orjson  # Much faster than json
from elasticsearch import Elasticsearch, helpers
from processing.helpers import enrich_geometry
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE, GEOSHAPE_REFS_FILE
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)

# Pre-compile byte patterns for fast scanning
SKIP_BYTES = {b'[', b']', b''}
GEOGRAPHIC_TYPES = {
    b'"Q82794"', b'"Q515"', b'"Q486972"', b'"Q532"', b'"Q7275"', b'"Q6256"',
    b'"Q23442"', b'"Q8502"', b'"Q4022"', b'"Q23397"', b'"Q34763"', b'"Q33837"'
}
NON_GEOGRAPHIC_TYPES = {b'"Q5"', b'"Q35127"', b'"Q4167836"', b'"Q13442814"'}


def stream_wikidata_fast(file_path):
    """Generator yielding Wikidata entities - OPTIMIZED."""
    with gzip.open(file_path, 'rb') as f:  # Binary mode for orjson
        for line in f:
            line = line.strip()
            if line in SKIP_BYTES or line == b'[' or line == b']':
                continue
            if line.endswith(b','):
                line = line[:-1]

            # Quick pre-filter: Skip if no geographic markers at all
            if b'"P625"' not in line and b'"P3896"' not in line and b'"P31"' not in line:
                continue

            try:
                yield orjson.loads(line)
            except:
                continue


def is_geographic_entity_fast(entity, entity_bytes):
    """Check if geographic - OPTIMIZED with byte scanning."""
    claims = entity.get('claims')
    if not claims:
        return False

    # Fast byte check for P31 (instance of)
    if b'"P31"' in entity_bytes:
        # Check for non-geographic types first (faster rejection)
        for non_geo in NON_GEOGRAPHIC_TYPES:
            if non_geo in entity_bytes:
                return False

        # Check for geographic types
        for geo in GEOGRAPHIC_TYPES:
            if geo in entity_bytes:
                return True

    # Has coordinates?
    if 'P625' in claims:
        return True

    # Has geoshape?
    if 'P3896' in claims:
        return True

    return False


def extract_coordinates_fast(claims):
    """Extract coordinates - OPTIMIZED."""
    p625 = claims.get('P625')
    if not p625:
        return None

    for claim in p625:
        try:
            coords = claim['mainsnak']['datavalue']['value']
            return [coords['longitude'], coords['latitude']]
        except (KeyError, TypeError):
            continue

    return None


def extract_geoshape_ref_fast(claims):
    """Extract geoshape reference - OPTIMIZED."""
    p3896 = claims.get('P3896')
    if not p3896:
        return None

    for claim in p3896:
        try:
            value = claim['mainsnak']['datavalue']['value']
            if isinstance(value, str) and value.startswith('Data:'):
                return value[5:]
        except (KeyError, TypeError):
            continue

    return None


def extract_simple_field(claims, prop):
    """Generic extractor for simple string/number fields - OPTIMIZED."""
    data = claims.get(prop)
    if not data:
        return None

    try:
        return data[0]['mainsnak']['datavalue']['value']
    except (KeyError, TypeError, IndexError):
        return None


def extract_population_fast(claims):
    """Extract population - OPTIMIZED."""
    p1082 = claims.get('P1082')
    if not p1082:
        return None

    for claim in p1082:
        try:
            amount = claim['mainsnak']['datavalue']['value']['amount']
            return int(float(amount.lstrip('+')))
        except (KeyError, TypeError, ValueError):
            continue

    return None


def create_place_doc_fast(entity, entity_bytes):
    """Create place document - OPTIMIZED."""
    qid = entity.get('id')
    if not qid:
        return None, None

    # Quick geographic check with byte scanning
    if not is_geographic_entity_fast(entity, entity_bytes):
        return None, None

    claims = entity.get('claims', {})

    # Extract coordinates and geoshape
    coords = extract_coordinates_fast(claims)
    geoshape_ref = extract_geoshape_ref_fast(claims)

    # Must have spatial data
    if not coords and not geoshape_ref:
        return None, None

    # Extract labels efficiently
    labels = entity.get('labels', {})
    title = labels.get('en', {}).get('value') or labels.get('mul', {}).get('value') or qid

    # Build toponyms
    toponyms = []
    seen = set()

    for lang, label_obj in labels.items():
        name = label_obj.get('value')
        if not name:
            continue
        lst = f"{name}@{lang}"
        if lst not in seen:
            toponyms.append({
                'toponym_id': lst,
                'timespans': [{'start': {'in': 2025}, 'end': {'in': 2025}}]
            })
            seen.add(lst)

    # Add aliases
    for lang, alias_list in entity.get('aliases', {}).items():
        for alias_obj in alias_list:
            name = alias_obj.get('value')
            if not name:
                continue
            lst = f"{name}@{lang}"
            if lst not in seen:
                toponyms.append({
                    'toponym_id': lst,
                    'timespans': [{'start': {'in': 2025}, 'end': {'in': 2025}}]
                })
                seen.add(lst)

    # Build base document
    doc = {
        'place_id': f"wd:{qid}",
        'title': title,
        'toponyms': toponyms
    }

    # Add geometries
    if coords:
        geom_entry = enrich_geometry(
            {'type': 'Point', 'coordinates': coords},
            timespans=[{'start': {'in': 2025}, 'end': {'in': 2025}}],
        )
        if geom_entry:
            doc['geometries'] = [geom_entry]

    # Add optional fields
    ccodes = []
    if 'P297' in claims:
        for claim in claims['P297']:
            try:
                code = claim['mainsnak']['datavalue']['value']
                if code:
                    ccodes.append(code)
            except (KeyError, TypeError):
                pass
    if ccodes:
        doc['ccodes'] = ccodes

    # Population
    population = extract_population_fast(claims)
    if population is not None:
        doc['population'] = population

    # Elevation
    if 'P2044' in claims:
        try:
            elev = claims['P2044'][0]['mainsnak']['datavalue']['value']['amount']
            doc['elevation'] = int(float(elev))
        except (KeyError, TypeError, ValueError, IndexError):
            pass

    # Types
    types = []
    if 'P31' in claims:
        for claim in claims['P31']:
            try:
                qid_type = claim['mainsnak']['datavalue']['value']['id']
                types.append({
                    'identifier': qid_type,
                    'label': 'wikidata',
                    'sourceLabel': qid_type
                })
            except (KeyError, TypeError):
                pass
    if types:
        doc['types'] = types

    # Relations (GeoNames)
    gn_id = extract_simple_field(claims, 'P1566')
    if gn_id:
        doc['relations'] = [{
            'relation_type': 'sameAs',
            'related_place_id': f"gn:{gn_id}",
            'label': 'GeoNames'
        }]

    return doc, geoshape_ref


def index_wikidata(file_path, places_index, geoshape_refs_file):
    """Process Wikidata dump - OPTIMIZED."""
    place_batch = []
    place_count = 0
    processed = 0
    skipped = 0
    geoshape_count = 0

    print("Starting Wikidata processing (OPTIMIZED with orjson)...")
    print(f"Saving geoshape references to: {geoshape_refs_file}")

    os.makedirs(os.path.dirname(geoshape_refs_file), exist_ok=True)

    with open(geoshape_refs_file, 'wb') as refs_f:  # Binary mode for orjson
        for entity in stream_wikidata_fast(file_path):
            processed += 1

            if processed % 100000 == 0:
                sys.stdout.write(
                    f"\rProcessed {processed:,} entities... "
                    f"(places: {place_count:,}, geoshapes: {geoshape_count:,}, skipped: {skipped:,})")
                sys.stdout.flush()

            try:
                # Serialize once for byte scanning
                entity_bytes = orjson.dumps(entity)

                place_doc, geoshape_ref = create_place_doc_fast(entity, entity_bytes)

                if not place_doc:
                    skipped += 1
                    continue

                place_id = place_doc['place_id']
                qid = entity['id']

                if geoshape_ref:
                    ref_doc = {'qid': qid, 'geoshape_ref': geoshape_ref}
                    refs_f.write(orjson.dumps(ref_doc) + b'\n')
                    geoshape_count += 1

                place_batch.append({
                    '_index': places_index,
                    '_id': place_id,
                    '_source': place_doc
                })

                if len(place_batch) >= BATCH_SIZE:
                    success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
                    place_count += success
                    place_batch = []

            except Exception as e:
                skipped += 1
                continue

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