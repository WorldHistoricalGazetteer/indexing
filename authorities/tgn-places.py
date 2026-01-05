#!/usr/bin/env python
"""
TGN Ingestion (Final Architecture)
1. Loads PlaceMap (Inverted) to map CoordinateIDs -> ConceptIDs
2. Loads Terms & Subjects to link Concepts -> Names
3. Sanity Check: Verifies resolution chain before starting
4. Iterates Coordinates -> PlaceID -> ConceptID -> TermID -> Name
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

es = Elasticsearch(ES_HOST, request_timeout=180)


def stream_nt(file_path, filename_in_zip):
    path = Path(file_path)
    with zipfile.ZipFile(path, 'r') as zf:
        if filename_in_zip not in zf.namelist():
            # Fuzzy match for filenames (e.g. TGNOut_1Subjects.nt vs TGNOut_Subjects.nt)
            candidates = [n for n in zf.namelist() if filename_in_zip.replace("1", "") in n]
            if candidates:
                filename_in_zip = candidates[0]
            else:
                return

        with zf.open(filename_in_zip, 'r') as f:
            for line in f:
                yield line.decode('utf-8')


def parse_nt(line):
    """Parses N-Triples into Subject, Predicate, Object."""
    line = line.strip()
    if not line or line[0] != "<": return None
    try:
        s_end = line.index(">")
        subj = line[1:s_end]
        p_start = line.index("<", s_end)
        p_end = line.index(">", p_start)
        pred = line[p_start + 1:p_end]
        rest = line[p_end + 1:].strip()

        # Object is URI
        if rest.startswith("<"):
            return subj, pred, rest[1:rest.index(">")]

        # Object is Literal
        if rest.startswith('"'):
            last_quote = rest.rindex('"')
            value = rest[1:last_quote]
            remaining = rest[last_quote + 1:].strip()
            # Handle language tags (@en)
            lang = remaining[1:].split()[0].rstrip(".") if remaining.startswith("@") else ""
            return subj, pred, (value, lang)
        return None
    except ValueError:
        return None


def build_side_index(zip_path):
    print("Building indexes...")

    # 1. Place Map (The Critical "Backwards" Link)
    place_map = {}
    print("Step 1/4: Loading Place Map (Place -> Concept)...")
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_PlaceMap.nt"), 1):
        parsed = parse_nt(line)
        if not parsed: continue
        subj, pred, obj = parsed  # Subj=Concept, Obj=Place

        if pred.endswith("focus"):
            # Clean IDs (remove URI prefix)
            place_id = obj.split("/tgn/")[-1]
            concept_id = subj.split("/tgn/")[-1]
            place_map[place_id] = concept_id

        if i % 1_000_000 == 0: sys.stdout.write(f"\r  {i:,} triples"); sys.stdout.flush()
    print(f"\n  ✓ Mapped {len(place_map):,} places to concepts")

    # 2. Coordinates
    coordinates = {}
    print("Step 2/4: Loading Coordinates...")
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

    # Filter incomplete coordinates
    coordinates = {k: tuple(v) for k, v in coordinates.items() if None not in v}
    print(f"\n  ✓ Loaded {len(coordinates):,} coords")

    # 3. Term Definitions
    term_literals = {}
    print("Step 3/4: Loading Term Definitions...")
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_2Terms.nt"), 1):
        parsed = parse_nt(line)
        if not parsed: continue
        subj, pred, obj = parsed
        if pred.endswith("literalForm"):
            if not isinstance(obj, tuple): obj = (obj, "")
            term_literals[subj] = obj
        if i % 1_000_000 == 0: sys.stdout.write(f"\r  {i:,} triples"); sys.stdout.flush()
    print(f"\n  ✓ {len(term_literals):,} terms")

    # 4. Subject Links (Concept -> Term)
    place_terms = defaultdict(list)
    place_pref = {}
    print("Step 4/4: Loading Concept-Term Links...")
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_1Subjects.nt"), 1):
        parsed = parse_nt(line)
        if not parsed: continue
        subj, pred, obj = parsed  # Subj=Concept, Obj=TermURI
        if isinstance(obj, tuple): obj = obj[0]

        tgn_id = subj.split("/tgn/")[-1]

        # Capture standard and GVP preferred labels
        if "prefLabel" in pred or "Label" in pred:
            if pred.endswith("prefLabelGVP") or pred.endswith("prefLabel"):
                place_pref[tgn_id] = obj
            place_terms[tgn_id].append(obj)

        if i % 1_000_000 == 0: sys.stdout.write(f"\r  {i:,} triples"); sys.stdout.flush()
    print(f"\n  ✓ Linked terms for {len(place_terms):,} concepts")

    return coordinates, place_map, term_literals, place_pref, place_terms


def index_tgn(zip_path, places_index):
    print("=" * 60)
    print(f"INDEXING TGN (Final Arch)")
    print("=" * 60)

    coordinates, place_map, term_literals, place_pref, place_terms = build_side_index(zip_path)

    # --- SANITY CHECK START ---
    print("\n🔎 RUNNING SANITY CHECK (First 5 records)...")
    sample_coords = list(coordinates.keys())[:5]
    failures = 0
    for uri in sample_coords:
        raw_place_id = uri.split("/tgn/")[-1]

        if raw_place_id in place_map:
            concept_id = place_map[raw_place_id]
            status = "✅ MAP HIT"
        else:
            concept_id = raw_place_id.replace("-place", "")
            status = "⚠️ FALLBACK"

        terms = place_terms.get(concept_id, [])
        term_count = len(terms)

        print(f"   [{status}] {raw_place_id} -> Concept: {concept_id} -> Terms: {term_count}")
        if term_count == 0:
            failures += 1

    if failures == 5:
        print("\n❌ CRITICAL: First 5 records failed to resolve terms.")
        print("   Aborting to prevent empty index. Check map keys or subject IDs.")
        sys.exit(1)
    print("   Sanity Check Passed. Starting Ingestion.\n")
    # --- SANITY CHECK END ---

    batch = []
    count = 0
    start_time = time.time()
    total = len(coordinates)
    processed = 0

    for place_uri, (lat, lon) in coordinates.items():
        processed += 1

        # 1. Get the Coordinate ID (e.g., "2742337-place")
        raw_place_id = place_uri.split("/tgn/")[-1]

        # 2. RESOLVE to Concept ID using the Map
        if raw_place_id in place_map:
            tgn_id = place_map[raw_place_id]
        else:
            # Fallback for IDs that aren't in the map (usually safe simple records)
            tgn_id = raw_place_id.replace("-place", "")

        # 3. Get Terms for the Concept ID
        term_uris = set(place_terms.get(tgn_id, []))
        if place_pref.get(tgn_id): term_uris.add(place_pref[tgn_id])

        toponyms = []
        seen_ids = set()

        for term_uri in term_uris:
            literal_data = term_literals.get(term_uri)
            if not literal_data: continue
            name, lang = literal_data

            # Format REQUIRED by pipeline: "Name@lang"
            toponym_id = f"{name}@{lang}"
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
                sys.stdout.write(f"\rIndexed {count:,} ({(processed / total) * 100:.1f}%)")
                sys.stdout.flush()

    if batch:
        helpers.bulk(es, batch, stats_only=True, raise_on_error=False)
        count += len(batch)

    print(f"\n\n✓ DONE: {count:,} places in {(time.time() - start_time) / 60:.1f} min")
    create_checkpoint_snapshot(es, "tgn_places_fixed")


if __name__ == "__main__":
    SOURCE_FILE = f"{DATA_DIR}/authorities/tgn/explicit.zip"
    PLACES_INDEX = "places"

    # Slurm Scratch Logic
    scratch_dir = os.environ.get("TMPDIR")
    if scratch_dir and os.path.isdir(scratch_dir):
        print(f"🚀 SLURM DETECTED: Copying to {scratch_dir}")
        target_path = os.path.join(scratch_dir, "tgn_final.zip")
        try:
            shutil.copy2(SOURCE_FILE, target_path)
            SOURCE_FILE = target_path
            print("   ✅ Copy successful.")
        except Exception as e:
            print(f"   ⚠️ Copy failed ({e}). Using network path.")

    if not Path(SOURCE_FILE).exists(): sys.exit(1)

    try:
        index_tgn(SOURCE_FILE, PLACES_INDEX)
    finally:
        if scratch_dir and SOURCE_FILE.startswith(scratch_dir) and os.path.exists(SOURCE_FILE):
            os.remove(SOURCE_FILE)