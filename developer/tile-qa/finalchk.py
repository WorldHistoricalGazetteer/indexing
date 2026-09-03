import glob, json, os, re, sqlite3, subprocess, sys
import os, sys
# Path was hardcoded to /vast/ishi/tiles-verify, whose .mbtiles were released
# on 3 Sep 2026. Takes a directory argument now; the original default is kept
# so a verbatim re-run is still possible. Original in the 3 Sep manifest.
TILEDIR = (sys.argv[1] if len(sys.argv) > 1
           else os.environ.get("WHG_TILE_QA_DIR", "/vast/ishi/tiles-verify"))


LOGS = "/vast/ishi/elastic/logs"
TILES = TILEDIR

# poly/point counts as the run itself reported them
poly = {}
for f in glob.glob(LOGS + "/tiles-ns-10756209_*.out") + glob.glob(LOGS + "/tl-*.out"):
    try:
        for line in open(f, errors="ignore"):
            m = re.match(r"\s+([a-z_]+) → [a-z_]+: [\d,]+ features \(poly=(\d+) point=(\d+)\)", line)
            if m:
                b, p, q = m.group(1), int(m.group(2)), int(m.group(3))
                cur = poly.get(b, (0, 0))
                poly[b] = (cur[0] + p, cur[1] + q)
    except Exception:
        pass

rows = []
for path in sorted(glob.glob(TILES + "/*.mbtiles")):
    b = os.path.basename(path)[:-8]
    if any(x in b for x in (".base", ".coverage", ".labels")):
        continue
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=20)
        md = dict(c.execute("SELECT name,value FROM metadata").fetchall())
        n = c.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        c.close()
        j = json.loads(md.get("json", "{}"))
        fields = set()
        for vl in j.get("vector_layers", []):
            fields |= set((vl.get("fields") or {}).keys())
        has_label = "label" in fields
    except Exception as e:
        n, has_label = -1, False
    p, q = poly.get(b, (None, None))
    mb = os.path.getsize(path) / 1048576
    rows.append((b, p, q, n, has_label, mb))

print("  %-10s %9s %11s %10s %7s %9s  %s" % ("bucket", "poly", "point", "tiles", "label", "MB", "verdict"))
bad = []
for b, p, q, n, lab, mb in rows:
    if p is None:
        verdict = "no log"
    elif p > 0 and not lab:
        verdict = "*** LABELS MISSING ***"; bad.append(b)
    elif p == 0 and lab:
        verdict = "*** unexpected labels ***"; bad.append(b)
    elif p > 0:
        verdict = "ok (labelled)"
    else:
        verdict = "ok (points only)"
    print("  %-10s %9s %11s %10s %7s %9.1f  %s" % (
        b, "{:,}".format(p) if p is not None else "-",
        "{:,}".format(q) if q is not None else "-",
        "{:,}".format(n), "YES" if lab else "no", mb, verdict))
print("\n  buckets: %d   mismatches: %d %s" % (len(rows), len(bad), bad or ""))
