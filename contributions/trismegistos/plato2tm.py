"""The inverse of tm2plato.py: PLATO JSON Lines back to Trismegistos Geo's own fields, compared with
tm_geo.db field by field (place-attestation-ontology #16).

    python3 contributions/trismegistos/plato2tm.py PLATO.jsonl [--report OUT.json]

A field compares in the form tm2plato.py reads it: names as their variants (the order and the
brackets TM writes them in are presentation), a status as its ';' segments, a date as its two
years and its written form. Every field TM has is listed with how many records it matched in, out
of how many have it, so that a field the comparison does not read cannot pass for one that matched.
Exit status 1 when any field the conversion claims to carry differs.
"""
import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tm2plato import (DB, P, PLACE, IDENTITY, TM, GAZETTEER, TITLE, DESCRIPTION, CITO, DEMOTIC, EGYPTOLOGICAL, LAND_OF_A_PEOPLE, NOME,  # noqa: E402
                      variants, year, country_codes, located, citation, is_people)

CCODES = country_codes()
LESS = P + 'LessCertain'

# What tm2plato.py claims to carry, field by field; the rest it says it does not (see README.md).
CARRIED = ['standard_name', 'latin_name', 'greek_unicode', 'egyptian_unicode', 'coptic_unicode', 'ethnicon', 'status',
           'coordinates', 'location', 'province', 'nomos_code', 'begin_date', 'end_date', 'begin_date_fmt', 'end_date_fmt',
           'country', 'georelations']
NOT_CARRIED = {'region': 'not mapped (it repeats the province outside Egypt, and is a code in Egypt)',
               'full_name': 'only its modern name in brackets is carried; the rest repeats country, region and name'}


def doubted(a):
    return a.get('certaintyLevel') == LESS


def from_plato(rec):
    """One PLATO record -> TM's fields, as far as the PLATO says them."""
    out = collections.defaultdict(list)
    for a in rec.get('attestations', []):
        for n in a.get('names', []):
            form = n.get('sourceLabel') or n['toponym']
            if a.get('formStatus') == P + 'Headword':
                out['standard_name'].append(form)
            elif 'demonym' in (n.get('nameType') or []):
                out['ethnicon'].append(form)
            elif n.get('language') == 'la':
                out['latin_name'].append(form)
            elif n.get('language') == 'grc':
                out['greek_unicode'].append(form)
            elif n.get('language') == DEMOTIC:
                out['egyptian_unicode'].append(form)
            elif n.get('language') == 'cop':
                out['coptic_unicode'].append(form)
        # The attestation window: the one attestation whose timespans are the span of the evidence.
        for ts in a.get('timespans') or []:
            out['window'].append((a.get('timespanRole'), ts.get('startEarliest'), ts.get('endLatest'), ts.get('sourceLabel')))
        if 'timespanRole' in a and not a.get('timespans'):
            out['window'].append((a.get('timespanRole'), None, None, None))
        for t in a.get('types', []):
            out['status'].append(t.get('sourceLabel') or t['label'] + ('?' if doubted(t.get('qualification') or {}) else ''))
        for g in a.get('geometries', []):
            if 'geojson' in g:
                # Carried twice, as TM's text and as GeoJSON numbers: they must agree, or neither is trusted.
                lon, lat = g['geojson']['coordinates']
                text = g.get('sourceLabel') or ''
                agree = [float(x) for x in text.split(',')] == [lat, lon] if ',' in text else False
                out['coordinates'].append(text if agree else f'{text} (GeoJSON {lon},{lat} disagrees)')
            else:
                out['location'].append(g.get('sourceLabel'))
        for r in a.get('relations', []):
            label = r.get('relatedLabel', '')
            if (r.get('relatesTo') or '').startswith(GAZETTEER):
                # A minted province or nome; doubt about a nome code is doubt about the relation.
                if label.startswith('nome '):
                    out['nomos_code'].append(label[5:] + ('?' if doubted(a) else ''))
                else:
                    out['province'].append(label + ('?' if doubted(a) else ''))
            elif 'relationLabel' in r:
                out['location'].append(r['relationLabel'])
            else:
                out['country_doubt'].append(label + ('?' if doubted(a) else ''))
    for i in rec.get('identityRelations', []):
        out['georelations'].append(i['object'])
    out['ccodes'] = rec.get('ccodes') or []
    out['location'] = sorted(set(out['location']))
    return out


def from_tm(r, rel, wd):
    """TM's own fields, in the form the comparison reads them."""
    f = {}
    f['standard_name'] = [r['standard_name']] if r['standard_name'] else []
    f['latin_name'] = variants(r['latin_name'])
    g = r['greek_unicode']
    f['greek_unicode'] = [] if g and re.search(r'[A-Za-z]', g) and not re.search(r'[Ͱ-Ͽἀ-῿]', g) else variants(g)
    f['egyptian_unicode'] = variants(r['egyptian_unicode'])
    f['coptic_unicode'] = variants(r['coptic_unicode'])
    f['ethnicon'] = variants(r['ethnicon'])
    f['status'] = [s.strip() for s in r['status'].split(';') if s.strip()]
    f['coordinates'] = [r['coordinates']] if r['coordinates'] else []
    f['location'] = [r['location']] if r['location'] and r['location'].lower() != 'unknown' else []
    f['province'] = [r['province']] if r['province'] and r['province'] != 'N/A' else []
    f['nomos_code'] = [r['nomos_code']] if r['nomos_code'] and NOME.fullmatch(r['nomos_code']) else []
    objs = set()
    for partner, pid in rel:
        if partner == 'wikipedia' and pid in wd:
            partner, pid = 'wikidata', wd[pid]
        if partner in IDENTITY:
            objs.add(IDENTITY[partner].format(pid))
    f['georelations'] = sorted(objs)
    return f


SCRIPT = {'grc': ('Grek', None), 'cop': ('Copt', None), DEMOTIC: ('Latn', EGYPTOLOGICAL)}
HEADER = {'@id': GAZETTEER, 'title': TITLE, 'description': DESCRIPTION, 'licence': 'https://creativecommons.org/licenses/by-sa/4.0/', 'status': 'draft'}


def no_ids(a):
    return {k: v for k, v in a.items() if k != '@id'}


def disagreements(rec, r, units):
    """Everything the PLATO says twice, or derives from a value it also keeps, must agree with it: a
    value carried twice and read once is never verified. Returns what does not, and records which
    minted units this record points at, for the check of the units themselves."""
    out = []
    people = is_people(r['status'])
    want = r['standard_name'] or r['latin_name'] or r['full_name']
    if rec.get('label') != want:
        out.append(('label', rec.get('label'), want))
    if rec.get('entityIdentifier') != str(r['tm_geo_id']) or rec.get('namespace') != 'https://www.trismegistos.org/geo/':
        out.append(('identifier', rec.get('entityIdentifier'), rec.get('namespace')))
    cite = citation(r['tm_geo_id'])
    positions = []
    for a in rec.get('attestations', []):
        for c in a.get('citations', []):
            src = c.get('source') if isinstance(c.get('source'), dict) else {'@id': c.get('source')}
            if (src.get('@id'), src.get('title'), c.get('locator'), c.get('citationFunction')) != (TM['@id'], TM['title'], f"geo {r['tm_geo_id']}", CITO + 'citesAsDataSource'):
                out.append(('citation', src.get('@id'), src.get('title'), c.get('locator'), c.get('citationFunction')))
        for n in a.get('names', []):
            script, system = SCRIPT.get(n.get('language'), (None, None))
            if (n.get('script'), n.get('transliterationSystem')) != (script, system):
                out.append(('script', n.get('language'), n.get('script'), n.get('transliterationSystem')))
            # A people's record is the land it lived in: every name but the demonyms is toponym and ethnonym.
            kind = n.get('nameType')
            if kind != ['demonym'] and kind != (['toponym', 'ethnonym'] if people else None):
                out.append(('nameType', n.get('toponym'), kind, people))
        for t in a.get('types', []):
            if t.get('sourceLabel'):
                cls = t['sourceLabel'].split(':')[0].replace('?', '').strip()
                if t['label'] != (LAND_OF_A_PEOPLE if cls == 'people' else cls):
                    out.append(('type label', t['label'], t['sourceLabel']))
            elif t['label'] == LAND_OF_A_PEOPLE:
                out.append(('type label', t['label'], None))
        for rl in a.get('relations', []):
            label = rl.get('relatedLabel', '')
            if (rl.get('relatesTo') or '').startswith(GAZETTEER):
                kind, key = ('nome', label[5:]) if label.startswith('nome ') else ('province', label)
                iri = f"{GAZETTEER}/{kind}/{re.sub(r'[^A-Za-z0-9]+', '-', key).strip('-')}"
                if rl.get('relatesTo') != iri or rl.get('relationType') != P + 'ContainedIn' or 'relationLabel' in rl:
                    out.append(('relation', rl.get('relatesTo'), iri))
                units[iri].add(r['tm_geo_id'])
            elif 'relationLabel' not in rl:
                # Doubt about the country: a relation by name alone, with the doubt on the attestation.
                if (rl.get('relationType'), label, rl.get('relatesTo'), doubted(a), r['country'].endswith('?')) != (P + 'ContainedIn', r['country'].rstrip('?').strip(), None, True, True) or rec.get('ccodes'):
                    out.append(('country doubt', rl, r['country'], rec.get('ccodes')))
        if any('geojson' not in g for g in a.get('geometries', [])) or any('relationLabel' in rl for rl in a.get('relations', [])):
            positions.append(no_ids(a))
        if (a.get('timespans') or 'timespanRole' in a) and set(a) - {'timespans', 'timespanRole', 'citations', '@id'}:
            out.append(('window', 'not an attestation of the timespan alone', sorted(a)))
    # A location in words: its positions must be exactly what the words give, with the words on each.
    if r['location'] and r['location'].lower() != 'unknown':
        expect = sorted(json.dumps(a, sort_keys=True) for a in located(r['location'], cite)[0])
        got = sorted(json.dumps(a, sort_keys=True) for a in positions)
        if expect != got:
            out.append(('location', got[:2], expect[:2]))
    elif positions:
        out.append(('location', 'positions with no location', len(positions)))
    # An identity link: its basis names the partner whose address pattern its object follows (a
    # Wikipedia slug resolved to Wikidata is a Wikidata link), from TM, of unspecified type.
    for i in rec.get('identityRelations', []):
        partner = next((k for k, v in IDENTITY.items() if i['object'].startswith(v.split('{}')[0])), None)
        src = i.get('source') if isinstance(i.get('source'), dict) else {'@id': i.get('source')}
        if (i.get('basis'), src.get('@id'), i.get('identityType')) != (f'Trismegistos GeoRelations ({partner})', TM['@id'], 'unspecified'):
            out.append(('identity', i.get('basis'), src.get('@id'), i.get('identityType')))
    return out


def check_units(units, records):
    """The minted units: each must be in the file with the name or code TM gives it, attested from
    the first TM record that names it, and saying in how many records it is named."""
    bad = []
    for iri, ids in sorted(units.items()):
        u = records.get(iri)
        kind, key = iri[len(GAZETTEER) + 1:].split('/', 1)
        label = u.get('label') if u else None
        name = (label[5:] if kind == 'nome' else label) if label else None
        want = [{'names': [{'toponym': name}], 'citations': citation(min(ids)),
                 'notes': f'Trismegistos names this unit in {len(ids)} record{"s" if len(ids) != 1 else ""} without a record of its own, so it is minted as a place of this dataset.'}]
        got = [no_ids(a) for a in (u or {}).get('attestations', [])]
        if u is None or not label or re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-') != key or got != want:
            bad.append({'unit': iri, 'got': got[:1], 'want': want})
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('plato')
    ap.add_argument('--report')
    ap.add_argument('--only-present', action='store_true', help='compare only the TM records the file holds (a sample)')
    a = ap.parse_args()
    conn = sqlite3.connect(f'file:{DB}?immutable=1', uri=True)
    conn.row_factory = sqlite3.Row
    rel = collections.defaultdict(list)
    for t, p, i in conn.execute('SELECT tm_geo_id, partner, partner_id FROM georelations'):
        rel[t].append((p, i))
    wd = {s: q for s, q in conn.execute('SELECT slug, qid FROM _wikidata_resolved') if q}
    plato, others, head = {}, {}, {}
    with open(a.plato, encoding='utf-8') as fh:
        for line in fh:
            rec = json.loads(line)
            iri = rec.get('@id', '')
            if 'gazetteer' in rec:
                head = rec['gazetteer']
            elif iri.startswith(PLACE.format('')):
                plato[int(iri[len(PLACE.format('')):])] = rec
            else:
                others[iri] = rec
    have, match, unmapped = collections.Counter(), collections.Counter(), collections.Counter()
    diffs = collections.defaultdict(list)
    units = collections.defaultdict(set)
    missing = 0
    for r in map(dict, conn.execute('SELECT * FROM geo ORDER BY tm_geo_id')):
        rec = plato.get(r['tm_geo_id'])
        if rec is None:
            missing += not a.only_present
            continue
        mine, theirs = from_plato(rec), from_tm(r, rel[r['tm_geo_id']], wd)
        have['agreement'] += 1
        dis = disagreements(rec, r, units)
        match['agreement'] += not dis
        if dis and len(diffs['agreement']) < 5:
            diffs['agreement'].append({'tm': r['tm_geo_id'], 'disagree': dis[:3]})
        for k in CARRIED:
            if k in ('begin_date', 'end_date', 'begin_date_fmt', 'end_date_fmt', 'country'):
                continue
            if theirs[k] or mine[k]:
                have[k] += 1
                ok = sorted(theirs[k]) == sorted(mine[k])
                match[k] += ok
                if not ok and len(diffs[k]) < 5:
                    diffs[k].append({'tm': r['tm_geo_id'], 'source': theirs[k], 'plato': mine[k]})
        # The attestation window: one attestation, EvidenceSpan, the same two years and written form;
        # and no other attestation dated at all.
        if r['begin_date'] or r['end_date'] or mine['window']:
            have['dates'] += 1
            written = r['begin_date_fmt'] if r['begin_date_fmt'] == r['end_date_fmt'] else f"{r['begin_date_fmt']} - {r['end_date_fmt']}"
            want = [(P + 'EvidenceSpan', year(r['begin_date']), year(r['end_date']), written)] if r['begin_date'] or r['end_date'] else []
            ok = mine['window'] == want
            match['dates'] += ok
            if not ok and len(diffs['dates']) < 5:
                diffs['dates'].append({'tm': r['tm_geo_id'], 'source': want, 'plato': mine['window']})
        if r['country'] and r['country'] != 'ghost name':
            have['country'] += 1
            doubt = r['country'].endswith('?')
            code = CCODES.get(r['country'].rstrip('?').strip())
            if doubt:
                ok = mine['ccodes'] == [] and mine['country_doubt'] == [r['country']]
            else:
                ok = mine['ccodes'] == ([code] if code else []) and not mine['country_doubt']
            match['country'] += ok
            if not ok and len(diffs['country']) < 5:
                diffs['country'].append({'tm': r['tm_geo_id'], 'source': r['country'], 'plato': (mine['ccodes'], mine['country_doubt'])})
            if not code and not doubt:
                unmapped[r['country']] += 1
    have['header'] = 1
    match['header'] = int(all(head.get(k) == v for k, v in HEADER.items()))
    if not match['header']:
        diffs['header'].append({k: head.get(k) for k in ('@id', 'title', 'licence', 'status')})
    have['units'] = len(units)
    bad_units = check_units(units, others)
    match['units'] = len(units) - len(bad_units)
    diffs['units'] = bad_units[:5]
    total = conn.execute('SELECT COUNT(*) FROM geo').fetchone()[0]
    print(f'{len(plato)} of {total} TM records found in {a.plato}, with {len(others)} other places' + (' (a sample: only those compared)' if a.only_present else ''))
    bad = False
    for k in sorted(have):
        flag = '' if match[k] == have[k] else '   DIFFERS'
        bad |= bool(flag)
        print(f'  {k:18} {match[k]:6} of {have[k]:6} {"units" if k == "units" else "records"} match{flag}')
        for d in diffs[k][:3]:
            print(f'      e.g. {d}')
    if unmapped:
        print(f'  country: {sum(unmapped.values())} records name a country with no ISO code, so ccodes cannot carry it: {dict(unmapped.most_common(8))}')
    for k, why in NOT_CARRIED.items():
        print(f'  {k:18} not compared: {why}')
    if a.report:
        Path(a.report).write_text(json.dumps({'have': have, 'match': match, 'diffs': diffs, 'missing': missing}, ensure_ascii=False, indent=1))
    return 1 if bad or missing else 0


if __name__ == '__main__':
    sys.exit(main())
