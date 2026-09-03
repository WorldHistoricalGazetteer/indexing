#!/bin/bash
#SBATCH --job-name=water236-accept
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/35-accept-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/35-accept-%j.err
#SBATCH --time=06:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=64G

# place#236 — acceptance for water-v2, run on NODE-LOCAL SCRATCH.
#
# ⚠️ WHY THIS IS A SEPARATE JOB. The rebuild's inline acceptance sat for 47
# minutes at 0.2% CPU and 7 seconds of CPU time: not computing, BLOCKED ON
# I/O. SQLite scanning every blob of a 1.18 GB mbtiles over NFS is the
# documented /ix1 small-random-read pathology -- the reason ES and Kibana
# were moved off that volume. I put tippecanoe's TEMP on node-local NVMe and
# then left the far more random-access analysis on NFS.
#
# The tiling itself completed cleanly (tippecanoe elapsed=8671s) and the file
# is intact, so only the analysis is redone. Copy both tilesets to
# $SLURM_SCRATCH first; sequential copy is what NFS is good at, random blob
# access is what it is bad at.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
D="${RUN_ROOT}/data"
S="${SLURM_SCRATCH:-/tmp}/accept"
mkdir -p "$S"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
echo "=== copying to node-local scratch (sequential read, NFS's good case) ==="
t0=$(date +%s)
cp "${D}/water-v2.mbtiles"            "$S/new.mbtiles"
cp "${D}/param-exp/ocean-E.mbtiles"   "$S/ref.mbtiles"
echo "copied in $(( $(date +%s) - t0 ))s"
df -h "$S" | tail -1

echo
echo "=== metadata ==="
python - "$S/new.mbtiles" <<'PY'
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
md = dict(con.execute("SELECT name, value FROM metadata").fetchall())
for k in ("name", "description", "format", "minzoom", "maxzoom", "type", "attribution"):
    print(f"  {k:12s} {str(md.get(k))[:100]}")
vl = sorted(l.get("id") for l in json.loads(md.get("json","{}")).get("vector_layers", []))
print(f"  vector_layers {vl}")
print(f"  tiles         {con.execute('SELECT COUNT(*) FROM tiles').fetchone()[0]:,}")
print("\n  per-zoom:")
worst = 0
for z, cnt, tot, mx in con.execute(
        "SELECT zoom_level, COUNT(*), SUM(LENGTH(tile_data)), MAX(LENGTH(tile_data)) "
        "FROM tiles GROUP BY zoom_level ORDER BY zoom_level"):
    worst = max(worst, mx)
    print(f"    z{z:<3}{cnt:>9,} tiles{tot:>15,} bytes   max {mx/1024:>8.1f} KB")
print(f"  worst tile: {worst/1024:.1f} KB")
bad = []
if md.get("minzoom") != "0" or md.get("maxzoom") != "10": bad.append("not z0-10")
if vl != ["ice","lakes","ocean","rivers"]: bad.append(f"layers {vl}")
if str(md.get("name","")).startswith("/"): bad.append("name is a path")
if worst > 500*1024: bad.append(f"tile over 500 KB ({worst/1024:.1f})")
if bad: print("  *** METADATA FAILED: " + "; ".join(bad)); sys.exit(3)
print("  metadata OK")
PY

echo
echo "=== 🛑 GATE: ocean detail vs the same ocean tiled ALONE (z0-10 reference) ==="
# NOT monotonicity: the shipped build was monotonic from z4 up while retaining
# 40.3% at z3. Monotonicity is a property of shape; this defect is magnitude.
# The reference is the same input under the same flags with nothing competing,
# so ~100% is the criterion rather than a number anyone argued for.
python - "$S/new.mbtiles" "$S/ref.mbtiles" <<'PY'
import collections, json, re, subprocess, sys
zoom_re = re.compile(r'"zoom":\s*(\d+)'); lay_re = re.compile(r'"layer":\s*"([^"]+)"')
def ocean(mb, label):
    v = collections.Counter(); f = collections.Counter(); z = lay = None
    print(f"  decoding {label} ...", flush=True)
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
        v[z] += n; f[z] += 1
    p.wait(); return v, f
nv, nf = ocean(sys.argv[1], "rebuild")
rv, rf = ocean(sys.argv[2], "reference")
print()
print(f"  {'z':<4}{'rebuild verts':>16}{'alone verts':>15}{'retained':>11}")
fail = []
for z in range(11):
    if not rv[z]: continue
    pct = 100.0 * nv[z] / rv[z]
    flag = ""
    if pct < 95.0: fail.append(z); flag = "  <<LOW"
    print(f"  z{z:<3}{nv[z]:>16,}{rv[z]:>15,}{pct:>10.1f}%{flag}")
print()
if fail:
    print(f"  *** GATE FAILED: ocean below 95% at z{', z'.join(map(str,fail))}")
    print( "  *** Still starved. DO NOT DEPLOY.")
    sys.exit(3)
print("  GATE PASSED: ocean retains >=95% of tiled-alone detail at every zoom.")
PY

echo
echo "--- for the deployer ---"
stat -c 'size=%s bytes' "${D}/water-v2.mbtiles"
sha256sum "${D}/water-v2.mbtiles"
echo "=== DONE $(date -Is) ==="
