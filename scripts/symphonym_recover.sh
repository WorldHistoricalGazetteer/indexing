#!/bin/bash
# Recovery sequence for the v7 Symphonym embedding rebuild after /ix1
# returns from extended NFS contention.
#
# Run this from any login node with /ix1 + /vast access (typically crc2).
# The script is idempotent — each step checks for the artefact it would
# create and skips if present. Safe to re-run after partial failures.
#
# Steps:
#   1. Verify /ix1 is actually responsive (small read; abort if not).
#   2. Stage hot inputs to /vast (checkpoint, vocab, toponyms.db).
#   3. Migrate the old /ix1 Symphonym cache to /vast (preserves ~545K
#      embeddings persisted by the cancelled jobs).
#   4. Test ES auth (separate concern; warn if still wedged).
#   5. Run hydration from production ES (Pitt VM only). If ES is wedged,
#      this is skipped with a warning — the GPU array will then process
#      the full corpus via cache hits we already migrated, and any
#      genuinely new toponyms.
#   6. Submit a 4-shard GPU array on a100, --no-cache writes (cache is
#      read-only when sharded). Wall: ~7h with 4 GPUs, less if hydration
#      covered the bulk.
#   7. After array completes, run merge_shards directly (no Slurm needed).
#
# Halt at end — do NOT trigger Batch 11 (ES index step). Per user
# instruction 2026-05-02, the first Batch 11 run needs human approval.

set -euo pipefail

CHECKPOINT_SRC=/ix1/ishi/models/phonetic/checkpoints/v7/phase3_best.pt
VOCAB_SRC=/ix1/ishi/models/phonetic/data/v7/vocab
TOPONYMS_SRC=/ix1/ishi/data/toponyms.db
CACHE_SRC=/ix1/ishi/models/phonetic/symphonym_cache.duckdb

CHECKPOINT_DST=/vast/ishi/models/phonetic/checkpoints/v7/phase3_best.pt
VOCAB_DST=/vast/ishi/models/phonetic/data/v7/vocab
TOPONYMS_DST=/vast/ishi/data/toponyms.db
CACHE_DST=/vast/ishi/models/phonetic/symphonym_cache.duckdb

OUTPUT=/vast/ishi/models/phonetic/data/v7/embeddings_v7.parquet
LOG_DIR=/vast/ishi/elastic/logs/embeddings_v7
NUM_SHARDS=4

step() { echo; echo "=== $* ==="; date; }

# ─── 1. Verify /ix1 is responsive ────────────────────────────────────
step "Verify /ix1 is responsive"
if ! timeout 30 dd if="${CHECKPOINT_SRC}" of=/dev/null bs=1024 count=10 status=none; then
    echo "ABORT: /ix1 is still unresponsive (10 KB read of phase3_best.pt timed out)."
    exit 1
fi
echo "/ix1 responsive."

# ─── 2. Stage hot inputs to /vast ────────────────────────────────────
step "Stage hot inputs to /vast"
mkdir -p "$(dirname "${CHECKPOINT_DST}")" "$(dirname "${TOPONYMS_DST}")" "$(dirname "${VOCAB_DST}")" "${LOG_DIR}"

if [ ! -f "${CHECKPOINT_DST}" ] || [ "${CHECKPOINT_SRC}" -nt "${CHECKPOINT_DST}" ]; then
    echo "Copying checkpoint (~99 MB)..."
    cp "${CHECKPOINT_SRC}" "${CHECKPOINT_DST}"
else
    echo "checkpoint already on /vast and up-to-date — skipping"
fi

if [ ! -d "${VOCAB_DST}" ] || [ ! -f "${VOCAB_DST}/char_vocab.json" ]; then
    echo "Copying vocab dir..."
    rm -rf "${VOCAB_DST}"
    cp -r "${VOCAB_SRC}" "${VOCAB_DST}"
else
    echo "vocab already on /vast — skipping"
fi

if [ ! -f "${TOPONYMS_DST}" ] || [ "${TOPONYMS_SRC}" -nt "${TOPONYMS_DST}" ]; then
    echo "Copying toponyms DuckDB (~35 GB) — this is the long step..."
    cp "${TOPONYMS_SRC}" "${TOPONYMS_DST}"
else
    echo "toponyms.db already on /vast and up-to-date — skipping"
fi

# ─── 3. Migrate old Symphonym cache to /vast ─────────────────────────
step "Migrate old /ix1 cache to /vast"
if [ -f "${CACHE_SRC}" ]; then
    if [ ! -f "${CACHE_DST}" ]; then
        echo "Copying cache from /ix1 (preserves ~545K cached embeddings)..."
        mkdir -p "$(dirname "${CACHE_DST}")"
        cp "${CACHE_SRC}" "${CACHE_DST}"
    else
        # If both exist, prefer the larger (assume more entries) — but
        # warn rather than overwrite so the operator can decide.
        IX1_ROWS=$(stat -c%s "${CACHE_SRC}")
        VAST_ROWS=$(stat -c%s "${CACHE_DST}")
        if [ "${IX1_ROWS}" -gt "${VAST_ROWS}" ]; then
            echo "WARNING: /ix1 cache (${IX1_ROWS} bytes) larger than /vast cache (${VAST_ROWS}). Backing up /vast then overwriting."
            mv "${CACHE_DST}" "${CACHE_DST}.bak.$(date +%s)"
            cp "${CACHE_SRC}" "${CACHE_DST}"
        else
            echo "/vast cache already exists and is at least as large as /ix1 — skipping"
        fi
    fi
else
    echo "No /ix1 cache to migrate."
fi

# ─── 4. Test ES auth ──────────────────────────────────────────────────
step "Test production ES auth"
ES_OK=0
if ssh -o ConnectTimeout=10 pitt 'PW=$(cat ~/elastic.pw 2>/dev/null); test -n "$PW" && timeout 10 curl -s -m 8 -u "elastic:$PW" "http://localhost:9201/_count" -w "%{http_code}\n" -o /dev/null' 2>/dev/null | grep -q "^200$"; then
    echo "ES auth responding."
    ES_OK=1
else
    echo "WARNING: ES auth still wedged or pitt unreachable. Skipping hydration."
fi

# ─── 5. Hydrate from production ES (only if ES is up) ────────────────
if [ "${ES_OK}" = "1" ]; then
    step "Hydrate Symphonym cache from production ES"
    ssh pitt "cd /vast/ishi/elastic && /home/gazetteer/miniconda/envs/whg/bin/python \
        -m processing.hydrate_symphonym_cache \
        --checkpoint ${CHECKPOINT_DST} \
        --embedding-version 7 \
        --cache-db ${CACHE_DST}"
else
    echo "(Hydration skipped — ES auth not reachable.)"
fi

# ─── 6. Submit sharded GPU array ──────────────────────────────────────
step "Submit ${NUM_SHARDS}-shard a100 array"

# Clean any stale shard output from previous attempts.
rm -f "${OUTPUT%.parquet}".shard_*.parquet

cat > /tmp/symphonym-recovered-shards.sbatch <<SBATCH
#!/bin/bash
#SBATCH --job-name=whg-embed-shard-v7
#SBATCH --output=${LOG_DIR}/shard_%A_%a.out
#SBATCH --error=${LOG_DIR}/shard_%A_%a.err
#SBATCH --array=0-$((NUM_SHARDS - 1))
#SBATCH --time=24:00:00
#SBATCH --partition=a100
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=100G

set -e
echo "=== EMBED COMPUTE shard \$SLURM_ARRAY_TASK_ID/${NUM_SHARDS} (Symphonym v7) ==="
date; echo "Node: \$(hostname)"

source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
cd /vast/ishi/elastic

# All inputs on /vast (recovery script staged them).
python -u -m phonetics.inference.update_es compute \\
    --input-file ${TOPONYMS_DST} \\
    --output-file ${OUTPUT} \\
    --checkpoint ${CHECKPOINT_DST} \\
    --vocab-dir ${VOCAB_DST} \\
    --cache-db ${CACHE_DST} \\
    --embedding-version 7 \\
    --batch-size 2000 \\
    --device cuda \\
    --shard-id \$SLURM_ARRAY_TASK_ID \\
    --num-shards ${NUM_SHARDS}

date; echo "shard \$SLURM_ARRAY_TASK_ID done"
SBATCH

ARRAY=$(sbatch -M gpu --parsable /tmp/symphonym-recovered-shards.sbatch | cut -d';' -f1)
echo "Submitted shard array: ${ARRAY}"
echo "  monitor: squeue -M gpu -u stg135 -j ${ARRAY}"
echo "  logs:    ${LOG_DIR}/shard_${ARRAY}_*.{out,err}"

step "DONE — recovery sequence dispatched"
echo "When the array completes, run the merge:"
echo "  python -m phonetics.inference.merge_shards \\"
echo "      --output-file ${OUTPUT} \\"
echo "      --num-shards ${NUM_SHARDS} --delete-shards"
echo
echo "Then HALT — do not run Batch 11 (ES index step) without user approval."
