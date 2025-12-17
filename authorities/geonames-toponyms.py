# authorities/geonames_toponyms.py

"""
Index GeoNames alternate names (toponyms) data into Elasticsearch.

MEMORY-EFFICIENT VERSION: Uses streaming approach to avoid loading
17M records into memory.

Three-pass approach:
1. Index unique toponyms directly (streaming)
2. Update places with toponyms (streaming)
3. Update places with relations (streaming)
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


def pass1_index_unique_toponyms(file_path):
    """
    PASS 1: Stream through file and index unique toponyms.

    Uses a small in-memory cache to aggregate timespans for the same LST,
    then flushes when cache is full.

    Memory usage: ~500MB max (cache of 100k LSTs)
    """
    print("=" * 80)
    print("PASS 1: INDEXING UNIQUE TOPONYMS")
    print("=" * 80)

    # Small cache to aggregate timespans for same LST before indexing
    # Flush when it reaches max size
    lst_cache = defaultdict(lambda: {'timespans': [], 'suggest_input': None, 'suggest_lang': None})
    CACHE_MAX = 100000

    processed = 0
    skipped = 0
    toponyms_indexed = 0

    def flush_cache():
        """Index the current cache and clear it."""
        nonlocal toponyms_indexed

        if not lst_cache:
            return

        batch = []
        for lst, data in lst_cache.items():
            doc = {
                'toponym_id': lst,  # The full LST (pipeline will extract name/lang)
            }

            # Add timespans if any
            if data['timespans']:
                doc['timespans'] = data['timespans']

            # Add completion suggester
            if data['suggest_input'] and data['suggest_lang']:
                doc['suggest'] = {
                    'input': [data['suggest_input']],
                    'contexts': {
                        'lang': [data['suggest_lang']]
                    }
                }

            batch.append({
                '_index': 'toponyms',
                '_id': lst,  # LST is the document ID (ensures uniqueness)
                '_source': doc
            })

        # Bulk index
        success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
        toponyms_indexed += success

        # Clear cache
        lst_cache.clear()

        sys.stdout.write(f"\r  Processed {processed:,} lines, indexed {toponyms_indexed:,} toponyms...")
        sys.stdout.flush()

    # Stream through file
    for line in stream_file(file_path):
        if not line or line.startswith("#"):
            continue

        processed += 1

        try:
            result = parse_alternatename_line(line)

            if result[0] != 'toponym':
                skipped += 1
                continue

            _, lst, timespan, is_preferred, place_id = result

            # Add to cache
            if timespan and timespan not in lst_cache[lst]['timespans']:
                lst_cache[lst]['timespans'].append(timespan)

            # Set suggest fields (first time we see this LST)
            if lst_cache[lst]['suggest_input'] is None:
                name_part = lst.split('@')[0] if '@' in lst else lst
                lang_part = lst.split('@')[1] if '@' in lst else 'und'
                lang_context = lang_part.split('-')[0] if '-' in lang_part else lang_part

                lst_cache[lst]['suggest_input'] = name_part
                lst_cache[lst]['suggest_lang'] = lang_context

            # Flush cache when it gets large
            if len(lst_cache) >= CACHE_MAX:
                flush_cache()

        except Exception as e:
            skipped += 1
            if skipped % 10000 == 0:
                print(f"\nWarning: {skipped:,} lines skipped due to errors")
            continue

    # Final flush
    flush_cache()

    print(f"\n  Total lines processed: {processed:,}")
    print(f"  Unique toponyms indexed: {toponyms_indexed:,}")
    print(f"  Lines skipped: {skipped:,}")

    return toponyms_indexed


def pass2_update_places_with_toponyms(file_path):
    """
    PASS 2: Stream through file and update places with toponyms.

    Batches updates by place_id to avoid duplicate updates.
    Memory usage: ~200MB (one batch of place updates)
    """
    print("\n" + "=" * 80)
    print("PASS 2: UPDATING PLACES WITH TOPONYMS")
    print("=" * 80)

    # Batch updates by place_id
    # Key: place_id, Value: {'toponyms': set(), 'label': str}
    place_updates = defaultdict(lambda: {'toponyms': set(), 'label': None})

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
                        for (toponym in params.new_toponyms) {
                            if (!ctx._source.toponyms.contains(toponym)) {
                                ctx._source.toponyms.add(toponym);
                            }
                        }
                        if (params.label != null) {
                            ctx._source.label = params.label;
                        }
                    ''',
                    'params': {
                        'new_toponyms': list(data['toponyms']),
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
                sys.stdout.write(f"\r  Updated {places_updated:,} places...")
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
            sys.stdout.write(f"\r  Processed {processed:,} lines...")
            sys.stdout.flush()

        try:
            result = parse_alternatename_line(line)

            if result[0] != 'toponym':
                skipped += 1
                continue

            _, lst, timespan, is_preferred, place_id = result

            # Add to place's toponyms set
            place_updates[place_id]['toponyms'].add(lst)

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


def pass3_update_places_with_relations(file_path):
    """
    PASS 3: Stream through file and add relations to places.

    Memory usage: ~50MB (one batch of relation updates)
    """
    print("\n" + "=" * 80)
    print("PASS 3: ADDING RELATIONS TO PLACES")
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
    print("This script uses a three-pass streaming approach:")
    print("  Pass 1: Index unique toponyms (streaming with small cache)")
    print("  Pass 2: Update places with toponyms (streaming)")
    print("  Pass 3: Add relations to places (streaming)")
    print()
    print("Peak memory usage: <1GB")
    print()

    # Pass 1: Index unique toponyms
    toponyms_count = pass1_index_unique_toponyms(ALTERNATENAMES_FILE)

    # Pass 2: Update places with toponyms
    places_updated = pass2_update_places_with_toponyms(ALTERNATENAMES_FILE)

    # Pass 3: Add relations
    relations_count = pass3_update_places_with_relations(ALTERNATENAMES_FILE)

    print("\n" + "=" * 80)
    print("INGESTION COMPLETE")
    print("=" * 80)
    print(f"Unique toponyms indexed: {toponyms_count:,}")
    print(f"Places updated: {places_updated:,}")
    print(f"Relations added: {relations_count:,}")
    print()

    print("Creating checkpoint snapshot...")
    create_checkpoint_snapshot(es, "geonames_toponyms")

    print("\n" + "=" * 80)
    print("COMPLETE")
    print("=" * 80)