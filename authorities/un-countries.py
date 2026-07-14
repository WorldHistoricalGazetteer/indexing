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

# Temporal scope of the BNDA boundaries. BNDA_simplified is a present-day
# snapshot (dataset currency ~2023) and carries NO per-feature dates — the
# national outline of a country is not dated by the source, and (unlike its
# subnational SALB admin units) does not change over the dataset's span. So we
# state only what the data itself supports: the boundary was established at
# some point up to the present (``start.latest`` = 2025, i.e. before 2026) and
# is **ongoing** (no ``end`` — the WHG convention for a current feature).
# Encoded with the LPF ``latest`` qualifier rather than a false exact
# ``start.in`` year. If a country ever gains dated historical boundary versions,
# they are added as additional timespanned entries in ``geometries[]``.
_BOUNDARY_ESTABLISHED_BY = 2025
_TS_ONGOING = [{'start': {'latest': _BOUNDARY_ESTABLISHED_BY}}]


def _load_bnda_features(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    feats = data.get("features") or []
    print(f"✓ Read {len(feats)} UN BNDA features from {path}")
    return feats


def _is_antarctica(iso2, iso3, name):
    return (iso2 or '').upper() == 'AQ' or (iso3 or '').upper() == 'ATA' \
        or (name or '').lower() == 'antarctica'


def _group_features_by_country(features):
    """Group BNDA features by country key (ISO3, else name slug), preserving
    first-seen order."""
    from collections import OrderedDict
    groups = OrderedDict()
    for f in features:
        props = f.get('properties') or {}
        iso3 = (props.get('iso3cd') or '').strip()
        if iso3 and iso3 != '-99':
            key = iso3.upper()
        else:
            key = (props.get('nam_en') or props.get('lbl_en') or '').strip().lower() \
                or f"obj{props.get('objectid')}"
        groups.setdefault(key, []).append(f)
    return groups


def _merge_country_features(feats):
    """Merge a country's BNDA parts into one feature: union all geometries,
    take properties from the primary part (non-empty name + ``stscod==1``
    preferred, so a real country name wins over a disputed-zone empty)."""
    def _rank(f):
        p = f.get('properties') or {}
        nm = (p.get('nam_en') or '').strip()
        return (0 if nm else 1, 0 if p.get('stscod') == 1 else 1)

    primary = sorted(feats, key=_rank)[0]
    if len(feats) == 1:
        return primary
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union
    geoms = [shape(f['geometry']) for f in feats if f.get('geometry')]
    if not geoms:
        return primary
    merged = unary_union(geoms) if len(geoms) > 1 else geoms[0]
    return {'properties': primary['properties'], 'geometry': mapping(merged)}


def _normalize_lons(geom):
    """Wrap any longitude outside [-180, 180] back into range (BNDA represents
    the US Aleutians with unwrapped lon up to 191). This turns an unwrapped
    dateline span into a proper ±180-crossing geometry, which
    ``helpers._polyfill_adaptive`` then splits correctly for h3_cover; without
    it, ``geo_to_h3shape`` chokes on lon>180 and the cover collapses to a single
    centroid cell (USA had h3_cover=1)."""
    def _wrap(lon):
        if lon > 180.0:
            return lon - 360.0
        if lon < -180.0:
            return lon + 360.0
        return lon

    def _walk(node):
        # shapely.mapping() yields tuples, json.load yields lists — handle both.
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)):
                return [_wrap(float(node[0]))] + [float(x) for x in node[1:]]
            return [_walk(x) for x in node]
        return node

    if not isinstance(geom, dict) or "coordinates" not in geom:
        return geom
    out = dict(geom)
    out["coordinates"] = _walk(geom.get("coordinates"))
    return out


def create_country_place_doc(feature):
    """Build a ``un:`` place doc from one BNDA feature."""
    props = feature.get('properties') or {}
    geometry = feature.get('geometry')
    if not geometry:
        return None
    geometry = _normalize_lons(geometry)

    iso2 = (props.get('iso2cd') or '').strip()
    iso3 = (props.get('iso3cd') or '').strip()
    m49 = (props.get('m49_cd') or '').strip()
    name = (props.get('nam_en') or props.get('lbl_en') or '').strip()
    name_fr = (props.get('name_fr') or '').strip()
    if not name:
        return None

    # place_id: prefer ISO alpha-3 (stable, unique); fall back to a name slug.
    if iso3 and iso3 != '-99':
        place_id = f"un:{iso3.lower()}"
    else:
        place_id = f"un:{name.lower().replace(' ', '_').replace('/', '_')}"

    toponyms = []
    seen = set()
    for nm, lang in ((name, 'en'), (name_fr, 'fr')):
        if nm and (nm, lang) not in seen:
            toponyms.append({'toponym_id': f"{nm}@{lang}", 'timespans': list(_TS_ONGOING)})
            seen.add((nm, lang))

    geom_entry = enrich_geometry(geometry, timespans=list(_TS_ONGOING),
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
    # geometries[].h3_*; doc-level is silently dropped). Polyfill the REAL
    # (normalized) polygon rather than select_h3_cover_geometry's convex-hull
    # shortcut: for antimeridian countries (US/RU) the hull is a globe-spanning
    # degenerate polygon that collapses h3_cover to a single cell, whereas the
    # real geometry goes through the antimeridian-aware _polyfill_adaptive.
    if geom_entry.get('repr_point'):
        rp = geom_entry['repr_point']
        h3c, h3cover = compute_h3_fields(rp['lon'], rp['lat'], geometry)
        # Fall back to the convex hull only when the real geometry collapses —
        # Antarctica's pole-encircling ring can't be polyfilled directly but its
        # hull (a polar cap) can. (The reverse of the US/RU antimeridian case.)
        if len(h3cover or []) <= 1:
            hull_geom = select_h3_cover_geometry(geom_entry, geometry)
            h3c2, cover_hull = compute_h3_fields(rp['lon'], rp['lat'], hull_geom)
            if len(cover_hull or []) > len(h3cover or []):
                h3c, h3cover = h3c2, cover_hull
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

    # Group features by country (iso3) and MERGE their geometries: several
    # countries span multiple BNDA features — mainland + offshore parts (Spain +
    # Canaries, Portugal + Madeira + Azores, Ecuador + Galápagos, USA's two
    # rows) and disputed/undetermined zones (stscod=99, empty name — Halayeb for
    # Egypt/Sudan, Abyei for Sudan/S.Sudan). Skipping duplicates would drop that
    # territory; instead we union all of a country's parts into one geometry.
    grouped = _group_features_by_country(features)
    print(f"\n{len(features)} features -> {len(grouped)} countries; staging...\n")

    stats = {'places_staged': 0, 'no_iso': 0, 'errors': 0, 'multipart': 0}

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, "un") as gsw:
        configure_module_writer(gsw)
        for i, (key, feats) in enumerate(grouped.items()):
            try:
                if len(feats) > 1:
                    stats['multipart'] += 1
                feature = _merge_country_features(feats)
                doc = create_country_place_doc(feature)
                if doc is None:
                    stats['errors'] += 1
                    continue
                if not doc.get('ccodes'):
                    stats['no_iso'] += 1
                write_staged_place_doc(namespace='un', doc=doc)
                stats['places_staged'] += 1
                if (i + 1) % 50 == 0:
                    print(f"Processed {i + 1}...")
            except Exception as e:
                print(f"Error {key}: {e}")
                stats['errors'] += 1
                continue
        configure_module_writer(None)

    print("\n" + "=" * 80)
    print("COMPLETE")
    print("=" * 80)
    print(f"Staged:        {stats['places_staged']}")
    print(f"Multi-part:    {stats['multipart']}")
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
