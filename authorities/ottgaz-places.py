# authorities/ottgaz-places.py

"""
Stage the Ottoman Gazetteer ("ottgaz", Will Hanley, FSU) — a vocabulary of
Ottoman administrative units (eyalet/vilayet, sancak, kaza, nahiye, + admin
seats) transformed from Tahir Sezen, *Osmanlı Yer Adları* — to the staged
extract for namespace ``og``.

Source: https://ottgaz.org  /  https://github.com/whanley/Ottoman-Gazetteer
        (data/archived-versions/ottgaz-data-9.tsv)  — Licence: CC-BY-NC 4.0.

ottgaz carries NO native coordinates (only ~222 records link to Wikidata).
Geometry is therefore WHG-COMPUTED, flagged via the geometries[] provenance
fields established for this:
  * source        = 'ofs'  → convex hull of the member ``ofs`` points in this
                             admin unit (this script; an AREA approximation).
                  = 'wd'   → geometry pulled from our linked Wikidata record
                             (post-index UPGRADE — applied ONLY when that record
                             has a polygon richer than the ofs hull; a bare wd
                             point never replaces a computed hull).
                  = 'og'   → inherent in ottgaz (none today).
  * approximation = 'convex_hull' | 'centroid' for the computed geoms above;
                    'exact' for a real wd polygon.
This script computes the 'ofs' hull for EVERY ofs-matching unit (incl. those
with a WD link — an area beats a point); the WD upgrade happens later, pitt-side.

Output: ``{STAGED_BASE_DIR}/og/extract/places.jsonl``.
ES indexing happens later via ``processing.index_namespace`` (geometry-less
records are fine — WHG allows them; they gain geometry later via WD/attestation).
"""
from __future__ import annotations

import csv
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from shapely.geometry import MultiPoint, mapping

from processing.helpers import (
    enrich_geometry,
    compute_h3_fields,
    select_h3_cover_geometry,
    write_staged_place_doc,
)
from processing.settings import DATA_DIR, STAGED_BASE_DIR, AUTHORITIES
from processing.temporal import bounded, lifespan

NAMESPACE = "og"
OG_CONFIG = next((a for a in AUTHORITIES if a["namespace"] == NAMESPACE), None)

# Ottoman admin geography has no single defining period in Sezen, so an
# undated unit is bounded only by the empire itself.
EMPIRE_FIRST, EMPIRE_LAST = 1300, 1922


def unit_timespans(start_year: int | None, end_year: int | None) -> list[dict]:
    """Timespans for one Sezen admin unit, bounded by the empire's own span.

    Sezen's ``StartDate``/``EndDate`` are genuine administrative lifespans, so
    a known year uses ``in``. What is *not* a lifespan is the fallback: this
    used to write ``[{"start": {"in": 1300}, "end": {"in": 1922}}]`` for every
    undated unit, asserting that each one came into being in 1300 and ceased in
    1922 — 622 years of existence Sezen never claims (place#164). The honest
    reading of "an Ottoman unit of unknown date" is outer bounds only: possibly
    alive across the empire, definitely alive at no year we can name.

    The same bound closes a half-dated unit — a unit with a known start cannot
    have outlived the empire, and one with a known end cannot predate it.
    """
    if start_year is not None and end_year is not None:
        return lifespan(start_year, end_year)
    if start_year is None and end_year is None:
        return bounded(start_earliest=EMPIRE_FIRST, end_latest=EMPIRE_LAST)
    ts = lifespan(start_year, end_year)  # closure fills start.latest when only an end
    if not ts:
        return ts
    if end_year is None:
        ts[0].setdefault("end", {})["latest"] = EMPIRE_LAST
    else:
        ts[0].setdefault("start", {})["earliest"] = EMPIRE_FIRST
    return ts

# Ottoman admin level → Getty AAT (WHG is AAT-first; the Unit string is kept as
# sourceLabel for provenance). IDs verified against vocab.getty.edu:
#   provinces 300000774 · districts 300000705 · counties 300000771
#   inhabited places 300008347 · political administrative bodies 300261086
_UNIT_TO_AAT = {
    "eyalet": (300000774, "provinces"),
    "vilayet": (300000774, "provinces"),
    "sancak": (300000705, "districts"),     # sancak/liva ≈ sub-province
    "liva": (300000705, "districts"),
    "kaza": (300000771, "counties"),        # kaza ≈ county / district
    "nahiye": (300000705, "districts"),     # nahiye ≈ sub-district (nearest)
}
_AAT_ADMIN_BODY = 300261086   # political administrative bodies (unknown level)
_AAT_INHABITED = 300008347    # inhabited places (admin seats, GeoNames P.PPLG)

_TR = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ç": "c", "Ç": "c",
                     "ğ": "g", "Ğ": "g", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
                     "â": "a", "î": "i", "û": "u", "Î": "i", "Â": "a"})


def _norm(s: str) -> str:
    if not s:
        return ""
    s = s.translate(_TR)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _basename(s: str) -> str:
    """Strip Ottoman register compounding: 'X maa ...', 'X ve ...'."""
    return re.split(r"\s+ma['’ ]?a\s+|\s+ve\s+", s or "", maxsplit=1)[0]


# ---------------------------------------------------------------------------
# ofs member-point index (for computed convex hulls)
# ---------------------------------------------------------------------------

def build_ofs_point_index() -> dict:
    """Index ofs repr_points by admin level for hull computation.

    Keys (all normalised, with maa/ve base-name folding so 'Aksaray' matches the
    register form 'Aksaray maa nevâhî'):
      ('sancak', liva)          → [(lon,lat), …]
      ('kaza',   liva, kaza)    → […]   (disambiguated kaza-within-sancak)
      ('kaza',   kaza)          → […]   (fallback, any sancak)
      ('nahiye', nahiye)        → […]
    """
    path = Path(STAGED_BASE_DIR) / "ofs" / "extract" / "places.jsonl"
    idx: dict = defaultdict(list)
    if not path.exists():
        print(f"WARNING: ofs extract not found ({path}); no computed hulls.")
        return idx
    n = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            d = json.loads(line)
            geoms = d.get("geometries") or []
            rp = geoms[0].get("repr_point") if geoms else None
            if not rp:
                continue
            pt = (rp["lon"], rp["lat"])
            liva = _norm(d.get("liva_1848"))
            kaza = _norm(d.get("kaza_1848"))
            kaza_b = _norm(_basename(d.get("kaza_1848") or ""))
            nahiye = _norm(d.get("nahiye"))
            for lv in {liva}:
                if lv:
                    idx[("sancak", lv)].append(pt)
            for kz in {kaza, kaza_b}:
                if kz:
                    idx[("kaza", kz)].append(pt)
                    if liva:
                        idx[("kaza", liva, kz)].append(pt)
            if nahiye:
                idx[("nahiye", nahiye)].append(pt)
            n += 1
    print(f"ofs point index: {n:,} places → {len(idx):,} admin keys")
    return idx


def _hull_geometry(points: list) -> tuple[dict | None, str | None]:
    """Convex hull (or centroid) GeoJSON for a set of (lon,lat) points."""
    uniq = list({(round(x, 6), round(y, 6)) for x, y in points})
    if not uniq:
        return None, None
    if len(uniq) < 3:
        # 1-2 points: no area — use the centroid as a representative point.
        cx = sum(p[0] for p in uniq) / len(uniq)
        cy = sum(p[1] for p in uniq) / len(uniq)
        return {"type": "Point", "coordinates": [round(cx, 6), round(cy, 6)]}, "centroid"
    hull = MultiPoint(uniq).convex_hull
    geo = mapping(hull)
    return geo, ("convex_hull" if geo.get("type") in ("Polygon", "MultiPolygon") else "centroid")


def _match_points(unit: str, name_n: str, parents_n: list, ofs_idx: dict) -> list:
    """Find ofs member points for an ottgaz unit by level + name (+ parent)."""
    if unit == "sancak":
        return ofs_idx.get(("sancak", name_n), [])
    if unit == "kaza":
        for p in parents_n:                       # disambiguate kaza-within-sancak
            hit = ofs_idx.get(("kaza", p, name_n))
            if hit:
                return hit
        return ofs_idx.get(("kaza", name_n), [])
    if unit == "nahiye":
        return ofs_idx.get(("nahiye", name_n), [])
    return []  # eyalet/vilayet: geometry-less for now (union of children later)


# ---------------------------------------------------------------------------
# Row → place doc
# ---------------------------------------------------------------------------

_TR_NAME_COLS = ["Placename@tr", "Placename_2@tr", "Placename_3@tr",
                 "Placename_4@tr", "Placename_5@tr"]
_OTA_NAME_COLS = ["Placename@ota", "Placename_2@ota", "Placename_3@ota"]


def _add_toponym(name, lang, ts, toponyms, seen):
    name = (name or "").strip()
    if not name:
        return
    lst = f"{name}@{lang}"
    if lst in seen:
        return
    seen.add(lst)
    toponyms.append({"toponym_id": lst, "timespans": ts})


def _year(s):
    m = re.search(r"-?\d{3,4}", str(s or ""))
    return int(m.group(0)) if m else None


def process_row(row, ofs_idx, name_to_id):
    oid = (row.get("ottgaz_id") or "").strip()
    if not oid:
        return None  # placeholder/hierarchy row, no stable id
    title = (row.get("Placename@tr") or "").strip()
    if not title:
        return None
    place_id = f"{NAMESPACE}:{oid}"
    unit = (row.get("Unit") or "").strip()

    ts = unit_timespans(_year(row.get("StartDate")), _year(row.get("EndDate")))

    toponyms, seen = [], set()
    for c in _TR_NAME_COLS:
        _add_toponym(row.get(c), "tr", ts, toponyms, seen)
    for c in _OTA_NAME_COLS:
        _add_toponym(row.get(c), "ota", ts, toponyms, seen)
    if not toponyms:
        return None

    doc = {"place_id": place_id, "title": title, "toponyms": toponyms, "geometries": []}

    # --- type: map the Ottoman admin level to Getty AAT (AAT-first) ----------
    types = []
    aat = _UNIT_TO_AAT.get(_norm(unit))
    if aat:
        aid, term = aat
        types.append({"identifier": f"aat:{aid}", "aat_ids": [aid],
                      "label": "ottgaz", "sourceLabel": f"unit:{unit}"})
    # GeoNames P.PPLG = seat of government → also an inhabited place
    if (row.get("Place_type") or "").strip().endswith("P.PPLG"):
        types.append({"identifier": f"aat:{_AAT_INHABITED}", "aat_ids": [_AAT_INHABITED],
                      "label": "ottgaz", "sourceLabel": "seat (P.PPLG)"})
    if not types:  # unknown/blank Unit → generic administrative body
        types.append({"identifier": f"aat:{_AAT_ADMIN_BODY}", "aat_ids": [_AAT_ADMIN_BODY],
                      "label": "ottgaz", "sourceLabel": f"unit:{unit}" if unit else "ottgaz"})
    doc["types"] = types

    # --- relations: belongsTo hierarchy + Wikidata ---------------------------
    relations = []
    parents_n = []
    for col in ("belongsTo1", "belongsTo2", "belongsTo3", "belongsTo4"):
        pname = (row.get(col) or "").strip()
        if not pname:
            continue
        pn = _norm(pname)
        if pn:
            parents_n.append(pn)
        pid = name_to_id.get(pn) or name_to_id.get(_norm(_basename(pname)))
        related = f"{NAMESPACE}:{pid}" if pid else f"og-admin:{_norm(_basename(pname)) or pn}"
        relations.append({"relation_type": "within", "related_place_id": related,
                          "label": f"belongsTo: {pname}", "timespans": ts})

    wd_url = (row.get("Wikidata_url") or "").strip()
    qid = None
    m = re.search(r"Q\d+", wd_url)
    if m:
        qid = m.group(0)
        relations.append({"relation_type": "closeMatch",
                          "related_place_id": f"wd:{qid}", "label": "Wikidata"})
    if relations:
        doc["relations"] = relations
    if qid:
        doc["wikidata_qid"] = qid  # marker for the post-index 'wd' geometry pull

    # --- geometry: computed convex hull from ofs member points ---------------
    # Compute the 'ofs' hull for EVERY unit that matches ofs members — an admin
    # AREA beats a single point, so we do this even for wikidata-linked units.
    # A post-index step then UPGRADES a unit to its 'wd' geometry ONLY when that
    # record carries a polygon richer than this hull (never hull → bare point).
    pts = _match_points(unit.lower(), _norm(title), parents_n, ofs_idx)
    geo, approx = _hull_geometry(pts) if pts else (None, None)
    if geo:
        # geom_key is REQUIRED for the hull's WKB to reach the geom store —
        # without it enrich_geometry computes repr_point/hull/bounds, returns
        # has_geom=False, and the polygon itself is discarded. og shipped
        # without it, so its computed hulls were unservable (no exact
        # containment, no geometry retrieval) and untileable. Matches the
        # `{place_id}_{geometry_index}` convention; og emits one geometry.
        ge = enrich_geometry(geo, timespans=ts, geom_key=f"{place_id}_0")
        if ge:
            ge["source"] = "ofs"
            ge["approximation"] = approx
            rp = ge.get("repr_point")
            if rp:
                h3g = select_h3_cover_geometry(ge, geo)
                h3c, h3cov = compute_h3_fields(rp["lon"], rp["lat"], h3g)
                if h3c:
                    doc["h3_centroid"] = h3c
                    doc["h3_cover"] = h3cov
            doc["geometries"] = [ge]

    if unit:
        doc["admin_unit"] = unit
    return doc


def build_name_index(rows) -> dict:
    """norm(Placename@tr) → ottgaz_id, preferring lead entries (for belongsTo)."""
    idx = {}
    for row in rows:
        oid = (row.get("ottgaz_id") or "").strip()
        nm = _norm(row.get("Placename@tr") or "")
        if not oid or not nm:
            continue
        lead = (row.get("Lead entry?") or "").strip().lower() == "y"
        if nm not in idx or lead:
            idx[nm] = oid
    return idx


def stage_file(tsv_path):
    print(f"Processing ottgaz: {tsv_path}")
    if not Path(tsv_path).exists():
        std = Path(DATA_DIR) / "authorities" / NAMESPACE / Path(tsv_path).name
        if std.exists():
            tsv_path = std
        else:
            print(f"ERROR: file not found: {tsv_path}")
            return
    with open(tsv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    print(f"Read {len(rows):,} rows")

    ofs_idx = build_ofs_point_index()
    name_to_id = build_name_index(rows)

    staged = skipped = with_geom = 0
    stored_geom = 0
    start = datetime.now()

    def _run():
        nonlocal staged, skipped, with_geom, stored_geom
        for i, row in enumerate(rows):
            if (i + 1) % 2000 == 0:
                print(f"\r  {i + 1}/{len(rows)} staged={staged} geom={with_geom}", end="", flush=True)
            try:
                doc = process_row(row, ofs_idx, name_to_id)
                if not doc:
                    skipped += 1
                    continue
                if doc["geometries"]:
                    with_geom += 1
                    if doc["geometries"][0].get("has_geom"):
                        stored_geom += 1
                write_staged_place_doc(namespace=NAMESPACE, doc=doc)
                staged += 1
            except Exception as e:
                print(f"\n  ERROR row {i}: {e}")
                skipped += 1

    # og's hulls are polygons, so their WKB must reach the geom-store staging
    # dir through a configured module writer — enrich_geometry ignores
    # geom_key without one. NAMESPACE is the shard name. Same pattern as
    # hgis-places.py:314.
    from processing.geom_store import GeomStoreWriter, configure_module_writer
    from processing.settings import GEOM_STORE_STAGING_DIR

    with GeomStoreWriter(GEOM_STORE_STAGING_DIR, NAMESPACE) as gsw:
        configure_module_writer(gsw)
        try:
            _run()
        finally:
            configure_module_writer(None)

    print(f"\n{'=' * 70}\nOTTGAZ STAGING COMPLETE\n{'=' * 70}")
    print(f"Time: {(datetime.now() - start).seconds}s")
    print(f"Staged: {staged:,}  (with computed geometry: {with_geom:,})")
    print(f"Skipped (no id/name): {skipped:,}")
    # Report the two counts separately. They were identical before the
    # geom_key fix only in the sense that BOTH described documents whose
    # polygon had been thrown away — `with_geom` counts geometry entries,
    # `stored_geom` counts polygons that actually reached the store, and a
    # divergence is the defect predicate.
    print(f"Geometries written to store: {stored_geom:,}")
    if with_geom != stored_geom:
        print(f"  ** WARNING: {with_geom - stored_geom:,} geometry entries did "
              f"NOT reach the geom store — has_geom will lie for those docs **")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Stage the ottgaz Ottoman gazetteer")
    p.add_argument("--file", help="Path to ottgaz-data-N.tsv")
    args = p.parse_args()
    if args.file:
        tsv = args.file
    elif OG_CONFIG and OG_CONFIG.get("files"):
        name = OG_CONFIG["files"][0].get("name") or Path(OG_CONFIG["files"][0]["url"]).name
        tsv = Path(DATA_DIR) / "authorities" / NAMESPACE / name
    else:
        print("ERROR: no --file and no AUTHORITIES['og'] config")
        sys.exit(1)
    print(f"ottgaz (STAGING)\nFile: {tsv}\n")
    stage_file(str(tsv))
