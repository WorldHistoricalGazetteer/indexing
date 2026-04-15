"""
Build the DGSD SQLite database from dgsd11.sql (MySQL dump).

The dump is the Digital Gazetteer of the Song Dynasty (DGSD) v1.1,
created by Ruth Mostern and Elijah Meeks at UC Merced.  It catalogues
~3,828 named administrative entities (circuits, prefectures, counties,
towns, markets, stockades) across Song-dynasty China (960–1276 CE),
with ~4,849 historical instances tracking name changes, promotions,
demotions, and transfers over time.

The companion shapefiles (44108_DGSDshapefiles.zip) contain pre-joined
point geometries for prefectures (652 records) and counties (1,938
records) keyed by historical_instance ID — they replicate the SQL
point_location data with additional denormalised attributes.

Usage:
    python -m authorities.dgsd.build_database                # full build
    python -m authorities.dgsd.build_database --summary      # print summary only

The SQL dump is extracted from 44108_dgsd11.zip (bundled in this directory).
The resulting database is saved as authorities/dgsd/dgsd.db
"""

import argparse
import logging
import os
import re
import sqlite3
import time
import zipfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DIR = Path(__file__).resolve().parent
SQL_ZIP = DIR / "44108_dgsd11.zip"
SHP_ZIP = DIR / "44108_DGSDshapefiles.zip"
DB_FILE = DIR / "dgsd.db"


# =========================================================================
# Extract SQL from zip
# =========================================================================

def extract_sql() -> str:
    """Extract dgsd11.sql from the zip and return its content."""
    if not SQL_ZIP.exists():
        logger.error(f"SQL zip not found: {SQL_ZIP}")
        raise FileNotFoundError(SQL_ZIP)

    with zipfile.ZipFile(SQL_ZIP) as zf:
        names = zf.namelist()
        sql_name = [n for n in names if n.endswith('.sql')][0]
        logger.info(f"Extracting {sql_name} from {SQL_ZIP.name}")
        return zf.read(sql_name).decode('utf-8')


# =========================================================================
# MySQL → SQLite value parser
# =========================================================================

def decode_hex_blob(hex_str: str) -> str:
    """Decode a MySQL hex literal (0xABCD...) to a UTF-8 string."""
    try:
        return bytes.fromhex(hex_str).decode('utf-8', errors='replace')
    except Exception:
        return hex_str


def parse_tuples(text: str) -> list:
    """Parse MySQL VALUES text into list of tuples.

    Handles:
    - Single-quoted strings with backslash escapes and '' escapes
    - Integers and decimals (including negative, quoted decimals)
    - NULL values
    - Hex blob literals (0xABCD...)
    - Full-width commas and other UTF-8 in strings
    """
    rows = []
    i = 0
    n = len(text)

    while i < n:
        # Find next opening paren
        while i < n and text[i] != '(':
            i += 1
        if i >= n:
            break
        i += 1  # skip '('

        fields = []
        while i < n and text[i] != ')':
            # Skip whitespace
            while i < n and text[i] in (' ', '\t', '\n', '\r'):
                i += 1
            if i >= n or text[i] == ')':
                break

            if text[i] == "'":
                # Quoted string
                i += 1
                val = []
                while i < n:
                    if text[i] == '\\' and i + 1 < n:
                        nc = text[i + 1]
                        val.append({"'": "'", "\\": "\\", "n": "\n",
                                    "r": "\r", "t": "\t", "0": "\0"}.get(nc, nc))
                        i += 2
                    elif text[i] == "'" and i + 1 < n and text[i + 1] == "'":
                        val.append("'")
                        i += 2
                    elif text[i] == "'":
                        i += 1
                        break
                    else:
                        val.append(text[i])
                        i += 1
                fields.append("".join(val))
            elif text[i:i + 2] == '0x':
                # Hex blob literal
                i += 2
                hex_chars = []
                while i < n and text[i] in '0123456789abcdefABCDEF':
                    hex_chars.append(text[i])
                    i += 1
                fields.append(decode_hex_blob("".join(hex_chars)))
            elif text[i:i + 4].upper() == 'NULL':
                fields.append(None)
                i += 4
            elif text[i] in ('-', '+') or text[i].isdigit():
                val = []
                while i < n and (text[i].isdigit() or text[i] in ('-', '+', '.', 'e', 'E')):
                    val.append(text[i])
                    i += 1
                s = "".join(val)
                try:
                    fields.append(float(s) if '.' in s or 'e' in s.lower() else int(s))
                except ValueError:
                    fields.append(s)
            else:
                i += 1
                continue

            # Skip trailing whitespace and comma
            while i < n and text[i] in (' ', '\t', '\n', '\r'):
                i += 1
            if i < n and text[i] == ',':
                i += 1
            while i < n and text[i] in (' ', '\t', '\n', '\r'):
                i += 1

        if i < n and text[i] == ')':
            i += 1
        if fields:
            rows.append(tuple(fields))

    return rows


# =========================================================================
# Schema creation
# =========================================================================

def create_schema(conn: sqlite3.Connection):
    """Create all tables in the SQLite database."""
    cur = conn.cursor()

    # ── Lookup/definition tables ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS attribute_type (
            id          INTEGER PRIMARY KEY,
            pinyin      TEXT,
            chinese     TEXT,
            english     TEXT,
            notes       TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS change_type (
            id          INTEGER PRIMARY KEY,
            pinyin      TEXT,
            english     TEXT,
            chinese     TEXT,
            notes       TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feature_type (
            id          INTEGER PRIMARY KEY,
            pinyin      TEXT,
            chinese     TEXT,
            english     TEXT,
            status      INTEGER,
            notes       TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rank_type (
            id          INTEGER PRIMARY KEY,
            pinyin      TEXT,
            chinese     TEXT,
            english     TEXT,
            notes       TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS source (
            id          INTEGER PRIMARY KEY,
            pinyin      TEXT,
            chinese     TEXT,
            english     TEXT,
            notes       TEXT
        )
    """)

    # ── Core tables ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS entity (
            id          INTEGER PRIMARY KEY,
            pinyin      TEXT,
            chinese     TEXT,
            english     TEXT,
            parent_name TEXT,
            notes       TEXT,
            parent2     TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ent_pinyin ON entity(pinyin)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ent_chinese ON entity(chinese)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS historical_instance (
            id              INTEGER PRIMARY KEY,
            entity_id       INTEGER NOT NULL,
            pinyin          TEXT,
            chinese         TEXT,
            begin_change_type INTEGER,
            end_change_type INTEGER,
            begin_date      TEXT,
            end_date        TEXT,
            target_id       INTEGER,
            feature_type    INTEGER,
            notes           TEXT,
            prefecture      INTEGER,
            circuit         INTEGER,
            FOREIGN KEY (entity_id) REFERENCES entity(id),
            FOREIGN KEY (begin_change_type) REFERENCES change_type(id),
            FOREIGN KEY (end_change_type) REFERENCES change_type(id),
            FOREIGN KEY (feature_type) REFERENCES feature_type(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_hi_entity ON historical_instance(entity_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_hi_ftype ON historical_instance(feature_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_hi_pref ON historical_instance(prefecture)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_hi_circuit ON historical_instance(circuit)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS attribute (
            id              INTEGER PRIMARY KEY,
            entity_id       INTEGER NOT NULL,
            attribute_type  INTEGER NOT NULL,
            numeric_value   INTEGER,
            rank_type       TEXT,
            text_value      TEXT,
            chinese_value   TEXT,
            begin_date      TEXT,
            end_date        TEXT,
            source_id       TEXT,
            notes           TEXT,
            FOREIGN KEY (entity_id) REFERENCES entity(id),
            FOREIGN KEY (attribute_type) REFERENCES attribute_type(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attr_entity ON attribute(entity_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attr_type ON attribute(attribute_type)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS point_location (
            id          INTEGER PRIMARY KEY,
            entity_id   INTEGER,
            x_coord     REAL,
            y_coord     REAL,
            notes       TEXT,
            source_id   INTEGER,
            priority    INTEGER,
            FOREIGN KEY (entity_id) REFERENCES entity(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pl_entity ON point_location(entity_id)")

    conn.commit()


# =========================================================================
# Table mapping: SQL table name → (sqlite table, expected columns)
# =========================================================================

TABLE_MAP = {
    'attribute':          ('attribute', 11),
    'attribute_type':     ('attribute_type', 5),
    'change_type':        ('change_type', 5),
    'entity':             ('entity', 7),
    'feature_type':       ('feature_type', 6),
    'historical_instance': ('historical_instance', 13),
    'point_location':     ('point_location', 7),
    'rank_type':          ('rank_type', 5),
    'source':             ('source', 5),
}


# =========================================================================
# Parse MySQL dump → SQLite
# =========================================================================

def load_sql_dump(conn: sqlite3.Connection, content: str):
    """Parse the MySQL dump and insert rows into the SQLite database."""
    cur = conn.cursor()

    # Check if already loaded
    cur.execute("SELECT COUNT(*) FROM entity")
    existing = cur.fetchone()[0]
    if existing > 0:
        logger.info(f"entity table already has {existing:,} rows — skipping SQL parse")
        return existing

    logger.info("Parsing MySQL dump...")
    start_time = time.time()

    # Find all INSERT statements
    insert_pattern = re.compile(
        r"INSERT INTO `(\w+)` VALUES\s*(.*?);",
        re.DOTALL
    )

    table_counts = {}
    total_rows = 0
    errors = 0

    for match in insert_pattern.finditer(content):
        table = match.group(1)
        values_text = match.group(2)

        mapping = TABLE_MAP.get(table)
        if mapping is None:
            logger.warning(f"Unknown table '{table}' — skipping")
            continue

        sqlite_table, expected_cols = mapping
        rows = parse_tuples(values_text)

        if not rows:
            continue

        placeholders = ','.join(['?'] * expected_cols)
        sql = f"INSERT OR IGNORE INTO {sqlite_table} VALUES ({placeholders})"

        batch_count = 0
        for row in rows:
            if len(row) != expected_cols:
                # Some rows have fewer columns (trailing NULLs omitted)
                if len(row) < expected_cols:
                    row = row + (None,) * (expected_cols - len(row))
                else:
                    errors += 1
                    if errors <= 10:
                        logger.warning(
                            f"  {table}: row has {len(row)} cols, expected {expected_cols} "
                            f"(first val: {row[0] if row else '?'})"
                        )
                    continue

            try:
                cur.execute(sql, row)
                batch_count += 1
            except Exception as e:
                errors += 1
                if errors <= 10:
                    logger.warning(f"  {table}: insert error: {e}")

        table_counts[sqlite_table] = table_counts.get(sqlite_table, 0) + batch_count
        total_rows += batch_count

    conn.commit()

    elapsed = time.time() - start_time
    logger.info(f"Loaded {total_rows:,} rows in {elapsed:.1f}s ({errors} errors)")
    logger.info("")
    logger.info("Per-table row counts:")
    for table, count in sorted(table_counts.items(), key=lambda x: -x[1]):
        logger.info(f"  {table:25s} {count:>10,}")

    return total_rows


# =========================================================================
# Load shapefiles into SQLite
# =========================================================================

def load_shapefiles(conn: sqlite3.Connection):
    """Load shapefile DBF attributes + geometries into SQLite tables.

    Creates two tables: shp_counties, shp_prefectures.
    Requires dbfread (for attributes) and shapefile (for geometries).
    Falls back to dbfread-only if pyshp not available.
    """
    cur = conn.cursor()

    # Check if already loaded
    try:
        cur.execute("SELECT COUNT(*) FROM shp_counties")
        if cur.fetchone()[0] > 0:
            logger.info("Shapefile tables already loaded — skipping")
            return
    except Exception:
        pass

    if not SHP_ZIP.exists():
        logger.warning(f"Shapefile zip not found: {SHP_ZIP}")
        return

    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="dgsd_shp_")

    with zipfile.ZipFile(SHP_ZIP) as zf:
        zf.extractall(tmpdir)

    try:
        import dbfread
    except ImportError:
        logger.warning("dbfread not installed — skipping shapefile load (pip install dbfread)")
        return

    # Counties
    # DBF files use UTF-8 but some records have truncated multi-byte chars
    dbf_encoding = 'utf-8'
    dbf_errors = 'replace'

    counties_dbf = os.path.join(tmpdir, "DGSD", "Counties", "dgsd_11_counties.dbf")
    if os.path.exists(counties_dbf):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shp_counties (
                hi_id       INTEGER,
                ent_id      INTEGER,
                begin_year  INTEGER,
                end_year    INTEGER,
                type        TEXT,
                pinyin      TEXT,
                chinese     TEXT,
                prefecture  TEXT,
                circuit     TEXT,
                x_coord     REAL,
                y_coord     REAL,
                zhen        INTEGER,
                cantons_ss  INTEGER,
                cantons_yf  INTEGER
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_shpc_hi ON shp_counties(hi_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_shpc_ent ON shp_counties(ent_id)")

        dbf = dbfread.DBF(counties_dbf, encoding=dbf_encoding,
                          char_decode_errors=dbf_errors)
        count = 0
        for rec in dbf:
            cur.execute(
                "INSERT INTO shp_counties VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec['hi_id'], rec['ent_id'], rec['begin_year'], rec['end_year'],
                 rec['type'], rec['Pinyin'], rec['Chinese'],
                 rec['Prefecture'], rec['Circuit'],
                 rec['x_coord'], rec['y_coord'],
                 rec['zhen'], rec['cantons_ss'], rec['cantons_yf'])
            )
            count += 1
        logger.info(f"Loaded {count:,} county shapefile records")

    # Prefectures
    prefs_dbf = os.path.join(tmpdir, "DGSD", "Prefectures", "dgsd_11_prefs.dbf")
    if os.path.exists(prefs_dbf):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shp_prefectures (
                noname      INTEGER,
                hi_id       INTEGER,
                ent_id      INTEGER,
                begin_year  INTEGER,
                end_year    INTEGER,
                type        TEXT,
                pinyin      TEXT,
                chinese     TEXT,
                circuit     TEXT,
                x_coord     REAL,
                y_coord     REAL,
                counties    INTEGER,
                ding_tp     INTEGER,
                ding_yf     INTEGER,
                mil_rank    TEXT,
                civ_rank    TEXT,
                hu_ss       INTEGER,
                hu_tp       INTEGER,
                hu_yf       INTEGER,
                kou_ss      INTEGER,
                kou_tp      INTEGER,
                kou_yf      INTEGER
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_shpp_hi ON shp_prefectures(hi_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_shpp_ent ON shp_prefectures(ent_id)")

        dbf2 = dbfread.DBF(prefs_dbf, encoding=dbf_encoding,
                           char_decode_errors=dbf_errors)
        count = 0
        for rec in dbf2:
            cur.execute(
                "INSERT INTO shp_prefectures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec['NoName'], rec['hi_id'], rec['ent_id'],
                 rec['begin_year'], rec['end_year'],
                 rec['type'], rec['Pinyin'], rec['Chinese'], rec['Circuit'],
                 rec['x_coord'], rec['y_coord'],
                 rec['counties'], rec['ding_tp'], rec['ding_yf'],
                 rec['mil_rank'], rec['civ_rank'],
                 rec['hu_ss'], rec['hu_tp'], rec['hu_yf'],
                 rec['kou_ss'], rec['kou_tp'], rec['kou_yf'])
            )
            count += 1
        logger.info(f"Loaded {count:,} prefecture shapefile records")

    conn.commit()

    # Clean up temp dir
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


# =========================================================================
# Summary
# =========================================================================

def print_summary(conn: sqlite3.Connection):
    """Print a comprehensive summary of the database."""
    cur = conn.cursor()

    print(f"\n{'=' * 70}")
    print(f"  DGSD — Digital Gazetteer of the Song Dynasty  (v1.1)")
    print(f"  Source: dgsd11.sql (MySQL) + shapefiles")
    print(f"{'=' * 70}")

    # Core table counts
    for table in ['entity', 'historical_instance', 'attribute', 'point_location',
                   'feature_type', 'change_type', 'attribute_type', 'rank_type', 'source',
                   'shp_counties', 'shp_prefectures']:
        try:
            cur.execute(f"SELECT COUNT(*) FROM [{table}]")
            count = cur.fetchone()[0]
            print(f"  {table:30s} {count:>10,} rows")
        except Exception:
            print(f"  {table:30s}  (not loaded)")

    # ── Entity analysis ──
    print(f"\n{'─' * 70}")
    print("  Entity analysis")
    print(f"{'─' * 70}")

    cur.execute("SELECT COUNT(*) FROM entity")
    print(f"  Total entities:             {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(*) FROM entity WHERE pinyin IS NOT NULL AND pinyin != ''")
    print(f"  With Pinyin name:           {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(*) FROM entity WHERE chinese IS NOT NULL AND chinese != ''")
    print(f"  With Chinese name:          {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(DISTINCT pinyin) FROM entity WHERE pinyin IS NOT NULL")
    print(f"  Distinct Pinyin names:      {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(DISTINCT chinese) FROM entity WHERE chinese IS NOT NULL")
    print(f"  Distinct Chinese names:     {cur.fetchone()[0]:>10,}")

    # ── Historical instances ──
    print(f"\n{'─' * 70}")
    print("  Historical instance analysis")
    print(f"{'─' * 70}")

    cur.execute("SELECT COUNT(*) FROM historical_instance")
    print(f"  Total instances:            {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(DISTINCT entity_id) FROM historical_instance")
    print(f"  Entities with instances:    {cur.fetchone()[0]:>10,}")

    cur.execute("""
        SELECT CAST(SUBSTR(begin_date, 1, 4) AS INTEGER) AS yr
        FROM historical_instance
        WHERE begin_date IS NOT NULL AND begin_date != ''
        ORDER BY yr ASC LIMIT 1
    """)
    row = cur.fetchone()
    min_yr = row[0] if row else '?'

    cur.execute("""
        SELECT CAST(SUBSTR(end_date, 1, 4) AS INTEGER) AS yr
        FROM historical_instance
        WHERE end_date IS NOT NULL AND end_date != ''
        ORDER BY yr DESC LIMIT 1
    """)
    row = cur.fetchone()
    max_yr = row[0] if row else '?'
    print(f"  Temporal range:             {min_yr} – {max_yr}")

    cur.execute("""
        SELECT AVG(cnt) FROM (
            SELECT COUNT(*) as cnt FROM historical_instance GROUP BY entity_id
        )
    """)
    print(f"  Avg instances per entity:   {cur.fetchone()[0]:>10.1f}")

    # By feature type
    cur.execute("""
        SELECT ft.english, ft.chinese, ft.pinyin, COUNT(*) as cnt
        FROM historical_instance hi
        JOIN feature_type ft ON hi.feature_type = ft.id
        GROUP BY hi.feature_type
        ORDER BY cnt DESC
    """)
    print(f"\n  By feature type:")
    for en, ch, py, cnt in cur.fetchall():
        label = en or py or '?'
        ch_str = f" [{ch}]" if ch else ""
        print(f"    {label:25s}{ch_str:10s} {cnt:>6,}")

    # By change type
    cur.execute("""
        SELECT ct.english, COUNT(*) as cnt
        FROM historical_instance hi
        JOIN change_type ct ON hi.begin_change_type = ct.id
        GROUP BY hi.begin_change_type
        ORDER BY cnt DESC
    """)
    print(f"\n  By begin change type:")
    for en, cnt in cur.fetchall():
        print(f"    {en:25s} {cnt:>6,}")

    # ── Point locations ──
    print(f"\n{'─' * 70}")
    print("  Point locations")
    print(f"{'─' * 70}")

    cur.execute("SELECT COUNT(*) FROM point_location")
    print(f"  Total point locations:      {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(DISTINCT entity_id) FROM point_location")
    print(f"  Entities with coordinates:  {cur.fetchone()[0]:>10,}")

    cur.execute("""
        SELECT MIN(x_coord), MAX(x_coord), MIN(y_coord), MAX(y_coord)
        FROM point_location
        WHERE x_coord IS NOT NULL
    """)
    row = cur.fetchone()
    if row and row[0] is not None:
        print(f"  Longitude range:            {row[0]:.3f} – {row[1]:.3f}")
        print(f"  Latitude range:             {row[2]:.3f} – {row[3]:.3f}")

    # Source breakdown
    cur.execute("""
        SELECT s.english, s.pinyin, COUNT(*) as cnt
        FROM point_location pl
        JOIN source s ON pl.source_id = s.id
        GROUP BY pl.source_id
        ORDER BY cnt DESC
    """)
    print(f"\n  By source:")
    for en, py, cnt in cur.fetchall():
        label = en or py or '?'
        print(f"    {label:25s} {cnt:>6,}")

    # ── Attributes ──
    print(f"\n{'─' * 70}")
    print("  Attributes")
    print(f"{'─' * 70}")

    cur.execute("SELECT COUNT(*) FROM attribute")
    print(f"  Total attributes:           {cur.fetchone()[0]:>10,}")

    cur.execute("""
        SELECT at.english, COUNT(*) as cnt
        FROM attribute a
        JOIN attribute_type at ON a.attribute_type = at.id
        GROUP BY a.attribute_type
        ORDER BY cnt DESC
        LIMIT 15
    """)
    print(f"\n  Top attribute types:")
    for en, cnt in cur.fetchall():
        print(f"    {en:35s} {cnt:>6,}")

    # ── Shapefile comparison ──
    try:
        cur.execute("SELECT COUNT(*) FROM shp_counties")
        shp_c = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM shp_prefectures")
        shp_p = cur.fetchone()[0]

        print(f"\n{'─' * 70}")
        print("  Shapefile vs SQL comparison")
        print(f"{'─' * 70}")

        # Check how many shapefile hi_ids match SQL historical_instance IDs
        cur.execute("""
            SELECT COUNT(*) FROM shp_counties sc
            WHERE sc.hi_id IN (SELECT id FROM historical_instance)
        """)
        match_c = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM shp_prefectures sp
            WHERE sp.hi_id IN (SELECT id FROM historical_instance)
        """)
        match_p = cur.fetchone()[0]

        print(f"  Shapefile counties:         {shp_c:>10,}  ({match_c:,} match SQL hi_id)")
        print(f"  Shapefile prefectures:      {shp_p:>10,}  ({match_p:,} match SQL hi_id)")

        # Compare coordinate coverage
        cur.execute("""
            SELECT COUNT(DISTINCT entity_id)
            FROM point_location
            WHERE x_coord IS NOT NULL
        """)
        sql_with_coords = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(DISTINCT ent_id)
            FROM (
                SELECT ent_id FROM shp_counties WHERE x_coord IS NOT NULL
                UNION
                SELECT ent_id FROM shp_prefectures WHERE x_coord IS NOT NULL
            )
        """)
        shp_with_coords = cur.fetchone()[0]

        print(f"  SQL entities with coords:   {sql_with_coords:>10,}")
        print(f"  SHP entities with coords:   {shp_with_coords:>10,}")

        # Check if shapefiles have entities NOT in point_location
        cur.execute("""
            SELECT COUNT(DISTINCT ent_id) FROM (
                SELECT ent_id FROM shp_counties
                UNION
                SELECT ent_id FROM shp_prefectures
            ) sub
            WHERE ent_id NOT IN (SELECT entity_id FROM point_location WHERE entity_id IS NOT NULL)
        """)
        extra = cur.fetchone()[0]
        print(f"  SHP entities not in SQL pl: {extra:>10,}")

    except Exception:
        pass

    # ── Sample records ──
    print(f"\n{'─' * 70}")
    print("  Sample entities (first 5)")
    print(f"{'─' * 70}")

    cur.execute("""
        SELECT e.id, e.pinyin, e.chinese,
               (SELECT COUNT(*) FROM historical_instance hi WHERE hi.entity_id = e.id) as hi_cnt,
               (SELECT COUNT(*) FROM point_location pl WHERE pl.entity_id = e.id) as pl_cnt,
               (SELECT COUNT(*) FROM attribute a WHERE a.entity_id = e.id) as attr_cnt
        FROM entity e
        LIMIT 5
    """)
    for row in cur.fetchall():
        eid, py, ch, hi_cnt, pl_cnt, attr_cnt = row
        print(f"\n  id={eid}, {py} [{ch}]")
        print(f"    Historical instances: {hi_cnt}, Locations: {pl_cnt}, Attributes: {attr_cnt}")

    # ── Database size ──
    print(f"\n{'─' * 70}")
    db_size = DB_FILE.stat().st_size / (1024 * 1024)
    print(f"  Database: {DB_FILE}")
    print(f"  Size:     {db_size:.1f} MB")
    print()


# =========================================================================
# Main
# =========================================================================

def main():
    parser = argparse.ArgumentParser(description="Build DGSD SQLite database")
    parser.add_argument("--summary", action="store_true", help="Print summary only")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_FILE))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    create_schema(conn)

    if args.summary:
        print_summary(conn)
        conn.close()
        return

    content = extract_sql()
    load_sql_dump(conn, content)
    load_shapefiles(conn)
    print_summary(conn)
    conn.close()


if __name__ == "__main__":
    main()









