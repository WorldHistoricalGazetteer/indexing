# authorities/alcedo-places.py

"""
Stage the Alcedo gazetteer (ANR TopUrbi digitisation) to the staged extract
directory used by the rebuild / incremental pipeline.

Source: Antonio de Alcedo, *Diccionario geográfico-histórico de las Indias
Occidentales ó América* (1786-1789). TEI digital edition by Werner Stangl under
the French ANR TopUrbi project (PI Jean-Paul Zúñiga; technical lead Carmen
Brando, EHESS). Licence CC-BY-NC 4.0; ANR mandates record-level attribution of
the project code (carried in AUTHORITIES['alc']['citation_text']).

We ingest Werner's PRISTINE structured export `Alcedo_structured.csv` (pipe-
delimited, ~19.3k rows, from the OFFICIAL gitlab repo) directly — NOT Karl
Grossner's derived LP-TSV, which dropped the per-row AAT and confidence columns.
This lets WHG own the full transform and gain, for free:
  * Featuretype_AAT      — per-row Getty AAT id (99.6%; all valid in our
                           aat_hierarchy) -> types[].aat_ids (aat_paths are
                           path-filled by processing.aat_enrich).
  * content              — full Spanish entry text -> descriptions[].
  * conf_loc_verbal      — rich location confidence -> geometries[].approximation
                           (and drives dropping bogus unlocated coordinates).
  * gazetteermatch       — numeric HGIS de las Indias ids -> indias: links
                           (HGIS is already in WHG as lugares/territorios).
  * Province/District     — colonial admin parents -> named `within` relations.

POINT geometries only. NB `Nation` is the COLONIAL power (Spain/Britain/France…),
NOT the modern country — so ccodes are NOT taken from it; they are assigned
SPATIALLY at ingest (point-in-modern-country), see the runbook below.

Output: ``{STAGED_BASE_DIR}/alc/extract/places.jsonl``. This script never talks
to Elasticsearch; indexing happens later via the incremental ``index_namespace``
path (small; suits the single-namespace add, NOT a full-rebuild cutover).

=== INCREMENTAL SINGLE-NAMESPACE ADD RUNBOOK (ns=alc) ======================
alc is point-only, so the geom_store / H3 staging chain is NO-OP and skipped
(helpers compute h3_cover/h3_centroid INLINE during EXTRACT). UNLIKE the pure
point-only shortcut, ccodes are NOT skipped — `Nation` is colonial, so run a
SPATIAL ccode pass after extract. Follow authorities/ottnfs-places.py's runbook
substituting alc for ofs, with these deltas:
  0. FETCH: python -m processing.fetch_authorities -n alc --age 0
  1. EXTRACT: python -m authorities.alcedo-places
  2. CCODES (spatial — points need it; Nation is colonial not modern):
       python -m processing.ccode_enrichment --namespace alc --source-stage extract \
         --out ${STAGED_BASE_DIR}/alc/ccode/places.ccode.jsonl
       (then apply at/after index via processing.apply_ccode_patch, or fold in
        before indexing). On prod incremental adds use processing.apply_ccode_patch.
  3. INDEX:  python -m processing.index_namespace --namespace alc --source-stage extract --es-host <PROD> --execute
  4. AAT:    types already carry aat_ids (intrinsic, from Featuretype_AAT). Run
       processing.apply_aat_enrich --namespace alc  to path-fill aat_paths.
  5. Symphonym embedding backfill for the new Spanish toponyms (name-only).
  6-8. aggregates -> tiles (bucket `alc` registered in generate_tiles) ->
       update_tileserver_config -> push_gazetteer_inventory --namespace alc.
===========================================================================
"""

import csv
import sys
from pathlib import Path
from datetime import datetime

from processing.helpers import (
    enrich_geometry,
    compute_h3_fields,
    select_h3_cover_geometry,
    write_staged_place_doc,
)
from processing.settings import DATA_DIR, AUTHORITIES

NAMESPACE = "alc"  # Alcedo / TopUrbi
ADM_NS = "alc-adm"  # pseudo-namespace for named (unresolved) colonial admin parents

ALC_CONFIG = next((a for a in AUTHORITIES if a['namespace'] == NAMESPACE), None)

# The five volumes were published 1786-1789; every entry carries this window.
ALC_START, ALC_END = 1786, 1789
LANG = "es"
LINK_TYPE = "closeMatch"

# Source-page reference into the TEI edition (TEI Publisher). Each Alcedo entry is
# a TEI <entry xml:id="..."> whose id == the source entry_id (e.g. id_00005),
# living in the per-volume document Alcedo_vol_{N}.xml. We emit a links[] entry of
# type 'primaryTopicOf' (the TEI entry's primary topic IS this place).
# NB the TEI Publisher app was being (re)deployed by JINNTEC as of 2026-06 (the
# documented base 404s currently) — these links are canonical + forward-compatible
# and resolve once it is live. If the deployed route differs, fix _TEI_BASE only.
_TEI_BASE = "https://sourcesetdonnees.huma-num.fr/exist/apps/topurbi-alcedo"


def _tei_link(volume, entry_id):
    vol = (volume or "").strip()
    if not vol or not entry_id:
        return None
    return f"{_TEI_BASE}/Alcedo_vol_{vol}.xml#{entry_id}"

# conf_loc_verbal -> geometries[].approximation (our convex_hull/centroid/exact
# convention). Values not listed, or in _DROP_GEOM, mean we keep NO geometry
# (the coordinates are an unlocated sentinel, e.g. 0.03/0.03).
_APPROX = {
    "well_placed": "exact", "exact": "exact",
    "sufficient": "approximate", "automatic": "approximate", "auto": "approximate",
    "zonal": "approximate", "broad_area": "approximate",
    "provincial dummy coordinates": "centroid",
    "regional dummy coordinates": "centroid",
}
_DROP_GEOM = {"unlocated", "unspecified", ""}


def _v(row, key):
    """Cleaned cell value; treat pandas/CSV null sentinels as empty."""
    val = (row.get(key) or "").strip()
    return "" if val in ("nan", "None", "NaN", "\\N", "-") else val


def _aat_id(raw):
    """'300008707.0' -> 300008707; '0.0'/'1'/'' -> None.

    Guards the Getty AAT id range (all are 9-digit, 300000000-300999999) so
    junk sentinels like '1.0' that appear in featuretype_literal_AAT are dropped.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if 300000000 <= v <= 300999999 else None


def process_row(row):
    """Map one Alcedo_structured.csv row (dict) to a place doc, or None."""
    entry_id = _v(row, "entry_id")
    title = _v(row, "Normname") or _v(row, "lemma")
    if not entry_id or not title:
        return None
    # Keep only actual PLACE entries. The exclusion is on entrytype, NOT on a
    # missing featuretype/geometry — WHG allows untyped + geometryless places, and
    # a handful of real Toponyms lack a featuretype. The other entrytypes are
    # genuinely non-places:
    #   Referral   — "Véase el artículo X" see-also pointers
    #   Correction — editorial errata
    #   Term       — the vol-5 americanismos glossary (animals/plants/foods/
    #                diseases…); all AAT-less, with bogus representative coords.
    # `fantastical` (legendary places, ~11) is deliberately KEPT — Alcedo
    # documented them and they are valid historical-gazetteer content.
    if _v(row, "entrytype") != "Toponym":
        return None
    featuretype = _v(row, "featuretype")

    place_id = f"{NAMESPACE}:{entry_id}"
    timespans = [{"start": {"in": ALC_START}, "end": {"in": ALC_END}}]

    # --- geometry: points only; drop unlocated sentinels -----------------
    conf = _v(row, "conf_loc_verbal").lower()
    geometry = None
    if conf not in _DROP_GEOM:
        try:
            lon, lat = float(row["Lon"]), float(row["Lat"])
            sentinel = (abs(lon) < 0.05 and abs(lat) < 0.05)  # 0.03/0.03 dummy or 0/0
            if -180 <= lon <= 180 and -90 <= lat <= 90 and not sentinel:
                geometry = {"type": "Point", "coordinates": [lon, lat]}
        except (KeyError, TypeError, ValueError):
            pass

    # --- toponyms (Spanish) ----------------------------------------------
    # Normname (title) is the canonical normalised form. The raw `lemma` is the
    # printed headword (ALL-CAPS in the dictionary), so add it only when it is a
    # genuinely different name, not merely a casing/duplicate variant.
    toponyms = [{"toponym_id": f"{title}@{LANG}", "timespans": timespans}]
    lemma = _v(row, "lemma")
    if lemma and lemma.casefold() != title.casefold():
        toponyms.append({"toponym_id": f"{lemma}@{LANG}", "timespans": timespans})

    place_doc = {
        "place_id": place_id,
        "title": title,
        "toponyms": toponyms,
        "geometries": [],
    }

    if geometry:
        geom_entry = enrich_geometry(geometry, timespans=timespans)
        if geom_entry:
            geom_entry["source"] = NAMESPACE
            geom_entry["approximation"] = _APPROX.get(conf, "approximate")
            place_doc["geometries"] = [geom_entry]
            rp = geom_entry.get("repr_point")
            if rp:
                h3_geom = select_h3_cover_geometry(geom_entry, geometry)
                h3c, h3cover = compute_h3_fields(rp["lon"], rp["lat"], h3_geom)
                if h3c:
                    # Nest h3 INSIDE the geometry (NOT top-level): that is where
                    # the schema, ccode_enrichment._extract_place_h3_cells, and
                    # gateway/spatial.py read it. (Top-level placement was the
                    # ofs/og bug that needed a post-index relocation.)
                    geom_entry["h3_centroid"] = h3c
                    geom_entry["h3_cover"] = h3cover

    # --- type: verbatim featuretype + intrinsic AAT (per-row) ------------
    # Only emitted when the source assigned a featuretype (untyped places are
    # allowed). identifier == sourceLabel == the Spanish featuretype. aat_ids are
    # taken straight from the source's full AAT assignment (both the normalised
    # Featuretype_AAT and the more specific featuretype_literal_AAT — WHG now
    # accepts ALL AAT concepts, not a supported subset); aat_paths are path-filled
    # by processing.aat_enrich.
    if featuretype:
        aat_ids = []
        for col in ("Featuretype_AAT", "featuretype_literal_AAT"):
            a = _aat_id(row.get(col))
            if a and a not in aat_ids:
                aat_ids.append(a)
        t = {"identifier": featuretype, "label": "alcedo", "sourceLabel": featuretype}
        if aat_ids:
            t["aat_ids"] = aat_ids
        place_doc["types"] = [t]

    # --- descriptions (full Spanish entry text) --------------------------
    desc = _v(row, "content")
    if desc:
        place_doc["descriptions"] = [{"value": desc, "lang": LANG}]

    # --- links: HGIS reconciliation + TEI source-page reference -----------
    links = []
    # numeric gazetteermatch == HGIS de las Indias id (in WHG as lugares/territorios)
    gm = _v(row, "gazetteermatch")
    if gm and gm.replace(".", "").isdigit():
        links.append({"type": LINK_TYPE, "identifier": f"indias:{int(float(gm))}"})
    # source-page reference into the TEI edition (by entry xml:id, per volume)
    tei = _tei_link(_v(row, "volume"), entry_id)
    if tei:
        links.append({"type": "primaryTopicOf", "identifier": tei})
    if links:
        place_doc["links"] = links

    # --- colonial admin parents as NAMED `within` relations --------------
    # Province/District/Partido are free-text colonial units (not boundary IDs),
    # so synthesise stable pseudo-ids under alc-adm: to graph-link siblings
    # without claiming a match to a real boundary in `places`.
    relations = []
    for lvl in ("Province", "District", "Partido"):
        name = _v(row, lvl)
        if name:
            relations.append({
                "relation_type": "within",
                "related_place_id": f"{ADM_NS}:{lvl.lower()}:{name.lower().replace(' ', '_')}",
                "label": f"{lvl}: {name}",
                "timespans": timespans,
            })
    if relations:
        place_doc["relations"] = relations

    # ccodes deliberately UNSET — assigned spatially downstream (Nation is the
    # colonial power, not the modern country).
    return place_doc


def _row_iter(csv_path):
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="|"):
            yield row


def stage_alcedo_file(csv_path, limit=None, dry=False):
    print(f"Processing Alcedo structured CSV: {csv_path}")
    if not Path(csv_path).exists():
        std = Path(DATA_DIR) / "authorities" / NAMESPACE / Path(csv_path).name
        if std.exists():
            csv_path = std
        else:
            print(f"ERROR: File not found: {csv_path}")
            return

    staged, skipped, errors = 0, 0, 0
    start = datetime.now()
    for i, row in enumerate(_row_iter(csv_path)):
        if limit and i >= limit:
            break
        if not dry and (i + 1) % 2000 == 0:
            print(f"\r  {i + 1} rows - staged: {staged}", end="", flush=True)
        try:
            doc = process_row(row)
            if not doc:
                skipped += 1
                continue
            if dry:
                import json
                print(json.dumps(doc, ensure_ascii=False)[:1500])
                staged += 1
                continue
            write_staged_place_doc(namespace=NAMESPACE, doc=doc)
            staged += 1
        except Exception as e:
            print(f"\n  ERROR row {i} (entry_id={row.get('entry_id')!r}): {e}")
            errors += 1

    print(f"\n{'=' * 80}\nALCEDO STAGING {'(DRY)' if dry else 'COMPLETE'}\n{'=' * 80}")
    print(f"Time: {(datetime.now() - start).seconds}s")
    print(f"Staged: {staged:,}\nSkipped: {skipped:,}\nErrors: {errors:,}")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Stage the Alcedo (TopUrbi) gazetteer")
    p.add_argument("--file", help="Path to Alcedo_structured.csv (defaults to configured file)")
    p.add_argument("--limit", type=int, help="Process only the first N rows")
    p.add_argument("--dry", action="store_true", help="Print docs instead of staging")
    args = p.parse_args()

    if args.file:
        csv_path = args.file
    elif ALC_CONFIG and ALC_CONFIG.get("files"):
        name = ALC_CONFIG["files"][0].get("name") or Path(ALC_CONFIG["files"][0]["url"]).name
        csv_path = Path(DATA_DIR) / "authorities" / NAMESPACE / name
    else:
        print("ERROR: no --file and no AUTHORITIES['alc'] config")
        sys.exit(1)

    print(f"Alcedo gazetteer (STAGING)\nFile: {csv_path}\n")
    stage_alcedo_file(str(csv_path), limit=args.limit, dry=args.dry)
