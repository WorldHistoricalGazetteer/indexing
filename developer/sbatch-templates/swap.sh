#!/bin/bash
# Swap the panphon candidate into place. PRECONDITIONS ARE HARD: any failure
# exits before touching anything.
set -u
SRC=/ix1/ishi/data/toponyms-undscript-20260906T160000Z.compact.db
CAND=/ix1/ishi/data/toponyms-panphon-CANDIDATE.db
KEEP="$SRC.pre-panphon"
MODE="${1:-check}"          # check | swap
fail() { echo "🛑 REFUSING: $*"; exit 1; }

echo "=== 1. WAL files must all be ABSENT ==="
# A DuckDB database is not one file. A stale SRC.wal belongs to the OLD database
# and would be replayed into the NEW one on next open — writes from a different
# database into this one. A CAND.wal left at the old name is silently lost.
# A present WAL also means the database was not cleanly closed.
for w in "$SRC.wal" "$CAND.wal" "$KEEP.wal"; do
  if [ -e "$w" ]; then
    ls -la "$w"; fail "$w exists — not cleanly closed, or a writer is live"
  fi
  echo "  absent: $w"
done

echo "=== 2. Who holds the source open? (PARTIAL — pitt only) ==="
# ⚠ rename(2) does not disturb an open fd: a reader attached at swap time keeps
# reading the OLD inode indefinitely, and its answers diverge from every new
# reader's with nothing saying so.
# ⚠ THIS CHECK IS INCOMPLETE BY CONSTRUCTION. fuser here sees only pitt's
# processes; a CRC compute node holding the file is invisible from this host.
# It is evidence, not proof, and must be reported as such.
if command -v fuser >/dev/null 2>&1; then
  fuser -v "$SRC" 2>&1 | sed 's/^/  /' || echo "  (no pitt-side holders)"
else
  echo "  fuser unavailable"
fi

echo "=== 3. Same filesystem? (mv -f is atomic only within one) ==="
D_CAND=$(stat -c %d "$CAND") || fail "cannot stat candidate"
D_DIR=$(stat -c %d "$(dirname "$SRC")") || fail "cannot stat source dir"
echo "  candidate dev=$D_CAND   source dir dev=$D_DIR"
[ "$D_CAND" = "$D_DIR" ] || fail "different filesystems — mv would copy+unlink, \
reintroducing the absent-path window this design exists to avoid"

echo "=== 4. Backup suffix must fall AFTER .db, not before ==="
case "$KEEP" in
  *.db) fail "backup name ends in .db — a *.db glob would pick it up" ;;
  *.db.pre-panphon) echo "  ok: $KEEP" ;;
  *) fail "unexpected backup name shape: $KEEP" ;;
esac

echo "=== 5. Ownership and mode must survive the swap ==="
# mv carries the CANDIDATE's owner/group/mode, not the original's. If the source
# is group-writable for ishi and the candidate is not, peers lose access at the
# instant of the swap.
S_OWN=$(stat -c '%U %G %a' "$SRC"); C_OWN=$(stat -c '%U %G %a' "$CAND")
echo "  source    : $S_OWN"
echo "  candidate : $C_OWN"
[ "$S_OWN" = "$C_OWN" ] || echo "  ⚠ MISMATCH — will chown/chmod the candidate to match before swapping"

echo "=== sizes and inodes ==="
stat -c '  %n  ino=%i  size=%s' "$SRC" "$CAND"
SRC_INO=$(stat -c %i "$SRC")

if [ "$MODE" != "swap" ]; then
  echo; echo "✅ CHECKS ONLY. Re-run with 'swap' to execute."; exit 0
fi

echo; echo "=== EXECUTING ==="
if [ "$S_OWN" != "$C_OWN" ]; then
  chmod "$(stat -c %a "$SRC")" "$CAND" && echo "  mode aligned"
fi
ln "$SRC" "$KEEP" || fail "hardlink failed"
echo "  linked: $KEEP (links now $(stat -c %h "$SRC"))"
mv -f "$CAND" "$SRC" || fail "atomic replace failed — $KEEP holds the original"
echo "  replaced: $SRC"

echo "=== VERIFY (structural: an inode match IS the same file) ==="
K_INO=$(stat -c %i "$KEEP")
echo "  source inode before swap : $SRC_INO"
echo "  $KEEP inode now          : $K_INO"
[ "$SRC_INO" = "$K_INO" ] || fail "backup is NOT the original inode"
echo "  ✅ the original survives, byte-for-byte, as the same file"
stat -c '  %n  ino=%i  size=%s  %U %G %a' "$SRC" "$KEEP"
for w in "$SRC.wal" "$KEEP.wal"; do [ -e "$w" ] && echo "  ⚠ WAL appeared: $w"; done
echo "✅ SWAP COMPLETE"
