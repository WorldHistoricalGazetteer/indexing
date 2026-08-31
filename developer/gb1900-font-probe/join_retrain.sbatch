#!/bin/bash
#SBATCH -M htc
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH -c 16
#SBATCH --mem=64G
#SBATCH -t 08:00:00
#SBATCH -J joinretrain
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/join_retrain.log
# Early look at the join, trained on the corpus-fed pass while the sweep is still running.
#
# Two things make this worth doing before the sweep finishes: `gline` exists for the first time (the model's
# own baseline, so the tangent reconstruction can finally be ablated against it), and ~8,700 regions is two
# orders of magnitude more than the 60 the current model saw.
#
# Everything below is scored on ONE held-out split, produced by join_train and reused through --blocks-from.
# Mixing splits is how a change gets credited with an improvement that is really a change of denominator;
# the published 0.219/0.381/0.425 and the later 0.442/0.453 are not comparable for exactly that reason.
set -eu
export LC_ALL=C.UTF-8 LANG=C.UTF-8 PYTHONUTF8=1
cd /vast/ishi/gb1900/probe/font
PY=/vast/ishi/envs/mapreader/bin/python
SNAP=$SLURM_SCRATCH/snap
NEW=/vast/ishi/gb1900/edition/spot2
OLD=/vast/ishi/gb1900/edition/spot

# 1. Snapshot to node-local scratch. The sweep is writing into spot2 RIGHT NOW: spot_sheet writes a region's
#    file in one go at the end, so a reader can catch one part-written, and a half-line of JSON would either
#    crash the run or silently drop a region. A snapshot also makes this experiment repeatable, which a live
#    directory that grows by ~50 regions a minute is not.
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
        os.remove(p); bad += 1           # caught mid-write; it will be in the next snapshot
print(f"snapshot: {len(glob.glob(f'{sys.argv[1]}/boxes_*.jsonl'))} regions, {bad} dropped as part-written",
      flush=True)
EOF

echo "=== 1. TRAIN on the corpus-fed pass ==="
# Sample broadly, not deeply: --sample-per-region caps the draw from any one region so the budget spreads
# across the country rather than being taken entirely from a few dense towns, which is what produced the
# selection bias the first time.
$PY join_train.py --boxes "$SNAP/boxes_gb_*.jsonl" \
    --max-files 4000 --sample-per-region 200 --out join_rf6.joblib

echo; echo "=== 2. LEARNED join, held-out blocks ==="
$PY assemble_labels.py --boxes "$SNAP/boxes_gb_*.jsonl" --validate \
    --blocks-from join_rf6.test_blocks.json --model join_rf6.joblib --model-thr 0.5 \
    --max-lines 3 --centre-tol 0.25 --out /dev/null

echo; echo "=== 3. HAND-SET RULES, same blocks (no model) ==="
$PY assemble_labels.py --boxes "$SNAP/boxes_gb_*.jsonl" --validate \
    --blocks-from join_rf6.test_blocks.json \
    --max-lines 3 --centre-tol 0.25 --out /dev/null

echo; echo "=== 4. ABLATION: no end tangent, model's own baseline discarded ==="
GBSTAMP_NO_TANGENT=1 $PY assemble_labels.py --boxes "$SNAP/boxes_gb_*.jsonl" --validate \
    --blocks-from join_rf6.test_blocks.json --model join_rf6.joblib --model-thr 0.5 \
    --max-lines 3 --centre-tol 0.25 --out /dev/null

echo; echo "=== 5. CONTROL: same model, same blocks, OLD network-fed pass ==="
# Isolates the DATA. Same model, same held-out blocks, same parameters — the only thing that changes is
# whether the imagery underneath was complete. The old pass covers ~1,465 regions, so this scores whichever
# of them fall in the held-out blocks.
$PY assemble_labels.py --boxes "$OLD/boxes_gb_*.jsonl" --validate \
    --blocks-from join_rf6.test_blocks.json --model join_rf6.joblib --model-thr 0.5 \
    --max-lines 3 --centre-tol 0.25 --out /dev/null || echo "control skipped (no overlap in held-out blocks)"

echo; echo "JOINRETRAINDONE"
