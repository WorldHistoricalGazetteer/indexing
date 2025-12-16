# processing/geonames-toponyms.py

"""
Index GeoNames alternate names (toponyms) data into Elasticsearch.

Updated to use new file paths from settings.py
"""

from elasticsearch8 import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import stream_file, create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)


def parse_year(year_str):
    """
    Parse a year string which could be:
    - Empty
    - A year (e.g., "1850")
    - A negative year for BCE (e.g., "-500")
    - Various other formats
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

    Returns:
    - ('toponym', toponym_with_lang, timespan) for regular toponyms
    - ('relation', relation_dict) for wkdt/link entries
    - (None, None, None) for entries to skip
    """
    fields = line.split("\t")

    lang_code = fields[2] if len(fields) > 2 else ''
    geoname_id = fields[1]
    value = fields[3] if len(fields) > 3 else ''

    # Handle wikidata IDs - create sameAs relation
    if lang_code == 'wkdt' and value:
        return ('relation', {
            'place_id': f"gn:{geoname_id}",
            'relationType': 'sameAs',
            'relationTo': f"wd:{value}",
            'source': 'geonames',
            'method': 'curated'
        }, None)

    # Handle links - create describedBy relation
    if lang_code == 'link' and value:
        return ('relation', {
            'place_id': f"gn:{geoname_id}",
            'relationType': 'describedBy',
            'relationTo': value,
            'source': 'geonames',
            'method': 'curated'
        }, None)

    # Skip other non-linguistic entries
    skip_codes = ['post', 'iata', 'icao', 'faac', 'unlc']
    if lang_code in skip_codes:
        return (None, None, None)

    # Skip if no actual name
    if not value:
        return (None, None, None)

    # Build toponym with language tag
    # Handle language variants (e.g., zh-CN, fr_1793)
    if '-' in lang_code or '_' in lang_code:
        # Keep the full code as-is for variants
        toponym = f"{value}@{lang_code}"
    elif lang_code:
        toponym = f"{value}@{lang_code}"
    else:
        # No language code - mark as undetermined
        toponym = f"{value}@und"

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

    return ('toponym', toponym, timespan, is_preferred)


def index_toponyms_and_relations(file_path, toponyms_index, places_index):
    """
    Read alternateNames file and:
    1. Update places with toponyms array
    2. Create toponym documents in toponyms index
    3. Stream relations to place updates
    """
    toponym_batch = []
    relations_batch = []
    places_toponyms = {}  # place_id -> list of toponyms
    preferred_names = {}  # place_id -> preferred name

    toponym_count = 0
    relations_count = 0
    skipped = 0

    print("Pass 1: Collecting toponyms and relations...")

    for line in stream_file(file_path):
        if not line or line.startswith("#"):
            continue

        try:
            result = parse_alternatename_line(line)

            if result[0] is None:
                skipped += 1
                continue

            # Handle relations
            if result[0] == 'relation':
                relation_dict = result[1]
                place_id = relation_dict['place_id']

                # Use script to append to existing relations array
                relations_batch.append({
                    "_op_type": "update",
                    "_index": places_index,
                    "_id": place_id,
                    "script": {
                        "source": """
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
                        """,
                        "params": {
                            "relation": {
                                "relationType": relation_dict['relationType'],
                                "relationTo": relation_dict['relationTo'],
                                "source": relation_dict['source'],
                                "method": relation_dict['method']
                            }
                        }
                    },
                    "upsert": {
                        "relations": [{
                            "relationType": relation_dict['relationType'],
                            "relationTo": relation_dict['relationTo'],
                            "source": relation_dict['source'],
                            "method": relation_dict['method']
                        }]
                    }
                })

                if len(relations_batch) >= BATCH_SIZE:
                    success, failed = helpers.bulk(es, relations_batch, raise_on_error=False, stats_only=True)
                    relations_count += success
                    print(f"Relations updated: {relations_count}, skipped: {skipped}")
                    relations_batch = []

                continue

            # Handle toponyms
            if result[0] == 'toponym':
                toponym, timespan, is_preferred = result[1], result[2], result[3]
                alt_name_id = line.split("\t")[0]
                geoname_id = line.split("\t")[1]
                place_id = f"gn:{geoname_id}"

                # Collect toponyms for place update
                if place_id not in places_toponyms:
                    places_toponyms[place_id] = []
                places_toponyms[place_id].append(toponym)

                # Track preferred name for label update
                if is_preferred and place_id not in preferred_names:
                    # Extract name without language tag
                    name = toponym.split('@')[0] if '@' in toponym else toponym
                    preferred_names[place_id] = name

                # Create toponym document
                toponym_doc = {
                    'place_id': place_id,
                    'name': toponym  # Full toponym@lang format
                }

                # Add timespan if available
                if timespan:
                    toponym_doc['timespans'] = [timespan]

                # Add preferred flag
                if is_preferred:
                    toponym_doc['is_preferred'] = True

                # Add to completion suggester
                name_part = toponym.split('@')[0] if '@' in toponym else toponym
                lang_part = toponym.split('@')[1] if '@' in toponym else 'und'

                toponym_doc['suggest'] = {
                    'input': [name_part],
                    'contexts': {
                        'lang': [lang_part.split('-')[0] if '-' in lang_part else lang_part]
                    }
                }

                toponym_batch.append({
                    "_index": toponyms_index,
                    "_id": f"gn_alt:{alt_name_id}",
                    "_source": toponym_doc
                })

                if len(toponym_batch) >= BATCH_SIZE:
                    success, failed = helpers.bulk(es, toponym_batch, raise_on_error=False, stats_only=True)
                    toponym_count += success
                    toponym_batch = []

        except Exception as e:
            print(f"Error processing line: {str(e)}")
            continue

    # Index remaining batches
    if toponym_batch:
        success, failed = helpers.bulk(es, toponym_batch, raise_on_error=False, stats_only=True)
        toponym_count += success

    if relations_batch:
        success, failed = helpers.bulk(es, relations_batch, raise_on_error=False, stats_only=True)
        relations_count += success

    print(f"Toponyms indexed: {toponym_count}, relations: {relations_count}, skipped: {skipped}")

    # Now update places with collected toponyms
    print("\nPass 2: Updating places with toponyms arrays and preferred labels...")

    update_batch = []
    update_count = 0

    for place_id, toponyms_list in places_toponyms.items():
        update_doc = {
            "_op_type": "update",
            "_index": places_index,
            "_id": place_id,
            "script": {
                "source": """
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
                """,
                "params": {
                    "new_toponyms": toponyms_list,
                    "label": preferred_names.get(place_id)
                }
            }
        }

        update_batch.append(update_doc)

        if len(update_batch) >= BATCH_SIZE:
            try:
                success, failed = helpers.bulk(es, update_batch, raise_on_error=False, stats_only=True)
                update_count += success
                print(f"Updated {update_count} places with toponyms...")
                update_batch = []
            except Exception as e:
                print(f"Error updating batch: {str(e)}")
                update_batch = []

    # Update remaining
    if update_batch:
        try:
            success, failed = helpers.bulk(es, update_batch, raise_on_error=False, stats_only=True)
            update_count += success
        except Exception as e:
            print(f"Error updating final batch: {str(e)}")

    print(f"\nIndexing complete!")
    print(f"Toponyms indexed: {toponym_count}")
    print(f"Relations added: {relations_count}")
    print(f"Places updated with toponyms: {update_count}")
    print(f"Skipped: {skipped}")


if __name__ == "__main__":
    ALTERNATENAMES_FILE = f"{DATA_DIR}authorities/gn/alternateNamesV2.zip"
    TOPONYMS_INDEX = "toponyms"
    PLACES_INDEX = "places"

    print(f"Starting to index Geonames alternate names from {ALTERNATENAMES_FILE}")
    print(f"Target indices: {TOPONYMS_INDEX}, {PLACES_INDEX}")

    index_toponyms_and_relations(ALTERNATENAMES_FILE, TOPONYMS_INDEX, PLACES_INDEX)
    create_checkpoint_snapshot(es, "geonames_toponyms")