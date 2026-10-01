"""Trismegistos Geo to PLATO JSON Lines: a conformance test of PLATO (place-attestation-ontology #16).

Reads authorities/trismegistos/tm_geo.db in place (opened immutable, so no side files are made),
and writes one place-centric PLATO JSON Lines file, with a report of every value that could not
be said in PLATO, or only by stretching a term. See README.md for the mapping and its reasons.

    python3 contributions/trismegistos/tm2plato.py --out /tmp/tm/tm-geo.jsonl [--limit N]

Written against PLATO 7720890, which ruled on what the first pass found (#18 to #22): a relation
may name its target by relatedLabel alone, relativeTo takes two anchors for "between", an
attestation window is one attestation with timespanRole EvidenceSpan, a transliteration is a
toponym, and a people's record is the land it lived in.

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
# Demotic, known only in Egyptological transliteration: BCP 47 with the transform extension (#21).
DEMOTIC = 'egy-Latn-t-egy-egyd'
EGYPTOLOGICAL = 'Egyptological transliteration'
# A record of a people is the land Trismegistos places it in (#22): its type keeps TM's word.
LAND_OF_A_PEOPLE = 'land of a people'
NOME = re.compile(r'(U|L|00)\d*[a-z]?\??')

# Partners whose record addresses follow a pattern we can vouch for. The others (EDH, Talbert's
# Peutinger, DASI, RIB …) are named by an identifier only; see the report.
IDENTITY = {
    'pleiades': 'https://pleiades.stoa.org/places/{}',
    'geonames': 'https://sws.geonames.org/{}/',
    'wikidata': 'http://www.wikidata.org/entity/{}',
    'syriaca': 'http://syriaca.org/place/{}',
}
BEARING = {'north': 0, 'northeast': 45, 'east': 90, 'southeast': 135, 'south': 180, 'southwest': 225, 'west': 270, 'northwest': 315}
DIRECTION = r'(north|south|east|west|northeast|northwest|southeast|southwest)\b'
KM = re.compile(r'(?:ca\.\s*)?(\d+(?:[.,]\d+)?)\s*km\s+' + DIRECTION)
DIR = re.compile(DIRECTION)
REF = re.compile(r'\((\d+)\)')

report = collections.Counter()
examples = collections.defaultdict(list)
UNIT = {}   # kind -> 'records' or 'links': the denominator each count is out of
WRITTEN = []  # the TM ids written, for the count of their links
UNITS = {}  # the administrative units met, by address -> [label, name, first TM record, records]


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


def status_classes(status):
    """'city: polis; village?: kome' -> ['city', 'village']: the class of each ';' segment, without doubt."""
    return [seg.split(':')[0].replace('?', '').strip() for seg in status.split(';') if seg.strip()]


def is_people(status):
    return 'people' in status_classes(status)


def types(status):
    """'city: polis; village: kome' -> one type per ';' segment: its class as the label, the
    segment as TM writes it as sourceLabel, and doubt where the class carries '?'. A people is not
    a SpatialEntity (#22): the record is the land it lived in, and the type says so, keeping TM's
    word in sourceLabel."""
    out = []
    for seg in [s.strip() for s in status.split(';') if s.strip()]:
        cls = seg.split(':')[0].strip()
        label = cls.replace('?', '').strip() or seg
        t = {'label': LAND_OF_A_PEOPLE if label == 'people' else label}
        if t['label'] != seg:
            t['sourceLabel'] = seg
        if '?' in cls:
            t['qualification'] = {'certaintyLevel': P + 'LessCertain'}
        out.append(t)
    return out


def clauses(text):
    """A location in words -> its clauses. TM separates positions with ';' and ', '; a clause that
    names two or more TM places is split on ' and ' too ('in the territory of A (1) and B (2)',
    'west of A (1) and north of B (2)'), except a 'between', whose 'and' joins its two ends."""
    out = []
    for seg in re.split(r';\s*|,\s+', text):
        seg = re.sub(r'^(and|or)\s+', '', seg.strip(), flags=re.I).strip()
        if not seg:
            continue
        if re.match(r'between\b', seg, re.I) or len(REF.findall(seg)) < 2:
            out.append(seg)
        else:
            out.extend(p.strip() for p in re.split(r'\s+and\s+', seg) if p.strip())
    return out


def listlike(clause):
    """'00b Philoteris (Wadfa) (1780)': a bare list of places, continuing the clause before it."""
    bare = re.sub(r'\([^()]*\)', '', re.sub(r'\([^()]*\)', '', clause))
    words = bare.replace('?', '').split()
    return bool(words) and all(w[0].isupper() or w[0].isdigit() or w in ('and', 'the', '/') for w in words)


def parse(clause):
    """One clause -> (kind, the relative qualification its words give)."""
    low = clause.lower()
    m = KM.match(low)
    if m:
        return 'distance and bearing', {'relativeDistance': round(float(m.group(1).replace(',', '.')) * 1000), 'relativeBearing': BEARING[m.group(2)]}
    d = DIR.match(low)
    if d:
        return 'bearing', {'relativeBearing': BEARING[d.group(1)]}
    if low.startswith('near'):
        return 'near', {'relativeQualifier': P + 'Near'}
    if re.match(r'in\b', low):
        return 'within', {}
    if low.startswith('between'):
        return 'between', {'relativeQualifier': P + 'BetweenXAndY'}
    return 'words', {}


def location(text):
    """A location in words -> the facets PLATO can hold of it, and what was understood, as
    (facets, kinds). A facet is ('geometry', qualification, doubted) or ('relation', relation, doubted).

    Each clause gives its own facets (#19: several places are several attestations, not one with
    many anchors): 'near X' a geometry Near X, one per place named; 'in X' a ContainedIn relation
    to X, or to X by name alone where X is no TM place (#18); a bearing or a distance a geometry
    with one anchor; 'between X and Y' a geometry BetweenXAndY with exactly two (#19); any other
    clause naming one place a geometry anchored on it. The rest stays in words. A clause ending
    '?' is doubted. The caller puts the words, in full, on every facet."""
    facets, kinds = [], collections.Counter()
    prev = None
    for cl in clauses(text):
        doubt = cl.endswith('?')
        refs = REF.findall(cl)
        if ' or ' in cl.lower():
            kinds['alternatives, in words'] += 1
            prev = None
            continue
        kind, q = parse(cl)
        if kind == 'words' and prev and refs and listlike(cl):
            kind, q = prev
        prev = (kind, q) if kind in ('near', 'within', 'bearing', 'distance and bearing') else None
        if kind == 'near' and refs:
            for ref in refs:
                facets.append(('geometry', {**q, 'relativeTo': PLACE.format(ref)}, doubt))
        elif kind == 'near':
            kind = 'near a region that is not a TM place, in words'
        elif kind == 'within' and refs:
            for ref in refs:
                facets.append(('relation', {'relationType': P + 'ContainedIn', 'relatesTo': PLACE.format(ref)}, doubt))
        elif kind == 'within':
            label = re.sub(r'^in\s+', '', cl, flags=re.I).rstrip('?').strip()
            facets.append(('relation', {'relationType': P + 'ContainedIn', 'relatedLabel': label}, doubt))
            kind = 'within a region known only by name'
        elif kind == 'between':
            if len(refs) == 2 and refs[0] != refs[1]:
                facets.append(('geometry', {**q, 'relativeTo': [PLACE.format(r) for r in refs]}, doubt))
            else:
                kind = 'between, without exactly two TM places: in words'
        elif kind in ('bearing', 'distance and bearing') and len(refs) == 1:
            facets.append(('geometry', {**q, 'relativeTo': PLACE.format(refs[0])}, doubt))
        elif kind in ('bearing', 'distance and bearing'):
            kind += ' with no single TM place to measure from: in words'
        elif len(refs) == 1:
            facets.append(('geometry', {'relativeTo': PLACE.format(refs[0])}, doubt))
            kind = 'words, anchored on the TM place they name'
        elif refs:
            kind = 'words naming several TM places, none a position'
        kinds[kind] += 1
    if '(but ' in text.lower() or 'according to' in text.lower():
        kinds['the sources disagree, in words'] += 1
    return facets, kinds


def located(text, cite):
    """The attestations of a location in words: one per facet, each carrying the words in full
    (as the geometry's sourceLabel, or the relation's relationLabel), or one geometry of words
    alone when nothing in them is a position PLATO can hold."""
    facets, kinds = location(text)
    out, seen = [], set()
    for kind, f, doubt in facets:
        if kind == 'geometry':
            q = {**f, **({'certaintyLevel': P + 'LessCertain'} if doubt else {})}
            a = {'geometries': [{'sourceLabel': text, 'qualification': q}]}
        else:
            a = {'relations': [{**f, 'relationLabel': text}]}
            if doubt:
                a['certaintyLevel'] = P + 'LessCertain'
        key = json.dumps(a, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append({**a, 'citations': cite})
    if not out:
        out.append({'geometries': [{'sourceLabel': text}], 'citations': cite})
    return out, kinds


def convert(r, rel, wd_resolved, ccodes):
    tm_id = r['tm_geo_id']
    ghost = r['country'] == 'ghost name'
    people = is_people(r['status'])
    rec = {'@id': PLACE.format(tm_id), 'label': r['standard_name'] or r['latin_name'] or r['full_name'],
           'entityIdentifier': str(tm_id), 'namespace': 'https://www.trismegistos.org/geo/', 'attestations': []}
    cite = citation(tm_id)
    attest = lambda **facets: rec['attestations'].append({**facets, 'citations': cite})
    false = {'transcriptionAccuracy': P + 'TranscriptionFalse'}

    # Names. The headword is TM's own filing form; the Latin, Greek, Egyptian and Coptic forms are
    # the forms the sources use. A people's names name the land too (#22): toponym and ethnonym.
    def names(forms, status=None, **extra):
        ns = []
        for f in forms:
            n = name(f, **extra)
            if people and 'nameType' not in n:
                n['nameType'] = ['toponym', 'ethnonym']
            if ghost:
                n['qualification'] = {**n.get('qualification', {}), **false}
            ns.append(n)
        if ns:
            a = {'names': ns}
            if status:
                a['formStatus'] = status
            rec['attestations'].append({**a, 'citations': cite})
    names([r['standard_name']] if r['standard_name'] else [], status=P + 'Headword')
    names(variants(r['latin_name']), language='la')
    greek = r['greek_unicode']
    if greek and re.search(r'[A-Za-z]', greek) and not re.search(r'[Ͱ-Ͽἀ-῿]', greek):
        note('greek_unicode: not Greek (a placeholder)', greek)
        greek = ''
    names(variants(greek), language='grc', script='Grek')
    # Demotic, known only in transliteration: a toponym in its own right (#21), tagged as such.
    names(variants(r['egyptian_unicode']), language=DEMOTIC, script='Latn', transliterationSystem=EGYPTOLOGICAL)
    if r['egyptian_unicode']:
        note(f'egyptian: known only in transliteration, a toponym tagged {DEMOTIC}')
    names(variants(r['coptic_unicode']), language='cop', script='Copt')
    modern = re.search(r'\(([^()]+)\)\s*$', r['full_name'])
    if modern and not ghost:
        names([modern.group(1)])
        note('full_name: a modern name, given without a date or language')
    if r['ethnicon']:
        names(variants(r['ethnicon']), nameType=['demonym'])

    # The attestation window: the span of the texts that mention the place, which is not the
    # dates of the place or of any name. One attestation, with the timespan alone (#20).
    if r['begin_date'] or r['end_date']:
        window = {'startEarliest': year(r['begin_date']), 'endLatest': year(r['end_date']),
                  'sourceLabel': r['begin_date_fmt'] if r['begin_date_fmt'] == r['end_date_fmt'] else f"{r['begin_date_fmt']} - {r['end_date_fmt']}"}
        attest(timespans=[{k: v for k, v in window.items() if v}], timespanRole=P + 'EvidenceSpan')
        note('dates: an attestation window, as EvidenceSpan')

    if r['status']:
        attest(types=types(r['status']))
        if people:
            note('status "people": the land of a people, its names toponym and ethnonym', tm_id)
    if r['coordinates']:
        lat, lon = (float(x) for x in r['coordinates'].split(','))
        attest(geometries=[{'geojson': {'type': 'Point', 'coordinates': [lon, lat]}, 'sourceLabel': r['coordinates']}])
    if r['location'] and r['location'].lower() != 'unknown':
        atts, kinds = located(r['location'], cite)
        rec['attestations'].extend(atts)
        for kind in kinds:
            note(f'location: {kind}', r['location'])
        if len(atts) > 1:
            note('location: several positions, as several attestations', r['location'])

    # Administrative units are given as names and codes, not as TM places. They recur and can be
    # listed, so each is minted as a place of this dataset, with a name attestation citing the
    # record that first names it (#18); the relation keeps the name or code exactly as given.
    units = []
    if r['province'] and r['province'] != 'N/A':
        units.append(('province', r['province'], r['province'], False))
    if r['nomos_code'] and NOME.fullmatch(r['nomos_code']):
        code = r['nomos_code'].rstrip('?')
        units.append(('nome', code, f'nome {code}', r['nomos_code'].endswith('?')))
    for kind, key, label, doubt in units:
        iri = f"{GAZETTEER}/{kind}/{re.sub(r'[^A-Za-z0-9]+', '-', key).strip('-')}"
        u = UNITS.setdefault(iri, [label, key, tm_id, 0])
        u[3] += 1
        a = {'relations': [{'relationType': P + 'ContainedIn', 'relatesTo': iri, 'relatedLabel': label}]}
        if doubt:
            # Doubt about the code is doubt about the whole relation (#16, finding 6).
            a['certaintyLevel'] = P + 'LessCertain'
            note('nomos: doubt ("?") on the code, as the relation\'s certaintyLevel', r['nomos_code'])
        attest(**a)
        note(f'{kind}: named or coded, not a TM place, so a place is minted for it')

    # The country: an ISO code where TM is sure of it. Where TM doubts it ('Israel?'), a bare code
    # would drop the doubt, so it is a ContainedIn relation by name with the doubt on the
    # attestation (#16, finding 6).
    country = r['country'].rstrip('?').strip()
    if not ghost and country:
        if r['country'].endswith('?'):
            attest(relations=[{'relationType': P + 'ContainedIn', 'relatedLabel': country}], certaintyLevel=P + 'LessCertain')
            note('country: doubt ("?"), as a ContainedIn relation by name with LessCertain', r['country'])
        elif ccodes.get(country):
            rec['ccodes'] = [ccodes[country]]
        else:
            note('country: no ISO code', country)

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


def unit_record(iri, label, key, first, count):
    """A minted administrative unit: a place of this dataset with the name or code TM gives it,
    attested from the record that first names it (#18)."""
    return {'@id': iri, 'label': label, 'attestations': [{
        'names': [{'toponym': key}], 'citations': citation(first),
        'notes': f'Trismegistos names this unit in {count} record{"s" if count != 1 else ""} without a record of its own, so it is minted as a place of this dataset.'}]}


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
                     "location LIKE 'between%(%)%(%)%'", "location LIKE 'in %' AND location NOT LIKE '%(%'",
                     "location LIKE 'near%(%),%(%)%'", "location LIKE 'in %(%), near%'", "location LIKE '%?'",
                     "location LIKE '%km%'", "location LIKE 'north%'", "location <> '' AND location NOT LIKE '%(%'",
                     "latin_name <> ''", "greek_unicode <> ''", "egyptian_unicode <> ''", "coptic_unicode <> ''",
                     "ethnicon <> ''", "status LIKE '%;%'", "status LIKE '%?%'", "status LIKE 'people%'", "status LIKE '%; people%'",
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
        # The administrative units, as places of this dataset, each with the name TM gives it.
        for iri, (label, key, first, count) in sorted(UNITS.items()):
            f.write(json.dumps(unit_record(iri, label, key, first, count), ensure_ascii=False) + '\n')
    total = conn.execute('SELECT COUNT(*) FROM geo').fetchone()[0]
    links = sum(len(rel[t]) for t in WRITTEN)
    print(f'{n} of {total} TM Geo records written to {out}, with {links} links to other gazetteers;'
          f' {len(UNITS)} administrative units minted as places')
    of = {'records': n, 'links': links}
    for k, c in sorted(report.items(), key=lambda x: (UNIT[x[0]], -x[1])):
        print(f'  {c:6} of {of[UNIT[k]]:6} {UNIT[k]:7}  {k}' + (f'   e.g. {examples[k]}' if examples[k] else ''))


if __name__ == '__main__':
    sys.exit(main())
