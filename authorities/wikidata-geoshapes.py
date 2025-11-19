# authorities/wikidata-geoshapes.py

"""
Post-processing script to fetch and add geoshape geometries to Wikidata places.

This script:
1. Queries Elasticsearch for Wikidata places (wd:* prefix)
2. For each place, checks Wikidata for P3896 (geoshape) property
3. Fetches the GeoJSON from Wikimedia Commons
4. Updates the place with the full geometry and computed centroid

This is separated from main ingestion because fetching geoshapes requires
API calls to Commons and would make the main ingestion extremely slow.
"""

import json
import requests
import time
from elasticsearch import Elasticsearch, helpers

from authorities.settings import ES_HOST, BATCH_SIZE
from authorities.helpers import compute_representative_point

es = Elasticsearch(ES_HOST)


def fetch_geojson_from_commons(data_page):
    """
    Fetch GeoJSON data from Wikimedia Commons Data page.
    Returns GeoJSON geometry dict or None.
    """
    if not data_page or not data_page.startswith('Data:'):
        return None

    try:
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

        pages = data.get('query', {}).get('pages', [])
        if not pages:
            return None

        content = pages[0].get('revisions', [{}])[0].get('slots', {}).get('main', {}).get('content')
        if not content:
            return None

        geojson = json.loads(content)

        # Extract geometry
        if geojson.get('type') == 'Feature':
            return geojson.get('geometry')
        elif geojson.get('type') == 'FeatureCollection':
            features = geojson.get('features', [])
            if features:
                return features[0].get('geometry')
        elif geojson.get('type') in ['Point', 'LineString', 'Polygon', 'MultiPoint',
                                     'MultiLineString', 'MultiPolygon', 'GeometryCollection']:
            return geojson

    except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"Error fetching {data_page}: {str(e)}")
        return None

    return None


def fetch_geoshape_from_wikidata(qid):
    """
    Query Wikidata SPARQL endpoint for geoshape property (P3896).
    Returns Data page reference or None.
    """
    try:
        sparql_endpoint = "https://query.wikidata.org/sparql"
        query = f"""
        SELECT ?geoshape WHERE {{
            wd:{qid} wdt:P3896 ?geoshape .
        }}
        LIMIT 1
        """

        response = requests.get(
            sparql_endpoint,
            params={'query': query, 'format': 'json'},
            headers={'User-Agent': 'WHG-Gazetteer/1.0'},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()

        bindings = data.get('results', {}).get('bindings', [])
        if bindings:
            geoshape_url = bindings[0].get('geoshape', {}).get('value', '')
            # URL like: http://commons.wikimedia.org/data/main/Data:France.map
            if 'Data:' in geoshape_url:
                return geoshape_url.split('Data:')[1]

    except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"Error querying Wikidata for {qid}: {str(e)}")

    return None


def process_places_with_geoshapes(places_index, batch_size=100):
    """
    Query places, fetch their geoshapes, and update with full geometry + centroid.
    """
    query = {
        "query": {
            "prefix": {
                "place_id": "wd:"
            }
        },
        "_source": ["place_id", "locations"],
        "size": batch_size
    }

    # Scroll through all Wikidata places
    resp = es.search(index=places_index, body=query, scroll='5m')
    scroll_id = resp['_scroll_id']
    hits = resp['hits']['hits']

    updates = []
    processed = 0
    updated = 0
    skipped = 0

    print("Starting geoshape fetching and updating...")
    print("This will take several hours due to API rate limiting.\n")

    while hits:
        for hit in hits:
            place_id = hit['_source']['place_id']
            qid = place_id.split(':')[1]  # Extract Q12345 from wd:Q12345

            processed += 1

            # Check if place already has complex geometry
            existing_locations = hit['_source'].get('locations', [])
            has_complex_geom = any(
                loc.get('geometry', {}).get('type') not in ['Point', None]
                for loc in existing_locations
            )

            if has_complex_geom:
                skipped += 1
                continue

            # Fetch geoshape reference from Wikidata
            geoshape_ref = fetch_geoshape_from_wikidata(qid)
            if not geoshape_ref:
                skipped += 1
                continue

            # Fetch actual GeoJSON from Commons
            geometry = fetch_geojson_from_commons(f"Data:{geoshape_ref}")
            if not geometry:
                skipped += 1
                continue

            # Compute geodetically-correct centroid
            rep_point = compute_representative_point(geometry)

            # Build updated location
            new_location = {
                'geometry': geometry
            }
            if rep_point:
                new_location['rep_point'] = rep_point

            # Update place
            updates.append({
                "_op_type": "update",
                "_index": places_index,
                "_id": place_id,
                "doc": {
                    "locations": [new_location]
                }
            })

            if len(updates) >= BATCH_SIZE:
                try:
                    success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
                    updated += success
                    print(f"Processed: {processed}, Updated: {updated}, Skipped: {skipped}")
                    updates = []
                except Exception as e:
                    print(f"Error updating batch: {str(e)}")
                    updates = []

            # Rate limiting - be nice to Wikimedia APIs
            time.sleep(0.1)

        # Get next batch
        resp = es.scroll(scroll_id=scroll_id, scroll='5m')
        scroll_id = resp['_scroll_id']
        hits = resp['hits']['hits']

    # Update remaining
    if updates:
        try:
            success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
            updated += success
        except Exception as e:
            print(f"Error updating final batch: {str(e)}")

    # Clear scroll
    es.clear_scroll(scroll_id=scroll_id)

    print(f"\nGeoshape processing complete!")
    print(f"Total processed: {processed}")
    print(f"Updated with geoshapes: {updated}")
    print(f"Skipped: {skipped}")


if __name__ == "__main__":
    PLACES_INDEX = "places"

    print("This script fetches complex geometries (polygons/lines) for Wikidata places.")
    print("It makes API calls to Wikidata SPARQL and Wikimedia Commons.")
    print("Expected runtime: several hours with rate limiting.\n")

    response = input("Continue? (y/n): ")
    if response.lower() != 'y':
        print("Cancelled.")
        exit(0)

    process_places_with_geoshapes(PLACES_INDEX)
