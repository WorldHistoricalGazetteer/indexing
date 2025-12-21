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


# SQLite cache functions
_thread_local = threading.local()


def get_cache_conn():
    """Get thread-local SQLite connection."""
    if not hasattr(_thread_local, 'conn'):
        _thread_local.conn = sqlite3.connect(CACHE_DB, check_same_thread=False)
        _thread_local.conn.execute("PRAGMA journal_mode=WAL;")
    return _thread_local.conn


def init_cache():
    os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
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
    conn.close()


def cache_get(data_page):
    conn = get_cache_conn()
    cur = conn.execute("SELECT geometry_json, status FROM geoshape_cache WHERE data_page = ?", (data_page,))
    row = cur.fetchone()
    if not row: return None, None
    geometry_json, status = row
    if status == "ok": return json.loads(geometry_json), "ok"
    return None, "error"


def cache_put_ok(data_page, geometry):
    conn = get_cache_conn()
    conn.execute("INSERT OR REPLACE INTO geoshape_cache VALUES (?, ?, 'ok', NULL, ?)",
                 (data_page, json.dumps(geometry), datetime.now().isoformat()))
    conn.commit()


def cache_put_error(data_page, error_msg):
    conn = get_cache_conn()
    conn.execute("INSERT OR REPLACE INTO geoshape_cache VALUES (?, NULL, 'error', ?, ?)",
                 (data_page, error_msg, datetime.now().isoformat()))
    conn.commit()


# Logging helpers
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


# Commons fetch
def fetch_geojson_from_commons(data_page, place_id):
    """Fetch GeoJSON geometry from Wikimedia Commons."""
    if not data_page: return None

    geometry, status = cache_get(data_page)
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
                cache_put_error(data_page, str(e))
                log_error(place_id, str(e))
                return None

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

        # Wikimedia Commons wraps GeoJSON in a "data" field
        if "data" in geojson and isinstance(geojson["data"], dict):
            geojson = geojson["data"]

        geometry = None
        if geojson.get("type") == "Feature":
            geometry = geojson.get("geometry")
        elif geojson.get("type") == "FeatureCollection":
            feats = geojson.get("features", [])
            if feats: geometry = feats[0].get("geometry")
        elif geojson.get("type") in {"Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon"}:
            geometry = geojson
        elif geojson.get("type") == "GeometryCollection":
            # Merge all Polygons and MultiPolygons into a single MultiPolygon
            geometries = geojson.get("geometries", [])
            all_polygons = []

            for g in geometries:
                if g.get("type") == "Polygon":
                    # Polygon coordinates are [[ring1], [ring2], ...]
                    all_polygons.append(g.get("coordinates", []))
                elif g.get("type") == "MultiPolygon":
                    # MultiPolygon coordinates are [[[ring1], [ring2], ...], [[ring1], ...]]
                    all_polygons.extend(g.get("coordinates", []))

            if all_polygons:
                # If only one polygon, use Polygon type; otherwise MultiPolygon
                if len(all_polygons) == 1:
                    geometry = {
                        "type": "Polygon",
                        "coordinates": all_polygons[0]
                    }
                else:
                    geometry = {
                        "type": "MultiPolygon",
                        "coordinates": all_polygons
                    }
            else:
                raise ValueError(f"GeometryCollection contains no Polygon/MultiPolygon")

        if not geometry:
            raise ValueError(f"Unexpected GeoJSON structure: {geojson.get('type', 'unknown')}")

        cache_put_ok(data_page, geometry)
        return geometry

    except Exception as e:
        cache_put_error(data_page, str(e))
        log_error(place_id, str(e))
        return None


def check_elasticsearch_connection():
    try:
        if not es.indices.exists(index=PLACES_INDEX): return False
        return True
    except:
        return False


def fetch_task(args):
    geoshape_ref, place_id = args
    geometry = fetch_geojson_from_commons(geoshape_ref, place_id)
    return place_id, geometry, geoshape_ref


def process_geoshapes_from_file(places_index, refs_file, batch_size=100):
    init_cache()  # Initialize cache database
    downloaded_ids = get_downloaded_list()

    print("Scanning for tasks...")
    tasks = []

    with open(refs_file, "r") as f:
        for line in f:
            ref = json.loads(line)
            place_id = f"wd:{ref['qid']}"

            if place_id in downloaded_ids: continue
            tasks.append((ref["geoshape_ref"], place_id))

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

            if completed % 5 == 0 or completed == total_tasks:
                percent = (completed / total_tasks) * 100
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0

                if rate > 0:
                    remaining_items = total_tasks - completed
                    eta_seconds = int(remaining_items / rate)
                    eta_str = str(timedelta(seconds=eta_seconds))
                else:
                    eta_str = "--:--:--"

                display_name = (ref_name[:20] + '..') if len(ref_name) > 20 else ref_name

                sys.stdout.write(
                    f"\r[WD-Shapes] {completed:,}/{total_tasks:,} ({percent:.1f}%) | {rate:.1f} it/s | ETA: {eta_str} | Active: {display_name:<20}")
                sys.stdout.flush()

            if not geometry:
                continue

            rep_point = compute_representative_point(geometry)

            # Update geometries array
            updates.append({
                "_op_type": "update",
                "_index": places_index,
                "_id": place_id,
                "script": {
                    "source": """
                        if (ctx._source.geometries == null || ctx._source.geometries.size() == 0) {
                            ctx._source.geometries = [params.new_geom];
                        } else {
                            ctx._source.geometries[0].geom = params.geom;
                            ctx._source.geometries[0].repr_point = params.rep;
                        }
                    """,
                    "params": {
                        "geom": geometry,
                        "rep": rep_point,
                        "new_geom": {
                            "geom": geometry,
                            "repr_point": rep_point,
                            "timespans": [{
                                "start": {"in": 2025},
                                "end": {"in": 2025}
                            }]
                        }
                    }
                }
            })
            log_downloaded(place_id)

            if len(updates) >= batch_size:
                helpers.bulk(es, updates, raise_on_error=False)
                updates.clear()

    if updates:
        helpers.bulk(es, updates, raise_on_error=False)

    print("\nDone.")


def main():
    if not check_elasticsearch_connection():
        sys.exit(1)
    process_geoshapes_from_file(PLACES_INDEX, REFS_FILE, batch_size=BATCH_SIZE)
    create_checkpoint_snapshot(es, "wikidata_geoshapes")


if __name__ == "__main__":
    main()