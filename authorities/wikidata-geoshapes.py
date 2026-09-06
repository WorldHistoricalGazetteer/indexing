# authorities/wikidata-geoshapes.py

"""
Stage Wikidata geoshape geometries as a Phase 3 update patch.

Reads ``GEOSHAPE_REFS_FILE`` (one ``{qid, geoshape_ref}`` per line), fetches
each referenced geometry from Wikimedia Commons (with on-disk SQLite cache
and rate limiting), writes the polygon to the geometry store, and emits one
``staged/wd/update_patch/places.update.jsonl`` row per fetched geometry.
``processing/update_merge.py`` collapses the patch into the namespace's
``update_merged/`` snapshot before H3 derivation.

Per Master Plan + Batch 4c Phase 3: this script never contacts Elasticsearch.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests

from processing.geometry_collection_processor import validate_geometry
from processing.helpers import (
    compute_h3_fields,
    enrich_geometry,
    select_h3_cover_geometry,
)
from processing.temporal import attested_at, source_release_year
from processing.settings import (
    BATCH_SIZE,
    DATA_DIR,
    GEOSHAPE_LOG_FILE,
    GEOSHAPE_REFS_FILE,
    STAGED_BASE_DIR,
)
from processing.staging_contract import UPDATE_PATCH_FILENAME


# ----------------------------------------------------------------------------
# Cache + rate-limit (carried over from the legacy ES path)
# ----------------------------------------------------------------------------

ERROR_LOG = os.path.join(DATA_DIR, "wikidata", "geoshapes_errors.log")
CACHE_DB = os.path.join(DATA_DIR, "wikidata", "geoshape_cache.sqlite")


def _observation_year() -> int:
    """Year these Commons shapes attest, from the refs file that selected them.

    Was a hardcoded ``[{"start": {"in": 2025}, "end": {"in": 2025}}]``, which
    claimed every Wikidata geoshape existed *only* in 2025 — the
    attestation-as-lifespan defect (place#164) — and went stale the moment a
    newer dump was fetched. The refs file is regenerated from each Wikidata
    dump, so its mtime dates the generation of shapes this run observes.
    """
    return source_release_year(GEOSHAPE_REFS_FILE, label='wd-geoshapes')


#: Timespans asserting "attested alive in <observation year>" — started no
#: later than it, ended no earlier, with both outer bounds left unbounded.
GEOSHAPE_TIMESPANS = attested_at(_observation_year())

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


_thread_local = threading.local()


def get_cache_conn():
    if not hasattr(_thread_local, "conn"):
        _thread_local.conn = sqlite3.connect(CACHE_DB, check_same_thread=False)
        _thread_local.conn.execute("PRAGMA journal_mode=WAL;")
    return _thread_local.conn


def init_cache():
    os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geoshape_cache
        (
            data_page TEXT PRIMARY KEY,
            geometry_json TEXT,
            status TEXT NOT NULL,
            error_msg TEXT,
            fetched_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def cache_get(data_page):
    conn = get_cache_conn()
    cur = conn.execute(
        "SELECT geometry_json, status FROM geoshape_cache WHERE data_page = ?",
        (data_page,),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    geometry_json, status = row
    if status == "ok":
        return json.loads(geometry_json), "ok"
    return None, "error"


def cache_put_ok(data_page, geometry):
    conn = get_cache_conn()
    conn.execute(
        "INSERT OR REPLACE INTO geoshape_cache VALUES (?, ?, 'ok', NULL, ?)",
        (data_page, json.dumps(geometry), datetime.now().isoformat()),
    )
    conn.commit()


def cache_put_error(data_page, error_msg):
    conn = get_cache_conn()
    conn.execute(
        "INSERT OR REPLACE INTO geoshape_cache VALUES (?, NULL, 'error', ?, ?)",
        (data_page, error_msg, datetime.now().isoformat()),
    )
    conn.commit()


def log_error(place_id, error_msg):
    with open(ERROR_LOG, "a") as f:
        f.write(f"{datetime.now().isoformat()} | {place_id} | {error_msg}\n")


def get_downloaded_list():
    if not os.path.exists(GEOSHAPE_LOG_FILE):
        return set()
    with open(GEOSHAPE_LOG_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def log_downloaded(place_id):
    with open(GEOSHAPE_LOG_FILE, "a") as f:
        f.write(f"{place_id}\n")


# ----------------------------------------------------------------------------
# Commons fetch
# ----------------------------------------------------------------------------


def fetch_geojson_from_commons(data_page, place_id):
    """Fetch a GeoJSON geometry from Wikimedia Commons.

    Cached on disk; returns ``None`` on transient or terminal failure.
    """
    if not data_page:
        return None

    geometry, status = cache_get(data_page)
    if status == "ok":
        return geometry
    if status == "error":
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
        if "data" in geojson and isinstance(geojson["data"], dict):
            geojson = geojson["data"]

        geometry = None
        if geojson.get("type") == "Feature":
            geometry = geojson.get("geometry")
        elif geojson.get("type") == "FeatureCollection":
            feats = geojson.get("features", [])
            if feats:
                geometry = feats[0].get("geometry")
        elif geojson.get("type") in {
            "Point", "LineString", "Polygon", "MultiPoint",
            "MultiLineString", "MultiPolygon",
        }:
            geometry = geojson
        elif geojson.get("type") == "GeometryCollection":
            geometry = geojson

        if not geometry:
            raise ValueError(f"Unexpected GeoJSON structure: {geojson.get('type', 'unknown')}")

        geometry = validate_geometry(geometry)
        if not geometry:
            raise ValueError("Geometry validation failed - invalid coordinate structure")

        cache_put_ok(data_page, geometry)
        return geometry
    except Exception as e:
        cache_put_error(data_page, str(e))
        log_error(place_id, str(e))
        return None


def _fetch_task(args):
    geoshape_ref, place_id = args
    geometry = fetch_geojson_from_commons(geoshape_ref, place_id)
    return place_id, geometry, geoshape_ref


# ----------------------------------------------------------------------------
# Patch emission
# ----------------------------------------------------------------------------


def _patch_path() -> Path:
    return Path(STAGED_BASE_DIR) / "wd" / "update_patch" / UPDATE_PATCH_FILENAME


def stage_geoshapes(refs_file: str, batch_size: int = BATCH_SIZE) -> dict:
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    init_cache()
    downloaded_ids = get_downloaded_list()

    print("=" * 80)
    print("WIKIDATA GEOSHAPES — STAGED PATCH EMISSION")
    print("=" * 80)
    print(f"Refs:   {refs_file}")
    out_path = _patch_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Output: {out_path}")

    tasks: list[tuple[str, str]] = []
    with open(refs_file, "r") as f:
        for line in f:
            ref = json.loads(line)
            place_id = f"wd:{ref['qid']}"
            if place_id in downloaded_ids:
                continue
            tasks.append((ref["geoshape_ref"], place_id))

    total_tasks = len(tasks)
    print(f"Pending fetches: {total_tasks:,}")
    if total_tasks == 0:
        print("Nothing to do.")
        return {"fetched": 0, "rows_written": 0, "patch_path": str(out_path)}

    rows_written = 0
    completed = 0
    started = time.time()

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, "wd_geoshapes") as gsw, \
            out_path.open("a", encoding="utf-8") as out_jsonl:
        configure_module_writer(gsw)
        try:
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {executor.submit(_fetch_task, t): t for t in tasks}
                for future in as_completed(futures):
                    place_id, geometry, ref_name = future.result()
                    completed += 1
                    if completed % 5 == 0 or completed == total_tasks:
                        rate = completed / max(1.0, time.time() - started)
                        eta = (
                            str(timedelta(seconds=int((total_tasks - completed) / rate)))
                            if rate > 0 else "--:--:--"
                        )
                        sys.stdout.write(
                            f"\r[WD-Shapes] {completed:,}/{total_tasks:,} "
                            f"({completed / total_tasks * 100:.1f}%) | "
                            f"{rate:.1f} it/s | ETA: {eta}"
                        )
                        sys.stdout.flush()

                    if not geometry:
                        continue

                    geom_entry = enrich_geometry(
                        geometry,
                        timespans=GEOSHAPE_TIMESPANS,
                        geom_key=f"{place_id}_0",
                    )
                    # Single retry on enrichment failure (network jitter at fetch time
                    # can leave a malformed geometry in the cache).
                    if not geom_entry:
                        conn = get_cache_conn()
                        conn.execute(
                            "DELETE FROM geoshape_cache WHERE data_page = ?",
                            (ref_name,),
                        )
                        conn.commit()
                        geometry = fetch_geojson_from_commons(ref_name, place_id)
                        if geometry:
                            geom_entry = enrich_geometry(
                                geometry,
                                timespans=GEOSHAPE_TIMESPANS,
                                geom_key=f"{place_id}_0",
                            )
                        if not geom_entry:
                            continue

                    h3_centroid = None
                    h3_cover: list = []
                    if geom_entry.get("repr_point"):
                        rp = geom_entry["repr_point"]
                        h3_geom = select_h3_cover_geometry(geom_entry, geometry)
                        h3_centroid, h3_cover = compute_h3_fields(
                            rp["lon"], rp["lat"], h3_geom,
                        )

                    # h3_centroid/h3_cover are PER-GEOMETRY fields: the schema
                    # declares geometries.h3_centroid / geometries.h3_cover and
                    # has no root equivalents. They used to be emitted at the
                    # patch root, which apply_update_patch then wrote to the
                    # document root — 1,310,192 undeclared root instances.
                    if h3_centroid:
                        geom_entry["h3_centroid"] = h3_centroid
                    if h3_cover:
                        geom_entry["h3_cover"] = h3_cover
                    row: dict = {
                        "place_id": place_id,
                        "geometries_to_replace": [geom_entry],
                    }
                    out_jsonl.write(json.dumps(row, ensure_ascii=True) + "\n")
                    log_downloaded(place_id)
                    rows_written += 1
        finally:
            configure_module_writer(None)

    print(f"\n[VAST] wikidata geoshape geometries staged: {gsw.count:,}")
    print(f"Patch rows written: {rows_written:,}")
    return {
        "fetched": completed,
        "rows_written": rows_written,
        "patch_path": str(out_path),
        "geometries_staged": int(gsw.count),
    }


def main():
    if not os.path.exists(GEOSHAPE_REFS_FILE):
        print(f"ERROR: refs file not found: {GEOSHAPE_REFS_FILE}", file=sys.stderr)
        sys.exit(1)
    summary = stage_geoshapes(GEOSHAPE_REFS_FILE, batch_size=BATCH_SIZE)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
