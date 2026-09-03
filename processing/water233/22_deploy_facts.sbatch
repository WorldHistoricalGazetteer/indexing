#!/bin/bash
#SBATCH --job-name=water233-deployfacts
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/22-deployfacts-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/22-deployfacts-%j.err
#SBATCH --time=02:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G

# place#233 — facts the DEPLOYER needs, which I can measure and they cannot
# easily, plus the one open question that is now about the SHIPPED artifact.
#
# The proximity check (a) failed earlier on a with_locations() bug and I left
# it unanswered. It is worth closing now for a reason that did not apply then:
# the shipped build (c=0.001) carries 5 not_closed errors, so "did the 37
# absent ways cause any unclosed ring" is a question about the artifact being
# deployed rather than about a build we discarded.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
D="${RUN_ROOT}/data"
MB="${D}/water.mbtiles"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="

echo "=== 1. THE FILE ITSELF ==="
stat -c 'path=%n%nsize=%s bytes%nmode=%A (%a)%nowner=%U:%G%nmtime=%y%nlinks=%h' "${MB}"
echo "--- anything holding it open? ---"
if command -v lsof >/dev/null 2>&1; then lsof "${MB}" 2>/dev/null || echo "  lsof: no open handles"; else echo "  (lsof unavailable; the tiling job completed 33m38s ago and nothing else writes here)"; fi
echo "--- checksum, so the deployer can verify the transfer ---"
sha256sum "${MB}"

echo
echo "=== 2. WHAT THE TILESERVER WILL READ OUT OF IT ==="
python - "${MB}" <<'PY'
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
md = dict(con.execute("SELECT name, value FROM metadata").fetchall())
for k in sorted(md):
    v = md[k]
    if k == "json":
        vl = json.loads(v)
        print(f"  {k:12s} vector_layers: {[l['id'] for l in vl.get('vector_layers', [])]}")
        continue
    print(f"  {k:12s} {v[:110]}")
print()
n = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
print(f"  tile count: {n:,}   (expect 1,030,437 -- a truncated transfer still opens fine)")
row = con.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
GZIP_MAGIC = bytes([0x1f, 0x8b])
is_gz = row[0][:2] == GZIP_MAGIC
print("  first tile gzipped: " + str(is_gz)
      + "  (tileserver must serve Content-Encoding: gzip)")
if md.get("name", "").startswith("/"):
    print()
    print("  ⚠️ metadata 'name' is the BUILD PATH, because tippecanoe defaults it")
    print("     to the output filename and I did not pass --name. It surfaces in")
    print("     the served TileJSON. Harmless, but cosmetic and worth overriding")
    print("     at registration if the tileserver exposes a name field.")
PY

echo
echo "=== 3. THE OPEN QUESTION, NOW ABOUT THE SHIPPED BUILD ==="
echo "Do any of the shipped build's not_closed errors sit near the 37 absent ways?"
conda activate whg-coastline
rm -f "${D}/error_points-c001.geojson"
ogr2ogr -f GeoJSON "${D}/error_points-c001.geojson" "${D}/coastline-c001.db" \
    error_points -t_srs EPSG:4326 2>&1 | head -3 || true
conda activate whg
python - "${D}/coastline-relations.osm.pbf" "${D}/error_points-c001.geojson" <<'PY'
import json, math, os, sys
import osmium
rel_path, err_path = sys.argv[1], sys.argv[2]

# with_locations() needs NODE in the entity mask -- the bug that sank the
# first attempt was passing WAY alone.
tagged, coords = {}, {}
# with_locations() needs NODE in the mask, which means the stream yields
# Nodes AND Ways -- so filter by type rather than assuming every object is a
# way. (Fixing the mask without handling the mixed stream was the second
# bug in this same six lines.)
for w in osmium.FileProcessor(rel_path,
                              osmium.osm.NODE | osmium.osm.WAY).with_locations():
    if not isinstance(w, osmium.osm.Way):
        continue
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
located = {w: coords[w] for w in untagged if w in coords}
print(f"  untagged member ways: {len(untagged)}, with geometry: {len(located)}")

if not os.path.exists(err_path):
    print("  no error_points export; cannot compare"); raise SystemExit(0)
gj = json.load(open(err_path, encoding="utf-8"))
errs = [(f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][1],
         (f.get("properties") or {}).get("error"))
        for f in gj.get("features", [])
        if (f.get("geometry") or {}).get("type") == "Point"]
print(f"  error points in SHIPPED build: {len(errs)}")

def km(a, b):
    dlat = (b[1]-a[1])*111.32
    dlon = (b[0]-a[0])*111.32*math.cos(math.radians((a[1]+b[1])/2))
    return math.hypot(dlat, dlon)

worst = None
print()
print(f"  {'error':22s} {'lon':>11s} {'lat':>10s}  nearest of the 37")
for lon, lat, kind in errs:
    best, bw = None, None
    for wid, pts in located.items():
        d = min(km((lon, lat), p) for p in pts)
        if best is None or d < best:
            best, bw = d, wid
    if best is not None and (worst is None or best < worst[0]):
        worst = (best, bw)
    print(f"  {str(kind)[:22]:22s} {lon:11.5f} {lat:10.5f}  "
          f"{best:,.1f} km (w{bw})" if best is not None else "n/a")
print()
NEAR_KM = 5.0
if worst is None:
    print("  no comparison possible")
elif worst[0] > NEAR_KM:
    print(f"  CLOSED: nearest error is {worst[0]:,.1f} km from the nearest of the 37,")
    print(f"  against a {NEAR_KM:.0f} km scale for 'same place'. An absent way leaves a")
    print(f"  gap AT its own endpoint, so this rules out causation. The 37 absent")
    print(f"  ways caused none of the shipped build's coastline errors.")
else:
    print(f"  *** NOT CLOSED: an error sits {worst[0]:,.1f} km from way {worst[1]}, one of")
    print(f"  *** the 37. My mechanism explanation needs revisiting.")
PY
echo "=== DONE $(date -Is) ==="
