# processing/wikidata-geoshapes.py

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
from datetime import datetime

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

from processing.helpers import compute_representative_point

# --- Configuration ---
es = Elasticsearch(ES_HOST)
PLACES_INDEX = "places"
REFS_FILE = os.path.join(DATA_DIR, "wikidata", "wikidata_geoshape_refs.jsonl")
LOG_FILE = os.path.join(DATA_DIR, "wikidata", "geoshapes_downloaded.log")
ERROR_LOG = os.path.join(DATA_DIR, "wikidata", "geoshapes_errors.log")
HEADERS = {
    'User-Agent': 'PittCRC-GeoFetcher/1.0 (stg135@pitt.edu; https://whgazetteer.org/) python-requests',
}


def log_error(place_id, error_msg):
    """Log errors to a separate error file for debugging."""
    with open(ERROR_LOG, 'a') as f:
        f.write(f"{datetime.now().isoformat()} | {place_id} | {error_msg}\n")


def fetch_geojson_from_commons(data_page, place_id):
    """
    Fetch GeoJSON data from Wikimedia Commons Data page.
    The data_page should be the file name (e.g., 'France.map').
    Returns GeoJSON geometry dict or None.
    """
    if not data_page:
        print(f"  No data_page for {place_id}")
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

        print(f"  Fetching {title} for {place_id}...")

        # Use exponential backoff for resilience against rate limiting
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = requests.get(url, params=params, headers=HEADERS, timeout=30)

                if response.status_code == 429:  # Rate limited
                    wait_time = 60 * (2 ** attempt)  # Start with 60 seconds
                    print(f"    Rate limited. Waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue

                response.raise_for_status()
                data = response.json()
                break  # Success, break out of retry loop
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"    Request failed ({str(e)}). Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    error_msg = f"Failed after {max_retries} attempts: {str(e)}"
                    print(f"    {error_msg}")
                    log_error(place_id, error_msg)
                    return None

        pages = data.get('query', {}).get('pages', [])
        if not pages:
            error_msg = f"No pages returned for {title}"
            print(f"    {error_msg}")
            log_error(place_id, error_msg)
            return None

        # Check if page exists (missing = -1)
        if pages[0].get('missing', False):
            error_msg = f"Page not found: {title}"
            print(f"    {error_msg}")
            log_error(place_id, error_msg)
            return None

        # Extract the content from the revisions structure
        revisions = pages[0].get('revisions', [])
        if not revisions:
            error_msg = f"No revisions for {title}"
            print(f"    {error_msg}")
            log_error(place_id, error_msg)
            return None

        content = revisions[0].get('slots', {}).get('main', {}).get('content')
        if not content:
            error_msg = f"No content in {title}"
            print(f"    {error_msg}")
            log_error(place_id, error_msg)
            return None

        # Parse JSON content
        try:
            geojson = json.loads(content)
        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON in {title}: {str(e)}"
            print(f"    {error_msg}")
            log_error(place_id, error_msg)
            return None

        # Extract geometry from FeatureCollection or Feature object
        if geojson.get('type') == 'Feature':
            geometry = geojson.get('geometry')
            if geometry:
                print(f"    ✓ Got {geometry.get('type')} geometry")
                return geometry
        elif geojson.get('type') == 'FeatureCollection':
            features = geojson.get('features', [])
            if features and features[0].get('geometry'):
                geometry = features[0].get('geometry')
                print(f"    ✓ Got {geometry.get('type')} from FeatureCollection")
                return geometry
        elif geojson.get('type') in ['Point', 'LineString', 'Polygon', 'MultiPoint',
                                     'MultiLineString', 'MultiPolygon', 'GeometryCollection']:
            print(f"    ✓ Got direct {geojson.get('type')} geometry")
            return geojson

        error_msg = f"Unexpected GeoJSON structure in {title}: type={geojson.get('type')}"
        print(f"    {error_msg}")
        log_error(place_id, error_msg)

    except Exception as e:
        error_msg = f"Unexpected error fetching {data_page}: {str(e)}"
        print(f"    {error_msg}")
        log_error(place_id, error_msg)

    return None


def get_downloaded_list():
    """Reads the log file to get a set of already processed place_ids."""
    if not os.path.exists(LOG_FILE):
        return set()
    with open(LOG_FILE, 'r') as f:
        return set(line.strip() for line in f if line.strip())


def log_downloaded(place_id):
    """Appends a place_id to the log file."""
    with open(LOG_FILE, 'a') as f:
        f.write(f"{place_id}\n")
        f.flush()  # Ensure it's written immediately


def check_elasticsearch_connection():
    """Verify Elasticsearch is accessible."""
    try:
        info = es.info()
        print(f"Connected to Elasticsearch {info['version']['number']}")

        # Check if places index exists
        if not es.indices.exists(index=PLACES_INDEX):
            print(f"ERROR: Index '{PLACES_INDEX}' does not exist!")
            return False

        # Get count of Wikidata places
        count_response = es.count(
            index=PLACES_INDEX,
            body={"query": {"prefix": {"place_id": "wd:"}}}
        )
        wd_count = count_response['count']
        print(f"Found {wd_count:,} Wikidata places in index")

        return True
    except Exception as e:
        print(f"ERROR: Cannot connect to Elasticsearch: {e}")
        return False


def verify_place_exists(place_id):
    """Check if a place exists in Elasticsearch."""
    try:
        exists = es.exists(index=PLACES_INDEX, id=place_id)
        if not exists:
            print(f"    WARNING: {place_id} not found in index, skipping")
        return exists
    except Exception as e:
        print(f"    ERROR checking {place_id}: {e}")
        return False


def process_geoshapes_from_file(places_index, refs_file, batch_size=100):
    """
    Reads QIDs and geoshape references from a file, fetches GeoJSON, and updates ES.
    """
    if not os.path.exists(refs_file):
        print(f"Error: References file not found at {refs_file}")
        print("Please run wikidata-places.py first to generate the references file.")
        return

    # Create data directory if needed
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    os.makedirs(os.path.dirname(ERROR_LOG), exist_ok=True)

    downloaded_ids = get_downloaded_list()

    updates = []
    processed = 0
    updated = 0
    skipped_resumed = 0
    skipped_no_place = 0
    failed_fetch = 0
    last_report_time = time.time()

    print("\n" + "=" * 80)
    print("Starting geoshape fetching from local reference file")
    print("=" * 80)
    print(f"References file: {refs_file}")
    print(f"Log file: {LOG_FILE}")
    print(f"Error log: {ERROR_LOG}")
    print(f"Batch size: {batch_size}")

    # Count total references
    total_refs = sum(1 for _ in open(refs_file))
    print(f"Total references to process: {total_refs:,}")
    print(f"Already processed (from log): {len(downloaded_ids):,}")
    print(f"Remaining to process: {total_refs - len(downloaded_ids):,}")
    print("-" * 80)

    with open(refs_file, 'r') as f:
        for line_num, line in enumerate(f, 1):
            processed += 1

            # Extract QID and geoshape reference from the JSONL line
            try:
                ref_data = json.loads(line.strip())
                qid = ref_data['qid']
                geoshape_ref = ref_data['geoshape_ref']
                place_id = f"wd:{qid}"
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Line {line_num}: Skipping malformed line: {e}")
                continue

            # Progress reporting every 10 seconds
            current_time = time.time()
            if current_time - last_report_time > 10:
                percent = (processed / total_refs) * 100
                print(f"\nProgress: {processed:,}/{total_refs:,} ({percent:.1f}%) | "
                      f"Updated: {updated:,} | Resumed: {skipped_resumed:,} | "
                      f"No place: {skipped_no_place:,} | Failed: {failed_fetch:,}")
                last_report_time = current_time

            # --- Resumption Check ---
            if place_id in downloaded_ids:
                skipped_resumed += 1
                continue

            # --- Check if place exists in ES ---
            if not verify_place_exists(place_id):
                skipped_no_place += 1
                log_downloaded(place_id)  # Log as processed to avoid rechecking
                continue

            print(f"\n[{processed}/{total_refs}] Processing {place_id}...")

            # Fetch actual GeoJSON from Commons
            geometry = fetch_geojson_from_commons(geoshape_ref, place_id)

            if not geometry:
                failed_fetch += 1
                # Don't log as downloaded so we can retry later
                continue

            # Compute geodetically-correct representative point
            try:
                rep_point_new = compute_representative_point(geometry)
                if not rep_point_new:
                    print(f"    WARNING: Could not compute representative point")
                    rep_point_new = None
            except Exception as e:
                print(f"    ERROR computing rep_point: {e}")
                rep_point_new = None

            # Build the complete new location object
            new_location = {
                'geometry': geometry
            }
            if rep_point_new:
                new_location['rep_point'] = rep_point_new

            updates.append({
                "_op_type": "update",
                "_index": places_index,
                "_id": place_id,
                "script": {
                    "source": """
                        // Check if locations array exists and has at least one item.
                        if (ctx._source.locations != null && ctx._source.locations.size() > 0) {
                            // Update geometry in first location
                            ctx._source.locations[0].geometry = params.new_location.geometry;
                            if (params.new_location.rep_point != null) {
                                ctx._source.locations[0].rep_point = params.new_location.rep_point;
                            }
                        } else {
                            // Create the array if it was missing
                            ctx._source.locations = [params.new_location];
                        }
                    """,
                    "params": {
                        "new_location": new_location
                    }
                }
            })

            log_downloaded(place_id)  # Log immediately after successful fetch

            # Bulk update when batch is full
            if len(updates) >= batch_size:
                print(f"\n  Bulk updating {len(updates)} documents...")
                try:
                    success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
                    updated += success
                    if failed > 0:
                        print(f"    WARNING: {failed} updates failed")
                    else:
                        print(f"    ✓ Successfully updated {success} documents")
                    updates = []
                except Exception as e:
                    print(f"    ERROR in bulk update: {str(e)}")
                    log_error("BULK_UPDATE", str(e))
                    updates = []

                # Small delay to avoid overwhelming the system
                time.sleep(0.5)

    # Index remaining batches
    if updates:
        print(f"\n  Final bulk update of {len(updates)} documents...")
        try:
            success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
            updated += success
            if failed > 0:
                print(f"    WARNING: {failed} updates failed")
        except Exception as e:
            print(f"    ERROR in final bulk update: {str(e)}")
            log_error("FINAL_BULK_UPDATE", str(e))

    print("\n" + "=" * 80)
    print("GEOSHAPE PROCESSING COMPLETE")
    print("=" * 80)
    print(f"Total entries processed: {processed:,}")
    print(f"Successfully updated: {updated:,}")
    print(f"Skipped (already processed): {skipped_resumed:,}")
    print(f"Skipped (place not in index): {skipped_no_place:,}")
    print(f"Failed to fetch geometry: {failed_fetch:,}")

    if failed_fetch > 0:
        print(f"\nCheck {ERROR_LOG} for details on failed fetches")
        print("You can re-run this script to retry failed fetches")


def main():
    print("=" * 80)
    print("WIKIDATA GEOSHAPE FETCHER")
    print("=" * 80)
    print("\nThis script fetches complex geometries for Wikidata places from Commons.")
    print(f"Input file: {REFS_FILE}")
    print(f"Target index: {PLACES_INDEX}")

    if not os.path.exists(REFS_FILE):
        print(f"\nERROR: References file not found: {REFS_FILE}")
        print("Please run wikidata-places.py first to generate this file.")
        sys.exit(1)

    # Check Elasticsearch connection
    if not check_elasticsearch_connection():
        print("\nERROR: Cannot proceed without Elasticsearch connection")
        sys.exit(1)

    # Count references
    ref_count = sum(1 for _ in open(REFS_FILE))
    print(f"\nFound {ref_count:,} geoshape references to process")

    # Check for resume
    downloaded = get_downloaded_list()
    if downloaded:
        print(f"Found {len(downloaded):,} already processed (will resume)")
        remaining = ref_count - len(downloaded)
        print(f"Remaining to process: {remaining:,}")

        if remaining == 0:
            print("\nAll references have been processed!")
            # response = input("Re-process all? (y/n): ")  # Cannot use input in slurm jobs
            # if response.lower() != 'y':
            #     print("Nothing to do.")
            #     sys.exit(0)
            # # Clear the log to start fresh
            # os.remove(LOG_FILE)
            # print("Cleared log file, starting fresh...")

    print("\nThis will make many API calls to Wikimedia Commons.")
    print("Expected runtime: 4-8 hours with rate limiting.")
    print("The process is resumable - you can stop and restart anytime.")

    process_geoshapes_from_file(PLACES_INDEX, REFS_FILE, batch_size=100)
    create_checkpoint_snapshot(es, "wikidata_geoshapes")


if __name__ == "__main__":
    main()