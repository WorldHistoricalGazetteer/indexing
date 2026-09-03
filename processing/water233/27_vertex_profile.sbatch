#!/bin/bash
#SBATCH --job-name=water236-vertexprof
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/27-vertexprof-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/27-vertexprof-%j.err
#SBATCH --time=04:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G

# place#236 — per-zoom vertex profile of the four experiment tilesets.
#
# ⚠️ FIXING MY OWN BROKEN INSTRUMENT. The first attempt returned 0 vertices
# for every tile and then reported "monotonic: YES" for all four configs,
# INCLUDING config A which was built to reproduce the defect. A list of zeros
# never decreases. Cause: I read `coordinates` from the top level of each
# GeoJSON Feature; it is nested under `geometry`.
#
# The known-bad control is what caught it. Without config A the all-zero
# result would have read as "every config is fine" and I would have picked
# one arbitrarily.
#
# Two changes beyond the fix:
#  * decode EVERY tile, not one tile per zoom. place#236 warns that
#    --coalesce-densest-as-needed acts per tile according to that tile's
#    density, so the z3/z5 parity may be incidental to which tiles carry the
#    most coastline. A whole-zoom total cannot be fooled by tile choice.
#  * stream one tippecanoe-decode per tileset rather than one per tile.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
EXP="${RUN_ROOT}/data/param-exp"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
python - "${EXP}" <<'PY'
import glob, json, os, re, subprocess, sys, collections

EXP = sys.argv[1]
CFG = {"A": "coalesce + drop + simp4 + shared-borders  (the shipped flags)",
       "B": "drop + simp4 + shared-borders",
       "C": "drop + shared-borders, simplification DEFAULT",
       "D": "drop only, simplification DEFAULT"}

zoom_re = re.compile(r'"zoom":\s*(\d+)')
prof = {}

for tag in sorted(CFG):
    mb = os.path.join(EXP, f"ocean-{tag}.mbtiles")
    if not os.path.exists(mb):
        print(f"  {tag}: MISSING"); continue
    verts = collections.Counter()
    feats = collections.Counter()
    z = None
    p = subprocess.Popen(["tippecanoe-decode", mb], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1 << 20)
    for line in p.stdout:
        if '"zoom":' in line and '"x":' in line:
            m = zoom_re.search(line)
            if m:
                z = int(m.group(1))
            continue
        if '"geometry"' not in line or z is None:
            continue
        s = line.strip().rstrip(",")
        if not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except Exception:
            continue
        # ⚠️ coordinates live under geometry, NOT at the Feature's top level.
        coords = (obj.get("geometry") or {}).get("coordinates", [])
        n = 0
        stack = [coords]
        while stack:
            c = stack.pop()
            if isinstance(c, list):
                if c and isinstance(c[0], (int, float)):
                    n += 1
                else:
                    stack.extend(c)
        verts[z] += n
        feats[z] += 1
    p.wait()
    prof[tag] = (verts, feats)
    print(f"  decoded {tag}: {sum(verts.values()):,} vertices in "
          f"{sum(feats.values()):,} features")

print()
print("=== VERTICES PER ZOOM (all tiles, whole planet) ===")
tags = sorted(prof)
print("  zoom " + "".join(f"{t:>16s}" for t in tags))
for z in range(7):
    print(f"  z{z:<4}" + "".join(f"{prof[t][0][z]:>16,}" for t in tags))
print()
print("=== ratio to previous zoom (must be > 1; a trough is the defect) ===")
print("  zoom " + "".join(f"{t:>16s}" for t in tags))
for z in range(1, 7):
    row = f"  z{z:<4}"
    for t in tags:
        prev, cur = prof[t][0][z-1], prof[t][0][z]
        r = (cur / prev) if prev else float("inf")
        row += f"{(f'{r:.2f}x' + ('  <<' if r < 1 else '')):>16s}"
    print(row)
print()
for t in tags:
    v = [prof[t][0][z] for z in range(7)]
    drops = [z for z in range(1, 7) if v[z] < v[z-1]]
    verdict = "MONOTONIC" if not drops else f"TROUGH at z{', z'.join(map(str, drops))}"
    print(f"  {t}  {verdict:28s} {CFG[t]}")
print()
print("  Reminder: judged on VERTEX COUNT, never area. An areal check was run")
print("  on the original and passed -- blocks and a coastline enclosing the")
print("  same area score identically.")
PY
echo "=== DONE $(date -Is) ==="
