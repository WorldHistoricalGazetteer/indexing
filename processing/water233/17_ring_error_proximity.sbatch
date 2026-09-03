#!/bin/bash
#SBATCH --job-name=water233-ringprox
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/17-ringprox-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/17-ringprox-%j.err
#SBATCH --time=02:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G

# place#233 — close the ring prediction HONESTLY, by location not outcome.
#
# I predicted the 37 absent ways would cause zero additional ring errors,
# because untagged ways were never part of the coastline ring topology.
# osmcoastline reported 8 initially-unclosed nodes and 0 remaining after
# closing Antarctica and 3 rings within the close distance.
#
# ⚠️ Zero remaining is consistent with my claim, but ALSO with a weaker world
# in which some of those 8 WERE caused by the absent ways and happened to
# fall inside the 1-unit close distance. That is "rescued by tolerance",
# not "never disturbed" -- same outcome, different mechanism, and I asserted
# the mechanism. (indexing-5e)
#
# ⚠️ AND A SECOND, LARGER QUESTION FOUND WHILE CHECKING THIS ONE.
# osmcoastline's --close-distance is expressed IN THE UNITS OF THE OUTPUT
# SRS. The ocean job passed --srs=4326 and left --close-distance at its
# default of 1, and the log confirms "Closing if distance between nodes
# smaller than 1." In degrees that is ~111 km at the equator, not 1 metre --
# an enormously permissive tolerance that could join coastlines which are
# not actually connected. The default is sane for osmcoastline's DEFAULT srs
# (3857, metres); it is not sane for 4326.
#
# Whether it did any harm is a separate question from whether it could have,
# and only 3 rings were closed. So MEASURE THE ACTUAL CLOSURE DISTANCES
# rather than re-running an 80-minute job on a fear. If all three closures
# span metres, the permissive default was never exercised and the geometry
# is fine. If any spans kilometres, the ocean needs rebuilding with
# -c 0.0001 (~11 m).
#
# So: locate the 8, locate the 37, and measure the distance between them.
# If the errors are Antarctica plus scattered points nowhere near the 37,
# the prediction closes for the stated reason. If any sits near one of the
# 37, my explanation is wrong even though the result was fine.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
D="${RUN_ROOT}/data"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
[ -s "${D}/coastline.db" ] || { echo "FATAL: coastline.db missing" >&2; exit 1; }

# osmcoastline's DB is written with conda's GDAL, so read it with the same.
conda activate whg-coastline
echo "=== error tables in coastline.db ==="
ogrinfo -so "${D}/coastline.db" 2>&1 | head -20
echo
for lyr in error_points error_lines; do
    echo "--- ${lyr} ---"
    ogrinfo -so -al "${D}/coastline.db" "${lyr}" 2>&1 \
        | grep -E "Feature Count|Geometry:" || echo "  (layer absent)"
done

echo
echo "=== exporting error_points to GeoJSON ==="
rm -f "${D}/error_points.geojson"
ogr2ogr -f GeoJSON "${D}/error_points.geojson" "${D}/coastline.db" error_points \
    -t_srs EPSG:4326 2>&1 | head -5 || echo "(export reported issues)"
ls -la "${D}/error_points.geojson" 2>/dev/null || echo "no error_points exported"

# The 37 ways come from the relation extract, and pyosmium lives in whg.
conda activate whg
echo
echo "=== locating the untagged member ways, and measuring proximity ==="
set +e
python - "${D}/coastline-relations.osm.pbf" "${D}/error_points.geojson" <<'PY'
import json, math, os, sys, collections
import osmium

rel_path, err_path = sys.argv[1], sys.argv[2]

# Rebuild the 37: member ways of natural=coastline relations that are not
# themselves tagged natural=coastline.
tagged, coords = {}, {}
for w in osmium.FileProcessor(rel_path,
        osmium.osm.NODE | osmium.osm.WAY).with_locations():
    tagged[w.id] = (w.tags.get("natural") == "coastline")
    pts = []
    for n in w.nodes:
        try:
            pts.append((n.location.lon, n.location.lat))
        except Exception:
            pass
    if pts:
        coords[w.id] = pts

untagged = set()
for r in osmium.FileProcessor(rel_path, osmium.osm.RELATION):
    if r.tags.get("natural") != "coastline":
        continue
    for m in r.members:
        if m.type == "w" and tagged.get(m.ref) is False:
            untagged.add(m.ref)

located = {wid: coords[wid] for wid in untagged if wid in coords}
print(f"  untagged member ways        : {len(untagged):,}")
print(f"  of which we have geometry   : {len(located):,}")

if not os.path.exists(err_path):
    print("  no error_points.geojson -- cannot do the proximity check")
    sys.exit(0)

errs = []
with open(err_path, encoding="utf-8") as fh:
    gj = json.load(fh)
for f in gj.get("features", []):
    g = f.get("geometry") or {}
    if g.get("type") == "Point":
        errs.append((g["coordinates"][0], g["coordinates"][1],
                     f["properties"].get("error"),
                     f["properties"].get("osm_id")))
print(f"  error points in database    : {len(errs):,}")
by_kind = collections.Counter(e[2] for e in errs)
for k, v in by_kind.most_common():
    print(f"      {str(k):28s} {v:6,}")
print()

def km(a, b):
    (lon1, lat1), (lon2, lat2) = a, b
    dlat = (lat2 - lat1) * 111.32
    dlon = (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)

# The close distance in real units, so the numbers below can be read.
print("  CLOSE DISTANCE CONTEXT")
print("    --srs=4326 was used, so --close-distance 1 means 1 DEGREE")
print("    ~= 111 km at the equator, not 1 metre. Any ring closed by that")
print("    tolerance spanning more than a few metres is suspect.")
print()
print(f"  {'error':26s} {'lon':>11s} {'lat':>10s} {'nearest of the 37':>19s}")
worst = None
for lon, lat, kind, oid in errs[:40]:
    best, bw = None, None
    for wid, pts in located.items():
        d = min(km((lon, lat), p) for p in pts)
        if best is None or d < best:
            best, bw = d, wid
    if best is not None and (worst is None or best < worst[0]):
        worst = (best, bw, lon, lat, kind)
    near = f"{best:,.1f} km (w{bw})" if best is not None else "n/a"
    print(f"  {str(kind)[:26]:26s} {lon:11.5f} {lat:10.5f} {near:>19s}")

print()
# Two SEPARATE questions, deliberately not collapsed into one threshold.
#
# (a) CAUSATION. If an absent way created a gap, the resulting error point
#     sits essentially AT that way's endpoint -- metres to a couple of km.
#     This does not depend on the closure tolerance at all: the tolerance
#     governs whether a gap got closed, not where the error appears. So a
#     few km is the right scale here regardless of --close-distance.
# (b) TOLERANCE. Whether the ~111 km close distance was actually exercised
#     is a different question, answered by the closure distances, not by
#     these proximities.
NEAR_KM = 5.0
if worst is None:
    print("  No comparison possible.")
elif worst[0] > NEAR_KM:
    print(f"  (a) CAUSATION -- PREDICTION CLOSES. Nearest coastline error is")
    print(f"      {worst[0]:,.1f} km from the nearest of the 37 absent ways, against a")
    print(f"      {NEAR_KM:.0f} km scale for 'at the same place'. An absent way leaves a gap")
    print(f"      AT its own endpoint, so this separation rules out causation")
    print(f"      independently of the closure tolerance. The ring topology was")
    print(f"      never disturbed rather than rescued.")
else:
    print(f"  (a) *** CAUSATION NOT RULED OUT: an error point sits {worst[0]:,.1f} km from")
    print(f"      way {worst[1]}, one of the 37. Outcome was fine but my mechanism")
    print(f"      explanation needs revisiting.")
print()
print("  (b) TOLERANCE -- see closure distances below; the 111 km default is")
print("      the open question, independent of (a).")
PY
PROX_RC=$?
set -e
[ "${PROX_RC}" -eq 0 ] || echo "NOTE: proximity check (a) failed rc=${PROX_RC}; (b) still runs -- it decides whether the ocean needs rebuilding"

echo
echo "=== (b) WAS THE 111 km TOLERANCE ACTUALLY EXERCISED? ==="
# indexing-5e's bounding argument, stated as an expectation to CONFIRM rather
# than a finding: coastline rings are assembled by matching NODE IDS, not by
# distance. --close-distance is consulted only for ends still open after
# topological chaining fails. osmcoastline logged "Closed 3 rings", so the
# permissive tolerance had exactly three opportunities to do damage and every
# other join among 828,499 rings is exact by construction.
#
# ⚠️ THAT BOUNDING IS TESTABLE AND THIS TESTS IT. If the error tables show
# distance-based closures beyond those three, the bounding is WRONG and the
# exposed surface is wider than either of us thinks. Antarctica is excluded:
# its closure is synthetic, forced by the projection boundary rather than by
# a data error, so it is evidence about neither side.
conda activate whg-coastline
for lyr in error_points error_lines; do
    echo "--- exporting ${lyr} ---"
    rm -f "${D}/${lyr}.geojson"
    ogr2ogr -f GeoJSON "${D}/${lyr}.geojson" "${D}/coastline.db" "${lyr}" \
        -t_srs EPSG:4326 2>&1 | head -3 || echo "  (layer absent)"
done
conda activate whg
python - "${D}/error_points.geojson" "${D}/error_lines.geojson" <<'PY2'
import collections, json, os, sys
for path in sys.argv[1:]:
    name = os.path.basename(path)
    if not os.path.exists(path):
        print(f"  {name}: absent")
        continue
    with open(path, encoding="utf-8") as fh:
        gj = json.load(fh)
    feats = gj.get("features", [])
    kinds = collections.Counter(
        (f.get("properties") or {}).get("error") for f in feats)
    print(f"  {name}: {len(feats):,} features")
    for k, v in kinds.most_common():
        print(f"      {str(k):30s} {v:8,}")
print()
PY2

echo
echo "=== (b) MEASURE THE THREE CLOSURES DIRECTLY ==="
# osmcoastline records each gap it closed as an error_line of type
# 'added_line'. The LENGTH of that line IS the closure distance. So this is
# not an inference from the tolerance -- it is the distance actually spanned.
#
# 3 added_line features against 828,496 rings also TESTS indexing-5e's
# bounding argument: rings chain by node id, and --close-distance is
# consulted only where chaining fails. If added_line == 3, the permissive
# 111 km tolerance had exactly three opportunities and the bounding holds.
python - "${D}/error_lines.geojson" <<'PY3'
import json, math, os, sys
path = sys.argv[1]
if not os.path.exists(path):
    print("  error_lines.geojson absent -- cannot measure closures")
    raise SystemExit(0)
with open(path, encoding="utf-8") as fh:
    gj = json.load(fh)

def seg_km(a, b):
    dlat = (b[1] - a[1]) * 111.32
    dlon = (b[0] - a[0]) * 111.32 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot(dlat, dlon)

added = [f for f in gj.get("features", [])
         if (f.get("properties") or {}).get("error") == "added_line"]

# THE DELTA OF 3: log reported 828,499 rings, the table stores 828,496.
# indexing-5e's hypothesis: each added_line that joins ends of two DIFFERENT
# open fragments merges them into one ring, dropping the count by 1; three
# such closures give exactly 3.
#
# ⚠️ The obvious test does NOT work. The rings table holds POST-closure
# geometry, so after a merge both endpoints lie on the same stored ring
# whichever way it happened -- a spatial lookup cannot distinguish the two
# cases. So do not run that test and report a result from it. Instead dump
# whatever these features actually carry, and decide afterwards whether the
# question is answerable from the data at all rather than designing a test
# around an assumed schema.
print("  --- full properties of each added_line (schema unknown, so print it) ---")
for f in added:
    print(f"    {json.dumps(f.get('properties'), sort_keys=True)}")
print()
print(f"  added_line features (= gaps osmcoastline closed): {len(added)}")
print()
worst = 0.0
for f in added:
    g = f.get("geometry") or {}
    cs = g.get("coordinates") or []
    if g.get("type") == "MultiLineString":
        cs = [pt for part in cs for pt in part]
    total = sum(seg_km(cs[i], cs[i + 1]) for i in range(len(cs) - 1))
    worst = max(worst, total)
    lon, lat = (cs[0][0], cs[0][1]) if cs else (0, 0)
    print(f"    closure length {total*1000:10.1f} m   at {lon:10.5f},{lat:9.5f}")
print()
print(f"  longest closure: {worst*1000:,.1f} m ({worst:.4f} km)")
print(f"  tolerance in force: 1 degree ~= 111 km")
if worst < 0.1:
    print("  VERDICT: every closure is sub-100 m. The 111 km tolerance was")
    print("  present but NEVER EXERCISED at anything like its magnitude, so the")
    print("  geometry is as sound as if -c 0.0001 had been passed. Harmless")
    print("  BY LUCK, not by being right -- pass -c explicitly next time.")
elif worst < 5:
    print(f"  VERDICT: longest closure {worst:.3f} km. Small in absolute terms but")
    print( "  far above a coastline node spacing; inspect before trusting.")
else:
    print(f"  *** VERDICT: a closure spans {worst:.1f} km. The permissive tolerance")
    print( "  *** WAS exercised and coastlines may be joined that are not")
    print( "  *** connected. Rebuild with -c 0.0001 before tiling.")
if len(added) != 3:
    print()
    print(f"  ⚠️ added_line count is {len(added)}, not the 3 the log reported.")
    print( "  The node-id bounding argument does not hold as stated and the")
    print( "  exposed surface is wider than expected.")
PY3

echo
echo "=== DONE $(date -Is) ==="
