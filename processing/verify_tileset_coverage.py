#!/usr/bin/env python
"""Post-build check: refuse a tileset with holes over land at low zoom.

place#160: `ohm` shipped missing `0/0/0` (the entire world), both z1
northern-hemisphere tiles, two z2 tiles and central Europe at z3. Every stage
reported success — `tile-join` drops an over-large tile and still exits 0 — so
the tileset reached production with square voids in it and was found by eye in
the Atlas months later.

The issue's own conclusion: *"Worth a post-build assertion: fail the job if any
land tile at z0-z4 is absent. This shipped silently."* This is that assertion.

**Land, specifically.** Most low-zoom tiles are legitimately empty — ocean and
the polar caps carry no boundary geometry, and `osm`'s only gaps are exactly
those. Failing on any missing tile would cry wolf on every tileset. The land
mask below is deliberately coarse: it lists tiles that certainly contain
substantial land, so a missing one is unambiguous.

Usage::

    python -m processing.verify_tileset_coverage /ix1/ishi/data/tiles/ohm.mbtiles
    python -m processing.verify_tileset_coverage --dir /ix1/ishi/data/tiles
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path

MAX_CHECK_ZOOM = 4

# Only these are DENSE GLOBAL boundary tilesets — every land point on earth
# should fall inside one of their polygons, so a missing land tile is
# unambiguously a hole. Everything else is regional (`ukhc` = 92 English and
# Welsh counties) or sparse (`osm_misc` = scattered categories, with no
# features near Johannesburg or Jakarta), and asserting global land coverage
# against those manufactures failures that are really just the shape of the
# data. A check that cries wolf is a check that gets ignored.
GLOBAL_DENSE_BUCKETS = {"osm", "ohm"}

# Points that are unambiguously on land with mapped administrative boundaries,
# spread across every populated continent. A tile containing one of these must
# exist in any global boundary tileset.
_LAND_POINTS: list[tuple[str, float, float]] = [
    ("London", -0.13, 51.51),
    ("Berlin", 13.40, 52.52),
    ("Moscow", 37.62, 55.75),
    ("Istanbul", 28.98, 41.01),
    ("Cairo", 31.24, 30.04),
    ("Lagos", 3.38, 6.52),
    ("Johannesburg", 28.05, -26.20),
    ("Delhi", 77.21, 28.61),
    ("Beijing", 116.41, 39.90),
    ("Tokyo", 139.69, 35.69),
    ("Jakarta", 106.83, -6.21),
    ("Sydney", 151.21, -33.87),
    ("New York", -74.01, 40.71),
    ("Chicago", -87.62, 41.88),
    ("Mexico City", -99.13, 19.43),
    ("Bogota", -74.07, 4.71),
    ("Sao Paulo", -46.63, -23.55),
    ("Buenos Aires", -58.38, -34.60),
    ("Toronto", -79.38, 43.65),
    ("Madrid", -3.70, 40.42),
]


def _tile_for(lon: float, lat: float, z: int) -> tuple[int, int]:
    """Web-Mercator tile containing (lon, lat) at zoom z."""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return min(x, n - 1), min(y, n - 1)


def required_tiles(max_zoom: int = MAX_CHECK_ZOOM) -> dict[tuple[int, int, int], list[str]]:
    """{(z, x, y): [place names]} that any global boundary tileset must hold."""
    out: dict[tuple[int, int, int], list[str]] = {}
    for z in range(0, max_zoom + 1):
        for name, lon, lat in _LAND_POINTS:
            x, y = _tile_for(lon, lat, z)
            out.setdefault((z, x, y), []).append(name)
    return out


def present_tiles(mbtiles: Path, max_zoom: int = MAX_CHECK_ZOOM) -> set[tuple[int, int, int]]:
    """Tiles actually stored, in (z, x, y) with y in XYZ (not TMS) order.

    MBTiles stores `tile_row` flipped (TMS), so it is converted here — getting
    this backwards would report every tile missing.
    """
    conn = sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute(
            "SELECT zoom_level, tile_column, tile_row FROM tiles "
            "WHERE zoom_level <= ?", (max_zoom,)).fetchall()
    finally:
        conn.close()
    return {(z, x, (2 ** z - 1) - row) for z, x, row in rows}


def tileset_bounds(mbtiles: Path) -> tuple[float, float, float, float] | None:
    """(minlon, minlat, maxlon, maxlat) from mbtiles metadata, or None."""
    try:
        conn = sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True, timeout=30)
        row = conn.execute(
            "SELECT value FROM metadata WHERE name='bounds'").fetchone()
        conn.close()
        if not row:
            return None
        parts = [float(v) for v in str(row[0]).split(",")]
        return (parts[0], parts[1], parts[2], parts[3]) if len(parts) == 4 else None
    except Exception:
        return None


def declared_zoom_range(mbtiles: Path) -> tuple[int | None, int | None]:
    """(minzoom, maxzoom) from mbtiles metadata.

    A tileset is not obliged to start at z0: `osm_misc`'s bands begin at z2, so
    demanding a z0/z1 tile from it reports a hole where the pipeline is working
    exactly as configured.
    """
    try:
        conn = sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True, timeout=30)
        rows = dict(conn.execute(
            "SELECT name, value FROM metadata "
            "WHERE name IN ('minzoom','maxzoom')").fetchall())
        conn.close()
        lo = int(rows["minzoom"]) if "minzoom" in rows else None
        hi = int(rows["maxzoom"]) if "maxzoom" in rows else None
        return lo, hi
    except Exception:
        return None, None


def verify(mbtiles: Path, max_zoom: int = MAX_CHECK_ZOOM) -> list[str]:
    """Return a list of failures; empty means the tileset covers its own land.

    Scoped to the tileset's DECLARED BOUNDS. Demanding global coverage from a
    regional gazetteer is nonsense — `ukhc` is 92 English and Welsh counties
    and was reported as missing 34 land tiles, all of them in Africa, Asia and
    the Americas. A check that cannot pass gets ignored, which would have cost
    the global check that does matter.

    A tileset whose bounds contain none of the land points is skipped rather
    than failed: it is regional and this check has nothing useful to say.
    """
    if not mbtiles.exists():
        return [f"{mbtiles.name}: file does not exist"]
    have = present_tiles(mbtiles, max_zoom)

    bounds = tileset_bounds(mbtiles)
    if bounds:
        minlon, minlat, maxlon, maxlat = bounds
        points = [(n, lon, lat) for n, lon, lat in _LAND_POINTS
                  if minlon <= lon <= maxlon and minlat <= lat <= maxlat]
    else:
        points = list(_LAND_POINTS)

    if not points:
        return []  # regional tileset — nothing global to assert

    # Never demand a zoom the tileset does not claim to cover.
    lo, hi = declared_zoom_range(mbtiles)
    z_from = lo if lo is not None else 0
    z_to = min(max_zoom, hi) if hi is not None else max_zoom
    if z_from > z_to:
        return []

    need: dict[tuple[int, int, int], list[str]] = {}
    for z in range(z_from, z_to + 1):
        for name, lon, lat in points:
            x, y = _tile_for(lon, lat, z)
            need.setdefault((z, x, y), []).append(name)

    failures = []
    for (z, x, y), names in sorted(need.items()):
        if (z, x, y) not in have:
            failures.append(
                f"{mbtiles.name}: MISSING tile {z}/{x}/{y} — contains "
                f"{', '.join(sorted(set(names))[:4])}")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mbtiles", nargs="*", type=Path)
    ap.add_argument("--dir", type=Path,
                    help="Check every .mbtiles in this directory")
    ap.add_argument("--max-zoom", type=int, default=MAX_CHECK_ZOOM)
    ap.add_argument("--all", action="store_true",
                    help="Check every tileset, not just the dense global ones "
                         "(expect sparsity false alarms on regional buckets)")
    args = ap.parse_args()

    targets = list(args.mbtiles)
    if args.dir:
        targets += sorted(args.dir.glob("*.mbtiles"))
    if not targets:
        ap.error("give one or more .mbtiles paths, or --dir")

    total_failures = 0
    for t in targets:
        bucket = t.name[:-8] if t.name.endswith(".mbtiles") else t.name
        if not args.all and bucket not in GLOBAL_DENSE_BUCKETS:
            print(f"– {t.name}: skipped (not a dense global boundary tileset; "
                  f"pass --all to check anyway)")
            continue
        failures = verify(t, args.max_zoom)
        if failures:
            total_failures += len(failures)
            print(f"✗ {t.name}: {len(failures)} missing land tile(s) "
                  f"at z0-z{args.max_zoom}")
            for f in failures[:12]:
                print(f"    {f}")
        else:
            print(f"✓ {t.name}: all land tiles present at z0-z{args.max_zoom}")

    if total_failures:
        print(f"\nFAILED — {total_failures} missing land tile(s). "
              f"A tileset with holes must not be published (place#160).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
