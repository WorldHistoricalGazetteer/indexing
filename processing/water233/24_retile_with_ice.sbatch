#!/bin/bash
#SBATCH --job-name=water233-retile
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/24-retile-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/24-retile-%j.err
#SBATCH --time=12:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=128G

# place#233 (revised) — re-tile with the ice layer. Five style layers now
# have a matching source-layer: ocean, lakes, rivers, ice.
#
# ⚠️ THE ICE EXTRACT CONTAINS 258 NON-ICE FEATURES AND THEY MUST NOT SHIP.
# `osmium tags-filter` includes referenced objects, so the glacier-only pass
# pulled in ways that carry natural=water as well. My classifier duly filed
# 258 of them as `lakes`. Those same objects were ALSO captured by the main
# water filter (nwr/natural=water) and are already in lakes.geojsonl, so
# emitting them again would silently duplicate 258 polygons -- two coincident
# copies of the same lake, invisible in any count of the output.
#
# Filtering to layer == "ice" is correct on its own terms regardless (an ice
# layer should contain ice), which is why I am not relying on the duplication
# argument alone to justify it.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"
OUT="${D}/water.mbtiles"
TMPDIR_TP="${SLURM_SCRATCH:-/tmp}/tippecanoe-water233"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0
guard_volume ix1  "${IX1_PATH}"  "${IX1_FLOOR_GB}" 30
ulimit -n 65536
mkdir -p "${TMPDIR_TP}"

echo
echo "=== separating true ice from the referenced-object bycatch ==="
python - "${D}/ice.geojsonl" "${D}/ice-only.geojsonl" <<'PY'
import collections, json, sys
src, dst = sys.argv[1], sys.argv[2]
counts = collections.Counter()
kept = 0
with open(src, encoding="utf-8") as fh, open(dst, "w", encoding="utf-8") as out:
    for line in fh:
        f = json.loads(line)
        layer = f["properties"].get("layer")
        counts[layer] += 1
        if layer == "ice":
            out.write(line); kept += 1
print("  layers present in the glacier extract:")
for k, v in counts.most_common():
    print(f"    {str(k):8s} {v:10,}")
print(f"  kept as ice : {kept:,}")
print(f"  DISCARDED   : {sum(counts.values()) - kept:,}  (already present in "
      f"lakes.geojsonl via the main water filter; emitting them here would "
      f"duplicate them)")
PY
ls -la "${D}/ice-only.geojsonl"

echo
echo "=== inputs ==="
declare -A SRC=( [ocean]="ocean-c001.geojsonl" [lakes]="lakes.geojsonl" \
                 [rivers]="rivers.geojsonl" [ice]="ice-only.geojsonl" )
INPUTS=()
for layer in ocean lakes rivers ice; do
    f="${D}/${SRC[$layer]}"
    [ -s "$f" ] || { echo "FATAL: ${f} missing or empty" >&2; exit 1; }
    echo "  ${layer}: $(wc -l < "$f") features   $(stat -c %s "$f") bytes"
    INPUTS+=( -L "${layer}:${f}" )
done

echo
echo "=== tippecanoe -Z0 -z10 ==="
# --name/--description: the previous build left the BUILD PATH in the mbtiles
# metadata, which surfaces in the served TileJSON and leaks an internal path
# to a public endpoint.
rm -f "${OUT}"
START=$(date +%s)
tippecanoe \
    --output "${OUT}" \
    --name "whg-water" \
    --description "WHG water: ocean, lakes, rivers and ice from OpenStreetMap planet 2026-07-20 (place#233)" \
    --attribution "© OpenStreetMap contributors" \
    --minimum-zoom=0 --maximum-zoom=10 \
    --coalesce-densest-as-needed \
    --drop-densest-as-needed \
    --simplification=4 \
    --detect-shared-borders \
    --temporary-directory="${TMPDIR_TP}" \
    --force \
    "${INPUTS[@]}"
echo "tippecanoe elapsed=$(( $(date +%s) - START ))s"

echo
echo "=== STEP 3 ACCEPTANCE ==="
ls -la "${OUT}"
SIZE=$(stat -c %s "${OUT}")
awk -v b="${SIZE}" 'BEGIN{printf "water.mbtiles = %.2f GB (%d bytes)\n", b/1073741824, b}'
python - "${OUT}" <<'PY'
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
md = dict(con.execute("SELECT name, value FROM metadata").fetchall())
for k in ("name", "description", "format", "minzoom", "maxzoom", "type", "attribution"):
    print(f"  {k:12s} {str(md.get(k))[:100]}")
vl = [l.get("id") for l in json.loads(md.get("json", "{}")).get("vector_layers", [])]
print(f"  vector_layers {sorted(vl)}")
n = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
print(f"  tiles         {n:,}")
print("  largest tile per zoom:")
worst = 0
for z, cnt, mx, tot in con.execute(
        "SELECT zoom_level, COUNT(*), MAX(LENGTH(tile_data)), SUM(LENGTH(tile_data)) "
        "FROM tiles GROUP BY zoom_level ORDER BY zoom_level"):
    worst = max(worst, mx)
    flag = "  <-- OVER 500 KB" if mx > 500*1024 else ""
    print(f"    z{z:<3} tiles={cnt:>8,}  max={mx/1024:9.1f} KB  total={tot/1048576:8.1f} MB{flag}")
print(f"  worst tile overall: {worst/1024:.1f} KB")
bad = []
if md.get("minzoom") != "0" or md.get("maxzoom") != "10": bad.append("not z0-10")
if str(md.get("name","")).startswith("/"): bad.append("name is still a path")
if sorted(vl) != ["ice", "lakes", "ocean", "rivers"]: bad.append(f"layers are {sorted(vl)}")
if worst > 500*1024: bad.append("a tile exceeds 500 KB (tile-join would skip it)")
if bad:
    print("  *** ACCEPTANCE PROBLEMS: " + "; ".join(bad)); sys.exit(3)
print("  OK: z0-10, four layers, no oversize tile, name is not a path")
PY
echo
echo "--- checksum for the deployer ---"
sha256sum "${OUT}"
report_volumes
echo "=== DONE $(date -Is) ==="
