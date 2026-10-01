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
from tm2plato import DB, P, PLACE, IDENTITY, TM, GAZETTEER, TITLE, DESCRIPTION, CITO, variants, year, country_codes, location, types  # noqa: E402

CCODES = country_codes()

# What tm2plato.py claims to carry, field by field; the rest it says it does not (see README.md).
CARRIED = ['standard_name', 'latin_name', 'greek_unicode', 'egyptian_unicode', 'coptic_unicode', 'ethnicon', 'status',
           'coordinates', 'location', 'province', 'nomos_code', 'begin_date', 'end_date', 'begin_date_fmt', 'end_date_fmt',
           'country', 'georelations']
NOT_CARRIED = {'region': 'not mapped (it repeats the province outside Egypt, and is a code in Egypt)',
               'full_name': 'only its modern name in brackets is carried; the rest repeats country, region and name'}


def from_plato(rec):
    """One PLATO record -> TM's fields, as far as the PLATO says them."""
    out = collections.defaultdict(list)
    ccodes = rec.get('ccodes') or []
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
            elif n.get('language') == 'egy':
                out['egyptian_unicode'].append(form)
            elif n.get('language') == 'cop':
                out['coptic_unicode'].append(form)
            ts = a.get('timespans') or []
            if ts:
                out['window'].append((ts[0].get('startEarliest'), ts[0].get('endLatest'), ts[0].get('sourceLabel')))
        for t in a.get('types', []):
            out['status'].append(t.get('sourceLabel') or t['label'] + ('?' if (t.get('qualification') or {}).get('certaintyLevel') == P + 'LessCertain' else ''))
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
            out['nomos_code' if label.startswith('nome ') else 'province'].append(label[5:] if label.startswith('nome ') else label)
    for i in rec.get('identityRelations', []):
        out['georelations'].append(i['object'])
    out['ccodes'] = ccodes
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
    f['nomos_code'] = [r['nomos_code'].rstrip('?')] if r['nomos_code'] and re.fullmatch(r'(U|L|00)\d*[a-z]?\??', r['nomos_code']) else []
    objs = set()
    for partner, pid in rel:
        if partner == 'wikipedia' and pid in wd:
            partner, pid = 'wikidata', wd[pid]
        if partner in IDENTITY:
            objs.add(IDENTITY[partner].format(pid))
    f['georelations'] = sorted(objs)
    return f


SCRIPT = {'grc': ('Grek', None), 'cop': ('Copt', None), 'egy': ('Latn', 'Egyptological transliteration')}
HEADER = {'@id': GAZETTEER, 'title': TITLE, 'description': DESCRIPTION, 'licence': 'https://creativecommons.org/licenses/by-sa/4.0/', 'status': 'draft'}


def disagreements(rec, r):
    """Everything the PLATO says twice, or derives from a value it also keeps, must agree with it: a
    value carried twice and read once is never verified. Returns what does not."""
    out = []
    want = r['standard_name'] or r['latin_name'] or r['full_name']
    if rec.get('label') != want:
        out.append(('label', rec.get('label'), want))
    if rec.get('entityIdentifier') != str(r['tm_geo_id']) or rec.get('namespace') != 'https://www.trismegistos.org/geo/':
        out.append(('identifier', rec.get('entityIdentifier'), rec.get('namespace')))
    for a in rec.get('attestations', []):
        for c in a.get('citations', []):
            src = c.get('source') if isinstance(c.get('source'), dict) else {'@id': c.get('source')}
            if (src.get('@id'), src.get('title'), c.get('locator'), c.get('citationFunction')) != (TM['@id'], TM['title'], f"geo {r['tm_geo_id']}", CITO + 'citesAsDataSource'):
                out.append(('citation', src.get('@id'), src.get('title'), c.get('locator'), c.get('citationFunction')))
        for n in a.get('names', []):
            script, system = SCRIPT.get(n.get('language'), (None, None))
            if (n.get('script'), n.get('transliterationSystem')) != (script, system):
                out.append(('script', n.get('language'), n.get('script'), n.get('transliterationSystem')))
        for t in a.get('types', []):
            if t.get('sourceLabel') and t['label'] != t['sourceLabel'].split(':')[0].replace('?', '').strip():
                out.append(('type label', t['label'], t['sourceLabel']))
        for rl in a.get('relations', []):
            label = rl.get('relatedLabel', '')
            kind, key = ('nome', label[5:]) if label.startswith('nome ') else ('province', label)
            iri = f"{GAZETTEER}/{kind}/{re.sub(r'[^A-Za-z0-9]+', '-', key).strip('-')}"
            if rl.get('relatesTo') != iri or rl.get('relationType') != P + 'ContainedIn':
                out.append(('relation', rl.get('relatesTo'), iri))
        for g in a.get('geometries', []):
            if 'geojson' not in g:
                expected = location(g.get('sourceLabel', ''))[0].get('qualification')
                if g.get('qualification') != expected:
                    out.append(('location', g.get('qualification'), expected))
    for i in rec.get('identityRelations', []):
        partner = next((k for k, v in IDENTITY.items() if i['object'].startswith(v.split('{}')[0])), None)
        src = i.get('source') if isinstance(i.get('source'), dict) else {'@id': i.get('source')}
        if i.get('basis') != f'Trismegistos GeoRelations ({partner})' and not i.get('basis', '').startswith('Trismegistos GeoRelations (wikipedia') \
                or src.get('@id') != TM['@id'] or i.get('identityType') != 'unspecified':
            out.append(('identity', i.get('basis'), src.get('@id'), i.get('identityType')))
    return out


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
    plato, head = {}, {}
    with open(a.plato, encoding='utf-8') as fh:
        for line in fh:
            rec = json.loads(line)
            iri = rec.get('@id', '')
            if 'gazetteer' in rec:
                head = rec['gazetteer']
            if iri.startswith(PLACE.format('')):
                plato[int(iri[len(PLACE.format('')):])] = rec
    have, match, unmapped = collections.Counter(), collections.Counter(), collections.Counter()
    diffs = collections.defaultdict(list)
    missing = 0
    for r in map(dict, conn.execute('SELECT * FROM geo ORDER BY tm_geo_id')):
        rec = plato.get(r['tm_geo_id'])
        if rec is None:
            missing += not a.only_present
            continue
        mine, theirs = from_plato(rec), from_tm(r, rel[r['tm_geo_id']], wd)
        have['agreement'] += 1
        dis = disagreements(rec, r)
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
        # The attestation window, on every attested name: the same two years and written form.
        if r['begin_date'] or r['end_date']:
            have['dates'] += 1
            written = r['begin_date_fmt'] if r['begin_date_fmt'] == r['end_date_fmt'] else f"{r['begin_date_fmt']} - {r['end_date_fmt']}"
            want = (year(r['begin_date']), year(r['end_date']), written)
            wins = set(mine['window'])
            ok = wins == {want} or (not mine['window'] and not any(theirs[k] for k in ('latin_name', 'greek_unicode', 'egyptian_unicode', 'coptic_unicode', 'ethnicon')))
            match['dates'] += ok
            if not ok and len(diffs['dates']) < 5:
                diffs['dates'].append({'tm': r['tm_geo_id'], 'source': want, 'plato': sorted(wins)})
        if r['country'] and r['country'] != 'ghost name':
            have['country'] += 1
            code = CCODES.get(r['country'].rstrip('?').strip())
            ok = mine['ccodes'] == ([code] if code else [])
            match['country'] += ok
            if not ok and len(diffs['country']) < 5:
                diffs['country'].append({'tm': r['tm_geo_id'], 'source': r['country'], 'plato': mine['ccodes']})
            if not code:
                unmapped[r['country']] += 1
    have['header'] = 1
    match['header'] = int(all(head.get(k) == v for k, v in HEADER.items()))
    if not match['header']:
        diffs['header'].append({k: head.get(k) for k in ('@id', 'title', 'licence', 'status')})
    total = conn.execute('SELECT COUNT(*) FROM geo').fetchone()[0]
    print(f'{len(plato)} of {total} TM records found in {a.plato}' + (' (a sample: only those compared)' if a.only_present else ''))
    bad = False
    for k in sorted(have):
        flag = '' if match[k] == have[k] else '   DIFFERS'
        bad |= bool(flag)
        print(f'  {k:18} {match[k]:6} of {have[k]:6} records match{flag}')
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
