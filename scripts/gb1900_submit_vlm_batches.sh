#!/bin/bash
# One-shot: submit an h200 VLM job for every ready batch manifest that hasn't been
# submitted yet, then EXIT. Run interactively from a login node at checkpoints
# (submit-and-poll — NOT a standing driver). The pitt cropper produces the batch
# manifests concurrently with the tile fetch; this just dispatches them.
#
#   bash scripts/gb1900_submit_vlm_batches.sh [batch_dir] [out_dir]
#
# Idempotent: a batch with a .submitted marker or existing shard output is skipped,
# so re-running only dispatches new batches.
set -uo pipefail
BATCH_DIR="${1:-/vast/ishi/gb1900/edition/batches}"
OUT_DIR="${2:-/vast/ishi/gb1900/edition/vlm}"
REPO="/vast/ishi/elastic"
mkdir -p "$OUT_DIR"

submitted=0; skipped=0
shopt -s nullglob
for man in "$BATCH_DIR"/batch_*.jsonl; do
    name=$(basename "$man" .jsonl)
    marker="$OUT_DIR/$name.submitted"
    out="$OUT_DIR/$name/shard-0.jsonl"
    if [[ -f "$marker" || -s "$out" ]]; then
        skipped=$((skipped+1)); continue
    fi
    jid=$(sbatch -M gpu --array=0-0 --parsable "$REPO/processing/gb1900_vlm.sbatch" \
          "$man" "$OUT_DIR/$name" 2>&1)
    if [[ "$jid" =~ ^[0-9] ]]; then
        echo "$jid" > "$marker"
        echo "submitted $name -> job $jid"
        submitted=$((submitted+1))
    else
        echo "FAILED to submit $name: $jid" >&2
    fi
done
echo "done: submitted=$submitted skipped=$skipped"
