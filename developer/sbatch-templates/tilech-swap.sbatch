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
# place#166 prod swap (SG's go, 10 Oct 2026). usage:
#   sbatch -M htc swap.sbatch "<bucket> [<bucket>...]"   (pushed in the order given)
#   sbatch -M htc swap.sbatch "<b1> <b2> ..." whg           (2nd arg "whg": push each, ONE config
#                                                           rewrite + restart at the end)
# Per bucket: push (rsync --inplace, direct) -> update_tileserver_config --execute (config merge,
# restart, /data/<b>.json on the tileserver's localhost) -> independent check of the TileJSON
# fields -> on ANY failure, rsync the verified backup back, --execute again, and STOP.
# A monitor on the tileserver polls /data/<b>.json and z0/0/0 every 0.25 s during the push +
# restart; non-200 samples x 0.25 s is the measured downtime for that bucket.
set -o pipefail
NEW=/ix1/ishi/data/tiles-channels-20261010b
BAK=/ix1/ishi/data/tiles-live-backup-20261010
K=/vast/ishi/secrets/tileserver_id_ed25519
H=whgadmin@134.209.177.234
T=/srv/tileserver/tiles
SSH="ssh -i $K -o BatchMode=yes -o ConnectTimeout=20 $H"
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh && conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
cd /vast/ishi/elastic-tile-channels
MODE="${2:-each}"
BUCKETS="$1"

echo "SWAP START $(date -u +%FT%TZ) mode=$MODE buckets: $BUCKETS"
echo "--- preflight"
for b in $BUCKETS; do
  [ -f $NEW/$b.mbtiles ] || { echo "PREFLIGHT FAIL: no $NEW/$b.mbtiles"; exit 2; }
  [ -f $NEW/$b.channels.json ] || { echo "PREFLIGHT FAIL: no ledger for $b"; exit 2; }
  [ -f $BAK/$b.mbtiles ] || { echo "PREFLIGHT FAIL: no backup for $b (refusing to push a bucket that cannot be rolled back)"; exit 2; }
done
$SSH "df -h $T | tail -1" || { echo "PREFLIGHT FAIL: cannot ssh to the tileserver"; exit 2; }

monitor_start() {  # $1 bucket
  $SSH "rm -f /tmp/swapdone-$1 /tmp/swapmon-$1.txt; nohup bash -c 'bad=0; n=0; end=\$((SECONDS+3600)); while [ ! -f /tmp/swapdone-$1 ] && [ \$SECONDS -lt \$end ]; do c1=\$(curl -s -o /dev/null -w %{http_code} --max-time 2 http://localhost:8080/data/$1.json); c2=\$(curl -s -o /dev/null -w %{http_code} --max-time 2 http://localhost:8080/data/$1/0/0/0.pbf); n=\$((n+1)); { [ \"\$c1\" != 200 ] || { [ \"\$c2\" != 200 ] && [ \"\$c2\" != 204 ]; }; } && bad=\$((bad+1)); sleep 0.25; done; echo \"samples=\$n bad=\$bad\" > /tmp/swapmon-$1.txt' >/dev/null 2>&1 &"
}
monitor_stop() {  # $1 bucket -> prints samples/bad
  $SSH "touch /tmp/swapdone-$1; for i in 1 2 3 4 5 6 7 8; do [ -f /tmp/swapmon-$1.txt ] && break; sleep 1; done; cat /tmp/swapmon-$1.txt 2>/dev/null || echo 'samples=? bad=?'"
}
check_tilejson() {  # $1 bucket -> exit 0 if served with the fields the ledger implies
  local want="$2"
  $SSH "curl -s --max-time 10 http://localhost:8080/data/$1.json" > /tmp/tilejson.$$.$1
  python - "$1" "$want" "/tmp/tilejson.$$.$1" <<'EOF'
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
rollback() {  # $1 bucket
  echo "  ROLLBACK $1: restoring $BAK/$1.mbtiles"
  rsync -a --inplace --info=stats1 -e "ssh -i $K -o BatchMode=yes" $BAK/$1.mbtiles $H:$T/ | tail -1
  python -u -m processing.update_tileserver_config --bucket $1 --execute 2>&1 | grep -E "serving|rolled|rewritten|restart|services" | sed 's/^/    /'
  $SSH "sha256sum $T/$1.mbtiles | cut -c1-16" | sed 's/^/    tileserver sha256 now: /'
  grep " $1.mbtiles\$" $BAK/SHA256SUMS | cut -c1-16 | sed 's/^/    backup sha256:         /'
}

RC=0
for b in $BUCKETS; do
  echo; echo "################ $b  $(date -u +%FT%TZ)  new=$(stat -c %s $NEW/$b.mbtiles) bytes, live=$(stat -c %s $BAK/$b.mbtiles)"
  want=""; python - "$NEW/$b.channels.json" > /tmp/want.$$ <<'EOF'
import json, sys
d = json.load(open(sys.argv[1])); c = d.get("counts") or {}
want = []
if (c.get("polygon", 0) + c.get("line", 0)) and d.get("label_anchors"): want.append("label")
if d.get("extent"): want.append("coverage")
print(",".join(want))
EOF
  want=$(cat /tmp/want.$$)
  monitor_start $b
  t0=$(date +%s)
  python -u -m processing.generate_tiles --bucket $b --output-dir $NEW --redeploy-only 2>&1 | grep -E "pushed|failed|✗|Deploy summary" | sed 's/^/  /'
  prc=${PIPESTATUS[0]}
  t1=$(date +%s)
  if [ $prc -ne 0 ]; then echo "  PUSH FAILED rc=$prc"; monitor_stop $b; rollback $b; RC=1; break; fi
  if [ "$MODE" = "whg" ]; then
    mon=$(monitor_stop $b); echo "  push ${t1}s-$((t1-t0))s elapsed=$((t1-t0))s  monitor: $mon (x0.25s)"
    continue
  fi
  python -u -m processing.update_tileserver_config --bucket $b --execute 2>&1 | grep -E "serving|rolled|rewritten|restart|services|Nothing|FAIL|rc=" | sed 's/^/  /'
  crc=${PIPESTATUS[0]}
  t2=$(date +%s)
  mon=$(monitor_stop $b)
  echo "  push=$((t1-t0))s restart+verify=$((t2-t1))s total=$((t2-t0))s  monitor: $mon (x0.25 s = downtime)"
  if [ $crc -ne 0 ] || ! check_tilejson $b "$want"; then echo "  CHECK FAILED for $b"; rollback $b; RC=1; break; fi
  $SSH "sha256sum $T/$b.mbtiles | cut -c1-16" | sed 's/^/  tileserver sha256: /'
  sha256sum $NEW/$b.mbtiles | cut -c1-16 | sed 's/^/  new build sha256:  /'
  case $b in po|clio|wd) $SSH "df -h $T | tail -1" | sed 's/^/  df: /';; esac
done

if [ "$MODE" = "whg" ] && [ $RC -eq 0 ]; then
  echo; echo "################ whg-*: one config rewrite + restart for all $(echo $BUCKETS | wc -w) buckets"
  args=""; for b in $BUCKETS; do args="$args --bucket $b"; done
  python -u -m processing.update_tileserver_config $args --execute 2>&1 | grep -E "serving|rolled|rewritten|restart|services|Nothing|FAIL" | sed 's/^/  /'
  crc=${PIPESTATUS[0]}
  bad=""
  for b in $BUCKETS; do
    want=$(python - "$NEW/$b.channels.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1])); c = d.get("counts") or {}
w = []
if (c.get("polygon", 0) + c.get("line", 0)) and d.get("label_anchors"): w.append("label")
if d.get("extent"): w.append("coverage")
print(",".join(w))
EOF
)
    check_tilejson $b "$want" || bad="$bad $b"
  done
  if [ $crc -ne 0 ] || [ -n "$bad" ]; then
    echo "  whg CHECK FAILED for:$bad (config rc=$crc) — rolling those back"
    for b in $bad; do rollback $b; done; RC=1
  fi
fi
rm -f /tmp/want.$$
echo; echo "SWAP END $(date -u +%FT%TZ) rc=$RC"
$SSH "df -h $T | tail -1"
exit $RC
