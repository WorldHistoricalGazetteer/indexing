#!/bin/bash
# z17 full-corpus campaign driver: 28 z16 lat-band batches over the GB1900 extent (y 18714-22263).
# Sequential (no shared-edge-tile race), resumable (z17_batch skips .done). Each band:
# fetch needed z17 tiles -> type (top-3) -> tar to /ix1 -> drop from /vast.
#   sbatch -M htc --account=ishi --partition=htc --qos=htc-htc-l --cpus-per-task=16 --mem=20G \
#          --time=6-00:00:00 run_z17_campaign.sh
set -uo pipefail
PY=/vast/ishi/envs/boundary/bin/python
D=/vast/ishi/gb1900/probe/typing
OUT=/vast/ishi/gb1900/edition/types_z17
Y0=18714; Y1=22264; NB=28
STEP=$(( (Y1 - Y0) / NB + 1 ))
mkdir -p "$OUT"
echo "=== z17 campaign: bands of $STEP z16-rows from $Y0 to $Y1 ==="
for (( y=Y0; y<Y1; y+=STEP )); do
  ye=$(( y + STEP )); (( ye > Y1 )) && ye=$Y1
  echo "=== BAND ${y}_${ye} $(date -u +%H:%M:%S) ==="
  $PY "$D/z17_batch.py" --ymin "$y" --ymax "$ye" --out "$OUT" --names "$D/admin_names.json" --workers 72 \
    || echo "!! band ${y}_${ye} FAILED (continuing)"
done
echo "=== CAMPAIGN COMPLETE $(date -u) ==="
ls "$OUT"/*.done | wc -l
