#!/bin/bash
#SBATCH -M gpu
#SBATCH --partition=preempt
#SBATCH --account=ishi
#SBATCH --qos=gpu-preempt-l
#SBATCH --gres=gpu:1
#SBATCH --constraint=a100|l40s
#SBATCH -c 8
#SBATCH --mem=24G
#SBATCH -t 6-00:00:00
#SBATCH -J spotpre
#SBATCH --requeue
#SBATCH --array=9-14
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/spotall_%a.log
# The second half of the sweep, on spare capacity instead of behind the account's 8-GPU a100 cap.
#
# ishi may hold only 8 a100 GPUs at once, so shards 8-15 sat PENDING behind shards 0-7 and would not have
# started for ~3 days. The preempt partition has idle nodes and accepts our QOS; the cost is that a higher
# priority job can requeue us at any moment.
#
# That trade is nearly free HERE and would not be elsewhere: spot_sheet writes one file per region at the
# end of that region and skips any region whose boxes file already exists, so a preemption costs the single
# region in flight and nothing more. --requeue lets Slurm restart the shard automatically; it re-reads the
# output directory and carries on from where it stopped. Same shard numbering and the same 16-way split as
# spot_all.sbatch, so these regions are disjoint from the ones shards 0-7 are working.
#
# The CONSTRAINT is not optional. Unlike the a100 partition, preempt is heterogeneous: it also holds rtx6k
# (Turing) nodes, and this torch build has no Turing kernels. Shards landing on gpu-n86 raised "CUDA error:
# no kernel image is available for execution on the device" on every single region — and because do_region
# catches per region, they churned through the shard logging FAIL and exited reporting success with zero
# regions spotted. a100 and l40s are both verified working (gpu-n45, gpu-n61).
set -u
# Force UTF-8 regardless of the node. Python encodes text files with the LOCALE charset, and one
# preempt node (gpu-n61) has no UTF-8 locale — so the same code that ran clean on fifteen shards
# failed 34% of regions on that one, throwing away nine mosaics of inference each time. Don't rely
# on the compute node being configured the way the last one was.
export LC_ALL=C.UTF-8 LANG=C.UTF-8 PYTHONUTF8=1
cd /vast/ishi/gb1900/probe/font
export SPOT_OUT=/vast/ishi/gb1900/edition/spot2
export SPOT_NO_FETCH=1
# Retry on failure, because --requeue does NOT cover it. Slurm requeues on preemption and node failure; a
# process that EXITS non-zero is simply done, and the shard's 2,220 regions are silently orphaned. Shard 14
# hit `CUDA error: out of memory` building the model 26s in, on a preempt node whose GPU a co-tenant was
# already occupying. On a shared partition that will recur.
#
# Retrying in place is the right response because the work is resumable: each attempt re-reads SPOT_OUT and
# skips every region already written, so a retry resumes rather than repeats. The sleep gives a transient
# co-tenant time to release memory; if the GPU is persistently full, the attempts are cheap (~30s each) and
# the shard is then left visibly failed rather than quietly incomplete.
for attempt in 1 2 3 4 5; do
  OMP_NUM_THREADS=6 /vast/ishi/envs/mapreader/bin/python spot_sheet.py \
    --centres centres_all.txt --shard $SLURM_ARRAY_TASK_ID --of 16 --r 8 && break
  echo "attempt $attempt failed (exit $?), retrying in 300s" >&2
  sleep 300
done
