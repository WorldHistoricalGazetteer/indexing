#!/bin/bash
# Canonical re-embed — compute phase, as a preempt job array.
#
#   sbatch -M gpu --account=ishi --array=0-255%100 \
#          processing/reembed_canonical.sbatch /vast/ishi/reembed/<run-id>
#
# WHY PREEMPT. It is a large idle pool and this job is interruptible by
# construction: every shard is written to a temp file and renamed, a completed
# shard carries a .done marker, and a re-run skips it. `--requeue` is safe ONLY
# because of that. If you change the output path handling, re-check that
# invariant before you keep the flag.
#
# THREE TRAPS, each of which costs a day:
#
#   1. The QOS is `gpu-preempt-l`. It is UNDISCOVERABLE — `sacctmgr` does not
#      list it and `gpu-a100-l` is rejected on this partition. It is correct.
#      Do not "fix" it to something sacctmgr can see.
#   2. `sbatch --test-only` validates a job with the WRONG QOS. It does not
#      check QOS/partition permission, so it is not a safety net here. The only
#      test is a real submission — submit ONE array index first.
#   3. preempt also holds rtx6k (Turing) nodes, where this torch build dies with
#      "no kernel image is available". Hence the constraint. Drop it only if you
#      have positively established the environment supports Turing; without it
#      the job dies silently on those nodes.
#
# This sbatch deliberately does NOT source scripts/_common.sh. That file runs
# `[ -d "$JAVA_HOME/bin" ]` at source time with JAVA_HOME=/ix1/ishi/jdk-21.0.1,
# an unbounded stat on a hard NFS mount that hangs whenever /ix1 wedges — which
# it was doing on the day this was written. Nothing here needs Java.
#
#SBATCH --job-name=reembed-canonical
#SBATCH --partition=preempt
#SBATCH --qos=gpu-preempt-l
#SBATCH --constraint=a100|l40s
#SBATCH --requeue
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=02:00:00
#SBATCH --output=/vast/ishi/elastic/logs/reembed-%A_%a.out
#SBATCH --error=/vast/ishi/elastic/logs/reembed-%A_%a.out

set -euo pipefail

# TWO directories, deliberately separate. CODE_DIR is the immutable staged tree
# (`git archive <sha>` — see `reembed stage`); RUN_DIR holds the shards, the pin
# and the ledger. Running from the live checkout instead of the staged tree is
# the whole fault staging exists to prevent, so the code directory is an
# explicit argument rather than a default that could quietly be the wrong one.
CODE_DIR="${1:?usage: sbatch reembed_canonical.sbatch <code-dir> <run-dir>}"
RUN_DIR="${2:?usage: sbatch reembed_canonical.sbatch <code-dir> <run-dir>}"
SHARD="${SLURM_ARRAY_TASK_ID:?this script must be submitted as an array}"

# The array index maps 1:1 onto an export slice, so the array width MUST equal
# the export's --slices. A mismatch is not a partial run: indices above the
# slice count find no shard and abort, and slices below it are never computed.

echo "=== reembed shard ${SHARD} on $(hostname) at $(date -Is) ==="
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true

# `set -u` is off across conda activate on purpose: the activation script reads
# unset variables and would abort the job under -u.
set +u
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
set -u

cd "${CODE_DIR}"
echo "running from staged code: $(pwd)"
python -c "import sys, unicodedata; print(f'python {sys.version.split()[0]} unicodedata {unicodedata.unidata_version}')"

python -u -m processing.reembed compute \
    --in-dir "${RUN_DIR}" \
    --shard-id "${SHARD}" \
    --device cuda \
    --scope all

echo "=== shard ${SHARD} finished at $(date -Is) ==="
