#!/bin/bash
#SBATCH --job-name=water236-rebuild
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/30-rebuild-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/30-rebuild-%j.err
#SBATCH --time=12:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=128G

# place#236 — rebuild water.mbtiles with the layer-starvation defect fixed.
#
# CAUSE, established by ablation rather than inferred from the flag list:
#   ocean alone, shipped flags, z0-6   -> monotonic
#   ocean alone, shipped flags, z0-10  -> monotonic
#   four layers, shipped flags, z0-10  -> troughs at z3 and z5
# --drop-densest-as-needed operates on the WHOLE TILE. 22.4 M lakes, 73.5%
# of them under a hectare, blow the 500 KB budget at z3 where a pixel is
# ~19.6 km; the dropper evicts indiscriminately and the coastline goes with
# ponds that could never be drawn there.
#
# FIX: per-feature minzoom on lakes/rivers/ice from each feature's longest
# bbox side, so nothing enters the pyramid above the zoom at which it spans a
# pixel. ⚠️ Written as a FEATURE-LEVEL "tippecanoe":{"minzoom":N} member --
# a PROPERTY named tippecanoe:minzoom is silently IGNORED (proved on a
# 1,000-feature sample, job 11115929). Verified at z0-6: ocean restored to
# 100.0% of its tiled-alone detail at every zoom.
#
# 🛑 NO minzoom ON THE OCEAN. Ocean is a FILL: a missing piece is a HOLE IN
# THE SEA, not an absent pond. Its 57,413 pieces are split by
# osmcoastline --max-points, so piece size reflects vertex density rather
# than visual importance, and thresholding on it would punch holes in open
# water. Ocean alone already tiles monotonically at z0-10 (config E), so it
# needs no help.
#
# Flags kept EXACTLY as shipped. They were exonerated by ablation:
# --coalesce-densest-as-needed produced a byte-identical tileset, and the
# full shipped flag set gives a monotonic ocean when tiled alone. Changing
# them now would be changing something that measurement cleared.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"
OUT="${D}/water-v2.mbtiles"
TMPDIR_TP="${SLURM_SCRATCH:-/tmp}/tippecanoe-v2"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0
guard_volume ix1  "${IX1_PATH}"  "${IX1_FLOOR_GB}" 40
mkdir -p "${TMPDIR_TP}"; ulimit -n 65536

for f in ocean-c001.geojsonl lakes-mz2.geojsonl rivers-mz2.geojsonl ice-mz2.geojsonl; do
    [ -s "${D}/${f}" ] || { echo "FATAL: ${D}/${f} missing — run 29 first" >&2; exit 1; }
    echo "  $(printf '%-22s' "$f") $(wc -l < "${D}/${f}") features"
done

echo
echo "=== tippecanoe -Z0 -z10 ==="
rm -f "${OUT}"
t0=$(date +%s)
tippecanoe --output "${OUT}" \
    --name "whg-water" \
    --description "WHG water: ocean, lakes, rivers and ice from OpenStreetMap planet 2026-07-20 (place#233, rebuilt place#236)" \
    --attribution "© OpenStreetMap contributors" \
    --minimum-zoom=0 --maximum-zoom=10 \
    --coalesce-densest-as-needed --drop-densest-as-needed \
    --simplification=4 --detect-shared-borders \
    --temporary-directory="${TMPDIR_TP}" --force \
    -L "ocean:${D}/ocean-c001.geojsonl" \
    -L "lakes:${D}/lakes-mz2.geojsonl" \
    -L "rivers:${D}/rivers-mz2.geojsonl" \
    -L "ice:${D}/ice-mz2.geojsonl" 2>&1 | tail -6
echo "tippecanoe elapsed=$(( $(date +%s) - t0 ))s"

echo
echo "=== ACCEPTANCE ==="
ls -la "${OUT}"
python - "${OUT}" <<'PY'
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
md = dict(con.execute("SELECT name, value FROM metadata").fetchall())
for k in ("name", "format", "minzoom", "maxzoom", "attribution"):
    print(f"  {k:12s} {str(md.get(k))[:90]}")
vl = sorted(l.get("id") for l in json.loads(md.get("json","{}")).get("vector_layers", []))
print(f"  vector_layers {vl}")
print(f"  tiles         {con.execute('SELECT COUNT(*) FROM tiles').fetchone()[0]:,}")
worst = 0
for z, cnt, tot, mx in con.execute(
        "SELECT zoom_level, COUNT(*), SUM(LENGTH(tile_data)), MAX(LENGTH(tile_data)) "
        "FROM tiles GROUP BY zoom_level ORDER BY zoom_level"):
    worst = max(worst, mx)
    print(f"    z{z:<3}{cnt:>9,} tiles{tot:>15,} bytes  max {mx/1024:>8.1f}KB")
bad = []
if md.get("minzoom") != "0" or md.get("maxzoom") != "10": bad.append("not z0-10")
if vl != ["ice","lakes","ocean","rivers"]: bad.append(f"layers {vl}")
if str(md.get("name","")).startswith("/"): bad.append("name is a path")
if worst > 500*1024: bad.append(f"tile over 500KB ({worst/1024:.1f}KB)")
if bad:
    print("  *** FAILED: " + "; ".join(bad)); sys.exit(3)
print("  metadata OK")
PY

echo
echo "=== 🛑 THE ACCEPTANCE THAT MATTERS: ocean detail vs ocean tiled ALONE ==="
# ⚠️ NOT monotonicity. The shipped tileset was monotonic from z4 up while the
# ocean sat at 4% of correct -- a sequence can rise at every step and still be
# two orders of magnitude low. Monotonicity is a property of SHAPE; this
# defect is in MAGNITUDE, and a shape test cannot bound a magnitude error.
#
# The reference is the same ocean, same flags, same input, tiled with nothing
# competing (config E). It is not a threshold anyone chose -- it is what this
# input produces under these flags, so ~100% is the criterion rather than an
# argued number.
python - "${OUT}" "${D}/param-exp/ocean-E.mbtiles" <<'PY'
import collections, json, re, subprocess, sys
zoom_re = re.compile(r'"zoom":\s*(\d+)'); lay_re = re.compile(r'"layer":\s*"([^"]+)"')
def ocean(mb):
    v = collections.Counter(); z = lay = None
    p = subprocess.Popen(["tippecanoe-decode", mb], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1 << 20)
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
        try: o = json.loads(s)
        except Exception: continue
        n = 0; st = [(o.get("geometry") or {}).get("coordinates", [])]
        while st:
            c = st.pop()
            if isinstance(c, list):
                if c and isinstance(c[0], (int, float)): n += 1
                else: st.extend(c)
        v[z] += n
    p.wait(); return v
print("  decoding rebuild ...", flush=True); new = ocean(sys.argv[1])
print("  decoding reference ...", flush=True); ref = ocean(sys.argv[2])
print()
print(f"  {'z':<4}{'rebuild':>15}{'alone':>15}{'retained':>11}")
fail = []
for z in range(11):
    r = ref[z]
    if not r: continue
    pct = 100.0 * new[z] / r
    flag = ""
    if pct < 95.0: fail.append(z); flag = "  <<LOW"
    print(f"  z{z:<3}{new[z]:>15,}{r:>15,}{pct:>10.1f}%{flag}")
print()
if fail:
    print(f"  *** FAILED: ocean below 95% of tiled-alone detail at z{', z'.join(map(str,fail))}")
    print( "  *** The ocean is still being starved. Do not deploy.")
    sys.exit(3)
print("  PASSED: ocean retains >=95% of its tiled-alone detail at every zoom.")
PY

echo
echo "--- checksum for the deployer ---"
sha256sum "${OUT}"
report_volumes
echo "=== DONE $(date -Is) ==="
