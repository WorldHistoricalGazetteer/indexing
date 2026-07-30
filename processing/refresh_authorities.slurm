#!/bin/bash
#SBATCH --job-name=refresh_authorities
#SBATCH --output=/ix1/ishi/logs/%x_%j.log
#SBATCH --error=/ix1/ishi/logs/%x_%j.err
#SBATCH --time=3-00:00:00
#SBATCH --qos=htc-htc-n
#SBATCH --mem=8G
#SBATCH --cpus-per-task=2
#
# Refresh authority source dumps.
#
# Parameterised via environment variables at submit time — do NOT edit this
# file to change what is fetched:
#
#   NS   comma-separated namespaces (default: all)
#   AGE  refresh files older than this many days; 0 = force (default: 365)
#
# Export them in the submitting shell (NOT via `--export=ALL,NS=a,b` — sbatch
# splits that on commas and would swallow the namespace list):
#
#   NS=wd AGE=0 sbatch -M htc --account=ishi -J fetch_wd \
#     --export=ALL processing/refresh_authorities.slurm
#
#   NS=gn,pl,ohm,tgn AGE=0 sbatch -M htc --account=ishi -J fetch_small \
#     --time=12:00:00 --qos=htc-htc-s --export=ALL processing/refresh_authorities.slurm
#
# NOTE: downloads land on a `.part` file and are only moved over the existing
# dump on success (`temp.replace(dest)`), so the old dump stays readable until
# the new one is complete and is then replaced in place — no manual deletion,
# but plan for peak disk = old + new for the namespace being fetched.

set -o pipefail

NS="${NS:-}"
AGE="${AGE:-365}"

DATA_ROOT=/ix1/ishi/data/authorities

echo "=== refresh_authorities: NS='${NS:-ALL}' AGE=${AGE} ==="
echo "Host: $(hostname)  Job: ${SLURM_JOB_ID:-none}  Started: $(date -Is)"

echo "--- disk before ---"
df -h "${DATA_ROOT}"
if [ -n "$NS" ]; then
    for n in ${NS//,/ }; do
        du -sh "${DATA_ROOT}/${n}" 2>/dev/null || true
    done
fi

# Activate conda environment
CONDA_SETUP="/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh"
if [ ! -f "$CONDA_SETUP" ]; then
    CONDA_SETUP="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
fi
source "$CONDA_SETUP"
conda activate whg

cd "${SLURM_SUBMIT_DIR:-/vast/ishi/elastic}" || exit 1

ARGS=(--age "$AGE")
[ -n "$NS" ] && ARGS+=(-n "$NS")

python -u -m processing.fetch_authorities "${ARGS[@]}"
RC=$?

echo "--- disk after (exit ${RC}) ---"
df -h "${DATA_ROOT}"
if [ -n "$NS" ]; then
    for n in ${NS//,/ }; do
        ls -la --time-style=long-iso "${DATA_ROOT}/${n}" 2>/dev/null || true
    done
fi
# Surface any partial downloads left behind — these are resumable, not junk,
# but they mean the fetch did not complete.
find "${DATA_ROOT}" -name '*.part' -printf '%s\t%p\n' 2>/dev/null \
    | sed 's/^/LEFTOVER PART: /'

echo "Finished: $(date -Is)  exit=${RC}"
exit $RC
