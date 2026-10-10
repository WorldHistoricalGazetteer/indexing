#!/usr/bin/env python
"""Name every tile over a byte limit, at every zoom, in one or two tilesets.

Gate 3 of plan-tile-channels-166.md §4.3: a new oversize tile is a stop until it
is measured and NAMED (never dropped). ``tsize.py`` prints per-zoom maxima to
z8 only; this prints each offending tile as ``z/x/y`` (XYZ, the URL form — the
mbtiles row is TMS and is converted) with its stored size, so the record can
quote them. Per-zoom maxima are printed for every zoom too.

    python developer/tile-qa/oversize.py NEW.mbtiles [OLD.mbtiles] [--limit 500000]

With OLD given, each NEW oversize tile says whether OLD also had that tile over
the limit (an inherited one) and what size OLD stored for it.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys


def open_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def per_zoom(c: sqlite3.Connection) -> list[tuple[int, int, int, int]]:
    return c.execute(
        "SELECT zoom_level, COUNT(*), MAX(LENGTH(tile_data)), "
        "CAST(AVG(LENGTH(tile_data)) AS INTEGER) FROM tiles GROUP BY zoom_level "
        "ORDER BY zoom_level").fetchall()


def oversize(c: sqlite3.Connection, limit: int) -> dict[tuple[int, int, int], int]:
    rows = c.execute(
        "SELECT zoom_level, tile_column, tile_row, LENGTH(tile_data) FROM tiles "
        "WHERE LENGTH(tile_data) > ? ORDER BY zoom_level, LENGTH(tile_data) DESC",
        (limit,)).fetchall()
    return {(z, x, (1 << z) - 1 - y_tms): n for z, x, y_tms, n in rows}


def size_of(c: sqlite3.Connection, z: int, x: int, y: int) -> int | None:
    row = c.execute(
        "SELECT LENGTH(tile_data) FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, (1 << z) - 1 - y)).fetchone()
    return None if row is None else row[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("new")
    ap.add_argument("old", nargs="?")
    ap.add_argument("--limit", type=int, default=500_000)
    a = ap.parse_args()
    new = open_ro(a.new)
    old = open_ro(a.old) if a.old else None
    total = new.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    print(f"NEW {a.new}: {total:,} tiles; per zoom (count, max bytes, avg bytes):")
    for z, n, mx, avg in per_zoom(new):
        flag = "  <-- over limit" if mx > a.limit else ""
        print(f"  z{z:<2} {n:>9,}  max {mx:>9,}  avg {avg:>8,}{flag}")
    over = oversize(new, a.limit)
    print(f"\ntiles over {a.limit:,} bytes in NEW: {len(over)} of {total:,}")
    old_over = oversize(old, a.limit) if old else {}
    for (z, x, y), n in over.items():
        if old is not None:
            o = size_of(old, z, x, y)
            inherited = (z, x, y) in old_over
            print(f"  {z}/{x}/{y}  {n:>9,} B   OLD {('%9s' % format(o, ',')) if o is not None else '  (absent)'} B"
                  f"{'  (OLD also over the limit)' if inherited else '  NEW'}")
        else:
            print(f"  {z}/{x}/{y}  {n:>9,} B")
    if old is not None:
        only_old = [k for k in old_over if k not in over]
        print(f"tiles over the limit in OLD but not NEW: {len(only_old)} of {len(old_over)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
