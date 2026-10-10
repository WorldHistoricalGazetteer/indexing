#!/bin/bash
#SBATCH --job-name=tilech-verify-points
#SBATCH --output=/vast/ishi/elastic-tile-channels/logs/tilech-verify-points-%j.out
#SBATCH --error=/vast/ishi/elastic-tile-channels/logs/tilech-verify-points-%j.err
#SBATCH --time=06:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=16G
# place#166 points-only pass: the §9 verification, per bucket, NEW against the verified LIVE backup.
# usage: sbatch -M htc --export=ALL,NEW=<built dir>,OLD=<backup dir> tilech-verify-points.sbatch "<b> [<b>...]"
#   ledger match (verify_tileset_channels), land-tile check, every tile over 500 KB by name at every
#   zoom (oversize.py NEW OLD), per-zoom OLD/NEW decode (chancmp: nothing lost at z10, cluster counts and
#   point_count mass at z0–z7), sha256 of NEW.
set -o pipefail
NEW=${NEW:?set NEW=<dir of new tilesets>}
OLD=${OLD:?set OLD=<dir of live backups>}
REPO=${REPO:-/vast/ishi/elastic-tile-channels}
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh && conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
cd $REPO || exit 2
echo "VERIFY START $(date -u +%FT%TZ) NEW=$NEW OLD=$OLD code=$(git rev-parse --short HEAD)"
RC=0
for b in $1; do
  echo; echo "################ $b ################"
  [ -f $NEW/$b.mbtiles ] || { echo "NO NEW TILESET for $b"; RC=1; continue; }
  [ -f $OLD/$b.mbtiles ] || { echo "NO LIVE BACKUP for $b"; RC=1; continue; }
  echo "sizes: NEW $(stat -c %s $NEW/$b.mbtiles) B  OLD $(stat -c %s $OLD/$b.mbtiles) B"
  echo "--- ledger:"; python -c "import json,sys;d=json.load(open('$NEW/$b.channels.json'));print({k:d[k] for k in d if k!='channels'});print('channels:',d.get('channels'))" || echo "NO LEDGER"
  echo "--- verify_tileset_channels:"
  python -m processing.verify_tileset_channels $NEW/$b.mbtiles; r=$?; echo "verify_tileset_channels exit=$r"; [ $r -ne 0 ] && RC=1
  echo "--- verify_tileset_coverage --all:"
  python -m processing.verify_tileset_coverage --all $NEW/$b.mbtiles; echo "verify_tileset_coverage exit=$?"
  echo "--- oversize.py NEW OLD (every tile over 500,000 B, by name):"
  python developer/tile-qa/oversize.py $NEW/$b.mbtiles $OLD/$b.mbtiles
  echo "--- chancmp NEW OLD (full decode to z7, 200-tile sample above):"
  python developer/tile-qa/chancmp.py $NEW/$b.mbtiles $OLD/$b.mbtiles --sample 200 --full-upto 7
  echo "--- sha256 NEW:"; sha256sum $NEW/$b.mbtiles
done
echo; echo "VERIFY DONE $(date -u +%FT%TZ) rc=$RC"
exit $RC
