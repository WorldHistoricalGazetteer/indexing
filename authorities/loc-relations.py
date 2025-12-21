# processing/loc-relations.py
"""
Process Library of Congress geographic authority data.
"""
import json, gzip, os, sys
from pathlib import Path
from datetime import datetime
from elasticsearch import Elasticsearch
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE, AUTHORITIES
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)
LOC_CONFIG = next((auth for auth in AUTHORITIES if auth['namespace'] == 'loc'), None)


def extract_loc_geographic_data(record):
    """Extract geographic info from LOC MADS/RDF."""
    if '@graph' in record:
        graph_items = record['@graph']
    elif isinstance(record, list):
        graph_items = record
    else:
        graph_items = [record]

    results = []
    for item in graph_items:
        if not isinstance(item, dict): continue
        item_types = item.get('@type', [])
        if isinstance(item_types, str): item_types = [item_types]
        if not any('GeographicElement' in t or 'Geographic' in t for t in item_types): continue

        loc_id = item.get('@id', '')
        if not loc_id: continue
        if loc_id.startswith('http://id.loc.gov/'):
            loc_id = loc_id.replace('http://id.loc.gov/', '')

        label = None
        if 'madsrdf:authoritativeLabel' in item:
            label_obj = item['madsrdf:authoritativeLabel']
            if isinstance(label_obj, dict):
                label = label_obj.get('@value', '')
            elif isinstance(label_obj, str):
                label = label_obj
        if not label and 'rdfs:label' in item:
            label = item['rdfs:label']
            if isinstance(label, dict): label = label.get('@value', '')
        if not label: continue

        external_links = []
        for field in ['madsrdf:hasExactExternalAuthority', 'madsrdf:hasCloseExternalAuthority',
                      'madsrdf:identifiesRWO']:
            if field not in item: continue
            values = item[field]
            if not isinstance(values, list): values = [values]
            for value in values:
                if isinstance(value, dict) and '@id' in value:
                    uri = value['@id']
                elif isinstance(value, str):
                    uri = value
                else:
                    continue

                if 'geonames.org' in uri:
                    gn_id = uri.rstrip('/').split('/')[-1]
                    if gn_id.isdigit():
                        external_links.append({
                            'type': 'geonames',
                            'id': f"gn:{gn_id}",
                            'uri': uri,
                            'relation': 'sameAs' if 'Exact' in field else 'closeMatch'
                        })
                elif 'wikidata.org' in uri:
                    qid = uri.rstrip('/').split('/')[-1]
                    if qid.startswith('Q'):
                        external_links.append({
                            'type': 'wikidata',
                            'id': f"wd:{qid}",
                            'uri': uri,
                            'relation': 'sameAs' if 'Exact' in field else 'closeMatch'
                        })
                elif 'viaf.org' in uri:
                    viaf_id = uri.rstrip('/').split('/')[-1]
                    if viaf_id.isdigit():
                        external_links.append({
                            'type': 'viaf',
                            'id': f"viaf:{viaf_id}",
                            'uri': uri,
                            'relation': 'sameAs' if 'Exact' in field else 'closeMatch'
                        })

        if external_links:
            results.append({'loc_id': loc_id, 'label': label, 'external_links': external_links})

    return results


def update_place_relations(external_links, loc_id, label):
    """Update places with LOC relations."""
    updated = 0
    for link in external_links:
        place_id = link['id']
        try:
            if not es.exists(index='places', id=place_id): continue
        except:
            continue

        loc_relation = {
            'relation_type': 'hasAuthority',
            'related_place_id': f"loc:{loc_id}",
            'label': f"LOC: {label}"
        }

        try:
            es.update(
                index='places',
                id=place_id,
                body={
                    'script': {
                        'source': '''
                            if (ctx._source.relations == null) {
                                ctx._source.relations = [];
                            }
                            boolean exists = false;
                            for (rel in ctx._source.relations) {
                                if (rel.related_place_id == params.relation.related_place_id) {
                                    exists = true;
                                    break;
                                }
                            }
                            if (!exists) {
                                ctx._source.relations.add(params.relation);
                            }
                        ''',
                        'params': {'relation': loc_relation}
                    }
                }
            )
            updated += 1
        except:
            continue

        reverse_relation = {
            'relation_type': link['relation'],
            'related_place_id': place_id,
            'label': 'LOC Authority'
        }

        try:
            es.update(
                index='places',
                id=place_id,
                body={
                    'script': {
                        'source': '''
                            if (ctx._source.relations == null) {
                                ctx._source.relations = [];
                            }
                            boolean exists = false;
                            for (rel in ctx._source.relations) {
                                if (rel.related_place_id == params.relation.related_place_id && 
                                    rel.relation_type == params.relation.relation_type) {
                                    exists = true;
                                    break;
                                }
                            }
                            if (!exists) {
                                ctx._source.relations.add(params.relation);
                            }
                        ''',
                        'params': {'relation': reverse_relation}
                    }
                }
            )
        except:
            continue

    return updated


def index_loc_file(ndjson_file):
    """Process LOC file and create relations."""
    print(f"Processing: {ndjson_file}")
    if not os.path.exists(ndjson_file):
        standard_path = Path(DATA_DIR) / 'authorities' / 'loc' / Path(ndjson_file).name
        if standard_path.exists():
            ndjson_file = standard_path
        else:
            print(f"ERROR: Not found: {ndjson_file}")
            return

    total_records = 0
    geographic_records = 0
    relations_created = 0
    places_updated = 0
    errors = 0

    print("Reading LOC data...")
    start_time = datetime.now()

    file_opener = gzip.open if ndjson_file.endswith('.gz') else open
    mode = 'rt' if ndjson_file.endswith('.gz') else 'r'

    try:
        with file_opener(ndjson_file, mode, encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip(): continue
                total_records += 1

                if total_records % 10000 == 0:
                    elapsed = (datetime.now() - start_time).seconds
                    rate = total_records / elapsed if elapsed > 0 else 0
                    print(
                        f"\r  {total_records:,} ({rate:.1f}/s) - geo: {geographic_records:,}, rels: {relations_created:,}", end='', flush=True)

                try:
                    record = json.loads(line)
                    geo_items = extract_loc_geographic_data(record)
                    if not geo_items: continue

                    geographic_records += len(geo_items)
                    for geo_item in geo_items:
                        if not geo_item['external_links']: continue
                        updated = update_place_relations(geo_item['external_links'], geo_item['loc_id'],
                                                         geo_item['label'])
                        if updated > 0:
                            places_updated += updated
                            relations_created += len(geo_item['external_links'])

                except json.JSONDecodeError as e:
                    if total_records < 10: print(f"  JSON error line {line_num}: {e}")
                    errors += 1
                    continue
                except Exception as e:
                    if errors < 10: print(f"  Error line {line_num}: {e}")
                    errors += 1
                    continue

    except Exception as e:
        print(f"ERROR: {e}")
        return

    elapsed = (datetime.now() - start_time).seconds
    print(f"\n{'=' * 80}")
    print(f"LOC COMPLETE")
    print(f"{'=' * 80}")
    print(f"Time: {elapsed}s")
    print(f"Records: {total_records:,}")
    print(f"Geographic: {geographic_records:,}")
    print(f"Relations: {relations_created:,}")
    print(f"Places updated: {places_updated:,}")
    print(f"Errors: {errors:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Process LOC geographic authority data')
    parser.add_argument('--file', help='Path to LOC NDJSON file')
    args = parser.parse_args()

    if args.file:
        ndjson_file = args.file
    else:
        loc_files = LOC_CONFIG.get('files', [])
        if not loc_files:
            print("ERROR: No LOC files configured")
            sys.exit(1)
        file_url = loc_files[0]['url']
        filename = Path(file_url).name
        if not filename: filename = 'names.madsrdf.jsonld.gz'
        ndjson_file = Path(DATA_DIR) / 'authorities' / 'loc' / filename

    print("LOC Authority Processing (SCHEMA V2)")
    print(f"File: {ndjson_file}\n")
    index_loc_file(str(ndjson_file))
    create_checkpoint_snapshot(es, 'loc_authorities')