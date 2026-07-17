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

# --- NLS layer config (CONFIRMED 2026-07-17) --------------------------------
# OS Six-inch 2nd ed. 1888-1915 seamless, EPSG:3857 — GB1900's source era. Layer
# path verified by fetching real PNGs over a GB1900 pin at z15 & z16 (CRC reaches
# this S3 host). Max zoom >= 16 (z16 legible for typography).
NLS_TILE_URL = os.getenv(
    "GB1900_NLS_TILE_URL",
    "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/{z}/{x}/{y}.png",
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


def _global_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    xt, yt, px, py = deg2num(lat, lon, z)
    return xt * TILE_PX + px, yt * TILE_PX + py


def _load_tile(z: int, x: int, y: int):
    from PIL import Image
    p = tile_path(z, x, y)
    if p.exists() and p.stat().st_size > 0:
        try:
            return Image.open(p).convert("RGB")
        except Exception:
            return None
    return None


def stitch_crop(lat: float, lon: float, text: str, z: int):
    """Marker-in-context crop, stitched from the /vast tile cache. Returns
    (PIL.Image | None, meta).

    Rationale (2026-07-17): a rigid rightward box fails two ways — it clips
    non-horizontal labels (OS sets river/coast/range names sloped or curved) and,
    when widened, grabs neighbours. Instead we stitch a *generous context window*
    and draw a small red ring at the label's baseline-left anchor. The VLM reads
    the RING-marked label following it in ANY direction and ignores others — so
    orientation and neighbour-disambiguation are handled by the model, not by
    guessing a box. meta lets a detected pixel bbox back-project to geo.
    """
    from PIL import Image, ImageDraw
    gpx, gpy = _global_px(lat, lon, z)
    n = max(len(text), 3)
    # Context window: generous in all directions so a sloped/curved label fits.
    # Size the guess from the transcription length AND case — ALLCAPS labels have
    # much wider glyphs (and OS sets prominent CAPS names larger), so they need a
    # wider window. Anchor left-of-centre so a rightward label has room, with
    # vertical headroom for slope either way.
    letters = [c for c in text if c.isalpha()]
    allcaps = bool(letters) and all(c.isupper() for c in letters)
    per_char = 22 if allcaps else 14                    # px/char @ z16
    cw = min(520, max(230, int(n * per_char)))
    ch = 240 if allcaps else 210                        # caps run taller too
    ax_frac, ay_frac = 0.28, 0.50            # anchor position within the window
    bl = int(gpx - cw * ax_frac)
    bt = int(gpy - ch * ay_frac)
    br, bb = bl + cw, bt + ch
    canvas = Image.new("RGB", (cw, ch), (240, 240, 235))
    tx0, tx1 = bl // TILE_PX, (br - 1) // TILE_PX
    ty0, ty1 = bt // TILE_PX, (bb - 1) // TILE_PX
    missing = 0
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            im = _load_tile(z, tx, ty)
            if im is None:
                missing += 1
                continue
            canvas.paste(im, (tx * TILE_PX - bl, ty * TILE_PX - bt))
    ax, ay = int(gpx - bl), int(gpy - bt)    # anchor pixel within the crop
    # Hollow ring so it points at the first glyph's base without covering it.
    dr = ImageDraw.Draw(canvas)
    dr.ellipse([ax - 6, ay - 6, ax + 6, ay + 6], outline=(230, 0, 0), width=2)
    meta = {"z": z, "origin_gpx": bl, "origin_gpy": bt, "w": cw, "h": ch,
            "anchor_gpx": gpx, "anchor_gpy": gpy, "anchor_px": [ax, ay],
            "missing_tiles": missing}
    return (None if missing == (tx1 - tx0 + 1) * (ty1 - ty0 + 1) else canvas), meta


def cmd_crops(args) -> None:
    """Crop an over-sized window per label from the cached tiles → PNG + manifest.

    The manifest (one JSON line per crop) feeds the VLM step; it carries the crop's
    geo-referencing so the VLM's detected pixel bbox can be back-projected (plan
    §11.1: record detected bboxes)."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    man = open(args.manifest, "w", encoding="utf-8") if args.manifest else None
    n = saved = skipped = 0
    for rec in _iter_pins(args.pins):
        # VLM runs on the RESIDUAL only — Tier-0 already types abbreviations with
        # high confidence, and the VLM mis-reads tiny abbrevs (neighbour-hijack,
        # pilot 2026-07-17). --untyped-only crops just the Tier-0-untyped pins.
        if args.untyped_only and (rec.get("type") is not None):
            continue
        n += 1
        img, meta = stitch_crop(rec["lat"], rec["lon"], rec.get("text", {}).get("value", "")
                                if isinstance(rec.get("text"), dict) else (rec.get("text") or ""),
                                args.zoom)
        if img is None:
            skipped += 1
            continue
        pid = rec["pin_id"]
        crop_path = out / f"gb_{pid}.png"
        img.save(crop_path)
        saved += 1
        if man is not None:
            man.write(json.dumps({
                "place_id": rec.get("place_id", f"gb:{pid}"), "pin_id": pid,
                "text": rec.get("text"), "token": (rec.get("type") or {}).get("token")
                if isinstance(rec.get("type"), dict) else rec.get("token"),
                "lon": rec["lon"], "lat": rec["lat"],
                "crop_path": str(crop_path), "crop": meta,
            }, ensure_ascii=False) + "\n")
        if args.limit and saved >= args.limit:
            break
    if man is not None:
        man.close()
    print(f"[crops] pins {n:,}; crops saved {saved:,}; skipped(no tiles) {skipped:,} → {out}")
    if args.manifest:
        print(f"[crops] manifest → {args.manifest}")


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
    c.add_argument("--manifest", help="write crop manifest JSONL (feeds VLM step)")
    c.add_argument("--untyped-only", action="store_true",
                   help="crop only Tier-0-untyped (residual) pins — the VLM's domain")
    c.add_argument("--limit", type=int, default=None, help="stop after N crops")
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
