#!/usr/bin/env python
# processing/ingest_all_authorities.py

"""
Master script to ingest all authority data sources into Elasticsearch.

This script coordinates the ingestion of all configured authorities in the
optimal order, considering dependencies and data volume.

Recommended order:
1. GeoNames (base gazetteer with most places)
2. Wikidata (extensive modern coverage)
3. TGN (Getty Thesaurus - art historical places)
4. Pleiades (ancient places)
5. GB1900 (British historical places)
6. UN Countries (country boundaries)
7. OSM (if available - very large)
8. Native Land (indigenous territories)
9. D-PLACE (linguistic/cultural data)
10. Index Villaris (17th century English places)
11. LOC (relations only - enriches existing data)
12. Wikidata Geoshapes (post-processing for polygons)
"""

import sys
import os
import time
import argparse
from pathlib import Path
from datetime import datetime
from elasticsearch8 import Elasticsearch

from processing.settings import ES_HOST, DATA_DIR, AUTHORITIES

es = Elasticsearch(ES_HOST)


def check_elasticsearch():
    """Verify Elasticsearch is running and indices exist."""
    try:
        info = es.info()
        print(f"✓ Elasticsearch {info['version']['number']} is running")

        # Check indices
        if not es.indices.exists(index='places'):
            print("✗ 'places' index does not exist")
            print("  Run: python -m processing.create_indices")
            return False

        if not es.indices.exists(index='toponyms'):
            print("✗ 'toponyms' index does not exist")
            print("  Run: python -m processing.create_indices")
            return False

        print("✓ Required indices exist")
        return True

    except Exception as e:
        print(f"✗ Cannot connect to Elasticsearch: {e}")
        print(f"  Make sure Elasticsearch is running on {ES_HOST}")
        return False


def check_data_files():
    """Check which authority data files are available."""
    available = {}

    for auth in AUTHORITIES:
        namespace = auth['namespace']
        auth_dir = Path(DATA_DIR) / 'authorities' / namespace

        if not auth_dir.exists():
            available[namespace] = False
            continue

        # Check if any expected files exist
        has_files = False
        for file_config in auth.get('files', []):
            if 'name' in file_config:
                filename = file_config['name']
            else:
                # Extract from URL
                url = file_config['url']
                filename = Path(url).name

            if (auth_dir / filename).exists():
                has_files = True
                file_size = (auth_dir / filename).stat().st_size
                print(f"  {namespace}: {filename} ({file_size / 1024 / 1024:.1f} MB)")
                break

        available[namespace] = has_files

    return available


def get_index_counts():
    """Get current document counts by source."""
    counts = {}

    for auth in AUTHORITIES:
        namespace = auth['namespace']
        try:
            response = es.count(
                index='places',
                body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
            )
            counts[namespace] = response['count']
        except:
            counts[namespace] = 0

    return counts


def run_ingestion(namespace, script_name, skip_if_exists=False):
    """Run a single ingestion script."""

    print(f"\n{'=' * 80}")
    print(f"INGESTING: {namespace.upper()}")
    print(f"Script: {script_name}")
    print(f"{'=' * 80}")

    # Check if already ingested
    if skip_if_exists:
        count = es.count(
            index='places',
            body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
        )['count']

        if count > 0:
            print(f"Already ingested: {count:,} places found")
            response = input("Re-ingest? (y/n): ")
            if response.lower() != 'y':
                print("Skipped")
                return True

    start_time = datetime.now()

    # Run the ingestion script
    try:
        os.system(f"python -m authorities.{script_name}")

        elapsed = (datetime.now() - start_time).seconds
        print(f"\n✓ Completed in {elapsed} seconds")

        # Get new count
        count = es.count(
            index='places',
            body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
        )['count']

        print(f"  Total {namespace.upper()} places: {count:,}")
        return True

    except Exception as e:
        print(f"\n✗ Failed: {e}")
        return False


def ingest_all(authorities_to_run=None, skip_existing=False):
    """
    Run all configured ingestions in order.

    Args:
        authorities_to_run: List of namespace codes to run (None = all)
        skip_existing: Skip authorities that already have data
    """

    # Define ingestion order and script names
    ingestion_order = [
        ('gn', 'geonames-places', 'GeoNames places'),
        ('gn', 'geonames-toponyms', 'GeoNames toponyms'),
        ('wd', 'wikidata-places', 'Wikidata places'),
        ('tgn', 'tgn-places', 'Getty TGN'),
        ('pl', 'pleiades-places', 'Pleiades ancient places'),
        ('gb', 'gb1900-places', 'GB1900 British places'),
        ('un', 'un-countries', 'UN member countries'),
        ('osm', 'osm-places', 'OpenStreetMap (if available)'),
        ('nl', 'nativeland-places', 'Native Land territories'),
        ('dp', 'dplace-places', 'D-PLACE linguistic data'),
        ('iv', 'indexvillaris-places', 'Index Villaris 1680'),
        ('loc', 'loc-relations', 'Library of Congress relations'),
        ('wd', 'wikidata-geoshapes', 'Wikidata geoshapes (post-process)'),
    ]

    # Filter if specific authorities requested
    if authorities_to_run:
        ingestion_order = [
            (ns, script, desc)
            for ns, script, desc in ingestion_order
            if ns in authorities_to_run
        ]

    print("\nPlanned ingestion order:")
    for i, (ns, script, desc) in enumerate(ingestion_order, 1):
        print(f"  {i}. {desc} ({ns})")

    # Track results
    results = {
        'successful': [],
        'failed': [],
        'skipped': []
    }

    # Run each ingestion
    for ns, script, desc in ingestion_order:
        # Check if data file exists
        auth_dir = Path(DATA_DIR) / 'authorities' / ns
        if not auth_dir.exists() or not any(auth_dir.iterdir()):
            print(f"\n⚠ Skipping {desc}: No data files found")
            print(f"  Run: python -m processing.fetch_authorities -n {ns}")
            results['skipped'].append(ns)
            continue

        # Special handling for certain authorities
        if ns == 'osm':
            # OSM is huge, confirm before running
            print(f"\nWARNING: OSM ingestion can take many hours")
            response = input("Run OSM ingestion? (y/n): ")
            if response.lower() != 'y':
                results['skipped'].append(ns)
                continue

        if ns == 'loc':
            # LOC only creates relations
            print(f"\nNOTE: LOC creates relations only, not new places")

        # Run ingestion
        success = run_ingestion(ns, script, skip_if_exists=skip_existing)

        if success:
            results['successful'].append(ns)
        else:
            results['failed'].append(ns)

        # Brief pause between ingestions
        time.sleep(2)

    # Final summary
    print(f"\n{'=' * 80}")
    print("INGESTION SUMMARY")
    print(f"{'=' * 80}")

    print(f"\n✓ Successful: {', '.join(results['successful']) or 'None'}")
    print(f"⚠ Skipped: {', '.join(results['skipped']) or 'None'}")
    print(f"✗ Failed: {', '.join(results['failed']) or 'None'}")

    # Get final counts
    print("\nFinal document counts by source:")
    counts = get_index_counts()

    total = 0
    for ns in sorted(counts.keys()):
        if counts[ns] > 0:
            print(f"  {ns:8} {counts[ns]:>12,}")
            total += counts[ns]

    print(f"  {'Total:':8} {total:>12,}")

    # Get total index counts
    places_total = es.count(index='places')['count']
    toponyms_total = es.count(index='toponyms')['count']

    print(f"\nTotal places in index: {places_total:,}")
    print(f"Total toponyms in index: {toponyms_total:,}")


def main():
    parser = argparse.ArgumentParser(
        description='Ingest all authority data sources into Elasticsearch'
    )
    parser.add_argument(
        '-n', '--namespaces',
        help='Comma-separated list of namespaces to ingest (default: all)'
    )
    parser.add_argument(
        '--skip-existing',
        action='store_true',
        help='Skip authorities that already have data in the index'
    )
    parser.add_argument(
        '--check-only',
        action='store_true',
        help='Only check data availability, don\'t run ingestion'
    )
    parser.add_argument(
        '--prepare-production',
        action='store_true',
        help='Run production preparation after ingestion'
    )

    args = parser.parse_args()

    print("=" * 80)
    print("AUTHORITY DATA INGESTION COORDINATOR")
    print("=" * 80)

    # Check Elasticsearch
    if not check_elasticsearch():
        sys.exit(1)

    # Check available data files
    print("\nChecking available data files:")
    available = check_data_files()

    print("\nData availability:")
    for ns in sorted(available.keys()):
        status = "✓ Available" if available[ns] else "✗ Not downloaded"
        print(f"  {ns:8} {status}")

    # Show current counts
    print("\nCurrent index counts:")
    counts = get_index_counts()
    for ns in sorted(counts.keys()):
        if counts[ns] > 0:
            print(f"  {ns:8} {counts[ns]:>12,} places")

    if args.check_only:
        print("\nCheck complete (--check-only specified)")
        return

    # Parse namespaces
    if args.namespaces:
        namespaces = [ns.strip() for ns in args.namespaces.split(',')]
        print(f"\nWill ingest: {', '.join(namespaces)}")
    else:
        namespaces = None
        print("\nWill ingest all available authorities")

    # Run ingestion
    ingest_all(namespaces, skip_existing=args.skip_existing)

    # Optionally prepare for production
    if args.prepare_production:
        print("\nPreparing indices for production...")
        os.system("python -m processing.prepare_for_production")


if __name__ == "__main__":
    main()