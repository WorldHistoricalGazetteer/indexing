#!/usr/bin/env python
"""Post-build check: hold a per-namespace tileset to its build ledger (place#166).

``generate_tiles`` writes ``<bucket>.channels.json`` beside every
``<bucket>.mbtiles`` it builds: how many points, polygons and lines it
streamed, how many label anchors, whether an extent footprint was written, and
which channels were tiled at which zooms. This script reads that ledger and
checks the tileset actually carries what the ledger says, by decoding real
tiles — not by trusting metadata, which a tile-join preserves even when the
tiles it describes were dropped.

What is asserted, and why each can fail:

* **shapes ⇒ labels.** A bucket that streamed polygons or lines must carry a
  ``label`` field in ``vector_layers`` and ``label: 1`` features in sampled
  tiles at z >= the shapes minzoom. Fails on a tileset built by the old
  single-pass code, or one whose label pass failed.
* **shapes ⇒ extent at low zoom, nothing else.** With an extent, sampled tiles
  below the shapes minzoom carry a ``coverage: 1`` feature somewhere and NO
  polygon/line features — the z8 pin is what place#140 relies on, and a shape
  leaking below it means the pin was lost.
* **points ⇒ points below z8.** A bucket that streamed points shows Point
  features WITHOUT ``label`` (raw or clustered) in sampled low-zoom tiles.
  Fails on the old polygon-dominant path, which pinned a hybrid's points to
  z8 and never clustered them (``hgis``).
* **no shapes ⇒ no extent, no labels.** A points-only bucket must not carry
  ``coverage`` or ``label``. Fails when a label pass runs on points.
* **region sources publish shapes** (``--region-source``): a bucket the Atlas
  offers as a boundary source must have streamed at least one shape and must
  show polygon/line features in sampled high-zoom tiles.

Every count is printed with its denominator (tiles sampled per zoom), so a
pass is readable as "N label features in M tiles at z9", never as a bare tick.

Usage::

    python -m processing.verify_tileset_channels /ix1/ishi/data/tiles/wd.mbtiles
    python -m processing.verify_tileset_channels --dir <DIR> --region-source po --region-source clio
    python -m processing.verify_tileset_channels --dir <DIR> --region-sources-url https://whgazetteer.org/api/sources/
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from processing.generate_tiles import (
    CHANNEL_EXTENT,
    CHANNEL_LABELS,
    CHANNEL_LEDGER_SUFFIX,
    CHANNEL_POINTS,
    CHANNEL_SHAPES,
)

# Tiles decoded per zoom level. Every tile at z0-z2 (<= 21) plus a seeded
# random sample above that; decoding all of wd's z10 tiles would take longer
# than building them.
SAMPLE_PER_ZOOM = 64
_SEED = 166

_POINT = "Point"
_POLYGONAL = ("Polygon", "MultiPolygon")
_LINEAR = ("LineString", "MultiLineString")


def read_ledger(mbtiles: Path) -> dict[str, Any] | None:
    bucket = mbtiles.name[:-8] if mbtiles.name.endswith(".mbtiles") else mbtiles.name
    path = mbtiles.with_name(bucket + CHANNEL_LEDGER_SUFFIX)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _metadata(con: sqlite3.Connection) -> dict[str, Any]:
    md = dict(con.execute("SELECT name, value FROM metadata").fetchall())
    try:
        md["json"] = json.loads(md.get("json") or "{}")
    except json.JSONDecodeError:
        md["json"] = {}
    return md


def _fields(md: dict[str, Any], layer: str) -> set[str]:
    for vl in md["json"].get("vector_layers") or []:
        if vl.get("id") == layer:
            return set((vl.get("fields") or {}).keys())
    return set()


def _decode(blob: bytes) -> dict[str, Any]:
    import mapbox_vector_tile  # pure-python decoder; import lazily so the
    # module loads (for --help, for tests of the ledger logic) without it.
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    return mapbox_vector_tile.decode(blob)


def _classify(feature: dict[str, Any]) -> str:
    """One of: label, coverage, cluster, point, polygon, line, other."""
    props = feature.get("properties") or {}
    gtype = (feature.get("geometry") or {}).get("type")
    if props.get("label") == 1:
        return "label"
    if props.get("coverage") == 1:
        return "coverage"
    if gtype == _POINT or gtype == "MultiPoint":
        return "cluster" if "point_count" in props else "point"
    if gtype in _POLYGONAL:
        return "polygon"
    if gtype in _LINEAR:
        return "line"
    return "other"


def sample_tiles(con: sqlite3.Connection, max_zoom: int,
                 per_zoom: int = SAMPLE_PER_ZOOM) -> dict[int, list[tuple[int, int, bytes]]]:
    """``{z: [(x, tms_row, blob), ...]}`` — all tiles at z<=2, a seeded sample above."""
    rng = random.Random(_SEED)
    out: dict[int, list[tuple[int, int, bytes]]] = {}
    for z in range(0, max_zoom + 1):
        n = con.execute("SELECT COUNT(*) FROM tiles WHERE zoom_level=?", (z,)).fetchone()[0]
        if not n:
            out[z] = []
            continue
        if z <= 2 or n <= per_zoom:
            rows = con.execute(
                "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level=?",
                (z,)).fetchall()
        else:
            # ORDER BY random() is unseeded in SQLite; pick offsets ourselves.
            offsets = sorted(rng.sample(range(n), per_zoom))
            rows = []
            for off in offsets:
                r = con.execute(
                    "SELECT tile_column, tile_row, tile_data FROM tiles "
                    "WHERE zoom_level=? ORDER BY tile_column, tile_row LIMIT 1 OFFSET ?",
                    (z, off)).fetchone()
                if r:
                    rows.append(r)
        out[z] = [(x, y, blob) for x, y, blob in rows]
    return out


def count_classes(samples: dict[int, list[tuple[int, int, bytes]]], layer: str
                  ) -> dict[int, Counter]:
    """Per-zoom feature-class counts over the sampled tiles."""
    per_zoom: dict[int, Counter] = {}
    for z, tiles in samples.items():
        c: Counter = Counter()
        for _x, _y, blob in tiles:
            try:
                decoded = _decode(blob)
            except Exception as exc:  # noqa: BLE001 - a tile that won't decode is a finding
                c["undecodable"] += 1
                continue
            lyr = decoded.get(layer)
            if not lyr:
                c["no_layer"] += 1
                continue
            for f in lyr.get("features") or []:
                c[_classify(f)] += 1
        c["tiles"] = len(tiles)
        per_zoom[z] = c
    return per_zoom


def first_label_scan(mbtiles: Path, layer: str, z: int) -> tuple[int, int, int]:
    """Scan EVERY tile at zoom ``z`` in seeded random order until one holds a
    ``label: 1`` feature. Returns ``(labels_in_that_tile, tiles_scanned,
    tiles_total)``; ``(0, total, total)`` when no tile at that zoom has one."""
    con = sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True, timeout=30)
    try:
        coords = con.execute(
            "SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?", (z,)).fetchall()
        random.Random(_SEED).shuffle(coords)
        for i, (x, y) in enumerate(coords, 1):
            blob = con.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, y)).fetchone()[0]
            try:
                lyr = _decode(blob).get(layer) or {}
            except Exception:  # noqa: BLE001 - counted by the sampled pass
                continue
            n = sum(1 for f in lyr.get("features") or [] if _classify(f) == "label")
            if n:
                return n, i, len(coords)
        return 0, len(coords), len(coords)
    finally:
        con.close()


def verify(mbtiles: Path, ledger: dict[str, Any], *,
           require_shapes: bool = False) -> tuple[list[str], list[str]]:
    """Return ``(failures, report_lines)``."""
    failures: list[str] = []
    report: list[str] = []
    bucket = ledger.get("bucket") or mbtiles.name[:-8]
    counts = ledger.get("counts") or {}
    n_point = int(counts.get("point", 0))
    n_shape = int(counts.get("polygon", 0)) + int(counts.get("line", 0))
    channels = ledger.get("channels") or {}
    has_extent = bool(ledger.get("extent")) and CHANNEL_EXTENT in channels
    n_labels = int(ledger.get("label_anchors", 0))
    shapes_min = int(channels.get(CHANNEL_SHAPES, {}).get("minzoom", 0))

    report.append(f"{bucket}: ledger points={n_point:,} polygons={counts.get('polygon', 0):,} "
                  f"lines={counts.get('line', 0):,} anchors={n_labels:,} "
                  f"extent={'yes' if has_extent else 'no'} channels={sorted(channels)}")

    if require_shapes and n_shape == 0:
        failures.append(f"{bucket}: declared a region source but streamed no polygons or lines")

    con = sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True, timeout=30)
    try:
        md = _metadata(con)
        fields = _fields(md, bucket)
        layers = [vl.get("id") for vl in md["json"].get("vector_layers") or []]
        if layers != [bucket]:
            failures.append(f"{bucket}: vector_layers is {layers!r}, expected exactly [{bucket!r}] "
                            f"(channels must share ONE source-layer)")
        maxzoom = int(md.get("maxzoom") or 10)

        # Metadata-level expectations.
        if n_shape and CHANNEL_LABELS in channels and "label" not in fields:
            failures.append(f"{bucket}: ledger has {n_labels:,} label anchors but no 'label' field "
                            f"in vector_layers — the label channel did not reach the tileset")
        if has_extent and "coverage" not in fields:
            failures.append(f"{bucket}: ledger says an extent was tiled but 'coverage' is not a field")
        if not n_shape and "label" in fields:
            failures.append(f"{bucket}: 'label' field present on a bucket that streamed no shapes")
        if not n_shape and "coverage" in fields:
            failures.append(f"{bucket}: 'coverage' field present on a bucket that streamed no shapes")
        # NB no assertion on a ``point_count`` field: tippecanoe only writes
        # it when two points actually fell within the cluster radius, so a
        # sparse bucket legitimately lacks it. Clustering is checked below
        # from the tiles themselves (points present below z8).

        # Tile-level expectations, from decoded samples.
        samples = sample_tiles(con, maxzoom)
    finally:
        con.close()

    per_zoom = count_classes(samples, bucket)
    for z in sorted(per_zoom):
        c = per_zoom[z]
        if not c["tiles"]:
            continue
        report.append(
            f"  z{z}: {c['tiles']} tiles sampled — point={c['point']} cluster={c['cluster']} "
            f"polygon={c['polygon']} line={c['line']} label={c['label']} "
            f"coverage={c['coverage']}"
            + (f" undecodable={c['undecodable']}" if c["undecodable"] else "")
            + (f" no_layer={c['no_layer']}" if c["no_layer"] else ""))

    low = Counter()
    high = Counter()
    for z, c in per_zoom.items():
        (low if z < shapes_min else high).update(c)
    undecodable = sum(c["undecodable"] for c in per_zoom.values())
    if undecodable:
        failures.append(f"{bucket}: {undecodable} sampled tile(s) could not be decoded")

    if n_shape:
        if high["polygon"] + high["line"] == 0:
            failures.append(f"{bucket}: ledger has {n_shape:,} shapes but no polygon/line feature "
                            f"in {sum(c['tiles'] for z, c in per_zoom.items() if z >= shapes_min)} "
                            f"sampled tiles at z>={shapes_min}")
        if n_labels and high["label"] == 0:
            # A sample cannot see one anchor per country-sized polygon: po
            # (7,815 period extents) has label features in 304 of its 39,300
            # z8 tiles, and a 64-tile sample found none (10 Oct 2026). Before
            # failing, scan the whole zoom in seeded random order and stop at
            # the first label; a tileset with no labels at all still scans
            # every tile and still fails.
            found, scanned, total = first_label_scan(mbtiles, bucket, shapes_min)
            if found:
                report.append(f"  z{shapes_min}: no label in the sample; full scan found "
                              f"{found} label feature(s) after {scanned:,} of {total:,} tiles")
            else:
                failures.append(f"{bucket}: ledger has {n_labels:,} label anchors but none in "
                                f"sampled tiles at z>={shapes_min}, nor in any of the "
                                f"{total:,} tiles at z{shapes_min}")
        if has_extent:
            if low["coverage"] == 0:
                failures.append(f"{bucket}: extent was tiled but no coverage feature in "
                                f"{sum(c['tiles'] for z, c in per_zoom.items() if z < shapes_min)} "
                                f"sampled tiles below z{shapes_min}")
            if low["polygon"] + low["line"]:
                failures.append(f"{bucket}: {low['polygon'] + low['line']} shape feature(s) leaked "
                                f"below z{shapes_min} — the z8 pin was lost")
            if high["coverage"]:
                failures.append(f"{bucket}: coverage feature present at z>={shapes_min}")
    else:
        if low["label"] + high["label"]:
            failures.append(f"{bucket}: label features in a bucket that streamed no shapes")
        if low["coverage"] + high["coverage"]:
            failures.append(f"{bucket}: coverage feature in a bucket that streamed no shapes")

    if n_point:
        seen = sum(c["point"] + c["cluster"] for c in per_zoom.values())
        if seen == 0:
            failures.append(f"{bucket}: ledger has {n_point:,} points but no point feature in any "
                            f"sampled tile")
        elif CHANNEL_POINTS in channels and channels[CHANNEL_POINTS].get("cluster_points"):
            below8 = sum(c["point"] + c["cluster"] for z, c in per_zoom.items() if z < 8)
            if below8 == 0 and any(c["tiles"] for z, c in per_zoom.items() if z < 8):
                failures.append(f"{bucket}: points were streamed for clustering but none appear "
                                f"below z8 — were they pinned with the shapes?")
    return failures, report


def region_sources_from_url(url: str, timeout: float = 15.0) -> list[str] | None:
    """Namespaces flagged ``region_source`` by the WHG registry, or None when
    the endpoint does not expose the flag (older whg3), so the caller can say
    so instead of silently asserting nothing."""
    from urllib.request import Request, urlopen
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as resp:
        payload = json.loads(resp.read())
    sources = payload.get("sources") or []
    if not sources or not any("region_source" in s for s in sources):
        return None
    return [s["namespace"] for s in sources if s.get("region_source")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mbtiles", nargs="*", type=Path)
    ap.add_argument("--dir", type=Path, help="Check every *.mbtiles with a ledger in this directory")
    ap.add_argument("--region-source", action="append", default=[],
                    help="Bucket that must publish shapes (repeatable)")
    ap.add_argument("--region-sources-url",
                    help="Read region_source flags from this /api/sources/ endpoint")
    ap.add_argument("--allow-missing-ledger", action="store_true",
                    help="Skip (rather than fail) tilesets with no channels.json beside them")
    args = ap.parse_args()

    targets = list(args.mbtiles)
    if args.dir:
        targets += sorted(args.dir.glob("*.mbtiles"))
    if not targets:
        ap.error("give one or more .mbtiles paths, or --dir")

    region_sources = set(args.region_source)
    if args.region_sources_url:
        found = region_sources_from_url(args.region_sources_url)
        if found is None:
            print(f"✗ {args.region_sources_url} does not expose region_source — "
                  f"pass --region-source explicitly", file=sys.stderr)
            return 2
        region_sources |= set(found)
        print(f"region sources from registry: {sorted(region_sources)}")

    total = 0
    checked = 0
    for t in targets:
        bucket = t.name[:-8] if t.name.endswith(".mbtiles") else t.name
        ledger = read_ledger(t)
        if ledger is None:
            if args.allow_missing_ledger:
                print(f"– {t.name}: no {bucket}{CHANNEL_LEDGER_SUFFIX} beside it; skipped")
                continue
            print(f"✗ {t.name}: no {bucket}{CHANNEL_LEDGER_SUFFIX} beside it — nothing to hold "
                  f"the tileset to (built by the old pipeline, or the ledger was not copied)")
            total += 1
            continue
        failures, report = verify(t, ledger, require_shapes=bucket in region_sources)
        checked += 1
        for line in report:
            print(line)
        if failures:
            total += len(failures)
            print(f"✗ {t.name}: {len(failures)} failure(s)")
            for f in failures:
                print(f"    {f}")
        else:
            print(f"✓ {t.name}: channels match the ledger")

    missing_region = region_sources - {t.name[:-8] for t in targets}
    if missing_region:
        print(f"⚠ region source(s) not among the checked tilesets: {sorted(missing_region)}")

    print(f"\n{checked} tileset(s) checked, {total} failure(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
