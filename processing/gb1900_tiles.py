#!/usr/bin/env python
"""GB1900 Tier-1 imagery — NLS tile fetch + per-label crop (scaffolding).

Fetches the georeferenced OS Six-inch 2nd-edition (1888-1915) raster covering
each GB1900 pin, caches every tile PERMANENTLY on /vast for research re-use
(SG policy, plan §5.3), and crops an over-sized window around each label's
baseline-left anchor (the only geometry the source records — plan §2.1) for the
VLM typography+text pass.

STATUS: scaffolding. The one gating unknown is the exact NLS six-inch XYZ
template + max zoom (plan P1). Set NLS_TILE_URL/NLS_MAX_Z once confirmed from the
NLS georeferenced-maps viewer (`maps.nls.uk/geo/explore/` → "XYZ"). CRC CAN reach
`mapseries-tilesets.s3.amazonaws.com` (verified 2026-07-17), so fetching runs on a
CRC node. Runs network-bound (throttled) — NOT on a GPU node.

Usage (pilot, one county / a pins subset):
  python -m processing.gb1900_tiles fetch  --pins pins.jsonl --zoom 16 [--rps 5]
  python -m processing.gb1900_tiles crops  --pins pins.jsonl --zoom 16 --out crops/
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path

# --- NLS layer config (CONFIRM at P1) ---------------------------------------
# OS Six-inch 2nd ed. 1888-1915 seamless, EPSG:3857. Placeholder template — the
# exact layer path + max zoom must be read off the NLS georef viewer's XYZ box.
NLS_TILE_URL = os.getenv(
    "GB1900_NLS_TILE_URL",
    "https://mapseries-tilesets.s3.amazonaws.com/os_6inch_gb_1900/{z}/{x}/{y}.png",
)
NLS_MAX_Z = int(os.getenv("GB1900_NLS_MAX_Z", "16"))
TILE_CACHE = Path(os.getenv("GB1900_TILE_CACHE", "/vast/ishi/gb1900/tiles"))
TILE_PX = 256
NLS_ATTRIB = "Reproduced with the permission of the National Library of Scotland"


def deg2num(lat: float, lon: float, z: int) -> tuple[int, int, float, float]:
    """(lat,lon)->(xtile, ytile, px, py) at zoom z (Web Mercator, EPSG:3857)."""
    n = 2 ** z
    xf = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    yf = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    xt, yt = int(xf), int(yf)
    return xt, yt, (xf - xt) * TILE_PX, (yf - yt) * TILE_PX


def covering_tiles(lat: float, lon: float, z: int, pad_tiles: int = 1):
    """Tiles covering a padded window around a point (labels read rightward)."""
    xt, yt, _, _ = deg2num(lat, lon, z)
    return {(z, xt + dx, yt + dy)
            for dx in range(0, pad_tiles + 1)      # rightward reading direction
            for dy in range(-pad_tiles, pad_tiles + 1)}


def tile_path(z: int, x: int, y: int) -> Path:
    return TILE_CACHE / str(z) / str(x) / f"{y}.png"


def fetch_tile(z: int, x: int, y: int, session, rps: float) -> Path | None:
    """Fetch one tile to the permanent /vast cache; skip if already cached."""
    p = tile_path(z, x, y)
    if p.exists() and p.stat().st_size > 0:
        return p
    url = NLS_TILE_URL.format(z=z, x=x, y=y)
    r = session.get(url, timeout=30)
    if r.status_code == 200 and r.content:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
        time.sleep(1.0 / rps)   # polite throttle to NLS
        return p
    return None


def _iter_pins(path: str):
    with open(path, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("lat") is not None and rec.get("lon") is not None:
                yield rec


def cmd_fetch(args) -> None:
    import httpx  # deferred; network host only
    needed: set = set()
    for rec in _iter_pins(args.pins):
        needed |= covering_tiles(rec["lat"], rec["lon"], args.zoom, args.pad)
    print(f"[fetch] {len(needed):,} unique tiles cover the pins (dedup from labels)")
    got = skipped = 0
    with httpx.Client(headers={"User-Agent": "WHG-research/1.0"}) as s:
        for z, x, y in sorted(needed):
            if tile_path(z, x, y).exists():
                skipped += 1
                continue
            if fetch_tile(z, x, y, s, args.rps):
                got += 1
    print(f"[fetch] fetched {got:,}, already-cached {skipped:,} → {TILE_CACHE}")


def cmd_crops(args) -> None:
    """Crop an over-sized window per label (OCR-refine happens in the VLM step)."""
    from PIL import Image  # scaffolding: real stitch/crop
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for rec in _iter_pins(args.pins):
        # anchor pixel = baseline-left of the first glyph (source convention).
        # Over-crop rightward + margin; label re-detected inside by the VLM/OCR.
        # (Stitch the covering tiles, crop [px-8 : px+W, py-H/2 : py+H/2].)
        n += 1  # TODO: implement stitch+crop against the /vast tile cache
    print(f"[crops] {n:,} pins queued; writes crop PNG + provisional bbox to {out}")
    print("[crops] NB record detected bbox into the edition record (plan §11.1).")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.set_defaults(fn=cmd_fetch)
    c = sub.add_parser("crops"); c.set_defaults(fn=cmd_crops)
    for sp in (f, c):
        sp.add_argument("--pins", required=True, help="typed JSONL from gb1900_text_types")
        sp.add_argument("--zoom", type=int, default=NLS_MAX_Z)
        sp.add_argument("--pad", type=int, default=1, help="tile padding around anchor")
    f.add_argument("--rps", type=float, default=5.0, help="fetch rate limit")
    c.add_argument("--out", default="/vast/ishi/gb1900/crops")
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
