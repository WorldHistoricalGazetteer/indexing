#!/bin/bash
#SBATCH --job-name=water233-verifylayers
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/36-verifyv2-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/36-verifyv2-%j.err
#SBATCH --time=02:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G

# place#233 — is each layer actually IN the tiles, or merely declared?
#
# water.mbtiles metadata lists ocean, lakes and rivers as vector_layers. That
# is a DECLARATION written from the inputs, not a measurement of the output.
# A layer whose features were all dropped, or that failed to write, would
# still appear there -- the same shape as every other defect this campaign
# has found: a well-formed answer to a question nobody asked.
#
# So decode real tiles and count features per layer, at several zooms and at
# known locations, including the Neum viewport used for the pre-build control.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
MB="${RUN_ROOT}/data/water-v2.mbtiles"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
[ -s "${MB}" ] || { echo "FATAL: water.mbtiles missing" >&2; exit 1; }

python - "${MB}" <<'PY'
import collections, gzip, math, sqlite3, sys, zlib

con = sqlite3.connect(sys.argv[1])

def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lr = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lr) + 1/math.cos(lr)) / math.pi) / 2.0 * n)
    return x, y

def get_tile(z, x, y):
    # mbtiles stores TMS y
    ty = (2 ** z - 1) - y
    r = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND "
                    "tile_column=? AND tile_row=?", (z, x, ty)).fetchone()
    if not r:
        return None
    d = r[0]
    if d[:2] == b"\x1f\x8b":
        d = gzip.decompress(d)
    return d

def layers_in(buf):
    """Minimal MVT scan: top-level field 3 (layer), then its field 1 (name)
    and count of field 2 (feature) entries. Avoids a protobuf dependency."""
    out = collections.Counter()
    def varint(b, i):
        s = r = 0
        while True:
            x = b[i]; i += 1
            r |= (x & 0x7F) << s
            if not x & 0x80: return r, i
            s += 7
    i = 0
    while i < len(buf):
        key, i = varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 2:
            ln, i = varint(buf, i); payload = buf[i:i+ln]; i += ln
            if fn == 3:
                name, nfeat, j = None, 0, 0
                while j < len(payload):
                    k2, j = varint(payload, j)
                    f2, w2 = k2 >> 3, k2 & 7
                    if w2 == 2:
                        l2, j = varint(payload, j); v = payload[j:j+l2]; j += l2
                        if f2 == 1: name = v.decode("utf-8", "replace")
                        elif f2 == 2: nfeat += 1
                    elif w2 == 0:
                        _, j = varint(payload, j)
                    else:
                        break
                if name: out[name] += nfeat
        elif wt == 0:
            _, i = varint(buf, i)
        else:
            break
    return out

PROBES = [
    # Ice probes first: the layer added in the revised scope, and the one
    # whose absence the earlier probe set could not have detected.
    # ⚠️ NOT lon=180.0: x = (lon+180)/360*n gives exactly n at the
    # antimeridian, which is out of range (valid 0..n-1), so every zoom
    # returned NO TILE and looked like missing ice. Probe bug, not data gap.
    ("Ross Ice Shelf",          -175.0, -81.5, [4, 6, 8]),
    ("Ross, east side",          170.0, -78.0, [4, 6, 8]),
    ("Filchner-Ronne Ice Shelf",  -60.0, -79.0, [4, 6, 8]),
    ("Greenland interior",       -42.0,  72.0, [6, 8, 10]),
    ("Alps (Aletsch)",             8.05, 46.50, [8, 10]),
    ("Neum  (control-3 viewport)", 17.62, 42.92, [8, 9, 10]),
    ("open Pacific",             -140.0,  0.0,  [4, 8, 10]),
    ("Amazon basin",              -60.0, -3.0,  [8, 10]),
    ("Finnish Lakeland",           28.5, 61.6,  [8, 10]),
    ("Nile delta",                 31.2, 31.1,  [8, 10]),
]
seen_total = collections.Counter()
for label, lon, lat, zooms in PROBES:
    print(f"  {label}")
    for z in zooms:
        x, y = lonlat_to_tile(lon, lat, z)
        buf = get_tile(z, x, y)
        if buf is None:
            print(f"    z{z:<3} {x}/{y}: NO TILE")
            continue
        c = layers_in(buf)
        seen_total.update(c)
        desc = "  ".join(f"{k}={v}" for k, v in sorted(c.items())) or "(no layers)"
        print(f"    z{z:<3} {x}/{y}: {len(buf):7,} B   {desc}")
    print()

print("  layers observed across all probed tiles:")
for k, v in sorted(seen_total.items()):
    print(f"    {k:8s} {v:8,} features")
declared = {"ocean", "lakes", "rivers", "ice"}
missing = declared - set(seen_total)
print()
if missing:
    print(f"  *** DECLARED BUT NEVER OBSERVED: {sorted(missing)}")
    print(f"  *** Present in metadata, absent from every probed tile.")
    sys.exit(3)
print(f"  All {len(declared)} declared layers were observed carrying real features.")
PY
echo "=== DONE $(date -Is) ==="
