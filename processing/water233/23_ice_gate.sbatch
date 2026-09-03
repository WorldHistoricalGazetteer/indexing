#!/bin/bash
#SBATCH --job-name=water233-icegate
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/23-icegate-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/23-icegate-%j.err
#SBATCH --time=08:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=64G

# place#233 (revised) — ICE, and the gate that decides whether it may ship.
#
# 🛑 THE GATE, NOT THE TILING, IS THE RISK. Replacing Natural Earth's ice
# shelves with a sparser OSM layer would be a REGRESSION THAT LOOKS LIKE
# PROGRESS: four layers improving while the fifth quietly loses coverage.
# So compare OSM's assembled glacier extent against NE's
# ne_10m_antarctic_ice_shelves BEFORE building anything shippable.
#
# ⚠️ NOBODY HAS ESTABLISHED HOW OSM MAPS ANTARCTIC ICE. It may be
# natural=glacier, it may be folded into natural=coastline, it may be sparse.
# This job answers that question with counts and area rather than assuming
# the tag yields what we want.
#
# Supplementary glacier-only pass rather than re-running the whole Step 0c:
# a planet pass costs the same either way (~23 min), but this avoids
# repeating the 50-minute inland assembly for layers that do not change.
# Tiling takes one file per layer anyway.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
OSMIUM="env LD_LIBRARY_PATH=$CONDA_PREFIX/lib osmium"

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"
PIN=/ix1/ishi/data/authorities/osm/.pin-place233/planet-pinned.osm.pbf
GLACIER="${D}/glacier-source.osm.pbf"
ICE="${D}/ice.geojsonl"
NE_DIR="${D}/ne_ice"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0
guard_volume ix1  "${IX1_PATH}"  "${IX1_FLOOR_GB}" 40

echo
echo "=== verifying the pin is still the same edition ==="
python - "${RUN_ROOT}/pin.json" "${PIN}" <<'PY'
import json, os, sys
rec = json.load(open(sys.argv[1])); st = os.stat(sys.argv[2])
bad = []
if st.st_ino  != rec["inode"]:      bad.append(f"inode {st.st_ino} != {rec['inode']}")
if st.st_size != rec["size_bytes"]: bad.append(f"size {st.st_size} != {rec['size_bytes']}")
if bad: print("FATAL: " + "; ".join(bad)); sys.exit(1)
print(f"pin ok: inode {st.st_ino}, size {st.st_size}")
PY

echo
echo "=== [0c-supplementary] tags-filter glacier from the pinned planet ==="
watchdog_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 300
START=$(date +%s)
${OSMIUM} tags-filter "${PIN}" \
    nwr/natural=glacier nwr/landuse=glacier \
    --output "${GLACIER}" --output-format pbf --overwrite
echo "tags-filter elapsed=$(( $(date +%s) - START ))s"
watchdogs_stop
ls -la "${GLACIER}"
${OSMIUM} fileinfo -e "${GLACIER}" 2>&1 | grep -E "Number of (nodes|ways|relations)|replication"

echo
echo "=== census: what does the tag actually yield, and WHERE? ==="
python "${RUN_ROOT}/scripts/count_water.py" "${GLACIER}" \
    --out "${D}/counts.glacier.json" 2>&1 | tail -12 || true

echo
echo "=== assembling glacier areas -> ice layer ==="
python "${RUN_ROOT}/scripts/assemble_inland_water.py" "${GLACIER}" \
    --out "${ICE}" --stats-out "${D}/assembly.ice.json" --progress-every 50000
ls -la "${ICE}"

echo
echo "=== fetching Natural Earth ice shelves for the comparison ==="
mkdir -p "${NE_DIR}"
cd "${NE_DIR}"
NE_OK=0
for u in \
  "https://naciscdn.org/naturalearth/10m/physical/ne_10m_antarctic_ice_shelves_polys.zip" \
  "https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_antarctic_ice_shelves_polys.zip" ; do
    echo "  trying ${u}"
    if curl -sSL -m 300 -o ne_ice.zip "${u}" && unzip -oq ne_ice.zip; then NE_OK=1; break; fi
done
ls -la "${NE_DIR}" | head
if [ "${NE_OK}" -eq 1 ] && ls "${NE_DIR}"/*.shp >/dev/null 2>&1; then
    conda activate whg-coastline
    ogr2ogr -f GeoJSONSeq "${D}/ne_ice.geojsonl" "$(ls "${NE_DIR}"/*.shp | head -1)" -t_srs EPSG:4326
    conda activate whg
    echo "  NE features: $(wc -l < "${D}/ne_ice.geojsonl")"
else
    echo "  ⚠️ could not fetch Natural Earth ice shelves"
fi

echo
echo "=== 🛑 THE GATE: is OSM's ice at least as complete as NE's? ==="
cd "${RUN_ROOT}"
python - "${ICE}" "${D}/ne_ice.geojsonl" <<'PY'
import json, math, os, sys

def ring_deg2(r):
    s = 0.0
    for i in range(len(r) - 1):
        s += r[i][0]*r[i+1][1] - r[i+1][0]*r[i][1]
    return abs(s) / 2.0

M = 111320.0
def area_m2(g):
    t = g.get("type"); polys = g.get("coordinates") or []
    if t == "Polygon": polys = [polys]
    elif t != "MultiPolygon": return 0.0, 0.0
    tot = 0.0; lat_sum = 0.0; n = 0
    for poly in polys:
        if not poly: continue
        lats = [p[1] for p in poly[0]]
        if not lats: continue
        clat = sum(lats)/len(lats)
        lat_sum += clat; n += 1
        c = math.cos(math.radians(clat))
        a = ring_deg2(poly[0])
        for h in poly[1:]: a -= ring_deg2(h)
        tot += max(a, 0.0) * (M**2) * c
    return tot, (lat_sum/n if n else 0.0)

def summarise(path, label):
    if not os.path.exists(path):
        print(f"  {label}: FILE ABSENT"); return None
    n = 0; total = 0.0; ant = 0.0; ant_n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            g = json.loads(line).get("geometry") or {}
            a, clat = area_m2(g)
            n += 1; total += a
            if clat < -60.0:
                ant += a; ant_n += 1
    print(f"  {label}")
    print(f"    features            {n:12,}")
    print(f"    total area          {total/1e6:12,.0f} km2")
    print(f"    features below 60S  {ant_n:12,}")
    print(f"    area below 60S      {ant/1e6:12,.0f} km2")
    return n, total, ant, ant_n

osm = summarise(sys.argv[1], "OSM natural=glacier / landuse=glacier")
print()
ne  = summarise(sys.argv[2], "Natural Earth ne_10m_antarctic_ice_shelves")
print()
if osm is None or ne is None:
    print("  GATE UNDECIDED: one side is missing. Do not tile ice.")
    sys.exit(4)
_, _, osm_ant, _ = osm
_, ne_total, _, _ = ne
# NE's layer is Antarctic ice shelves only, so compare it against OSM's
# below-60S extent rather than against OSM's global glacier total (which
# includes every alpine glacier on Earth and would flatter the comparison).
ratio = osm_ant / ne_total if ne_total else 0.0
print(f"  OSM below-60S ice vs NE ice shelves: {osm_ant/1e6:,.0f} / "
      f"{ne_total/1e6:,.0f} km2 = {ratio:.3f}")
print()
if ratio >= 0.95:
    print("  ✅ GATE PASSES: OSM's Antarctic ice is at least as extensive as")
    print("  Natural Earth's. Replacing the layer does not lose coverage.")
elif ratio >= 0.5:
    print("  ⚠️ GATE MARGINAL: OSM has substantial but not equivalent Antarctic")
    print("  coverage. STOP AND REPORT -- this needs a judgement, not a default.")
    sys.exit(3)
else:
    print("  🛑 GATE FAILS: OSM's Antarctic ice is materially sparser than")
    print("  Natural Earth's. Shipping it would be a REGRESSION that looks like")
    print("  progress. Leave `ice` on whg-ne-basic and mitigate the ocean/ice")
    print("  draw-order artefact another way. DO NOT TILE ICE.")
    sys.exit(3)
PY
report_volumes
echo "=== DONE $(date -Is) ==="
