#!/bin/bash
#SBATCH --job-name=tilech-exp-droprate
#SBATCH --output=/vast/ishi/elastic-tile-channels/logs/tilech-exp-droprate-%j.out
#SBATCH --error=/vast/ishi/elastic-tile-channels/logs/tilech-exp-droprate-%j.err
#SBATCH --time=01:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
# Experiment, NOT a build: the points channel of hgis and pl re-tiled from the
# kept <b>.points.geojsonl with (A) the flags as built (control) and (B) the same
# plus --drop-rate 1, so SG can see what rate-dropping costs the low-zoom heat.
set -eo pipefail
ulimit -n 65536
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh && conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
T=$CONDA_PREFIX/bin/tippecanoe
IN=/ix1/ishi/data/tiles-channels-20261010
OUT=/vast/ishi/staged/runs/tilech-adhoc/exp
ADHOC=/vast/ishi/staged/runs/tilech-adhoc
COMMON="--force --simplification 10 --detect-shared-borders --no-tile-compression --read-parallel --coalesce-densest-as-needed --drop-densest-as-needed --cluster-distance 10 --cluster-maxzoom 8 --cluster-densest-as-needed --accumulate-attribute start:min --accumulate-attribute end:max --accumulate-attribute start_def:min --accumulate-attribute end_def:max --minimum-zoom 0 --maximum-zoom 10"
for b in hgis pl; do
  echo "################ $b  ($(wc -l < $IN/$b.points.geojsonl) point features)"
  for v in A B; do
    EXTRA=""; [ $v = B ] && EXTRA="--drop-rate 1"
    /usr/bin/time -f "  variant $v wall=%es maxrss=%MKB" $T --output $OUT/$b.points.$v.mbtiles --layer $b --name "exp $b $v" $COMMON $EXTRA $IN/$b.points.geojsonl 2> >(grep -vE "^\s*$|features|tiles$" >&2 || true)
    ls -la $OUT/$b.points.$v.mbtiles | awk "{print \"  variant $v bytes=\" \$5}"
  done
  echo "--- chancmp: NEW=variant B (--drop-rate 1)  OLD=variant A (as built)"
  python $ADHOC/chancmp.py $OUT/$b.points.B.mbtiles $OUT/$b.points.A.mbtiles --sample 300 --full-upto 8 | grep -vE "^columns:|vector_layers|zooms"
done
echo "EXPERIMENT DONE"
