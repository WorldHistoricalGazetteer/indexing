# authorities/chgis/places.py

"""
Stage CHGIS/TGAZ places to the staged extract directory.

Reads from the pre-built SQLite database (tgaz.db) produced by
build_database.py. Each placename record with valid coordinates becomes
a place document with rich temporal, hierarchical, and multilingual data.

Output: ``{STAGED_BASE_DIR}/chgis/extract/places.jsonl``

ES indexing for this authority happens later via ``index_from_stage`` —
this script no longer talks to Elasticsearch.

Records: ~82K places (100% with coordinates).
"""

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from processing.helpers import (
    enrich_geometry,
    merge_place_docs,
    write_staged_place_doc,
)

NAMESPACE = "chgis"
DIR = Path(__file__).resolve().parent
DB_FILE = DIR / "tgaz.db"

# Year sentinel: 9999 = "still existing" in CHGIS
STILL_EXISTING = 9999


def ensure_database():
    """Build the SQLite database if it's missing or empty.

    The earlier behaviour (rebuild only if missing) silently produced
    zero-row outputs when an empty placeholder ``tgaz.db`` already
    existed — typically when a prior build aborted because the SQL
    dump on disk was a Git LFS pointer instead of the real content.
    Now we also trigger a rebuild when the ``placename`` table is
    empty (or the schema isn't there at all), which covers both cases.
    """
    rebuild = not DB_FILE.exists()
    if not rebuild:
        try:
            conn = sqlite3.connect(str(DB_FILE))
            try:
                row = conn.execute("SELECT COUNT(*) FROM placename").fetchone()
                if not row or row[0] == 0:
                    rebuild = True
            except sqlite3.OperationalError:
                # Schema not initialised — treat as missing.
                rebuild = True
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            # Corrupt/empty file — re-build.
            rebuild = True

    if not rebuild:
        return

    if DB_FILE.exists():
        print(f"Existing database is empty/invalid — rebuilding from SQL dump...")
        DB_FILE.unlink()
    else:
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


def stage_chgis():
    """Read CHGIS SQLite database and write staged place docs."""
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

    # ORDER BY sys_id groups the rows for one place together so they can be
    # merged in a single pass without holding the whole table in memory.
    #
    # place_id is chgis:<sys_id>, but sys_id is NOT the primary key of
    # `placename` — `id` is, and TGAZ carries up to 11 byte-identical rows per
    # sys_id (differing only in that surrogate). Emitting one document per row
    # produced 82,117 documents for 81,292 places; bulk indexing then collapsed
    # them on _id, silently, keeping whichever was written last. Because
    # h3_stage had enriched only one copy, 127 places ended up with a null
    # h3_cover and disappeared from fuzzy spatial containment.
    cur = conn.cursor()
    cur.execute("""
        SELECT id, sys_id, ftype_id, data_src, data_src_ref,
               beg_yr, end_yr, obj_type, x_coord, y_coord
        FROM placename
        WHERE x_coord IS NOT NULL AND x_coord != ''
          AND y_coord IS NOT NULL AND y_coord != ''
        ORDER BY sys_id, id
    """)

    start_time = time.time()
    staged = 0
    merged_rows = 0

    def _flush(group):
        """Emit one document for a sys_id, merging its rows."""
        nonlocal staged, merged_rows
        if not group:
            return
        docs = [build_document(r, lookups) for r in group]
        docs = [d for d in docs if d]
        if not docs:
            return
        if len(docs) > 1:
            merged_rows += len(docs) - 1
        write_staged_place_doc(NAMESPACE, merge_place_docs(docs))
        staged += 1

    group: list = []
    current_sys_id = None
    for row in cur:
        sys_id = row["sys_id"]
        if sys_id != current_sys_id:
            _flush(group)
            group, current_sys_id = [], sys_id
        group.append(row)
        if staged and staged % 5_000 == 0:
            elapsed = time.time() - start_time
            rate = staged / elapsed if elapsed > 0 else 0
            print(f"\r  Staged: {staged:,}  Rate: {rate:.0f} docs/s",
                  end="", flush=True)

    _flush(group)  # the final sys_id has no successor to trigger its flush

    conn.close()
    elapsed = time.time() - start_time

    print(f"\n\n{'=' * 60}")
    print(f"  CHGIS/TGAZ STAGING COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Staged:   {staged:,}")
    print(f"  Merged:   {merged_rows:,} duplicate placename rows folded into "
          f"their sys_id")
    print(f"  Time:     {elapsed:.0f}s ({elapsed / 60:.1f}m)")
    if elapsed > 0:
        print(f"  Rate:     {staged / elapsed:.0f} docs/s")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage CHGIS/TGAZ places")
    args = parser.parse_args()

    print("=" * 60)
    print("CHGIS / TGAZ PLACES STAGING")
    print("=" * 60)
    print(f"Source: {DB_FILE}")
    print()

    stage_chgis()
    print("COMPLETE")

