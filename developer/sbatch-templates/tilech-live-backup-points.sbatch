#!/bin/bash
#SBATCH --job-name=tiles-live-backup-points
#SBATCH --output=/vast/ishi/elastic-tile-channels/logs/tiles-live-backup-points-%j.out
#SBATCH --error=/vast/ishi/elastic-tile-channels/logs/tiles-live-backup-points-%j.err
#SBATCH --time=03:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
# place#166 points-only pass: back up the LIVE points-only tilesets into the same verified backup
# directory as the 10 Oct swap. Seeds from the /ix1 copies whose size+mtime match live, then
# rsync --checksum FROM the tileserver proves or corrects each byte. READS the tileserver only.
set -eo pipefail
B=/ix1/ishi/data/tiles-live-backup-20261010
K=/vast/ishi/secrets/tileserver_id_ed25519
H=whgadmin@134.209.177.234
T=/srv/tileserver/tiles
NS="gn tgn gb chgis alc iv tm ofs og dgsd dp"
[ -f "$B/SHA256SUMS" ] || { echo "no $B/SHA256SUMS — wrong directory"; exit 2; }
for b in $NS; do
  [ -f "$B/$b.mbtiles" ] && { echo "already present: $b"; continue; }
  for src in /ix1/ishi/data/tiles-20260902-retile /ix1/ishi/data/tiles; do
    [ -f "$src/$b.mbtiles" ] && { cp -p "$src/$b.mbtiles" "$B/"; echo "seeded $b from $src"; break; }
  done
done
REM=""; for b in $NS; do REM="$REM $H:$T/$b.mbtiles"; done
echo "rsync --checksum of the points-only tilesets from the tileserver (a transferred file = the seed was NOT live):"
rsync -a --checksum --itemize-changes --info=stats2 --bwlimit=25m -e "ssh -i $K -o BatchMode=yes" $REM "$B/"
cd "$B"
for b in $NS; do grep -q " $b.mbtiles\$" SHA256SUMS || sha256sum $b.mbtiles >> SHA256SUMS; done
sha256sum -c SHA256SUMS --quiet && echo "SHA256SUMS verified: $(wc -l < SHA256SUMS) lines"
for b in $NS; do echo "$b $(stat -c %s $b.mbtiles) bytes"; done
echo "BACKUP-POINTS DONE: $(ls *.mbtiles | wc -l) mbtiles in $B"
