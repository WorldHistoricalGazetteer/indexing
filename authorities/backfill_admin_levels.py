# authorities/backfill_admin_levels.py

"""
Backfill admin_level 0 and 1 boundaries via Overpass API.

Two-phase pipeline with local caching:

  Phase 1 — FETCH: Query Overpass for each (source, level) pair and cache
  the raw JSON response to disk.  Each query is independent and cached
  separately, so a failure or retry doesn't repeat successful work.

  Phase 2 — INDEX: Read cached JSON, assemble polygons via osm2geojson,
  build boundary docs, and index into ES.

Cache files are written to ``{DATA_DIR}/boundaries/overpass_cache/``.
Existing cache files are reused unless ``--force-fetch`` is given.

Designed to run as a Slurm job on CRC (where ES is accessible):

    # On CRC login node:
    sbatch processing/backfill_admin_levels.slurm

Or manually:

    # Fetch only (can run anywhere with internet):
    python -m authorities.backfill_admin_levels --fetch-only

    # Index only (requires ES access, uses cached data):
    python -m authorities.backfill_admin_levels --index-only --es-host URL

    # Both phases:
    python -m authorities.backfill_admin_levels --es-host URL

    # Dry run (fetch + report, no indexing):
    python -m authorities.backfill_admin_levels --dry-run

Requires:
    pip install osm2geojson requests shapely elasticsearch
"""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import requests
import osm2geojson
from shapely.geometry import shape, mapping
from shapely.validation import make_valid

from elasticsearch import Elasticsearch, helpers
from processing.helpers import compute_representative_point
from processing.settings import ES_HOST, DATA_DIR, BOUNDARIES_INDEX

# ---- Configuration ----

OVERPASS_ENDPOINTS = {
    'osm': 'https://overpass-api.de/api/interpreter',
    'ohm': 'https://overpass-api.openhistoricalmap.org/api/interpreter',
}

BACKFILL_LEVELS = [0, 1]

# Default cache directory for raw Overpass JSON responses (overridable via --cache-dir)
CACHE_DIR = Path(DATA_DIR) / 'boundaries' / 'overpass_cache'

# Overpass request timeout (seconds)
OVERPASS_TIMEOUT = 600


# ---- Date parsing (inlined from osm-boundaries.py to avoid osmium dep) ----

_YEAR_RE = re.compile(
    r'^~?(?:(?:before|after|about|circa|ca)\s*:?\s*)?'
    r'(-?\d{1,5})(?:[-/]\d{1,2})?(?:[-/]\d{1,2})?(?:T.*)?$',
    re.IGNORECASE,
)
_CENTURY_RE = re.compile(r'^C(\d{1,2})$', re.IGNORECASE)


def _parse_year(date_str):
    if not date_str:
        return None
    date_str = date_str.strip()
    m = _YEAR_RE.match(date_str)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, OverflowError):
            return None
    m = _CENTURY_RE.match(date_str)
    if m:
        try:
            return (int(m.group(1)) - 1) * 100
        except ValueError:
            return None
    return None


def _build_timespans(tags):
    start_year = _parse_year(tags.get('start_date'))
    end_year = _parse_year(tags.get('end_date'))
    if start_year is not None or end_year is not None:
        ts = {}
        if start_year is not None:
            ts['start'] = {'in': start_year}
        if end_year is not None:
            ts['end'] = {'in': end_year}
        return [ts]
    return []


# ---- Phase 1: Fetch from Overpass with caching ----

def _cache_path(source, level):
    """Return the cache file path for a given (source, level) pair."""
    return CACHE_DIR / f'{source}_admin_level_{level}.json'


def fetch_level(source, level, force=False):
    """
    Fetch a single admin_level from Overpass and cache to disk.

    Returns:
        Path to cached JSON file, or None on failure.
    """
    cache_file = _cache_path(source, level)

    if cache_file.exists() and not force:
        size_mb = cache_file.stat().st_size / 1e6
        print(f"  ✓ Cached: {cache_file.name} ({size_mb:.1f} MB) — skipping fetch")
        return cache_file

    endpoint = OVERPASS_ENDPOINTS[source]
    query = (
        f'[out:json][timeout:{OVERPASS_TIMEOUT}];'
        f'relation["boundary"="administrative"]["admin_level"="{level}"];'
        f'out geom;'
    )

    print(f"  Fetching {source.upper()} admin_level={level} from {endpoint} ...")

    try:
        resp = requests.post(
            endpoint,
            data={'data': query},
            timeout=OVERPASS_TIMEOUT + 60,
            headers={'User-Agent': 'WHG-Boundary-Backfill/1.0'},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ✗ Overpass request failed: {e}")
        return None

    # Write raw response bytes to cache (fast, no re-serialization).
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_file, 'wb') as f:
        f.write(resp.content)

    size_mb = cache_file.stat().st_size / 1e6
    print(f"  ✓ Cached: {cache_file} ({size_mb:.1f} MB)")
    return cache_file


def do_fetch(sources, force=False):
    """Phase 1: Fetch all (source, level) pairs from Overpass."""
    print(f"\n{'=' * 70}")
    print("PHASE 1: FETCH FROM OVERPASS")
    print(f"{'=' * 70}")

    results = {}
    for source in sources:
        for level in BACKFILL_LEVELS:
            key = f"{source}_level{level}"
            path = fetch_level(source, level, force=force)
            if path:
                results[key] = path
            print()

    cached = sum(1 for v in results.values() if v)
    total = len(sources) * len(BACKFILL_LEVELS)
    print(f"  Fetched/cached: {cached}/{total}")
    return results


# ---- Phase 2: Assemble geometry and index into ES ----

def feature_to_boundary_doc(feature, namespace):
    """Convert a GeoJSON Feature (from osm2geojson) to a boundary ES doc."""
    props = feature.get('properties', {})
    tags = props.get('tags', {})
    geojson_geom = feature.get('geometry')

    name = tags.get('name')
    if not name or not geojson_geom:
        return None

    try:
        admin_level = int(tags.get('admin_level', ''))
    except (ValueError, TypeError):
        return None

    relation_id = props.get('id')
    if not relation_id:
        return None

    try:
        shapely_geom = shape(geojson_geom)
        if not shapely_geom.is_valid:
            shapely_geom = make_valid(shapely_geom)
            if not shapely_geom.is_valid:
                return None
        if shapely_geom.is_empty:
            return None
    except Exception:
        return None

    boundary_id = f"{namespace}:r{relation_id}"
    full_geom = mapping(shapely_geom)

    doc = {
        'boundary_id': boundary_id,
        'namespace': namespace,
        'name': name,
        'source': namespace,
        'admin_level': admin_level,
        'indexed_at': datetime.now().isoformat(),
        'geom': full_geom,
    }

    # Convex hull
    try:
        hull = shapely_geom.convex_hull.simplify(0).buffer(0)
        if hull.is_empty or not hull.is_valid:
            hull = shapely_geom.envelope
    except Exception:
        hull = shapely_geom.envelope

    if hull and not hull.is_empty:
        doc['hull'] = mapping(hull)
        hb = hull.bounds
        doc['bounds'] = [round(hb[0], 6), round(hb[1], 6),
                         round(hb[2], 6), round(hb[3], 6)]

    rep_point = compute_representative_point(full_geom)
    if rep_point:
        doc['repr_point'] = rep_point

    # Alternate names
    alt_names = {}
    for k, v in tags.items():
        if k.startswith('name:'):
            alt_names[k[5:]] = v
        elif k == 'int_name':
            alt_names['int'] = v
    if alt_names:
        doc['alt_names'] = alt_names

    official = tags.get('official_name')
    if official and official != name:
        doc['name_local'] = official

    # Country codes
    ccodes = []
    iso1 = tags.get('ISO3166-1:alpha2') or tags.get('ISO3166-1')
    if iso1:
        ccodes.append(iso1.upper())
    iso2 = tags.get('ISO3166-2')
    if iso2:
        parts = iso2.split('-')
        if parts and len(parts[0]) == 2:
            cc = parts[0].upper()
            if cc not in ccodes:
                ccodes.append(cc)
    if ccodes:
        doc['ccodes'] = ccodes

    pop = tags.get('population')
    if pop:
        try:
            doc['population'] = int(pop)
        except (ValueError, TypeError):
            pass

    wd = tags.get('wikidata')
    if wd:
        doc['wikidata_id'] = wd

    timespans = _build_timespans(tags)
    if timespans:
        doc['timespans'] = timespans

    return doc


def process_cache_file(cache_file, namespace):
    """
    Read a cached Overpass JSON file, assemble polygons, build ES docs.

    Returns:
        list of ES doc dicts
    """
    print(f"  Processing {cache_file.name} ...")

    with open(cache_file) as f:
        overpass_data = json.load(f)

    elements = overpass_data.get('elements', [])
    if not elements:
        print(f"    No elements in cache file")
        return []

    print(f"    {len(elements)} relation(s) → assembling polygons ...")
    geojson = osm2geojson.json2geojson(overpass_data)
    features = geojson.get('features', [])
    print(f"    {len(features)} feature(s) assembled")

    docs = []
    skipped = 0
    for feature in features:
        doc = feature_to_boundary_doc(feature, namespace=namespace)
        if doc:
            docs.append(doc)
        else:
            skipped += 1

    print(f"    Documents: {len(docs)}  Skipped: {skipped}")
    return docs


def do_index(sources, es_host=None, dry_run=False):
    """Phase 2: Read cached data, assemble geometry, index into ES."""
    print(f"\n{'=' * 70}")
    print(f"PHASE 2: ASSEMBLE & {'REPORT (dry run)' if dry_run else 'INDEX'}")
    print(f"{'=' * 70}")

    all_docs = []
    for source in sources:
        for level in BACKFILL_LEVELS:
            cache_file = _cache_path(source, level)
            if not cache_file.exists():
                print(f"  ✗ No cache for {source} level {level}"
                      f" — run with --fetch-only first")
                continue
            docs = process_cache_file(cache_file, namespace=source)
            all_docs.extend(docs)

    if not all_docs:
        print(f"\n  No documents to index.")
        return 0

    # Summary
    print(f"\n  Total documents: {len(all_docs)}")
    level_counts = {}
    ns_counts = {}
    for d in all_docs:
        lvl = d['admin_level']
        ns = d['namespace']
        level_counts[lvl] = level_counts.get(lvl, 0) + 1
        ns_counts[ns] = ns_counts.get(ns, 0) + 1
    for ns in sorted(ns_counts):
        print(f"    {ns}: {ns_counts[ns]}")
    for lvl in sorted(level_counts):
        print(f"    admin_level={lvl}: {level_counts[lvl]}")

    # Samples
    print(f"\n  Sample boundaries:")
    for d in all_docs[:10]:
        ts = ''
        if 'timespans' in d:
            t = d['timespans'][0]
            s = t.get('start', {}).get('in', '?')
            e = t.get('end', {}).get('in', '?')
            ts = f'  [{s}→{e}]'
        print(f"    {d['boundary_id']}: {d['name']}"
              f" (level {d['admin_level']}){ts}")
    if len(all_docs) > 10:
        print(f"    ... and {len(all_docs) - 10} more")

    if dry_run:
        print(f"\n  DRY RUN — skipping indexing.")
        return len(all_docs)

    # Index
    target = es_host or ES_HOST
    if not target:
        print(f"\n  ERROR: No ES host. Use --es-host or ensure staging is running.")
        return 0

    print(f"\n  Indexing {len(all_docs)} docs into"
          f" {BOUNDARIES_INDEX} at {target} ...")
    es = Elasticsearch(
        target, request_timeout=120, max_retries=5, retry_on_timeout=True,
    )

    actions = [
        {'_index': BOUNDARIES_INDEX, '_id': doc['boundary_id'],
         '_source': doc}
        for doc in all_docs
    ]

    success_count = 0
    fail_count = 0
    for ok, info in helpers.parallel_bulk(
        es, actions, thread_count=2, raise_on_error=False,
    ):
        if ok:
            success_count += 1
        else:
            fail_count += 1
            if fail_count <= 5:
                print(f"    Bulk error: {info}")

    print(f"\n  ✓ Indexed: {success_count}")
    if fail_count:
        print(f"  ✗ Failed:  {fail_count}")

    return success_count


# ---- CLI ----

def main():
    parser = argparse.ArgumentParser(
        description="Backfill admin_level 0/1 boundaries from Overpass API",
    )
    parser.add_argument(
        '--source', choices=['osm', 'ohm', 'both'], default='ohm',
        help='Which source(s) to backfill (default: ohm)',
    )
    parser.add_argument(
        '--es-host',
        help='ES URL override (default: auto-detect from staging info)',
    )
    parser.add_argument(
        '--fetch-only', action='store_true',
        help='Only fetch from Overpass and cache — do not index',
    )
    parser.add_argument(
        '--index-only', action='store_true',
        help='Only index from cached data — do not fetch',
    )
    parser.add_argument(
        '--cache-dir',
        help='Override cache directory (default: {DATA_DIR}/boundaries/overpass_cache)',
    )
    parser.add_argument(
        '--force-fetch', action='store_true',
        help='Re-fetch from Overpass even if cache exists',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Fetch + assemble + report, but do not index',
    )
    args = parser.parse_args()

    # Allow overriding cache dir for local testing
    global CACHE_DIR
    if args.cache_dir:
        CACHE_DIR = Path(args.cache_dir)

    sources = ['osm', 'ohm'] if args.source == 'both' else [args.source]

    if not args.index_only:
        do_fetch(sources, force=args.force_fetch)

    if not args.fetch_only:
        count = do_index(sources, es_host=args.es_host, dry_run=args.dry_run)
    else:
        count = 0
        print(f"\n  --fetch-only: skipping index phase.")

    label = 'would be indexed' if args.dry_run else 'indexed'
    print(f"\n{'=' * 70}")
    print(f"DONE: {count} boundaries {label}")
    print(f"{'=' * 70}")


if __name__ == '__main__':
    main()





