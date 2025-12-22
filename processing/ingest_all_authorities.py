#!/usr/bin/env python
# processing/ingest_all_authorities.py

"""
Master script to ingest all authority data sources into Elasticsearch.

This script coordinates the ingestion of all configured authorities in the
optimal order, considering dependencies and data volume.

When you specify `-n gn`, it runs BOTH geonames-places AND geonames-toponyms.
When you specify `-n gn,wd`, it runs all GeoNames scripts then all Wikidata scripts.

21 Dec 2025 Final document counts by source:

  dp              2,599
  gb          1,174,449
  gn         13,378,039
  iv             24,000
  nl              4,343
  osm        18,113,756
  pl             34,085
  tgn         2,972,410
  un                257
  wd         11,456,496

  Total:     47,160,434

Unique Toponyms: 74,417,599 indexed in 1 day, 0:45:53
"""

import subprocess
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime
from elasticsearch import Elasticsearch, helpers

from processing.settings import ES_HOST, DATA_DIR, AUTHORITIES, PLACES_INDEX, TOPONYMS_INDEX

es = Elasticsearch(ES_HOST)


def delete_existing_namespace(namespace):
    """
    Delete all places for a given authority namespace.
    """
    print(f"\nDeleting existing data for namespace '{namespace}'")
    sys.stdout.flush()

    query = {
        "query": {
            "prefix": {
                "place_id": f"{namespace}:"
            }
        }
    }

    resp = es.options(request_timeout=3600).delete_by_query(
        index=PLACES_INDEX,
        body=query,
        conflicts="proceed",
        refresh=True,
        slices="auto",
        wait_for_completion=True
    )

    deleted = resp.get("deleted", 0)
    print(f"  Deleted {deleted:,} places")
    sys.stdout.flush()
    return deleted


def deduplicate_and_index_toponyms():
    """
    Extract unique toponym_ids from places.toponyms (nested)
    and index them into the toponyms index without overwriting existing enriched documents.
    """
    from datetime import timedelta

    print("\n" + "=" * 80)
    print("DEDUPLICATING AND INDEXING TOPONYMS (SAFE MODE)")
    print("=" * 80)
    sys.stdout.flush()

    start_time = datetime.now()
    indexed_created = 0
    batch = []
    BATCH_SIZE = 10000

    # First, get an estimate of total unique toponyms for ETA calculation
    print("Estimating total unique toponyms...")
    count_query = {
        "size": 0,
        "aggs": {
            "toponyms_nested": {
                "nested": {"path": "toponyms"},
                "aggs": {
                    "unique_count": {
                        "cardinality": {
                            "field": "toponyms.toponym_id",
                            "precision_threshold": 40000
                        }
                    }
                }
            }
        }
    }
    count_resp = es.search(index=PLACES_INDEX, body=count_query, request_timeout=300)
    estimated_total = count_resp["aggregations"]["toponyms_nested"]["unique_count"]["value"]
    estimated_pages = max(1, int(estimated_total / 10000))
    print(f"Estimated unique toponyms: ~{estimated_total:,}")
    print(f"Estimated pages: ~{estimated_pages:,}\n")
    sys.stdout.flush()

    query = {
        "size": 0,
        "aggs": {
            "toponyms_nested": {
                "nested": {"path": "toponyms"},
                "aggs": {
                    "unique_toponyms": {
                        "composite": {
                            "size": 10000,
                            "sources": [
                                {"toponym": {"terms": {"field": "toponyms.toponym_id"}}}
                            ]
                        }
                    }
                }
            }
        }
    }

    after_key = None
    page = 0

    try:
        while True:
            page += 1
            if after_key:
                query["aggs"]["toponyms_nested"]["aggs"]["unique_toponyms"]["composite"]["after"] = after_key

            resp = es.search(index=PLACES_INDEX, body=query, request_timeout=300)
            agg = resp["aggregations"]["toponyms_nested"]["unique_toponyms"]
            buckets = agg["buckets"]

            if not buckets:
                break

            # Calculate progress and ETA
            percent = (page / estimated_pages) * 100 if estimated_pages > 0 else 0
            elapsed = (datetime.now() - start_time).total_seconds()
            rate = page / elapsed if elapsed > 0 else 0

            if rate > 0 and page < estimated_pages:
                remaining_pages = estimated_pages - page
                eta_seconds = int(remaining_pages / rate)
                eta_str = str(timedelta(seconds=eta_seconds))
            else:
                eta_str = "--:--:--"

            print(f"\r  Page {page:,}/{estimated_pages:,} ({percent:.1f}%) | "
                  f"{len(buckets):,} toponyms | {rate:.2f} pages/s | ETA: {eta_str}",
                  end='', flush=True)
            sys.stdout.flush()

            for bucket in buckets:
                toponym_id = bucket["key"]["toponym"]
                batch.append({
                    "_op_type": "create",
                    "_index": TOPONYMS_INDEX,
                    "_id": toponym_id,
                    "_source": {"toponym_id": toponym_id}
                })

                if len(batch) >= BATCH_SIZE:
                    success, _ = helpers.bulk(
                        es, batch, raise_on_error=False, raise_on_exception=False, stats_only=True
                    )
                    indexed_created += success
                    batch.clear()

            after_key = agg.get("after_key")
            if not after_key:
                break

        if batch:
            success, _ = helpers.bulk(
                es, batch, raise_on_error=False, raise_on_exception=False, stats_only=True
            )
            indexed_created += success
            batch.clear()

    except Exception as e:
        print("\nERROR during toponym deduplication:")
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        return False

    es.indices.refresh(index=TOPONYMS_INDEX)
    elapsed = datetime.now() - start_time

    print("\n✓ TOPONYM DEDUPLICATION COMPLETE")
    print(f"  Newly created toponyms: {indexed_created:,}")
    print(f"  Time elapsed: {str(elapsed).split('.')[0]}")
    final_count = es.count(index=TOPONYMS_INDEX)["count"]
    print(f"  Total toponyms in index: {final_count:,}")
    sys.stdout.flush()
    return True


def check_elasticsearch():
    try:
        info = es.info()
        print(f"✓ Elasticsearch {info['version']['number']} is running")
        if not es.indices.exists(index=PLACES_INDEX):
            print(f"✗ '{PLACES_INDEX}' index does not exist")
            return False
        if not es.indices.exists(index=TOPONYMS_INDEX):
            print(f"✗ '{TOPONYMS_INDEX}' index does not exist")
            return False
        print("✓ Required indices exist")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"✗ Cannot connect to Elasticsearch: {e}")
        sys.stdout.flush()
        return False


def check_data_files():
    available = {}
    for auth in AUTHORITIES:
        namespace = auth['namespace']
        auth_dir = Path(DATA_DIR) / 'authorities' / namespace

        sys.stdout.write(f"  Checking {namespace}...")
        sys.stdout.flush()

        if not auth_dir.exists():
            available[namespace] = False
            print(" directory not found")
            continue

        try:
            # Check if any configured files exist
            files = auth.get('files', [])
            if not files:
                available[namespace] = False
                print(" no files configured")
                continue

            has_files = False
            for file_config in files:
                filename = file_config.get('name') or Path(file_config['url']).name
                filepath = auth_dir / filename
                if filepath.exists():
                    has_files = True
                    break

            available[namespace] = has_files
            print(" OK" if has_files else " files not found")

        except Exception as e:
            print(f" ERROR: {e}")
            available[namespace] = False

    sys.stdout.flush()
    return available


def get_index_counts():
    counts = {}
    total_count = 0

    for auth in AUTHORITIES:
        namespace = auth['namespace']
        dataset_name = auth['dataset_name']
        sys.stdout.write(f"  Counting '{dataset_name}'...")
        sys.stdout.flush()
        try:
            response = es.options(request_timeout=30).count(
                index=PLACES_INDEX,
                body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
            )
            count = response['count']
            counts[namespace] = count
            total_count += count

            print(f" {count:,}")
            sys.stdout.flush()
        except Exception as e:
            print(f" ERROR: {e}")
            sys.stdout.flush()
            counts[namespace] = 0

    print()
    print(f"  TOTAL PLACES: {total_count:,}")
    print()
    sys.stdout.flush()

    return counts


def run_ingestion(namespace, script_name, skip_existing=True, replace_existing=False):
    """
    Run a single ingestion script, handling skip/replace silently.
    """
    print(f"\n{'=' * 80}")
    print(f"INGESTING: {namespace.upper()} ({script_name})")
    print(f"{'=' * 80}")
    sys.stdout.flush()

    # For update scripts (toponyms, geoshapes, relations), always run
    update_scripts = ['geonames-toponyms', 'wikidata-geoshapes', 'loc-relations']
    is_update_script = script_name in update_scripts

    if not is_update_script:
        count = es.options(request_timeout=30).count(
            index=PLACES_INDEX,
            body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
        )['count']

        if count > 0:
            if replace_existing:
                delete_existing_namespace(namespace)
            elif skip_existing:
                print(f"Skipping {namespace}: {count:,} places already exist")
                sys.stdout.flush()
                return True

    start_time = datetime.now()
    try:
        cmd = [sys.executable, "-u", "-m", f"authorities.{script_name}"]
        subprocess.run(cmd, check=True, stdout=sys.stdout, stderr=sys.stderr)
        es.indices.refresh(index=f"{PLACES_INDEX},{TOPONYMS_INDEX}")
        elapsed = datetime.now() - start_time
        print(f"\n✓ Completed in {str(elapsed).split('.')[0]}")
        sys.stdout.flush()

        if not is_update_script:
            count = es.options(request_timeout=30).count(
                index=PLACES_INDEX,
                body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
            )['count']
            print(f"  Total {namespace.upper()} places: {count:,}")
            sys.stdout.flush()

        return True
    except subprocess.CalledProcessError as e:
        print(f"\n✗ Script failed with exit code {e.returncode}")
        sys.stdout.flush()
        return False
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        sys.stdout.flush()
        return False


def ingest_all(authorities_to_run=None, skip_existing=True, replace_existing=False, delete_only=False):
    """
    Run all configured ingestions in order.

    If authorities_to_run is specified, it should be namespace codes.
    All scripts for that namespace will be included in order.
    """
    # Full ingestion order with script IDs for tracking
    # Format: (namespace, script_name, description, script_id)
    ingestion_order = [
        ('osm', 'osm-places', 'OpenStreetMap', 'osm-places'),  # 18,113,756 4:04:14
        ('gn', 'geonames-places', 'GeoNames places', 'gn-places'),  # 13,378,039 0:21:43
        ('gn', 'geonames-toponyms', 'GeoNames toponyms (updates places)', 'gn-toponyms'),  # Places updated: 7,600,036; Relations added: 1,820,560; 0:35:40
        ('wd', 'wikidata-places', 'Wikidata places', 'wd-places'),  # 11,456,496 2:52:55
        ('tgn', 'tgn-places', 'Getty TGN', 'tgn-places'),  # 2,972,410 0:05:57
        ('pl', 'pleiades-places', 'Pleiades ancient places', 'pl-places'),  # 34,085 0:01:15
        ('un', 'un-countries', 'UN member countries', 'un-countries'),  # 257 0:00:45
        ('dp', 'dplace-places', 'D-PLACE linguistic data', 'dp-places'),  # 2,599 0:00:05
        ('nl', 'nativeland-places', 'Native Land territories', 'nl-places'),  # 4,343 0:00:09
        ('gb', 'gb1900-places', 'GB1900 British places', 'gb-places'),  # 1,174,449 0:02:02
        ('iv', 'indexvillaris-places', 'Index Villaris 1680', 'iv-places'),  # 24,000 0:00:10
        ('loc', 'loc-relations', 'Library of Congress relations (updates places)', 'loc-relations'),  # Places updated: 3,335 0:03:50
        ('wd', 'wikidata-geoshapes', 'Wikidata geoshapes (updates places)', 'wd-geoshapes'),  # Places updated: 58,681 0:00:22
    ]

    # Filter by requested namespaces (includes ALL scripts for that namespace)
    if authorities_to_run:
        ingestion_order = [
            (ns, script, desc, script_id)
            for ns, script, desc, script_id in ingestion_order
            if ns in authorities_to_run
        ]

    print("\nWill operate on: " + (', '.join(authorities_to_run) if authorities_to_run else "all available authorities") + " (including all scripts for each)")
    print("\nPlanned ingestion order:")
    for i, (ns, script, desc, script_id) in enumerate(ingestion_order, 1):
        print(f"  {i}. {desc} ({script_id})")
    sys.stdout.flush()

    if not ingestion_order:
        print("\nNo authorities to process!")
        sys.stdout.flush()
        return

    results = {'successful': [], 'failed': [], 'skipped': []}

    for ns, script, desc, script_id in ingestion_order:
        auth_dir = Path(DATA_DIR) / 'authorities' / ns

        # Skip if no data files found (only check for the first script of each namespace)
        if script_id.endswith('-places') or script_id == 'loc-relations':
            if not auth_dir.exists() or not any(auth_dir.iterdir()):
                print(f"\n⚠ Skipping {ns}: No data files found")
                sys.stdout.flush()
                if ns not in results['skipped']:
                    results['skipped'].append(ns)
                continue

        if delete_only:
            # Only delete for the first script of each namespace
            if script_id.endswith('-places') or script_id == 'loc-relations':
                delete_existing_namespace(ns)
                if ns not in results['successful']:
                    results['successful'].append(ns)
            continue

        if ns == 'loc':
            print(f"\nNOTE: LOC creates relations only, not new places")
            sys.stdout.flush()

        success = run_ingestion(ns, script, skip_existing=skip_existing, replace_existing=replace_existing)

        if success:
            if ns not in results['successful']:
                results['successful'].append(ns)
        else:
            if ns not in results['failed']:
                results['failed'].append(ns)
            # Stop processing further scripts for this namespace if one fails
            print(f"Stopping further {ns} scripts due to failure")
            sys.stdout.flush()
            break

        time.sleep(2)

    print(f"\n{'=' * 80}")
    print("INGESTION SUMMARY")
    print(f"{'=' * 80}")

    print(f"\n✓ Successful: {', '.join(results['successful']) or 'None'}")
    print(f"⚠ Skipped: {', '.join(results['skipped']) or 'None'}")
    print(f"✗ Failed: {', '.join(results['failed']) or 'None'}")
    sys.stdout.flush()

    counts = get_index_counts()
    print("\nFinal document counts by source:")
    total = 0
    for ns in sorted(counts.keys()):
        if counts[ns] > 0:
            print(f"  {ns:8} {counts[ns]:>12,}")
            total += counts[ns]
    print(f"  {'Total:':8} {total:>12,}")

    print(f"\nTotal places in index: {es.count(index=PLACES_INDEX)['count']:,}")
    print(f"Total toponyms in index: {es.count(index=TOPONYMS_INDEX)['count']:,}")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description='Ingest all authority data sources into Elasticsearch')
    parser.add_argument('-n', '--namespaces',
                        help='Comma-separated list of namespaces to ingest/delete (runs ALL scripts for each namespace in order)')
    parser.add_argument('--check-only', action='store_true', help='Only check data availability, don\'t run ingestion')
    parser.add_argument('--skip-counts', action='store_true', help='Skip counting existing documents (faster startup)')
    parser.add_argument('--prepare-production', action='store_true', help='Run production preparation after ingestion')
    parser.add_argument('-d', '--delete-only', action='store_true',
                        help='Delete specified authorities without ingesting')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--skip-existing', action='store_true', help='Skip authorities that already have data (default)')
    group.add_argument('-r', '--replace-existing', action='store_true', help='Delete existing data before re-ingesting')
    args = parser.parse_args()

    skip_existing = not args.replace_existing

    print("=" * 80)
    print("AUTHORITY DATA INGESTION COORDINATOR")
    print("=" * 80)
    sys.stdout.flush()

    if not check_elasticsearch():
        sys.exit(1)

    print("\nChecking available data files:")
    sys.stdout.flush()
    available = check_data_files()

    if not args.skip_counts:
        print("\nCurrent index counts:")
        sys.stdout.flush()
        counts = get_index_counts()
    else:
        print("\nSkipping index counts (--skip-counts specified)")
        sys.stdout.flush()
        counts = {}

    if args.check_only:
        print("\nCheck complete (--check-only specified)")
        sys.stdout.flush()
        return

    namespaces = [ns.strip() for ns in args.namespaces.split(',')] if args.namespaces else None

    ingest_all(namespaces, skip_existing=skip_existing,
               replace_existing=args.replace_existing, delete_only=args.delete_only)

    if not args.delete_only and not namespaces:
        # Only run deduplication if processing all authorities
        deduplicate_and_index_toponyms()
    elif not args.delete_only:
        print("\nNote: Skipping toponyms deduplication (only runs when processing all authorities)")
        print("      Run without -n flag to deduplicate toponyms across all authorities")
        sys.stdout.flush()


if __name__ == "__main__":
    main()