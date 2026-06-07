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
  * gazetteermatch       — HGIS de las Indias ids -> hgis: closeMatch links
                           (resolved against the staged `hgis` authority) AND a
                           geometry UPGRADE: weak/missing alc placements are filled
                           from the matched hgis lugar's precise point. This alc↔hgis
                           interlink is SELF-TRIGGERING here (no separate pipeline
                           step) and so DEPENDS ON `hgis` being staged first
                           (INGESTION_ORDER places hgis before alc). See
                           _load_hgis_index / [[hgis_authority]].
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
  1. EXTRACT: python -m authorities.alcedo-places   (h3 nested in geometries[])
  2. INDEX (prod ES on pitt; firewalled, no /ihome). index_namespace.main builds
     an UNAUTH client + prints es_host, so drive its funcs with a basic_auth
     client (pw from /ix1/ishi/es/config/elastic.password) — see the verified
     driver in git log 2026-06-06 (/tmp/alc_index_driver.py). Add
     --emit-new-toponyms /vast/ishi/staged/alc/backfill/new.jsonl for step 5.
  3. AAT path-fill: processing.apply_aat_enrich --namespace alc --execute (types
     already carry intrinsic aat_ids from Featuretype_AAT; this adds aat_paths).
  4. CCODES (spatial — points need it; Nation is colonial, not modern). NB
     ccode_enrichment.main is --run-id/--namespace and reads the namespace's
     h3_merged/ + un's h3_merged/. For a point-only incremental add:
       cp staged/alc/extract/places.jsonl staged/alc/h3_merged/places.jsonl
       # on a CRC compute node, 28G (UN polygons in mem):
       run_ccode_enrichment(run_id="alc-incr", namespace="alc", manifest_path=None)
       # -> staged/alc/ccode/places.ccode.jsonl ; then on pitt:
       processing.apply_ccode_patch --patch .../alc/ccode/places.ccode.jsonl --execute
  5. EMBEDDINGS: backfill_embeddings compute (CRC GPU, -M gpu --partition a100
     --gres=gpu:1) on new.jsonl -> embeddings.jsonl; then on pitt:
       backfill_embeddings index --es-host ... --in embeddings.jsonl --embedding-version 7
  6. AGGREGATES (--run-id alc-incr --namespace alc): gazetteer_temporal_extent
     works off extract; gazetteer_h3_coverage needs staged/alc/h3/places.h3.jsonl
     — synthesise it from the extract's nested geometries[].h3_cover (point-only
     skips h3_stage): one line per place {place_id, geometries:[{geometry_index,
     h3_centroid, h3_cover}]}.
  7. TILES (CRC compute node; won't import on pitt — antimeridian):
       generate_tiles --bucket alc  (auto-deploys mbtiles), then on pitt:
       update_tileserver_config --bucket alc --execute  (rewrite+restart+verify).
  8. REGISTRY (LAST, gated on tileset serving; run on crc0 for the native-uid
     token): push_gazetteer_inventory --namespace alc  (prod + dev).
  Loaded to prod 2026-06-06: 18,031 places, 15,981 toponyms (all embedded),
  15,174 ccodes, 17,999 AAT, tileset + registry on prod+dev.
===========================================================================
"""

import csv
import re
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


def _clean(val):
    """Treat pandas/CSV null sentinels as empty."""
    val = (val or "").strip()
    return "" if val in ("nan", "None", "NaN", "\\N", "-") else val


def _v(row, key):
    """Cleaned cell value for a row/key."""
    return _clean(row.get(key))


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


# Admin/container featuretypes that can be the TARGET of a `within` relation, in
# resolution-preference order (a Province value prefers a 'Provincia' entry, etc.).
_ADMIN_FT_PRIORITY = {
    "Reyno": 0, "Provincia": 1, "Gobierno": 2, "Capitanía general": 2,
    "Audiencia": 2, "Intendencia": 3, "Jurisdicción": 4, "Corregimiento": 4,
    "Alcaldía mayor": 4, "Partido": 5, "Distrito": 5, "Departamento": 5,
}
# Non-informative admin field values — never resolve/emit a relation for these.
_ADMIN_NOISE = {"unspecified", "unknown", "ambiguous"}


def _admin_val(raw):
    v = _clean(raw)
    return "" if v.lower() in _ADMIN_NOISE else v


def build_admin_index(csv_path):
    """Map admin-unit name (lower) → its alc place_id, so colonial Province/
    District/Partido fields can be reconciled to the REAL indexed alc entry that
    IS that admin unit (best featuretype wins per _ADMIN_FT_PRIORITY)."""
    best: dict[str, tuple[int, str]] = {}
    for row in _row_iter(csv_path):
        if _v(row, "entrytype") != "Toponym":
            continue
        pri = _ADMIN_FT_PRIORITY.get(_v(row, "featuretype"))
        if pri is None:
            continue
        name = _v(row, "Normname").lower()
        eid = _v(row, "entry_id")
        if not name or not eid:
            continue
        cur = best.get(name)
        if cur is None or pri < cur[0]:
            best[name] = (pri, f"{NAMESPACE}:{eid}")
    return {name: pid for name, (pri, pid) in best.items()}


_HGIS_IDX = None  # cached (set_of_hgis_src_ids, {lugar_src_id: point_geom_fields})


def _load_hgis_index():
    """Load the staged `hgis` authority for the self-triggering alc→hgis interlink:
    returns (all hgis src_ids, {lugar src_id → point geom fields}). Read once.

    DEPENDENCY: `hgis` must be staged BEFORE `alc` (INGESTION_ORDER places hgis
    first). If absent, links are emitted best-effort and no geometry upgrade runs.
    """
    global _HGIS_IDX
    if _HGIS_IDX is not None:
        return _HGIS_IDX
    src_ids, points = set(), {}
    base = Path(STAGED_BASE_DIR) / "hgis"
    path = next((base / st / "places.jsonl" for st in ("h3_merged", "extract")
                 if (base / st / "places.jsonl").exists()), None)
    if path is None:
        print("  WARNING: no staged `hgis` data found — gazetteermatch links emitted "
              "best-effort, no geometry upgrade. Ingest `hgis` before `alc`.")
        _HGIS_IDX = (src_ids, points)
        return _HGIS_IDX
    print(f"  loading hgis interlink index from {path} ...")
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            pid = d.get("place_id") or ""
            if not pid.startswith("hgis:"):
                continue
            sid = pid.split(":", 1)[1]
            src_ids.add(sid)
            g = (d.get("geometries") or [None])[0]
            if g and not g.get("has_geom") and g.get("repr_point"):  # lugar (point)
                points[sid] = {k: g[k] for k in
                               ("repr_point", "bounds", "h3_centroid", "h3_cover") if g.get(k)}
    print(f"  hgis interlink index: {len(src_ids):,} src_ids, {len(points):,} lugar points")
    _HGIS_IDX = (src_ids, points)
    return _HGIS_IDX


def _gm_key(gm):
    """gazetteermatch → hgis src_id key (numeric str | alnum territorio code) | None."""
    if not gm:
        return None
    if gm.replace(".", "").isdigit():
        return str(int(float(gm)))
    if re.fullmatch(r"[A-Z0-9]{4,}", gm):
        return gm
    return None


def process_row(row, admin_index, hgis_idx):
    """Map one Alcedo_structured.csv row (dict) to a place doc, or None.

    ``admin_index`` resolves Province/District/Partido → real indexed alc admin
    place_ids. ``hgis_idx`` = (hgis_src_ids, hgis_lugar_points) from the staged
    `hgis` authority — resolves gazetteermatch → real hgis: links AND upgrades/fills
    weak/missing alc geometry from the matched hgis lugar's precise point. This is
    the self-triggering HGIS interlink (no separate pipeline step; reproducible on
    re-extract once hgis is staged).
    """
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
    hgis_src_ids, hgis_points = hgis_idx
    gmkey = _gm_key(_v(row, "gazetteermatch"))

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

    # HGIS interlink: if gazetteermatch resolves to an hgis lugar point AND alc's
    # own placement is missing or weak (centroid/approximate province stand-in),
    # use the hgis precise point (source='hgis', approximation='exact'); else use
    # alc's own point. h3 is nested INSIDE the geometry (where the schema,
    # ccode_enrichment, and gateway/spatial.py read it).
    approx = _APPROX.get(conf, "approximate")
    hgis_pt = hgis_points.get(gmkey) if gmkey else None
    if hgis_pt and (geometry is None or approx in ("centroid", "approximate")):
        ge = {"has_geom": False, "source": "hgis", "approximation": "exact",
              "repr_point": hgis_pt["repr_point"], "timespans": timespans}
        for k in ("bounds", "h3_centroid", "h3_cover"):
            if hgis_pt.get(k):
                ge[k] = hgis_pt[k]
        place_doc["geometries"] = [ge]
    elif geometry:
        geom_entry = enrich_geometry(geometry, timespans=timespans)
        if geom_entry:
            geom_entry["source"] = NAMESPACE
            geom_entry["approximation"] = approx
            place_doc["geometries"] = [geom_entry]
            rp = geom_entry.get("repr_point")
            if rp:
                h3_geom = select_h3_cover_geometry(geom_entry, geometry)
                h3c, h3cover = compute_h3_fields(rp["lon"], rp["lat"], h3_geom)
                if h3c:
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
    # gazetteermatch is the entry's match in HGIS de las Indias, now ingested as
    # the WHG `hgis` authority (place_id = hgis:<src_id>): NUMERIC ids are lugares
    # (settlements/features), uppercase-alnum codes (e.g. JUPECUAB) are territorios
    # (admin districts). Emit hgis: closeMatch links (the legacy indias: aliases
    # only resolved in WHG's deprecated indices).
    links = []
    # hgis closeMatch — resolved against the staged hgis index: emit only when the
    # target hgis place exists (if hgis wasn't staged, emit best-effort to resolve
    # later). lugares (numeric) + territorios (alnum) both resolve.
    if gmkey and (not hgis_src_ids or gmkey in hgis_src_ids):
        links.append({"type": LINK_TYPE, "identifier": f"hgis:{gmkey}"})
    # source-page reference into the TEI edition (by entry xml:id, per volume)
    tei = _tei_link(_v(row, "volume"), entry_id)
    if tei:
        links.append({"type": "primaryTopicOf", "identifier": tei})
    if links:
        place_doc["links"] = links

    # --- containing admin units → `within` relations to REAL indexed places ---
    # Resolve each colonial Province/District/Partido NAME to the alc entry that
    # IS that admin unit (same source + period; that entry is itself indexed and
    # carries its own HGIS/wd reconciliation). No pseudo-ids — emit a relation
    # only when it resolves to a real, different alc place_id.
    relations = []
    seen_targets = set()
    for lvl in ("Province", "District", "Partido"):
        name = _admin_val(row.get(lvl))
        if not name:
            continue
        target = admin_index.get(name.lower())
        if target and target != place_id and target not in seen_targets:
            seen_targets.add(target)
            relations.append({
                "relation_type": "within",
                "related_place_id": target,
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

    # First pass: index the admin entries so within-relations resolve to real ids.
    admin_index = build_admin_index(csv_path)
    print(f"admin index: {len(admin_index):,} admin-unit names → alc place_ids")
    # Load the staged hgis authority for the self-triggering interlink (links +
    # geometry upgrade). Depends on hgis being staged first (INGESTION_ORDER).
    hgis_idx = _load_hgis_index()

    staged, skipped, errors = 0, 0, 0
    start = datetime.now()
    for i, row in enumerate(_row_iter(csv_path)):
        if limit and i >= limit:
            break
        if not dry and (i + 1) % 2000 == 0:
            print(f"\r  {i + 1} rows - staged: {staged}", end="", flush=True)
        try:
            doc = process_row(row, admin_index, hgis_idx)
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
