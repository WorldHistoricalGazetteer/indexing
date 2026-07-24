"""GB1900 gazetteer -> compact pin index in z17 global pixel space.

The gazetteer is the TRANSCRIPT SOURCE for the whole GB-STAMP pipeline (2.55M volunteer-placed pins carrying
`final_text` at known coords), so every downstream stage wants the same thing: "which pins fall in this window,
and where are they in window pixels?". Parsing the 700 MB UTF-16 CSV per job is wasteful, so do it once here and
persist float32 z17 pixel coords alongside the text.

Also emits a per-z17-tile pin COUNT map (`tilecount`), so a coverage planner can enumerate exactly the windows
that contain pins instead of tiling all of Britain blindly.

    python build_pin_index.py                       # -> /vast/ishi/gb1900/pins_z17.npz
"""
import argparse, codecs, csv, math, os, sys, numpy as np

CSV = "/vast/ishi/gb1900/gb1900_gazetteer_complete.csv"
OUT = "/vast/ishi/gb1900/pins_z17.npz"
N17 = 2 ** 17


def lonlat_to_px(lon, lat):
    """Web-Mercator z17 GLOBAL pixel coords — identical convention to spot_sheet.lonlat_to_px."""
    x = (lon + 180.0) / 360.0 * N17 * 256
    y = (1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y


def load_pins(path=OUT):
    """Load the index. Returns a dict of arrays sorted by packed tile key (see `pins_in_box`)."""
    z = np.load(path, allow_pickle=True)
    return {k: z[k] for k in z.files}


def pins_in_box(P, x0, y0, x1, y1):
    """Indices of pins inside the global-px box [x0,x1) x [y0,y1).

    Narrows by the sorted tile key first (one searchsorted per tile column) so a window query touches a few
    thousand rows instead of 2.5M — worth it because the per-window loop runs ~10^5-10^6 times in a full pass.
    """
    tx0, tx1 = int(x0) // 256, int(x1) // 256
    ty0, ty1 = int(y0) // 256, int(y1) // 256
    key = P["tile_key"]
    idx = []
    for txi in range(tx0, tx1 + 1):
        lo = np.searchsorted(key, txi * (1 << 20) + ty0, "left")
        hi = np.searchsorted(key, txi * (1 << 20) + ty1, "right")
        if hi > lo:
            idx.append(np.arange(lo, hi))
    if not idx:
        return np.empty(0, np.int64)
    idx = np.concatenate(idx)
    gx, gy = P["gx"][idx], P["gy"][idx]
    return idx[(gx >= x0) & (gx < x1) & (gy >= y0) & (gy < y1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=CSV)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()

    # stdlib csv rather than pandas: the file is UTF-16 with quoted text fields, and this keeps the index
    # buildable from the Hi-SAM env (which has no pandas) so the whole pipeline needs exactly one environment.
    pid, txt, lons, lats = [], [], [], []
    n0 = 0
    with codecs.open(a.csv, "r", "utf-16") as fh:
        for row in csv.DictReader(fh):
            n0 += 1
            t = (row.get("final_text") or "").strip()
            try:
                lat, lon = float(row["latitude"]), float(row["longitude"])
            except (TypeError, ValueError):
                continue
            # GB1900 covers Britain only; anything outside is a coordinate error, and Mercator y explodes
            # near the poles.
            if not t or not (49.0 <= lat <= 61.5) or not (-9.0 <= lon <= 2.5):
                continue
            pid.append(row["pin_id"]); txt.append(t); lats.append(lat); lons.append(lon)
    print(f"{n0} rows -> {len(pid)} usable pins (dropped {n0 - len(pid)}: bad coords / empty text)", flush=True)

    lons = np.asarray(lons, np.float64); lats = np.asarray(lats, np.float64)
    gx, gy = lonlat_to_px(lons, lats)
    # Sort by tile so window queries hit a contiguous slice (searchsorted on the tile key).
    tx = (gx // 256).astype(np.int32)
    ty = (gy // 256).astype(np.int32)
    key = tx.astype(np.int64) * (1 << 20) + ty            # ty < 2^20 at z17, so the pack is lossless
    order = np.argsort(key, kind="stable")

    tx, ty, key = tx[order], ty[order], key[order]
    ukey, ucount = np.unique(key, return_counts=True)
    print(f"{len(ukey)} distinct z17 tiles carry pins; max {ucount.max()} pins/tile, "
          f"median {np.median(ucount):.0f}", flush=True)

    np.savez_compressed(
        a.out,
        pin_id=np.asarray(pid, dtype="U24")[order],
        text=np.asarray(txt, dtype=object)[order],
        lon=lons[order],
        lat=lats[order],
        gx=gx[order].astype(np.float64),                  # float64: 33.5M px range needs >24 bits of mantissa
        gy=gy[order].astype(np.float64),
        tile_key=key,                                     # sorted; use searchsorted for window queries
        tile_x=tx, tile_y=ty,
    )
    print(f"wrote {a.out} ({os.path.getsize(a.out)/1e6:.0f} MB)", flush=True)
    print("PININDEXDONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
