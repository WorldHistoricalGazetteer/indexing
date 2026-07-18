#!/usr/bin/env bash
# GB-STAMP pipeline — orchestration runbook (replicable record of every stage).
#
# This is a STAGED runbook, not a one-click job: the pipeline spans the pitt VM (fetch,
# crop-driver history, long processes) and the CRC Slurm clusters (gpu/htc). Run one stage
# at a time:  bash processing/gb1900/orchestrate.sh <stage>
# Stages are idempotent/resumable; state lives on /vast, keyed on gb:<pin_id>. Nothing here
# deletes data. See developer/plan-gb1900-typing.md §0a (as-built) / §0b (stop-and-tune).
#
# HOSTS:  pitt = gazetteer VM (ssh pitt);  crc0 = a CRC login node for `sbatch` (ssh crc0,
#         falls back to crc1/crc2/crc3). NEVER run compute on a login node — only submit.
set -uo pipefail

# ---- config ---------------------------------------------------------------
REPO=/vast/ishi/elastic                                  # shared repo on /vast (both hosts)
ED=/vast/ishi/gb1900/edition
TILES=/vast/ishi/gb1900/tiles
CROPS=/vast/ishi/gb1900/crops/national
BENV=/vast/ishi/envs/boundary/bin/python                 # CPU env (PIL/scipy/pyshp/sklearn/cv2)
VLLM_ENV=/vast/ishi/envs/vllm                            # GOTW-shared vLLM env (GPU)
WHG=/home/gazetteer/miniconda/envs/whg/bin/python        # pitt whg env (fetch/crop legacy)
GAZ=/vast/ishi/gb1900/gb1900_gazetteer_complete.csv      # CC-BY-SA complete gazetteer (utf-16)
HCT=/vast/ishi/gb1900/probe/boundary/UKDefinitionA.shp   # HCT/ukhc historic counties (open)
NSHARDS=12
say(){ printf '\n=== %s ===\n' "$*"; }

stage="${1:-help}"
case "$stage" in

setup-envs)   # one-time. Creates the CPU env on /vast. (vLLM env is the GOTW-shared one.)
  bash "$(dirname "$0")/setup_envs.sh" ;;

setup-mapreader)  # one-time, tricky (detectron2 + MapTextPipeline + weights). See the script.
  bash "$(dirname "$0")/mapreader_setup.sh" ;;

fetch)        # PITT. Parallel S3 tile fetch over ALL labels, retry-loop backstop (~2-3h).
  say "fetch (run ON pitt)"
  echo "ssh pitt: cd $REPO && nohup bash -c 'for i in \$(seq 1 100); do \\"
  echo "  $WHG -m processing.gb1900.tiles fetch --pins $ED/national_typed.jsonl --zoom 16 --workers 16 && break; \\"
  echo "  echo retry \$i; sleep 20; done' >> $ED/national_fetch.log 2>&1 &" ;;

crop)         # CRC htc. Sharded cropper (12-way). Needs the fetch complete (all tiles cached).
  say "crop (submit from crc0)"
  ssh crc0 "sbatch -M htc --array=0-$((NSHARDS-1)) $REPO/processing/gb1900/crop_shard.sbatch" ;;

vlm)          # CRC gpu. Autonomous VLM worker pool (a100 TP=2 ×4 + h200 TP=1 ×2 = account cap).
  say "vlm workers (submit from crc0)"
  ssh crc0 "cd $REPO && \
    sbatch -M gpu --partition=a100 --qos=gpu-a100-l --gres=gpu:2 --array=0-3 --export=ALL,TP=2 \
      processing/gb1900/vlm_worker.sbatch $ED/batches $ED/vlm && \
    sbatch -M gpu --array=0-1 processing/gb1900/vlm_worker.sbatch $ED/batches $ED/vlm" ;;

reconcile)    # merge VLM shards + tier0 -> edition (the last VLM worker also does this inline).
  say "reconcile"
  ssh pitt "cd $REPO && cat $ED/vlm/*/shard-0.jsonl > $ED/national_vlm.jsonl && \
    $BENV -m processing.gb1900.reconcile --tier0 $ED/national_typed.jsonl \
      --vlm $ED/national_vlm.jsonl --out $ED/gb-stamp_edition.jsonl --version gbtype-v1" ;;

date)         # sheet-precise per-label dating (needs the NLS sheet index geojson).
  say "date"
  ssh pitt "cd $REPO && $BENV -m processing.gb1900.dating --edition $ED/gb-stamp_edition.jsonl \
    --sheets $ED/../sheets/os_6inch_2nd_sheets.geojson --out $ED/gb-stamp_edition.dated.jsonl" ;;

admin)        # nation/district/parish via GB1900-gazetteer join + Voronoi(k-NN) fallback.
  say "admin join"
  ssh pitt "cd $REPO && $BENV -m processing.gb1900.admin_join --gazetteer $GAZ \
    --records $ED/national_typed.jsonl --out $ED/gb_admin.jsonl --encoding utf-16" ;;

county)       # historic county (HCT HCS code) via point-in-polygon of the label centre.
  say "county attribution"
  ssh pitt "cd $REPO && $BENV -m processing.gb1900.county_attribution --records $ED/national_typed.jsonl \
    --hct $HCT --out $ED/gb_hc_county.jsonl --uncertain-out $ED/gb_hc_county_uncertain.jsonl" ;;

export)       # CSV of the current edition + admin/county tags.
  say "export csv"
  ssh pitt "cd $REPO && $BENV -m processing.gb1900.export_csv --records $ED/national_typed.jsonl \
    --vlm-glob '$ED/vlm/*/shard-0.jsonl' --hct $HCT --out $ED/gb-stamp_so_far.csv" ;;

hitl)         # stratified font-review sample -> inject into the browser review tool.
  say "hitl sample"
  ssh pitt "cd $REPO && $BENV -m processing.gb1900.font_hitl_sample \
    --vlm-glob '$ED/vlm/*/shard-0.jsonl' --crops $CROPS \
    --lettering typesystem/data/gb1900_os_lettering.json --per-style 24 \
    --out /vast/ishi/gb1900/probe/hitl/manifest.json"
  echo "then inject: python processing/gb1900/hitl_build.py <manifest.json> processing/gb1900/font_hitl_review.html <out.html>" ;;

spotter)      # text-spotting (boxes = authority; VLM does reading). See mapreader_text/ + setup-mapreader.
  say "spotter — see developer/plan-gb1900-typing.md §12 + mapreader_setup.sh" ;;

status)
  ssh crc0 'squeue -M gpu -u stg135 -h -o "%.9P %.8T"|sort|uniq -c; squeue -M htc -u stg135 -h|wc -l|xargs echo htc-jobs:' ;;

help|*)
  grep -E '^[a-z-]+\)' "$0" | sed 's/)//' | tr -d ' ' | sort | \
    sed 's/^/  /' | { echo "GB-STAMP orchestrator — stages:"; cat; } ;;
esac
