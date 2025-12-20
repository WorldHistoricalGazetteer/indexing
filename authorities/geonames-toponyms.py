# authorities/geonames_toponyms.py

"""
Update GeoNames places with alternate names (toponyms) data.
"""
import sys
from collections import defaultdict

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import stream_file, create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)


def normalize_lst(name, lang='und'):
    """Ensure toponym is in LST format (name@lang)."""
    if not name:
        return None
    if '@' in name:
        return name
    return f"{name}@{lang}"


def parse_year(year_str):
    """Parse year string, handling empty, positive, and negative years."""
    if not year_str or year_str.strip() == '':
        return None
    try:
        return int(year_str.strip())
    except ValueError:
        return None


def parse_alternatename_line(line):
    """
    Parse alternateNames line - SCHEMA COMPLIANT.

    Returns:
    - ('toponym', lst, timespan, is_preferred, place_id) for regular toponyms
    - ('relation', place_id, relation_dict) for wkdt/link entries
    - (None, ...) for entries to skip
    """
    fields = line.split("\t")

    lang_code = fields[2] if len(fields) > 2 else ''
    geoname_id = fields[1]
    value = fields[3] if len(fields) > 3 else ''
    place_id = f"gn:{geoname_id}"

    # Handle wikidata IDs
    if lang_code == 'wkdt' and value:
        return ('relation', place_id, {
            'relation_type': 'sameAs',
            'related_place_id': f"wd:{value}",
            'label': 'Wikidata'
        })

    # Handle links
    if lang_code == 'link' and value:
        return ('relation', place_id, {
            'relation_type': 'describedBy',
            'related_place_id': value,
            'label': 'External Link'
        })

    # Skip other non-linguistic entries
    skip_codes = ['post', 'iata', 'icao', 'faac', 'unlc', 'tcid', 'abbr']
    if lang_code in skip_codes:
        return (None,)

    if not value:
        return (None,)

    # Build LST
    if lang_code:
        lang_code = lang_code.replace('_', '-')
        lst = normalize_lst(value, lang_code)
    else:
        lst = normalize_lst(value, 'und')

    # Parse temporal information
    year_from = parse_year(fields[8]) if len(fields) > 8 else None
    year_to = parse_year(fields[9]) if len(fields) > 9 else None

    timespan = None
    if year_from is not None or year_to is not None:
        timespan = {}
        if year_from is not None:
            timespan["start"] = year_from
        if year_to is not None:
            timespan["end"] = year_to

    is_preferred = fields[4] == '1' if len(fields) > 4 else False

    return ('toponym', lst, timespan, is_preferred, place_id)


def pass1_update_places_with_toponyms(file_path):
    """
    PASS 1: Stream through file and update places with toponyms - SCHEMA COMPLIANT.
    """
    print("\n" + "=" * 80)
    print("PASS 1: UPDATING PLACES WITH TOPONYMS")
    print("=" * 80)

    # Batch updates by place_id
    place_updates = defaultdict(lambda: {'toponyms': [], 'seen': set(), 'title': None})

    processed = 0
    skipped = 0
    places_updated = 0

    def flush_updates():
        """Flush accumulated place updates."""
        nonlocal places_updated

        if not place_updates:
            return

        batch = []
        for place_id, data in place_updates.items():
            if not data['toponyms']:
                continue

            update_op = {
                '_op_type': 'update',
                '_index': 'places',
                '_id': place_id,
                'script': {
                    'source': '''
                        if (ctx._source.toponyms == null) {
                            ctx._source.toponyms = [];
                        }
                        for (new_toponym in params.new_toponyms) {
                            boolean exists = false;
                            for (existing in ctx._source.toponyms) {
                                if (existing.toponym_id == new_toponym.toponym_id) {
                                    exists = true;
                                    break;
                                }
                            }
                            if (!exists) {
                                ctx._source.toponyms.add(new_toponym);
                            }
                        }
                        if (params.title != null) {
                            ctx._source.title = params.title;
                        }
                    ''',
                    'params': {
                        'new_toponyms': data['toponyms'],
                        'title': data['title']
                    }
                }
            }
            batch.append(update_op)

        try:
            success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
            places_updated += success

            if failed > 0:
                sys.stdout.write(f"\r  Updated {places_updated:,} places ({failed} failed)...")
            else:
                sys.stdout.write(f"\r  Updated {places_updated:,} places...")
            sys.stdout.flush()
        except Exception as e:
            print(f"\nError updating batch: {str(e)}")

        place_updates.clear()

    # Stream through file
    for line in stream_file(file_path):
        if not line or line.startswith("#"):
            continue

        processed += 1
        if processed % 100000 == 0:
            sys.stdout.write(f"\r  Processed {processed:,} lines...")
            sys.stdout.flush()

        try:
            result = parse_alternatename_line(line)

            if result[0] != 'toponym':
                skipped += 1
                continue

            _, lst, timespan, is_preferred, place_id = result

            if lst in place_updates[place_id]['seen']:
                continue

            place_updates[place_id]['seen'].add(lst)

            toponym_entry = {'toponym_id': lst}

            if timespan:
                scoped_timespan = {}
                if 'start' in timespan:
                    scoped_timespan['start'] = {'in': timespan['start']}
                if 'end' in timespan:
                    scoped_timespan['end'] = {'in': timespan['end']}
                toponym_entry['timespan'] = scoped_timespan

            place_updates[place_id]['toponyms'].append(toponym_entry)

            if is_preferred and place_updates[place_id]['title'] is None:
                name = lst.split('@')[0] if '@' in lst else lst
                place_updates[place_id]['title'] = name

            if len(place_updates) >= BATCH_SIZE:
                flush_updates()

        except Exception as e:
            skipped += 1
            continue

    flush_updates()

    print(f"\n  Total places updated: {places_updated:,}")
    return places_updated


def pass2_update_places_with_relations(file_path):
    """
    PASS 2: Stream through file and add relations.
    """
    print("\n" + "=" * 80)
    print("PASS 2: ADDING RELATIONS TO PLACES")
    print("=" * 80)

    batch = []
    processed = 0
    skipped = 0
    relations_added = 0

    for line in stream_file(file_path):
        if not line or line.startswith("#"):
            continue

        processed += 1
        if processed % 100000 == 0:
            sys.stdout.write(f"\r  Processed {processed:,} lines, added {relations_added:,} relations...")
            sys.stdout.flush()

        try:
            result = parse_alternatename_line(line)

            if result[0] != 'relation':
                skipped += 1
                continue

            _, place_id, relation = result

            update_op = {
                '_op_type': 'update',
                '_index': 'places',
                '_id': place_id,
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
                    'params': {
                        'relation': relation
                    }
                }
            }

            batch.append(update_op)

            if len(batch) >= BATCH_SIZE:
                try:
                    success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
                    relations_added += success
                    sys.stdout.write(f"\r  Added {relations_added:,} relations...")
                    sys.stdout.flush()
                    batch = []
                except Exception as e:
                    print(f"\nError updating batch: {str(e)}")
                    batch = []

        except Exception as e:
            skipped += 1
            continue

    if batch:
        try:
            success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
            relations_added += success
        except Exception as e:
            print(f"\nError updating final batch: {str(e)}")

    print(f"\n  Total relations added: {relations_added:,}")
    return relations_added


if __name__ == "__main__":
    ALTERNATENAMES_FILE = f"{DATA_DIR}/authorities/gn/alternateNamesV2.zip"

    print("=" * 80)
    print("GEONAMES TOPONYMS INGESTION")
    print("=" * 80)
    print(f"Source: {ALTERNATENAMES_FILE}")
    print()

    places_updated = pass1_update_places_with_toponyms(ALTERNATENAMES_FILE)
    relations_count = pass2_update_places_with_relations(ALTERNATENAMES_FILE)

    print("\n" + "=" * 80)
    print("INGESTION COMPLETE")
    print("=" * 80)
    print(f"Places updated: {places_updated:,}")
    print(f"Relations added: {relations_count:,}")
    print()

    print("Creating checkpoint snapshot...")
    create_checkpoint_snapshot(es, "geonames_toponyms")

    print("\n" + "=" * 80)
    print("COMPLETE")
    print("=" * 80)