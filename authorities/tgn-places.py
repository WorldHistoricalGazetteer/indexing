#!/usr/bin/env python
"""
TGN Ingestion for Elasticsearch (Optimized for Pitt CRC)
- fixes: reads TGNOut_1Subjects.nt (links places to names)
- fixes: captures language tags (prevents pipeline deletion)
- performance: copies ZIP to local scratch ($TMPDIR) before processing
"""

import zipfile
import sys
import shutil
import os
import time
from pathlib import Path
from collections import defaultdict
from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

# Connect to ES
es = Elasticsearch(ES_HOST, request_timeout=180)


def stream_nt(file_path, filename_in_zip):
    """Streams lines from a specific file inside the zip (with fuzzy match)."""
    path = Path(file_path)
    with zipfile.ZipFile(path, 'r') as zf:
        if filename_in_zip not in zf.namelist():
            # Fallback: fuzzy match (e.g. find TGNOut_1Subjects.nt if looking for Subjects)
            candidates = [n for n in zf.namelist() if filename_in_zip.replace("1", "") in n]
            if candidates:
                filename_in_zip = candidates[0]
            else:
                print(f"❌ Error: Could not find {filename_in_zip} in zip.")
                return

        with zf.open(filename_in_zip, 'r') as f:
            for line in f:
                yield line.decode('utf-8')


def parse_nt(line):
    """
    Parses N-Triples line.
    Returns:
       - (subj, pred, uri_string) for URIs
       - (subj, pred, (literal_string, lang_string)) for Literals
    """
    line = line.strip()
    if not line or line[0] != "<":
        return None
    try:
        s_end = line.index(">")
        subj = line[1:s_end]

        p_start = line.index("<", s_end)
        p_end = line.index(">", p_start)
        pred = line[p_start + 1:p_end]

        rest = line[p_end + 1:].strip()

        # URI
        if rest.startswith("<"):
            obj = rest[1:rest.index(">")]
            return subj, pred, obj

        # Literal
        if rest.startswith('"'):
            last_quote = rest.rindex('"')
            value = rest[1:last_quote]

            remaining = rest[last_quote + 1:]
            lang = ""
            if remaining.strip().startswith("@"):
                lang_part = remaining.strip()[1:].split()[0]
                if lang_part.endswith("."): lang_part = lang_part[:-1]
                lang = lang_part

            return subj, pred, (value, lang)
        return None
    except ValueError:
        return None


def build_side_index(zip_path):
    """Load coordinates, terms, and LINKS into memory."""
    print("Building in-memory side index...")

    coordinates = {}
    term_literals = {}
    place_pref = {}
    place_terms = defaultdict(list)

    # 1. Coordinates
    print("Step 1/3: Loading Coordinates...")
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_Coordinates.nt"), 1):
        parsed = parse_nt(line)
        if not parsed: continue
        subj, pred, obj = parsed
        if isinstance(obj, tuple): obj = obj[0]

        coord = coordinates.setdefault(subj, [None, None])
        if pred.endswith("#lat"):
            coord[0] = float(obj)
        elif pred.endswith("#long"):
            coord[1] = float(obj)

        if i % 1_000_000 == 0: sys.stdout.write(f"\r  {i:,} triples"); sys.stdout.flush()

    coordinates = {k: tuple(v) for k, v in coordinates.items() if None not in v}
    print(f"\n  ✓ Loaded {len(coordinates):,} complete coordinates")

    # 2. Term Literals (Definitions)
    print("Step 2/3: Loading Term Literals (TGNOut_2Terms.nt)...")
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_2Terms.nt"), 1):
        parsed = parse_nt(line)
        if not parsed: continue
        subj, pred, obj = parsed

        if pred.endswith("literalForm"):
            if not isinstance(obj, tuple): obj = (obj, "")
            term_literals[subj] = obj

        if i % 1_000_000 == 0: sys.stdout.write(f"\r  {i:,} triples"); sys.stdout.flush()
    print(f"\n  ✓ {len(term_literals):,} term definitions loaded")

    # 3. Place-Term Links (TGNOut_1Subjects.nt)
    print("Step 3/3: Loading Place-to-Term Links (TGNOut_1Subjects.nt)...")
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_1Subjects.nt"), 1):
        parsed = parse_nt(line)
        if not parsed: continue
        subj, pred, obj = parsed
        if isinstance(obj, tuple): obj = obj[0]

        tgn_id = subj.split("/tgn/")[-1]

        if pred.endswith("prefLabelGVP"):
            place_pref[tgn_id] = obj
            place_terms[tgn_id].append(obj)
        elif pred.endswith("prefLabel"):
            place_terms[tgn_id].append(obj)

        if i % 1_000_000 == 0: sys.stdout.write(f"\r  {i:,} triples"); sys.stdout.flush()

    print(f"\n  ✓ {len(place_pref):,} preferred labels linked")
    return coordinates, term_literals, place_pref, place_terms


def index_tgn(zip_path, places_index):
    print("=" * 60)
    print(f"INDEXING TGN from {zip_path}")
    print("=" * 60)

    coordinates, term_literals, place_pref, place_terms = build_side_index(zip_path)

    batch = []
    count = 0
    start_time = time.time()
    total_places = len(coordinates)
    processed = 0

    print(f"\nStarting Indexing of {total_places:,} places...")

    for place_uri, (lat, lon) in coordinates.items():
        processed += 1
        tgn_id = place_uri.split("/tgn/")[-1]

        # Build Toponyms
        term_uris = set(place_terms.get(tgn_id, []))
        if place_pref.get(tgn_id): term_uris.add(place_pref[tgn_id])

        toponyms = []
        seen_ids = set()

        for term_uri in term_uris:
            literal_data = term_literals.get(term_uri)
            if not literal_data: continue

            name, lang = literal_data
            toponym_id = f"{name}@{lang}"  # Required format for pipeline

            if toponym_id in seen_ids: continue

            toponyms.append({
                "toponym_id": toponym_id,
                "timespans": [{"start": {"in": 2025}, "end": {"in": 2025}}]
            })
            seen_ids.add(toponym_id)

        # Fallback Title
        title = toponyms[0]["toponym_id"].split("@")[0] if toponyms else f"TGN {tgn_id}"
        place_id = f"tgn:{tgn_id}"

        doc = {
            "place_id": place_id,
            "title": title,
            "toponyms": toponyms,
            "geometries": [{
                "geom": {"type": "Point", "coordinates": [lon, lat]},
                "repr_point": {"lon": lon, "lat": lat},
                "timespans": [{"start": {"in": 2025}, "end": {"in": 2025}}]
            }],
            "source": "tgn",
            "namespace": "tgn",
            "types": [{"identifier": "place", "label": "tgn", "sourceLabel": "getty-tgn"}]
        }

        batch.append({"_index": places_index, "_id": place_id, "_source": doc})

        if len(batch) >= BATCH_SIZE:
            helpers.bulk(es, batch, stats_only=True, raise_on_error=False)
            count += len(batch)
            batch.clear()
            if count % 10000 == 0:
                sys.stdout.write(f"\rIndexed {count:,} places ({(processed / total_places) * 100:.1f}%)")
                sys.stdout.flush()

    if batch:
        helpers.bulk(es, batch, stats_only=True, raise_on_error=False)
        count += len(batch)

    elapsed = time.time() - start_time
    print(f"\n\n✓ DONE: {count:,} places in {elapsed / 60:.1f} min")
    create_checkpoint_snapshot(es, "tgn_places_fixed")


if __name__ == "__main__":
    SOURCE_FILE = f"{DATA_DIR}/authorities/tgn/explicit.zip"
    PLACES_INDEX = "places"

    # --- SCRATCH DETECTION & COPY ---
    # Using TMPDIR as confirmed by your cluster check
    scratch_dir = os.environ.get("TMPDIR")

    if scratch_dir and os.path.isdir(scratch_dir):
        print(f"🚀 SLURM DETECTED: Using Scratch {scratch_dir}")
        target_path = os.path.join(scratch_dir, "tgn_explicit.zip")

        print(f"   Copying source file (3GB+)...")
        start_copy = time.time()
        try:
            shutil.copy2(SOURCE_FILE, target_path)
            print(f"   ✅ Copy complete ({time.time() - start_copy:.1f}s)")
            TGN_FILE = target_path
        except Exception as e:
            print(f"   ⚠️ Copy failed ({e}). Using network path.")
            TGN_FILE = SOURCE_FILE
    else:
        print("⚠️  TMPDIR not found. Using shared storage (Slower).")
        TGN_FILE = SOURCE_FILE

    # --- RUN INGESTION ---
    if not Path(TGN_FILE).exists():
        print(f"❌ File not found: {TGN_FILE}")
        sys.exit(1)

    try:
        index_tgn(TGN_FILE, PLACES_INDEX)
    finally:
        # Cleanup scratch if we used it
        if scratch_dir and TGN_FILE.startswith(scratch_dir) and os.path.exists(TGN_FILE):
            print(f"🧹 Cleaning up {TGN_FILE}...")
            os.remove(TGN_FILE)