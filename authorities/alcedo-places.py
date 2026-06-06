# authorities/alcedo-places.py

"""
Stage the Alcedo gazetteer (ANR TopUrbi digitisation) to the staged extract
directory used by the rebuild / incremental pipeline.

Source: Antonio de Alcedo, *Diccionario geográfico-histórico de las Indias
Occidentales ó América* (1786-1789). TEI digital edition by Werner Stangl under
the French ANR TopUrbi project (PI Jean-Paul Zúñiga; technical lead Carmen
Brando, EHESS). LP-TSV export prepared by Karl Grossner.
  https://gitlab.huma-num.fr/plateforme-geomatique-et-hn/topurbi-project
  Licence: CC-BY-NC 4.0. ANR requires record-level attribution of the project
  code (carried in AUTHORITIES['alc']['citation_text']).

~17,467 entries across the colonial Americas. POINT geometries only (lat/lon
columns; the `geowkt` column is deferred — empty in the current export). ~8,780
of the points are dummy / province-centroid placements flagged via the
`approximation` column (see APPROX_MAP). Linked to HGIS de las Indias (already in
WHG as `lugares`/`territorios`) plus GeoNames/TGN via the `links` column.

LP-TSV columns (tab-delimited, header row):
  id, title, title_source, title_uri, ccodes, fclasses, types, lat, lon,
  geowkt, start, end, links, description, approximation
The Postgres build (make_alcedo_candidates.py) also derives `aat_identifier`
per featuretype, but the current `export_lptsv.py` DROPS it — so this mapper
reads `aat_identifier` ONLY if a future re-export adds it (forward-compatible).
Until then `types[]` carries the verbatim Spanish featuretype as both
`identifier` and `sourceLabel` (label='alcedo'), ready for a central-pipeline
AAT crosswalk (typesystem/data/alcedo.json keyed by featuretype) — see the AAT
follow-up note in the runbook below.

Output: ``{STAGED_BASE_DIR}/alc/extract/places.jsonl``
ES indexing happens later via the incremental ``index_namespace`` path (small +
point-only, so it suits the single-namespace add workflow, NOT a full-rebuild
cutover). This script never talks to Elasticsearch.

=== INCREMENTAL SINGLE-NAMESPACE ADD RUNBOOK (ns=alc) ======================
POINT-ONLY: like `ofs`, alcedo is point-only, so the geom_store / H3 / ccode
staging chain is ALL NO-OPS and SKIPPED. helpers compute h3_cover/h3_centroid
INLINE during EXTRACT (from repr_point), and ccodes come straight from the
source `ccodes` column. Follow authorities/ottnfs-places.py's runbook verbatim,
substituting alc for ofs, with these alc-specific deltas:
  0. FETCH: python -m processing.fetch_authorities -n alc --age 0
            (downloads alcedo_lptsv.tsv from the kgeographer/topurbi repo)
  1. EXTRACT: python -m authorities.alcedo-places
  4. INDEX:  python -m processing.index_namespace --namespace alc --source-stage extract --es-host <PROD> --execute
  5a. Symphonym embedding backfill for the new Spanish toponyms (name-only).
  5-8. aggregates -> tiles (register `alc` in generate_tiles._PER_NAMESPACE_BUCKETS)
       -> update_tileserver_config -> push_gazetteer_inventory --namespace alc.
  AAT FOLLOW-UP: the LP-TSV lacks per-row AAT. To enrich, either (a) get Karl to
  re-export with the `aat_identifier` column (this mapper then sets aat_ids
  directly, like ofs/og), or (b) build typesystem/data/alcedo.json keyed by the
  Spanish featuretype and wire 'alcedo' into processing/aat_data_lookup.py
  (vocab matched on `identifier`), then run processing.apply_aat_enrich.
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

ALC_CONFIG = next((a for a in AUTHORITIES if a['namespace'] == NAMESPACE), None)

# The dictionary's five volumes were published 1786-1789; every entry carries
# this attestation window (the LP-TSV start/end columns are fixed to it).
ALC_START, ALC_END = 1786, 1789

# Alcedo's text is Spanish.
LANG = "es"

# Source `approximation` (CRM/GeoSPARQL predicates) -> our geometries[].approximation
# token (matching the convex_hull/centroid/exact convention used by ofs/og).
#   ''                       well-placed / geocoded -> exact
#   crm:P189_approximates    approximate location   -> approximate
#   geo:sfWithin             province-centroid stand-in for an unlocated entry
APPROX_MAP = {
    "": "exact",
    "crm:p189_approximates": "approximate",
    "geo:sfwithin": "centroid",
}

# Linked Places link relation for the cross-references in the `links` column
# (indias: -> HGIS de las Indias already in WHG; gn:/tgn: -> GeoNames/Getty).
# closeMatch is the conservative standard for gazetteer cross-refs.
LINK_TYPE = "closeMatch"


def _approx(raw):
    return APPROX_MAP.get((raw or "").strip().lower(), "approximate")


def _ccodes(raw):
    out = []
    for tok in str(raw or "").replace(",", ";").split(";"):
        tok = tok.strip().upper()
        if tok and tok not in out:
            out.append(tok)
    return out


def _links(raw):
    links = []
    seen = set()
    for tok in str(raw or "").replace(",", ";").split(";"):
        tok = tok.strip()
        if tok and ":" in tok and tok not in seen:
            seen.add(tok)
            links.append({"type": LINK_TYPE, "identifier": tok})
    return links


def process_row(row):
    """Map one LP-TSV row (dict, keyed by header) to a place doc, or None."""
    src_id = (row.get("id") or "").strip()
    title = (row.get("title") or "").strip()
    if not src_id or not title:
        return None
    place_id = f"{NAMESPACE}:{src_id}"

    timespans = [{"start": {"in": ALC_START}, "end": {"in": ALC_END}}]

    # --- geometry: points only (geowkt deferred / empty) -----------------
    geometry = None
    try:
        lon, lat = float(row["lon"]), float(row["lat"])
        if -180 <= lon <= 180 and -90 <= lat <= 90 and not (lon == 0 and lat == 0):
            geometry = {"type": "Point", "coordinates": [lon, lat]}
    except (KeyError, TypeError, ValueError):
        pass

    # --- toponym (Spanish, single normalised title) ----------------------
    toponyms = [{"toponym_id": f"{title}@{LANG}", "timespans": timespans}]

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
            geom_entry["approximation"] = _approx(row.get("approximation"))
            place_doc["geometries"] = [geom_entry]
            rp = geom_entry.get("repr_point")
            if rp:
                h3_geom = select_h3_cover_geometry(geom_entry, geometry)
                h3c, h3cover = compute_h3_fields(rp["lon"], rp["lat"], h3_geom)
                if h3c:
                    place_doc["h3_centroid"] = h3c
                    place_doc["h3_cover"] = h3cover

    # --- type: verbatim Alcedo featuretype -------------------------------
    # identifier == sourceLabel == the Spanish featuretype (e.g. "Pueblo"),
    # so a future typesystem/data/alcedo.json can key the AAT crosswalk on it.
    # If a re-export carries `aat_identifier` (aat:NNN), fold the id directly.
    featuretype = (row.get("types") or "").strip()
    if featuretype:
        t = {"identifier": featuretype, "label": "alcedo", "sourceLabel": featuretype}
        aat_raw = (row.get("aat_identifier") or "").strip()  # absent in current export
        if aat_raw:
            digits = "".join(ch for ch in aat_raw if ch.isdigit())
            if digits:
                t["aat_ids"] = [int(digits)]
        place_doc["types"] = [t]

    # --- ccodes (provided by the source spatial lookup) ------------------
    ccodes = _ccodes(row.get("ccodes"))
    if ccodes:
        place_doc["ccodes"] = ccodes

    # --- links (HGIS de las Indias / GeoNames / TGN cross-refs) ----------
    links = _links(row.get("links"))
    if links:
        place_doc["links"] = links

    # --- description (full Spanish TEI <sense> text) ---------------------
    desc = (row.get("description") or "").strip()
    if desc:
        place_doc["descriptions"] = [{"value": desc, "lang": LANG}]

    return place_doc


def _row_iter(tsv_path):
    # Descriptions can be tens of kB (Peru ~54k chars) — lift csv's field cap.
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    with open(tsv_path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            yield row


def stage_alcedo_file(tsv_path, limit=None, dry=False):
    print(f"Processing Alcedo LP-TSV: {tsv_path}")
    if not Path(tsv_path).exists():
        std = Path(DATA_DIR) / "authorities" / NAMESPACE / Path(tsv_path).name
        if std.exists():
            tsv_path = std
        else:
            print(f"ERROR: File not found: {tsv_path}")
            return

    staged, skipped, errors = 0, 0, 0
    start = datetime.now()
    for i, row in enumerate(_row_iter(tsv_path)):
        if limit and i >= limit:
            break
        if not dry and (i + 1) % 1000 == 0:
            print(f"\r  {i + 1} rows - staged: {staged}", end="", flush=True)
        try:
            doc = process_row(row)
            if not doc:
                skipped += 1
                continue
            if dry:
                import json
                print(json.dumps(doc, ensure_ascii=False, indent=2)[:2000])
                staged += 1
                continue
            write_staged_place_doc(namespace=NAMESPACE, doc=doc)
            staged += 1
        except Exception as e:
            print(f"\n  ERROR row {i} (id={row.get('id')!r}): {e}")
            errors += 1

    print(f"\n{'=' * 80}\nALCEDO STAGING {'(DRY)' if dry else 'COMPLETE'}\n{'=' * 80}")
    print(f"Time: {(datetime.now() - start).seconds}s")
    print(f"Staged: {staged:,}\nSkipped: {skipped:,}\nErrors: {errors:,}")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Stage the Alcedo (TopUrbi) gazetteer")
    p.add_argument("--file", help="Path to the LP-TSV (defaults to configured file)")
    p.add_argument("--limit", type=int, help="Process only the first N rows")
    p.add_argument("--dry", action="store_true",
                   help="Print docs instead of staging (implies a small --limit)")
    args = p.parse_args()

    if args.file:
        tsv = args.file
    elif ALC_CONFIG and ALC_CONFIG.get("files"):
        name = ALC_CONFIG["files"][0].get("name") or Path(ALC_CONFIG["files"][0]["url"]).name
        tsv = Path(DATA_DIR) / "authorities" / NAMESPACE / name
    else:
        print("ERROR: no --file and no AUTHORITIES['alc'] config")
        sys.exit(1)

    print(f"Alcedo gazetteer (STAGING)\nFile: {tsv}\n")
    stage_alcedo_file(str(tsv), limit=args.limit, dry=args.dry)
