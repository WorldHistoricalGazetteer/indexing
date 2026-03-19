#!/usr/bin/env python3
# processing/augment_ccodes.py

"""
Augment places with ccodes (ISO 3166-1 alpha-2 country codes) by
spatially intersecting each place's geometry against full-resolution
Natural Earth country polygons.

Two-phase approach:
  Phase 1 — Shapely STRtree envelope overlap for O(log N) candidates.
  Phase 2 — Prepared-geometry ``intersects`` for exact test.

Country geometries are loaded at full resolution from the Natural Earth
10 m shapefile (downloaded once and cached).  The simplified copies in
the ES index are *not* used — they were simplified at ~1 km during
ingest, which would misclassify places near borders.

Designed to be run periodically without overloading Elasticsearch:
  • Scans only places that lack ccodes (or optionally all places).
  • Batches bulk updates with configurable chunk size and throttle delay.
  • Supports dry-run mode for testing.

Usage:
    python -m processing.augment_ccodes
    python -m processing.augment_ccodes --dry-run --limit 100
    python -m processing.augment_ccodes --namespace osm --batch-size 200 --throttle 1.0
    python -m processing.augment_ccodes --recompute-all
"""

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

from processing.helpers import geojson_to_shapely
from processing.settings import ES_HOST, DATA_DIR, PLACES_INDEX

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CountryGeom = namedtuple("CountryGeom", ["iso_a2", "prepared", "geom"])

NATURAL_EARTH_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"


# ---------------------------------------------------------------------------
# 1. Load full-resolution country geometries from Natural Earth
# ---------------------------------------------------------------------------

def _download_natural_earth() -> Path:
    """Download Natural Earth 10 m countries ZIP if not already cached."""
    ne_dir = Path(DATA_DIR) / "ISO3166"
    ne_dir.mkdir(parents=True, exist_ok=True)
    zip_path = ne_dir / "ne_10m_admin_0_countries.zip"

    if zip_path.exists():
        log.info("Natural Earth data already cached: %s", zip_path)
        return zip_path

    log.info("Downloading Natural Earth 10 m countries …")
    urllib.request.urlretrieve(NATURAL_EARTH_URL, zip_path)
    log.info("Downloaded (%.1f MB)", zip_path.stat().st_size / 1024 / 1024)
    return zip_path


def _read_shapefile(zip_path: Path) -> list[dict]:
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


def load_country_geometries(*, download: bool = True) -> list[CountryGeom]:
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
            log.error("Natural Earth data not found at %s — run without --no-download", zip_path)
            sys.exit(1)

    features = _read_shapefile(zip_path)
    countries: list[CountryGeom] = []

    for feat in features:
        props = feat["properties"]
        iso_a2 = props.get("ISO_A2") or props.get("ISO_A2_EH") or ""
        if not iso_a2 or iso_a2 == "-99":
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
    return countries


def build_spatial_index(countries: list[CountryGeom]):
    """
    Build a Shapely STRtree for O(log N) candidate lookup.

    Returns (tree, countries) — the tree indexes geometries in the same
    order as *countries*, so ``tree.query`` indices map directly.
    """
    geoms = [c.geom for c in countries]
    tree = STRtree(geoms)
    return tree, countries


# ---------------------------------------------------------------------------
# 2. Scan places missing ccodes
# ---------------------------------------------------------------------------

def _build_scan_query(namespace: str | None, recompute_all: bool) -> dict:
    """
    Build the ES query for the scan.

    By default selects places where ``ccodes`` does not exist.
    If *recompute_all* is True, selects all places (still excluding un: docs).
    """
    filters: list[dict] = []
    must_not: list[dict] = [
        # Never reprocess the country documents themselves
        {"prefix": {"place_id": "un:"}},
    ]

    if not recompute_all:
        must_not.append({"exists": {"field": "ccodes"}})

    if namespace:
        filters.append({"prefix": {"place_id": f"{namespace}:"}})

    query: dict = {
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


def scan_places(es: Elasticsearch, namespace: str | None, recompute_all: bool):
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

def _extract_shapely_geom(hit_source: dict):
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


def match_countries(place_geom, tree: STRtree, countries: list[CountryGeom]) -> list[str]:
    """
    Return sorted list of ISO_A2 codes whose geometries intersect *place_geom*.

    Phase 1: STRtree.query returns candidate indices via R-tree envelope overlap.
    Phase 2: prepared geometry ``intersects`` for exact test.
    """
    candidate_indices = tree.query(place_geom)
    codes: set[str] = set()
    for idx in candidate_indices:
        country = countries[idx]
        if country.prepared.intersects(place_geom):
            codes.add(country.iso_a2)
    return sorted(codes)


# ---------------------------------------------------------------------------
# 4. Bulk update with throttling
# ---------------------------------------------------------------------------

def flush_bulk(es: Elasticsearch, actions: list[dict], stats: dict):
    """Send a bulk request, absorb partial failures, update *stats*."""
    if not actions:
        return
    try:
        success, failed = helpers.bulk(
            es, actions, raise_on_error=False, stats_only=True, request_timeout=120,
        )
        stats["updated"] += success
        stats["bulk_errors"] += failed
    except Exception as e:
        log.error("Bulk request failed: %s", e)
        stats["bulk_errors"] += len(actions)


# ---------------------------------------------------------------------------
# 5. Main orchestration
# ---------------------------------------------------------------------------

def augment_ccodes(
    es: Elasticsearch,
    countries: list[CountryGeom],
    tree: STRtree,
    *,
    namespace: str | None = None,
    recompute_all: bool = False,
    batch_size: int = 500,
    throttle: float = 0.5,
    limit: int | None = None,
    dry_run: bool = False,
):
    stats = {
        "scanned": 0,
        "no_geom": 0,
        "no_match": 0,
        "already_ok": 0,
        "updated": 0,
        "bulk_errors": 0,
    }

    actions: list[dict] = []
    t0 = time.monotonic()

    for hit in scan_places(es, namespace, recompute_all):
        stats["scanned"] += 1

        if limit and stats["scanned"] > limit:
            break

        src = hit["_source"]
        place_id = src.get("place_id", hit["_id"])

        # Extract geometry
        place_geom = _extract_shapely_geom(src)
        if place_geom is None:
            stats["no_geom"] += 1
            continue

        # Match
        new_codes = match_countries(place_geom, tree, countries)
        if not new_codes:
            stats["no_match"] += 1
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
                log.info("[dry-run] %s → %s", place_id, merged)
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
            if throttle > 0:
                time.sleep(throttle)

        # Progress logging
        if stats["scanned"] % 50_000 == 0:
            elapsed = time.monotonic() - t0
            rate = stats["scanned"] / elapsed if elapsed else 0
            log.info(
                "Progress: %s scanned | %s updated | %s no-geom | %s no-match | %.0f docs/s",
                f'{stats["scanned"]:,}',
                f'{stats["updated"]:,}',
                f'{stats["no_geom"]:,}',
                f'{stats["no_match"]:,}',
                rate,
            )

    # Final flush
    if actions:
        flush_bulk(es, actions, stats)

    elapsed = time.monotonic() - t0
    log.info("=" * 60)
    log.info("COMPLETE  %s%.1f s", "[DRY RUN]  " if dry_run else "", elapsed)
    log.info("  Scanned:      %s", f'{stats["scanned"]:,}')
    log.info("  Updated:      %s", f'{stats["updated"]:,}')
    log.info("  No geometry:  %s", f'{stats["no_geom"]:,}')
    log.info("  No match:     %s", f'{stats["no_match"]:,}')
    log.info("  Already OK:   %s", f'{stats["already_ok"]:,}')
    log.info("  Bulk errors:  %s", f'{stats["bulk_errors"]:,}')
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
    parser.add_argument("--es-host", type=str, default=None,
                        help="Override ES host (default: from settings)")
    args = parser.parse_args()

    host = args.es_host or ES_HOST
    if not host:
        log.error("No ES host configured. Set ES_HOST or pass --es-host.")
        sys.exit(1)

    es = Elasticsearch(host, request_timeout=300)
    log.info("Connected to %s", host)

    # 1. Load full-resolution country geometries from Natural Earth
    countries = load_country_geometries(download=not args.no_download)
    if not countries:
        log.error("No country geometries loaded — check Natural Earth data")
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
    )

    # 3. Optional snapshot
    if args.snapshot and not args.dry_run and stats["updated"] > 0:
        from processing.utilities import create_checkpoint_snapshot
        create_checkpoint_snapshot(es, "augment_ccodes")


if __name__ == "__main__":
    main()


