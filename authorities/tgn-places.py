#!/usr/bin/env python
"""
Index Getty Thesaurus of Geographic Names (TGN) into Elasticsearch.

Design goals:
- Fully streaming RDF ingestion
- SQLite side-index for scalable joins
- No in-memory mega-dicts
- Safe for Slurm / HPC environments
"""

import re
import sqlite3
import zipfile
from pathlib import Path
from contextlib import contextmanager
from collections import defaultdict

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot


# ------------------------------------------------------------------------------
# Elasticsearch
# ------------------------------------------------------------------------------

es = Elasticsearch(ES_HOST, request_timeout=180)
PLACES_INDEX = "places"


# ------------------------------------------------------------------------------
# SQLite helpers
# ------------------------------------------------------------------------------

SQLITE_PATH = Path(DATA_DIR) / "authorities" / "tgn" / "tgn_side_index.sqlite"


def init_sqlite(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.executescript("""
    PRAGMA journal_mode = WAL;
    PRAGMA synchronous = NORMAL;

    CREATE TABLE IF NOT EXISTS coordinates (
        coord_uri TEXT PRIMARY KEY,
        lat REAL,
        lon REAL
    );

    CREATE TABLE IF NOT EXISTS term_literals (
        term_uri TEXT PRIMARY KEY,
        text TEXT,
        lang TEXT
    );

    CREATE TABLE IF NOT EXISTS place_pref_term (
        tgn_id TEXT PRIMARY KEY,
        term_uri TEXT
    );

    CREATE TABLE IF NOT EXISTS place_terms (
        tgn_id TEXT,
        term_uri TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_place_terms_tgn
        ON place_terms (tgn_id);
    """)

    conn.commit()
    return conn


# ------------------------------------------------------------------------------
# RDF helpers
# ------------------------------------------------------------------------------

NT_RE = re.compile(r'<([^>]+)>\s+<([^>]+)>\s+(.+)\s+\.$')


def parse_ntriple(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    m = NT_RE.match(line)
    if not m:
        return None

    subject, predicate, obj = m.groups()

    if obj.startswith("<"):
        return subject, predicate, obj[1:-1], "uri"

    if obj.startswith('"'):
        end = 1
        while end < len(obj):
            if obj[end] == '"' and obj[end - 1] != "\\":
                break
            end += 1
        raw = obj[1:end]
        try:
            value = raw.encode("utf-8").decode("unicode_escape")
        except Exception:
            value = raw

        rest = obj[end + 1:].strip()
        lang = None
        if rest.startswith("@"):
            lang = rest[1:]

        return subject, predicate, value, lang or "literal"

    return None


@contextmanager
def open_nt_file(base_path: Path, filename: str):
    """
    Transparently open NT files from ZIP or directory.
    """
    if base_path.suffix == ".zip":
        with zipfile.ZipFile(base_path, "r") as zf:
            with zf.open(filename, "r") as f:
                yield f
    else:
        path = base_path if base_path.is_dir() else base_path.parent
        nt = path / filename
        if not nt.exists():
            raise FileNotFoundError(nt)
        with open(nt, "rb") as f:
            yield f


# ------------------------------------------------------------------------------
# Phase 1: Build SQLite side-index
# ------------------------------------------------------------------------------

def build_coordinates(conn, source_path: Path):
    print("\nBuilding coordinates index...")
    cur = conn.cursor()

    with open_nt_file(source_path, "TGNOut_Coordinates.nt") as f:
        for i, line in enumerate(f, 1):
            if i % 500_000 == 0:
                print(f"  {i:,} triples")

            parsed = parse_ntriple(line.decode("utf-8", errors="ignore"))
            if not parsed:
                continue

            subj, pred, val, _ = parsed

            if pred.endswith("#lat"):
                cur.execute(
                    "INSERT OR IGNORE INTO coordinates VALUES (?, NULL, NULL)",
                    (subj,)
                )
                cur.execute(
                    "UPDATE coordinates SET lat=? WHERE coord_uri=?",
                    (float(val), subj)
                )

            elif pred.endswith("#long"):
                cur.execute(
                    "INSERT OR IGNORE INTO coordinates VALUES (?, NULL, NULL)",
                    (subj,)
                )
                cur.execute(
                    "UPDATE coordinates SET lon=? WHERE coord_uri=?",
                    (float(val), subj)
                )

    conn.commit()


def build_terms(conn, source_path: Path):
    print("\nBuilding term and label indexes...")
    cur = conn.cursor()

    with open_nt_file(source_path, "TGNOut_2Terms.nt") as f:
        for i, line in enumerate(f, 1):
            if i % 1_000_000 == 0:
                print(f"  {i:,} triples")

            parsed = parse_ntriple(line.decode("utf-8", errors="ignore"))
            if not parsed:
                continue

            subj, pred, val, lang = parsed

            if pred.endswith("literalForm"):
                cur.execute(
                    "INSERT OR IGNORE INTO term_literals VALUES (?, ?, ?)",
                    (subj, val, lang if lang != "literal" else None)
                )

            elif pred.endswith("prefLabelGVP") and "/tgn/" in subj:
                tgn_id = subj.rsplit("/tgn/", 1)[-1]
                cur.execute(
                    "INSERT OR REPLACE INTO place_pref_term VALUES (?, ?)",
                    (tgn_id, val)
                )

            elif pred.endswith("prefLabel") and "/tgn/" in subj:
                tgn_id = subj.rsplit("/tgn/", 1)[-1]
                cur.execute(
                    "INSERT INTO place_terms VALUES (?, ?)",
                    (tgn_id, val)
                )

    conn.commit()


# ------------------------------------------------------------------------------
# Phase 2: Stream placemap and index ES
# ------------------------------------------------------------------------------

def iter_place_coords(source_path: Path):
    with open_nt_file(source_path, "TGNOut_PlaceMap.nt") as f:
        for line in f:
            parsed = parse_ntriple(line.decode("utf-8", errors="ignore"))
            if not parsed:
                continue

            subj, pred, obj, _ = parsed
            if pred.endswith("foaf/0.1/focus") and "/tgn/" in subj:
                yield subj.rsplit("/tgn/", 1)[-1], obj


def create_place_doc(cur, tgn_id, coord_uri):
    row = cur.execute(
        "SELECT lat, lon FROM coordinates WHERE coord_uri=?",
        (coord_uri,)
    ).fetchone()

    if not row or row[0] is None or row[1] is None:
        return None

    lat, lon = row

    label = f"TGN {tgn_id}"
    toponyms = []

    pref = cur.execute(
        "SELECT term_uri FROM place_pref_term WHERE tgn_id=?",
        (tgn_id,)
    ).fetchone()

    if pref:
        lit = cur.execute(
            "SELECT text, lang FROM term_literals WHERE term_uri=?",
            (pref[0],)
        ).fetchone()
        if lit:
            label = lit[0]
            toponyms.append({
                "toponym_id": f"{lit[0]}@{lit[1]}",
                "timespan": {"start": {"in": 2025}, "end": {"in": 2025}}
            })

    for (term_uri,) in cur.execute(
        "SELECT term_uri FROM place_terms WHERE tgn_id=?",
        (tgn_id,)
    ):
        lit = cur.execute(
            "SELECT text, lang FROM term_literals WHERE term_uri=?",
            (term_uri,)
        ).fetchone()
        if lit:
            toponyms.append({
                "toponym_id": f"{lit[0]}@{lit[1]}",
                "timespan": {"start": {"in": 2025}, "end": {"in": 2025}}
            })

    return {
        "place_id": f"tgn:{tgn_id}",
        "label": label,
        "toponyms": toponyms,
        "repr_point": {"lat": lat, "lon": lon},
        "geom": {"type": "Point", "coordinates": [lon, lat]},
        "src": "tgn",
        "types": [{
            "identifier": "place",
            "label": "tgn",
            "sourceLabel": "getty-tgn"
        }]
    }


def index_tgn(source_path: Path):
    print("\nIndexing TGN places...")
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()

    batch = []
    count = 0

    for i, (tgn_id, coord_uri) in enumerate(iter_place_coords(source_path), 1):
        doc = create_place_doc(cur, tgn_id, coord_uri)
        if not doc:
            continue

        batch.append({
            "_index": PLACES_INDEX,
            "_id": doc["place_id"],
            "_source": doc
        })

        if len(batch) >= BATCH_SIZE:
            helpers.bulk(es, batch, raise_on_error=False)
            count += len(batch)
            batch.clear()

        if i % 100_000 == 0:
            print(f"  processed {i:,}, indexed {count:,}")

    if batch:
        helpers.bulk(es, batch, raise_on_error=False)
        count += len(batch)

    print(f"\n✓ Indexed {count:,} TGN places")


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    SOURCE = Path(DATA_DIR) / "authorities" / "tgn" / "explicit.zip"

    print("=" * 80)
    print("TGN INGEST (STREAMING + SQLITE)")
    print("=" * 80)

    conn = init_sqlite(SQLITE_PATH)

    build_coordinates(conn, SOURCE)
    build_terms(conn, SOURCE)
    conn.close()

    index_tgn(SOURCE)
    create_checkpoint_snapshot(es, "tgn_places")
