# processing/wikidata-places.py

"""
Stage Wikidata places data to ``{STAGED_BASE_DIR}/wd/extract/places.jsonl``.

OPTIMIZED — uses orjson and byte-scanning for 3-5× speedup over the
naive parser.
"""

import gzip
import sys
import os
from urllib.parse import quote

import orjson  # Much faster than json
from processing.helpers import enrich_geometry, write_staged_place_doc
from processing.settings import DATA_DIR, GEOSHAPE_REFS_FILE, WIKIDATA_P1014_FILE
from typesystem.extract_wikidata_p1014 import extract_p1014_ids

# Wikipedia sitelink allow-list. A well-known place can carry 100–300 sitelinks;
# storing every URL bloats the index, so we keep only these language wikis.
# Tune this set to taste (kept broad enough to cover the WHG UI languages plus
# the largest Wikipedias). `enwiki` is effectively always desired.
WIKIPEDIA_SITELINK_LANGS = frozenset({
    'en', 'fr', 'de', 'es', 'it', 'pt', 'nl', 'ru', 'pl', 'sv',
    'zh', 'ja', 'ar', 'fa', 'tr', 'uk', 'he', 'el', 'cs', 'ca',
})

# Pre-compile byte patterns for fast scanning
SKIP_BYTES = {b'[', b']', b''}
GEOGRAPHIC_TYPES = {
    b'"Q82794"', b'"Q515"', b'"Q486972"', b'"Q532"', b'"Q7275"', b'"Q6256"',
    b'"Q23442"', b'"Q8502"', b'"Q4022"', b'"Q23397"', b'"Q34763"', b'"Q33837"'
}
NON_GEOGRAPHIC_TYPES = {b'"Q5"', b'"Q35127"', b'"Q4167836"', b'"Q13442814"'}


def stream_wikidata_fast(file_path):
    """Generator yielding Wikidata entities - OPTIMIZED."""
    with gzip.open(file_path, 'rb') as f:  # Binary mode for orjson
        for line in f:
            line = line.strip()
            if line in SKIP_BYTES or line == b'[' or line == b']':
                continue
            if line.endswith(b','):
                line = line[:-1]

            # Quick pre-filter: skip if no geographic markers at all — but KEEP
            # lines carrying P1014 (Getty AAT ID), which sit on non-geographic
            # type-concept entities and feed the AAT crosswalk side-output.
            if (b'"P625"' not in line and b'"P3896"' not in line
                    and b'"P31"' not in line and b'"P1014"' not in line):
                continue

            try:
                yield orjson.loads(line)
            except:
                continue


def is_geographic_entity_fast(entity, entity_bytes):
    """Check if geographic - OPTIMIZED with byte scanning."""
    claims = entity.get('claims')
    if not claims:
        return False

    # Fast byte check for P31 (instance of)
    if b'"P31"' in entity_bytes:
        # Check for non-geographic types first (faster rejection)
        for non_geo in NON_GEOGRAPHIC_TYPES:
            if non_geo in entity_bytes:
                return False

        # Check for geographic types
        for geo in GEOGRAPHIC_TYPES:
            if geo in entity_bytes:
                return True

    # Has coordinates?
    if 'P625' in claims:
        return True

    # Has geoshape?
    if 'P3896' in claims:
        return True

    return False


def extract_coordinates_fast(claims):
    """Extract coordinates - OPTIMIZED."""
    p625 = claims.get('P625')
    if not p625:
        return None

    for claim in p625:
        try:
            coords = claim['mainsnak']['datavalue']['value']
            return [coords['longitude'], coords['latitude']]
        except (KeyError, TypeError):
            continue

    return None


def extract_geoshape_ref_fast(claims):
    """Extract geoshape reference - OPTIMIZED."""
    p3896 = claims.get('P3896')
    if not p3896:
        return None

    for claim in p3896:
        try:
            value = claim['mainsnak']['datavalue']['value']
            if isinstance(value, str) and value.startswith('Data:'):
                return value[5:]
        except (KeyError, TypeError):
            continue

    return None


def extract_simple_field(claims, prop):
    """Generic extractor for simple string/number fields - OPTIMIZED."""
    data = claims.get(prop)
    if not data:
        return None

    try:
        return data[0]['mainsnak']['datavalue']['value']
    except (KeyError, TypeError, IndexError):
        return None


def extract_population_fast(claims):
    """Extract population - OPTIMIZED."""
    p1082 = claims.get('P1082')
    if not p1082:
        return None

    for claim in p1082:
        try:
            amount = claim['mainsnak']['datavalue']['value']['amount']
            return int(float(amount.lstrip('+')))
        except (KeyError, TypeError, ValueError):
            continue

    return None


def _year_from_wikidata_time(time_str):
    """Parse a Wikidata time string into an integer year.

    Format is ``+YYYY-MM-DDTHH:MM:SSZ`` (CE) or ``-YYYY-MM-DDTHH:MM:SSZ``
    (BCE), with variable-width year for deep past. Returns ``None`` for
    unparseable input.
    """
    if not isinstance(time_str, str) or len(time_str) < 5:
        return None
    sign = time_str[0]
    if sign not in ("+", "-"):
        return None
    rest = time_str[1:]
    dash = rest.find("-")
    if dash <= 0:
        return None
    try:
        year = int(rest[:dash])
    except ValueError:
        return None
    return -year if sign == "-" else year


def _earliest_year(claims, prop):
    years = []
    for claim in claims.get(prop) or ():
        try:
            year = _year_from_wikidata_time(
                claim["mainsnak"]["datavalue"]["value"]["time"]
            )
        except (KeyError, TypeError):
            continue
        if year is not None:
            years.append(year)
    return min(years) if years else None


def _latest_year(claims, prop):
    years = []
    for claim in claims.get(prop) or ():
        try:
            year = _year_from_wikidata_time(
                claim["mainsnak"]["datavalue"]["value"]["time"]
            )
        except (KeyError, TypeError):
            continue
        if year is not None:
            years.append(year)
    return max(years) if years else None


def build_wikidata_timespans(claims):
    """Build a ``timespans`` list from Wikidata temporal claims.

    Properties consulted:
      * P571 — inception (start)
      * P580 — start time (alternative start)
      * P576 — dissolved, abolished or demolished (end)
      * P582 — end time (alternative end)
      * P585 — point in time (single-point fallback for both ends)

    Returns ``[]`` when no usable temporal data is present, so docs without
    historical metadata don't drag the corpus-wide temporal extent toward
    today's date.
    """
    start = _earliest_year(claims, "P571")
    if start is None:
        start = _earliest_year(claims, "P580")
    end = _latest_year(claims, "P576")
    if end is None:
        end = _latest_year(claims, "P582")
    if start is None and end is None:
        # Fall back to a single point-in-time reference; otherwise skip.
        point = _earliest_year(claims, "P585")
        if point is None:
            return []
        return [{"start": {"in": point}, "end": {"in": point}}]
    ts = {}
    if start is not None:
        ts["start"] = {"in": start}
    if end is not None:
        ts["end"] = {"in": end}
    return [ts]


def create_place_doc_fast(entity, entity_bytes):
    """Create place document - OPTIMIZED."""
    qid = entity.get('id')
    if not qid:
        return None, None

    # Quick geographic check with byte scanning
    if not is_geographic_entity_fast(entity, entity_bytes):
        return None, None

    claims = entity.get('claims', {})

    # Extract coordinates and geoshape
    coords = extract_coordinates_fast(claims)
    geoshape_ref = extract_geoshape_ref_fast(claims)

    # Must have spatial data
    if not coords and not geoshape_ref:
        return None, None

    # Extract labels efficiently
    labels = entity.get('labels', {})
    title = labels.get('en', {}).get('value') or labels.get('mul', {}).get('value') or qid

    # Build toponyms — attach the entity-level timespan when available so
    # toponym attestations span the period the place actually existed (real
    # historical settlements, dissolved kingdoms, etc.). Authorities without
    # temporal data emit no timespans, leaving them out of the corpus-wide
    # extent rather than pinning them to today.
    timespans = build_wikidata_timespans(claims)

    toponyms = []
    seen = set()

    for lang, label_obj in labels.items():
        name = label_obj.get('value')
        if not name:
            continue
        lst = f"{name}@{lang}"
        if lst not in seen:
            entry = {'toponym_id': lst}
            if timespans:
                entry['timespans'] = timespans
            toponyms.append(entry)
            seen.add(lst)

    # Add aliases
    for lang, alias_list in entity.get('aliases', {}).items():
        for alias_obj in alias_list:
            name = alias_obj.get('value')
            if not name:
                continue
            lst = f"{name}@{lang}"
            if lst not in seen:
                entry = {'toponym_id': lst}
                if timespans:
                    entry['timespans'] = timespans
                toponyms.append(entry)
                seen.add(lst)

    # Build base document
    doc = {
        'place_id': f"wd:{qid}",
        'title': title,
        'toponyms': toponyms
    }

    # Add geometries — propagate timespans onto each geometry entry too so
    # gazetteer_temporal_extent picks them up alongside the toponyms.
    if coords:
        point_geom = {'type': 'Point', 'coordinates': coords}
        geom_entry = enrich_geometry(point_geom, timespans=timespans or None)
        if geom_entry:
            doc['geometries'] = [geom_entry]

    # Add optional fields
    ccodes = []
    if 'P297' in claims:
        for claim in claims['P297']:
            try:
                code = claim['mainsnak']['datavalue']['value']
                if code:
                    ccodes.append(code)
            except (KeyError, TypeError):
                pass
    if ccodes:
        doc['ccodes'] = ccodes

    # Population
    population = extract_population_fast(claims)
    if population is not None:
        doc['population'] = population

    # Elevation
    if 'P2044' in claims:
        try:
            elev = claims['P2044'][0]['mainsnak']['datavalue']['value']['amount']
            doc['elevation'] = int(float(elev))
        except (KeyError, TypeError, ValueError, IndexError):
            pass

    # Types
    types = []
    if 'P31' in claims:
        for claim in claims['P31']:
            try:
                qid_type = claim['mainsnak']['datavalue']['value']['id']
                types.append({
                    'identifier': qid_type,
                    'label': 'wikidata',
                    'sourceLabel': qid_type
                })
            except (KeyError, TypeError):
                pass
    if types:
        doc['types'] = types

    # Relations (GeoNames)
    gn_id = extract_simple_field(claims, 'P1566')
    if gn_id:
        doc['relations'] = [{
            'relation_type': 'sameAs',
            'related_place_id': f"gn:{gn_id}",
            'label': 'GeoNames'
        }]

    # Wikipedia links from Wikidata sitelinks. `seeAlso` is the LPF/SKOS-neutral
    # choice for an article URL (NOT closeMatch/exactMatch — those are reserved
    # for authority co-references). O(sitelinks), allocation-light: this runs in
    # the hot per-entity loop over the whole dump.
    wiki_links = []
    for site, sl in (entity.get('sitelinks') or {}).items():
        if not site.endswith('wiki'):  # skip wikiquote/wikivoyage/commons/etc.
            continue
        lang = site[:-4]  # 'enwiki' -> 'en'
        if lang not in WIKIPEDIA_SITELINK_LANGS:
            continue
        title = sl.get('title')
        if not title:
            continue
        url = f"https://{lang}.wikipedia.org/wiki/" + quote(title.replace(' ', '_'))
        wiki_links.append({'type': 'seeAlso', 'identifier': url})
    if wiki_links:
        doc['links'] = wiki_links

    return doc, geoshape_ref


def stage_wikidata(file_path, geoshape_refs_file, p1014_file=WIKIDATA_P1014_FILE):
    """Process Wikidata dump and stage records to /vast/ishi/staged/wd/extract/.

    Emits two side-outputs during the single (expensive) dump scan:
      * ``geoshape_refs_file`` — ``{qid, geoshape_ref}`` for P3896 geoshapes.
      * ``p1014_file`` — the Wikidata→Getty AAT crosswalk (``{qid, aat_id}``),
        extracted independently of the geographic place filter (the P1014-bearing
        type-concept entities are not places). Consumed by ``aat_mapper wikidata``.
    """
    place_count = 0
    processed = 0
    skipped = 0
    geoshape_count = 0
    p1014_count = 0

    print("Starting Wikidata processing (OPTIMIZED with orjson)...")
    print(f"Saving geoshape references to: {geoshape_refs_file}")
    print(f"Saving P1014 (AAT) crosswalk to: {p1014_file}")

    os.makedirs(os.path.dirname(geoshape_refs_file), exist_ok=True)
    os.makedirs(os.path.dirname(p1014_file), exist_ok=True)

    with open(geoshape_refs_file, 'wb') as refs_f, open(p1014_file, 'wb') as p1014_f:
        for entity in stream_wikidata_fast(file_path):
            processed += 1

            if processed % 100000 == 0:
                sys.stdout.write(
                    f"\rProcessed {processed:,} entities... "
                    f"(places: {place_count:,}, geoshapes: {geoshape_count:,}, "
                    f"p1014: {p1014_count:,}, skipped: {skipped:,})")
                sys.stdout.flush()

            # AAT crosswalk side-output — BEFORE the place filter, since the
            # type-concept entities that carry P1014 are non-geographic and get
            # dropped by create_place_doc_fast.
            try:
                qid_e = entity.get('id')
                if qid_e:
                    for aat_id in extract_p1014_ids(entity.get('claims', {})):
                        p1014_f.write(orjson.dumps({'qid': qid_e, 'aat_id': aat_id}) + b'\n')
                        p1014_count += 1
            except Exception:
                pass

            try:
                entity_bytes = orjson.dumps(entity)
                place_doc, geoshape_ref = create_place_doc_fast(entity, entity_bytes)

                if not place_doc:
                    skipped += 1
                    continue

                qid = entity['id']
                if geoshape_ref:
                    ref_doc = {'qid': qid, 'geoshape_ref': geoshape_ref}
                    refs_f.write(orjson.dumps(ref_doc) + b'\n')
                    geoshape_count += 1

                write_staged_place_doc("wd", place_doc)
                place_count += 1

            except Exception:
                skipped += 1
                continue

    print(f"\n\nStaging complete!")
    print(f"Total entities processed: {processed:,}")
    print(f"Places staged: {place_count:,}")
    print(f"Geoshape references saved: {geoshape_count:,}")
    print(f"P1014 (AAT) crosswalk rows saved: {p1014_count:,}")
    print(f"Skipped (non-geographic): {skipped:,}")


if __name__ == "__main__":
    WIKIDATA_FILE = f"{DATA_DIR}/wikidata/latest-all/latest-all.json.gz"

    print(f"Starting to stage Wikidata from {WIKIDATA_FILE}")
    print(f"Saving geoshape references to: {GEOSHAPE_REFS_FILE}")
    print(f"Saving P1014 (AAT) crosswalk to: {WIKIDATA_P1014_FILE}\n")

    stage_wikidata(WIKIDATA_FILE, GEOSHAPE_REFS_FILE, WIKIDATA_P1014_FILE)
