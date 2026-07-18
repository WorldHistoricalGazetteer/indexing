#!/bin/bash
# B' region proof: train a REAL-domain CRNN on (crop,transcript) pairs, then compare its encoder
# vs the synthetic StyleEncoder for font-style separability (per class + within size band).
#   sbatch -M gpu --account=ishi --partition=l40s --gres=gpu:1 developer/.../run_bprime.sbatch
#SBATCH --job-name=gbfont-bprime
#SBATCH --account=ishi
#SBATCH --partition=l40s
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH --time=04:00:00
#SBATCH --output=/vast/ishi/gb1900/logs/bprime-%j.out

set -uo pipefail
PROBE=/vast/ishi/gb1900/probe/font
MR=/vast/ishi/gb1900/probe/mapreader_text
PY=/vast/ishi/envs/boundary/bin/python
export FONT_DIR=$PROBE/fonts
OUT=$PROBE/out_bprime
mkdir -p $OUT /vast/ishi/gb1900/logs
cd $PROBE

echo "=== TRAIN CRNN (real-domain, region pairs) ==="; nvidia-smi -L 2>/dev/null | head -1
$PY train_crnn.py --boxes "$MR/region/boxes/worker*.jsonl" \
    --tiles $MR/region/tiles /vast/ishi/gb1900/tiles/16 --out $OUT --epochs 40 || exit 1

echo "=== EVAL: crnn vs synthetic encoder (per class + within size band) ==="
$PY crnn_eval.py --crnn $OUT/crnn.pt --vocab $OUT/vocab.json --synenc $PROBE/out3/encoder.pt \
    --boxes "$MR/region/boxes/worker*.jsonl" \
    --tiles $MR/region/tiles /vast/ishi/gb1900/tiles/16 \
    --labels $PROBE/font_labels.json --out $OUT || exit 1
echo "=== DONE ==="; cat $OUT/bprime_report.json
