# processing/osm_way_area_geometry.py

"""
OSM/OHM **way-area** geometry completion (place#145).

Counterpart to ``osm_boundary_geometry.py``. The way handler in
``authorities/osm-places.py`` builds every way with ``create_linestring`` — so a
*closed, area-tagged* way (a lake, park, building, island) was indexed as a line,
and after the 2026-05-04 geom-store loss those geometries are gone entirely
(``has_geom`` was cleared to ``false`` for ~10.5M ways). This pass restores the
**area** ones as real polygons, in place, without an ES reindex.

It mirrors ``run_boundary_pass`` exactly, with three differences:

1. It consumes ``Area`` objects with ``from_way() == True`` (a single closed way
   promoted to an area) rather than ``not from_way()`` (relation multipolygons).
   osmium's area assembler already applies the OSM area rules — a closed way is
   only emitted as an ``Area`` when its tags imply an area (``natural``,
   ``landuse``, ``leisure``, ``building``, ``water``, ``place``, … and not
   ``area=no``); linear ways (``highway``, ``waterway=river``) are never emitted
   as areas, so they are correctly left as-is. We do not re-implement those
   rules.

2. The geom-store key and place_id use the ``w`` prefix: ``{ns}:w{way_id}``.

3. **Update-only, never upsert.** The op carries no ``upsert`` body, so a way
   that was never indexed as a place 404s and is skipped — the pass can only
   upgrade the geometry of docs that already exist, never create one.

Linear named ways (rivers, streets) are a *separate* concern: their linestrings
were lost in the same accident, but a line is not a containment region and this
pass deliberately does not touch them — see the module CLI ``--help`` and the
place#145 discussion.
"""

from __future__ import annotations

import argparse
import gc
import os
import signal
import sys
import time

from elasticsearch import Elasticsearch, helpers
from shapely.geometry import mapping
from shapely.validation import make_valid

from processing.helpers import enrich_geometry
from processing.settings import ES_HOST
from processing.osm_boundary_geometry import (
    BULK_THREAD_COUNT,
    QUEUE_SIZE,
    _ProgressReporter,
    _require_osmium,
    _require_wkblib,
    build_h3_fields_for_geom_entry,
    build_timespans,
    split_at_antimeridian,
)


# The exact gate ``authorities/osm-places.py:process_tags`` applies to decide a
# way is a place worth indexing: a name plus at least one of these keys. We
# replicate it so the pass only builds polygons for ways that already have a
# ``{ns}:w*`` doc — matching osmium's area classification against the place set,
# rather than assembling every named building/area in the planet.
_PLACE_TAG_KEYS = ("natural", "water", "waterway", "historic", "landuse", "boundary")


def process_way_tags(tags):
    """Tag gate for a named area-way — mirrors ``osm-places`` process_tags.

    We do not decide *whether* it is an area (osmium's assembler already did, by
    emitting it as an ``Area``); we only reproduce the place-eligibility gate so
    the key set matches the indexed docs, and pull the OHM temporal tags.
    Returns None to skip.
    """
    if "name" not in tags:
        return None
    if "place" not in tags and not any(k in tags for k in _PLACE_TAG_KEYS):
        return None
    result = {"name": tags["name"]}
    for tag in tags:
        if tag.k in ("start_date", "end_date"):
            result[tag.k] = tag.v
    return result


class WayAreaPassProcessor:
    """Assemble polygons from closed area-tagged ways and emit place updates."""

    def __init__(self, buffer_callback, namespace):
        osmium = _require_osmium()
        self.osmium = osmium
        self.buffer_callback = buffer_callback
        self.namespace = namespace
        self.wkbfab = osmium.geom.WKBFactory()
        self.areas_seen = 0
        self.tag_rejected = 0
        self.extracted = 0
        self.geom_errors = 0
        self.raw_geom_fixed = 0
        self.skipped_invalid = 0
        self.skipped_empty = 0
        self.antimeridian_split = 0
        self.start_time = time.time()

    def process_area(self, area):
        wkblib = _require_wkblib()
        self.areas_seen += 1

        tags = process_way_tags(area.tags)
        if not tags:
            self.tag_rejected += 1
            return

        try:
            wkb = self.wkbfab.create_multipolygon(area)
            geom = wkblib.loads(wkb, hex=False)

            if not geom.is_valid:
                fixed = make_valid(geom)
                if fixed.is_valid:
                    geom = fixed
                    self.raw_geom_fixed += 1
                else:
                    fixed = geom.buffer(0)
                    if fixed.is_valid:
                        geom = fixed
                        self.raw_geom_fixed += 1
                    else:
                        self.skipped_invalid += 1
                        return
            if geom.is_empty:
                self.skipped_empty += 1
                return

            split_geom = split_at_antimeridian(geom)
            if split_geom is not geom:
                self.antimeridian_split += 1
                geom = split_geom

            way_id = area.orig_id()
            place_id = f"{self.namespace}:w{way_id}"
            timespans = build_timespans(tags)
            raw_geom = mapping(geom)
            geom_entry = enrich_geometry(
                raw_geom,
                timespans=timespans or None,
                geom_key=f"{place_id}_0",
            )
            if not geom_entry or not geom_entry.get("has_geom"):
                # enrich_geometry only sets has_geom when the polygon was
                # actually written to the geom store; anything else is a no-op
                # for our purpose (we are here to add a retrievable polygon).
                self.geom_errors += 1
                return

            # Replace the geometry entry outright — the way's single geometry IS
            # this polygon; the point entry left behind by the linestring collapse
            # is exactly what we are correcting.
            update_doc = {"geometries": [geom_entry]}
            h3_fields = build_h3_fields_for_geom_entry(geom_entry, raw_geom)
            if h3_fields:
                update_doc.update(h3_fields)

            self.buffer_callback(place_id, update_doc)
            self.extracted += 1

        except Exception as exc:
            self.geom_errors += 1
            if self.geom_errors <= 5:
                print(f"\n  Geometry error (way {area.orig_id()}): {exc}")


def run_way_area_pass(pbf_file, namespace, places_index="places",
                      es_host=None, dry_run=False, limit=None, sample_out=None,
                      geom_staging_dir=None):
    """Assemble way-area polygons and update ``{ns}:w*`` docs in place.

    ``geom_staging_dir`` is where the polygons are written (a
    ``GeomStoreWriter`` staging file ``{ns}_wa.bin`` to be consolidated into the
    main store afterwards). Without it ``enrich_geometry`` cannot persist a
    polygon, so ``has_geom`` never sets and nothing is emitted — the tool
    therefore requires it.

    ``dry_run`` builds + persists every polygon (validating assembly and the
    geom-store write) but performs NO Elasticsearch writes — for characterising
    a run on a small extract before touching prod. ``limit`` caps areas.
    """
    from processing.geom_store import GeomStoreWriter, configure_module_writer

    osmium = _require_osmium()
    es = None
    if not dry_run:
        es = Elasticsearch(es_host or ES_HOST, request_timeout=180,
                           max_retries=10, retry_on_timeout=True)

    processor = None

    def signal_handler(sig, frame):
        print("\n!!! SIGNAL RECEIVED !!!")
        if processor:
            print(f"  Extracted so far: {processor.extracted:,}")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    buffer_list = []
    updated = failed = missing = 0
    missing_samples = []
    built_ids = [] if sample_out else None

    def flush_buffer():
        nonlocal updated, failed, missing
        if not buffer_list or dry_run:
            buffer_list.clear()
            return
        for success, info in helpers.parallel_bulk(
            es, buffer_list, thread_count=BULK_THREAD_COUNT,
            queue_size=QUEUE_SIZE, raise_on_error=False,
        ):
            if success:
                updated += 1
            else:
                failed += 1
                op = next(iter(info.values())) if info else {}
                err = op.get("error", {}) if isinstance(op, dict) else {}
                if (isinstance(err, dict) and op.get("status") == 404
                        and err.get("type") == "document_missing_exception"):
                    missing += 1
                    if len(missing_samples) < 10:
                        missing_samples.append(op.get("_id", "?"))
                elif failed <= 10:
                    print(f"\n  Bulk update error: {info}")
        buffer_list.clear()
        if updated and updated % 5000 == 0:
            gc.collect()

    def add_to_buffer(place_id, update_doc):
        if built_ids is not None:
            built_ids.append(place_id)
        # Update-only: NO 'upsert' key → a way never indexed as a place 404s and
        # is counted, never created.
        buffer_list.append({
            "_op_type": "update",
            "_index": places_index,
            "_id": place_id,
            "doc": update_doc,
        })
        if len(buffer_list) >= 500:
            flush_buffer()

    src = "OSM" if namespace == "osm" else "OHM"
    print(f"\n{'=' * 80}\n{src} WAY-AREA PASS (polygon assembly + in-place update)"
          f"{'  [DRY RUN — no ES writes]' if dry_run else ''}\n{'=' * 80}")
    print(f"Source: {pbf_file}\nTarget: {places_index}\nNamespace: {namespace}\n")

    scratch = os.environ.get("SLURM_SCRATCH") or os.environ.get("TMPDIR")
    if scratch and os.path.isdir(scratch):
        idx_type = f"dense_file_array,{os.path.join(scratch, f'nodes_wa_{namespace}.idx')}"
    else:
        idx_type = "flex_mem"
    print(f"Node index: {idx_type}")

    if not geom_staging_dir:
        raise ValueError("geom_staging_dir is required — polygons must be "
                         "persisted for has_geom to set")

    processor = WayAreaPassProcessor(add_to_buffer, namespace)
    fp = osmium.FileProcessor(str(pbf_file)).with_locations(idx_type).with_areas()

    with GeomStoreWriter(geom_staging_dir, f"{namespace}_wa") as gsw:
        configure_module_writer(gsw)
        try:
            with _ProgressReporter(processor, interval=30):
                for obj in fp:
                    if isinstance(obj, osmium.osm.Area) and obj.from_way():
                        processor.process_area(obj)
                        if limit and processor.areas_seen >= limit:
                            break
            flush_buffer()
        finally:
            configure_module_writer(None)

    if sample_out and built_ids is not None:
        with open(sample_out, "w") as fh:
            fh.write("\n".join(built_ids) + ("\n" if built_ids else ""))
        print(f"  Wrote {len(built_ids):,} built place_ids -> {sample_out}")

    print(f"\n\n{src} way-area pass complete:")
    print(f"  Way-areas seen:       {processor.areas_seen:,}")
    print(f"  Tag-filter rejected:  {processor.tag_rejected:,} (no name)")
    print(f"  Polygons built:       {processor.extracted:,}")
    print(f"  Geometry errors:      {processor.geom_errors:,}")
    print(f"  Raw-geom repaired:    {processor.raw_geom_fixed:,}")
    print(f"  Skipped invalid:      {processor.skipped_invalid:,}")
    print(f"  Skipped empty:        {processor.skipped_empty:,}")
    print(f"  Antimeridian splits:  {processor.antimeridian_split:,}")
    if not dry_run:
        print(f"  Docs updated:         {updated:,}")
        print(f"  Docs missing (404):   {missing:,} (way built but not indexed as a place)")
        print(f"  Docs failed:          {failed:,}")
        if missing_samples:
            print(f"  Missing samples:      {', '.join(missing_samples)}")
    return processor.extracted if dry_run else updated


def main(argv=None):
    p = argparse.ArgumentParser(
        description="OSM/OHM way-area pass: assemble polygons for closed "
                    "area-tagged ways and update {ns}:w* docs in place (place#145)")
    p.add_argument("--source", choices=["osm", "ohm"], required=True)
    p.add_argument("--file", required=True, help="PBF path (full planet or a small extract)")
    p.add_argument("--es-host", default=None)
    p.add_argument("--places-index", default="places")
    p.add_argument("--dry-run", action="store_true",
                   help="build + validate geometry and write to the geom store, "
                        "but perform NO ES writes (for prototyping on a small extract)")
    p.add_argument("--limit", type=int, default=None, help="cap areas processed")
    p.add_argument("--sample-out", default=None,
                   help="write every built place_id here (for match-rate checks)")
    p.add_argument("--geom-staging", required=True,
                   help="GeomStoreWriter staging dir for the assembled polygons")
    args = p.parse_args(argv)

    run_way_area_pass(args.file, args.source, places_index=args.places_index,
                      es_host=args.es_host, dry_run=args.dry_run, limit=args.limit,
                      sample_out=args.sample_out, geom_staging_dir=args.geom_staging)


if __name__ == "__main__":
    main()
