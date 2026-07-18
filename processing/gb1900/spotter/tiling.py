import math, os, io, urllib.request
from PIL import Image

TILE_CACHE = "/vast/ishi/gb1900/tiles/16"
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/{z}/{x}/{y}.png"
Z = 16

def lonlat_to_tile(lon, lat, z=Z):
    n = 2**z
    x = (lon + 180.0)/360.0 * n
    latr = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(latr))/math.pi)/2.0 * n
    return x, y  # float

def tile_to_lonlat(x, y, z=Z):
    n = 2**z
    lon = x/n*360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi*(1 - 2*y/n))))
    return lon, lat

def fetch_tile(x, y, z=Z):
    p = os.path.join(TILE_CACHE, str(x), f"{y}.png")
    if os.path.exists(p):
        try: return Image.open(p).convert("RGB")
        except Exception: pass
    url = S3.format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent":"whg-probe"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return Image.open(io.BytesIO(data)).convert("RGB")

def build_mosaic(lon, lat, grid=4, z=Z):
    """grid x grid tiles centered on the tile containing (lon,lat). Returns
    (PIL image gridx256, x0, y0, bbox=(w,s,e,n)) where x0,y0 = top-left tile idx."""
    fx, fy = lonlat_to_tile(lon, lat, z)
    cx, cy = int(fx), int(fy)
    half = grid//2
    x0 = cx - half; y0 = cy - half
    size = grid*256
    canvas = Image.new("RGB", (size, size), (255,255,255))
    missing = 0
    for i in range(grid):
        for j in range(grid):
            tx, ty = x0+i, y0+j
            try:
                t = fetch_tile(tx, ty, z)
                canvas.paste(t, (i*256, j*256))
            except Exception:
                missing += 1
    w, n = tile_to_lonlat(x0, y0, z)
    e, s = tile_to_lonlat(x0+grid, y0+grid, z)
    return canvas, x0, y0, (w, s, e, n), missing

def px_to_lonlat(px, py, x0, y0, z=Z):
    tx = x0 + px/256.0
    ty = y0 + py/256.0
    return tile_to_lonlat(tx, ty, z)
