# authorities/geonames-toponyms.py

import gzip
from elasticsearch import Elasticsearch, helpers

from authorities.settings import ES_HOST, BATCH_SIZE, DATA_DIR

es = Elasticsearch(ES_HOST)


def stream_file(file_path):
    """
    Generator yielding lines from a potentially compressed file.
    """
    if file_path.endswith(".gz"):
        open_func = gzip.open
        mode = 'rt'
    else:
        open_func = open
        mode = 'r'

    with open_func(file_path, mode, encoding='utf-8') as f:
        for line in f:
            yield line.strip()


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


def parse_lang_code(isolanguage):
    """
    Parse language code which may include variant.
    Examples: 'en', 'zh-CN', 'zh-Hant', 'fr_1793'
    Returns (lang, variant) tuple
    """
    if not isolanguage:
        return (None, None)

    # Split on hyphen or underscore
    if '-' in isolanguage:
        parts = isolanguage.split('-', 1)
        return (parts[0], parts[1])
    elif '_' in isolanguage:
        parts = isolanguage.split('_', 1)
        return (parts[0], parts[1])
    else:
        return (isolanguage, None)


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
    - ('toponym', doc) for regular toponyms
    - ('relation', doc) for wkdt/link entries
    - (None, None) for entries to skip
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
            'relationTo': f"wd:{value}"
        })

    # Handle links - create describedBy relation
    if lang_code == 'link' and value:
        return ('relation', {
            'place_id': f"gn:{geoname_id}",
            'relationType': 'describedBy',
            'relationTo': value
        })

    # Skip other non-linguistic entries
    skip_codes = ['post', 'iata', 'icao', 'faac', 'unlc']
    if lang_code in skip_codes:
        return (None, None)

    # Skip if no actual name
    if not value:
        return (None, None)

    geoname_id = fields[1]
    name = value

    # Parse language and variant
    lang, lang_variant = parse_lang_code(lang_code)

    # Parse boolean flags
    is_preferred = fields[4] == '1' if len(fields) > 4 else False
    is_short = fields[5] == '1' if len(fields) > 5 else False
    is_colloquial = fields[6] == '1' if len(fields) > 6 else False
    is_historic = fields[7] == '1' if len(fields) > 7 else False

    # Parse temporal information
    year_from = parse_year(fields[8]) if len(fields) > 8 else None
    year_to = parse_year(fields[9]) if len(fields) > 9 else None

    # Build the document
    doc = {
        "place_id": f"gn:{geoname_id}",
        "name": name,
        "name_lower": name.lower()
    }

    # Add language info
    if lang:
        doc["lang"] = lang
    if lang_variant:
        doc["lang_variant"] = lang_variant

    # Add boolean flags
    if is_preferred:
        doc["is_preferred"] = True
    if is_short:
        doc["is_short"] = True
    if is_colloquial:
        doc["is_colloquial"] = True
    if is_historic:
        doc["is_historic"] = True

    # Add temporal information
    if year_from is not None or year_to is not None:
        doc["timespans"] = []
        timespan = {}
        if year_from is not None:
            timespan["start"] = year_from
        if year_to is not None:
            timespan["end"] = year_to
        if timespan:
            doc["timespans"].append(timespan)

    # Add to completion suggester with language context
    suggest_input = [name]
    if lang:
        doc["suggest"] = {
            "input": suggest_input,
            "contexts": {
                "lang": [lang]
            }
        }
    else:
        doc["suggest"] = {
            "input": suggest_input
        }

    return ('toponym', doc)


def index_toponyms(file_path, toponyms_index):
    """
    Read alternateNames file and index toponyms.
    Also collect relations (wkdt, link) for updating places.
    """
    batch = []
    relations_by_place = {}  # Collect relations per place for batch updates
    count = 0
    skipped = 0
    relations_count = 0

    for line in stream_file(file_path):
        if not line or line.startswith("#"):
            continue

        try:
            entry_type, doc = parse_alternatename_line(line)

            if entry_type is None:
                skipped += 1
                continue

            # Handle relations
            if entry_type == 'relation':
                place_id = doc['place_id']
                if place_id not in relations_by_place:
                    relations_by_place[place_id] = []
                relations_by_place[place_id].append({
                    'relationType': doc['relationType'],
                    'relationTo': doc['relationTo']
                })
                relations_count += 1
                continue

            # Handle toponyms
            if entry_type == 'toponym':
                alt_name_id = line.split("\t")[0]
                batch.append({
                    "_index": toponyms_index,
                    "_id": f"gn_alt:{alt_name_id}",
                    "_source": doc
                })

                if len(batch) >= BATCH_SIZE:
                    success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
                    count += success
                    print(
                        f"Indexed {count} toponyms so far... (skipped {skipped} non-name entries, collected {relations_count} relations)")
                    batch = []

        except Exception as e:
            print(f"Error processing line: {str(e)}")
            continue

    # Index remaining batch
    if batch:
        success, failed = helpers.bulk(es, batch, raise_on_error=False, stats_only=True)
        count += success

    print(f"Toponym indexing complete. Total toponyms indexed: {count}, skipped: {skipped}")
    print(f"Collected {relations_count} relations for {len(relations_by_place)} places")

    return relations_by_place


def update_place_relations(relations_by_place, places_index):
    """
    Update place documents with relations collected from wkdt and link entries.
    """
    print("Updating place relations...")

    updates = []
    count = 0

    for place_id, relations in relations_by_place.items():
        updates.append({
            "_op_type": "update",
            "_index": places_index,
            "_id": place_id,
            "doc": {
                "relations": relations
            }
        })

        if len(updates) >= BATCH_SIZE:
            try:
                success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
                count += success
                print(f"Updated {count} places with relations...")
                updates = []
            except Exception as e:
                print(f"Error updating batch: {str(e)}")
                updates = []

    # Update remaining
    if updates:
        try:
            success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
            count += success
        except Exception as e:
            print(f"Error updating final batch: {str(e)}")

    print(f"Place relations update complete. Total updated: {count}")


def update_place_labels(toponyms_index, places_index):
    """
    Update place documents with preferred names from toponyms.
    For each place, find the preferred name and update the label field.
    """
    print("Updating place labels with preferred names...")

    # Query for all preferred names
    query = {
        "query": {
            "term": {
                "is_preferred": True
            }
        },
        "_source": ["place_id", "name", "lang"],
        "size": 1000
    }

    # Scroll through all preferred names
    resp = es.search(index=toponyms_index, body=query, scroll='5m')
    scroll_id = resp['_scroll_id']
    hits = resp['hits']['hits']

    updates = []
    count = 0

    while hits:
        for hit in hits:
            source = hit['_source']
            place_id = source['place_id']
            name = source['name']

            updates.append({
                "_op_type": "update",
                "_index": places_index,
                "_id": place_id,
                "doc": {
                    "label": name
                }
            })

            if len(updates) >= BATCH_SIZE:
                try:
                    success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
                    count += success
                    print(f"Updated {count} place labels...")
                    updates = []
                except Exception as e:
                    print(f"Error updating batch: {str(e)}")
                    updates = []

        # Get next batch
        resp = es.scroll(scroll_id=scroll_id, scroll='5m')
        scroll_id = resp['_scroll_id']
        hits = resp['hits']['hits']

    # Update remaining
    if updates:
        try:
            success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
            count += success
        except Exception as e:
            print(f"Error updating final batch: {str(e)}")

    # Clear scroll
    es.clear_scroll(scroll_id=scroll_id)

    print(f"Place label update complete. Total updated: {count}")


if __name__ == "__main__":
    ALTERNATENAMES_FILE = f"{DATA_DIR}geonames/alternateNamesV2/alternateNamesV2.txt"
    TOPONYMS_INDEX = "toponyms"
    PLACES_INDEX = "places"

    print(f"Starting to index Geonames alternate names from {ALTERNATENAMES_FILE}")
    print(f"Target index: {TOPONYMS_INDEX}")

    # Index all toponyms and collect relations
    relations_by_place = index_toponyms(ALTERNATENAMES_FILE, TOPONYMS_INDEX)

    # Update places with relations
    update_place_relations(relations_by_place, PLACES_INDEX)

    # Update place labels with preferred names
    update_place_labels(TOPONYMS_INDEX, PLACES_INDEX)