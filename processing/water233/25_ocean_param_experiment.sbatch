#!/bin/bash
#SBATCH --job-name=water236-params
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/25-params-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/25-params-%j.err
#SBATCH --time=06:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=64G

# place#236 — find the build parameter that blocks the coastline at z3/z5.
#
# The defect is at LOW zoom, so this tiles OCEAN ONLY at z0-6 under four
# configurations and measures per-zoom vertex counts. Minutes per config
# instead of 35 minutes for a full rebuild, so the parameter can be
# identified by experiment rather than by argument.
#
# ⚠️ CONFIG A DELIBERATELY REPRODUCES THE DEFECT. A fix that is only ever
# compared against a remembered description of the problem is not measured
# against anything. If A does not show the trough, the defect is not in these
# parameters and every conclusion below is void -- which is the single most
# useful thing this job can tell me.
#
# Suspects named by the issue, from the build's own generator_options:
#   --coalesce-densest-as-needed   merges adjacent features when over budget
#   --simplification=4             four times tippecanoe's default of 1
# I have added --detect-shared-borders as a third: it changes how borders
# between adjacent polygons are simplified, and "coastline degenerating
# toward its bounding box" is a shared-border story as much as a
# simplification one. Not named in the issue; my addition, and if D differs
# from C that is where it shows.
#
# ⚠️ Validation is per-zoom VERTEX COUNTS, never area. An areal check was run
# on the original and passed: blocks and a coastline enclosing the same area
# score identically. Area is insensitive to shape.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"
EXP="${D}/param-exp"
OCEAN="${D}/ocean-c001.geojsonl"
TMPDIR_TP="${SLURM_SCRATCH:-/tmp}/tippecanoe-exp"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 20
[ -s "${OCEAN}" ] || { echo "FATAL: ${OCEAN} missing" >&2; exit 1; }
mkdir -p "${EXP}" "${TMPDIR_TP}"
ulimit -n 65536
command -v tippecanoe-decode >/dev/null || { echo "FATAL: tippecanoe-decode missing" >&2; exit 1; }

run_cfg () {
    local tag="$1"; shift
    local out="${EXP}/ocean-${tag}.mbtiles"
    echo
    echo "--- config ${tag}: $* ---"
    rm -f "${out}"
    local t0=$(date +%s)
    tippecanoe --output "${out}" --name "exp-${tag}" \
        --minimum-zoom=0 --maximum-zoom=6 \
        --temporary-directory="${TMPDIR_TP}" --force --quiet \
        "$@" -L "ocean:${OCEAN}" 2>&1 | tail -3
    echo "    built in $(( $(date +%s) - t0 ))s, $(stat -c %s "${out}") bytes"
}

# A reproduces the shipped build's flags exactly (minus zoom range).
run_cfg A --coalesce-densest-as-needed --drop-densest-as-needed \
          --simplification=4 --detect-shared-borders
run_cfg B --drop-densest-as-needed --simplification=4 --detect-shared-borders
run_cfg C --drop-densest-as-needed --detect-shared-borders
run_cfg D --drop-densest-as-needed

echo
echo "=== per-zoom VERTEX COUNTS in fixed windows ==="
python - "${EXP}" <<'PY'
import glob, json, math, os, subprocess, sys

EXP = sys.argv[1]
WINDOWS = [("Gulf/New Orleans", -90.07, 29.95), ("NW Europe", 0.0, 52.0),
           ("SE Australia", 150.0, -35.0)]

def tile_of(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lr = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lr) + 1/math.cos(lr)) / math.pi) / 2.0 * n)
    return min(x, n-1), min(y, n-1)

def vertices(mb, z, x, y):
    """Decode one tile and count ocean coordinates. tippecanoe-decode is the
    reference decoder, so this does not depend on my own MVT parsing."""
    try:
        out = subprocess.run(["tippecanoe-decode", "-f", mb, str(z), str(x), str(y)],
                             capture_output=True, text=True, timeout=180)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        n = 0
        for line in out.stdout.splitlines():
            line = line.strip().rstrip(",")
            if '"coordinates"' not in line:
                continue
            try:
                obj = json.loads(line if line.startswith("{") else "{" + line + "}")
            except Exception:
                continue
            def walk(c):
                nonlocal n
                if isinstance(c, list) and c and isinstance(c[0], (int, float)):
                    n += 1
                elif isinstance(c, list):
                    for v in c: walk(v)
            walk(obj.get("coordinates", []))
        return n
    except Exception:
        return None

cfgs = sorted(glob.glob(os.path.join(EXP, "ocean-*.mbtiles")))
for label, lon, lat in WINDOWS:
    print(f"\n  {label}")
    hdr = "    zoom  " + "".join(f"{os.path.basename(c)[6:-8]:>12s}" for c in cfgs)
    print(hdr)
    trends = {c: [] for c in cfgs}
    for z in range(7):
        x, y = tile_of(lon, lat, z)
        row = f"    z{z:<4} "
        for c in cfgs:
            v = vertices(c, z, x, y)
            trends[c].append(v)
            row += f"{('-' if v is None else format(v, ',')):>12s}"
        print(row)
    print("    monotonic (vertices rise with zoom)?")
    for c in cfgs:
        vs = [v for v in trends[c] if v is not None]
        drops = [i for i in range(1, len(vs)) if vs[i] < vs[i-1]]
        tag = os.path.basename(c)[6:-8]
        verdict = "YES" if not drops else f"NO — falls at index {drops}"
        print(f"      {tag:4s} {verdict}")
PY
report_volumes
echo "=== DONE $(date -Is) ==="
