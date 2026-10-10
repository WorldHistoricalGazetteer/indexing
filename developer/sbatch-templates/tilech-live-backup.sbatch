#!/bin/bash
#SBATCH --job-name=tiles-live-backup-20261010
#SBATCH --output=/vast/ishi/elastic-tile-channels/logs/tiles-live-backup-%j.out
#SBATCH --error=/vast/ishi/elastic-tile-channels/logs/tiles-live-backup-%j.err
#SBATCH --time=03:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
set -eo pipefail
B=/ix1/ishi/data/tiles-live-backup-20261010
SRC=/ix1/ishi/data/tiles-20260902-retile
K=/vast/ishi/secrets/tileserver_id_ed25519
H=whgadmin@134.209.177.234
T=/srv/tileserver/tiles
NS="hgis pl ukhc un vob_rd vob_rc vob_cty vob_lgd kain_par nl po clio wd"
mkdir -p "$B"
echo "seed from $SRC (same size+mtime as live; rsync --checksum below proves or corrects it)"
for b in $NS; do cp -p "$SRC/$b.mbtiles" "$B/"; done
cp -p "$SRC"/whg-*.mbtiles "$B/" || true
echo "seeded: $(ls "$B"/*.mbtiles | wc -l) files"
REM=""; for b in $NS; do REM="$REM $H:$T/$b.mbtiles"; done
echo "rsync --checksum of the 13 per-namespace tilesets from the tileserver:"
rsync -a --checksum --itemize-changes --info=stats2 --bwlimit=25m -e "ssh -i $K -o BatchMode=yes" $REM "$B/"
echo "rsync --checksum of whg-*:"
rsync -a --checksum --itemize-changes --info=stats2 --bwlimit=25m -e "ssh -i $K -o BatchMode=yes" "$H:$T/whg-*.mbtiles" "$B/"
cd "$B" && sha256sum *.mbtiles > SHA256SUMS
echo "BACKUP DONE: $(ls *.mbtiles | wc -l) mbtiles, $(du -sh . | cut -f1), sha256 manifest $(wc -l < SHA256SUMS) lines"
