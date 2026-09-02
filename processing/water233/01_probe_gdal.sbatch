#!/bin/bash
#SBATCH --job-name=water233-gdal
#SBATCH --output=/vast/ishi/elastic/water233-20260902T220703Z/logs/01-gdal-%j.out
#SBATCH --error=/vast/ishi/elastic/water233-20260902T220703Z/logs/01-gdal-%j.err
#SBATCH --time=00:20:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=4G

# place#233 Step 0a follow-up. Two probes in job 11111619 were WRONG:
#   - "tile-join MISSING" was my probe's fault: tile-join has no --version.
#   - "ogr2ogr MISSING" is real but conditional: it breaks ONLY under the
#     conda LD_LIBRARY_PATH that osmium REQUIRES. The two Step 0a
#     instructions interact destructively, so establish the ordering now
#     rather than at the osmcoastline stage.
set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
echo "=== node $(hostname) job ${SLURM_JOB_ID} ==="
SYS_LDLP="${LD_LIBRARY_PATH}"

echo
echo "=== tile-join (correct probe: no --version flag exists) ==="
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${SYS_LDLP}"
command -v tile-join && (tile-join 2>&1 | head -3) || echo "tile-join genuinely absent"

echo
echo "=== ogr2ogr WITHOUT conda lib path (system loader) ==="
env LD_LIBRARY_PATH="${SYS_LDLP}" ogr2ogr --version 2>&1 | head -2 || echo "FAILED clean"

echo
echo "=== ogr2ogr WITH conda lib path (what osmium needs) ==="
env LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${SYS_LDLP}" ogr2ogr --version 2>&1 | head -2 || echo "FAILED under conda libs"

echo
echo "=== conda's own gdal? ==="
ls -la "$CONDA_PREFIX/bin/ogr2ogr" 2>&1 || echo "no conda ogr2ogr binary"
echo "--- python GDAL bindings ---"
python -c 'from osgeo import gdal, ogr; print("osgeo GDAL", gdal.__version__)' 2>&1 | head -3 || echo "no osgeo bindings"
echo "--- shapely / geos ---"
python -c 'import shapely; print("shapely", shapely.__version__); from shapely import geos_version; print("geos", geos_version)' 2>&1 | head -3 || echo "no shapely"
echo "--- osmcoastline build deps present in env? ---"
for lib in libosmium protozero geos gdal boost-cpp cmake sqlite; do
  printf '%-12s ' "$lib"; conda list -n whg 2>/dev/null | awk -v L="$lib" '$1==L {print $2; f=1} END{if(!f) print "ABSENT"}'
done
echo "--- headers ---"
ls "$CONDA_PREFIX/include/osmium/version.hpp" 2>&1 || echo "no libosmium headers"
ls "$CONDA_PREFIX/include/protozero/version.hpp" 2>&1 || echo "no protozero headers"
command -v cmake && cmake --version | head -1 || echo "no cmake"
echo "=== DONE ==="
