# authorities/hgis-places.py

"""
Stage HGIS de las Indias (Werner Stangl) to the staged extract for the
incremental pipeline — a SINGLE `hgis` authority combining its two parts:

  * lugares (settlements)      → POINT geometries        (~13.3k)
  * territorios (administrative) → MultiPolygon geometries (~893)

  HGIS de las Indias: the historical geography of Bourbon Spanish America
  (1701-1808), Werner Stangl, University of Graz, FWF-funded 2015-2017.
  Canonical source: https://www.hgis-indias.net/ (also Harvard Dataverse:
  https://dataverse.harvard.edu/dataverse/hgis-indias). Licence: CC-BY-SA 4.0.

Input here is two WHG LPF exports (lugares + territorios). NB those WHG dataset
records are transient and may be removed — they are NOT the canonical source and
are deliberately not referenced in attribution; we treat the LPF files as a local
import of HGIS de las Indias. The lugares/territorios split is likewise a legacy
expediency; combining them under one namespace is what makes the lugares→territorios
containment resolvable: each lugar's `related` (gvp:broaderPartitive) points at a
territorio by its src_id, and 888/889 parent ids ARE territorio src_ids — so
`within` relations resolve internally to real `hgis:` places.

Input: WHG LPF exports (FeatureCollection; content nested under `properties`).
Read from DATA_DIR/authorities/hgis/ (scp the .lpf files there; they're large +
not committed). Output: {STAGED_BASE_DIR}/hgis/extract/places.jsonl + polygon
geoms written to the geom-store staging (territorios). Never talks to ES.

Per-feature mapping:
  place_id     hgis:<src_id>  (lugares numeric, territorios alnum — 0 collisions)
  title        properties.title
  toponyms     properties.names[].toponym @es (+ when.timespans for territorios)
  geometries   Point (lugar) → inline h3 ; (Multi)Polygon (territorio) → geom-store
               + h3 via the h3_stage chain. source='hgis', approximation='exact'.
  types        properties.types[] → {identifier=sourceLabel, label='hgis',
               sourceLabel, aat_ids=[<aat: id>]}  (aat_paths path-filled by aat_enrich)
  ccodes       properties.ccodes (ISO; territorios may span several modern states)
  links        properties.links[] closeMatch wd/gn/loc/viaf/tgn  (STABLE reconciliation)
  relations    properties.related[] gvp:broaderPartitive → within hgis:<territorio>
Known minor losses (no schema slot): per-name bibliographic `citations` (lugares)
and type-level `gn_class` / `when`. Default attestation window 1701-1808 when a
feature carries no explicit `when`.

=== INCREMENTAL ADD RUNBOOK (ns=hgis — POLYGON path, like ukhc) =============
hgis has polygons (territorios), so this is the FULL polygon chain, NOT the
point-only shortcut. Run on CRC unless noted (index/apply on pitt).
  0. Place the LPF files: scp authorities/hgis/*.lpf → DATA_DIR/authorities/hgis/
  1. EXTRACT: python -m authorities.hgis-places
     (writes places.jsonl + territorio polygons → geom-store staging)
  2. GEOM MERGE: python -m processing.geom_store --merge --keep-staging   (BEFORE h3)
  3. H3: python -m processing.h3_stage --run-id hgis-incr --namespace hgis
         python -m processing.h3_merge --run-id hgis-incr --namespace hgis
  4. CCODE (spatial; un h3_merged reference; 28G compute node):
         run_ccode_enrichment(run_id="hgis-incr", namespace="hgis", manifest_path=None)
         → apply_ccode_patch on pitt
  5. FINAL stage + INDEX (concrete index behind the `places` alias, via the
     basic_auth driver on pitt — see authorities/alcedo-places.py runbook):
         index_namespace --namespace hgis --source-stage final --execute
         (emit --emit-new-toponyms for the Symphonym backfill)
  6. AAT path-fill: apply_aat_enrich --namespace hgis --execute
  7. EMBEDDINGS: backfill_embeddings compute (GPU) → index (pitt, v7)
  8. AGGREGATES: gazetteer_h3_coverage + gazetteer_temporal_extent (--run-id hgis-incr)
  9. TILES: register `hgis` in generate_tiles._PER_NAMESPACE_BUCKETS;
     generate_tiles --bucket hgis (points+polygons) → update_tileserver_config
 10. REGISTRY: push_gazetteer_inventory --namespace hgis  (crc0)
 11. gateway-restart  (polygons ARE in the geom store → needed for exact containment)
===========================================================================
"""

import json
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
from processing.temporal import apply_closure, attested_window, normalise_timespans

NAMESPACE = "hgis"
HGIS_START, HGIS_END = 1701, 1808   # Bourbon Spanish America window (dataset scope)
LANG = "es"

HGIS_CONFIG = next((a for a in AUTHORITIES if a["namespace"] == NAMESPACE), None)

# The dataset's scope, used where a feature carries no `when` of its own. This
# module's own docstring calls it an "attestation window", but it was encoded as
# `{"start": {"in": 1701}, "end": {"in": 1808}}` — a lifespan asserting that
# every such place came into being in 1701 and ceased in 1808 (place#164). HGIS
# documents these places across the Bourbon period, so the honest reading is
# attested somewhere within it: started no later than 1808, ended no earlier
# than 1701, with the definite core legitimately empty.
_DEFAULT_TS = attested_window(HGIS_START, HGIS_END)


def _timespans(when):
    """LPF `when` → schema timespans list (ints), or None.

    Admin-unit lifespans, so `in` is correct where the source gives it
    (place#164 class C). Two fixes: this used to read only `in` and drop any
    `earliest`/`latest` the source stated, and it never applied the closure
    rule, so a unit known only by its end tested as definitely alive at no
    year. `normalise_timespans` forwards whatever the source states (coercing
    string years to int); `apply_closure` bounds a lone end.
    """
    if not isinstance(when, dict):
        return None
    return apply_closure(normalise_timespans(when.get("timespans") or [])) or None


def _dokuid(url):
    """Extract the HGIS dokuwiki id (== a territorio src_id) from a related URL."""
    m = re.search(r"id=([A-Za-z0-9_]+)", url or "")
    return m.group(1) if m else None


def _aat(identifier):
    if isinstance(identifier, str) and identifier.startswith("aat:"):
        digits = "".join(ch for ch in identifier[4:] if ch.isdigit())
        if digits:
            v = int(digits)
            if 300000000 <= v <= 300999999:
                return v
    return None


def process_feature(feat):
    """Map one LPF feature (dict) to a place doc, or None."""
    p = feat.get("properties") or {}
    src = str(p.get("src_id") or "").strip()
    title = (p.get("title") or "").strip()
    if not src or not title:
        return None
    place_id = f"{NAMESPACE}:{src}"

    # --- toponyms (Spanish; territorio names carry when.timespans) -------
    toponyms, seen = [], set()
    for nm in (p.get("names") or []):
        t = (nm.get("toponym") or "").strip()
        if not t:
            continue
        lst = f"{t}@{LANG}"
        if lst in seen:
            continue
        seen.add(lst)
        toponyms.append({"toponym_id": lst,
                         "timespans": _timespans(nm.get("when")) or _DEFAULT_TS})
    if not toponyms:
        toponyms = [{"toponym_id": f"{title}@{LANG}", "timespans": list(_DEFAULT_TS)}]

    place_doc = {"place_id": place_id, "title": title,
                 "toponyms": toponyms, "geometries": []}

    # --- geometry: Point (lugar, inline h3) | (Multi)Polygon (territorio, geom-store) ---
    geom = feat.get("geometry")
    if isinstance(geom, dict) and geom.get("type") and geom.get("coordinates"):
        is_poly = geom["type"] in ("Polygon", "MultiPolygon")
        if is_poly:
            ge = enrich_geometry(geom, timespans=_DEFAULT_TS, geom_key=f"{place_id}_0")
        else:
            ge = enrich_geometry(geom, timespans=_DEFAULT_TS)
        if ge:
            ge["source"] = NAMESPACE
            ge["approximation"] = "exact"
            place_doc["geometries"] = [ge]
            if not is_poly:  # point: compute h3 inline (h3_stage handles polygons)
                rp = ge.get("repr_point")
                if rp:
                    h3g = select_h3_cover_geometry(ge, geom)
                    h3c, h3cover = compute_h3_fields(rp["lon"], rp["lat"], h3g)
                    if h3c:
                        ge["h3_centroid"] = h3c
                        ge["h3_cover"] = h3cover

    # --- types (AAT, intrinsic from the LPF aat: identifier) -------------
    types = []
    for t in (p.get("types") or []):
        native = (t.get("sourceLabel") or t.get("label") or "").strip()
        if not native:
            continue
        tt = {"identifier": native, "label": NAMESPACE, "sourceLabel": native}
        aat = _aat(t.get("identifier"))
        if aat:
            tt["aat_ids"] = [aat]
        types.append(tt)
    if types:
        place_doc["types"] = types

    # --- ccodes ----------------------------------------------------------
    cc = []
    for c in (p.get("ccodes") or []):
        c = (c or "").strip().upper()
        if c and c not in cc:
            cc.append(c)
    if cc:
        place_doc["ccodes"] = cc

    # --- links: STABLE external reconciliation (wd/gn/loc/viaf/tgn) ------
    links, seen_l = [], set()
    for l in (p.get("links") or []):
        ident = str(l.get("identifier") or "").strip()
        if ident and ident not in seen_l:
            seen_l.add(ident)
            links.append({"type": l.get("type") or "closeMatch", "identifier": ident})
    if links:
        place_doc["links"] = links

    # --- relations: containment → within hgis:<territorio> ---------------
    rels, seen_r = [], set()
    for r in (p.get("related") or []):
        pid = _dokuid(r.get("relation_to"))
        if not pid:
            continue
        target = f"{NAMESPACE}:{pid}"
        if target == place_id or target in seen_r:
            continue
        seen_r.add(target)
        rels.append({"relation_type": "within", "related_place_id": target,
                     "label": (r.get("label") or "").strip(),
                     "timespans": _timespans(r.get("when")) or list(_DEFAULT_TS)})
    if rels:
        place_doc["relations"] = rels

    return place_doc


def _iter_features(lpf_path):
    """Yield LPF features. Tolerates a truncated FeatureCollection (recovers
    complete features) — though a complete file is expected."""
    t = Path(lpf_path).read_text(encoding="utf-8").rstrip()
    if t.endswith("},"):
        t = t[:-1] + "]}"
    elif not t.endswith("}"):
        t = t.rstrip(", \n\t") + "]}"
    yield from json.loads(t).get("features", [])


def _resolve(name):
    p = Path(name)
    if p.exists():
        return p
    for base in (Path(DATA_DIR) / "authorities" / NAMESPACE,
                 Path(__file__).resolve().parent / NAMESPACE):
        cand = base / Path(name).name
        if cand.exists():
            return cand
    return p


def stage_hgis(files, limit=None, dry=False):
    import contextlib
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    staged = skipped = errors = 0
    start = datetime.now()

    def _run():
        nonlocal staged, skipped, errors
        for fn in files:
            path = _resolve(fn)
            if not path.exists():
                print(f"ERROR: not found: {path}")
                continue
            print(f"Processing {path} ...")
            for i, feat in enumerate(_iter_features(path)):
                if limit and i >= limit:
                    break
                try:
                    doc = process_feature(feat)
                    if not doc:
                        skipped += 1
                        continue
                    if dry:
                        print(json.dumps(doc, ensure_ascii=False)[:1400])
                        staged += 1
                        continue
                    write_staged_place_doc(namespace=NAMESPACE, doc=doc)
                    staged += 1
                    if staged % 2000 == 0:
                        print(f"\r  staged: {staged}", end="", flush=True)
                except Exception as e:
                    print(f"\n  ERROR {path.name} #{i} "
                          f"(src_id={(feat.get('properties') or {}).get('src_id')!r}): {e}")
                    errors += 1

    # Territorios are polygons → their WKB must be written to the geom-store
    # staging via a configured module writer (points carry no geom_key and ignore
    # it). Skip the writer in --dry (inspection only). NAMESPACE is the shard name.
    if dry:
        _run()
    else:
        with GeomStoreWriter(GEOM_STORE_STAGING_DIR, NAMESPACE) as gsw:
            configure_module_writer(gsw)
            try:
                _run()
            finally:
                configure_module_writer(None)

    print(f"\n{'=' * 80}\nHGIS STAGING {'(DRY)' if dry else 'COMPLETE'}\n{'=' * 80}")
    print(f"Time: {(datetime.now() - start).seconds}s")
    print(f"Staged: {staged:,}\nSkipped: {skipped:,}\nErrors: {errors:,}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Stage HGIS de las Indias (lugares + territorios) as one `hgis` authority")
    ap.add_argument("--files", nargs="*", help="LPF paths (default: from AUTHORITIES['hgis'])")
    ap.add_argument("--limit", type=int, help="Process only the first N features per file")
    ap.add_argument("--dry", action="store_true", help="Print docs instead of staging")
    args = ap.parse_args()

    if args.files:
        files = args.files
    elif HGIS_CONFIG and HGIS_CONFIG.get("files"):
        files = [f.get("name") or Path(f["url"]).name for f in HGIS_CONFIG["files"]]
    else:
        print("ERROR: no --files and no AUTHORITIES['hgis'] config")
        sys.exit(1)

    print(f"HGIS de las Indias (STAGING)\nFiles: {files}\n")
    stage_hgis(files, limit=args.limit, dry=args.dry)
