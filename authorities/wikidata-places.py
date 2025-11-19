# authorities/wikidata-places.py

import gzip
import json
import requests
from elasticsearch import Elasticsearch, helpers

from authorities.settings import ES_HOST, BATCH_SIZE, DATA_DIR

es = Elasticsearch(ES_HOST)


def stream_wikidata(file_path):
    """
    Generator yielding Wikidata entities from compressed JSON dump.
    The file format is one JSON object per line.
    """
    with gzip.open(file_path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and the first/last lines which are array brackets
            if not line or line == '[' or line == ']':
                continue
            # Remove trailing comma if present
            if line.endswith(','):
                line = line[:-1]
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"JSON decode error: {e}")
                continue


def is_geographic_entity(entity):
    """
    Check if a Wikidata entity represents a geographic place.
    Uses instance_of (P31) and subclass_of (P279) claims.

    Common geographic entity types:
    - Q82794: geographic region
    - Q515: city
    - Q486972: human settlement
    - Q532: village
    - Q7275: state
    - Q6256: country
    - Q23442: island
    - Q8502: mountain
    - Q4022: river
    - Q23397: lake
    - Q35127: website (EXCLUDE)
    - Q5: human (EXCLUDE)
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

    return False


def extract_labels(entity):
    """
    Extract labels from all languages.
    Returns dict of {lang: label}
    """
    labels = {}
    if 'labels' in entity:
        for lang, label_obj in entity['labels'].items():
            labels[lang] = label_obj['value']
    return labels


def extract_coordinates(entity):
    """
    Extract coordinates from P625 (coordinate location).
    Returns [longitude, latitude] or None.
    """
    if 'claims' not in entity or 'P625' not in entity['claims']:
        return None

    for claim in entity['claims']['P625']:
        if 'mainsnak' in claim and 'datavalue' in claim['mainsnak']:
            coords = claim['mainsnak']['datavalue'].get('value', {})
            if 'latitude' in coords and 'longitude' in coords:
                return [coords['longitude'], coords['latitude']]

    return None


def compute_centroid(geometry):
    """
    Compute centroid from GeoJSON geometry.
    Returns [longitude, latitude] or None.
    """
    geom_type = geometry.get('type')
    coords = geometry.get('coordinates')

    if not coords:
        return None

    try:
        if geom_type == 'Point':
            return coords

        elif geom_type == 'LineString':
            # Average of all points
            lon = sum(c[0] for c in coords) / len(coords)
            lat = sum(c[1] for c in coords) / len(coords)
            return [lon, lat]

        elif geom_type == 'Polygon':
            # Use exterior ring (first ring)
            ring = coords[0]
            lon = sum(c[0] for c in ring) / len(ring)
            lat = sum(c[1] for c in ring) / len(ring)
            return [lon, lat]

        elif geom_type == 'MultiPoint':
            lon = sum(c[0] for c in coords) / len(coords)
            lat = sum(c[1] for c in coords) / len(coords)
            return [lon, lat]

        elif geom_type == 'MultiLineString':
            # Flatten all lines
            all_points = [pt for line in coords for pt in line]
            lon = sum(c[0] for c in all_points) / len(all_points)
            lat = sum(c[1] for c in all_points) / len(all_points)
            return [lon, lat]

        elif geom_type == 'MultiPolygon':
            # Use first ring of each polygon
            all_points = [pt for poly in coords for pt in poly[0]]
            lon = sum(c[0] for c in all_points) / len(all_points)
            lat = sum(c[1] for c in all_points) / len(all_points)
            return [lon, lat]

        elif geom_type == 'GeometryCollection':
            # Recursively compute centroids and average them
            centroids = [compute_centroid(g) for g in geometry.get('geometries', [])]
            centroids = [c for c in centroids if c]  # Filter None
            if centroids:
                lon = sum(c[0] for c in centroids) / len(centroids)
                lat = sum(c[1] for c in centroids) / len(centroids)
                return [lon, lat]

    except (TypeError, IndexError, ZeroDivisionError):
        return None

    return None


def extract_geoshape(entity):
    """
    Extract geoshape from P3896 (geoshape property).
    This returns a reference to a GeoJSON file on Commons.
    Returns URL string or None.
    """
    if 'claims' not in entity or 'P3896' not in entity['claims']:
        return None

    for claim in entity['claims']['P3896']:
        if 'mainsnak' in claim and 'datavalue' in claim['mainsnak']:
            # The value is like "Data:France.map"
            value = claim['mainsnak']['datavalue'].get('value', '')
            if value:
                return value

    return None


def fetch_geojson_from_commons(data_page):
    """
    Fetch GeoJSON data from Wikimedia Commons Data page.
    Returns GeoJSON geometry dict or None.

    Example data_page: "Data:France.map"
    """
    if not data_page or not data_page.startswith('Data:'):
        return None

    try:
        # Use Commons API to get the GeoJSON content
        url = 'https://commons.wikimedia.org/w/api.php'
        params = {
            'action': 'query',
            'prop': 'revisions',
            'rvslots': '*',
            'rvprop': 'content',
            'format': 'json',
            'titles': data_page,
            'formatversion': '2'
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Navigate to the content
        pages = data.get('query', {}).get('pages', [])
        if not pages:
            return None

        content = pages[0].get('revisions', [{}])[0].get('slots', {}).get('main', {}).get('content')
        if not content:
            return None

        # Parse the GeoJSON
        geojson = json.loads(content)

        # Extract geometry - could be Feature, FeatureCollection, or raw geometry
        if geojson.get('type') == 'Feature':
            return geojson.get('geometry')
        elif geojson.get('type') == 'FeatureCollection':
            # Return first feature's geometry
            features = geojson.get('features', [])
            if features:
                return features[0].get('geometry')
        elif geojson.get('type') in ['Point', 'LineString', 'Polygon', 'MultiPoint',
                                     'MultiLineString', 'MultiPolygon', 'GeometryCollection']:
            # Raw geometry
            return geojson

    except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
        return None

    return None


def extract_coordinates_and_geoshape(entity):
    """
    Extract both point coordinates and geoshape, returning a locations array.
    Returns list of location dicts with geometry and optional centroid.
    """
    locations = []

    # First, try to get geoshape (P3896) - this is the full geometry
    geoshape_ref = extract_geoshape(entity)
    if geoshape_ref:
        # Note: We'll fetch geoshapes in a separate pass to avoid slowing down
        # the main ingestion. For now, just store the reference.
        # You could fetch it here with: geometry = fetch_geojson_from_commons(geoshape_ref)
        # But that would make ingestion MUCH slower (API call per entity)

        # For now, we'll add a placeholder and rely on P625 for the point
        pass

    # Get point coordinates from P625
    coords = extract_coordinates(entity)
    if coords:
        location = {
            'geometry': {
                'type': 'Point',
                'coordinates': coords
            }
        }
        # For simple points, rep_point is the same as geometry
        location['rep_point'] = {
            'lon': coords[0],
            'lat': coords[1]
        }
        locations.append(location)

    return locations if locations else None


def extract_country_codes(entity):
    """
    Extract country codes from P297 (ISO 3166-1 alpha-2 code).
    Returns list of country codes.
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
    Returns integer elevation in meters or None.
    """
    if 'claims' not in entity or 'P2044' not in entity['claims']:
        return None

    for claim in entity['claims']['P2044']:
        if 'mainsnak' in claim and 'datavalue' in claim['mainsnak']:
            value = claim['mainsnak']['datavalue'].get('value', {})
            if isinstance(value, dict) and 'amount' in value:
                try:
                    # Amount is stored as string like "+123"
                    return int(float(value['amount']))
                except (ValueError, TypeError):
                    pass

    return None


def extract_types(entity):
    """
    Extract type information from P31 (instance of).
    Returns list of type objects.
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
    Returns geonames ID or None.
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
    Returns dict suitable for places index or None.
    """
    qid = entity.get('id')
    if not qid:
        return None

    # Get English label as default
    labels = extract_labels(entity)
    label = labels.get('en', labels.get('mul', qid))

    # Build the document
    doc = {
        'place_id': f"wd:{qid}",
        'label': label
    }

    # Add coordinates and geoshape
    locations = extract_coordinates_and_geoshape(entity)
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
    gn_id = extract_geonames_id(entity)
    if gn_id:
        doc['relations'] = [{
            'relationType': 'sameAs',
            'relationTo': f"gn:{gn_id}"
        }]

    return doc


def create_toponym_docs(entity, place_id):
    """
    Create toponym documents from a Wikidata entity's labels and aliases.
    Returns list of toponym documents.
    """
    toponyms = []

    # Extract labels (one per language)
    if 'labels' in entity:
        for lang, label_obj in entity['labels'].items():
            name = label_obj['value']
            doc = {
                'place_id': place_id,
                'name': name,
                'name_lower': name.lower(),
                'lang': lang,
                'is_preferred': True,  # Labels are preferred names
                'suggest': {
                    'input': [name],
                    'contexts': {
                        'lang': [lang]
                    }
                }
            }
            toponyms.append(doc)

    # Extract aliases (multiple per language)
    if 'aliases' in entity:
        for lang, alias_list in entity['aliases'].items():
            for alias_obj in alias_list:
                name = alias_obj['value']
                doc = {
                    'place_id': place_id,
                    'name': name,
                    'name_lower': name.lower(),
                    'lang': lang,
                    'suggest': {
                        'input': [name],
                        'contexts': {
                            'lang': [lang]
                        }
                    }
                }
                toponyms.append(doc)

    return toponyms


def index_wikidata(file_path, places_index, toponyms_index):
    """
    Process Wikidata dump and index places and toponyms.
    """
    place_batch = []
    toponym_batch = []

    place_count = 0
    toponym_count = 0
    processed = 0
    skipped = 0

    print("Starting Wikidata processing...")
    print("This will take several hours for the full dump.")

    for entity in stream_wikidata(file_path):
        processed += 1

        # Progress update every 100k entities
        if processed % 100000 == 0:
            print(f"Processed {processed:,} entities... "
                  f"(places: {place_count:,}, toponyms: {toponym_count:,}, skipped: {skipped:,})")

        # Check if it's a geographic entity
        if not is_geographic_entity(entity):
            skipped += 1
            continue

        try:
            # Create place document
            place_doc = create_place_doc(entity)
            if not place_doc:
                skipped += 1
                continue

            place_id = place_doc['place_id']
            qid = entity['id']

            # Add to place batch
            place_batch.append({
                '_index': places_index,
                '_id': place_id,
                '_source': place_doc
            })

            # Create toponym documents
            toponym_docs = create_toponym_docs(entity, place_id)
            for i, toponym_doc in enumerate(toponym_docs):
                toponym_batch.append({
                    '_index': toponyms_index,
                    '_id': f"wd:{qid}:{i}",
                    '_source': toponym_doc
                })

            # Bulk index places
            if len(place_batch) >= BATCH_SIZE:
                success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
                place_count += success
                place_batch = []

            # Bulk index toponyms
            if len(toponym_batch) >= BATCH_SIZE:
                success, failed = helpers.bulk(es, toponym_batch, raise_on_error=False, stats_only=True)
                toponym_count += success
                toponym_batch = []

        except Exception as e:
            print(f"Error processing entity {entity.get('id', 'unknown')}: {str(e)}")
            continue

    # Index remaining batches
    if place_batch:
        success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
        place_count += success

    if toponym_batch:
        success, failed = helpers.bulk(es, toponym_batch, raise_on_error=False, stats_only=True)
        toponym_count += success

    print(f"\nIndexing complete!")
    print(f"Total entities processed: {processed:,}")
    print(f"Places indexed: {place_count:,}")
    print(f"Toponyms indexed: {toponym_count:,}")
    print(f"Skipped (non-geographic): {skipped:,}")


if __name__ == "__main__":
    WIKIDATA_FILE = f"{DATA_DIR}wikidata/latest-all/latest-all.json.gz"
    PLACES_INDEX = "places"
    TOPONYMS_INDEX = "toponyms"

    print(f"Starting to index Wikidata from {WIKIDATA_FILE}")
    print(f"Target indices: {PLACES_INDEX}, {TOPONYMS_INDEX}")
    print("\nNote: This will take several hours for the full Wikidata dump (~110M entities)")
    print("Only geographic entities will be indexed.\n")

    index_wikidata(WIKIDATA_FILE, PLACES_INDEX, TOPONYMS_INDEX)