#!/bin/bash
# FULL-COVERAGE spotting driver for GB-STAMP. Runs on the pitt VM (network + long-process OK); grinds through
# all 35.5k label-bearing z17 regions (centres_all.txt) a batch at a time, protecting the shared /vast:
#   1. pick up to BATCH centres that have no boxes yet (retry-safe: a crashed sheet writes no file, so it is
#      re-picked; an empty region writes an empty file, so it is NOT re-picked)
#   2. guard /vast free space (pause if prod ES headroom is threatened)
#   3. prefetch that batch's tiles here on pitt (GPU nodes have no S3 route)
#   4. submit the GPU spot job to crc0 and poll squeue until it leaves the queue
#   5. prune the batch's tiles -> peak tile use stays ~one batch
# Idempotent / resumable: just re-launch; the done-set scan resumes where it left off.
set -u
cd /vast/ishi/gb1900/probe/font
PY=/vast/ishi/envs/boundary/bin/python
CENTRES=centres_all.txt
BATCH=${BATCH:-800}
MAXBATCHES=${MAXBATCHES:-0}             # 0 = run to completion; >0 = stop after N batches (validation)
MINFREE_KB=$((60*1024*1024))            # keep >=60 GB free on /vast for prod ES
SPOT_SBATCH=/vast/ishi/gb1900/probe/font/spot_coverage.sbatch
mkdir -p cov
echo "=== coverage_driver start $(date) BATCH=$BATCH total=$(wc -l < $CENTRES) ==="

nb=0
while :; do
  if [ "$MAXBATCHES" -gt 0 ] && [ "$nb" -ge "$MAXBATCHES" ]; then echo "=== MAXBATCHES $MAXBATCHES reached ==="; break; fi
  nb=$((nb+1))
  ls /vast/ishi/gb1900/edition/spot/boxes_*.jsonl 2>/dev/null | sed 's#.*/boxes_##; s#\.jsonl$##' > cov/done.txt
  awk 'NR==FNR{d[$0]=1;next} !($3 in d)' cov/done.txt "$CENTRES" | head -"$BATCH" > cov/batch.txt
  n=$(wc -l < cov/batch.txt)
  [ "$n" -eq 0 ] && { echo "=== ALLDONE $(date) $(wc -l < cov/done.txt) regions spotted ==="; break; }
  done_n=$(wc -l < cov/done.txt)
  echo "--- batch of $n ($done_n done) $(date) ---"

  free=$(df -k --output=avail /vast/ishi 2>/dev/null | tail -1)
  if [ -n "$free" ] && [ "$free" -lt "$MINFREE_KB" ]; then
    echo "  /vast low (${free}KB free < ${MINFREE_KB}); pausing 10 min"; sleep 600; continue
  fi

  $PY prefetch_tiles.py cov/batch.txt --r 8 --workers 64 2>&1 | tail -1

  raw=$(ssh crc0 "cd /vast/ishi/gb1900/probe/font && sbatch --parsable --export=ALL,CENTRES_FILE=/vast/ishi/gb1900/probe/font/cov/batch.txt $SPOT_SBATCH" 2>/dev/null)
  jid=${raw%%;*}                          # sbatch --parsable -M gpu returns "<jobid>;gpu"
  if ! [[ "$jid" =~ ^[0-9]+$ ]]; then echo "  submit failed ('$raw'); retry in 5 min"; sleep 300; continue; fi
  echo "  GPU job $jid submitted; polling"
  while ssh crc0 "squeue -M gpu -j $jid -h -o %T" 2>/dev/null | grep -qiE "pending|running|configuring|completing"; do sleep 120; done
  echo "  job $jid finished $(date)"

  $PY prune_tiles.py cov/batch.txt --r 8 2>&1 | tail -1
done
