import sqlite3, sys
def stats(path, label):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    rows = c.execute(
        "SELECT zoom_level, COUNT(*), MAX(LENGTH(tile_data)), "
        "       AVG(LENGTH(tile_data)) "
        "FROM tiles GROUP BY zoom_level ORDER BY zoom_level").fetchall()
    gmax = c.execute("SELECT MAX(LENGTH(tile_data)) FROM tiles").fetchone()[0]
    over = c.execute("SELECT COUNT(*) FROM tiles WHERE LENGTH(tile_data) > 500000").fetchone()[0]
    tot = c.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    c.close()
    print(f"--- {label} ---")
    print(f"  tiles={tot:,}  global max={gmax:,}  over 500KB={over:,}")
    for z, n, mx, avg in rows:
        if z <= 8:
            print(f"    z{z}: {n:>8,} tiles  max={mx:>9,}  avg={int(avg):>8,}")
for path, label in ((sys.argv[1], "NEW (with fix)"), (sys.argv[2], "OLD (published)")):
    try:
        stats(path, label)
    except Exception as e:
        print(f"--- {label}: {e}")
