#!/bin/bash
# GB1900 STAMP — final reconcile (auto-fires when the VLM worker array completes).
# Merges the per-batch VLM outputs and folds them into the provenance edition
# (Tier-0 typed + VLM-reconciled). Submit with a dependency on the worker array:
#
#   sbatch -M htc --dependency=afterany:<worker_jobid> processing/gb1900/reconcile.sbatch
#
#SBATCH --job-name=gbstamp-reconcile
#SBATCH --account=ishi
#SBATCH --clusters=htc
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=02:00:00
#SBATCH --output=/vast/ishi/gb1900/logs/reconcile-%j.out

set -uo pipefail
ED=/vast/ishi/gb1900/edition
VLM_DIR="${1:-$ED/vlm}"
# merge all per-batch VLM shards
cat "$VLM_DIR"/batch_*/shard-0.jsonl > "$ED/national_vlm.jsonl" 2>/dev/null || true
echo "merged VLM records: $(wc -l < $ED/national_vlm.jsonl 2>/dev/null || echo 0)"

# reconcile + build the GB-STAMP edition (pure stdlib — any python3)
cd /vast/ishi/elastic
python3 -m processing.gb1900.reconcile \
  --tier0 "$ED/national_typed.jsonl" --vlm "$ED/national_vlm.jsonl" \
  --out "$ED/gb-stamp_edition.jsonl" --version gbtype-v1
echo "GB-STAMP edition -> $ED/gb-stamp_edition.jsonl"
