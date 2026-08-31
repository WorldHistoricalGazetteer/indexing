#!/bin/bash
#SBATCH -M htc
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH -c 16
#SBATCH --mem=400G
#SBATCH -t 08:00:00
#SBATCH -J joineval
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/join_eval.log
# The four evaluations, on the model join_retrain.sbatch already trained (join_rf6.joblib).
#
# Split out and given 400G because the combined job was OOM-killed at 64G in stage 2. assemble_labels holds
# every word AND every candidate pair in memory at once; that was unremarkable when the join was scored on
# 60 regions and is not at ~2,700 held-out region files. htc has 1.5TB nodes, so this is an under-request,
# not a design limit — but if it OOMs again the answer is to bound the evaluation set, applied identically
# to all four runs, rather than to keep bidding the memory up.
#
# Training is NOT repeated: the model is on disk and re-running it would change nothing except the clock.
set -eu
export LC_ALL=C.UTF-8 LANG=C.UTF-8 PYTHONUTF8=1
cd /vast/ishi/gb1900/probe/font
PY=/vast/ishi/envs/mapreader/bin/python
SNAP=$SLURM_SCRATCH/snap
NEW=/vast/ishi/gb1900/edition/spot2
OLD=/vast/ishi/gb1900/edition/spot

mkdir -p "$SNAP"
cp "$NEW"/boxes_gb_*.jsonl "$SNAP"/ 2>/dev/null || true
$PY - "$SNAP" <<'EOF'
import glob, json, os, sys
bad = 0
for p in glob.glob(f"{sys.argv[1]}/boxes_*.jsonl"):
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                if ln.strip(): json.loads(ln)
    except Exception:
        os.remove(p); bad += 1
print(f"snapshot: {len(glob.glob(f'{sys.argv[1]}/boxes_*.jsonl'))} regions, {bad} dropped part-written",
      flush=True)
EOF

# -u so the stages report as they go. Without it Python block-buffers into the log file and a running job is
# indistinguishable from a hung one — which cost 45 minutes of watching a frozen log earlier.
run () { echo; echo "=== $1 ==="; shift; "$@"; }

run "LEARNED join, held-out blocks" \
  $PY -u assemble_labels.py --boxes "$SNAP/boxes_gb_*.jsonl" --validate \
    --blocks-from join_rf6.test_blocks.json --model join_rf6.joblib --model-thr 0.5 \
    --max-lines 3 --centre-tol 0.25 --out "$SLURM_SCRATCH/assembled.jsonl"

run "HAND-SET RULES, same blocks (no model)" \
  $PY -u assemble_labels.py --boxes "$SNAP/boxes_gb_*.jsonl" --validate \
    --blocks-from join_rf6.test_blocks.json \
    --max-lines 3 --centre-tol 0.25 --out "$SLURM_SCRATCH/assembled.jsonl"

run "ABLATION: no end tangent (model's own gline discarded)" \
  env GBSTAMP_NO_TANGENT=1 $PY -u assemble_labels.py --boxes "$SNAP/boxes_gb_*.jsonl" --validate \
    --blocks-from join_rf6.test_blocks.json --model join_rf6.joblib --model-thr 0.5 \
    --max-lines 3 --centre-tol 0.25 --out "$SLURM_SCRATCH/assembled.jsonl"

run "CONTROL: same model, same blocks, OLD network-fed pass" \
  $PY -u assemble_labels.py --boxes "$OLD/boxes_gb_*.jsonl" --validate \
    --blocks-from join_rf6.test_blocks.json --model join_rf6.joblib --model-thr 0.5 \
    --max-lines 3 --centre-tol 0.25 --out "$SLURM_SCRATCH/assembled.jsonl"

echo; echo "JOINEVALDONE"
