#!/usr/bin/env python
"""
TGN Ingestion (Refined Production Version)
1. Inverted PlaceMap (Place -> Concept)
2. Strict Predicate Matching (prefLabel, altLabel only)
3. Semantic Title Selection (Uses prefLabel for title if available)
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
            candidates = [n for n in zf.namelist() if filename_in_zip.replace("1", "") in n]
            if candidates:
                filename_in_zip = candidates[0]
            else:
                return

        with zf.open(filename_in_zip, 'r') as f:
            for line in f:
                yield line.decode('utf-8')


def parse_nt(line):
    line = line.strip()
    if not line or line[0] != "<": return None
    try:
        s_end = line.index(">")
        subj = line[1:s_end]
        p_start = line.index("<", s_end)
        p_end = line.index(">", p_start)
        pred = line[p_start + 1:p_end]
        rest = line[p_end + 1:].strip()

        if rest.startswith("<"):
            return subj, pred, rest[1:rest.index(">")]

        if rest.startswith('"'):
            last_quote = rest.rindex('"')
            value = rest[1:last_quote]
            remaining = rest[last_quote + 1:].strip()
            lang = remaining[1:].split()[0].rstrip(".") if remaining.startswith("@") else ""
            return subj, pred, (value, lang)
        return None
    except ValueError:
        return None


def build_side_index(zip_path):
    print("Building indexes...")

    # 1. Place Map (Inverted)
    place_map = {}
    print("Step 1/4: Loading Place Map (Place -> Concept)...")
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_PlaceMap.nt"), 1):
        parsed = parse_nt(line)
        if not parsed: continue
        subj, pred, obj = parsed
        if pred.endswith("focus"):
            place_id = obj.split("/tgn/")[-1]
            concept_id = subj.split("/tgn/")[-1]
            place_map[place_id] = concept_id
        if i % 1_000_000 == 0: sys.stdout.write(f"\r  {i:,} triples"); sys.stdout.flush()
    print(f"\n  ✓ Mapped {len(place_map):,} places")

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
    coordinates = {k: tuple(v) for k, v in coordinates.items() if None not in v}
    print(f"\n  ✓ Loaded {len(coordinates):,} coords")

    # 3. Term Definitions
    term_literals = {}
    print("Step 3/4: Loading Term Definitions...")
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_2Terms.nt"), 1):
        parsed = parse_nt(line)
        if not parsed: continue
        subj, pred, obj = parsed
        if "literalForm" in pred:
            if not isinstance(obj, tuple): obj = (obj, "")
            term_literals[subj] = obj
        if i % 1_000_000 == 0: sys.stdout.write(f"\r  {i:,} triples"); sys.stdout.flush()
    print(f"\n  ✓ {len(term_literals):,} terms")

    # 4. Subject Links (Refined Predicate Matching)
    place_terms = defaultdict(list)
    place_pref = {}

    # Allowed predicates for linking Concepts to Label Nodes
    VALID_LABEL_PREDS = ("prefLabel", "altLabel", "prefLabelGVP")

    print("Step 4/4: Loading Concept-Term Links...")
    count_links = 0
    for i, line in enumerate(stream_nt(zip_path, "TGNOut_1Subjects.nt"), 1):
        parsed = parse_nt(line)
        if not parsed: continue
        subj, pred, obj = parsed
        if isinstance(obj, tuple): continue

        # DEBUG: Print first 20 predicates to see their actual format
        if i <= 20:
            print(f"  DEBUG pred: {pred}")

        # Strict check: Must be one of the known label predicates
        if pred.endswith(VALID_LABEL_PREDS):
            tgn_id = subj.split("/tgn/")[-1]

            # Store preferred label separately for title selection
            if pred.endswith("prefLabelGVP") or pred.endswith("prefLabel"):
                place_pref[tgn_id] = obj

            place_terms[tgn_id].append(obj)
            count_links += 1

        if i % 1_000_000 == 0: sys.stdout.write(f"\r  {i:,} triples"); sys.stdout.flush()

    print(f"\n  DEBUG: Sample term URIs from place_terms:")
    for concept_id, terms in list(place_terms.items())[:3]:
        print(f"    Concept {concept_id}: {terms[:2]}")

    print(f"\n  DEBUG: Sample term URIs from term_literals:")
    for uri in list(term_literals.keys())[:3]:
        print(f"    {uri}")

    print(f"\n  ✓ Found {count_links:,} links for {len(place_terms):,} concepts")
    return coordinates, place_map, term_literals, place_pref, place_terms


def index_tgn(zip_path, places_index):
    print("=" * 60)
    print(f"INDEXING TGN (Final Production)")
    print("=" * 60)

    coordinates, place_map, term_literals, place_pref, place_terms = build_side_index(zip_path)

    # --- SANITY CHECK ---
    print("\n🔎 RUNNING SANITY CHECK (First 5 records)...")
    sample_coords = list(coordinates.keys())[:5]
    failures = 0
    for uri in sample_coords:
        raw_place_id = uri.split("/tgn/")[-1]
        concept_id = place_map.get(raw_place_id, raw_place_id.replace("-place", ""))
        terms = place_terms.get(concept_id, [])

        # Check if resolved to literal
        resolved = 0
        first_name = "None"
        for t in terms:
            if t in term_literals:
                resolved += 1
                if first_name == "None": first_name = term_literals[t][0]

        print(f"   {raw_place_id} -> Concept:{concept_id} -> Names:{resolved} (e.g. {first_name})")
        if resolved == 0: failures += 1

    if failures == 5:
        print("\n❌ CRITICAL: First 5 records failed. Aborting.")
        sys.exit(1)
    print("   Sanity Check Passed. Starting Ingestion.\n")
    # ---------------------

    batch = []
    count = 0
    start_time = time.time()
    total = len(coordinates)
    processed = 0

    for place_uri, (lat, lon) in coordinates.items():
        processed += 1

        raw_place_id = place_uri.split("/tgn/")[-1]
        tgn_id = place_map.get(raw_place_id, raw_place_id.replace("-place", ""))

        # Get all terms
        term_uris = set(place_terms.get(tgn_id, []))

        toponyms = []
        seen_ids = set()

        # Build Toponyms List
        for term_uri in term_uris:
            literal_data = term_literals.get(term_uri)
            if not literal_data: continue
            name, lang = literal_data

            toponym_id = f"{name}@{lang}"
            if toponym_id in seen_ids: continue

            toponyms.append({
                "toponym_id": toponym_id,
                "timespans": [{"start": {"in": 2025}, "end": {"in": 2025}}]
            })
            seen_ids.add(toponym_id)

        # SEMANTIC TITLE SELECTION
        title = None
        # 1. Try Preferred Label first
        if tgn_id in place_pref:
            pref_uri = place_pref[tgn_id]
            if pref_uri in term_literals:
                title = term_literals[pref_uri][0]

        # 2. Fallback to first available toponym
        if not title and toponyms:
            title = toponyms[0]["toponym_id"].split("@")[0]

        # 3. Last Resort
        if not title:
            title = f"TGN {tgn_id}"

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

    scratch_dir = os.environ.get("TMPDIR")
    if scratch_dir and os.path.isdir(scratch_dir):
        print(f"🚀 SLURM DETECTED: Copying to {scratch_dir}")
        target_path = os.path.join(scratch_dir, "tgn_final.zip")
        try:
            shutil.copy2(SOURCE_FILE, target_path)
            SOURCE_FILE = target_path
        except Exception:
            pass

    if not Path(SOURCE_FILE).exists(): sys.exit(1)

    try:
        index_tgn(SOURCE_FILE, PLACES_INDEX)
    finally:
        if scratch_dir and SOURCE_FILE.startswith(scratch_dir) and os.path.exists(SOURCE_FILE):
            os.remove(SOURCE_FILE)