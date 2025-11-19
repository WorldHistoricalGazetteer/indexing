# authorities/wikidata-geoshapes.py

"""
Post-processing script to fetch and add geoshape geometries to Wikidata places.

This script:
1. Reads QID and geoshape references from a local JSONL file (created by wikidata-places.py).
2. Fetches the GeoJSON content directly from Wikimedia Commons using the Commons API.
3. Updates the place in Elasticsearch with the full geometry and computed representative point.
4. Implements a resumable mechanism using a log file.
"""

import json
import requests
import time
import os
import sys
from elasticsearch8 import Elasticsearch, helpers

from authorities.settings import ES_HOST, BATCH_SIZE, DATA_DIR
from authorities.helpers import compute_representative_point

# --- Configuration ---
es = Elasticsearch(ES_HOST)
PLACES_INDEX = "places"
REFS_FILE = os.path.join(DATA_DIR, "wikidata", "wikidata_geoshape_refs.jsonl")  # Input file from wikidata-places.py
LOG_FILE = "geoshapes_downloaded.log"  # Log file for resumability


# --- Configuration ---


def fetch_geojson_from_commons(data_page):
    """
    Fetch GeoJSON data from Wikimedia Commons Data page.
    The data_page should be the file name (e.g., 'France.map').
    Returns GeoJSON geometry dict or None.
    """
    if not data_page:
        return None

    try:
        url = 'https://commons.wikimedia.org/w/api.php'
        # The full title in Commons is "Data:{filename}"
        title = f"Data:{data_page}"
        params = {
            'action': 'query',
            'prop': 'revisions',
            'rvslots': '*',
            'rvprop': 'content',
            'format': 'json',
            'titles': title,
            'formatversion': '2'
        }

        # Use exponential backoff for resilience against rate limiting
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = requests.get(url, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()
                break  # Success, break out of retry loop
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"Request failed ({str(e)}). Retrying in {wait_time}s...", file=sys.stderr)
                    time.sleep(wait_time)
                else:
                    raise  # Re-raise error if all retries fail

        pages = data.get('query', {}).get('pages', [])
        if not pages:
            return None

        # Extract the content from the revisions structure
        content = pages[0].get('revisions', [{}])[0].get('slots', {}).get('main', {}).get('content')
        if not content:
            return None

        geojson = json.loads(content)

        # Extract geometry from FeatureCollection or Feature object
        if geojson.get('type') == 'Feature':
            return geojson.get('geometry')
        elif geojson.get('type') == 'FeatureCollection':
            features = geojson.get('features', [])
            if features:
                # Assuming we only need the geometry of the first feature
                return features[0].get('geometry')
        elif geojson.get('type') in ['Point', 'LineString', 'Polygon', 'MultiPoint',
                                     'MultiLineString', 'MultiPolygon', 'GeometryCollection']:
            return geojson

    except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"Final error fetching Data:{data_page}: {str(e)}", file=sys.stderr)
        return None

    return None


def get_downloaded_list():
    """Reads the log file to get a set of already processed place_ids."""
    if not os.path.exists(LOG_FILE):
        return set()
    with open(LOG_FILE, 'r') as f:
        return set(line.strip() for line in f)


def log_downloaded(place_id):
    """Appends a place_id to the log file."""
    with open(LOG_FILE, 'a') as f:
        f.write(f"{place_id}\n")


def process_geoshapes_from_file(places_index, refs_file, batch_size=BATCH_SIZE):
    """
    Reads QIDs and geoshape references from a file, fetches GeoJSON, and updates ES.
    """
    if not os.path.exists(refs_file):
        print(f"Error: References file not found at {refs_file}. Run wikidata-places.py first.", file=sys.stderr)
        sys.exit(1)

    downloaded_ids = get_downloaded_list()

    updates = []
    processed = 0
    updated = 0
    skipped_resumed = 0

    print("Starting geoshape fetching from local reference file.")
    print(f"Loaded {len(downloaded_ids)} IDs from log file for resumption.")

    with open(refs_file, 'r') as f:
        for line in f:
            processed += 1

            # Extract QID and geoshape reference from the JSONL line
            try:
                ref_data = json.loads(line.strip())
                qid = ref_data['qid']
                geoshape_ref = ref_data['geoshape_ref']
                place_id = f"wd:{qid}"
            except json.JSONDecodeError:
                print(f"Skipping malformed line: {line.strip()}", file=sys.stderr)
                continue

            # --- Resumption Check ---
            if place_id in downloaded_ids:
                skipped_resumed += 1
                if processed % 10000 == 0:
                    sys.stdout.write(f"\rProcessed: {processed:,}, Skipped (Resumed): {skipped_resumed:,}")
                    sys.stdout.flush()
                continue
            # ------------------------

            # Fetch actual GeoJSON from Commons
            geometry = fetch_geojson_from_commons(geoshape_ref)

            # NOTE: The fetching function now handles the API rate limit delay.

            if not geometry:
                # If geometry fails to fetch, we skip, and DO NOT log it,
                # allowing a retry on a subsequent run.
                continue

            # Compute geodetically-correct representative point
            rep_point_new = compute_representative_point(geometry)

            # 2. Build the complete new location object
            new_location = {
                'geometry': geometry,
                'rep_point': rep_point_new
            }

            updates.append({
                "_op_type": "update",
                "_index": places_index,
                "_id": place_id,
                "script": {
                    "source": """
                        // Check if locations array exists and has at least one item.
                        // If it does, overwrite the first location object with the complete new data.
                        if (ctx._source.locations != null && ctx._source.locations.size() > 0) {
                            ctx._source.locations[0] = params.new_location;
                        } else {
                            // Safety fall-through: create the array if it was missing (shouldn't happen)
                            ctx._source.locations = [params.new_location];
                        }
                    """,
                    "params": {
                        "new_location": new_location
                    }
                }
            })

            log_downloaded(place_id)  # Log immediately after successful fetch

            if len(updates) >= batch_size:
                try:
                    success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
                    updated += success
                    sys.stdout.write(
                        f"\rProcessed: {processed:,}, Updated: {updated:,}, Skipped (Resumed): {skipped_resumed:,}")
                    sys.stdout.flush()
                    updates = []
                except Exception as e:
                    print(f"\nError updating batch: {str(e)}", file=sys.stderr)
                    updates = []

    # Index remaining batches
    if updates:
        try:
            success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
            updated += success
        except Exception as e:
            print(f"\nError updating final batch: {str(e)}", file=sys.stderr)

    print(f"\n\nGeoshape processing complete!")
    print(f"Total entries processed (from file): {processed:,}")
    print(f"Updated with geoshapes: {updated:,}")
    print(f"Skipped (Resumed from log): {skipped_resumed:,}")


if __name__ == "__main__":
    print("This script requires the output file from wikidata-places.py:")
    print(f"Input file: {REFS_FILE}")

    response = input(f"Proceed with downloading GeoJSON files from Commons and updating {PLACES_INDEX}? (y/n): ")
    if response.lower() != 'y':
        print("Cancelled.")
        sys.exit(0)

    process_geoshapes_from_file(PLACES_INDEX, REFS_FILE)