import json, sqlite3, glob, os
import os, sys
# Path was hardcoded to /vast/ishi/tiles-verify, whose .mbtiles were released
# on 3 Sep 2026. Takes a directory argument now; the original default is kept
# so a verbatim re-run is still possible. Original in the 3 Sep manifest.
TILEDIR = (sys.argv[1] if len(sys.argv) > 1
           else os.environ.get("WHG_TILE_QA_DIR", "/vast/ishi/tiles-verify"))

rows = []
for p in sorted(glob.glob(os.path.join(TILEDIR, "*.mbtiles"))):
    b = os.path.basename(p)[:-8]
    if any(x in b for x in (".base", ".coverage", ".labels")):
        continue
    try:
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=20)
        md = dict(c.execute("SELECT name,value FROM metadata").fetchall())
        n = c.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        c.close()
        j = json.loads(md.get("json", "{}"))
        fields = set()
        for vl in j.get("vector_layers", []):
            fields |= set((vl.get("fields") or {}).keys())
        rows.append((b, n, "label" in fields, os.path.getsize(p) / 1048576))
    except Exception as e:
        rows.append((b, -1, False, 0.0))
print("  %-12s %10s %12s %9s" % ("bucket", "tiles", "label field", "MB"))
for b, n, lab, mb in rows:
    print("  %-12s %10s %12s %8.1f" % (b, "{:,}".format(n), "YES" if lab else "no", mb))
print("\n  with label anchors: %d/%d" % (sum(1 for r in rows if r[2]), len(rows)))
