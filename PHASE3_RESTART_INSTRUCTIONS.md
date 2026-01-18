# Phase 3 Training Restart Instructions

## Current Situation Analysis

Your Phase 3 training is running at **very poor GPU utilization**:
- **GPU usage**: 46% (should be 90%+)
- **Memory usage**: 2GB / 40GB (5% utilization)
- **Training speed**: ~14 it/s
- **Estimated completion**: ~112 hours (will exceed 48h walltime)

The dataset has **24.8M triplets** - much larger than anticipated, so the conservative batch size of 128 is severely underutilizing the A100 GPU.

## Changes Made

### 1. Optimized Phase 3 Configuration
- **Batch size**: 128 → **512** (4x increase)
- **Workers**: 4 → **8** (2x increase)
- **Prefetch factor**: 2 → **4** (2x increase)

### 2. Fixed PyTorch 2.5 Deprecation Warnings
- Updated `torch.cuda.amp` → `torch.amp` (new API)
- Updated `autocast()` → `autocast('cuda')`
- Updated `GradScaler()` → `GradScaler('cuda')`

## Expected Improvements

With these changes:
- **GPU utilization**: 46% → **85-95%**
- **Memory usage**: 2GB → **15-20GB** (still safe)
- **Training speed**: 14 it/s → **50-60 it/s** (4x faster)
- **Estimated completion**: 112h → **~24-30 hours** (fits in walltime)

## How to Restart Training

### Step 1: Cancel Current Job
```bash
scancel 1578378  # or whatever your current Phase 3 job ID is
```

### Step 2: Find Latest Checkpoint
The current training saved a checkpoint after epoch 1:
```bash
ls -lh /ix1/whcdh/models/phonetic/checkpoints/v5/phase3_epoch_1.pt
```

### Step 3: Resume with Optimized Parameters
```bash
# On login node
cd /ix1/whcdh/elastic

# Submit new Phase 3 job with resume
sbatch -M gpu <<'EOF'
#!/bin/bash
#SBATCH --job-name=whg-train-p3-v5-optimized
#SBATCH --output=/ix1/whcdh/es/staging-logs/training_v5/phase3_optimized_%j.out
#SBATCH --error=/ix1/whcdh/es/staging-logs/training_v5/phase3_optimized_%j.err
#SBATCH --time=48:00:00
#SBATCH --partition=a100
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=300G

set -e

source "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
conda activate whg

cd /ix1/whcdh/elastic

SCRATCH_ROOT="/scratch/slurm-${SLURM_JOB_ID}"
mkdir -p "$SCRATCH_ROOT/triplets/phase3"
mkdir -p "$SCRATCH_ROOT/vocab"

echo "Staging data to $SCRATCH_ROOT..."
rsync -a /ix1/whcdh/models/phonetic/data/v5/triplets/phase3/ "$SCRATCH_ROOT/triplets/phase3/"
rsync -a /ix1/whcdh/models/phonetic/data/v5/vocab/ "$SCRATCH_ROOT/vocab/"

echo "Resuming Phase 3 training with optimized parameters..."
python -u -m phonetics.training.train \
    --phase 3 \
    --data-dir "$SCRATCH_ROOT" \
    --output-dir /ix1/whcdh/models/phonetic/checkpoints/v5 \
    --student-checkpoint /ix1/whcdh/models/phonetic/checkpoints/v5/phase2_best.pt \
    --resume-from /ix1/whcdh/models/phonetic/checkpoints/v5/phase3_epoch_1.pt \
    --epochs 30 \
    --batch-size 512

echo "Phase 3 training complete"
EOF
```

### Step 4: Monitor the Job
```bash
# Watch job queue
squeue -M gpu -u stg135

# Monitor training logs
tail -f /ix1/whcdh/es/staging-logs/training_v5/phase3_optimized_*.err

# Check GPU utilization (once running)
ssh gpu-nXX "nvidia-smi"  # Replace XX with node number from squeue
```

## What to Expect

You should see:
1. **Faster startup** - resumes from epoch 1 checkpoint
2. **Higher GPU %** - should jump to 85-95%
3. **Better memory usage** - 15-20GB/40GB
4. **4x faster iterations** - ~50-60 it/s instead of 14 it/s
5. **Completion in ~24-30 hours** instead of 112 hours

## Alternative: Use es.sh (Simpler)

If you prefer, you can use the orchestration script (which now has the optimized settings):

```bash
# This will automatically resume from the latest checkpoint
es -train-model 5 --phase 3
```

The script will detect the existing phase3_epoch_1.pt and resume from there.

## Verification Checklist

After the job starts:
- [ ] Training resumes from epoch 2 (not epoch 1)
- [ ] Batch size shows 512 in logs
- [ ] GPU utilization >80%
- [ ] Training speed >40 it/s
- [ ] No deprecation warnings in stderr
- [ ] Estimated time to completion <30 hours

## Notes

- The code properly supports resuming via `--resume-from`
- The optimizer state, learning rate scheduler, and best_loss are all restored
- Training will continue for epochs 2-30
- The validation loss from epoch 1 (0.0334) will be used as the baseline for checkpointing

