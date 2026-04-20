# authorities/chgis/places.py

"""
Index CHGIS/TGAZ places into Elasticsearch.

Reads from the pre-built SQLite database (tgaz.db) produced by
build_database.py.  Each placename record with valid coordinates becomes
a place document with rich temporal, hierarchical, and multilingual data.

Usage:
    python -m authorities.chgis.places
    python -m authorities.chgis.places --places-index places_20260401

Records: ~82K places (100% with coordinates).
"""

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from elasticsearch import Elasticsearch, helpers
from processing.helpers import enrich_geometry
from processing.settings import ES_HOST, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

DIR = Path(__file__).resolve().parent
DB_FILE = DIR / "tgaz.db"

es = Elasticsearch(ES_HOST, request_timeout=180)

# Year sentinel: 9999 = "still existing" in CHGIS
STILL_EXISTING = 9999


def ensure_database():
    """Build the SQLite database if it doesn't exist."""
    if DB_FILE.exists():
        return
    print("Database not found — building from SQL dump...")
    subprocess.run(
        [sys.executable, "-m", "authorities.chgis.build_database"],
        check=True,
    )
    if not DB_FILE.exists():
        print(f"ERROR: Database still not found after build: {DB_FILE}")
        sys.exit(1)


def load_lookups(conn: sqlite3.Connection) -> dict:
    """Preload all lookup tables into memory."""
    cur = conn.cursor()

    # Script ID → language code
    cur.execute("SELECT id, lang FROM script")
    scripts = {}
    for row in cur.fetchall():
        scripts[row[0]] = row[1] or "und"

    # Feature type ID → type dict
    cur.execute("SELECT id, name_en, name_vn, name_tr, adl_class FROM ftype")
    ftypes = {}
    for row in cur.fetchall():
        fid, name_en, name_vn, name_tr, adl_class = row
        ftypes[fid] = {
            "identifier": name_en or name_tr or str(fid),
            "label": "chgis",
            "sourceLabel": name_vn or name_en or name_tr or str(fid),
        }

    # Placename ID → sys_id mapping
    cur.execute("SELECT id, sys_id FROM placename")
    id_to_sys = {row[0]: row[1] for row in cur.fetchall()}

    # part_of relations: child_id → [(parent_id, begin_year, end_year), ...]
    cur.execute("SELECT child_id, parent_id, begin_year, end_year FROM part_of")
    part_of = {}
    for child, parent, beg, end in cur.fetchall():
        part_of.setdefault(child, []).append((parent, beg, end))

    # prec_by relations: placename_id → [prec_id, ...]
    cur.execute("SELECT placename_id, prec_id FROM prec_by")
    prec_by = {}
    for pn_id, prec_id in cur.fetchall():
        prec_by.setdefault(pn_id, []).append(prec_id)

    # present_loc: placename_id → country_code
    cur.execute("SELECT placename_id, country_code FROM present_loc")
    ccodes_map = {}
    for pn_id, cc in cur.fetchall():
        if cc and len(cc) == 2:
            ccodes_map.setdefault(pn_id, set()).add(cc.upper())

    # Wikidata links: data_src_ref → [(qid, geonames_id), ...]
    wikidata = {}
    try:
        cur.execute("SELECT data_src_ref, qid, geonames_id FROM wikidata_links")
        for ref, qid, gn_id in cur.fetchall():
            wikidata.setdefault(ref, []).append((qid, gn_id))
    except Exception:
        pass  # table may not exist

    # Spellings: placename_id → [(written_form, script_id, trsys_id, exonym_lang), ...]
    cur.execute("SELECT placename_id, written_form, script_id, trsys_id, exonym_lang, default_per_type FROM spelling")
    spellings = {}
    for pn_id, form, script_id, trsys_id, exonym_lang, default in cur.fetchall():
        spellings.setdefault(pn_id, []).append((form, script_id, trsys_id, exonym_lang, default))

    return {
        "scripts": scripts,
        "ftypes": ftypes,
        "id_to_sys": id_to_sys,
        "part_of": part_of,
        "prec_by": prec_by,
        "ccodes": ccodes_map,
        "wikidata": wikidata,
        "spellings": spellings,
    }


def build_timespans(beg_yr, end_yr):
    """Build timespans list from begin/end years."""
    if beg_yr is None:
        return []
    ts = {"start": {"in": int(beg_yr)}}
    if end_yr is not None and end_yr != STILL_EXISTING:
        ts["end"] = {"in": int(end_yr)}
    return [ts]


def build_document(row, lookups):
    """Build an ES document from a placename row."""
    pn_id = row["id"]
    sys_id = row["sys_id"]
    ftype_id = row["ftype_id"]
    beg_yr = row["beg_yr"]
    end_yr = row["end_yr"]
    x_coord = row["x_coord"]
    y_coord = row["y_coord"]
    data_src_ref = row["data_src_ref"]

    place_id = f"chgis:{sys_id}"
    timespans = build_timespans(beg_yr, end_yr)

    # ── Toponyms ──
    toponyms = []
    seen_toponyms = set()
    title = None
    spell_list = lookups["spellings"].get(pn_id, [])

    for form, script_id, trsys_id, exonym_lang, default in spell_list:
        if not form:
            continue
        lang = exonym_lang or lookups["scripts"].get(script_id, "und")
        # Normalise lang
        if lang == "n/a" or lang == "na":
            lang = "und"

        toponym_id = f"{form}@{lang}"
        if toponym_id not in seen_toponyms:
            seen_toponyms.add(toponym_id)
            toponyms.append({"toponym_id": toponym_id, "timespans": timespans})

        # Pick title: prefer default romanised/Pinyin form
        if title is None:
            title = form
        if default and lang in ("und", "en"):
            title = form

    if not title:
        title = sys_id

    # ── Geometry ──
    geometries = []
    try:
        x = float(x_coord)
        y = float(y_coord)
        if -180 <= x <= 180 and -90 <= y <= 90:
            geom = {"type": "Point", "coordinates": [x, y]}
            geometries.append(enrich_geometry(geom, timespans=timespans or None))
    except (ValueError, TypeError):
        pass

    # ── Types ──
    types = []
    ftype = lookups["ftypes"].get(ftype_id)
    if ftype:
        types.append(ftype)

    # ── Country codes ──
    ccodes = sorted(lookups["ccodes"].get(pn_id, []))

    # ── Relations ──
    relations = []
    id_to_sys = lookups["id_to_sys"]

    # part_of
    for parent_id, po_beg, po_end in lookups["part_of"].get(pn_id, []):
        parent_sys = id_to_sys.get(parent_id)
        if parent_sys:
            rel = {
                "relation_type": "partOf",
                "related_place_id": f"chgis:{parent_sys}",
                "label": "",
            }
            rel_ts = build_timespans(po_beg, po_end)
            if rel_ts:
                rel["timespans"] = rel_ts
            relations.append(rel)

    # preceded_by
    for prec_id in lookups["prec_by"].get(pn_id, []):
        prec_sys = id_to_sys.get(prec_id)
        if prec_sys:
            relations.append({
                "relation_type": "precededBy",
                "related_place_id": f"chgis:{prec_sys}",
                "label": "",
            })

    # ── Links (Wikidata) ──
    links = []
    if data_src_ref:
        for qid, gn_id in lookups["wikidata"].get(data_src_ref, []):
            if qid:
                links.append({"type": "sameAs", "identifier": f"wd:{qid}"})
            if gn_id:
                links.append({"type": "sameAs", "identifier": f"gn:{gn_id}"})

    doc = {
        "place_id": place_id,
        "title": title,
        "toponyms": toponyms,
        "geometries": geometries,
        "types": types,
        "ccodes": ccodes,
        "relations": relations,
        "links": links,
    }
    if timespans:
        doc["timespans"] = timespans

    return doc


def index_chgis(places_index: str):
    """Read CHGIS SQLite database and bulk-index into ES."""
    ensure_database()

    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row

    print("Loading lookup tables...")
    lookups = load_lookups(conn)
    print(f"  {len(lookups['id_to_sys']):,} placenames")
    print(f"  {sum(len(v) for v in lookups['spellings'].values()):,} spellings")
    print(f"  {sum(len(v) for v in lookups['part_of'].values()):,} part_of relations")
    print(f"  {sum(len(v) for v in lookups['prec_by'].values()):,} prec_by relations")
    print(f"  {sum(len(v) for v in lookups['wikidata'].values()):,} Wikidata links")

    cur = conn.cursor()
    cur.execute("""
        SELECT id, sys_id, ftype_id, data_src, data_src_ref,
               beg_yr, end_yr, obj_type, x_coord, y_coord
        FROM placename
        WHERE x_coord IS NOT NULL AND x_coord != ''
          AND y_coord IS NOT NULL AND y_coord != ''
    """)

    start_time = time.time()
    indexed = 0
    errors = 0
    batch = []

    for row in cur:
        doc = build_document(row, lookups)
        batch.append({
            "_index": places_index,
            "_id": doc["place_id"],
            "_source": doc,
        })

        if len(batch) >= BATCH_SIZE:
            success, failed = helpers.bulk(
                es, batch, raise_on_error=False, raise_on_exception=False,
                stats_only=True,
            )
            indexed += success
            errors += failed
            elapsed = time.time() - start_time
            rate = indexed / elapsed if elapsed > 0 else 0
            print(f"\r  Indexed: {indexed:,}  Errors: {errors:,}  "
                  f"Rate: {rate:.0f} docs/s", end="", flush=True)
            batch = []

    # Final batch
    if batch:
        success, failed = helpers.bulk(
            es, batch, raise_on_error=False, raise_on_exception=False,
            stats_only=True,
        )
        indexed += success
        errors += failed

    conn.close()
    elapsed = time.time() - start_time

    print(f"\n\n{'=' * 60}")
    print(f"  CHGIS/TGAZ INGESTION COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Indexed:  {indexed:,}")
    print(f"  Errors:   {errors:,}")
    print(f"  Time:     {elapsed:.0f}s ({elapsed / 60:.1f}m)")
    if elapsed > 0:
        print(f"  Rate:     {indexed / elapsed:.0f} docs/s")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index CHGIS/TGAZ places")
    parser.add_argument(
        "--places-index", default="places",
        help="Target ES index name (default: places)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("CHGIS / TGAZ PLACES INGESTION")
    print("=" * 60)
    print(f"Source: {DB_FILE}")
    print(f"Target index: {args.places_index}")
    print()

    index_chgis(args.places_index)

    print("Creating checkpoint snapshot...")
    create_checkpoint_snapshot(es, "chgis_places")

    print("COMPLETE")

