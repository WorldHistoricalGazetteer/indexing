#!/usr/bin/env python
# processing/ingest_all_authorities.py

"""
Master script to ingest all authority data sources into Elasticsearch.

This script coordinates the ingestion of all configured authorities in the
optimal order, considering dependencies and data volume.

When you specify `-n gn`, it runs BOTH geonames-places AND geonames-toponyms.
When you specify `-n gn,wd`, it runs all GeoNames scripts then all Wikidata scripts.

Recommended order:
1. GeoNames places + toponyms (base gazetteer)
2. Wikidata places + geoshapes (extensive modern coverage)
3. Other authorities (can run in parallel if desired)
4. LOC relations (enriches existing data)
5. Toponyms deduplication (final step)
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

    query = {
        "query": {
            "prefix": {
                "place_id": f"{namespace}:"
            }
        }
    }

    resp = es.delete_by_query(
        index=PLACES_INDEX,
        body=query,
        conflicts="proceed",
        refresh=True,
        slices="auto",
        wait_for_completion=True,
        request_timeout=3600  # 1 hour timeout for large deletes
    )

    deleted = resp.get("deleted", 0)
    print(f"  Deleted {deleted:,} places")
    return deleted


def deduplicate_and_index_toponyms():
    """
    Extract unique toponym_ids from places.toponyms (nested)
    and index them into the toponyms index without overwriting existing enriched documents.
    """

    print("\n" + "=" * 80)
    print("DEDUPLICATING AND INDEXING TOPONYMS (SAFE MODE)")
    print("=" * 80)

    start_time = datetime.now()
    indexed_created = 0
    batch = []
    BATCH_SIZE = 10000

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

            print(f"  Page {page}: {len(buckets):,} unique toponyms")

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
        print("ERROR during toponym deduplication:")
        import traceback
        traceback.print_exc()
        return False

    es.indices.refresh(index=TOPONYMS_INDEX)
    elapsed = datetime.now() - start_time

    print("\n✓ TOPONYM DEDUPLICATION COMPLETE")
    print(f"  Newly created toponyms: {indexed_created:,}")
    print(f"  Time elapsed: {str(elapsed).split('.')[0]}")
    final_count = es.count(index=TOPONYMS_INDEX)["count"]
    print(f"  Total toponyms in index: {final_count:,}")
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
        return True
    except Exception as e:
        print(f"✗ Cannot connect to Elasticsearch: {e}")
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

    return available


def get_index_counts():
    counts = {}
    for auth in AUTHORITIES:
        namespace = auth['namespace']
        sys.stdout.write(f"  Counting {namespace}...")
        sys.stdout.flush()
        try:
            response = es.options(request_timeout=30).count(
                index=PLACES_INDEX,
                body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
            )
            counts[namespace] = response['count']
            print(f" {response['count']:,}")
        except Exception as e:
            print(f" ERROR: {e}")
            counts[namespace] = 0
    return counts


def run_ingestion(namespace, script_name, skip_existing=True, replace_existing=False):
    """
    Run a single ingestion script, handling skip/replace silently.
    """
    print(f"\n{'=' * 80}")
    print(f"INGESTING: {namespace.upper()} ({script_name})")
    print(f"{'=' * 80}")

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
                return True

    start_time = datetime.now()
    try:
        cmd = [sys.executable, "-u", "-m", f"authorities.{script_name}"]
        subprocess.run(cmd, check=True)
        es.indices.refresh(index=f"{PLACES_INDEX},{TOPONYMS_INDEX}")
        elapsed = datetime.now() - start_time
        print(f"\n✓ Completed in {str(elapsed).split('.')[0]}")

        if not is_update_script:
            count = es.options(request_timeout=30).count(
                index=PLACES_INDEX,
                body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
            )['count']
            print(f"  Total {namespace.upper()} places: {count:,}")

        return True
    except subprocess.CalledProcessError as e:
        print(f"\n✗ Script failed with exit code {e.returncode}")
        return False
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
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
        ('gn', 'geonames-places', 'GeoNames places', 'gn-places'),
        ('gn', 'geonames-toponyms', 'GeoNames toponyms (updates places)', 'gn-toponyms'),
        ('wd', 'wikidata-places', 'Wikidata places', 'wd-places'),
        ('wd', 'wikidata-geoshapes', 'Wikidata geoshapes (updates places)', 'wd-geoshapes'),
        ('osm', 'osm-places', 'OpenStreetMap', 'osm-places'),
        ('tgn', 'tgn-places', 'Getty TGN', 'tgn-places'),
        ('pl', 'pleiades-places', 'Pleiades ancient places', 'pl-places'),
        ('gb', 'gb1900-places', 'GB1900 British places', 'gb-places'),
        ('un', 'un-countries', 'UN member countries', 'un-countries'),
        ('nl', 'nativeland-places', 'Native Land territories', 'nl-places'),
        ('dp', 'dplace-places', 'D-PLACE linguistic data', 'dp-places'),
        ('iv', 'indexvillaris-places', 'Index Villaris 1680', 'iv-places'),
        ('loc', 'loc-relations', 'Library of Congress relations (updates places)', 'loc-relations'),
    ]

    # Filter by requested namespaces (includes ALL scripts for that namespace)
    if authorities_to_run:
        ingestion_order = [
            (ns, script, desc, script_id)
            for ns, script, desc, script_id in ingestion_order
            if ns in authorities_to_run
        ]

    print("\nPlanned ingestion order:")
    for i, (ns, script, desc, script_id) in enumerate(ingestion_order, 1):
        print(f"  {i}. {desc} ({script_id})")

    if not ingestion_order:
        print("\nNo authorities to process!")
        return

    results = {'successful': [], 'failed': [], 'skipped': []}

    for ns, script, desc, script_id in ingestion_order:
        auth_dir = Path(DATA_DIR) / 'authorities' / ns

        # Skip if no data files found (only check for the first script of each namespace)
        if script_id.endswith('-places') or script_id == 'loc-relations':
            if not auth_dir.exists() or not any(auth_dir.iterdir()):
                print(f"\n⚠ Skipping {ns}: No data files found")
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

        success = run_ingestion(ns, script, skip_existing=skip_existing, replace_existing=replace_existing)

        if success:
            if ns not in results['successful']:
                results['successful'].append(ns)
        else:
            if ns not in results['failed']:
                results['failed'].append(ns)
            # Stop processing further scripts for this namespace if one fails
            print(f"Stopping further {ns} scripts due to failure")
            break

        time.sleep(2)

    print(f"\n{'=' * 80}")
    print("INGESTION SUMMARY")
    print(f"{'=' * 80}")

    print(f"\n✓ Successful: {', '.join(results['successful']) or 'None'}")
    print(f"⚠ Skipped: {', '.join(results['skipped']) or 'None'}")
    print(f"✗ Failed: {', '.join(results['failed']) or 'None'}")

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

    if not check_elasticsearch():
        sys.exit(1)

    print("\nChecking available data files:")
    available = check_data_files()

    if not args.skip_counts:
        print("\nCurrent index counts:")
        counts = get_index_counts()
    else:
        print("\nSkipping index counts (--skip-counts specified)")
        counts = {}

    if args.check_only:
        print("\nCheck complete (--check-only specified)")
        return

    namespaces = [ns.strip() for ns in args.namespaces.split(',')] if args.namespaces else None
    if namespaces:
        print(f"\nWill operate on: {', '.join(namespaces)} (including all scripts for each)")
    else:
        print("\nWill operate on all available authorities")

    ingest_all(namespaces, skip_existing=skip_existing,
               replace_existing=args.replace_existing, delete_only=args.delete_only)

    if not args.delete_only and not namespaces:
        # Only run deduplication if processing all authorities
        deduplicate_and_index_toponyms()
    elif not args.delete_only:
        print("\nNote: Skipping toponyms deduplication (only runs when processing all authorities)")
        print("      Run without -n flag to deduplicate toponyms across all authorities")


if __name__ == "__main__":
    main()