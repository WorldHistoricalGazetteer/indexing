# processing/osm_way_area_geometry.py

"""
OSM/OHM **way geometry** completion (place#145).

Counterpart to ``osm_boundary_geometry.py`` (which handles relation
multipolygons). ``authorities/osm-places.py`` / ``ohm-places.py`` build every
way with ``create_linestring`` — so a *closed, area-tagged* way (a lake, park,
building, island) was indexed as a line — and the 2026-05-04 geom-store loss
then dropped all way geometry (``has_geom`` cleared to ``false``). This pass
restores it in place, without an ES reindex, in two stages:

* **Area pass** — ``Area`` objects with ``from_way() == True`` (a single closed
  way osmium's assembler promoted to an area per the OSM area rules; we do not
  re-implement those rules) → real **polygons**, correcting the ways that should
  never have been lines.
* **Linear pass** (opt-in ``include_linear``) — every other named,
  gate-passing way → a **LineString**, faithful to the original handler. The
  area pass records the way ids it polygonised so a closed area-way is never
  also written as a line.

Both stages:
  * key the geom store / place_id as ``{ns}:w{way_id}``;
  * gate ways with a **source-specific** key set (``keys_for`` — OHM's is broader
    than OSM's; using the wrong one silently drops indexed ways);
  * are **update-only, never upsert** — a way not already indexed as a place
    404s and is skipped, so the pass can only upgrade existing docs.

**Where it runs.** CRC compute nodes cannot reach prod ES, so ``run`` does the
heavy PBF pass on Slurm — geometry to the ``/vast`` geom store, ES ops to a
``--export`` JSONL patch — and ``apply`` applies that patch to prod from the
Pitt VM (throttled scripted ``_bulk``; never ``_update_by_query``, which would
re-run the ``extract_namespace`` pipeline and rewrite toponym labels).
"""

from __future__ import annotations

import argparse
import gc
import glob
import json
import os
import signal
import sys
import time

from elasticsearch import Elasticsearch, helpers
from shapely.geometry import mapping
from shapely.validation import make_valid

from processing.helpers import enrich_geometry, geom_class_of  # noqa: F401
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


# The place-eligibility gates are SOURCE-SPECIFIC and must match the authority
# script exactly, or the pass rebuilds geometry for the wrong set of ways:
#   * osm-places.py  → name + one of these 7 keys.
#   * ohm-places.py  → name + one of a broader 13-key set (adds amenity,
#     man_made, military, building, leisure, tourism). Using the OSM set for OHM
#     silently drops ~57k indexed ways (place#145 OHM canary, verified 2026-07-24).
_OSM_KEYS = ("place", "natural", "water", "waterway", "historic", "landuse", "boundary")
_OHM_KEYS = ("place", "historic", "boundary", "natural", "water", "waterway",
             "landuse", "amenity", "man_made", "military", "building", "leisure", "tourism")


def keys_for(namespace):
    return _OHM_KEYS if namespace == "ohm" else _OSM_KEYS


# geom_class_of lives in processing.helpers (single source — also used by
# enrich_geometry); imported at module top and used by the way passes below.


def _finalize_geom_entry(geom_entry, raw_geom):
    """Bring a ``geom_entry`` to the canonical ES doc shape, in place.

    Two corrections over the raw ``enrich_geometry`` output:

    * **h3 nested, not top-level.** The schema and the gateway containment path
      read ``geometries[].h3_cover`` / ``.h3_centroid`` (nested). We set them on
      the entry itself — computed BEFORE stripping hull, since
      ``select_h3_cover_geometry`` prefers the hull for a cheaper polyfill.
    * **hull stripped.** ``hull`` is an ingestion intermediate deliberately kept
      OUT of ES (removed 2026-07-11, ``staged_parquet.strip_hull``); the way
      pass bypasses that stage, so drop it explicitly here.
    """
    h3_fields = build_h3_fields_for_geom_entry(geom_entry, raw_geom)
    if h3_fields:
        geom_entry["h3_centroid"] = h3_fields["h3_centroid"]
        geom_entry["h3_cover"] = h3_fields["h3_cover"]
    geom_entry.pop("hull", None)
    return geom_entry


def process_way_tags(tags, keys):
    """Tag gate for a named way — mirrors the authority script's process_tags.

    ``keys`` is the source-specific accepted key set (see ``keys_for``). We do
    not decide *whether* a way is an area (osmium's assembler already did, by
    emitting it as an ``Area``); we only reproduce the place-eligibility gate so
    the ways we rebuild match the indexed ``{ns}:w*`` set. Returns None to skip.
    """
    if "name" not in tags:
        return None
    if not any(k in tags for k in keys):
        return None
    result = {"name": tags["name"]}
    for tag in tags:
        if tag.k in ("start_date", "end_date"):
            result[tag.k] = tag.v
    return result


class WayAreaPassProcessor:
    """Assemble polygons from closed area-tagged ways and emit place updates."""

    def __init__(self, buffer_callback, namespace, done_ids=None):
        osmium = _require_osmium()
        self.osmium = osmium
        self.buffer_callback = buffer_callback
        self.namespace = namespace
        self.keys = keys_for(namespace)
        # Way ids that received a polygon — the linear pass skips these so a
        # closed area-way is never also written as a linestring.
        self.done_ids = done_ids if done_ids is not None else set()
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

        tags = process_way_tags(area.tags, self.keys)
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

            # Match the canonical staged-doc shape: enrich_geometry sets neither
            # geometry_index nor geom_ref (the staged pipeline does), but relations
            # and properly-ingested docs carry them, and tile/ccode consumers read
            # geom_ref. Set them so the patched doc is shape-identical.
            geom_entry["geometry_index"] = 0
            geom_entry["geom_ref"] = f"{place_id}_0"
            geom_entry["geom_class"] = geom_class_of(raw_geom)  # "area" (shape, not storage)
            _finalize_geom_entry(geom_entry, raw_geom)

            # Replace the geometry entry outright — the way's single geometry IS
            # this polygon; the point entry left behind by the linestring collapse
            # is exactly what we are correcting.
            update_doc = {"geometries": [geom_entry]}

            self.buffer_callback(place_id, update_doc)
            self.done_ids.add(way_id)
            self.extracted += 1

        except Exception as exc:
            self.geom_errors += 1
            if self.geom_errors <= 5:
                print(f"\n  Geometry error (way {area.orig_id()}): {exc}")


class WayLinearPassProcessor:
    """Rebuild LineStrings for named, gate-passing ways that are NOT areas.

    Faithful to the original ``osm-places`` handler (``create_linestring``) —
    rivers, streets, coastlines, closed non-area loops. ``done_ids`` (populated
    by the area pass) is skipped so an area-way keeps its polygon.
    """

    def __init__(self, buffer_callback, namespace, done_ids):
        osmium = _require_osmium()
        self.osmium = osmium
        self.buffer_callback = buffer_callback
        self.namespace = namespace
        self.keys = keys_for(namespace)
        self.done_ids = done_ids
        self.wkbfab = osmium.geom.WKBFactory()
        self.ways_seen = 0
        self.tag_rejected = 0
        self.skipped_area = 0
        self.extracted = 0
        self.geom_errors = 0
        self.areas_seen = 0  # for the shared _ProgressReporter
        self.start_time = time.time()

    def process_way(self, way):
        wkblib = _require_wkblib()
        self.ways_seen += 1
        self.areas_seen = self.ways_seen

        if way.id in self.done_ids:
            self.skipped_area += 1
            return
        tags = process_way_tags(way.tags, self.keys)
        if not tags:
            self.tag_rejected += 1
            return

        try:
            geom = wkblib.loads(self.wkbfab.create_linestring(way), hex=False)
            if geom.is_empty:
                return
            place_id = f"{self.namespace}:w{way.id}"
            timespans = build_timespans(tags)
            raw_geom = mapping(geom)
            geom_entry = enrich_geometry(
                raw_geom, timespans=timespans or None, geom_key=f"{place_id}_0")
            if not geom_entry or not geom_entry.get("has_geom"):
                self.geom_errors += 1
                return
            geom_entry["geometry_index"] = 0
            geom_entry["geom_ref"] = f"{place_id}_0"
            geom_entry["geom_class"] = geom_class_of(raw_geom)  # "line" (shape, not storage)
            _finalize_geom_entry(geom_entry, raw_geom)
            update_doc = {"geometries": [geom_entry]}
            self.buffer_callback(place_id, update_doc)
            self.extracted += 1
        except Exception as exc:
            self.geom_errors += 1
            if self.geom_errors <= 5:
                print(f"\n  Linestring error (way {way.id}): {exc}")


def run_way_area_pass(pbf_file, namespace, places_index="places",
                      es_host=None, dry_run=False, limit=None, sample_out=None,
                      geom_staging_dir=None, include_linear=False, export_path=None):
    """Restore way geometry and update ``{ns}:w*`` docs in place.

    Runs an **area pass** (closed area-tagged ways → polygons) and, when
    ``include_linear`` is set, a following **linear pass** (all other named
    gate-passing ways → LineStrings, faithful to the original ``osm-places``
    handler). The area pass records the way ids it polygonised so the linear
    pass never re-writes a closed area-way as a line.

    ``geom_staging_dir`` is where geometry is written (a ``GeomStoreWriter``
    staging file ``{ns}_wa.bin`` to be consolidated into the main store
    afterwards). Without it ``enrich_geometry`` cannot persist geometry, so
    ``has_geom`` never sets — the tool therefore requires it.

    **Output mode** (mutually exclusive):

    * ``export_path`` — write the ES update ops as JSONL (``{place_id, doc}``)
      instead of talking to ES. This is the Slurm path: CRC compute nodes cannot
      reach prod ES, so the heavy PBF pass runs here (geometry → ``/vast`` store,
      ops → patch file) and ``apply_patch`` applies the patch on the Pitt VM.
    * ``dry_run`` — build + persist geometry but emit nothing (prototyping).
    * neither — write directly to ES (only usable where ES is reachable, e.g.
      a staging ES on the same node).

    ``limit`` caps objects processed per pass.
    """
    from processing.geom_store import GeomStoreWriter, configure_module_writer

    osmium = _require_osmium()
    es = None
    if not dry_run and not export_path:
        es = Elasticsearch(es_host or ES_HOST, request_timeout=180,
                           max_retries=10, retry_on_timeout=True)
    export_fh = open(export_path, "w") if export_path else None

    processor = None

    def signal_handler(sig, frame):
        print("\n!!! SIGNAL RECEIVED !!!")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    buffer_list = []
    updated = failed = missing = exported = 0
    missing_samples = []
    built_ids = [] if sample_out else None

    def flush_buffer():
        nonlocal updated, failed, missing
        if not buffer_list or dry_run or export_fh is not None:
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
        nonlocal exported
        if built_ids is not None:
            built_ids.append(place_id)
        if export_fh is not None:
            export_fh.write(json.dumps({"place_id": place_id, "doc": update_doc}) + "\n")
            exported += 1
            return
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
        raise ValueError("geom_staging_dir is required — geometry must be "
                         "persisted for has_geom to set")

    done_ids: set = set()
    area_proc = WayAreaPassProcessor(add_to_buffer, namespace, done_ids=done_ids)
    lin_proc = None

    with GeomStoreWriter(geom_staging_dir, f"{namespace}_wa") as gsw:
        configure_module_writer(gsw)
        try:
            # Pass 1 — area assembly (from_way areas → polygons).
            print("Pass 1/2: area assembly (closed area-tagged ways → polygons)"
                  if include_linear else "Area assembly (closed area-tagged ways → polygons)")
            fp = osmium.FileProcessor(str(pbf_file)).with_locations(idx_type).with_areas()
            with _ProgressReporter(area_proc, interval=30):
                for obj in fp:
                    if isinstance(obj, osmium.osm.Area) and obj.from_way():
                        area_proc.process_area(obj)
                        if limit and area_proc.areas_seen >= limit:
                            break
            flush_buffer()

            # Pass 2 — linestrings for the remaining named gate-passing ways.
            if include_linear:
                print(f"\nPass 2/2: linestrings (named non-area ways; "
                      f"{len(done_ids):,} area ways will be skipped)")
                lin_proc = WayLinearPassProcessor(add_to_buffer, namespace, done_ids)
                fp2 = osmium.FileProcessor(str(pbf_file)).with_locations(idx_type)
                with _ProgressReporter(lin_proc, interval=30):
                    for obj in fp2:
                        if isinstance(obj, osmium.osm.Way):
                            lin_proc.process_way(obj)
                            if limit and lin_proc.ways_seen >= limit:
                                break
                flush_buffer()
        finally:
            configure_module_writer(None)
            if export_fh is not None:
                export_fh.close()

    if sample_out and built_ids is not None:
        with open(sample_out, "w") as fh:
            fh.write("\n".join(built_ids) + ("\n" if built_ids else ""))
        print(f"  Wrote {len(built_ids):,} built place_ids -> {sample_out}")

    built_total = area_proc.extracted + (lin_proc.extracted if lin_proc else 0)
    print(f"\n\n{src} way geometry pass complete:")
    print(f"  [area]  way-areas seen:   {area_proc.areas_seen:,}")
    print(f"  [area]  polygons built:   {area_proc.extracted:,}")
    print(f"  [area]  geom errors:      {area_proc.geom_errors:,}")
    print(f"  [area]  raw-geom repaired:{area_proc.raw_geom_fixed:,}")
    print(f"  [area]  antimeridian:     {area_proc.antimeridian_split:,}")
    if lin_proc:
        print(f"  [line]  ways seen:        {lin_proc.ways_seen:,}")
        print(f"  [line]  skipped (area):   {lin_proc.skipped_area:,}")
        print(f"  [line]  tag-rejected:     {lin_proc.tag_rejected:,}")
        print(f"  [line]  linestrings built:{lin_proc.extracted:,}")
        print(f"  [line]  geom errors:      {lin_proc.geom_errors:,}")
    if export_fh is not None:
        print(f"  Ops exported:         {exported:,} -> {export_path}")
    elif not dry_run:
        print(f"  Docs updated:         {updated:,}")
        print(f"  Docs missing (404):   {missing:,} (geom built but not indexed as a place)")
        print(f"  Docs failed:          {failed:,}")
        if missing_samples:
            print(f"  Missing samples:      {', '.join(missing_samples)}")
    if export_fh is not None:
        return exported
    return built_total if dry_run else updated


def apply_patch(patch_globs, es_host="http://localhost:9201",
                es_password_file="/ix1/ishi/es/config/elastic.password",
                places_index="places", rps=1000, batch=500):
    """Apply an exported way-geometry patch to ES via throttled scripted _bulk.

    Runs on the Pitt VM against prod (localhost:9201). Uses ``update`` ops (never
    ``_update_by_query``) so the ``extract_namespace`` default_pipeline does NOT
    re-run — a partial ``doc`` update replaces ``geometries`` and sets the h3
    fields, leaving title/toponyms/types/ccodes untouched. Update-only (no
    upsert): a place_id that no longer exists 404s and is skipped. Idempotent.
    """
    import time as _time
    import requests

    with open(es_password_file) as fh:
        auth = ("elastic", fh.read().strip())
    sess = requests.Session()
    sess.auth = auth
    sess.headers.update({"Content-Type": "application/x-ndjson"})
    idx = next(iter(sess.get(f"{es_host}/_alias/{places_index}").json().keys()))
    print(f"apply target: {idx}  (rps≈{rps})", flush=True)

    paths = []
    for g in (patch_globs if isinstance(patch_globs, (list, tuple)) else [patch_globs]):
        paths.extend(sorted(glob.glob(g)))
    sent = ok = failed = missing = 0
    lines: list[str] = []
    t0 = _time.time()

    def flush():
        nonlocal sent, ok, failed, missing, lines
        if not lines:
            return
        resp = sess.post(f"{es_host}/{idx}/_bulk?refresh=false", data="\n".join(lines) + "\n")
        resp.raise_for_status()
        body = resp.json()
        n = len(lines) // 2
        sent += n
        if body.get("errors"):
            for item in body.get("items", []):
                r = item.get("update", {})
                st = r.get("status", 200)
                if st >= 300:
                    failed += 1
                    if st == 404:
                        missing += 1
                    elif failed <= 5:
                        print("  err:", json.dumps(r)[:200], file=sys.stderr)
                else:
                    ok += 1
        else:
            ok += n
        lines = []
        elapsed = _time.time() - t0
        target = sent / max(rps, 1)
        if target > elapsed:
            _time.sleep(target - elapsed)
        if sent % (batch * 20) == 0:
            print(f"  sent={sent:,} ok={ok:,} missing404={missing:,} failed={failed:,} "
                  f"rate={sent/max(_time.time()-t0,1e-6):.0f}/s", flush=True)

    for path in paths:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                pid, doc = rec.get("place_id"), rec.get("doc")
                if not pid or not doc:
                    continue
                lines.append(json.dumps({"update": {"_id": pid}}))
                lines.append(json.dumps({"doc": doc}))
                if len(lines) >= batch * 2:
                    flush()
    flush()
    print(f"APPLY DONE sent={sent:,} ok={ok:,} missing404={missing:,} failed={failed:,} "
          f"in {_time.time()-t0:.0f}s", flush=True)
    return 1 if (failed - missing) else 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="OSM/OHM way geometry pass: restore polygons for area-ways "
                    "(+ optional linestrings) and update {ns}:w* docs (place#145)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="PBF pass — geometry to the geom store, ES ops "
                                    "to --export (Slurm) or directly to ES")
    r.add_argument("--source", choices=["osm", "ohm"], required=True)
    r.add_argument("--file", required=True, help="PBF path (planet or a small extract)")
    r.add_argument("--es-host", default=None)
    r.add_argument("--places-index", default="places")
    r.add_argument("--geom-staging", required=True,
                   help="GeomStoreWriter staging dir for the assembled geometry")
    r.add_argument("--include-linear", action="store_true",
                   help="also rebuild LineStrings for named non-area ways")
    r.add_argument("--export", dest="export_path", default=None,
                   help="write ES update ops as JSONL here instead of writing to "
                        "ES (the Slurm path — apply on Pitt with the apply cmd)")
    r.add_argument("--dry-run", action="store_true",
                   help="build + persist geometry but emit no ES ops (prototyping)")
    r.add_argument("--limit", type=int, default=None, help="cap objects per pass")
    r.add_argument("--sample-out", default=None,
                   help="write every built place_id here (for match-rate checks)")

    a = sub.add_parser("apply", help="apply an exported patch to prod ES (run on Pitt)")
    a.add_argument("--patch", required=True, nargs="+", help="patch file(s)/glob(s)")
    a.add_argument("--es-host", default="http://localhost:9201")
    a.add_argument("--places-index", default="places")
    a.add_argument("--rps", type=int, default=1000)
    a.add_argument("--batch", type=int, default=500)

    args = p.parse_args(argv)
    if args.cmd == "apply":
        sys.exit(apply_patch(args.patch, es_host=args.es_host,
                             places_index=args.places_index, rps=args.rps,
                             batch=args.batch))
    run_way_area_pass(args.file, args.source, places_index=args.places_index,
                      es_host=args.es_host, dry_run=args.dry_run, limit=args.limit,
                      sample_out=args.sample_out, geom_staging_dir=args.geom_staging,
                      include_linear=args.include_linear, export_path=args.export_path)


if __name__ == "__main__":
    main()
