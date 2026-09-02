#!/bin/bash
#SBATCH --job-name=water233-condaprobe
#SBATCH --output=/vast/ishi/elastic/water233-20260902T220703Z/logs/02-conda-%j.out
#SBATCH --error=/vast/ishi/elastic/water233-20260902T220703Z/logs/02-conda-%j.err
#SBATCH --time=00:30:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=8G

# place#233 Step 1 de-risking. The issue states osmcoastline is "ABSENT and
# not packaged", making a source build the plan. That is a claim about the
# CHANNEL, and if it is wrong the whole hard part of Step 1 collapses into an
# install. Verify before committing to a build. Also verify outbound network
# from an htc compute node, since everything must run under Slurm.
set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
echo "=== node $(hostname) job ${SLURM_JOB_ID} ==="

echo
echo "=== outbound network from compute node? ==="
curl -sS -m 25 -o /dev/null -w 'conda.anaconda.org HTTP %{http_code} in %{time_total}s\n' \
     https://conda.anaconda.org/conda-forge/noarch/repodata.json 2>&1 | head -2 \
     || echo "NO OUTBOUND NETWORK from this node"

echo
echo "=== is osmcoastline packaged in conda-forge? ==="
conda search -c conda-forge --override-channels osmcoastline 2>&1 | tail -12 \
    || echo ">>> osmcoastline NOT found in conda-forge"

echo
echo "=== osmium-tool (the CLI package) ==="
conda search -c conda-forge --override-channels osmium-tool 2>&1 | tail -5 || echo "not found"

echo
echo "=== build deps, latest available ==="
for p in libosmium protozero gdal libgdal geos boost-cpp; do
  echo "--- $p ---"
  conda search -c conda-forge --override-channels "$p" 2>&1 | tail -3 || echo "  not found"
done
echo "=== DONE ==="
