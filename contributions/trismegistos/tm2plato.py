"""Trismegistos Geo to PLATO JSON Lines: a conformance test of PLATO (place-attestation-ontology #16).

Reads authorities/trismegistos/tm_geo.db in place (opened immutable, so no side files are made),
and writes one place-centric PLATO JSON Lines file, with a report of every value that could not
be said in PLATO, or only by stretching a term. See README.md for the mapping and its reasons.

    python3 contributions/trismegistos/tm2plato.py --out /tmp/tm/tm-geo.jsonl [--limit N]

Every count in the report gives its denominator.
"""
import argparse
import ast
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE.parent.parent / 'authorities' / 'trismegistos' / 'tm_geo.db'
PLACES_PY = HERE.parent.parent / 'authorities' / 'trismegistos' / 'places.py'

P = 'https://w3id.org/plato#'
CITO = 'http://purl.org/spar/cito/'
PLACE = 'https://www.trismegistos.org/place/{}'
GAZETTEER = 'https://example.org/plato-proofs/trismegistos-geo'
TITLE = 'Trismegistos Geo in PLATO (a conformance test, not a publication)'
DESCRIPTION = ('Trismegistos Geo (dump of 8 April 2026), converted to PLATO to test PLATO against it '
               "(place-attestation-ontology issue #16). The data is Trismegistos's: https://www.trismegistos.org/.")
TM = {'@id': 'https://www.trismegistos.org/geo/', 'title': 'Trismegistos Geo', 'authorityType': 'dataset',
      'licence': 'https://creativecommons.org/licenses/by-sa/4.0/'}

# Partners whose record addresses follow a pattern we can vouch for. The others (EDH, Talbert's
# Peutinger, DASI, RIB …) are named by an identifier only; see the report.
IDENTITY = {
    'pleiades': 'https://pleiades.stoa.org/places/{}',
    'geonames': 'https://sws.geonames.org/{}/',
    'wikidata': 'http://www.wikidata.org/entity/{}',
    'syriaca': 'http://syriaca.org/place/{}',
}
BEARING = {'north': 0, 'northeast': 45, 'east': 90, 'southeast': 135, 'south': 180, 'southwest': 225, 'west': 270, 'northwest': 315}
REF = re.compile(r'\((\d+)\)')

report = collections.Counter()
examples = collections.defaultdict(list)
UNIT = {}   # kind -> 'records' or 'links': the denominator each count is out of
WRITTEN = []  # the TM ids written, for the count of their links
UNITS = {}  # the administrative units met, by address -> label: written as places of their own


def note(kind, example=None, unit='records'):
    UNIT[kind] = unit
    report[kind] += 1
    if example is not None and len(examples[kind]) < 3:
        examples[kind].append(example)


def country_codes():
    """WHG's own country-to-ISO map, read from places.py without importing it."""
    tree = ast.parse(PLACES_PY.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, 'id', '') == 'COUNTRY_TO_CCODE':
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == 'COUNTRY_TO_CCODE' for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit('COUNTRY_TO_CCODE not found in places.py')


def year(n):
    """TM's signed integer year (BC negative, 0 = none) as PLATO's isoOrYear: 500 BC is -0500."""
    return None if n == 0 else ('-' if n < 0 else '') + f'{abs(n):04d}'


def variants(text):
    """'X (Y - Z) - W?' -> ['X', 'Y', 'Z', 'W?']: TM joins variant forms with ' - ', and gives
    variants of a form in brackets; 'var.' and 'fem.' mark them."""
    out = []
    for part in re.split(r'\s+-\s+', re.sub(r'[()]', ' - ', text)):
        part = re.sub(r'^(var\.|fem\.)\s*', '', part.strip()).strip(' ,;')
        if part:
            out.append(part)
    return out


def qualified(form):
    """A name as TM writes it -> (toponym, qualification, sourceLabel). A trailing '?' is doubt;
    Leiden brackets mark a damaged name: letters inside [] are supplied, dots or a space are lost."""
    q = {}
    toponym = form
    if form.endswith('?'):
        toponym = form.rstrip('?').strip()
        q['certaintyLevel'] = P + 'LessCertain'
    gaps = re.findall(r'\[([^\]]*)\]|\(\s*\)', toponym)
    if gaps or '( )' in toponym:
        lost = any(not g or re.fullmatch(r'[.\s]*', g) for g in gaps) or '( )' in toponym
        q['transcriptionCompleteness'] = P + ('TranscriptionNonReconstructable' if lost else 'TranscriptionReconstructable')
    return toponym, q, (form if toponym != form else None)


def name(form, **extra):
    toponym, q, label = qualified(form)
    n = {'toponym': toponym, **extra}
    if label:
        n['sourceLabel'] = label
    if q:
        n['qualification'] = q
    return n


def citation(tm_id):
    return [{'source': TM, 'locator': f'geo {tm_id}', 'citationFunction': CITO + 'citesAsDataSource'}]


def types(status):
    """'city: polis; village: kome' -> one type per ';' segment: its class as the label, the
    segment as TM writes it as sourceLabel, and doubt where the class carries '?'."""
    out = []
    for seg in [s.strip() for s in status.split(';') if s.strip()]:
        cls = seg.split(':')[0].strip()
        t = {'label': cls.replace('?', '').strip() or seg}
        if t['label'] != seg:
            t['sourceLabel'] = seg
        if '?' in cls:
            t['qualification'] = {'certaintyLevel': P + 'LessCertain'}
        out.append(t)
    return out


def location(text):
    """A location in words -> a geometry with no coordinates: the words as sourceLabel, and as much
    of their meaning as PLATO's relative qualifiers hold. Returns (geometry, what was understood)."""
    g = {'sourceLabel': text}
    refs = REF.findall(text)
    low = text.lower()
    q = {}
    m = re.match(r'(?:ca\.\s*)?(\d+(?:[.,]\d+)?)\s*km\s+(north|south|east|west|northeast|northwest|southeast|southwest)\b', low)
    d = re.match(r'(north|south|east|west|northeast|northwest|southeast|southwest)\b', low)
    if m:
        q['relativeDistance'] = round(float(m.group(1).replace(',', '.')) * 1000)
        q['relativeBearing'] = BEARING[m.group(2)]
        kind = 'distance and bearing'
    elif d:
        q['relativeBearing'] = BEARING[d.group(1)]
        kind = 'bearing'
    elif low.startswith('near'):
        q['relativeQualifier'] = P + 'Near'
        kind = 'near'
    elif low.startswith('in '):
        q['relativeQualifier'] = P + 'Within'
        kind = 'within'
    elif low.startswith('between'):
        q['relativeQualifier'] = P + 'BetweenXAndY'
        kind = 'between'
    else:
        kind = 'words only'
    if refs:
        q['relativeTo'] = PLACE.format(refs[0])
        if len(refs) > 1:
            note('location: more anchors than relativeTo holds (only the first is kept)', text)
    elif q:
        note('location: a relation with no TM place to anchor it', text)
    if kind == 'between' and len(refs) != 2:
        note('location: "between" without exactly two TM places', text)
    if '(but ' in low or 'according to' in low:
        note('location: the sources disagree, in words', text)
    if q:
        g['qualification'] = q
    return g, kind


def convert(r, rel, wd_resolved, ccodes):
    tm_id = r['tm_geo_id']
    ghost = r['country'] == 'ghost name'
    rec = {'@id': PLACE.format(tm_id), 'label': r['standard_name'] or r['latin_name'] or r['full_name'],
           'entityIdentifier': str(tm_id), 'namespace': 'https://www.trismegistos.org/geo/', 'attestations': []}
    country = r['country'].rstrip('?').strip()
    if r['country'].endswith('?'):
        note('country: doubt ("?") has no place in ccodes, so it is dropped', r['country'])
    if not ghost and country:
        cc = ccodes.get(country)
        if cc:
            rec['ccodes'] = [cc]
        else:
            note('country: no ISO code', country)
    window = {}
    if r['begin_date'] or r['end_date']:
        window = {'startEarliest': year(r['begin_date']), 'endLatest': year(r['end_date']),
                  'sourceLabel': r['begin_date_fmt'] if r['begin_date_fmt'] == r['end_date_fmt'] else f"{r['begin_date_fmt']} - {r['end_date_fmt']}"}
        window = {k: v for k, v in window.items() if v}
    cite = citation(tm_id)
    attest = lambda **facets: rec['attestations'].append({**facets, 'citations': cite})
    false = {'transcriptionAccuracy': P + 'TranscriptionFalse'}

    # Names. The headword is TM's own filing form; the Latin, Greek, Egyptian and Coptic forms are
    # the forms the sources use. The attestation window goes on each of those (see README).
    def names(forms, status=None, window_too=True, **extra):
        ns = []
        for f in forms:
            n = name(f, **extra)
            if ghost:
                n['qualification'] = {**n.get('qualification', {}), **false}
            ns.append(n)
        if ns:
            a = {'names': ns}
            if status:
                a['formStatus'] = status
            if window and window_too:
                a['timespans'] = [window]
            rec['attestations'].append({**a, 'citations': cite})
    names([r['standard_name']] if r['standard_name'] else [], status=P + 'Headword', window_too=False)
    names(variants(r['latin_name']), language='la')
    greek = r['greek_unicode']
    if greek and re.search(r'[A-Za-z]', greek) and not re.search(r'[Ͱ-Ͽἀ-῿]', greek):
        note('greek_unicode: not Greek (a placeholder)', greek)
        greek = ''
    names(variants(greek), language='grc', script='Grek')
    names(variants(r['egyptian_unicode']), language='egy', script='Latn', transliterationSystem='Egyptological transliteration')
    if r['egyptian_unicode']:
        note('egyptian: known only in transliteration, so the toponym is not in its original script')
    names(variants(r['coptic_unicode']), language='cop', script='Copt')
    modern = re.search(r'\(([^()]+)\)\s*$', r['full_name'])
    if modern and not ghost:
        names([modern.group(1)], window_too=False)
        note('full_name: a modern name, given without a date or language')
    if r['ethnicon']:
        names(variants(r['ethnicon']), nameType=['demonym'])

    if r['status']:
        attest(types=types(r['status']))
        if r['status'].split(':')[0].strip().rstrip('?') == 'people':
            note('status "people": a population, recorded as a SpatialEntity of type people', tm_id)
    if r['coordinates']:
        lat, lon = (float(x) for x in r['coordinates'].split(','))
        attest(geometries=[{'geojson': {'type': 'Point', 'coordinates': [lon, lat]}, 'sourceLabel': r['coordinates']}])
    if r['location'] and r['location'].lower() != 'unknown':
        g, kind = location(r['location'])
        note(f'location: {kind}')
        attest(geometries=[g])
    # Administrative units are given as names and codes, not as TM places. A relation must name its
    # target by address, so each unit becomes a place of this dataset's own, minted here.
    units = []
    if r['province'] and r['province'] != 'N/A':
        units.append(('province', r['province'], r['province']))
    if r['nomos_code'] and re.fullmatch(r'(U|L|00)\d*[a-z]?\??', r['nomos_code']):
        code = r['nomos_code'].rstrip('?')
        units.append(('nome', code, f'nome {code}'))
        if r['nomos_code'].endswith('?'):
            note('nomos: doubt ("?") on the code has no place in a relation', r['nomos_code'])
    for kind, key, label in units:
        iri = f"{GAZETTEER}/{kind}/{re.sub(r'[^A-Za-z0-9]+', '-', key).strip('-')}"
        UNITS[iri] = label
        attest(relations=[{'relationType': P + 'ContainedIn', 'relatesTo': iri, 'relatedLabel': label}])
        note(f'{kind}: named or coded, not a TM place, so a place is minted for it')

    idrs, seen = [], set()
    for partner, pid in rel:
        if partner == 'wikipedia' and pid in wd_resolved:
            partner, pid = 'wikidata', wd_resolved[pid]
        if partner in IDENTITY:
            obj = IDENTITY[partner].format(pid)
            if obj not in seen:
                seen.add(obj)
                idrs.append({'object': obj, 'identityType': 'unspecified', 'basis': f'Trismegistos GeoRelations ({partner})', 'source': TM})
        else:
            note(f'georelation: no address for {partner}, so it cannot be an identity relation', f'{partner}:{pid}', unit='links')
    if idrs:
        rec['identityRelations'] = idrs
    if ghost:
        note('ghost name: kept, every name marked TranscriptionFalse')
    if not rec['attestations']:
        note('record with no attestation at all', tm_id)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--limit', type=int)
    ap.add_argument('--sample', type=int, help='at most N records for each feature, for quick runs such as the negative controls')
    a = ap.parse_args()
    conn = sqlite3.connect(f'file:{DB}?immutable=1', uri=True)
    conn.row_factory = sqlite3.Row
    rel = collections.defaultdict(list)
    for t, p, i in conn.execute('SELECT tm_geo_id, partner, partner_id FROM georelations ORDER BY 1, 2, 3'):
        rel[t].append((p, i))
    wd = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='_wikidata_resolved'").fetchone():
        cols = [c[1] for c in conn.execute('PRAGMA table_info(_wikidata_resolved)')]
        k, v = cols[0], next((c for c in cols if 'qid' in c.lower() or 'wikidata' in c.lower()), cols[1])
        wd = {s: q for s, q in conn.execute(f'SELECT {k}, {v} FROM _wikidata_resolved') if q}
    ccodes = country_codes()
    head = {'$schema': 'https://w3id.org/plato/schemas/place-centric.schema.json', 'profile': 'place-centric', 'gazetteer': {
        '@id': GAZETTEER, 'title': TITLE, 'description': DESCRIPTION,
        'licence': 'https://creativecommons.org/licenses/by-sa/4.0/', 'status': 'draft'}}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open('w', encoding='utf-8') as f:
        f.write(json.dumps(head, ensure_ascii=False) + '\n')
        if a.sample:
            # A few records of every kind the conversion handles, so that a control can reach each.
            wants = ["coordinates <> ''", "location LIKE 'near%'", "location LIKE 'in %'", "location LIKE 'between%'",
                     "location LIKE '%km%'", "location LIKE 'north%'", "location <> '' AND location NOT LIKE '%(%'",
                     "latin_name <> ''", "greek_unicode <> ''", "egyptian_unicode <> ''", "coptic_unicode <> ''",
                     "ethnicon <> ''", "status LIKE '%;%'", "status LIKE '%?%'", "status LIKE 'people%'",
                     "country = 'ghost name'", "country LIKE '%?'", "nomos_code LIKE 'U%'", "nomos_code LIKE '%?'",
                     "begin_date < 0", "begin_date <> end_date", "standard_name LIKE '%[%'", "standard_name LIKE '%?'",
                     "full_name LIKE '%)'", "tm_geo_id IN (SELECT tm_geo_id FROM georelations WHERE partner IN ('pleiades','geonames','wikidata','syriaca'))",
                     "tm_geo_id IN (SELECT tm_geo_id FROM georelations WHERE partner = 'wikipedia')"]
            ids = sorted({t for w in wants for t, in conn.execute(f'SELECT tm_geo_id FROM geo WHERE {w} ORDER BY tm_geo_id LIMIT {a.sample}')})
            rows = conn.execute(f"SELECT * FROM geo WHERE tm_geo_id IN ({','.join(map(str, ids))}) ORDER BY tm_geo_id")
        else:
            rows = conn.execute('SELECT * FROM geo ORDER BY tm_geo_id' + (f' LIMIT {a.limit}' if a.limit else ''))
        for r in rows:
            WRITTEN.append(r['tm_geo_id'])
            f.write(json.dumps(convert(dict(r), rel[r['tm_geo_id']], wd, ccodes), ensure_ascii=False) + '\n')
            n += 1
        # The administrative units, as referents: a label, and no evidence of their own.
        for iri, label in sorted(UNITS.items()):
            f.write(json.dumps({'@id': iri, 'label': label, 'attestations': []}, ensure_ascii=False) + '\n')
    total = conn.execute('SELECT COUNT(*) FROM geo').fetchone()[0]
    links = sum(len(rel[t]) for t in WRITTEN)
    print(f'{n} of {total} TM Geo records written to {out}, with {links} links to other gazetteers;'
          f' {len(UNITS)} administrative units minted as places')
    of = {'records': n, 'links': links}
    for k, c in sorted(report.items(), key=lambda x: (UNIT[x[0]], -x[1])):
        print(f'  {c:6} of {of[UNIT[k]]:6} {UNIT[k]:7}  {k}' + (f'   e.g. {examples[k]}' if examples[k] else ''))


if __name__ == '__main__':
    sys.exit(main())
