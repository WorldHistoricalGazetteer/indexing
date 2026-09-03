#!/bin/bash
#SBATCH --job-name=water236-mzfix
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/33-mzfix-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/33-mzfix-%j.err
#SBATCH --time=08:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=96G

# place#236 — apply minzoom in the form tippecanoe actually honours.
#
# ⚠️ THE PREVIOUS ATTEMPT USED A FORM TIPPECANOE IGNORES. Proved on a
# 1,000-feature sample with every feature forced to minzoom 6 (job 11115929):
#     properties["tippecanoe:minzoom"]        -> tiles at z0..z8   IGNORED
#     feature-level "tippecanoe":{"minzoom":} -> tiles at z6..z8   HONOURED
# So the hypothesis was never tested, only the null. The arithmetic had said
# so before the test did: 11,292 lakes computed with minzoom<=3, 53,966 lake
# features present at z3.
#
# ⚠️ AND THE VERIFICATION MUST BE PER-LAYER OCEAN VERTICES, NOT TOTAL BYTES.
# The fix deliberately withholds sub-pixel features, so low total bytes at
# low zoom is what SUCCESS looks like -- total bytes cannot separate "ocean
# starved" from "ponds correctly absent". Same conflation as the areal check
# that certified the blocky original.
#
# TARGET, from ocean tiled alone at z0-6 with identical flags (config A):
#     z0 119,479  z1 239,878  z2 454,569  z3 802,755  z4 1,376,570
# The ocean in a four-layer tile should approach those numbers. In the
# shipped build z3 was 7,851.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"
EXP="${D}/param-exp"
TMPDIR_TP="${SLURM_SCRATCH:-/tmp}/tippecanoe-mzfix"
mkdir -p "${EXP}" "${TMPDIR_TP}"; ulimit -n 65536

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 60

echo "=== rewriting with the HONOURED feature-level form ==="
python - "${D}" <<'PY'
import json, math, os, sys, collections
D = sys.argv[1]
R0 = 156543.03392

def size_and_lat(geom):
    xs = []; ys = []
    stack = [geom.get("coordinates", [])]
    while stack:
        c = stack.pop()
        if isinstance(c, list):
            if c and isinstance(c[0], (int, float)):
                xs.append(c[0]); ys.append(c[1])
            else:
                stack.extend(c)
    if not xs:
        return 0.0, 0.0
    lat = (min(ys) + max(ys)) / 2.0
    w = (max(xs) - min(xs)) * 111320.0 * math.cos(math.radians(lat))
    h = (max(ys) - min(ys)) * 110540.0
    return max(w, h), lat

def minzoom_for(size_m, lat, maxz=10):
    if size_m <= 0:
        return maxz
    res0 = R0 * math.cos(math.radians(lat))
    return max(0, min(int(math.ceil(math.log2(max(res0 / size_m, 1.0)))), maxz))

for layer, src_name in (("lakes", "lakes.geojsonl"),
                        ("rivers", "rivers.geojsonl"),
                        ("ice", "ice-only.geojsonl")):
    src = os.path.join(D, src_name)
    dst = os.path.join(D, f"{layer}-mz2.geojsonl")
    hist = collections.Counter(); n = 0
    with open(src, encoding="utf-8") as fh, open(dst, "w", encoding="utf-8") as out:
        for line in fh:
            f = json.loads(line)
            size, lat = size_and_lat(f.get("geometry") or {})
            mz = minzoom_for(size, lat)
            # THE HONOURED FORM: a feature-level "tippecanoe" member.
            f["tippecanoe"] = {"minzoom": mz}
            f["properties"].pop("tippecanoe:minzoom", None)
            hist[mz] += 1; n += 1
            out.write(json.dumps(f, separators=(",", ":")) + "\n")
    cum = 0
    print(f"  {layer}: {n:,} features")
    for z in range(11):
        cum += hist[z]
        if z <= 6:
            print(f"    minzoom<={z}: {cum:>10,} ({100*cum/n:5.2f}%)")
PY

echo
echo "=== FIXED-2: ocean + lakes(honoured minzoom), shipped flags, z0-6 ==="
rm -f "${EXP}/ol-fixed2.mbtiles"
t0=$(date +%s)
tippecanoe --output "${EXP}/ol-fixed2.mbtiles" --name ol-fixed2 \
    --minimum-zoom=0 --maximum-zoom=6 \
    --coalesce-densest-as-needed --drop-densest-as-needed \
    --simplification=4 --detect-shared-borders \
    --temporary-directory="${TMPDIR_TP}" --force --quiet \
    -L "ocean:${D}/ocean-c001.geojsonl" \
    -L "lakes:${D}/lakes-mz2.geojsonl" 2>&1 | tail -3
echo "built in $(( $(date +%s) - t0 ))s"

echo
echo "=== PER-LAYER vertices — the ocean row is the verdict ==="
python - "${EXP}/ol-fixed2.mbtiles" <<'PY'
import collections, json, re, subprocess, sys
zoom_re = re.compile(r'"zoom":\s*(\d+)'); lay_re = re.compile(r'"layer":\s*"([^"]+)"')
verts = collections.defaultdict(collections.Counter)
feats = collections.defaultdict(collections.Counter)
z = lay = None
p = subprocess.Popen(["tippecanoe-decode", sys.argv[1]], stdout=subprocess.PIPE,
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
    if '"geometry"' not in line or z is None or lay is None: continue
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
    verts[lay][z] += n; feats[lay][z] += 1
p.wait()

TARGET = {0: 119479, 1: 239878, 2: 454569, 3: 802755, 4: 1376570,
          5: 2257619, 6: 3733802}
for lay in sorted(verts):
    print(f"\n  layer '{lay}'")
    hdr = f"    {'z':<4}{'features':>11}{'vertices':>13}{'vs prev':>9}"
    if lay == "ocean": hdr += f"{'ocean-alone':>14}{'% of alone':>12}"
    print(hdr)
    prev = None; troughs = []
    for z in range(7):
        v = verts[lay][z]; f = feats[lay][z]
        r = f"{v/prev:.2f}x" if prev else "-"
        flag = ""
        if prev is not None and v < prev: troughs.append(z); flag = "  <<TROUGH"
        row = f"    z{z:<3}{f:>11,}{v:>13,}{r:>9}"
        if lay == "ocean":
            t = TARGET[z]
            row += f"{t:>14,}{100*v/t:>11.1f}%"
        print(row + flag)
        prev = v if v else prev
    print(f"    -> {lay}: " + ("MONOTONIC" if not troughs
                               else f"TROUGHS at z{', z'.join(map(str,troughs))}"))
PY
report_volumes
echo "=== DONE $(date -Is) ==="
