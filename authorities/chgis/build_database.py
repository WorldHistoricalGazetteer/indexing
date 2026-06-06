"""
Build the CHGIS/TGAZ SQLite database from 02-tgaz-dev-2018.sql (MySQL dump).

The dump is from the Temporal Gazetteer (TGAZ) of Administrative Entities,
the successor to the China Historical GIS (CHGIS). It contains ~82K
placename records spanning 220 BCE – 1911 CE with rich temporal, spatial,
hierarchical, and multilingual toponym metadata.

Usage:
    python -m authorities.chgis.build_database                # full build (SQL only)
    python -m authorities.chgis.build_database --fetch-wikidata  # + Wikidata P4711 links
    python -m authorities.chgis.build_database --summary      # print summary only

The SQL dump is not committed to git (too large). If absent, it is fetched
automatically from the fccs-dci/containerized_tgaz GitHub repository.

The resulting database is saved as authorities/chgis/tgaz.db
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DIR = Path(__file__).resolve().parent
SQL_FILE = DIR / "02-tgaz-dev-2018.sql"
DB_FILE = DIR / "tgaz.db"

# GitHub URL for the SQL dump. **Use ``github.com/.../raw/main/...``
# (NOT ``raw.githubusercontent.com``)** — the upstream file is tracked
# via Git LFS, and ``raw.githubusercontent.com`` serves the 134-byte LFS
# pointer instead of the actual content. The ``github.com/.../raw/``
# URL follows the LFS redirect to ``media.githubusercontent.com`` and
# returns the real ~123 MB SQL dump.
SQL_URL = (
    "https://github.com/fccs-dci/containerized_tgaz"
    "/raw/main/mysql-init/02-tgaz-dev-2018.sql"
)

# Detect the case where someone has committed an LFS pointer file in
# place of the real dump (the file we ship may already be a pointer if
# the repo was cloned without ``git lfs pull``). LFS pointer files
# begin with this exact line.
_LFS_POINTER_MARKER = b"version https://git-lfs.github.com/spec/v1"


def _is_lfs_pointer(path: Path) -> bool:
    if not path.exists() or path.stat().st_size > 1024:
        return False
    try:
        with path.open("rb") as fh:
            return fh.read(len(_LFS_POINTER_MARKER)) == _LFS_POINTER_MARKER
    except OSError:
        return False


def fetch_sql_dump():
    """Download the SQL dump from GitHub if missing or an LFS pointer."""
    if SQL_FILE.exists():
        if _is_lfs_pointer(SQL_FILE):
            logger.warning(
                f"{SQL_FILE.name} is a Git LFS pointer "
                f"({SQL_FILE.stat().st_size} bytes) — re-downloading"
            )
            SQL_FILE.unlink()
        else:
            logger.info(
                f"SQL dump already present ({SQL_FILE.stat().st_size / 1024 / 1024:.0f} MB)"
            )
            return

    logger.info(f"Downloading SQL dump from GitHub (LFS-redirect URL)...")
    logger.info(f"  {SQL_URL}")

    try:
        req = urllib.request.Request(SQL_URL, headers={
            "User-Agent": "WHG-Indexing/1.0 (https://whgazetteer.org; research)"
        })
        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 1024 * 1024  # 1 MB chunks

            with open(SQL_FILE, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 / total
                        print(f"\r  {downloaded / 1024 / 1024:.0f} / {total / 1024 / 1024:.0f} MB ({pct:.0f}%)",
                              end="", flush=True)
                    else:
                        print(f"\r  {downloaded / 1024 / 1024:.0f} MB downloaded",
                              end="", flush=True)

        print()
        logger.info(f"Saved to {SQL_FILE} ({SQL_FILE.stat().st_size / 1024 / 1024:.0f} MB)")

    except Exception as e:
        # Clean up partial download
        if SQL_FILE.exists():
            SQL_FILE.unlink()
        logger.error(f"Failed to download SQL dump: {e}")
        sys.exit(1)


# =========================================================================
# Phase 2: Fetch Wikidata links via P4711 (CHGIS ID) SPARQL query
# =========================================================================

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# Fetch all Wikidata items with P4711, plus optional GeoNames (P1566).
# Single query — the P4711 population is small (~5,700 items).
SPARQL_QUERY = """\
SELECT ?item ?chgis_id ?geonames ?itemLabel WHERE {
  ?item wdt:P4711 ?chgis_id .
  OPTIONAL { ?item wdt:P1566 ?geonames . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,zh" . }
}
"""


def fetch_wikidata_links(conn: sqlite3.Connection):
    """Fetch CHGIS → Wikidata Q-ID mappings via the Wikidata SPARQL endpoint.

    Uses property P4711 ("CHGIS ID") to find Wikidata items that reference
    CHGIS place records.  Also captures GeoNames IDs (P1566) when present.

    Results are stored in the wikidata_links table.  The function is
    idempotent — existing rows are preserved (INSERT OR IGNORE).
    """
    cur = conn.cursor()

    # Check existing count
    cur.execute("SELECT COUNT(*) FROM wikidata_links")
    existing = cur.fetchone()[0]
    if existing > 0:
        logger.info(f"wikidata_links already has {existing:,} rows — skipping SPARQL fetch")
        logger.info("  (delete the table or the .db file to re-fetch)")
        return

    logger.info("Querying Wikidata SPARQL for P4711 (CHGIS ID) links...")

    try:
        params = urllib.parse.urlencode({
            "query": SPARQL_QUERY,
            "format": "json",
        })
        req = urllib.request.Request(
            f"{WIKIDATA_SPARQL}?{params}",
            headers={
                "User-Agent": "WHG-Indexing/1.0 (https://whgazetteer.org; research)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))

    except Exception as e:
        logger.error(f"SPARQL query failed: {e}")
        logger.error("  Wikidata links not fetched — run again with --fetch-wikidata to retry")
        return

    bindings = data.get("results", {}).get("bindings", [])
    if not bindings:
        logger.warning("SPARQL returned no results")
        return

    inserted = 0
    matched = 0
    with_geonames = 0

    for row in bindings:
        chgis_id = row.get("chgis_id", {}).get("value", "")
        qid_uri = row.get("item", {}).get("value", "")
        geonames = row.get("geonames", {}).get("value")
        label = row.get("itemLabel", {}).get("value")

        if not chgis_id or not qid_uri:
            continue

        qid = qid_uri.rsplit("/", 1)[-1]  # http://www.wikidata.org/entity/Q123 → Q123

        try:
            cur.execute(
                "INSERT OR IGNORE INTO wikidata_links (data_src_ref, qid, geonames_id, label) "
                "VALUES (?, ?, ?, ?)",
                (chgis_id, qid, geonames, label),
            )
            if cur.rowcount > 0:
                inserted += 1
                if geonames:
                    with_geonames += 1
        except Exception as e:
            logger.warning(f"  Insert error for chgis_id={chgis_id}: {e}")

    conn.commit()

    # Count how many of those link to actual placename records in our DB
    cur.execute("""
        SELECT COUNT(DISTINCT wl.data_src_ref)
        FROM wikidata_links wl
        JOIN placename pn ON pn.data_src_ref = wl.data_src_ref
                         AND pn.data_src = 'CHGIS'
    """)
    matched = cur.fetchone()[0]

    logger.info(f"Wikidata links: {inserted:,} inserted, {matched:,} match CHGIS placenames, "
                f"{with_geonames:,} also have GeoNames IDs")


# =========================================================================
# MySQL value parser (handles escapes, NULLs, nested quotes in long lines)
# =========================================================================

def parse_tuples(text: str) -> list:
    """Parse MySQL VALUES text into list of tuples.

    Handles:
    - Single-quoted strings with backslash escapes and '' escapes
    - Integers and floats (including negative)
    - NULL values
    - Nested parentheses in strings
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
            elif text[i:i + 4] == 'NULL':
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
                # Unknown token — skip
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

    # ── Reference tables ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS data_src (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            org         TEXT,
            uri         TEXT,
            note        TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS citation_ref (
            ref_handle  TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            uri         TEXT,
            note        TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS drule (
            id          INTEGER PRIMARY KEY,
            name        TEXT,
            rule        TEXT,
            ld_vocab    TEXT,
            ld_uri      TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS script (
            id              INTEGER PRIMARY KEY,
            name            TEXT NOT NULL,
            lang            TEXT NOT NULL,
            dialect         TEXT,
            default_per_lang INTEGER NOT NULL DEFAULT 0,
            note            TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trsys (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            lang        TEXT NOT NULL,
            lang_subtype TEXT,
            note        TEXT
        )
    """)

    # ── Feature types ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ftype (
            id          INTEGER PRIMARY KEY,
            name_vn     TEXT,
            name_alt    TEXT,
            name_tr     TEXT,
            name_en     TEXT,
            period      TEXT,
            adl_class   TEXT,
            cit_src     TEXT,
            citation    TEXT,
            note        TEXT,
            ld_uri      TEXT,
            added_on    TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ftype_xx (
            order_id    INTEGER PRIMARY KEY,
            lang        TEXT,
            name_py     TEXT,
            name_ch     TEXT,
            id          TEXT,
            name_en     TEXT,
            name_alt    TEXT,
            adl_class   TEXT,
            period      TEXT,
            cit_src     TEXT,
            note        TEXT,
            status      TEXT,
            ts_added    TEXT
        )
    """)

    # ── Core placename table ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS placename (
            id                  INTEGER PRIMARY KEY,
            sys_id              TEXT NOT NULL,
            ftype_id            INTEGER NOT NULL,
            data_src            TEXT NOT NULL,
            data_src_ref        TEXT,
            snote_id            INTEGER,
            alt_of_id           INTEGER,
            lev_rank            TEXT,
            beg_yr              INTEGER,
            beg_rule_id         INTEGER,
            end_yr              INTEGER,
            end_rule_id         INTEGER,
            obj_type            TEXT,
            xy_type             TEXT,
            x_coord             TEXT,
            y_coord             TEXT,
            geo_src             TEXT,
            added_on            TEXT,
            default_parent_id   INTEGER,
            parent_status       TEXT DEFAULT 'earliest'
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pn_sys_id ON placename(sys_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pn_ftype ON placename(ftype_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pn_data_src ON placename(data_src)")

    # ── Spelling (toponym forms) ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS spelling (
            id              INTEGER PRIMARY KEY,
            placename_id    INTEGER NOT NULL,
            script_id       INTEGER NOT NULL,
            written_form    TEXT NOT NULL,
            exonym_lang     TEXT,
            trsys_id        TEXT NOT NULL,
            default_per_type INTEGER NOT NULL DEFAULT 0,
            attested_by     TEXT,
            note            TEXT,
            FOREIGN KEY (placename_id) REFERENCES placename(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sp_pn ON spelling(placename_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sp_form ON spelling(written_form)")

    # ── Present location ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS present_loc (
            id              INTEGER PRIMARY KEY,
            placename_id    INTEGER NOT NULL,
            type            TEXT NOT NULL,
            country_code    TEXT NOT NULL,
            text_value      TEXT NOT NULL,
            source          TEXT,
            attestation     TEXT,
            source_uri      TEXT,
            FOREIGN KEY (placename_id) REFERENCES placename(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ploc_pn ON present_loc(placename_id)")

    # ── Hierarchical relations (part_of) ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS part_of (
            id          INTEGER PRIMARY KEY,
            child_id    INTEGER NOT NULL,
            parent_id   INTEGER NOT NULL,
            begin_year  INTEGER,
            end_year    INTEGER,
            FOREIGN KEY (child_id) REFERENCES placename(id),
            FOREIGN KEY (parent_id) REFERENCES placename(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_po_child ON part_of(child_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_po_parent ON part_of(parent_id)")

    # ── Temporal succession (prec_by = "preceded by") ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS prec_by (
            id              INTEGER PRIMARY KEY,
            placename_id    INTEGER NOT NULL,
            prec_id         INTEGER NOT NULL,
            FOREIGN KEY (placename_id) REFERENCES placename(id),
            FOREIGN KEY (prec_id) REFERENCES placename(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pb_pn ON prec_by(placename_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pb_prec ON prec_by(prec_id)")

    # ── Administrative seats ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_seat (
            id              INTEGER PRIMARY KEY,
            placename_id    INTEGER NOT NULL,
            seat_id         INTEGER NOT NULL,
            begin_date      TEXT,
            end_date        TEXT,
            note            TEXT,
            FOREIGN KEY (placename_id) REFERENCES placename(id),
            FOREIGN KEY (seat_id) REFERENCES placename(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_as_pn ON admin_seat(placename_id)")

    # ── Scholarly notes ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS snote (
            id              INTEGER PRIMARY KEY,
            src_note_ref    TEXT,
            source          TEXT,
            compiler        TEXT,
            lang            TEXT,
            topic           TEXT,
            uri             TEXT,
            full_text       TEXT,
            added_on        TEXT
        )
    """)

    # ── External links ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS link (
            id              INTEGER PRIMARY KEY,
            placename_id    INTEGER NOT NULL,
            type            TEXT NOT NULL,
            source          TEXT NOT NULL,
            uri             TEXT NOT NULL,
            lang            TEXT,
            FOREIGN KEY (placename_id) REFERENCES placename(id)
        )
    """)

    # ── Wikidata cross-links (via P4711 SPARQL query) ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wikidata_links (
            data_src_ref    TEXT NOT NULL,
            qid             TEXT NOT NULL,
            geonames_id     TEXT,
            label           TEXT,
            PRIMARY KEY (data_src_ref, qid)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wd_qid ON wikidata_links(qid)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wd_gn ON wikidata_links(geonames_id)")

    # ── WKT geometry definitions ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wkt_definition (
            id              INTEGER PRIMARY KEY,
            placename_id    INTEGER NOT NULL,
            object_type     TEXT NOT NULL,
            object_text_value TEXT,
            FOREIGN KEY (placename_id) REFERENCES placename(id)
        )
    """)

    # ── Spatial system references ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS spatial_system_ref (
            id              INTEGER PRIMARY KEY,
            placename_id    INTEGER NOT NULL,
            system_name     TEXT NOT NULL,
            level           INTEGER,
            location_uri    TEXT,
            location_id     TEXT,
            FOREIGN KEY (placename_id) REFERENCES placename(id)
        )
    """)

    # ── Temporal annotations ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS temporal_annotation (
            id              INTEGER PRIMARY KEY,
            placename_id    INTEGER NOT NULL,
            temporal_type   TEXT,
            calendar_standard TEXT,
            rule_id         INTEGER,
            attested_by     TEXT,
            equivalent      TEXT,
            lang            TEXT,
            note            TEXT,
            FOREIGN KEY (placename_id) REFERENCES placename(id)
        )
    """)

    # ── CHGIS version ID mappings ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS v5_id (
            id      INTEGER PRIMARY KEY,
            flag    TEXT,
            sys_id  TEXT NOT NULL UNIQUE
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS v6_id (
            id      INTEGER PRIMARY KEY,
            flag    TEXT,
            sys_id  TEXT NOT NULL UNIQUE
        )
    """)

    # ── Legacy import tables (kept for completeness) ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS alt_name3 (
            order_id    INTEGER PRIMARY KEY,
            name_py     TEXT,
            name_utf    TEXT,
            name_utf_alt TEXT,
            type_py     TEXT,
            type_utf    TEXT,
            type_id     TEXT,
            type_eng    TEXT,
            beg_yr      INTEGER,
            end_yr      INTEGER,
            pgn_id      TEXT,
            pt_id       TEXT,
            line_id     TEXT,
            data_src    TEXT,
            parent_utf  TEXT,
            parent_py   TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS main_xx (
            sys_id      TEXT NOT NULL,
            nm_py       TEXT,
            nm_simp     TEXT,
            name_trad   TEXT,
            orig_ID     TEXT,
            beg_yr      INTEGER,
            end_yr      INTEGER,
            xy_type     TEXT,
            x_coord     TEXT,
            y_coord     TEXT,
            pres_loc    TEXT,
            type_py     TEXT,
            type_utf    TEXT,
            type_id     TEXT,
            type_eng    TEXT,
            lev_rank    TEXT,
            note_id     TEXT,
            nt_auto     TEXT,
            obj_type    TEXT,
            data_src    TEXT,
            auto_id     INTEGER PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gis_xx (
            order_id    INTEGER PRIMARY KEY,
            name_py     TEXT,
            name_utf    TEXT,
            name_utf_alt TEXT,
            sys_id      TEXT NOT NULL,
            xy_type     TEXT,
            x_coord     TEXT,
            y_coord     TEXT,
            pres_loc    TEXT,
            type_py     TEXT,
            type_utf    TEXT,
            lev_rank    TEXT,
            beg_yr      INTEGER,
            beg_rule    TEXT,
            beg_chg_type TEXT,
            end_yr      INTEGER,
            end_rule    TEXT,
            end_chg_type TEXT,
            note_id     TEXT,
            obj_type    TEXT,
            geo_src     TEXT,
            compiler    TEXT,
            geocompiler TEXT,
            checker     TEXT,
            filename    TEXT,
            src         TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS snote_xx (
            nts_comp    TEXT,
            nts_noteid  TEXT,
            nts_nmpy    TEXT,
            nts_nmch    TEXT,
            nts_nmft    TEXT,
            nts_fullnote TEXT,
            nts_autoid  INTEGER PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS precby_xx (
            pby_id          TEXT NOT NULL,
            pby_nmpy        TEXT,
            pby_nmch        TEXT,
            pby_nmft        TEXT,
            pby_obj_type    TEXT,
            pby_prev_id     TEXT,
            pby_prev_nmpy   TEXT,
            pby_prev_nmch   TEXT,
            pby_prev_nmft   TEXT,
            pby_uniq_id     INTEGER PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS partof_xx (
            order_id    INTEGER PRIMARY KEY,
            child_id    TEXT,
            parent_id   TEXT,
            beg_yr      TEXT,
            end_yr      TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS f2 (
            name    TEXT,
            sys_id  TEXT NOT NULL,
            x_coord TEXT,
            y_coord TEXT,
            auto_id INTEGER PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ck1 (
            id      INTEGER PRIMARY KEY,
            name_id TEXT,
            y       TEXT,
            x       TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tbt_rev (
            id      INTEGER PRIMARY KEY,
            name_id TEXT,
            y       TEXT,
            x       TEXT
        )
    """)

    # ── Materialized search view (mv_pn_srch) ──

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mv_pn_srch (
            id              INTEGER PRIMARY KEY,
            sys_id          TEXT NOT NULL,
            data_src        TEXT NOT NULL,
            name            TEXT,
            transcription   TEXT,
            beg_yr          INTEGER,
            end_yr          INTEGER,
            obj_type        TEXT,
            x_coord         TEXT,
            y_coord         TEXT,
            ftype_vn        TEXT,
            ftype_tr        TEXT,
            parent_id       INTEGER,
            parent_sys_id   TEXT,
            parent_vn       TEXT,
            parent_tr       TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mv_sys_id ON mv_pn_srch(sys_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mv_name ON mv_pn_srch(name)")

    conn.commit()


# =========================================================================
# Parse MySQL dump → SQLite
# =========================================================================

# Table → expected column count (from CREATE TABLE definitions)
TABLE_COLUMNS = {
    'admin_seat': 6,
    'alt_name3': 16,
    'citation_ref': 4,
    'ck1': 4,
    'data_src': 5,
    'drule': 5,
    'f2': 5,
    'ftype': 12,
    'ftype_xx': 13,
    'gis_xx': 26,
    'link': 6,
    'main_xx': 21,
    'mv_pn_srch': 16,
    'mv_pn_srch_new_test': 15,  # skip — test table
    'mv_pn_srch_old': 16,       # skip — old version
    'part_of': 5,
    'partof_xx': 5,
    'placename': 20,
    'prec_by': 3,
    'precby_xx': 10,
    'present_loc': 8,
    'script': 6,
    'snote': 9,
    'snote_xx': 7,
    'spatial_system_ref': 6,
    'spelling': 9,
    'tbt_rev': 4,
    'temporal_annotation': 9,
    'trsys': 5,
    'v5_id': 3,
    'v6_id': 3,
    'wkt_definition': 4,
}

# Tables to skip (test/old duplicates)
SKIP_TABLES = {'mv_pn_srch_new_test', 'mv_pn_srch_old', 'geom'}


def load_sql_dump(conn: sqlite3.Connection):
    """Parse the MySQL dump and insert rows into the SQLite database."""
    cur = conn.cursor()

    # Check if already loaded
    cur.execute("SELECT COUNT(*) FROM placename")
    existing = cur.fetchone()[0]
    if existing > 0:
        logger.info(f"placename table already has {existing:,} rows — skipping SQL parse")
        return existing

    logger.info(f"Parsing {SQL_FILE.name} ({SQL_FILE.stat().st_size / 1024 / 1024:.0f} MB)...")
    start_time = time.time()

    with open(SQL_FILE, 'r', encoding='utf-8') as f:
        content = f.read()

    # Find all INSERT statements. Terminate on the mysqldump statement boundary
    # `;\n` (optionally trailing spaces/CR), NOT the first bare `;` — a `;` inside
    # a quoted value (e.g. an ftype citation/note) otherwise truncates the INSERT
    # mid-statement and silently drops every later row. This lost 249 of 1147
    # `ftype` rows, so only 30% of CHGIS places resolved a feature type.
    insert_pattern = re.compile(
        r"INSERT INTO `(\w+)` VALUES\s*(.*?);[ \t\r]*\n",
        re.DOTALL
    )

    table_counts = {}
    total_rows = 0
    errors = 0

    for match in insert_pattern.finditer(content):
        table = match.group(1)
        values_text = match.group(2)

        if table in SKIP_TABLES:
            continue

        expected_cols = TABLE_COLUMNS.get(table)
        if expected_cols is None:
            logger.warning(f"Unknown table '{table}' — skipping")
            continue

        rows = parse_tuples(values_text)

        if not rows:
            continue

        placeholders = ','.join(['?'] * expected_cols)
        sql = f"INSERT OR IGNORE INTO {table} VALUES ({placeholders})"

        batch_count = 0
        for row in rows:
            if len(row) != expected_cols:
                # Try to handle mv_pn_srch_old having an extra counter_id column
                if len(row) == expected_cols + 1:
                    row = row[:expected_cols]
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

        table_counts[table] = table_counts.get(table, 0) + batch_count
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
# Summary
# =========================================================================

def print_summary(conn: sqlite3.Connection):
    """Print a comprehensive summary of the database."""
    cur = conn.cursor()

    print(f"\n{'=' * 70}")
    print(f"  CHGIS / TGAZ Database Summary")
    print(f"  Source: {SQL_FILE.name}")
    print(f"{'=' * 70}")

    # Core table counts
    for table in ['placename', 'spelling', 'ftype', 'present_loc', 'part_of',
                   'prec_by', 'admin_seat', 'snote', 'link', 'wkt_definition',
                   'spatial_system_ref', 'temporal_annotation',
                   'script', 'trsys', 'data_src', 'drule', 'citation_ref',
                   'alt_name3', 'main_xx', 'gis_xx', 'mv_pn_srch',
                   'v5_id', 'v6_id']:
        try:
            cur.execute(f"SELECT COUNT(*) FROM [{table}]")
            count = cur.fetchone()[0]
            print(f"  {table:30s} {count:>10,} rows")
        except Exception:
            print(f"  {table:30s}  (not loaded)")

    # ── Placename details ──
    print(f"\n{'─' * 70}")
    print("  Placename analysis")
    print(f"{'─' * 70}")

    cur.execute("SELECT COUNT(DISTINCT sys_id) FROM placename")
    print(f"  Distinct sys_id values:     {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(*) FROM placename WHERE x_coord IS NOT NULL AND x_coord != ''")
    print(f"  With coordinates:           {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(*) FROM placename WHERE beg_yr IS NOT NULL")
    print(f"  With begin year:            {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(*) FROM placename WHERE end_yr IS NOT NULL")
    print(f"  With end year:              {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT MIN(beg_yr), MAX(end_yr) FROM placename WHERE beg_yr IS NOT NULL")
    row = cur.fetchone()
    if row[0] is not None:
        print(f"  Temporal range:             {row[0]:>10} – {row[1]}")

    cur.execute("""
        SELECT data_src, COUNT(*) as cnt
        FROM placename
        GROUP BY data_src
        ORDER BY cnt DESC
    """)
    print(f"\n  By data source:")
    for src, cnt in cur.fetchall():
        print(f"    {src:25s} {cnt:>8,}")

    cur.execute("""
        SELECT obj_type, COUNT(*) as cnt
        FROM placename
        GROUP BY obj_type
        ORDER BY cnt DESC
    """)
    print(f"\n  By geometry type:")
    for obj_type, cnt in cur.fetchall():
        print(f"    {str(obj_type):25s} {cnt:>8,}")

    # ── Spelling details ──
    print(f"\n{'─' * 70}")
    print("  Spelling (toponym) analysis")
    print(f"{'─' * 70}")

    cur.execute("SELECT COUNT(DISTINCT placename_id) FROM spelling")
    print(f"  Placenames with spellings:  {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(DISTINCT written_form) FROM spelling")
    print(f"  Distinct written forms:     {cur.fetchone()[0]:>10,}")

    cur.execute("""
        SELECT s.name AS script_name, sc.lang, COUNT(*) as cnt
        FROM spelling sp
        JOIN script s ON sp.script_id = s.id
        LEFT JOIN script sc ON sp.script_id = sc.id
        GROUP BY sp.script_id
        ORDER BY cnt DESC
    """)
    print(f"\n  By writing system:")
    for name, lang, cnt in cur.fetchall():
        print(f"    {name:25s} ({lang:5s}) {cnt:>8,}")

    cur.execute("""
        SELECT t.name AS trsys_name, COUNT(*) as cnt
        FROM spelling sp
        JOIN trsys t ON sp.trsys_id = t.id
        GROUP BY sp.trsys_id
        ORDER BY cnt DESC
    """)
    print(f"\n  By transliteration system:")
    for name, cnt in cur.fetchall():
        print(f"    {name:25s} {cnt:>8,}")

    # ── Feature types ──
    print(f"\n{'─' * 70}")
    print("  Feature type analysis")
    print(f"{'─' * 70}")

    cur.execute("""
        SELECT ft.adl_class, COUNT(*) as cnt
        FROM ftype ft
        WHERE ft.adl_class IS NOT NULL AND ft.adl_class != ''
        GROUP BY ft.adl_class
        ORDER BY cnt DESC
    """)
    print(f"  ADL classes:")
    for cls, cnt in cur.fetchall():
        print(f"    {cls:35s} {cnt:>6,}")

    cur.execute("""
        SELECT ft.name_en, ft.name_vn, ft.name_tr, COUNT(pn.id) as usage_cnt
        FROM ftype ft
        LEFT JOIN placename pn ON pn.ftype_id = ft.id
        GROUP BY ft.id
        ORDER BY usage_cnt DESC
        LIMIT 20
    """)
    print(f"\n  Top 20 feature types by usage:")
    for en, vn, tr, cnt in cur.fetchall():
        label = en or tr or vn or '(unnamed)'
        vn_str = f" [{vn}]" if vn and vn != label else ""
        print(f"    {label:35s}{vn_str:15s} {cnt:>6,} places")

    # ── Hierarchical relations ──
    print(f"\n{'─' * 70}")
    print("  Hierarchical & temporal relations")
    print(f"{'─' * 70}")

    cur.execute("SELECT COUNT(*) FROM part_of")
    print(f"  part_of relations:          {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(DISTINCT child_id) FROM part_of")
    print(f"  Children with parents:      {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(DISTINCT parent_id) FROM part_of")
    print(f"  Distinct parents:           {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(*) FROM prec_by")
    print(f"  prec_by relations:          {cur.fetchone()[0]:>10,}")

    cur.execute("SELECT COUNT(*) FROM admin_seat")
    print(f"  admin_seat relations:       {cur.fetchone()[0]:>10,}")

    # ── Present locations ──
    print(f"\n{'─' * 70}")
    print("  Present-day locations")
    print(f"{'─' * 70}")

    cur.execute("SELECT COUNT(DISTINCT placename_id) FROM present_loc")
    print(f"  Placenames with present loc:{cur.fetchone()[0]:>10,}")

    cur.execute("""
        SELECT country_code, COUNT(*) as cnt
        FROM present_loc
        GROUP BY country_code
        ORDER BY cnt DESC
        LIMIT 10
    """)
    print(f"\n  By country code:")
    for cc, cnt in cur.fetchall():
        print(f"    {cc:10s} {cnt:>8,}")

    # ── Wikidata links ──
    try:
        cur.execute("SELECT COUNT(*) FROM wikidata_links")
        wd_total = cur.fetchone()[0]
        if wd_total > 0:
            print(f"\n{'─' * 70}")
            print("  Wikidata links (via P4711)")
            print(f"{'─' * 70}")
            print(f"  Total Wikidata Q-IDs:       {wd_total:>10,}")

            cur.execute("""
                SELECT COUNT(DISTINCT wl.data_src_ref)
                FROM wikidata_links wl
                JOIN placename pn ON pn.data_src_ref = wl.data_src_ref
                                 AND pn.data_src = 'CHGIS'
            """)
            print(f"  Matching CHGIS placenames:   {cur.fetchone()[0]:>10,}")

            cur.execute("SELECT COUNT(*) FROM wikidata_links WHERE geonames_id IS NOT NULL")
            print(f"  With GeoNames ID:           {cur.fetchone()[0]:>10,}")
    except Exception:
        pass  # table may not exist in older databases

    # ── Sample records ──
    print(f"\n{'─' * 70}")
    print("  Sample placename records (first 5)")
    print(f"{'─' * 70}")

    cur.execute("""
        SELECT pn.id, pn.sys_id, pn.beg_yr, pn.end_yr,
               pn.x_coord, pn.y_coord, pn.obj_type,
               ft.name_en, ft.name_vn,
               (SELECT GROUP_CONCAT(wf, ' / ') FROM
                (SELECT DISTINCT s2.written_form AS wf FROM spelling s2
                 WHERE s2.placename_id = pn.id LIMIT 10)) AS names
        FROM placename pn
        LEFT JOIN ftype ft ON pn.ftype_id = ft.id
        GROUP BY pn.id
        LIMIT 5
    """)
    for row in cur.fetchall():
        pn_id, sys_id, beg, end, x, y, obj, ft_en, ft_vn, names = row
        names_short = (names or '(no names)')[:80]
        print(f"\n  id={pn_id}, sys_id={sys_id}")
        print(f"    Names:    {names_short}")
        print(f"    Type:     {ft_en or '?'} [{ft_vn or '?'}]")
        print(f"    Temporal: {beg} – {end}")
        print(f"    Spatial:  {x}, {y} ({obj})")

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
    parser = argparse.ArgumentParser(description="Build CHGIS/TGAZ SQLite database")
    parser.add_argument("--summary", action="store_true", help="Print summary only")
    parser.add_argument("--fetch-wikidata", action="store_true",
                        help="Fetch Wikidata Q-IDs via P4711 SPARQL query")
    args = parser.parse_args()

    if not args.summary and not args.fetch_wikidata:
        fetch_sql_dump()

    conn = sqlite3.connect(str(DB_FILE))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    create_schema(conn)

    if args.summary:
        print_summary(conn)
        conn.close()
        return

    if args.fetch_wikidata:
        fetch_wikidata_links(conn)
        print_summary(conn)
        conn.close()
        return

    load_sql_dump(conn)
    print_summary(conn)
    conn.close()


if __name__ == "__main__":
    main()



