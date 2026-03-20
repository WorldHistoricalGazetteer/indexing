#!/usr/bin/env python3
# processing/augment_ccodes.py
"""
Augment places with ccodes (ISO 3166-1 alpha-2 country codes) by
spatially intersecting each place's geometry against full-resolution
Natural Earth country polygons.
Two-phase approach:
  Phase 1 - Shapely STRtree envelope overlap for O(log N) candidates.
  Phase 2 - Prepared-geometry ``intersects`` for exact test.
Country geometries are loaded at full resolution from the Natural Earth
10 m shapefile (downloaded once and cached).  The simplified copies in
the ES index are *not* used - they were simplified at ~1 km during
ingest, which would misclassify places near borders.
Designed to be run periodically without overloading Elasticsearch:
  * Scans only places that lack ccodes (or optionally all places).
  * Batches bulk updates with configurable chunk size and throttle delay.
  * Supports dry-run mode for testing.
Usage:
    python -m processing.augment_ccodes
    python -m processing.augment_ccodes --dry-run --limit 100
    python -m processing.augment_ccodes --namespace osm --batch-size 200 --throttle 1.0
    python -m processing.augment_ccodes --recompute-all
"""
from __future__ import annotations
import argparse
import logging
import sys
import time
import urllib.request
import zipfile
from collections import namedtuple
from pathlib import Path
from elasticsearch import Elasticsearch, helpers
from shapely.geometry import Point
from shapely.prepared import prep
from shapely import STRtree
from tqdm import tqdm
from processing.helpers import geojson_to_shapely
from processing.settings import ES_HOST, DATA_DIR, PLACES_INDEX
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
# Suppress per-request ES transport logging (clashes with tqdm bar)
logging.getLogger("elastic_transport.transport").setLevel(logging.WARNING)
CountryGeom = namedtuple("CountryGeom", ["iso_a2", "prepared", "geom"])
NATURAL_EARTH_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"
# ---------------------------------------------------------------------------
# 1. Load full-resolution country geometries from Natural Earth
# ---------------------------------------------------------------------------
def _download_natural_earth():
    """Download Natural Earth 10 m countries ZIP if not already cached."""
    ne_dir = Path(DATA_DIR) / "ISO3166"
    ne_dir.mkdir(parents=True, exist_ok=True)
    zip_path = ne_dir / "ne_10m_admin_0_countries.zip"
    if zip_path.exists():
        log.info("Natural Earth data already cached: %s", zip_path)
        return zip_path
    log.info("Downloading Natural Earth 10 m countries ...")
    urllib.request.urlretrieve(NATURAL_EARTH_URL, zip_path)
    log.info("Downloaded (%.1f MB)", zip_path.stat().st_size / 1024 / 1024)
    return zip_path
def _read_shapefile(zip_path):
    """Extract and read the shapefile from the Natural Earth ZIP."""
    try:
        import shapefile
    except ImportError:
        log.error("pyshp is required:  pip install pyshp")
        sys.exit(1)
    with zipfile.ZipFile(zip_path, "r") as zf:
        shp_files = [f for f in zf.namelist() if f.endswith(".shp")]
        if not shp_files:
            log.error("No .shp found in %s", zip_path)
            sys.exit(1)
        base_name = shp_files[0][:-4]
        temp_dir = Path(DATA_DIR) / "ISO3166" / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            fname = base_name + ext
            if fname in zf.namelist():
                zf.extract(fname, temp_dir)
        sf = shapefile.Reader(str(temp_dir / shp_files[0]))
        features = []
        for rec in sf.shapeRecords():
            features.append({
                "geometry": rec.shape.__geo_interface__,
                "properties": rec.record.as_dict(),
            })
    log.info("Read %d country features from shapefile", len(features))
    return features
def load_country_geometries(download=True):
    """
    Load Natural Earth 10 m country polygons at **full resolution**.
    Each feature's ``ISO_A2`` property is used as the country code.
    A Shapely prepared geometry is built for fast repeated intersection.
    """
    if download:
        zip_path = _download_natural_earth()
    else:
        zip_path = Path(DATA_DIR) / "ISO3166" / "ne_10m_admin_0_countries.zip"
        if not zip_path.exists():
            log.error("Natural Earth data not found at %s -- run without --no-download", zip_path)
            sys.exit(1)
    features = _read_shapefile(zip_path)
    countries = []
    skipped = []
    for feat in features:
        props = feat["properties"]
        iso_a2 = props.get("ISO_A2", "")
        if not iso_a2 or iso_a2 == "-99":
            iso_a2 = props.get("ISO_A2_EH", "")
        if not iso_a2 or iso_a2 == "-99":
            skipped.append(props.get("NAME", "?"))
            continue
        geom = geojson_to_shapely(feat["geometry"])
        if geom is None or geom.is_empty:
            continue
        countries.append(CountryGeom(
            iso_a2=iso_a2,
            prepared=prep(geom),
            geom=geom,
        ))
    log.info("Loaded %d country geometries with valid ISO_A2 codes", len(countries))
    if skipped:
        log.info("Skipped %d features with no usable ISO_A2: %s",
                 len(skipped), ", ".join(skipped))
    return countries
def build_spatial_index(countries):
    """
    Build a Shapely STRtree for O(log N) candidate lookup.
    Returns (tree, countries) -- the tree indexes geometries in the same
    order as *countries*, so ``tree.query`` indices map directly.
    """
    geoms = [c.geom for c in countries]
    tree = STRtree(geoms)
    return tree, countries
# ---------------------------------------------------------------------------
# 2. Scan places missing ccodes
# ---------------------------------------------------------------------------
def _build_scan_query(namespace, recompute_all):
    """
    Build the ES query for the scan.
    By default selects places where ``ccodes`` does not exist.
    If *recompute_all* is True, selects all places (still excluding un: docs).
    """
    filters = []
    must_not = [
        # Never reprocess the country documents themselves
        {"prefix": {"place_id": "un:"}},
    ]
    if not recompute_all:
        must_not.append({"exists": {"field": "ccodes"}})
    if namespace:
        filters.append({"prefix": {"place_id": "%s:" % namespace}})
    query = {
        "bool": {
            "must_not": must_not,
        }
    }
    if filters:
        query["bool"]["filter"] = filters
    return {
        "query": query,
        "_source": ["place_id", "ccodes", "geometries.geom", "geometries.repr_point"],
    }
def scan_places(es, namespace, recompute_all):
    """
    Yield place hits that need ccodes augmentation.
    Uses ``elasticsearch.helpers.scan`` with a 30-minute scroll window.
    """
    body = _build_scan_query(namespace, recompute_all)
    log.info("Scan query: %s", body["query"])
    return helpers.scan(
        es,
        index=PLACES_INDEX,
        query=body,
        scroll="30m",
        size=1000,
        request_timeout=180,
    )
# ---------------------------------------------------------------------------
# 3. Two-phase spatial matching
# ---------------------------------------------------------------------------
def _extract_shapely_geom(hit_source):
    """
    Extract a single Shapely geometry from a place document.
    Tries each entry in the ``geometries`` nested array; returns the first
    valid one.  Falls back to ``repr_point`` if ``geom`` cannot be parsed.
    """
    for gentry in hit_source.get("geometries") or []:
        geojson = gentry.get("geom")
        if geojson:
            g = geojson_to_shapely(geojson)
            if g and not g.is_empty:
                return g
        rp = gentry.get("repr_point")
        if rp:
            try:
                return Point(float(rp["lon"]), float(rp["lat"]))
            except (KeyError, TypeError, ValueError):
                pass
    return None
def match_countries(place_geom, tree, countries, snap_degrees=0.1):
    """
    Return sorted list of ISO_A2 codes whose geometries intersect *place_geom*.
    Phase 1: STRtree.query returns candidate indices via R-tree envelope overlap.
    Phase 2: prepared geometry ``intersects`` for exact test.
    Phase 3 (fallback): if no direct intersection, find the nearest country
             within *snap_degrees* (~11 km at the equator).  This catches
             coastal points that fall just outside Natural Earth polygon edges.
    """
    candidate_indices = tree.query(place_geom)
    codes = set()
    for idx in candidate_indices:
        country = countries[idx]
        if country.prepared.intersects(place_geom):
            codes.add(country.iso_a2)
    if codes:
        return sorted(codes)
    # Fallback: nearest country within snap distance
    if snap_degrees > 0:
        nearest_idx = tree.nearest(place_geom)
        if nearest_idx is not None:
            nearest = countries[nearest_idx]
            if nearest.geom.distance(place_geom) <= snap_degrees:
                return [nearest.iso_a2]
    return []
# ---------------------------------------------------------------------------
# 4. Bulk update with throttling
# ---------------------------------------------------------------------------
def flush_bulk(es, actions, stats, max_retries=2):
    """Send a bulk request with retries, log error details, update *stats*."""
    if not actions:
        return
    for attempt in range(1 + max_retries):
        try:
            success, errors = helpers.bulk(
                es.options(request_timeout=120), actions,
                raise_on_error=False, stats_only=False,
            )
            stats["updated"] += success
            if errors:
                # Log a sample of the actual error details (first occurrence of each type)
                error_types_seen = set()
                for err in errors:
                    err_info = err.get("update", err.get("index", {}))
                    err_type = err_info.get("error", {}).get("type", "unknown")
                    if err_type not in error_types_seen:
                        error_types_seen.add(err_type)
                        log.warning("Bulk error sample [%s]: %s",
                                    err_type, err_info.get("error", {}))
                    stats["error_types"][err_type] = (
                        stats["error_types"].get(err_type, 0) + 1
                    )
                # Retry only retryable errors (e.g. 429 Too Many Requests)
                retryable = [
                    err for err in errors
                    if (err.get("update", err.get("index", {}))
                        .get("status") == 429)
                ]
                if retryable and attempt < max_retries:
                    # Rebuild actions for only the retryable docs
                    retry_ids = {
                        err.get("update", err.get("index", {})).get("_id")
                        for err in retryable
                    }
                    actions = [a for a in actions if a["_id"] in retry_ids]
                    log.info("Retrying %d retryable errors (attempt %d/%d)",
                             len(actions), attempt + 2, 1 + max_retries)
                    time.sleep(2 ** attempt)  # exponential back-off
                    continue
                stats["bulk_errors"] += len(errors)
            return
        except Exception as e:
            if attempt < max_retries:
                log.warning("Bulk request failed (attempt %d/%d): %s",
                            attempt + 1, 1 + max_retries, e)
                time.sleep(2 ** attempt)
            else:
                log.error("Bulk request failed after %d attempts: %s",
                           1 + max_retries, e)
                stats["bulk_errors"] += len(actions)
            return
# ---------------------------------------------------------------------------
# 5. Main orchestration
# ---------------------------------------------------------------------------
def _count_target_docs(es, namespace, recompute_all):
    """Quick count so tqdm can show a meaningful progress bar."""
    body = _build_scan_query(namespace, recompute_all)
    try:
        resp = es.count(index=PLACES_INDEX, body={"query": body["query"]})
        return resp["count"]
    except Exception:
        return 0
def augment_ccodes(
    es,
    countries,
    tree,
    namespace=None,
    recompute_all=False,
    batch_size=500,
    throttle=0.5,
    limit=None,
    dry_run=False,
    snap_degrees=0.1,
):
    stats = {
        "scanned": 0,
        "no_geom": 0,
        "no_match": 0,
        "already_ok": 0,
        "updated": 0,
        "bulk_errors": 0,
        "error_types": {},
    }
    # Refresh so that updates from prior runs are visible
    # (the places index has refresh_interval=-1 for bulk ingest performance)
    log.info("Refreshing %s index ...", PLACES_INDEX)
    try:
        es.indices.refresh(index=PLACES_INDEX)
        log.info("Refresh complete")
    except Exception as e:
        log.warning("Refresh failed (non-fatal): %s", e)

    total = _count_target_docs(es, namespace, recompute_all)
    if limit:
        total = min(total, limit)
    log.info("Documents to process: %s", "{:,}".format(total))
    actions = []
    t0 = time.monotonic()
    pbar = tqdm(
        scan_places(es, namespace, recompute_all),
        total=total,
        unit="doc",
        desc="augment ccodes",
        dynamic_ncols=True,
    )
    for hit in pbar:
        stats["scanned"] += 1
        if limit and stats["scanned"] > limit:
            break
        src = hit["_source"]
        place_id = src.get("place_id", hit["_id"])
        # Extract geometry
        place_geom = _extract_shapely_geom(src)
        if place_geom is None:
            stats["no_geom"] += 1
            pbar.set_postfix(upd=stats["updated"], nogeom=stats["no_geom"],
                             nomatch=stats["no_match"], err=stats["bulk_errors"],
                             refresh=False)
            continue
        # Match
        new_codes = match_countries(place_geom, tree, countries, snap_degrees)
        if not new_codes:
            stats["no_match"] += 1
            if stats["no_match"] <= 20:
                centroid = place_geom.centroid
                pbar.write("  No-match sample: %s (%.4f, %.4f) [%s]"
                           % (place_id, centroid.x, centroid.y,
                              place_geom.geom_type))
            pbar.set_postfix(upd=stats["updated"], nogeom=stats["no_geom"],
                             nomatch=stats["no_match"], err=stats["bulk_errors"],
                             refresh=False)
            continue
        # Merge with any existing ccodes (set union, preserving authority data)
        existing = set(src.get("ccodes") or [])
        merged = sorted(existing | set(new_codes))
        if merged == sorted(existing):
            stats["already_ok"] += 1
            continue
        if dry_run:
            stats["updated"] += 1
            if stats["updated"] <= 20:
                pbar.write("[dry-run] %s -> %s" % (place_id, merged))
            pbar.set_postfix(upd=stats["updated"], nogeom=stats["no_geom"],
                             nomatch=stats["no_match"], err=stats["bulk_errors"],
                             refresh=False)
            continue
        actions.append({
            "_op_type": "update",
            "_index": PLACES_INDEX,
            "_id": hit["_id"],
            "doc": {"ccodes": merged},
        })
        if len(actions) >= batch_size:
            flush_bulk(es, actions, stats)
            actions.clear()
            pbar.set_postfix(upd=stats["updated"], nogeom=stats["no_geom"],
                             nomatch=stats["no_match"], err=stats["bulk_errors"],
                             refresh=False)
            if throttle > 0:
                time.sleep(throttle)
    pbar.close()
    # Final flush
    if actions:
        flush_bulk(es, actions, stats)
    elapsed = time.monotonic() - t0
    log.info("=" * 60)
    log.info("COMPLETE  %s%.1f s", "[DRY RUN]  " if dry_run else "", elapsed)
    log.info("  Scanned:      %s", "{:,}".format(stats["scanned"]))
    log.info("  Updated:      %s", "{:,}".format(stats["updated"]))
    log.info("  No geometry:  %s", "{:,}".format(stats["no_geom"]))
    log.info("  No match:     %s", "{:,}".format(stats["no_match"]))
    log.info("  Already OK:   %s", "{:,}".format(stats["already_ok"]))
    log.info("  Bulk errors:  %s", "{:,}".format(stats["bulk_errors"]))
    if stats["error_types"]:
        log.info("  Error breakdown:")
        for etype, count in sorted(stats["error_types"].items(),
                                    key=lambda x: -x[1]):
            log.info("    %-40s %s", etype, "{:,}".format(count))
    log.info("=" * 60)
    return stats
# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Augment places with ccodes via spatial intersection with UN country geometries.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute matches but do not write to ES")
    parser.add_argument("--recompute-all", action="store_true",
                        help="Process all places, not just those missing ccodes")
    parser.add_argument("--namespace", type=str, default=None,
                        help="Only process places with this namespace prefix (e.g. 'osm', 'wd')")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Bulk update chunk size (default 500)")
    parser.add_argument("--throttle", type=float, default=0.5,
                        help="Seconds to sleep between bulk flushes (default 0.5)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max documents to scan (for testing)")
    parser.add_argument("--snapshot", action="store_true",
                        help="Create a checkpoint snapshot after completion")
    parser.add_argument("--no-download", action="store_true",
                        help="Use already-cached Natural Earth data")
    parser.add_argument("--snap-degrees", type=float, default=0.1,
                        help="Max distance (degrees) for nearest-country fallback "
                             "on coastal points (~0.1° ≈ 11 km; 0 to disable)")
    parser.add_argument("--es-host", type=str, default=None,
                        help="Override ES host (default: from settings)")
    parser.add_argument("--es-pass-file", type=str, default=None,
                        help="Path to file containing the elastic password")
    args = parser.parse_args()
    host = args.es_host or ES_HOST
    if not host:
        log.error("No ES host configured. Set ES_HOST or pass --es-host.")
        sys.exit(1)
    es_kwargs = {"request_timeout": 300}
    if args.es_pass_file:
        password = Path(args.es_pass_file).read_text().strip()
        es_kwargs["basic_auth"] = ("elastic", password)
    es = Elasticsearch(host, **es_kwargs)
    log.info("Connected to %s", host)
    # 1. Load full-resolution country geometries from Natural Earth
    countries = load_country_geometries(download=not args.no_download)
    if not countries:
        log.error("No country geometries loaded -- check Natural Earth data")
        sys.exit(1)
    tree, countries = build_spatial_index(countries)
    # 2. Run augmentation
    stats = augment_ccodes(
        es,
        countries,
        tree,
        namespace=args.namespace,
        recompute_all=args.recompute_all,
        batch_size=args.batch_size,
        throttle=args.throttle,
        limit=args.limit,
        dry_run=args.dry_run,
        snap_degrees=args.snap_degrees,
    )
    # 3. Optional snapshot
    if args.snapshot and not args.dry_run and stats["updated"] > 0:
        from processing.utilities import create_checkpoint_snapshot
        create_checkpoint_snapshot(es, "augment_ccodes")
if __name__ == "__main__":
    main()
