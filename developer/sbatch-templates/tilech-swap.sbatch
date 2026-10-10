#!/bin/bash
#SBATCH --job-name=tilech-swap
#SBATCH --output=/vast/ishi/elastic-tile-channels/logs/tilech-swap-%j.out
#SBATCH --error=/vast/ishi/elastic-tile-channels/logs/tilech-swap-%j.err
#SBATCH --time=06:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
# place#166 prod swap (SG's go required for every submission). usage:
#   sbatch -M htc --export=ALL,NEW=<built dir>,BAK=<verified backup dir> swap.sbatch "<b> [<b>...]"
#   sbatch -M htc --export=ALL,NEW=...,BAK=... swap.sbatch "<b1> <b2> ..." whg   (push each in place, ONE
#                                                                                config rewrite + restart at the end)
# Optional: PUSH_MODE=auto|inplace|rename (default auto: rename at >= 200 MiB, see
# generate_tiles.ATOMIC_PUSH_MIN_BYTES), REPO=<checkout holding the tooling>.
#
# Per bucket, scheme chosen by generate_tiles.push_mode_for:
#   rename  : generate_tiles --redeploy-only --push-mode rename does the WHOLE swap — df refusal unless the
#             new file + margin fits, rsync to <b>.mbtiles.new (live file untouched), mv live→.prev and
#             .new→live in one shell, update_tileserver_config --execute (restart + /data/<b>.json), TileJSON
#             layer check, rm .prev — or on a failed check, .prev renamed back + restart (fast rollback).
#             This job then re-checks the TileJSON independently and, if the tool failed, proves by sha256
#             whether the tool's own rollback restored the live file; only if it did not is the verified
#             /ix1 backup rsynced back.
#   inplace : the 10 Oct flow — rsync --inplace → update_tileserver_config --execute → TileJSON check →
#             rollback from the backup on any failure. Kept for small buckets (torn for seconds, not minutes).
# A monitor on the tileserver polls /data/<b>.json and z0/0/0 every 0.25 s for the push + restart;
# non-200 samples x 0.25 s is the measured unavailability for that bucket. Any failure STOPS the job.
set -o pipefail
NEW=${NEW:?set NEW=<dir holding <b>.mbtiles + <b>.channels.json>}
BAK=${BAK:?set BAK=<verified live backup dir with SHA256SUMS>}
REPO=${REPO:-/vast/ishi/elastic-tile-channels}
PUSH_MODE=${PUSH_MODE:-auto}
K=/vast/ishi/secrets/tileserver_id_ed25519
H=whgadmin@134.209.177.234
T=/srv/tileserver/tiles
SSH="ssh -i $K -o BatchMode=yes -o ConnectTimeout=20 $H"
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh && conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
cd $REPO || exit 2
MODE="${2:-each}"
BUCKETS="$1"
LOGDIR=$REPO/logs/swap-$SLURM_JOB_ID; mkdir -p $LOGDIR

echo "SWAP START $(date -u +%FT%TZ) mode=$MODE push_mode=$PUSH_MODE buckets: $BUCKETS"
echo "NEW=$NEW BAK=$BAK REPO=$REPO ($(git -C $REPO rev-parse --short HEAD))"
echo "--- preflight"
for b in $BUCKETS; do
  [ -f $NEW/$b.mbtiles ] || { echo "PREFLIGHT FAIL: no $NEW/$b.mbtiles"; exit 2; }
  [ -f $NEW/$b.channels.json ] || { echo "PREFLIGHT FAIL: no ledger for $b"; exit 2; }
  [ -f $BAK/$b.mbtiles ] || { echo "PREFLIGHT FAIL: no backup for $b (refusing to push a bucket that cannot be rolled back)"; exit 2; }
  grep -q " $b.mbtiles\$" $BAK/SHA256SUMS || { echo "PREFLIGHT FAIL: $b.mbtiles not in $BAK/SHA256SUMS"; exit 2; }
done
$SSH "df -B1 $T | tail -1; df -h $T | tail -1" || { echo "PREFLIGHT FAIL: cannot ssh to the tileserver"; exit 2; }

scheme_for() {  # $1 bucket -> rename|inplace
  python -c "from pathlib import Path; from processing.generate_tiles import push_mode_for; print(push_mode_for(Path('$NEW/$1.mbtiles'), '$PUSH_MODE'))" 2>/dev/null
}
monitor_start() {  # $1 bucket
  $SSH "rm -f /tmp/swapdone-$1 /tmp/swapmon-$1.txt; nohup bash -c 'bad=0; n=0; end=\$((SECONDS+7200)); while [ ! -f /tmp/swapdone-$1 ] && [ \$SECONDS -lt \$end ]; do c1=\$(curl -s -o /dev/null -w %{http_code} --max-time 2 http://localhost:8080/data/$1.json); c2=\$(curl -s -o /dev/null -w %{http_code} --max-time 2 http://localhost:8080/data/$1/0/0/0.pbf); n=\$((n+1)); { [ \"\$c1\" != 200 ] || { [ \"\$c2\" != 200 ] && [ \"\$c2\" != 204 ]; }; } && bad=\$((bad+1)); sleep 0.25; done; echo \"samples=\$n bad=\$bad\" > /tmp/swapmon-$1.txt' >/dev/null 2>&1 &"
}
monitor_stop() {  # $1 bucket -> prints samples/bad
  $SSH "touch /tmp/swapdone-$1; for i in 1 2 3 4 5 6 7 8; do [ -f /tmp/swapmon-$1.txt ] && break; sleep 1; done; cat /tmp/swapmon-$1.txt 2>/dev/null || echo 'samples=? bad=?'"
}
want_fields() {  # $1 bucket -> comma list of TileJSON fields the ledger implies
  python - "$NEW/$1.channels.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1])); c = d.get("counts") or {}
want = []
if (c.get("polygon", 0) + c.get("line", 0)) and d.get("label_anchors"): want.append("label")
if d.get("extent"): want.append("coverage")
print(",".join(want))
EOF
}
check_tilejson() {  # $1 bucket $2 wanted fields -> exit 0 if served with exactly this layer and those fields
  local want="$2"
  $SSH "curl -s --max-time 10 http://localhost:8080/data/$1.json" > $LOGDIR/tilejson.$1
  python - "$1" "$want" "$LOGDIR/tilejson.$1" <<'EOF'
import json, sys
b, want = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(sys.argv[3]))
except Exception as e:
    print(f"  CHECK {b}: TileJSON unreadable: {e}"); sys.exit(1)
vl = d.get("vector_layers") or []
ids = [v.get("id") for v in vl]
fields = set((vl[0].get("fields") or {}).keys()) if vl else set()
missing = [f for f in want.split(",") if f and f not in fields]
ok = ids == [b] and not missing
print(f"  CHECK {b}: layers={ids} fields_has_label={'label' in fields} coverage={'coverage' in fields} missing={missing} -> {'OK' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
EOF
}
live_matches_backup() {  # $1 bucket -> exit 0 if the tileserver's file IS the verified backup (sha256)
  local live=$($SSH "sha256sum $T/$1.mbtiles" | cut -d" " -f1)
  local bak=$(grep " $1.mbtiles\$" $BAK/SHA256SUMS | cut -d" " -f1)
  echo "    tileserver sha256: ${live:0:16}  backup: ${bak:0:16}"
  [ -n "$live" ] && [ "$live" = "$bak" ]
}
rollback() {  # $1 bucket — full restore from the verified backup, then restart + verify
  echo "  ROLLBACK $1: restoring $BAK/$1.mbtiles"
  rsync -a --inplace --info=stats1 -e "ssh -i $K -o BatchMode=yes" $BAK/$1.mbtiles $H:$T/ | tail -1
  $SSH "rm -f $T/$1.mbtiles.new $T/$1.mbtiles.prev $T/$1.mbtiles.rejected"
  python -u -m processing.update_tileserver_config --bucket $1 --execute 2>&1 | grep -E "serving|rolled|rewritten|restart|services" | sed 's/^/    /'
  live_matches_backup $1 && echo "    rollback verified: live == backup" || echo "    ROLLBACK NOT VERIFIED — check the host by hand"
}

RC=0
for b in $BUCKETS; do
  scheme=$(scheme_for $b); [ -n "$scheme" ] || { echo "cannot decide the push scheme for $b"; RC=1; break; }
  [ "$MODE" = "whg" ] && scheme=inplace
  echo; echo "################ $b  $(date -u +%FT%TZ)  new=$(stat -c %s $NEW/$b.mbtiles) bytes, live=$(stat -c %s $BAK/$b.mbtiles)  scheme=$scheme"
  want=$(want_fields $b)
  monitor_start $b
  t0=$(date +%s)
  if [ "$scheme" = "rename" ]; then
    # the tool does push → rename → restart → check → cleanup/rollback; do NOT restart again after it
    python -u -m processing.generate_tiles --bucket $b --output-dir $NEW --redeploy-only --push-mode rename > $LOGDIR/push.$b 2>&1
    prc=$?
    grep -E "PUSH MODE|disk |REFUSED|pushed|renamed|rewritten|restart|serving|TileJSON|check|rollback|ROLLBACK|removed|✗|summary" $LOGDIR/push.$b | sed 's/^/  /'
    t2=$(date +%s)
    mon=$(monitor_stop $b)
    echo "  swap (push+rename+restart+check)=$((t2-t0))s  monitor: $mon (x0.25 s = unavailability)"
    if [ $prc -ne 0 ] || ! check_tilejson $b "$want"; then
      echo "  SWAP FAILED for $b (rc=$prc)"
      if live_matches_backup $b; then echo "  the tool's own rollback restored the live file (sha256 == backup); no rsync needed"
      else rollback $b; fi
      RC=1; break
    fi
  else
    python -u -m processing.generate_tiles --bucket $b --output-dir $NEW --redeploy-only --push-mode inplace 2>&1 | grep -E "pushed|failed|✗|summary" | sed 's/^/  /'
    prc=${PIPESTATUS[0]}
    t1=$(date +%s)
    if [ $prc -ne 0 ]; then echo "  PUSH FAILED rc=$prc"; monitor_stop $b; rollback $b; RC=1; break; fi
    if [ "$MODE" = "whg" ]; then
      mon=$(monitor_stop $b); echo "  push=$((t1-t0))s  monitor: $mon (x0.25s)"
      continue
    fi
    python -u -m processing.update_tileserver_config --bucket $b --execute 2>&1 | grep -E "serving|rolled|rewritten|restart|services|Nothing|FAIL|rc=" | sed 's/^/  /'
    crc=${PIPESTATUS[0]}
    t2=$(date +%s)
    mon=$(monitor_stop $b)
    echo "  push=$((t1-t0))s restart+verify=$((t2-t1))s total=$((t2-t0))s  monitor: $mon (x0.25 s = unavailability)"
    if [ $crc -ne 0 ] || ! check_tilejson $b "$want"; then echo "  CHECK FAILED for $b"; rollback $b; RC=1; break; fi
  fi
  $SSH "sha256sum $T/$b.mbtiles | cut -c1-16" | sed 's/^/  tileserver sha256: /'
  sha256sum $NEW/$b.mbtiles | cut -c1-16 | sed 's/^/  new build sha256:  /'
  $SSH "ls $T/$b.mbtiles.* 2>/dev/null" | sed 's/^/  LEFTOVER on the host: /'
  $SSH "df -h $T | tail -1" | sed 's/^/  df: /'
done

if [ "$MODE" = "whg" ] && [ $RC -eq 0 ]; then
  echo; echo "################ whg-*: one config rewrite + restart for all $(echo $BUCKETS | wc -w) buckets"
  args=""; for b in $BUCKETS; do args="$args --bucket $b"; done
  python -u -m processing.update_tileserver_config $args --execute 2>&1 | grep -E "serving|rolled|rewritten|restart|services|Nothing|FAIL" | sed 's/^/  /'
  crc=${PIPESTATUS[0]}
  bad=""
  for b in $BUCKETS; do check_tilejson $b "$(want_fields $b)" || bad="$bad $b"; done
  if [ $crc -ne 0 ] || [ -n "$bad" ]; then
    echo "  whg CHECK FAILED for:$bad (config rc=$crc) — rolling those back"
    for b in $bad; do rollback $b; done; RC=1
  fi
fi
echo; echo "SWAP END $(date -u +%FT%TZ) rc=$RC"
$SSH "df -h $T | tail -1"
exit $RC
