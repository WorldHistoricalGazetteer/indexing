#!/bin/bash
#SBATCH --job-name=water236-damage
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/34-damage-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/34-damage-%j.err
#SBATCH --time=04:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=32G

# place#236 — ⚠️ MONOTONICITY IS NOT A SUFFICIENT TEST, and the acceptance
# check I had written for the rebuild would have passed a broken build.
#
# SG reports square land/sea borders at z6.89 -- ABOVE the z6 threshold where
# 0c4de61 falls back to Natural Earth, so that is the OSM ocean being seen.
# My measurements called z6 healthy because total bytes rose. But per layer:
#
#     ocean at z6, tiled ALONE          3,733,802 vertices
#     ocean at z6, with lakes present     148,670 vertices   = 4.0%
#
# and 7,851 -> 29,465 -> 52,666 -> 148,670 IS monotonic. A sequence can rise
# at every step while sitting two orders of magnitude below where it belongs.
# The troughs at z3/z5 were the loudest symptom, not the extent of the damage.
#
# THE RIGHT TEST is the RATIO against the same ocean tiled alone -- an
# absolute reference built from identical input with identical flags, so the
# only difference is the competition. Measure that on the SHIPPED tileset,
# per zoom, so the damage SG can see has a number at every zoom.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
D="${RUN_ROOT}/data"
EXP="${D}/param-exp"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="

python - "${D}/water.mbtiles" "${EXP}/ocean-E.mbtiles" <<'PY'
import collections, json, re, subprocess, sys
zoom_re = re.compile(r'"zoom":\s*(\d+)'); lay_re = re.compile(r'"layer":\s*"([^"]+)"')

def ocean_profile(mb, maxz=8):
    verts = collections.Counter(); feats = collections.Counter()
    z = lay = None
    p = subprocess.Popen(["tippecanoe-decode", "-Z", "0", "-z", str(maxz), mb],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, bufsize=1 << 20)
    for line in p.stdout:
        if '"zoom":' in line and '"x":' in line:
            m = zoom_re.search(line)
            if m: z = int(m.group(1))
            continue
        if '"layer":' in line and '"extent"' in line:
            m = lay_re.search(line)
            if m: lay = m.group(1)
            continue
        if lay != "ocean" or '"geometry"' not in line or z is None: continue
        s = line.strip().rstrip(",")
        if not s.startswith("{"): continue
        try: obj = json.loads(s)
        except Exception: continue
        n = 0; stack = [(obj.get("geometry") or {}).get("coordinates", [])]
        while stack:
            c = stack.pop()
            if isinstance(c, list):
                if c and isinstance(c[0], (int, float)): n += 1
                else: stack.extend(c)
        verts[z] += n; feats[z] += 1
    p.wait()
    return verts, feats

print("  decoding SHIPPED water.mbtiles (ocean layer, z0-8) ...", flush=True)
sv, sf = ocean_profile(sys.argv[1])
print("  decoding REFERENCE ocean-alone (ocean layer, z0-8) ...", flush=True)
rv, rf = ocean_profile(sys.argv[2])

print()
print("  OCEAN DETAIL RETAINED IN THE SHIPPED TILESET")
print(f"  {'z':<4}{'shipped feats':>15}{'shipped verts':>16}"
      f"{'alone verts':>15}{'retained':>11}")
worst = (999.0, None)
for z in range(9):
    s, r = sv[z], rv[z]
    pct = (100.0 * s / r) if r else float("nan")
    if r and pct < worst[0]: worst = (pct, z)
    flag = "  <<" if r and pct < 50 else ""
    print(f"  z{z:<3}{sf[z]:>15,}{s:>16,}{r:>15,}{pct:>10.1f}%{flag}")
print()
print(f"  worst retention: {worst[0]:.1f}% at z{worst[1]}")
print()
print("  Note both sequences RISE with zoom. Monotonicity is satisfied")
print("  throughout and tells you nothing about this; the reference ratio is")
print("  what shows the ocean is a fraction of itself at every zoom.")
PY
echo "=== DONE $(date -Is) ==="
