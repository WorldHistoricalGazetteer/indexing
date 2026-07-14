# authorities/un-countries.py
"""
Stage the UN ``un`` gazetteer from the **UN Geospatial BNDA** country
boundaries (``processing/data/un_bnda_countries.geojson``) to
``{STAGED_BASE_DIR}/un/extract/places.jsonl``, and write the country
geometries to the persistent VAST geom store via ``GeomStoreWriter``.

This replaces the previous Natural Earth (``ne_10m_admin_0``) source. BNDA is
the United Nations' own authoritative, politically-neutral administrative
boundary set:

* native ISO 3166-1 alpha-2 (``iso2cd``) / alpha-3 (``iso3cd``) / M49 for every
  feature — no Natural Earth ``-99`` dropout (France/Norway/… had no ccode);
* dependent territories modelled as their own ISO features (PR/GF/PF/GU/AS/…);
* Antarctica (AQ) included; antimeridian geometries handled; topologically
  coherent borders (no per-country slivers).

The same file backs spatial ccode enrichment
(``processing.ccode_enrichment.UnCountryIndex.from_bnda_geojson``), so the
``un`` gazetteer and the ccodes assigned to every other place share one source
of truth.
"""
import json
import sys
from pathlib import Path

from processing.helpers import (
    enrich_geometry,
    compute_area_km2,
    compute_h3_fields,
    select_h3_cover_geometry,
    write_staged_place_doc,
)
from processing.settings import UN_BNDA_COUNTRIES_FILE

# Feature-status codes (BNDA ``stscod``) that are NOT sovereign/derived land
# units we want as gazetteer places (e.g. joint/undetermined administration
# lines). Kept permissive — anything with a valid iso2/iso3 is staged.
_TS_2025 = [{'start': {'in': 2025}, 'end': {'in': 2025}}]


def _load_bnda_features(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    feats = data.get("features") or []
    print(f"✓ Read {len(feats)} UN BNDA features from {path}")
    return feats


def _is_antarctica(iso2, iso3, name):
    return (iso2 or '').upper() == 'AQ' or (iso3 or '').upper() == 'ATA' \
        or (name or '').lower() == 'antarctica'


def create_country_place_doc(feature):
    """Build a ``un:`` place doc from one BNDA feature."""
    props = feature.get('properties') or {}
    geometry = feature.get('geometry')
    if not geometry:
        return None

    iso2 = (props.get('iso2cd') or '').strip()
    iso3 = (props.get('iso3cd') or '').strip()
    m49 = (props.get('m49_cd') or '').strip()
    name = (props.get('nam_en') or props.get('lbl_en') or '').strip()
    name_fr = (props.get('name_fr') or '').strip()
    label_en = (props.get('lbl_en') or '').strip()
    if not name:
        return None

    # place_id: prefer ISO alpha-3 (stable, unique); fall back to a name slug.
    if iso3 and iso3 != '-99':
        place_id = f"un:{iso3.lower()}"
    else:
        place_id = f"un:{name.lower().replace(' ', '_').replace('/', '_')}"

    toponyms = []
    seen = set()
    for nm, lang in ((name, 'en'), (name_fr, 'fr'), (label_en, 'en')):
        if nm and (nm, lang) not in seen:
            toponyms.append({'toponym_id': f"{nm}@{lang}", 'timespans': list(_TS_2025)})
            seen.add((nm, lang))

    geom_entry = enrich_geometry(geometry, timespans=list(_TS_2025),
                                 geom_key=f"{place_id}_0")
    if not geom_entry:
        return None

    doc = {
        'place_id': place_id,
        'title': name,
        'toponyms': toponyms,
        'geometries': [geom_entry],
        # Antarctica is a continent, not a sovereign state — distinct type so
        # the AAT pipeline maps it to "continents" rather than "nations".
        'types': [(
            {'identifier': 'continent', 'label': 'un', 'sourceLabel': 'continent'}
            if _is_antarctica(iso2, iso3, name)
            else {'identifier': 'country', 'label': 'un', 'sourceLabel': 'sovereign-country'}
        )],
        'boundary': '2',
    }

    # h3 lives INSIDE the geometry entry (schema + staged pipeline read
    # geometries[].h3_*; doc-level is silently dropped).
    if geom_entry.get('repr_point'):
        rp = geom_entry['repr_point']
        h3_geom = select_h3_cover_geometry(geom_entry, geometry)
        h3c, h3cover = compute_h3_fields(rp['lon'], rp['lat'], h3_geom)
        if h3c:
            geom_entry['h3_centroid'] = h3c
            geom_entry['h3_cover'] = h3cover

    if iso2 and iso2 != '-99':
        doc['ccodes'] = [iso2]

    relations = []
    if iso3 and iso3 != '-99':
        relations.append({
            'relation_type': 'hasIdentifier',
            'related_place_id': f"iso3166:{iso3}",
            'label': f"ISO 3166-1 alpha-3: {iso3}",
        })
    if m49 and m49 != '-99':
        relations.append({
            'relation_type': 'hasIdentifier',
            'related_place_id': f"m49:{m49}",
            'label': f"UN M49: {m49}",
        })
    if relations:
        doc['relations'] = relations

    doc['admin_level'] = 0
    area = compute_area_km2(geometry)
    if area:
        doc['area_km2'] = round(area, 2)
    if props.get('subreg'):
        doc['subregion'] = props['subreg']
    if props.get('georeg'):
        doc['continent'] = props['georeg']
    return doc


def stage_un_countries(**_ignored):
    """Stage UN BNDA countries to ``{STAGED_BASE_DIR}/un/extract/places.jsonl``.

    ``**_ignored`` keeps the historic ``download=`` kwarg accepted (now a no-op —
    the BNDA GeoJSON is committed to the repo)."""
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    print("=" * 80)
    print("UN COUNTRIES STAGING (UN BNDA)")
    print("=" * 80 + "\n")

    features = _load_bnda_features(UN_BNDA_COUNTRIES_FILE)
    print(f"\nProcessing {len(features)} features...\n")

    stats = {'processed': 0, 'places_staged': 0, 'no_iso': 0, 'errors': 0}
    seen_ids = set()

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, "un") as gsw:
        configure_module_writer(gsw)
        for i, feature in enumerate(features):
            try:
                doc = create_country_place_doc(feature)
                if doc is None:
                    stats['errors'] += 1
                    continue
                if not doc.get('ccodes'):
                    stats['no_iso'] += 1
                if doc['place_id'] in seen_ids:
                    # Defensive: a repeated iso3 would collide on geom_key; skip
                    # the duplicate rather than overwrite the store.
                    print(f"  duplicate place_id {doc['place_id']} — skipped")
                    continue
                seen_ids.add(doc['place_id'])
                write_staged_place_doc(namespace='un', doc=doc)
                stats['places_staged'] += 1
                stats['processed'] += 1
                if (i + 1) % 50 == 0:
                    print(f"Processed {i + 1}...")
            except Exception as e:
                print(f"Error {i}: {e}")
                stats['errors'] += 1
                continue
        configure_module_writer(None)

    print("\n" + "=" * 80)
    print("COMPLETE")
    print("=" * 80)
    print(f"Processed:     {stats['processed']}")
    print(f"Staged:        {stats['places_staged']}")
    print(f"Without ISO2:  {stats['no_iso']}")
    print(f"Errors:        {stats['errors']}")
    print(f"Geometries in VAST store: {gsw.count:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Stage UN countries from UN BNDA')
    # --no-download retained for CLI compatibility; the BNDA file is in-repo.
    parser.add_argument('--no-download', action='store_true',
                        help='(no-op) BNDA GeoJSON is committed to the repo')
    args = parser.parse_args()
    stage_un_countries()
