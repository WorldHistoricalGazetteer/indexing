#!/bin/bash
#SBATCH --job-name=water236-perlayer
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/31-perlayer-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/31-perlayer-%j.err
#SBATCH --time=04:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=32G

# place#236 — ⚠️ TOTAL BYTES IS NOW THE WRONG INSTRUMENT, and it nearly made
# me abandon a working fix.
#
# The fix deliberately withholds 98.1% of lakes from z<=6, because they span
# less than a pixel there. So LOW TOTAL BYTES AT LOW ZOOM IS WHAT SUCCESS
# LOOKS LIKE. A total-bytes trough at z3 is now consistent with two opposite
# situations:
#     the ocean is still being starved            <- the defect
#     the ponds are correctly absent               <- the fix working
# and total bytes cannot separate them. It is the same conflation as the
# areal check that certified the blocky original: an instrument answering a
# different question from the one asked.
#
# The defect was always about the OCEAN's shape. So measure the ocean's
# vertices, per layer, per zoom -- which is what place#236 asked for and what
# I substituted a cheaper proxy for.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
EXP="${RUN_ROOT}/data/param-exp"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
python - "${EXP}" <<'PY'
import collections, json, os, re, subprocess, sys
EXP = sys.argv[1]
zoom_re = re.compile(r'"zoom":\s*(\d+)')
lay_re  = re.compile(r'"layer":\s*"([^"]+)"')

def profile(mb):
    verts = collections.defaultdict(collections.Counter)
    feats = collections.defaultdict(collections.Counter)
    z = lay = None
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
        if '"geometry"' not in line or z is None or lay is None:
            continue
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
        verts[lay][z] += n
        feats[lay][z] += 1
    p.wait()
    return verts, feats

for tag, fn in (("CONTROL (no minzoom)", "ol-control.mbtiles"),
                ("FIXED (lakes minzoom)", "ol-fixed.mbtiles")):
    path = os.path.join(EXP, fn)
    if not os.path.exists(path):
        print(f"\n  {tag}: MISSING"); continue
    verts, feats = profile(path)
    print(f"\n=== {tag} ===")
    for lay in sorted(verts):
        print(f"\n  layer '{lay}'")
        print(f"    {'z':<4}{'features':>12}{'vertices':>14}{'vs prev':>10}")
        prev = None; troughs = []
        for z in range(7):
            v = verts[lay][z]; f = feats[lay][z]
            r = f"{v/prev:.2f}x" if prev else "-"
            flag = ""
            if prev is not None and v < prev:
                troughs.append(z); flag = "  <<TROUGH"
            print(f"    z{z:<3}{f:>12,}{v:>14,}{r:>10}{flag}")
            prev = v if v else prev
        verdict = "MONOTONIC" if not troughs else f"TROUGHS at z{', z'.join(map(str,troughs))}"
        print(f"    -> {lay}: {verdict}")

print()
print("  THE QUESTION IS THE OCEAN ROW. Lakes falling away at low zoom is the")
print("  fix working as designed; the ocean falling away is the defect.")
PY
echo "=== DONE $(date -Is) ==="
