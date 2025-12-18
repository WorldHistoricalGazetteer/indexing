# processing/loc-relations.py

"""
Process Library of Congress (LOC) geographic authority data.

LOC provides authority records for geographic names, but these are primarily
useful for creating relations/links to other gazetteers rather than as a
source of place data itself.

The LOC data includes:
- Links to GeoNames
- Links to Wikidata
- Links to VIAF (Virtual International Authority File)
- Historical name variants
- Administrative hierarchies

This script primarily creates relations in existing places rather than
creating new place records.

No changes needed for temporal scoping design - LOC only updates relations.
"""

import json
import gzip
import os
import sys
from pathlib import Path
from datetime import datetime

from elasticsearch import Elasticsearch
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE, AUTHORITIES
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)

# Get LOC configuration
LOC_CONFIG = next((auth for auth in AUTHORITIES if auth['namespace'] == 'loc'), None)
if not LOC_CONFIG:
    print("ERROR: LOC configuration not found in AUTHORITIES")
    sys.exit(1)


def extract_loc_geographic_data(record):
    """
    Extract geographic information from LOC MADS/RDF record.

    LOC records are complex JSON-LD with nested graph structure.
    We're looking for GeographicElement entries with external links.
    """

    # Handle different record formats
    if '@graph' in record:
        graph_items = record['@graph']
    elif isinstance(record, list):
        graph_items = record
    else:
        graph_items = [record]

    results = []

    for item in graph_items:
        # Skip non-geographic items
        if not isinstance(item, dict):
            continue

        item_types = item.get('@type', [])
        if isinstance(item_types, str):
            item_types = [item_types]

        # Check if this is a geographic element
        if not any('GeographicElement' in t or 'Geographic' in t for t in item_types):
            continue

        # Extract LOC identifier
        loc_id = item.get('@id', '')
        if not loc_id:
            continue

        # Clean up LOC ID
        if loc_id.startswith('http://id.loc.gov/'):
            loc_id = loc_id.replace('http://id.loc.gov/', '')

        # Extract label/name
        label = None
        if 'madsrdf:authoritativeLabel' in item:
            label_obj = item['madsrdf:authoritativeLabel']
            if isinstance(label_obj, dict):
                label = label_obj.get('@value', '')
            elif isinstance(label_obj, str):
                label = label_obj

        if not label and 'rdfs:label' in item:
            label = item['rdfs:label']
            if isinstance(label, dict):
                label = label.get('@value', '')

        if not label:
            continue

        # Extract external links
        external_links = []

        # GeoNames links
        for field in ['madsrdf:hasExactExternalAuthority',
                      'madsrdf:hasCloseExternalAuthority',
                      'madsrdf:identifiesRWO']:
            if field not in item:
                continue

            values = item[field]
            if not isinstance(values, list):
                values = [values]

            for value in values:
                if isinstance(value, dict) and '@id' in value:
                    uri = value['@id']
                elif isinstance(value, str):
                    uri = value
                else:
                    continue

                # Parse different URI types
                if 'geonames.org' in uri:
                    # Extract GeoNames ID
                    gn_id = uri.rstrip('/').split('/')[-1]
                    if gn_id.isdigit():
                        external_links.append({
                            'type': 'geonames',
                            'id': f"gn:{gn_id}",
                            'uri': uri,
                            'relation': 'sameAs' if 'Exact' in field else 'closeMatch'
                        })

                elif 'wikidata.org' in uri:
                    # Extract Wikidata QID
                    qid = uri.rstrip('/').split('/')[-1]
                    if qid.startswith('Q'):
                        external_links.append({
                            'type': 'wikidata',
                            'id': f"wd:{qid}",
                            'uri': uri,
                            'relation': 'sameAs' if 'Exact' in field else 'closeMatch'
                        })

                elif 'viaf.org' in uri:
                    # Extract VIAF ID
                    viaf_id = uri.rstrip('/').split('/')[-1]
                    if viaf_id.isdigit():
                        external_links.append({
                            'type': 'viaf',
                            'id': f"viaf:{viaf_id}",
                            'uri': uri,
                            'relation': 'sameAs' if 'Exact' in field else 'closeMatch'
                        })

        # Extract variant names
        variants = []
        if 'madsrdf:hasVariant' in item:
            variant_items = item['madsrdf:hasVariant']
            if not isinstance(variant_items, list):
                variant_items = [variant_items]

            for variant in variant_items:
                if isinstance(variant, dict):
                    for var_item in graph_items:
                        if isinstance(var_item, dict) and var_item.get('@id') == variant.get('@id'):
                            if 'madsrdf:variantLabel' in var_item:
                                var_label = var_item['madsrdf:variantLabel']
                                if isinstance(var_label, dict):
                                    var_label = var_label.get('@value', '')
                                if var_label:
                                    variants.append(var_label)

        # Extract broader/narrower relationships (hierarchies)
        hierarchies = []
        if 'madsrdf:hasBroaderAuthority' in item:
            broader = item['madsrdf:hasBroaderAuthority']
            if isinstance(broader, dict) and '@id' in broader:
                hierarchies.append({
                    'relation': 'partOf',
                    'uri': broader['@id']
                })

        if external_links:
            results.append({
                'loc_id': loc_id,
                'label': label,
                'external_links': external_links,
                'variants': variants,
                'hierarchies': hierarchies
            })

    return results


def update_place_relations(external_links, loc_id, label):
    """
    Update existing places with LOC relations.

    Returns: number of places updated
    """

    updated = 0

    for link in external_links:
        place_id = link['id']

        # Check if place exists
        try:
            if not es.exists(index='places', id=place_id):
                continue
        except:
            continue

        # Create LOC relation
        loc_relation = {
            'relationType': 'hasAuthority',
            'relationTo': f"loc:{loc_id}",
            'label': f"LOC: {label}",
            'source': 'loc',
            'method': 'authority',
            'certainty': 1.0 if link['relation'] == 'sameAs' else 0.9
        }

        # Update place with LOC relation
        try:
            es.update(
                index='places',
                id=place_id,
                body={
                    'script': {
                        'source': """
                            if (ctx._source.relations == null) {
                                ctx._source.relations = [];
                            }
                            // Check if LOC relation already exists
                            boolean exists = false;
                            for (rel in ctx._source.relations) {
                                if (rel.relationTo == params.relation.relationTo) {
                                    exists = true;
                                    break;
                                }
                            }
                            if (!exists) {
                                ctx._source.relations.add(params.relation);
                            }
                        """,
                        'params': {
                            'relation': loc_relation
                        }
                    }
                }
            )
            updated += 1

        except Exception as e:
            # Place might not exist or update might fail
            continue

    # Also create reverse relations from LOC ID to external IDs
    for link in external_links:
        place_id = link['id']

        # Create relation from external place to LOC
        reverse_relation = {
            'relationType': link['relation'],
            'relationTo': place_id,
            'source': 'loc',
            'method': 'authority',
            'certainty': 1.0 if link['relation'] == 'sameAs' else 0.9
        }

        try:
            es.update(
                index='places',
                id=place_id,
                body={
                    'script': {
                        'source': """
                            if (ctx._source.relations == null) {
                                ctx._source.relations = [];
                            }
                            // Add reverse relation if not exists
                            boolean exists = false;
                            for (rel in ctx._source.relations) {
                                if (rel.relationTo == params.relation.relationTo && 
                                    rel.relationType == params.relation.relationType) {
                                    exists = true;
                                    break;
                                }
                            }
                            if (!exists) {
                                ctx._source.relations.add(params.relation);
                            }
                        """,
                        'params': {
                            'relation': reverse_relation
                        }
                    }
                }
            )
        except:
            continue

    return updated


def index_loc_file(ndjson_file):
    """
    Process LOC NDJSON file and create relations in existing places.
    """

    print(f"Processing LOC file: {ndjson_file}")

    # Check file exists
    if not os.path.exists(ndjson_file):
        # Try standard location
        standard_path = Path(DATA_DIR) / 'authorities' / 'loc' / Path(ndjson_file).name
        if standard_path.exists():
            ndjson_file = standard_path
        else:
            print(f"ERROR: File not found: {ndjson_file}")
            print("\nTo download LOC data, run:")
            print("  python -m processing.fetch_authorities -n loc")
            return

    total_records = 0
    geographic_records = 0
    relations_created = 0
    places_updated = 0
    errors = 0

    print(f"Reading LOC authority data...")
    print("Note: This creates relations, not new places")

    start_time = datetime.now()

    # Determine if file is gzipped
    if ndjson_file.endswith('.gz'):
        file_opener = gzip.open
        mode = 'rt'
    else:
        file_opener = open
        mode = 'r'

    try:
        with file_opener(ndjson_file, mode, encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue

                total_records += 1

                if total_records % 10000 == 0:
                    elapsed = (datetime.now() - start_time).seconds
                    rate = total_records / elapsed if elapsed > 0 else 0
                    print(f"  Processed {total_records:,} records "
                          f"({rate:.1f}/sec) - "
                          f"geographic: {geographic_records:,}, "
                          f"relations: {relations_created:,}")

                try:
                    # Parse JSON-LD record
                    record = json.loads(line)

                    # Extract geographic data
                    geo_items = extract_loc_geographic_data(record)

                    if not geo_items:
                        continue

                    geographic_records += len(geo_items)

                    # Process each geographic item
                    for geo_item in geo_items:
                        if not geo_item['external_links']:
                            continue

                        # Update places with LOC relations
                        updated = update_place_relations(
                            geo_item['external_links'],
                            geo_item['loc_id'],
                            geo_item['label']
                        )

                        if updated > 0:
                            places_updated += updated
                            relations_created += len(geo_item['external_links'])

                except json.JSONDecodeError as e:
                    if total_records < 10:
                        print(f"  JSON error in line {line_num}: {e}")
                    errors += 1
                    continue
                except Exception as e:
                    if errors < 10:
                        print(f"  Error processing line {line_num}: {e}")
                    errors += 1
                    continue

    except Exception as e:
        print(f"ERROR reading file: {e}")
        return

    elapsed = (datetime.now() - start_time).seconds

    print(f"\n{'=' * 80}")
    print(f"LOC PROCESSING COMPLETE")
    print(f"{'=' * 80}")
    print(f"Time elapsed: {elapsed} seconds")
    print(f"Total records processed: {total_records:,}")
    print(f"Geographic records found: {geographic_records:,}")
    print(f"Relations created: {relations_created:,}")
    print(f"Places updated: {places_updated:,}")
    print(f"Errors: {errors:,}")

    # Show sample of updated places
    if places_updated > 0:
        print("\nSample of places with new LOC relations:")

        sample = es.search(
            index='places',
            body={
                'query': {
                    'nested': {
                        'path': 'relations',
                        'query': {
                            'prefix': {
                                'relations.relationTo': 'loc:'
                            }
                        }
                    }
                },
                'size': 3,
                '_source': ['place_id', 'label', 'relations']
            }
        )

        for hit in sample['hits']['hits']:
            doc = hit['_source']
            loc_rels = [r for r in doc.get('relations', [])
                        if r['relationTo'].startswith('loc:')]
            if loc_rels:
                print(f"  {doc['place_id']}: {doc['label']}")
                for rel in loc_rels[:2]:
                    print(f"    → {rel['relationTo']}: {rel.get('label', 'LOC authority')}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Process Library of Congress geographic authority data'
    )
    parser.add_argument(
        '--file',
        help='Path to LOC NDJSON file (default: auto-detect from settings)'
    )

    args = parser.parse_args()

    if args.file:
        ndjson_file = args.file
    else:
        # Get from configuration
        loc_files = LOC_CONFIG.get('files', [])
        if not loc_files:
            print("ERROR: No LOC files configured")
            sys.exit(1)

        # Extract filename from URL
        file_url = loc_files[0]['url']
        filename = Path(file_url).name
        if not filename:
            filename = 'names.madsrdf.jsonld.gz'

        ndjson_file = Path(DATA_DIR) / 'authorities' / 'loc' / filename

    print(f"Starting LOC authority processing")
    print(f"File: {ndjson_file}")
    print(f"Mode: Relations only (no new places created)")
    print()

    index_loc_file(str(ndjson_file))

    create_checkpoint_snapshot(es, 'loc_authorities')