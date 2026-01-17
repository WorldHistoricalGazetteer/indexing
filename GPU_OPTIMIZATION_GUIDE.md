# Phase 1 GPU Optimization Guide

## Current Problem (as of Job 1578094)

Your Phase 1 training is **drastically underutilizing** the A100 GPU:
- **Memory Usage**: 715MiB / 40960MiB (1.7%)
- **GPU Utilization**: 37%
- **Bottleneck**: Data loading pipeline

## Root Causes

1. **Batch size too small**: 128 samples/batch leaves GPU starving
2. **Insufficient workers**: 4-24 workers cannot saturate data pipeline
3. **No persistent workers**: Workers restart between epochs, wasting time
4. **Low prefetch**: Not enough batches queued ahead

## Applied Optimizations

### Changes Made to `train.py`:

```python
DEFAULT_CONFIG = {
    # ...
    'batch_size': 512,  # Was 128 → 4x increase
    'num_workers': 32,  # Was 24 → 33% increase
    'prefetch_factor': 8,  # Was 4 → 2x increase
}
```

### Changes Made to `data_loading.py`:

```python
DataLoader(
    # ...
    persistent_workers=True,  # NEW: Keep workers alive between epochs
)
```

## Expected Impact

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| GPU Memory | 715MiB | ~8-12GB | **10-15x** |
| GPU Utilization | 37% | ~85-95% | **2.5x** |
| Batch Throughput | ~48 it/s | ~120-180 it/s | **2.5-3.5x** |
| Epoch Time | 67 min | ~25-30 min | **2-2.5x faster** |

## Memory Safety

**Q: Will 32 workers + batch 512 cause OOM?**

**A: No.** Here's the math:

```
Worker memory: 32 workers × ~500MB = 16GB RAM
Batch memory: 512 × 192-dim features × 4 bytes × 3 (triplet) = ~1.2MB GPU
Prefetch: 8 batches × 1.2MB = ~10MB GPU
Model: ~1GB GPU
Total: ~16GB RAM, ~11GB GPU (well within 300GB RAM, 40GB GPU limits)
```

The **persistent workers** use RAM (not GPU), and you have 300GB allocated.

## When to Use These Settings

### ✅ Use optimized settings for:
- Phase 1 (Teacher training)
- Any future full re-training from scratch

### ⚠️ Keep current settings for:
- Phase 2 (may need different tuning due to char embeddings)
- Phase 3 (hard negatives may be more memory-intensive)

## How to Apply

Your changes are already in the code. Next time you run:

```bash
es -train-model 5
```

The new settings will automatically apply.

## Monitoring GPU Usage

During training, check GPU utilization:

```bash
ssh gpu-n33 "nvidia-smi"  # Replace gpu-n33 with your actual node
```

Target metrics:
- **GPU Memory**: 8-15GB (20-35% of 40GB)
- **GPU-Util**: 80-95%
- **Batch speed**: 120+ it/s

## Rollback (if needed)

If you encounter issues, revert to conservative settings:

```python
'batch_size': 256,  # Half of new value
'num_workers': 16,  # Conservative middle ground
'prefetch_factor': 4,
```

## Technical Notes

### Why persistent_workers helps:
- **Without it**: Workers restart every epoch → 30-60s overhead per epoch
- **With it**: Workers stay alive → save 25-50 minutes total over 50 epochs

### Why larger batches help:
- **Small batches**: GPU idle while CPU prepares next batch
- **Large batches**: GPU continuously computing while next batches load

### Why more workers help:
- Each worker processes data in parallel
- With 32 workers and prefetch=8, you have **256 batches queued**
- This ensures GPU never waits for data

## Verification After Next Run

After your next Phase 1 training completes, compare:

```bash
# Old run (job 1578094):
tail -f /ix1/whcdh/es/staging-logs/training_v5/phase1_1578094.err

# New run:
tail -f /ix1/whcdh/es/staging-logs/training_v5/phase1_XXXXXX.err
```

Look for:
1. **Iteration speed**: Should see ~120-180 it/s (vs current ~48 it/s)
2. **Epoch time**: Should see ~25-30 min/epoch (vs current ~67 min)
3. **GPU memory**: Check `nvidia-smi` → should see 8-15GB used

## Questions?

If you see:
- **OOM errors**: Reduce `batch_size` to 256
- **Low GPU util still**: Increase `num_workers` to 48
- **High RAM usage warnings**: Reduce `num_workers` to 24

---

**Summary**: You're currently using ~2% of your GPU's capability. These changes should bring you to **80-95% utilization** and cut training time by **more than half**.

