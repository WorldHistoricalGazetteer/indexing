#!/bin/bash
# Font-style feasibility probe: synthetic-trained style encoder -> cluster real spotter crops.
#   sbatch -M gpu --account=ishi --partition=l40s --gres=gpu:1 developer/.../run.sbatch
#SBATCH --job-name=gbfont-probe
#SBATCH --account=ishi
#SBATCH --partition=l40s
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH --time=03:00:00
#SBATCH --output=/vast/ishi/gb1900/logs/fontprobe-%j.out

set -uo pipefail
PROBE=/vast/ishi/gb1900/probe/font
MR=/vast/ishi/gb1900/probe/mapreader_text
PY=/vast/ishi/envs/boundary/bin/python
export FONT_DIR=$PROBE/fonts
OUT=$PROBE/out
mkdir -p $OUT /vast/ishi/gb1900/logs
cd $PROBE

echo "=== TRAIN ==="; nvidia-smi -L 2>/dev/null | head -1
$PY train.py --tiles $MR/region/tiles /vast/ishi/gb1900/tiles/16 --out $OUT --steps 4000 || exit 1

echo "=== EMBED + CLUSTER ==="
$PY embed_cluster.py --enc $OUT/encoder.pt \
    --boxes "$MR/region/boxes/worker*.jsonl" \
    --tiles $MR/region/tiles /vast/ishi/gb1900/tiles/16 \
    --hitl $MR/hitl/manifest_clean.json \
    --out $OUT --nmax 2500 || exit 1
echo "=== DONE ==="; ls -R $OUT | head -60
