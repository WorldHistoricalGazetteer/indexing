#!/usr/bin/env python
"""Compare a NEW per-geometry-channel tileset against the OLD (live) one, per zoom.

Decodes real tiles (mapbox_vector_tile) and classifies every feature as
coverage / label / cluster / point / polygon / line. Every count is printed with
its denominator (tiles decoded at that zoom, and how many of those exist in each
tileset), so a difference is readable as a difference and not as an absence.

z0..--full-upto: every tile in the UNION of both coordinate sets is decoded.
Above that: a seeded sample of --sample coords from the union.

"points represented" = raw Point features (no label, no coverage) + sum of
point_count over cluster features: the number of source points a tile stands
for. Compare across OLD/NEW at the unclustered zooms (>= cluster_maxzoom+1, i.e.
z9-z10 here) for a "nothing dropped" reading; at clustered zooms the figure is
the same quantity but aggregated.

Usage: chancmp.py NEW.mbtiles OLD.mbtiles [--sample 200] [--full-upto 7]
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import mapbox_vector_tile

CLASSES = ("coverage", "label", "cluster", "point", "polygon", "line", "other")


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def metadata(c: sqlite3.Connection) -> dict:
    return dict(c.execute("SELECT name, value FROM metadata").fetchall())


def coords_by_zoom(c: sqlite3.Connection) -> dict[int, set[tuple[int, int]]]:
    out: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for z, x, y in c.execute("SELECT zoom_level, tile_column, tile_row FROM tiles"):
        out[z].add((x, y))
    return out


def size_stats(c: sqlite3.Connection) -> dict[int, tuple[int, int, int]]:
    rows = c.execute(
        "SELECT zoom_level, COUNT(*), MAX(LENGTH(tile_data)), "
        "SUM(CASE WHEN LENGTH(tile_data) > 500000 THEN 1 ELSE 0 END) "
        "FROM tiles GROUP BY zoom_level"
    ).fetchall()
    return {z: (n, mx, over) for z, n, mx, over in rows}


def tile_bytes(c: sqlite3.Connection, z: int, x: int, y: int) -> bytes | None:
    row = c.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, y),
    ).fetchone()
    if row is None:
        return None
    b = row[0]
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b


def classify(feature: dict) -> str:
    props = feature.get("properties") or {}
    gtype = (feature.get("geometry") or {}).get("type", "")
    if "coverage" in props:
        return "coverage"
    if "label" in props:
        return "label"
    if "point_count" in props:
        return "cluster"
    if gtype in ("Point", "MultiPoint"):
        return "point"
    if gtype in ("Polygon", "MultiPolygon"):
        return "polygon"
    if gtype in ("LineString", "MultiLineString"):
        return "line"
    return "other"


def decode_counts(b: bytes) -> tuple[dict[str, int], int, set[str]]:
    counts: dict[str, int] = defaultdict(int)
    represented = 0
    layers: set[str] = set()
    for layer_name, layer in mapbox_vector_tile.decode(b).items():
        layers.add(layer_name)
        for f in layer.get("features", ()):
            k = classify(f)
            counts[k] += 1
            if k == "cluster":
                try:
                    represented += int((f.get("properties") or {}).get("point_count") or 0)
                except (TypeError, ValueError):
                    pass
            elif k == "point":
                represented += 1
    return counts, represented, layers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("new", type=Path)
    ap.add_argument("old", type=Path)
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--full-upto", type=int, default=7)
    ap.add_argument("--seed", type=int, default=166)
    args = ap.parse_args()

    cn, co = open_ro(args.new), open_ro(args.old)
    mn, mo = metadata(cn), metadata(co)
    print(f"NEW {args.new}  ({args.new.stat().st_size/1e6:,.1f} MB)")
    print(f"OLD {args.old}  ({args.old.stat().st_size/1e6:,.1f} MB)")
    for tag, m in (("NEW", mn), ("OLD", mo)):
        try:
            vl = json.loads(m.get("json", "{}")).get("vector_layers", [])
            desc = "; ".join(f"{v['id']}: {sorted((v.get('fields') or {}).keys())}" for v in vl)
        except (ValueError, KeyError, TypeError):
            desc = "(unreadable metadata.json)"
        print(f"  {tag} vector_layers -> {desc}")
        print(f"  {tag} zooms {m.get('minzoom')}-{m.get('maxzoom')}  bounds {m.get('bounds')}")

    zn, zo = coords_by_zoom(cn), coords_by_zoom(co)
    sn, so = size_stats(cn), size_stats(co)
    rng = random.Random(args.seed)
    zooms = sorted(set(zn) | set(zo))

    hdr = (f"{'z':>2} {'tilesN':>7} {'tilesO':>7} {'maxB_N':>8} {'maxB_O':>8} {'>500K N/O':>9} "
           f"| {'decoded':>7} {'inN':>5} {'inO':>5} | "
           + " ".join(f"{k[:6]+'_N':>8} {k[:6]+'_O':>8}" for k in CLASSES[:6])
           + f" | {'reprN':>9} {'reprO':>9}")
    print()
    print(hdr)
    print("-" * len(hdr))
    layers_seen_n: set[str] = set()
    layers_seen_o: set[str] = set()
    for z in zooms:
        union = sorted(zn.get(z, set()) | zo.get(z, set()))
        if z > args.full_upto and len(union) > args.sample:
            picked = rng.sample(union, args.sample)
        else:
            picked = union
        tot_n: dict[str, int] = defaultdict(int)
        tot_o: dict[str, int] = defaultdict(int)
        rep_n = rep_o = 0
        in_n = in_o = 0
        for (x, y) in picked:
            bn = tile_bytes(cn, z, x, y)
            bo = tile_bytes(co, z, x, y)
            if bn is not None:
                in_n += 1
                cnts, rep, lyr = decode_counts(bn)
                layers_seen_n |= lyr
                for k, v in cnts.items():
                    tot_n[k] += v
                rep_n += rep
            if bo is not None:
                in_o += 1
                cnts, rep, lyr = decode_counts(bo)
                layers_seen_o |= lyr
                for k, v in cnts.items():
                    tot_o[k] += v
                rep_o += rep
        n_stats = sn.get(z, (0, 0, 0))
        o_stats = so.get(z, (0, 0, 0))
        print(
            f"{z:>2} {n_stats[0]:>7,} {o_stats[0]:>7,} {n_stats[1] or 0:>8,} {o_stats[1] or 0:>8,} "
            f"{str(n_stats[2])+'/'+str(o_stats[2]):>9} | {len(picked):>7,} {in_n:>5,} {in_o:>5,} | "
            + " ".join(f"{tot_n.get(k,0):>8,} {tot_o.get(k,0):>8,}" for k in CLASSES[:6])
            + f" | {rep_n:>9,} {rep_o:>9,}"
        )
        if tot_n.get("other") or tot_o.get("other"):
            print(f"   (z{z} 'other' geometry: NEW {tot_n.get('other',0)} OLD {tot_o.get('other',0)})")
    print()
    print(f"source-layers decoded: NEW {sorted(layers_seen_n)}  OLD {sorted(layers_seen_o)}")
    print("columns: tilesN/O = tiles stored per zoom; decoded = tiles examined (union of both coord sets,"
          f" all up to z{args.full_upto}, then a seeded sample of {args.sample}); inN/inO = of those, present in each;"
          " class counts are over the decoded tiles only; reprN/O = points represented (raw points + cluster point_count).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
