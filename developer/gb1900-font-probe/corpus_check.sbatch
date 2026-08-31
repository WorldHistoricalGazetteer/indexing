#!/bin/bash
#SBATCH -M htc
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH -c 2
#SBATCH --mem=8G
#SBATCH -t 04:00:00
#SBATCH -J corpuschk
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/corpus_check.log
# Step 1 of the running order: verify then finalise the z17 corpus. Run ONLY after the fetch array is
# empty -- both modes open every block read-write and would contend with a shard still writing.
#
# A Slurm job rather than a login-node command: this opens 2,366 SQLite files on the /ix1 NFS mount, which
# is the small-file read pattern that pushed this project off /ix1 in the first place.
#
# --finalize checkpoints the WAL and drops each block to journal_mode=DELETE. spot_sheet already opens
# blocks read-WRITE so it can see an un-checkpointed WAL, but WAL still wants an mmap-able -shm segment,
# and 16 GPU shards sharing blocks over NFS is exactly where that goes wrong. DELETE mode removes the
# question entirely, and the corpus is read-only from here on.
# NO `set -u` around the activation: the env's activate-gcc_linux-64.sh dereferences SYS_SYSROOT unset and
# the job dies before it runs anything. Call the interpreter directly instead -- the activation buys nothing
# here, and it is what spot_all.sbatch already does.
set -eu
cd /vast/ishi/gb1900/probe/font
PY=/vast/ishi/envs/mapreader/bin/python
echo "=== verify ==="
$PY build_tile_corpus.py --verify
echo "=== finalize ==="
$PY build_tile_corpus.py --finalize
echo "CHECKDONE"
