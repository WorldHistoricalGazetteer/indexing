# processing/wikidata-geoshapes.py

"""
Post-processing script to fetch and add geoshape geometries to Wikidata places.
"""

import json
import requests
import time
import os
import sys
import sqlite3
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

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
CACHE_DB = os.path.join(DATA_DIR, "wikidata", "geoshape_cache.sqlite")

HEADERS = {
    "User-Agent": "PittCRC-GeoFetcher/1.0 (stg135@pitt.edu; https://whgazetteer.org/) python-requests",
}

RATE_LIMIT_SECONDS = 1.5
_rate_lock = threading.Lock()
_last_request_time = 0.0


def rate_limited():
    global _last_request_time
    with _rate_lock:
        now = time.time()
        delta = now - _last_request_time
        if delta < RATE_LIMIT_SECONDS:
            time.sleep(RATE_LIMIT_SECONDS - delta)
        _last_request_time = time.time()


# ----------------------------------------------------------------------
# SQLite cache
# ----------------------------------------------------------------------

def init_cache():
    os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)
    conn = sqlite3.connect(CACHE_DB, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
                 CREATE TABLE IF NOT EXISTS geoshape_cache
                 (
                     data_page
                     TEXT
                     PRIMARY
                     KEY,
                     geometry_json
                     TEXT,
                     status
                     TEXT
                     NOT
                     NULL,
                     error_msg
                     TEXT,
                     fetched_at
                     TEXT
                     NOT
                     NULL
                 )
                 """)
    conn.commit()
    return conn


def cache_get(conn, data_page):
    cur = conn.execute("SELECT geometry_json, status FROM geoshape_cache WHERE data_page = ?", (data_page,))
    row = cur.fetchone()
    if not row: return None, None
    geometry_json, status = row
    if status == "ok": return json.loads(geometry_json), "ok"
    return None, "error"


def cache_put_ok(conn, data_page, geometry):
    conn.execute("INSERT OR REPLACE INTO geoshape_cache VALUES (?, ?, 'ok', NULL, ?)",
                 (data_page, json.dumps(geometry), datetime.now().isoformat()))
    conn.commit()


def cache_put_error(conn, data_page, error_msg):
    conn.execute("INSERT OR REPLACE INTO geoshape_cache VALUES (?, NULL, 'error', ?, ?)",
                 (data_page, error_msg, datetime.now().isoformat()))
    conn.commit()


# ----------------------------------------------------------------------
# Logging helpers
# ----------------------------------------------------------------------

def log_error(place_id, error_msg):
    with open(ERROR_LOG, "a") as f:
        f.write(f"{datetime.now().isoformat()} | {place_id} | {error_msg}\n")


def get_downloaded_list():
    if not os.path.exists(LOG_FILE): return set()
    with open(LOG_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def log_downloaded(place_id):
    with open(LOG_FILE, "a") as f:
        f.write(f"{place_id}\n")


# ----------------------------------------------------------------------
# Commons fetch
# ----------------------------------------------------------------------

def fetch_geojson_from_commons(conn, data_page, place_id):
    """
    Fetch GeoJSON geometry from Wikimedia Commons.
    Returns geometry dict or None.
    NOTE: All print statements removed to allow progress bar in main loop.
    """
    if not data_page: return None

    # --- Cache check ---
    geometry, status = cache_get(conn, data_page)
    if status == "ok": return geometry
    if status == "error": return None

    url = "https://commons.wikimedia.org/w/api.php"
    title = f"Data:{data_page}"
    params = {
        "action": "query", "prop": "revisions", "rvslots": "*", "rvprop": "content",
        "format": "json", "titles": title, "formatversion": "2",
    }

    max_retries = 5
    for attempt in range(max_retries):
        try:
            rate_limited()
            response = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if response.status_code == 429:
                time.sleep(60 * (2 ** attempt))
                continue
            response.raise_for_status()
            data = response.json()
            break
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                cache_put_error(conn, data_page, str(e))
                log_error(place_id, str(e))
                return None

    # Parse response
    try:
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            raise ValueError(f"Page not found: {title}")

        revisions = pages[0].get("revisions", [])
        if not revisions:
            raise ValueError(f"No revisions: {title}")

        content = revisions[0].get("slots", {}).get("main", {}).get("content")
        if not content:
            raise ValueError(f"No content: {title}")

        geojson = json.loads(content)

        # Extract geometry
        geometry = None
        if geojson.get("type") == "Feature":
            geometry = geojson.get("geometry")
        elif geojson.get("type") == "FeatureCollection":
            feats = geojson.get("features", [])
            if feats: geometry = feats[0].get("geometry")
        elif geojson.get("type") in {"Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon"}:
            geometry = geojson

        if not geometry:
            raise ValueError(f"Unexpected GeoJSON type: {geojson.get('type')}")

        cache_put_ok(conn, data_page, geometry)
        return geometry

    except Exception as e:
        cache_put_error(conn, data_page, str(e))
        log_error(place_id, str(e))
        return None


# ----------------------------------------------------------------------
# ES Helpers
# ----------------------------------------------------------------------

def check_elasticsearch_connection():
    try:
        if not es.indices.exists(index=PLACES_INDEX): return False
        return True
    except:
        return False


def verify_place_exists(place_id):
    try:
        return es.exists(index=PLACES_INDEX, id=place_id)
    except:
        return False


# ----------------------------------------------------------------------
# Main Loop
# ----------------------------------------------------------------------

def fetch_task(args):
    conn, geoshape_ref, place_id = args
    geometry = fetch_geojson_from_commons(conn, geoshape_ref, place_id)
    return place_id, geometry, geoshape_ref


def process_geoshapes_from_file(places_index, refs_file, batch_size=100):
    conn = init_cache()
    downloaded_ids = get_downloaded_list()

    print("Scanning for tasks...")
    tasks = []

    with open(refs_file, "r") as f:
        for line in f:
            ref = json.loads(line)
            place_id = f"wd:{ref['qid']}"

            if place_id in downloaded_ids: continue
            # Optional: verify_place_exists is slow. Comment out if confident.
            # if not verify_place_exists(place_id):
            #    log_downloaded(place_id)
            #    continue

            tasks.append((conn, ref["geoshape_ref"], place_id))

    total_tasks = len(tasks)
    print(f"Starting {total_tasks:,} fetch tasks")

    updates = []
    completed = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_task, t): t for t in tasks}

        for future in as_completed(futures):
            place_id, geometry, ref_name = future.result()
            completed += 1

            # --- Progress Bar Update ---
            if completed % 5 == 0 or completed == total_tasks:
                percent = (completed / total_tasks) * 100
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0

                # Calculate ETA
                if rate > 0:
                    remaining_items = total_tasks - completed
                    eta_seconds = int(remaining_items / rate)
                    eta_str = str(timedelta(seconds=eta_seconds))
                else:
                    eta_str = "--:--:--"

                # Truncate filename for display
                display_name = (ref_name[:20] + '..') if len(ref_name) > 20 else ref_name

                # Clear line and Print
                sys.stdout.write(
                    f"\r[WD-Shapes] {completed:,}/{total_tasks:,} ({percent:.1f}%) | {rate:.1f} it/s | ETA: {eta_str} | Active: {display_name:<20}")
                sys.stdout.flush()
            # ---------------------------

            if not geometry:
                continue

            rep_point = compute_representative_point(geometry)
            updates.append({
                "_op_type": "update", "_index": places_index, "_id": place_id,
                "script": {
                    "source": """
                        if (ctx._source.locations != null && ctx._source.locations.size() > 0) {
                            ctx._source.locations[0].geometry = params.geom;
                            if (params.rep != null) { ctx._source.locations[0].rep_point = params.rep; }
                        } else {
                            ctx._source.locations = [params.newloc];
                        }
                    """,
                    "params": {
                        "geom": geometry, "rep": rep_point,
                        "newloc": {"geometry": geometry, "rep_point": rep_point},
                    },
                },
            })
            log_downloaded(place_id)

            if len(updates) >= batch_size:
                helpers.bulk(es, updates, raise_on_error=False)
                updates.clear()

    if updates:
        helpers.bulk(es, updates, raise_on_error=False)

    print("\nDone.")
    conn.close()


def main():
    if not check_elasticsearch_connection(): sys.exit(1)
    process_geoshapes_from_file(PLACES_INDEX, REFS_FILE, batch_size=BATCH_SIZE)
    create_checkpoint_snapshot(es, "wikidata_geoshapes")


if __name__ == "__main__":
    main()