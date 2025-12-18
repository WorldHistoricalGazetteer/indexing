#!/usr/bin/env python
"""
TGN ingestion into Elasticsearch (maximally optimized)

- Full streaming of NT files from ZIP or extracted
- Batched SQLite writes and/or in-memory dictionaries
- Pre-joined term literals for fast ES indexing
- Bulk ES inserts
- Scales to 40M+ triples
"""

import zipfile
from pathlib import Path
from collections import defaultdict
import sqlite3
import time
from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)

# -------------------------
# NT streaming & parsing
# -------------------------

def stream_nt(file_path, filename_in_zip=None):
    path = Path(file_path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path, 'r') as zf:
            with zf.open(filename_in_zip, 'r') as f:
                for line in f:
                    yield line.decode('utf-8')
    else:
        file_to_open = path if path.is_file() else path / filename_in_zip
        with open(file_to_open, 'r', encoding='utf-8') as f:
            for line in f:
                yield line

def parse_nt(line):
    line = line.strip()
    if not line or line[0] != "<":
        return None
    try:
        s_end = line.index(">")
        p_start = line.index("<", s_end)
        p_end = line.index(">", p_start)
        subj = line[1:s_end]
        pred = line[p_start+1:p_end]
        rest = line[p_end+1:].strip()
        if rest[0] == "<":
            return subj, pred, rest[1:rest.index(">")]
        if rest[0] == '"':
            q = rest.rindex('"')
            return subj, pred, rest[1:q]
        return None
    except ValueError:
        return None

# -------------------------
# In-memory side-index
# -------------------------

def build_side_index(zip_path):
    """Load coordinates and terms into memory for fast lookups"""
    print("Building in-memory side index...")

    coordinates = {}
    term_literals = {}
    place_pref = {}
    place_terms = defaultdict(list)

    # Coordinates
    print("Loading coordinates...")
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_Coordinates.nt"), 1):
        parsed = parse_nt(line)
        if not parsed:
            continue
        subj, pred, obj = parsed
        coord = coordinates.setdefault(subj, [None, None])
        if pred.endswith("#lat"):
            coord[0] = float(obj)
        elif pred.endswith("#long"):
            coord[1] = float(obj)
        if i % 500_000 == 0:
            print(f"  {i:,} triples")

    # Remove incomplete
    coordinates = {k: tuple(v) for k, v in coordinates.items() if None not in v}
    print(f"✓ Loaded {len(coordinates):,} complete coordinates")

    # Term literals and place-term mappings
    print("Loading term literals and place mappings...")
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_2Terms.nt"), 1):
        parsed = parse_nt(line)
        if not parsed:
            continue
        subj, pred, obj = parsed
        if pred.endswith("literalForm"):
            term_literals[subj] = obj
        elif pred.endswith("prefLabelGVP"):
            tgn_id = subj.split("/tgn/")[-1]
            place_pref[tgn_id] = obj
            place_terms[tgn_id].append(obj)
        elif pred.endswith("prefLabel"):
            tgn_id = subj.split("/tgn/")[-1]
            place_terms[tgn_id].append(obj)
        if i % 1_000_000 == 0:
            print(f"  {i:,} triples")

    print(f"✓ {len(term_literals):,} terms loaded")
    print(f"✓ {len(place_pref):,} preferred labels")
    print(f"✓ {len(place_terms):,} places with terms")

    return coordinates, term_literals, place_pref, place_terms

# -------------------------
# ES indexing
# -------------------------

def index_tgn(zip_path, places_index):
    print("="*80)
    print("TGN INDEXING (MAXIMALLY OPTIMIZED)")
    print("="*80)

    coordinates, term_literals, place_pref, place_terms = build_side_index(zip_path)

    batch = []
    count = 0
    start_time = time.time()

    for i, line in enumerate(stream_nt(zip_path, "TGNOut_PlaceMap.nt"), 1):
        parsed = parse_nt(line)
        if not parsed:
            continue
        subj, pred, obj = parsed
        if not pred.endswith("focus"):
            continue

        tgn_id = subj.split("/tgn/")[-1]
        coord_uri = obj

        if coord_uri not in coordinates:
            continue
        lat, lon = coordinates[coord_uri]

        # Build toponyms
        seen = set()
        toponyms = []

        for term_uri in [place_pref.get(tgn_id)] + place_terms.get(tgn_id, []):
            if not term_uri:
                continue
            text = term_literals.get(term_uri)
            if not text:
                continue
            if text not in seen:
                toponyms.append({
                    "toponym_id": text,
                    "timespan": {"start": {"in": 2025}, "end": {"in": 2025}}
                })
                seen.add(text)

        label = toponyms[0]["toponym_id"] if toponyms else f"TGN {tgn_id}"
        place_id = f"tgn:{tgn_id}"

        doc = {
            "place_id": place_id,
            "label": label,
            "toponyms": toponyms,
            "locations": [{"geometry": {"type": "Point", "coordinates": [lon, lat]},
                           "rep_point": {"lon": lon, "lat": lat}}],
            "source": "tgn",
            "types": [{"identifier": "place", "label": "tgn", "sourceLabel": "getty-tgn"}]
        }

        batch.append({"_index": places_index, "_id": place_id, "_source": doc})

        if len(batch) >= BATCH_SIZE:
            success, failed = helpers.bulk(es, batch, stats_only=True, raise_on_error=False)
            count += success
            batch.clear()

        if i % 100_000 == 0:
            print(f"Processed {i:,} triples, indexed {count:,} places")

    if batch:
        success, failed = helpers.bulk(es, batch, stats_only=True, raise_on_error=False)
        count += success

    elapsed = time.time() - start_time
    print(f"\n✓ INDEXING COMPLETE: {count:,} places in {elapsed/60:.1f} min")
    create_checkpoint_snapshot(es, "tgn_places")

# -------------------------
# Main
# -------------------------

if __name__ == "__main__":
    TGN_FILE = f"{DATA_DIR}/authorities/tgn/explicit.zip"
    PLACES_INDEX = "places"
    index_tgn(TGN_FILE, PLACES_INDEX)
