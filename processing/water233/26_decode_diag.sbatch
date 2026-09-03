#!/bin/bash
#SBATCH --job-name=water236-decodediag
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/26-decodediag-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/26-decodediag-%j.err
#SBATCH --time=00:30:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=8G

# place#236 — my vertex counter returned 0 for every tile, every config,
# every window, and then reported "monotonic: YES" for all four -- including
# config A, which was built to REPRODUCE the defect. A list of zeros never
# decreases, so the check passed its own known-bad control.
#
# Do not guess at the cause. LOOK at what the tools actually emit.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
D=/ix1/ishi/water233-20260902T220703Z/data
EXP="${D}/param-exp"

echo "=== node $(hostname) job ${SLURM_JOB_ID} ==="
echo "=== 1. did the experiment tilesets actually get built? ==="
ls -la "${EXP}"/ 2>&1
for f in "${EXP}"/ocean-*.mbtiles; do
    printf '%-40s ' "$(basename "$f")"
    python - "$f" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
n = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
z = con.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
b = con.execute("SELECT SUM(LENGTH(tile_data)) FROM tiles").fetchone()[0]
print(f"tiles={n:,} zooms={z} bytes={b:,}")
PY
done

echo
echo "=== 2. what does tippecanoe-decode actually print? ==="
F="${EXP}/ocean-A.mbtiles"
echo "--- tippecanoe-decode --help (arg form) ---"
tippecanoe-decode --help 2>&1 | head -15 || true
echo
echo "--- attempt: tippecanoe-decode -f FILE 2 0 1 (first 25 lines) ---"
tippecanoe-decode -f "${F}" 2 0 1 2>&1 | head -25 || echo "  (that form failed)"
echo
echo "--- attempt: tippecanoe-decode FILE 2 0 1 (first 25 lines) ---"
tippecanoe-decode "${F}" 2 0 1 2>&1 | head -25 || echo "  (that form failed)"

echo
echo "=== 3. is there a tile at that address at all? ==="
python - "${F}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
print("  zoom / count / total bytes:")
for row in con.execute("SELECT zoom_level, COUNT(*), SUM(LENGTH(tile_data)) "
                       "FROM tiles GROUP BY zoom_level ORDER BY zoom_level"):
    print(f"    z{row[0]:<3} {row[1]:>8,} tiles  {row[2]:>12,} bytes")
print("  sample rows (z,x,row) at z2:")
for row in con.execute("SELECT zoom_level,tile_column,tile_row,LENGTH(tile_data) "
                       "FROM tiles WHERE zoom_level=2 LIMIT 8"):
    print(f"    {row}")
PY
echo "=== DONE ==="
