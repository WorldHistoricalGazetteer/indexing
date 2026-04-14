"""
Build the Trismegistos Geo SQLite database from TM_geo.sql dump,
then augment with GeoRelations mappings from the TM API.

Usage:
    # Full build (parse SQL + fetch georelations)
    python -m authorities.trismegistos.build_database

    # Parse SQL dump only (no API calls)
    python -m authorities.trismegistos.build_database --sql-only

    # Fetch georelations only (resume after interruption)
    python -m authorities.trismegistos.build_database --relations-only

    # Fetch with custom concurrency
    python -m authorities.trismegistos.build_database --concurrency 5

    # Resolve Wikipedia slugs → Wikidata Q-IDs (resumable)
    python -m authorities.trismegistos.build_database --resolve-wikidata

The resulting database is saved as authorities/trismegistos/tm_geo.db
"""

import argparse
import asyncio
import logging
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DIR = Path(__file__).resolve().parent
SQL_FILE = DIR / "TM_geo.sql"
DB_FILE = DIR / "tm_geo.db"

# GeoRelations API
API_BASE = "https://www.trismegistos.org/dataservices/georelations"
DEFAULT_CONCURRENCY = 10
DELAY_BETWEEN_BATCHES = 0.1  # seconds between launching each request

# Partner projects whose IDs are useful for WHG cross-linking
# Maps JSON key → (our column name, is_whg_authority)
PARTNER_KEYS = {
    "Pleiades": ("pleiades", True),
    "GeoNames": ("geonames", True),
    "Wikipedia": ("wikipedia", False),
    "Wiktionary": ("wiktionary", False),
    "Wikisource": ("wikisource", False),
    "Wikivoyage": ("wikivoyage", False),
    "DARE": ("dare", False),
    "VICI": ("vici", False),
    "Syriaca": ("syriaca", False),
    "DASI": ("dasi", False),
    "Lexicon_Leponticum": ("lexicon_leponticum", False),
    "Talbert_Peutinger": ("talbert_peutinger", False),
    "RIB": ("rib", False),
    "Livius": ("livius", False),
    "FayumTex": ("fayum_tex", False),
    "FayumMap": ("fayum_map", False),
    "RFO": ("rfo", False),
    "Encylopedia_Iranica": ("encyclopedia_iranica", False),
    "ToposText": ("topostext", False),
    "HeritageGateway": ("heritage_gateway", False),
    "EDH": ("edh", False),
    "Medieval_Nubia": ("medieval_nubia", False),
    "nomisma": ("nomisma", False),
    "Desert_Networks": ("desert_networks", False),
    "4CARE/DEChriM": ("dechrim", False),
    "riig": ("riig", False),
    "logeion": ("logeion", False),
    "EB": ("eb", False),
}


# =========================================================================
# Phase 1: Parse MySQL dump → SQLite
# =========================================================================

def create_schema(conn: sqlite3.Connection):
    """Create the geo table and georelations table."""
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS geo (
            tm_geo_id       INTEGER PRIMARY KEY,
            country         TEXT NOT NULL DEFAULT '',
            region          TEXT NOT NULL DEFAULT '',
            nomos_code      TEXT NOT NULL DEFAULT '',
            latin_name      TEXT NOT NULL DEFAULT '',
            standard_name   TEXT NOT NULL DEFAULT '',
            full_name       TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT '',
            ethnicon        TEXT NOT NULL DEFAULT '',
            location        TEXT NOT NULL DEFAULT '',
            greek_unicode   TEXT NOT NULL DEFAULT '',
            egyptian_unicode TEXT NOT NULL DEFAULT '',
            coptic_unicode  TEXT NOT NULL DEFAULT '',
            begin_date      INTEGER NOT NULL DEFAULT 0,
            begin_date_fmt  TEXT NOT NULL DEFAULT '',
            end_date        INTEGER NOT NULL DEFAULT 0,
            end_date_fmt    TEXT NOT NULL DEFAULT '',
            province        TEXT NOT NULL DEFAULT '',
            coordinates     TEXT NOT NULL DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS georelations (
            tm_geo_id       INTEGER NOT NULL,
            partner         TEXT NOT NULL,
            partner_id      TEXT NOT NULL,
            PRIMARY KEY (tm_geo_id, partner, partner_id),
            FOREIGN KEY (tm_geo_id) REFERENCES geo(tm_geo_id)
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_georelations_partner
        ON georelations(partner, partner_id)
    """)

    # Track which IDs have been fetched (for resumability)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS _fetch_status (
            tm_geo_id   INTEGER PRIMARY KEY,
            fetched_at  TEXT,
            has_data    INTEGER DEFAULT 0
        )
    """)

    # Track Wikipedia → Wikidata resolutions (for resumability)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS _wikidata_resolved (
            slug    TEXT PRIMARY KEY,
            qid     TEXT
        )
    """)

    conn.commit()


def parse_tuples(text: str) -> list:
    """Parse MySQL VALUES text into list of tuples."""
    rows = []
    i = 0
    n = len(text)

    while i < n:
        while i < n and text[i] != '(':
            i += 1
        if i >= n:
            break
        i += 1

        fields = []
        while i < n and text[i] != ')':
            while i < n and text[i] in (' ', '\t', '\n', '\r'):
                i += 1
            if i >= n or text[i] == ')':
                break

            if text[i] == "'":
                i += 1
                val = []
                while i < n:
                    if text[i] == '\\' and i + 1 < n:
                        nc = text[i + 1]
                        val.append({"'": "'", "\\": "\\", "n": "\n",
                                    "r": "\r", "t": "\t"}.get(nc, nc))
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
            elif text[i] in ('-', '+') or text[i].isdigit():
                val = []
                while i < n and (text[i].isdigit() or text[i] in ('-', '+', '.')):
                    val.append(text[i])
                    i += 1
                s = "".join(val)
                fields.append(float(s) if '.' in s else int(s))
            elif text[i:i + 4] == 'NULL':
                fields.append(None)
                i += 4

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


def load_sql_dump(conn: sqlite3.Connection):
    """Parse TM_geo.sql and insert rows into the geo table."""
    cur = conn.cursor()

    # Check if already loaded
    cur.execute("SELECT COUNT(*) FROM geo")
    existing = cur.fetchone()[0]
    if existing > 0:
        logger.info(f"geo table already has {existing:,} rows, skipping SQL parse")
        return existing

    logger.info(f"Parsing {SQL_FILE}...")
    row_count = 0
    errors = 0

    with open(SQL_FILE, 'r', encoding='utf-8') as f:
        buffer = ""
        in_insert = False
        for line in f:
            if line.startswith("INSERT INTO"):
                in_insert = True
                idx = line.index("VALUES")
                buffer = line[idx + 6:].strip()
            elif in_insert:
                buffer += " " + line.strip()

            if in_insert and buffer.rstrip().endswith(";"):
                in_insert = False
                buffer = buffer.rstrip().rstrip(";")
                rows = parse_tuples(buffer)
                for row in rows:
                    try:
                        cur.execute(
                            "INSERT OR IGNORE INTO geo VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            row
                        )
                        row_count += 1
                    except Exception as e:
                        errors += 1
                        if errors <= 5:
                            logger.warning(f"Error inserting row {row[0] if row else '?'}: {e}")
                buffer = ""

    conn.commit()
    logger.info(f"Loaded {row_count:,} rows into geo table ({errors} errors)")
    return row_count


# =========================================================================
# Phase 2: Fetch GeoRelations from TM API
# =========================================================================

def get_unfetched_ids(conn: sqlite3.Connection, retry_errors: bool = False) -> list:
    """Get TM Geo IDs that haven't been fetched yet, excluding ghost names.

    If retry_errors is True, also include IDs that were previously fetched
    but returned errors (has_data=0 and no rows in georelations).
    """
    cur = conn.cursor()
    if retry_errors:
        # Re-fetch IDs that errored (has_data=-1)
        cur.execute("""
            SELECT fs.tm_geo_id
            FROM _fetch_status fs
            JOIN geo g ON g.tm_geo_id = fs.tm_geo_id
            WHERE fs.has_data = -1
              AND g.country != 'ghost name'
            ORDER BY fs.tm_geo_id
        """)
        ids = [r[0] for r in cur.fetchall()]
        # Remove their fetch status so they'll be re-fetched
        if ids:
            cur.executemany("DELETE FROM _fetch_status WHERE tm_geo_id = ?",
                            [(i,) for i in ids])
            conn.commit()
            logger.info(f"Reset {len(ids):,} error IDs for retry")

    cur.execute("""
        SELECT g.tm_geo_id
        FROM geo g
        LEFT JOIN _fetch_status fs ON g.tm_geo_id = fs.tm_geo_id
        WHERE fs.tm_geo_id IS NULL
          AND g.country != 'ghost name'
        ORDER BY g.tm_geo_id
    """)
    return [r[0] for r in cur.fetchall()]


def parse_api_response(tm_geo_id: int, data) -> list:
    """
    Parse the GeoRelations API response into (tm_geo_id, partner, partner_id) tuples.
    """
    links = []

    if isinstance(data, dict):
        # Error response: {"Message": "This GEO ID is not in our database."}
        return links

    if not isinstance(data, list):
        return links

    for item in data:
        if not isinstance(item, dict):
            continue
        for key, val in item.items():
            if key in ("TM_Geo_ID", "TM_Geo", "WARNING", "YOU CAN"):
                continue
            if val is None:
                continue

            # Normalise to list
            ids = val if isinstance(val, list) else [val]
            for pid in ids:
                if pid is not None and str(pid).strip():
                    partner_name = key
                    # Use our column name if mapped, else the raw key
                    col_info = PARTNER_KEYS.get(key)
                    if col_info:
                        partner_name = col_info[0]
                    links.append((tm_geo_id, partner_name, str(pid).strip()))

    return links


async def fetch_georelations(conn: sqlite3.Connection, concurrency: int = DEFAULT_CONCURRENCY,
                             retry_errors: bool = False):
    """Fetch GeoRelations for all unfetched TM Geo IDs."""
    try:
        import httpx
    except ImportError:
        logger.error("httpx required: pip install httpx")
        sys.exit(1)

    ids_to_fetch = get_unfetched_ids(conn, retry_errors=retry_errors)
    total = len(ids_to_fetch)

    if total == 0:
        logger.info("All IDs already fetched — nothing to do")
        return

    logger.info(f"Fetching georelations for {total:,} IDs (concurrency={concurrency})")

    fetched = 0
    links_total = 0
    errors = 0
    start_time = time.time()

    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_one(client: httpx.AsyncClient, tm_id: int) -> tuple:
        """Fetch a single ID with retry on 5xx, return (tm_id, links, error_flag)."""
        async with semaphore:
            await asyncio.sleep(DELAY_BETWEEN_BATCHES)
            last_exc = None
            for attempt in range(3):
                try:
                    resp = await client.get(f"{API_BASE}/{tm_id}", timeout=30.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        links = parse_api_response(tm_id, data)
                        return (tm_id, links, False)
                    elif resp.status_code >= 500:
                        # Server error — back off and retry
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        return (tm_id, [], True)
                except Exception as e:
                    last_exc = e
                    await asyncio.sleep(2 ** attempt)
            return (tm_id, [], True)

    # Process in batches for periodic commits
    BATCH_SIZE = 500
    cur = conn.cursor()

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=concurrency + 2, max_keepalive_connections=concurrency),
        headers={"User-Agent": "WHG-Indexing/1.0 (https://whgazetteer.org; research)"},
    ) as client:
        for batch_start in range(0, total, BATCH_SIZE):
            batch_ids = ids_to_fetch[batch_start:batch_start + BATCH_SIZE]

            tasks = [fetch_one(client, tm_id) for tm_id in batch_ids]
            results = await asyncio.gather(*tasks)

            for tm_id, links, error in results:
                if error:
                    errors += 1
                    # Mark as error (-1) so --retry-errors can target these specifically
                    cur.execute(
                        "INSERT OR REPLACE INTO _fetch_status (tm_geo_id, fetched_at, has_data) VALUES (?, datetime('now'), -1)",
                        (tm_id,)
                    )
                else:
                    has_data = 1 if links else 0
                    cur.execute(
                        "INSERT OR REPLACE INTO _fetch_status (tm_geo_id, fetched_at, has_data) VALUES (?, datetime('now'), ?)",
                        (tm_id, has_data)
                    )
                    for link in links:
                        cur.execute(
                            "INSERT OR IGNORE INTO georelations (tm_geo_id, partner, partner_id) VALUES (?, ?, ?)",
                            link
                        )
                    links_total += len(links)

                fetched += 1

            conn.commit()

            elapsed = time.time() - start_time
            rate = fetched / elapsed if elapsed > 0 else 0
            remaining = (total - fetched) / rate if rate > 0 else 0
            logger.info(
                f"  {fetched:,}/{total:,} fetched | "
                f"{links_total:,} links | "
                f"{errors:,} errors | "
                f"{rate:.0f}/s | "
                f"ETA {remaining / 60:.0f}m"
            )

    logger.info(f"GeoRelations fetch complete: {fetched:,} IDs, {links_total:,} links, {errors:,} errors")


# =========================================================================
# Phase 3: Resolve Wikipedia slugs → Wikidata Q-IDs
# =========================================================================

MEDIAWIKI_API = "https://en.wikipedia.org/w/api.php"
MEDIAWIKI_BATCH_SIZE = 50  # MediaWiki API limit for titles per request
MEDIAWIKI_DELAY = 0.5  # seconds between MediaWiki API requests (respect rate limits)


async def resolve_wikidata_ids(conn: sqlite3.Connection, concurrency: int = 2):
    """Resolve Wikipedia article slugs to Wikidata Q-IDs via the MediaWiki API.

    Reads wikipedia partner_ids from georelations, batches them into groups
    of 50, and queries the MediaWiki API for wikibase_item page properties.
    Results are stored as new georelations rows with partner='wikidata'.
    """
    try:
        import httpx
    except ImportError:
        logger.error("httpx required: pip install httpx")
        sys.exit(1)

    cur = conn.cursor()

    # Get all Wikipedia slugs that don't already have a wikidata resolution
    cur.execute("""
        SELECT DISTINCT gr.partner_id
        FROM georelations gr
        WHERE gr.partner = 'wikipedia'
          AND gr.partner_id NOT IN (
              SELECT slug FROM _wikidata_resolved
          )
        ORDER BY gr.partner_id
    """)
    slugs = [r[0] for r in cur.fetchall()]

    if not slugs:
        logger.info("All Wikipedia slugs already resolved — nothing to do")
        return

    logger.info(f"Resolving {len(slugs):,} Wikipedia slugs → Wikidata Q-IDs "
                f"(~{len(slugs) // MEDIAWIKI_BATCH_SIZE + 1} API calls)")

    resolved = 0
    found = 0
    missing = 0
    start_time = time.time()

    semaphore = asyncio.Semaphore(concurrency)

    async def resolve_batch(client: httpx.AsyncClient, batch: list[str]) -> dict[str, str | None]:
        """Resolve a batch of Wikipedia slugs to Q-IDs. Returns {slug: qid_or_None}."""
        async with semaphore:
            await asyncio.sleep(MEDIAWIKI_DELAY)
            # URL-decode slugs to get proper article titles
            titles = [unquote(slug) for slug in batch]
            params = {
                "action": "query",
                "prop": "pageprops",
                "ppprop": "wikibase_item",
                "titles": "|".join(titles),
                "format": "json",
                "redirects": "1",
            }

            result = {slug: None for slug in batch}

            for attempt in range(5):
                try:
                    resp = await client.get(MEDIAWIKI_API, params=params, timeout=30.0)
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("retry-after", 5 * (attempt + 1)))
                        logger.warning(f"Rate-limited (429), waiting {retry_after:.0f}s "
                                       f"(attempt {attempt + 1}/5)")
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status_code != 200:
                        await asyncio.sleep(2 ** attempt)
                        continue

                    data = resp.json()
                    query = data.get("query", {})
                    pages = query.get("pages", {})

                    # Build reverse map: normalised/redirected title → original slug
                    # MediaWiki normalises underscores to spaces and may redirect
                    title_to_slug = {}
                    for slug in batch:
                        decoded = unquote(slug)
                        # MediaWiki normalises: underscores → spaces, first letter uppercase
                        normalised = decoded.replace("_", " ")
                        title_to_slug[normalised] = slug
                        title_to_slug[decoded] = slug

                    # Track redirects: from → to
                    for redir in query.get("redirects", []):
                        title_to_slug[redir["to"]] = title_to_slug.get(redir["from"], redir["from"])
                    for norm in query.get("normalized", []):
                        title_to_slug[norm["to"]] = title_to_slug.get(norm["from"], norm["from"])

                    for page_id, page in pages.items():
                        if "missing" in page:
                            continue
                        qid = page.get("pageprops", {}).get("wikibase_item")
                        if qid:
                            title = page.get("title", "")
                            slug = title_to_slug.get(title)
                            if slug:
                                result[slug] = qid

                    return result

                except Exception:
                    await asyncio.sleep(2 ** attempt)

            return result

    # Process in batches
    batches = [slugs[i:i + MEDIAWIKI_BATCH_SIZE] for i in range(0, len(slugs), MEDIAWIKI_BATCH_SIZE)]

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=concurrency + 2, max_keepalive_connections=concurrency),
        headers={"User-Agent": "WHG-Indexing/1.0 (https://whgazetteer.org; research)"},
    ) as client:
        # Process batches with bounded concurrency (semaphore + delay handle rate limiting)
        for chunk_start in range(0, len(batches), concurrency):
            chunk = batches[chunk_start:chunk_start + concurrency]
            tasks = [resolve_batch(client, batch) for batch in chunk]
            results = await asyncio.gather(*tasks)

            for batch_result in results:
                for slug, qid in batch_result.items():
                    resolved += 1
                    if qid:
                        found += 1
                        # Insert wikidata links for all TM IDs that reference this Wikipedia slug
                        cur.execute(
                            "SELECT tm_geo_id FROM georelations WHERE partner = 'wikipedia' AND partner_id = ?",
                            (slug,)
                        )
                        for (tm_id,) in cur.fetchall():
                            cur.execute(
                                "INSERT OR IGNORE INTO georelations (tm_geo_id, partner, partner_id) VALUES (?, 'wikidata', ?)",
                                (tm_id, qid)
                            )
                    else:
                        missing += 1

                    # Track resolution to avoid re-fetching
                    cur.execute(
                        "INSERT OR IGNORE INTO _wikidata_resolved (slug, qid) VALUES (?, ?)",
                        (slug, qid)
                    )

            conn.commit()

            elapsed = time.time() - start_time
            rate = resolved / elapsed if elapsed > 0 else 0
            logger.info(
                f"  {resolved:,}/{len(slugs):,} resolved | "
                f"{found:,} Q-IDs found | "
                f"{missing:,} missing | "
                f"{rate:.0f}/s"
            )

    logger.info(f"Wikidata resolution complete: {found:,} Q-IDs found, {missing:,} missing out of {resolved:,}")


def print_summary(conn: sqlite3.Connection):
    """Print a summary of the database contents."""
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM geo")
    geo_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM geo WHERE country != 'ghost name'")
    real_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM _fetch_status")
    fetched_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM _fetch_status WHERE has_data = 1")
    with_data = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM georelations")
    link_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT tm_geo_id) FROM georelations")
    linked_ids = cur.fetchone()[0]

    print(f"\n{'='*60}")
    print(f"  Trismegistos Geo Database Summary")
    print(f"{'='*60}")
    print(f"  geo table:          {geo_count:>8,} records ({real_count:,} non-ghost)")
    print(f"  IDs fetched:        {fetched_count:>8,}")
    print(f"  IDs with relations: {with_data:>8,}")
    print(f"  georelations table: {link_count:>8,} links")
    print(f"  Linked TM IDs:      {linked_ids:>8,}")

    # Per-partner breakdown
    cur.execute("""
        SELECT partner, COUNT(*) as cnt, COUNT(DISTINCT tm_geo_id) as ids
        FROM georelations
        GROUP BY partner
        ORDER BY cnt DESC
    """)
    rows = cur.fetchall()
    if rows:
        print(f"\n  Partner link counts:")
        for partner, cnt, ids in rows:
            whg_marker = " ★" if partner in ("pleiades", "geonames") else ""
            print(f"    {partner:<25} {cnt:>6} links ({ids:,} TM IDs){whg_marker}")

    print(f"\n  Database: {DB_FILE}")
    db_size = DB_FILE.stat().st_size / (1024 * 1024)
    print(f"  Size:     {db_size:.1f} MB")
    print()


# =========================================================================
# Main
# =========================================================================

def main():
    parser = argparse.ArgumentParser(description="Build Trismegistos Geo SQLite database")
    parser.add_argument("--sql-only", action="store_true", help="Parse SQL dump only, skip API fetch")
    parser.add_argument("--relations-only", action="store_true", help="Fetch georelations only (resume)")
    parser.add_argument("--resolve-wikidata", action="store_true",
                        help="Resolve Wikipedia links to Wikidata Q-IDs")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Max concurrent API requests (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--retry-errors", action="store_true",
                        help="Re-fetch IDs that previously returned errors")
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

    if args.resolve_wikidata:
        asyncio.run(resolve_wikidata_ids(conn, concurrency=min(args.concurrency, 3)))
        print_summary(conn)
        conn.close()
        return

    if not args.relations_only:
        load_sql_dump(conn)

    if not args.sql_only:
        asyncio.run(fetch_georelations(conn, concurrency=args.concurrency,
                                       retry_errors=args.retry_errors))

    print_summary(conn)
    conn.close()


if __name__ == "__main__":
    main()


