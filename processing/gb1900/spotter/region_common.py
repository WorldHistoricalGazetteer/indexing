import os, io, math, urllib.request
from PIL import Image
from tiling import lonlat_to_tile, tile_to_lonlat, Z

# ---- region definition: 40x40 z16 tiles centred ~Shrewsbury, 10x10 mosaics ----
X0, Y0 = 32246, 21416
NT = 40          # tiles per side
MOS = 4          # mosaic = 4x4 tiles -> 1024px
NMOS = NT // MOS # 10 mosaics per side

BASE = "/vast/ishi/gb1900/probe/mapreader_text/region"
SHARED_CACHE = "/vast/ishi/gb1900/tiles/16"   # READ-ONLY
REGION_CACHE = BASE + "/tiles"                 # our own fetch cache
MOSAIC_DIR = BASE + "/mosaics"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/{z}/{x}/{y}.png"

def region_bbox():
    w, n = tile_to_lonlat(X0, Y0)
    e, s = tile_to_lonlat(X0 + NT, Y0 + NT)
    return (w, s, e, n)

def _load_png(p):
    try:
        return Image.open(p).convert("RGB")
    except Exception:
        return None

def fetch_tile(x, y):
    p = os.path.join(SHARED_CACHE, str(x), str(y) + ".png")
    if os.path.exists(p):
        im = _load_png(p)
        if im is not None:
            return im
    rp = os.path.join(REGION_CACHE, str(x), str(y) + ".png")
    if os.path.exists(rp):
        im = _load_png(rp)
        if im is not None:
            return im
    url = S3.format(z=Z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": "whg-region-probe"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            data = r.read()
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    try:
        im.save(rp)
    except Exception:
        pass
    return im

def mosaic_origin_tile(i, j):
    return X0 + i * MOS, Y0 + j * MOS

def build_mosaic(i, j):
    tx0, ty0 = mosaic_origin_tile(i, j)
    size = MOS * 256
    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    missing = 0
    for a in range(MOS):
        for b in range(MOS):
            t = fetch_tile(tx0 + a, ty0 + b)
            if t is None:
                missing += 1
            else:
                canvas.paste(t, (a * 256, b * 256))
    return canvas, tx0, ty0, missing

def global_px_to_lonlat(gpx, gpy):
    return tile_to_lonlat(gpx / 256.0, gpy / 256.0)

def lonlat_to_global_px(lon, lat):
    fx, fy = lonlat_to_tile(lon, lat)
    return fx * 256.0, fy * 256.0

def all_mosaics():
    for i in range(NMOS):
        for j in range(NMOS):
            yield i, j
