#!/bin/bash
#SBATCH --job-name=water236-mzsyntax
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/32-mzsyntax-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/32-mzsyntax-%j.err
#SBATCH --time=02:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G

# place#236 — ⚠️ MY minzoom WAS PROBABLY NEVER APPLIED.
#
# I set a PROPERTY named "tippecanoe:minzoom". The evidence it was ignored is
# arithmetic, not doubt: I computed 11,292 lakes with minzoom <= 3, and 53,966
# lake features appear at z3. If the attribute were honoured that is
# impossible.
#
# tippecanoe takes a FEATURE-LEVEL member -- {"type":"Feature","tippecanoe":
# {"minzoom":N},"properties":{...}} -- while a property of that name needs
# --use-attribute-for-minzoom. Verify which this build accepts on a tiny
# sample where the answer is countable, BEFORE rewriting 22 M features again.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
D="${RUN_ROOT}/data"
T="${SLURM_SCRATCH:-/tmp}/mzsyntax"; mkdir -p "$T"
ulimit -n 65536

echo "=== node $(hostname) job ${SLURM_JOB_ID} ==="
echo "=== what does this tippecanoe document? ==="
tippecanoe --help 2>&1 | grep -iE "minzoom|attribute" | head -20 || true

echo
echo "=== build a countable sample: 1,000 features, ALL forced to minzoom 6 ==="
# If the form works, z0-5 must be EMPTY and z6 must hold them. That is a
# prediction with a number, not an impression.
python - "${D}/lakes.geojsonl" "$T" <<'PY'
import json, sys
src, T = sys.argv[1], sys.argv[2]
feats = []
with open(src, encoding="utf-8") as fh:
    for i, line in enumerate(fh):
        if i >= 1000: break
        feats.append(json.loads(line))
# form A: property named tippecanoe:minzoom
with open(f"{T}/form_prop.geojsonl", "w") as out:
    for f in feats:
        g = json.loads(json.dumps(f))
        g["properties"]["tippecanoe:minzoom"] = 6
        out.write(json.dumps(g, separators=(",", ":")) + "\n")
# form B: feature-level "tippecanoe" member
with open(f"{T}/form_member.geojsonl", "w") as out:
    for f in feats:
        g = json.loads(json.dumps(f))
        g["tippecanoe"] = {"minzoom": 6}
        out.write(json.dumps(g, separators=(",", ":")) + "\n")
print(f"  wrote {len(feats)} features in both forms")
PY

for form in prop member; do
  echo
  echo "--- form ${form} ---"
  rm -f "$T/${form}.mbtiles"
  tippecanoe --output "$T/${form}.mbtiles" --name t -Z0 -z8 --force --quiet \
      -L "lakes:$T/form_${form}.geojsonl" 2>&1 | tail -2
  python - "$T/${form}.mbtiles" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
rows = list(con.execute("SELECT zoom_level, COUNT(*), SUM(LENGTH(tile_data)) "
                        "FROM tiles GROUP BY zoom_level ORDER BY zoom_level"))
low = sum(t for z, c, t in rows if z < 6)
print("    zooms present:", [z for z, c, t in rows])
print(f"    bytes below z6: {low:,}   <-- MUST be 0 if minzoom 6 was honoured")
print("    VERDICT:", "HONOURED" if low == 0 else "IGNORED")
PY
done
echo "=== DONE ==="
