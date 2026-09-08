#!/bin/bash
# Deploy repo files to the CRC code tree, and PROVE they arrived.
#
# 🛑 THREE TIMES IN ONE SESSION I COMMITTED LOCALLY AND RAN A STALE FILE ON CRC:
#   32 shard tasks died on `invalid choice: 'run'`
#   a merge ran without the provenance stamp and had to be cancelled mid-copy
#   a population-fix run reproduced the OLD number because the fix never shipped
# Each time the job "worked" -- it just wasn't running the code I had written.
# Resolving to remember is not a mechanism; this is.
#
# ⚠ A MATCHING HASH IS NOT ENOUGH ON ITS OWN. It proves the bytes travelled, not
# that they are the bytes with the capability you are about to invoke -- and the
# capability is what the job actually needs. So: hash AND a named symbol.
#
# usage:  deploy.sh <local-path> <remote-path> [required-symbol ...]
set -u
LOCAL="$1"; REMOTE="$2"; shift 2
HOST=pitt                       # shares /vast with CRC; no login node needed

scp -q -o ConnectTimeout=20 "$LOCAL" "$HOST:$REMOTE" || { echo "🛑 scp failed"; exit 1; }
ssh -o ConnectTimeout=20 "$HOST" "rm -rf \$(dirname $REMOTE)/__pycache__" 2>/dev/null

L=$(sha256sum "$LOCAL" | cut -d' ' -f1)
R=$(ssh -o ConnectTimeout=20 "$HOST" "sha256sum $REMOTE" 2>/dev/null | cut -d' ' -f1)
if [ "$L" != "$R" ]; then
  echo "🛑 HASH MISMATCH — local $L remote $R"; exit 1
fi
echo "  bytes match: ${L:0:16}…"

for sym in "$@"; do
  if ! ssh -o ConnectTimeout=20 "$HOST" "grep -q -- '$sym' $REMOTE" 2>/dev/null; then
    echo "🛑 DEPLOYED FILE LACKS '$sym' — the hash matched and the capability is absent,"
    echo "   which means you are deploying the wrong local file, not a stale remote one."
    exit 1
  fi
  echo "  capability present: $sym"
done
echo "✅ deployed $LOCAL -> $HOST:$REMOTE"
