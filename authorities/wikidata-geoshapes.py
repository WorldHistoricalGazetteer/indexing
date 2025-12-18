# processing/wikidata-geoshapes.py

"""
Post-processing script to fetch and add geoshape geometries to Wikidata places.

This version adds a persistent SQLite cache so Wikimedia Commons geoshapes
are fetched at most once per data page across all runs.
"""

import json
import requests
import time
import os
import sys
import sqlite3
from datetime import datetime

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot
from processing.helpers import compute_representative_point
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

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

RATE_LIMIT_SECONDS = 1.5  # conservative, safe
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
    cur = conn.execute(
        "SELECT geometry_json, status FROM geoshape_cache WHERE data_page = ?",
        (data_page,)
    )
    row = cur.fetchone()
    if not row:
        return None, None
    geometry_json, status = row
    if status == "ok":
        return json.loads(geometry_json), "ok"
    return None, "error"


def cache_put_ok(conn, data_page, geometry):
    conn.execute(
        """
        INSERT OR REPLACE INTO geoshape_cache
        (data_page, geometry_json, status, error_msg, fetched_at)
        VALUES (?, ?, 'ok', NULL, ?)
        """,
        (data_page, json.dumps(geometry), datetime.now().isoformat())
    )
    conn.commit()


def cache_put_error(conn, data_page, error_msg):
    conn.execute(
        """
        INSERT OR REPLACE INTO geoshape_cache
        (data_page, geometry_json, status, error_msg, fetched_at)
        VALUES (?, NULL, 'error', ?, ?)
        """,
        (data_page, error_msg, datetime.now().isoformat())
    )
    conn.commit()


# ----------------------------------------------------------------------
# Logging helpers
# ----------------------------------------------------------------------

def log_error(place_id, error_msg):
    with open(ERROR_LOG, "a") as f:
        f.write(f"{datetime.now().isoformat()} | {place_id} | {error_msg}\n")


def get_downloaded_list():
    if not os.path.exists(LOG_FILE):
        return set()
    with open(LOG_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def log_downloaded(place_id):
    with open(LOG_FILE, "a") as f:
        f.write(f"{place_id}\n")
        f.flush()


# ----------------------------------------------------------------------
# Commons fetch with cache
# ----------------------------------------------------------------------

def fetch_geojson_from_commons(conn, data_page, place_id):
    """
    Fetch GeoJSON geometry from Wikimedia Commons, with SQLite caching.
    """
    if not data_page:
        return None

    # --- Cache check ---
    geometry, status = cache_get(conn, data_page)
    if status == "ok":
        print(f"    ✓ Using cached geometry for Data:{data_page}")
        return geometry
    if status == "error":
        print(f"    ✗ Cached failure for Data:{data_page}")
        return None

    url = "https://commons.wikimedia.org/w/api.php"
    title = f"Data:{data_page}"
    params = {
        "action": "query",
        "prop": "revisions",
        "rvslots": "*",
        "rvprop": "content",
        "format": "json",
        "titles": title,
        "formatversion": "2",
    }

    print(f"    Fetching {title} for {place_id}...")

    max_retries = 5
    for attempt in range(max_retries):
        try:
            rate_limited()
            response = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if response.status_code == 429:
                wait = 60 * (2 ** attempt)
                print(f"      Rate limited, waiting {wait}s")
                time.sleep(wait)
                continue

            response.raise_for_status()
            data = response.json()
            break
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                msg = f"Failed after retries: {e}"
                cache_put_error(conn, data_page, msg)
                log_error(place_id, msg)
                return None

    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        msg = f"Page not found: {title}"
        cache_put_error(conn, data_page, msg)
        log_error(place_id, msg)
        return None

    revisions = pages[0].get("revisions", [])
    if not revisions:
        msg = f"No revisions: {title}"
        cache_put_error(conn, data_page, msg)
        log_error(place_id, msg)
        return None

    content = revisions[0].get("slots", {}).get("main", {}).get("content")
    if not content:
        msg = f"No content: {title}"
        cache_put_error(conn, data_page, msg)
        log_error(place_id, msg)
        return None

    try:
        geojson = json.loads(content)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON: {e}"
        cache_put_error(conn, data_page, msg)
        log_error(place_id, msg)
        return None

    geometry = None
    if geojson.get("type") == "Feature":
        geometry = geojson.get("geometry")
    elif geojson.get("type") == "FeatureCollection":
        feats = geojson.get("features", [])
        if feats:
            geometry = feats[0].get("geometry")
    elif geojson.get("type") in {
        "Point", "LineString", "Polygon",
        "MultiPoint", "MultiLineString",
        "MultiPolygon", "GeometryCollection",
    }:
        geometry = geojson

    if not geometry:
        msg = f"Unexpected GeoJSON structure: {geojson.get('type')}"
        cache_put_error(conn, data_page, msg)
        log_error(place_id, msg)
        return None

    cache_put_ok(conn, data_page, geometry)
    print(f"    ✓ Cached {geometry.get('type')} geometry")
    return geometry


# ----------------------------------------------------------------------
# Elasticsearch helpers
# ----------------------------------------------------------------------

def check_elasticsearch_connection():
    try:
        info = es.info()
        print(f"Connected to Elasticsearch {info['version']['number']}")
        if not es.indices.exists(index=PLACES_INDEX):
            print(f"ERROR: Index '{PLACES_INDEX}' does not exist")
            return False
        return True
    except Exception as e:
        print(f"ERROR: Elasticsearch unavailable: {e}")
        return False


def verify_place_exists(place_id):
    try:
        return es.exists(index=PLACES_INDEX, id=place_id)
    except Exception:
        return False


# ----------------------------------------------------------------------
# Main processing loop
# ----------------------------------------------------------------------

def fetch_task(args):
    conn, geoshape_ref, place_id = args
    geometry = fetch_geojson_from_commons(conn, geoshape_ref, place_id)
    return place_id, geometry


def process_geoshapes_from_file(places_index, refs_file, batch_size=100):
    conn = init_cache()
    downloaded_ids = get_downloaded_list()

    tasks = []
    updates = []

    with open(refs_file, "r") as f:
        for line in f:
            ref = json.loads(line)
            qid = ref["qid"]
            geoshape_ref = ref["geoshape_ref"]
            place_id = f"wd:{qid}"

            if place_id in downloaded_ids:
                continue
            if not verify_place_exists(place_id):
                log_downloaded(place_id)
                continue

            tasks.append((conn, geoshape_ref, place_id))

    print(f"Submitting {len(tasks):,} fetch tasks")

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_task, t): t for t in tasks}

        for future in as_completed(futures):
            place_id, geometry = future.result()
            if not geometry:
                continue

            rep_point = compute_representative_point(geometry)

            updates.append({
                "_op_type": "update",
                "_index": places_index,
                "_id": place_id,
                "script": {
                    "source": """
                        if (ctx._source.locations != null && ctx._source.locations.size() > 0) {
                            ctx._source.locations[0].geometry = params.geom;
                            if (params.rep != null) {
                                ctx._source.locations[0].rep_point = params.rep;
                            }
                        } else {
                            ctx._source.locations = [params.newloc];
                        }
                    """,
                    "params": {
                        "geom": geometry,
                        "rep": rep_point,
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

    conn.close()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def main():
    if not check_elasticsearch_connection():
        sys.exit(1)

    process_geoshapes_from_file(PLACES_INDEX, REFS_FILE, batch_size=BATCH_SIZE)
    create_checkpoint_snapshot(es, "wikidata_geoshapes")


if __name__ == "__main__":
    main()
