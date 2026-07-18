#!/bin/bash
# Font probe iteration 3: flat-field + real-consistency + FEW-SHOT REAL ANCHORS (--labels).
#   sbatch -M gpu --account=ishi --partition=l40s --gres=gpu:1 developer/.../run3.sbatch
#SBATCH --job-name=gbfont-i3
#SBATCH --account=ishi
#SBATCH --partition=l40s
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH --time=03:00:00
#SBATCH --output=/vast/ishi/gb1900/logs/fontprobe-i3-%j.out

set -uo pipefail
PROBE=/vast/ishi/gb1900/probe/font
MR=/vast/ishi/gb1900/probe/mapreader_text
PY=/vast/ishi/envs/boundary/bin/python
export FONT_DIR=$PROBE/fonts
OUT=$PROBE/out3
mkdir -p $OUT /vast/ishi/gb1900/logs
cd $PROBE

echo "=== TRAIN (iter3: + few-shot real anchors) ==="; nvidia-smi -L 2>/dev/null | head -1
$PY train.py --tiles $MR/region/tiles /vast/ishi/gb1900/tiles/16 \
    --boxes "$MR/region/boxes/worker*.jsonl" --labels $PROBE/font_labels.json \
    --out $OUT --steps 4000 --lam 0.5 || exit 1

echo "=== EMBED + CLUSTER ==="
$PY embed_cluster.py --enc $OUT/encoder.pt \
    --boxes "$MR/region/boxes/worker*.jsonl" \
    --tiles $MR/region/tiles /vast/ishi/gb1900/tiles/16 \
    --hitl $MR/hitl/manifest_clean.json --out $OUT --nmax 2500 || exit 1

echo "=== ANCHOR-kNN CLASSIFY (proper iter3 eval) ==="
$PY classify_real.py --enc $OUT/encoder.pt \
    --boxes "$MR/region/boxes/worker*.jsonl" \
    --tiles $MR/region/tiles /vast/ishi/gb1900/tiles/16 \
    --labels $PROBE/font_labels.json --out $OUT --nmax 2500 || exit 1
echo "=== DONE ==="; cat $OUT/synth_val.json; echo; cat $OUT/classify_report.json
