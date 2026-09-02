#!/bin/bash
#SBATCH --job-name=water233-buildcoast
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/04-buildcoast-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/04-buildcoast-%j.err
#SBATCH --time=03:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=16G

# place#233 Step 1 — build osmcoastline from source.
#
# The ocean is the specialised part of this issue. natural=coastline is carried
# on OPEN way fragments that must be stitched into closed rings planet-wide,
# with antimeridian and pole handling. osmcoastline exists to do exactly that
# and is confirmed ABSENT from conda-forge (job 11111623: "No match found for:
# osmcoastline"), so it must be built. All six deps ARE in conda-forge.
#
# ⚠️ BUILT INTO A DEDICATED ENV, NOT `whg`. Solving GDAL into the environment
# that carries torch/shapely/the production pipeline is a real risk for zero
# benefit — the issue itself flags it. A separate env means the worst case of a
# bad solve is a throwaway directory, not a broken ingestion pipeline.
# It also keeps the loader split clean: osmcoastline is built against conda's
# GDAL/GEOS, so it lives in the same camp as osmium and never needs the
# system ogr2ogr that breaks under conda's libs.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
COAST_ENV="${COAST_ENV:-whg-coastline}"
BUILD_DIR="${RUN_ROOT}/build"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0

echo
echo "=== /ihome capacity (conda envs live here; a gdal env is a few GB) ==="
df -h /ihome/ishi/stg135 2>/dev/null | tail -2 || df -h ~ | tail -2
IHOME_AVAIL=$(df -BG --output=avail /ihome/ishi/stg135 2>/dev/null | tail -1 | tr -dc '0-9')
echo "ihome_avail=${IHOME_AVAIL:-unknown}G"
if [ -n "${IHOME_AVAIL}" ] && [ "${IHOME_AVAIL}" -lt 10 ]; then
    echo "FATAL: <10G free on /ihome; refusing to create a GDAL env" >&2; exit 90
fi

echo
echo "=== creating isolated env: ${COAST_ENV} ==="
if conda env list | awk '{print $1}' | grep -qx "${COAST_ENV}"; then
    echo "env ${COAST_ENV} already exists — reusing"
else
    # ⚠️ Do NOT request boost-cpp. It is the DEPRECATED package name and
    # conflicts with the libboost that current libosmium depends on:
    #   libosmium-2.22.0 requires libboost >=1.86, but boost-cpp requires
    #   libboost <0.0a0  ->  LibMambaUnsatisfiableError.
    # The issue's tooling block lists "boost-cpp 1.85.0" as the dep to use;
    # that is the old name and it is what made this solve fail. Let libosmium
    # pull its own libboost and take headers from libboost-devel.
    conda create -y -n "${COAST_ENV}" -c conda-forge --override-channels \
        libosmium protozero libgdal gdal geos libboost-devel cmake make \
        cxx-compiler c-compiler zlib bzip2 expat lz4-c sqlite pkg-config git
fi
conda activate "${COAST_ENV}"
echo "CONDA_PREFIX=${CONDA_PREFIX}"

echo
echo "=== dependency presence (headers, not just packages) ==="
for h in osmium/version.hpp protozero/version.hpp; do
    if [ -f "${CONDA_PREFIX}/include/${h}" ]; then echo "  OK   ${h}"; else echo "  MISS ${h}"; fi
done
command -v gdal-config && gdal-config --version || echo "  MISS gdal-config"
command -v cmake && cmake --version | head -1

echo
echo "=== fetching osmcoastline source ==="
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"
if [ -d osmcoastline/.git ]; then
    echo "source tree present; leaving as-is"
else
    git clone --depth 1 https://github.com/osmcode/osmcoastline.git
fi
cd osmcoastline
git log --oneline -1 || true

echo
echo "=== cmake configure ==="
rm -rf build && mkdir -p build && cd build
cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="${CONDA_PREFIX}" \
    -DCMAKE_PREFIX_PATH="${CONDA_PREFIX}" \
    2>&1 | tail -30

echo
echo "=== build ==="
make -j"${SLURM_CPUS_PER_TASK}" 2>&1 | tail -40

echo
echo "=== install ==="
make install 2>&1 | tail -15

echo
echo "=== VERIFY the binary actually runs (presence != working) ==="
which osmcoastline || { echo "FATAL: osmcoastline not on PATH after install" >&2; exit 1; }
osmcoastline --version 2>&1 | head -5 || { echo "FATAL: osmcoastline will not run" >&2; exit 1; }
echo "--- osmcoastline_filter / osmcoastline_ways too ---"
which osmcoastline_filter osmcoastline_ways 2>&1 || true

echo
echo "=== record how to use this env ==="
cat > "${RUN_ROOT}/coastline_env.sh" <<ENVEOF
# Sourced by later steps. osmcoastline is built against conda's GDAL/GEOS in a
# dedicated env, so it needs that env active and NOT the whg env.
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate ${COAST_ENV}
OSMCOASTLINE="\${CONDA_PREFIX}/bin/osmcoastline"
ENVEOF
cat "${RUN_ROOT}/coastline_env.sh"
report_volumes
echo "=== DONE $(date -Is) ==="
