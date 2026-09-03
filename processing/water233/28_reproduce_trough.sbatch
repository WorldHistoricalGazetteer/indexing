#!/bin/bash
#SBATCH --job-name=water236-repro
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/28-repro-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/28-repro-%j.err
#SBATCH --time=06:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=64G

# place#236 — ⚠️ MY KNOWN-BAD CONTROL DID NOT REPRODUCE THE DEFECT.
#
# Config A used the shipped flags exactly and came out MONOTONIC. By the rule
# I wrote into that job, every conclusion from configs B/C/D is void: they
# were compared against a control that does not exhibit the thing being
# fixed. --coalesce-densest-as-needed producing a byte-identical tileset to
# not using it is still a real observation, but it is not a diagnosis.
#
# Two differences remain between the experiment and the real build:
#   (1) ocean ALONE vs four layers sharing one tile budget
#   (2) --maximum-zoom=6 vs 10
#
# (2) is the cheaper to test and has a mechanism: tippecanoe thins features
# below BASEZOOM, which defaults to maxzoom. z3 in a z0-10 build sits seven
# levels below basezoom; in a z0-6 build, three. That alone could change z3
# drastically while leaving the flags blameless.
#
# So: ocean alone, shipped flags, z0-10. If the trough appears, the cause is
# the zoom range and NOT layer competition. If it does not, the remaining
# suspect is the four layers competing for one tile budget -- at z3 the
# 22.4 M lakes features would dominate and the ocean would be thinned with
# them.
#
# Also reports BYTES per zoom, because the defect was first visible in bytes
# (my acceptance table: z2 2.3 MB -> z3 0.8 MB) and vertices and bytes need
# not move together.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"
EXP="${D}/param-exp"
TMPDIR_TP="${SLURM_SCRATCH:-/tmp}/tippecanoe-repro"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 20
mkdir -p "${EXP}" "${TMPDIR_TP}"
ulimit -n 65536

echo "=== config E: ocean alone, shipped flags, z0-10 ==="
rm -f "${EXP}/ocean-E.mbtiles"
t0=$(date +%s)
tippecanoe --output "${EXP}/ocean-E.mbtiles" --name exp-E \
    --minimum-zoom=0 --maximum-zoom=10 \
    --coalesce-densest-as-needed --drop-densest-as-needed \
    --simplification=4 --detect-shared-borders \
    --temporary-directory="${TMPDIR_TP}" --force --quiet \
    -L "ocean:${D}/ocean-c001.geojsonl" 2>&1 | tail -3
echo "built in $(( $(date +%s) - t0 ))s"

echo
echo "=== per-zoom BYTES: A (z0-6) vs E (z0-10) vs the SHIPPED build ==="
python - "${EXP}/ocean-A.mbtiles" "${EXP}/ocean-E.mbtiles" "${D}/water.mbtiles" <<'PY'
import sqlite3, sys
def prof(path, label):
    con = sqlite3.connect(path)
    rows = list(con.execute(
        "SELECT zoom_level, COUNT(*), SUM(LENGTH(tile_data)), MAX(LENGTH(tile_data)) "
        "FROM tiles GROUP BY zoom_level ORDER BY zoom_level"))
    print(f"\n  {label}")
    print(f"    {'z':<4}{'tiles':>10}{'total bytes':>16}{'vs prev':>10}{'max tile':>12}")
    prev = None
    for z, cnt, tot, mx in rows:
        r = f"{tot/prev:.2f}x" if prev else "-"
        flag = "  <<TROUGH" if prev and tot < prev else ""
        print(f"    z{z:<3}{cnt:>10,}{tot:>16,}{r:>10}{mx/1024:>10.1f}KB{flag}")
        prev = tot
    return rows
prof(sys.argv[1], "A: ocean alone, shipped flags, z0-6")
prof(sys.argv[2], "E: ocean alone, shipped flags, z0-10")
prof(sys.argv[3], "SHIPPED: four layers, shipped flags, z0-10 (all layers combined)")
PY

echo
echo "=== per-zoom OCEAN vertices in E ==="
python - "${EXP}/ocean-E.mbtiles" <<'PY'
import collections, json, re, subprocess, sys
zoom_re = re.compile(r'"zoom":\s*(\d+)')
verts = collections.Counter(); z = None
p = subprocess.Popen(["tippecanoe-decode", sys.argv[1]], stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, bufsize=1 << 20)
for line in p.stdout:
    if '"zoom":' in line and '"x":' in line:
        m = zoom_re.search(line)
        if m: z = int(m.group(1))
        continue
    if '"geometry"' not in line or z is None: continue
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
    verts[z] += n
p.wait()
prev = None
print(f"    {'z':<4}{'vertices':>14}{'vs prev':>10}")
for z in sorted(verts):
    r = f"{verts[z]/prev:.2f}x" if prev else "-"
    flag = "  <<TROUGH" if prev and verts[z] < prev else ""
    print(f"    z{z:<3}{verts[z]:>14,}{r:>10}{flag}")
    prev = verts[z]
PY
report_volumes
echo "=== DONE $(date -Is) ==="
