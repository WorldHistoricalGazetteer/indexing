#!/bin/bash
#SBATCH --job-name=water233-pin
#SBATCH --output=/vast/ishi/elastic/water233-20260902T220703Z/logs/00-pin-%j.out
#SBATCH --error=/vast/ishi/elastic/water233-20260902T220703Z/logs/00-pin-%j.err
#SBATCH --time=00:30:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G

# place#233 Step 0a + 0b: verify tooling, then PIN the planet PBF.
#
# Pinning is a HARDLINK, not a copy. Both download paths in
# processing/fetch_authorities.py write "<dest>.part" and then call
# Path.replace(dest) -- an atomic RENAME, not an in-place rewrite. So a
# hardlink holds the exact inode: a refresh swaps the directory entry and
# our link still resolves to the bytes the admin boundaries came from.
# Cost: zero bytes. A copy would be 87.5 GB of the 226 GB free on /vast,
# which is shared with production ES and has gone read-only before.

set -eo pipefail   # NOT -u: breaks conda activate on this cluster

source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH}"

echo "=== node: $(hostname)  job: ${SLURM_JOB_ID}  date: $(date -Is) ==="

PBF=/ix1/ishi/data/authorities/osm/planet-latest.osm.pbf
PIN_DIR=/ix1/ishi/data/authorities/osm/.pin-place233
PIN="${PIN_DIR}/planet-pinned.osm.pbf"
WORK=/vast/ishi/elastic/water233-20260902T220703Z
mkdir -p "${WORK}/logs" "${WORK}/data"

echo
echo "=== [0a] tooling ==="
echo "--- osmium CLI ---"
osmium --version 2>&1 | head -3
echo "--- pyosmium ---"
python - <<'PY' || echo "WARN: pyosmium probe failed (non-fatal)"
import osmium
def ver():
    try:
        import osmium.version as v
        return getattr(v, "pyosmium_release", None) or getattr(v, "version", None)
    except Exception:
        return getattr(osmium, "__version__", "unknown")
print("pyosmium", ver())
print("libosmium", getattr(__import__("osmium.version", fromlist=["x"]),
                           "libosmium_version", "?"))
import osmium.area
print("area.AreaManager:                 ", hasattr(osmium.area, "AreaManager"))
print("area.MultipolygonManager (exp F): ", hasattr(osmium.area, "MultipolygonManager"))
print("FileProcessor:                    ", hasattr(osmium, "FileProcessor"))
try:
    import osmium.filter as f
    names = [n for n in dir(f) if not n.startswith("_")]
    print("osmium.filter exports:", names)
except Exception as e:
    print("osmium.filter unavailable:", e)
PY
echo "--- tippecanoe / tile-join / ogr2ogr ---"
tippecanoe --version 2>&1 | head -1 || echo "tippecanoe MISSING"
tile-join --version 2>&1 | head -1 || echo "tile-join MISSING"
ogr2ogr --version 2>&1 | head -1 || echo "ogr2ogr MISSING"
echo "--- osmcoastline (expected ABSENT) ---"
command -v osmcoastline || echo "osmcoastline: not on PATH (expected)"

echo
echo "=== [0b] source identity BEFORE pin ==="
stat -c 'path=%n%nsize=%s%nmtime=%y%ninode=%i%nlinks=%h' "${PBF}"

echo
echo "=== [0b] creating hardlink pin ==="
mkdir -p "${PIN_DIR}"
if [ -e "${PIN}" ]; then
    echo "pin already exists -- verifying it is the SAME inode"
else
    ln "${PBF}" "${PIN}"
    echo "hardlink created: ${PIN}"
fi

SRC_INO=$(stat -c %i "${PBF}")
PIN_INO=$(stat -c %i "${PIN}")
SRC_SIZE=$(stat -c %s "${PBF}")
PIN_SIZE=$(stat -c %s "${PIN}")
echo "source inode=${SRC_INO} size=${SRC_SIZE}"
echo "pin    inode=${PIN_INO} size=${PIN_SIZE}"
if [ "${SRC_INO}" != "${PIN_INO}" ] || [ "${SRC_SIZE}" != "${PIN_SIZE}" ]; then
    echo "FATAL: pin does not share the source inode -- refusing to continue" >&2
    exit 1
fi
echo "PIN OK: same inode, same size"

echo
echo "=== [0b] content fingerprint (tripwire) ==="
# Deliberately NOT a full sha256: that is a second 87.5 GB read of the /ix1
# NFS volume for a weaker guarantee than the hardlink already gives. The
# hardlink makes byte-identity STRUCTURAL (same inode) rather than
# observational (same hash at one moment). Head+tail+size is a cheap
# tripwire that would catch a truncation or a wholesale replacement.
HEAD_SHA=$(head -c 16777216 "${PIN}" | sha256sum | cut -d' ' -f1)
TAIL_SHA=$(tail -c 16777216 "${PIN}" | sha256sum | cut -d' ' -f1)
echo "head16MiB_sha256=${HEAD_SHA}"
echo "tail16MiB_sha256=${TAIL_SHA}"

echo
echo "=== [0b] osmium fileinfo on the PIN ==="
osmium fileinfo "${PIN}" 2>&1 | head -25

cat > "${WORK}/pin.json" <<JSON
{
  "issue": "place#233",
  "pinned_at": "$(date -Is)",
  "job_id": "${SLURM_JOB_ID}",
  "source_path": "${PBF}",
  "pin_path": "${PIN}",
  "inode": ${SRC_INO},
  "size_bytes": ${SRC_SIZE},
  "mtime": "$(stat -c %y "${PBF}")",
  "head_16mib_sha256": "${HEAD_SHA}",
  "tail_16mib_sha256": "${TAIL_SHA}",
  "pin_method": "hardlink",
  "pin_rationale": "fetch_authorities writes <dest>.part then Path.replace(dest) = atomic rename, so a hardlink retains the original inode across a refresh"
}
JSON
echo
echo "=== wrote ${WORK}/pin.json ==="
cat "${WORK}/pin.json"
echo
echo "=== DONE $(date -Is) ==="
