# authorities/geonames_toponyms.py

"""
Update GeoNames places with alternate names (toponyms) data.

MEMORY-EFFICIENT VERSION: Uses streaming approach to avoid loading
17M records into memory.

Two-pass approach:
1. Update places with toponyms (streaming)
2. Update places with relations (streaming)

NOTE: This script does NOT index to the toponyms index. The toponyms index
is populated by a separate deduplication step (deduplicate_and_index_toponyms)
that runs after ALL authorities are ingested. This ensures each unique LST
appears only once in the toponyms index, regardless of how many authorities
reference it.
"""
import sys
from collections import defaultdict

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import stream_file, create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)


def normalize_lst(name, lang='und'):
    """
    Ensure toponym is in LST format (name@lang).

    Args:
        name: The toponym name
        lang: Language code (default: 'und' for undetermined)

    Returns:
        Normalized LST string
    """
    if not name:
        return None
    if '@' in name:
        return name
    return f"{name}@{lang}"


def parse_year(year_str):
    """
    Parse a year string which could be:
    - Empty
    - A year (e.g., "1850")
    - A negative year for BCE (e.g., "-500")
    Returns integer year or None
    """
    if not year_str or year_str.strip() == '':
        return None
    try:
        return int(year_str.strip())
    except ValueError:
        return None


def parse_alternatename_line(line):
    """
    Parse a single alternateNames line.

    Field positions from Geonames readme:
    0: alternateNameId
    1: geonameid
    2: isolanguage
    3: alternate name
    4: isPreferredName
    5: isShortName
    6: isColloquial
    7: isHistoric
    8: from (period)
    9: to (period)

    Returns tuple:
    - ('toponym', lst, timespan, is_preferred, place_id) for regular toponyms
    - ('relation', place_id, relation_dict) for wkdt/link entries
    - (None, ...) for entries to skip
    """
    fields = line.split("\t")

    lang_code = fields[2] if len(fields) > 2 else ''
    geoname_id = fields[1]
    value = fields[3] if len(fields) > 3 else ''
    place_id = f"gn:{geoname_id}"

    # Handle wikidata IDs - create sameAs relation
    if lang_code == 'wkdt' and value:
        return ('relation', place_id, {
            'relationType': 'sameAs',
            'relationTo': f"wd:{value}",
            'source': 'geonames',
            'method': 'curated',
            'certainty': 1.0
        })

    # Handle links - create describedBy relation
    if lang_code == 'link' and value:
        return ('relation', place_id, {
            'relationType': 'describedBy',
            'relationTo': value,
            'source': 'geonames',
            'method': 'curated'
        })

    # Skip other non-linguistic entries
    skip_codes = ['post', 'iata', 'icao', 'faac', 'unlc', 'tcid', 'abbr']
    if lang_code in skip_codes:
        return (None,)

    # Skip if no actual name
    if not value:
        return (None,)

    # Build LST (Language-Scoped Toponym)
    # Handle language variants (e.g., zh-CN, fr_1793)
    if lang_code:
        # Normalize underscore to hyphen for consistency
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

    # Check if it's a preferred name
    is_preferred = fields[4] == '1' if len(fields) > 4 else False

    return ('toponym', lst, timespan, is_preferred, place_id)


def pass1_update_places_with_toponyms(file_path):
    """
    PASS 1: Stream through file and update places with toponyms.

    Uses simplified schema: toponyms is a nested array with toponym_id and timespan.
    No separate string array.

    Batches updates by place_id to avoid duplicate updates.
    Memory usage: ~200MB (one batch of place updates)

    NOTE: This does NOT index to the toponyms index. The toponyms index
    will be populated by a separate deduplication step after all authorities
    are ingested.
    """
    print("\n" + "=" * 80)
    print("PASS 1: UPDATING PLACES WITH TOPONYMS")
    print("=" * 80)

    # Batch updates by place_id
    # Key: place_id, Value: {'toponyms': list(), 'seen': set(), 'label': str}
    place_updates = defaultdict(lambda: {'toponyms': [], 'seen': set(), 'label': None})

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
                        // Add new toponyms (with deduplication by toponym_id)
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
                        if (params.label != null) {
                            ctx._source.label = params.label;
                        }
                    ''',
                    'params': {
                        'new_toponyms': data['toponyms'],
                        'label': data['label']
                    }
                }
            }
            batch.append(update_op)

        # Bulk update
        try:
            success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
            places_updated += success

            if failed > 0:
                sys.stdout.write(f"\r  Updated {places_updated:,} places ({failed} failed)...")
            else:
                sys.stdout.write(f"\r  Updated {places_updated:,} places...                  ")
            sys.stdout.flush()
        except Exception as e:
            print(f"\nError updating batch: {str(e)}")

        # Clear batch
        place_updates.clear()

    # Stream through file
    for line in stream_file(file_path):
        if not line or line.startswith("#"):
            continue

        processed += 1
        if processed % 100000 == 0:
            sys.stdout.write(f"\r  Processed {processed:,} lines...                      ")
            sys.stdout.flush()

        try:
            result = parse_alternatename_line(line)

            if result[0] != 'toponym':
                skipped += 1
                continue

            _, lst, timespan, is_preferred, place_id = result

            # Check if we've already added this LST for this place
            if lst in place_updates[place_id]['seen']:
                continue

            place_updates[place_id]['seen'].add(lst)

            # Build toponym entry with temporal scoping
            toponym_entry = {'toponym_id': lst}

            if timespan:
                # Convert to nested structure expected by schema
                scoped_timespan = {}
                if 'start' in timespan:
                    scoped_timespan['start'] = {'in': timespan['start']}
                if 'end' in timespan:
                    scoped_timespan['end'] = {'in': timespan['end']}
                toponym_entry['timespan'] = scoped_timespan

            place_updates[place_id]['toponyms'].append(toponym_entry)

            # Set preferred label (first preferred name wins)
            if is_preferred and place_updates[place_id]['label'] is None:
                name = lst.split('@')[0] if '@' in lst else lst
                place_updates[place_id]['label'] = name

            # Flush when batch is large enough
            if len(place_updates) >= BATCH_SIZE:
                flush_updates()

        except Exception as e:
            skipped += 1
            continue

    # Final flush
    flush_updates()

    print(f"\n  Total places updated: {places_updated:,}")
    return places_updated


def pass2_update_places_with_relations(file_path):
    """
    PASS 2: Stream through file and add relations to places.

    Memory usage: ~50MB (one batch of relation updates)
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
                        // Check if this relation already exists
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

    # Final batch
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
    print("GEONAMES TOPONYMS INGESTION (MEMORY-EFFICIENT)")
    print("=" * 80)
    print(f"Source: {ALTERNATENAMES_FILE}")
    print()
    print("This script uses a two-pass streaming approach:")
    print("  Pass 1: Update places with toponyms (streaming)")
    print("  Pass 2: Add relations to places (streaming)")
    print()
    print("Note: Toponyms index will be populated by separate deduplication step")
    print("      after all authorities are ingested.")
    print()
    print("Peak memory usage: <1GB")
    print()

    # Pass 1: Update places with toponyms
    places_updated = pass1_update_places_with_toponyms(ALTERNATENAMES_FILE)

    # Pass 2: Add relations
    relations_count = pass2_update_places_with_relations(ALTERNATENAMES_FILE)

    print("\n" + "=" * 80)
    print("INGESTION COMPLETE")
    print("=" * 80)
    print(f"Places updated: {places_updated:,}")
    print(f"Relations added: {relations_count:,}")
    print()
    print("Note: Run deduplicate_and_index_toponyms() after all authorities")
    print("      are ingested to populate the toponyms index.")
    print()

    print("Creating checkpoint snapshot...")
    create_checkpoint_snapshot(es, "geonames_toponyms")

    print("\n" + "=" * 80)
    print("COMPLETE")
    print("=" * 80)