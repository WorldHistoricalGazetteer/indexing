# authorities/dgsd/places.py

"""
Stage DGSD (Digital Gazetteer of the Song Dynasty) places to the staged
extract directory.

Reads from the pre-built SQLite database (dgsd.db) produced by
build_database.py. Each entity with valid coordinates becomes a place
document; entities without coordinates are also staged for their toponym
and temporal value.

Output: ``{STAGED_BASE_DIR}/dgsd/extract/places.jsonl``

ES indexing for this authority happens later via ``index_from_stage`` —
this script no longer talks to Elasticsearch.

Records: ~3.8K entities (2,006 with coordinates).
"""

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from processing.helpers import enrich_geometry, write_staged_place_doc

NAMESPACE = "dgsd"
DIR = Path(__file__).resolve().parent
DB_FILE = DIR / "dgsd.db"


def ensure_database():
    """Build the SQLite database if it doesn't exist."""
    if DB_FILE.exists():
        return
    print("Database not found — building from SQL dump...")
    subprocess.run(
        [sys.executable, "-m", "authorities.dgsd.build_database"],
        check=True,
    )
    if not DB_FILE.exists():
        print(f"ERROR: Database still not found after build: {DB_FILE}")
        sys.exit(1)


def load_lookups(conn: sqlite3.Connection) -> dict:
    """Preload all lookup tables into memory."""
    cur = conn.cursor()

    # Feature types: id → {identifier, label, sourceLabel}
    cur.execute("SELECT id, english, chinese, pinyin FROM feature_type")
    ftypes = {}
    for fid, en, ch, py in cur.fetchall():
        ftypes[fid] = {
            "identifier": en or py or str(fid),
            "label": "dgsd",
            "sourceLabel": ch or en or py or str(fid),
        }

    # Best coordinates per entity (lowest priority number = highest priority)
    cur.execute("""
        SELECT entity_id, x_coord, y_coord
        FROM point_location
        WHERE x_coord IS NOT NULL AND y_coord IS NOT NULL
        ORDER BY entity_id, priority ASC
    """)
    coords = {}
    for eid, x, y in cur.fetchall():
        if eid not in coords:
            coords[eid] = (x, y)

    # Historical instances per entity
    cur.execute("""
        SELECT entity_id, pinyin, chinese, begin_date, end_date,
               feature_type, prefecture, circuit
        FROM historical_instance
        ORDER BY entity_id, begin_date
    """)
    instances = {}
    for eid, py, ch, beg, end, ft, pref, circ in cur.fetchall():
        instances.setdefault(eid, []).append({
            "pinyin": py, "chinese": ch,
            "begin_date": beg, "end_date": end,
            "feature_type": ft, "prefecture": pref, "circuit": circ,
        })

    # Entity pinyin → id mapping for parent resolution
    cur.execute("SELECT id, pinyin FROM entity")
    pinyin_to_id = {}
    for eid, py in cur.fetchall():
        if py:
            pinyin_to_id[py] = eid

    # Population attribute type IDs
    # Attribute types for households (hu) and persons (kou)
    cur.execute("SELECT id, english FROM attribute_type WHERE english LIKE '%household%' OR english LIKE '%person%' OR english LIKE '%population%'")
    pop_type_ids = {row[0] for row in cur.fetchall()}

    # Population attributes per entity (sum of household counts as proxy)
    pop_map = {}
    if pop_type_ids:
        placeholders = ",".join("?" * len(pop_type_ids))
        cur.execute(
            f"SELECT entity_id, MAX(numeric_value) FROM attribute "
            f"WHERE attribute_type IN ({placeholders}) AND numeric_value IS NOT NULL "
            f"GROUP BY entity_id",
            list(pop_type_ids),
        )
        for eid, val in cur.fetchall():
            if val and val > 0:
                pop_map[eid] = val

    return {
        "ftypes": ftypes,
        "coords": coords,
        "instances": instances,
        "pinyin_to_id": pinyin_to_id,
        "population": pop_map,
    }


def parse_year(date_str) -> int | None:
    """Extract year from a date string (e.g. '960', '1127-01-01')."""
    if not date_str:
        return None
    try:
        return int(str(date_str)[:4])
    except (ValueError, TypeError):
        return None


def build_document(entity_row, lookups):
    """Build an ES document from an entity row."""
    eid = entity_row["id"]
    pinyin = entity_row["pinyin"]
    chinese = entity_row["chinese"]
    parent_name = entity_row["parent_name"]

    place_id = f"dgsd:{eid}"
    title = pinyin or chinese or str(eid)

    # ── Toponyms ──
    toponyms = []
    seen = set()

    def add_toponym(name, lang, timespans=None):
        if not name:
            return
        tid = f"{name}@{lang}"
        if tid not in seen:
            seen.add(tid)
            entry = {"toponym_id": tid}
            if timespans:
                entry["timespans"] = timespans
            toponyms.append(entry)

    # Entity-level names
    add_toponym(pinyin, "und")
    add_toponym(chinese, "zh-Hant")

    # Historical instance names (may differ from entity name due to name changes)
    hist = lookups["instances"].get(eid, [])
    for inst in hist:
        inst_beg = parse_year(inst["begin_date"])
        inst_end = parse_year(inst["end_date"])
        inst_ts = []
        if inst_beg is not None:
            ts = {"start": {"in": inst_beg}}
            if inst_end is not None:
                ts["end"] = {"in": inst_end}
            inst_ts = [ts]

        add_toponym(inst["pinyin"], "und", inst_ts)
        add_toponym(inst["chinese"], "zh-Hant", inst_ts)

    # ── Timespans (aggregate from all instances) ──
    timespans = []
    for inst in hist:
        beg = parse_year(inst["begin_date"])
        end = parse_year(inst["end_date"])
        if beg is not None:
            ts = {"start": {"in": beg}}
            if end is not None:
                ts["end"] = {"in": end}
            timespans.append(ts)

    # ── Geometry ──
    geometries = []
    coord = lookups["coords"].get(eid)
    if coord:
        x, y = coord
        try:
            x, y = float(x), float(y)
            if -180 <= x <= 180 and -90 <= y <= 90:
                geom = {"type": "Point", "coordinates": [x, y]}
                geometries.append(enrich_geometry(geom, timespans=timespans or None))
        except (ValueError, TypeError):
            pass

    # ── Types (from historical instances, deduplicated) ──
    types = []
    seen_types = set()
    for inst in hist:
        ft_id = inst["feature_type"]
        if ft_id and ft_id not in seen_types:
            seen_types.add(ft_id)
            ft = lookups["ftypes"].get(ft_id)
            if ft:
                types.append(ft)

    # ── Relations ──
    relations = []

    # Parent entity
    if parent_name:
        parent_id = lookups["pinyin_to_id"].get(parent_name)
        if parent_id:
            relations.append({
                "relation_type": "partOf",
                "related_place_id": f"dgsd:{parent_id}",
                "label": parent_name,
            })

    # Temporally-scoped prefecture/circuit containment from instances
    for inst in hist:
        inst_beg = parse_year(inst["begin_date"])
        inst_end = parse_year(inst["end_date"])
        inst_ts = []
        if inst_beg is not None:
            ts = {"start": {"in": inst_beg}}
            if inst_end is not None:
                ts["end"] = {"in": inst_end}
            inst_ts = [ts]

        for rel_id in (inst.get("prefecture"), inst.get("circuit")):
            if rel_id:
                rel = {
                    "relation_type": "partOf",
                    "related_place_id": f"dgsd:{rel_id}",
                    "label": "",
                }
                if inst_ts:
                    rel["timespans"] = inst_ts
                relations.append(rel)

    # ── Country codes (all Song China) ──
    ccodes = ["CN"]

    # ── Population ──
    population = lookups["population"].get(eid)

    doc = {
        "place_id": place_id,
        "title": title,
        "toponyms": toponyms,
        "geometries": geometries,
        "types": types,
        "ccodes": ccodes,
        "relations": relations,
        "links": [],
    }
    if timespans:
        doc["timespans"] = timespans
    if population:
        doc["population"] = population

    return doc


def stage_dgsd():
    """Read DGSD SQLite database and write staged place docs."""
    ensure_database()

    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row

    print("Loading lookup tables...")
    lookups = load_lookups(conn)
    print(f"  {len(lookups['coords']):,} entities with coordinates")
    print(f"  {sum(len(v) for v in lookups['instances'].values()):,} historical instances")
    print(f"  {len(lookups['population']):,} entities with population data")

    cur = conn.cursor()
    cur.execute("SELECT id, pinyin, chinese, english, parent_name FROM entity")

    start_time = time.time()
    staged = 0

    for row in cur:
        doc = build_document(row, lookups)
        write_staged_place_doc(NAMESPACE, doc)
        staged += 1
        if staged % 500 == 0:
            elapsed = time.time() - start_time
            rate = staged / elapsed if elapsed > 0 else 0
            print(f"\r  Staged: {staged:,}  Rate: {rate:.0f} docs/s",
                  end="", flush=True)

    conn.close()
    elapsed = time.time() - start_time

    print(f"\n\n{'=' * 60}")
    print(f"  DGSD STAGING COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Staged:   {staged:,}")
    print(f"  Time:     {elapsed:.0f}s ({elapsed / 60:.1f}m)")
    if elapsed > 0:
        print(f"  Rate:     {staged / elapsed:.0f} docs/s")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage DGSD places")
    args = parser.parse_args()

    print("=" * 60)
    print("DGSD — DIGITAL GAZETTEER OF THE SONG DYNASTY (STAGING)")
    print("=" * 60)
    print(f"Source: {DB_FILE}")
    print()

    stage_dgsd()
    print("COMPLETE")


