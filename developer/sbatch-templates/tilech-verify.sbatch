#!/bin/bash
#SBATCH --job-name=tilech-verify
#SBATCH --output=/vast/ishi/elastic-tile-channels/logs/tilech-verify-%j.out
#SBATCH --error=/vast/ishi/elastic-tile-channels/logs/tilech-verify-%j.err
#SBATCH --time=04:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
# usage: sbatch -M htc verify.sbatch "<bucket> [<bucket>...]"   (BACKUP dir must hold the live copies)
set -o pipefail
NEW=/ix1/ishi/data/tiles-channels-20261010
OLD=/ix1/ishi/data/tiles-live-backup-20261010
ADHOC=/vast/ishi/staged/runs/tilech-adhoc
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh && conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
cd /vast/ishi/elastic-tile-channels
RC=0
for b in $1; do
  echo; echo "################ $b ################"
  echo "--- ledger:"; cat $NEW/$b.channels.json 2>/dev/null | python -c "import json,sys;d=json.load(sys.stdin);print({k:d[k] for k in d if k!=\"channels\"});print(\"channels:\",d.get(\"channels\"))" || echo "NO LEDGER"
  RS=""; case $b in po|clio|nl) RS="--region-source $b";; esac
  echo "--- verify_tileset_channels $RS:"
  python -m processing.verify_tileset_channels $NEW/$b.mbtiles $RS; r=$?; echo "verify_tileset_channels exit=$r"; [ $r -ne 0 ] && RC=1
  echo "--- verify_tileset_coverage --all:"
  python -m processing.verify_tileset_coverage --all $NEW/$b.mbtiles; echo "verify_tileset_coverage exit=$?"
  echo "--- tsize.py NEW OLD:"
  python developer/tile-qa/tsize.py $NEW/$b.mbtiles $OLD/$b.mbtiles
  echo "--- chancmp NEW OLD:"
  python $ADHOC/chancmp.py $NEW/$b.mbtiles $OLD/$b.mbtiles --sample 200 --full-upto 7
  echo "--- sha256 NEW:"; sha256sum $NEW/$b.mbtiles
done
echo; echo "VERIFY DONE rc=$RC"
exit $RC
