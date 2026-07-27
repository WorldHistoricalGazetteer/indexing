"""Fetch the whole z17 tile corpus once, into a few thousand SQLite blocks on /ix1.

WHY. Every stage of GB-STAMP re-fetches tiles on demand, and the fetch fails often enough to have cost real
work: 94 regions completed while starved of imagery and were then skipped forever because their output files
were non-empty, and a re-spot of one of them was killed by walltime after eight mosaics, three of which took
950s, 755s and 637s because 40, 32 and 27 tiles were missing. With the corpus held locally, `miss` is zero,
a mosaic costs 6-9s instead of minutes, and — the part that matters for a paper — a result stops depending
on whether a third-party CDN was healthy on the day it was computed.

WHY NOT STITCHED SHEETS. A sheet at z17 is about 13,800 x 9,200px, some 127 megapixels. A plain stitched
image has no windowed read, so pulling one 2048px mosaic out of it means decoding the whole thing; and the
spotting grid is not sheet-aligned, so a typical mosaic straddles two or four sheets and would need two to
four such decodes. That trades a file-count problem for a decode problem.

WHAT THIS DOES INSTEAD. Tiles are grouped into 64x64 blocks and each block is one SQLite file — about 2,366
files of ~183MB rather than 8,055,356 files of 42KB. The file count is the actual /ix1 NFS pathology (the
same small-file pattern that made Kibana take half an hour to start), and this cuts it ~3,400x while keeping
per-tile random access as an indexed lookup. The shard key is arithmetic on (tx, ty), so nothing needs a
geometry lookup, and blocks tile the plane exactly so no tile is stored twice.

Deliberately NOT the MBTiles schema: that spec stores rows TMS-flipped, and a silent y-flip between writer
and reader is exactly the bug that would be found months later in misplaced boxes. This is a private cache,
so it uses plain XYZ coordinates in an obviously-named table.

Absent tiles are recorded too. Sea and out-of-series tiles return 404 forever, and without a record of that
every future pass would retry all of them.

    python build_tile_corpus.py --shard 0 --of 16 --rps 4      # fetch
    python build_tile_corpus.py --verify                        # per-block completeness
"""
import argparse, io, json, math, os, sqlite3, sys, threading, time
import urllib.request
import concurrent.futures as cf

N17 = 2 ** 17
S3 = "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/17/{x}/{y}.png"
STORE = os.environ.get("TILE_STORE", "/ix1/ishi/gb1900/tilestore")
LOOSE = ["/vast/ishi/gb1900/tiles17", "/ix1/ishi/gb1900/tiles17"]
BLOCK = 64


def lonlat_px(lon, lat):
    x = (lon + 180.0) / 360.0 * N17 * 256
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * N17 * 256
    return x, y


def wanted_tiles(centres, r=8):
    """Every tile any spotting region will ask for, deduplicated (the regions overlap ~1.27x)."""
    out = set()
    for line in open(centres):
        p = line.split()
        if len(p) < 3:
            continue
        cx, cy = lonlat_px(float(p[0]), float(p[1]))
        ctx, cty = int(cx // 256), int(cy // 256)
        for tx in range(ctx - r, ctx + r + 1):
            for ty in range(cty - r, cty + r + 1):
                out.add((tx, ty))
    return out


def block_path(bx, by):
    return os.path.join(STORE, f"z17_{bx}_{by}.sqlite")


def open_block(bx, by, write=False):
    os.makedirs(STORE, exist_ok=True)
    con = sqlite3.connect(block_path(bx, by), timeout=60)
    if write:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("CREATE TABLE IF NOT EXISTS tile(tx INTEGER, ty INTEGER, data BLOB, "
                    "PRIMARY KEY(tx,ty)) WITHOUT ROWID")
        con.execute("CREATE TABLE IF NOT EXISTS absent(tx INTEGER, ty INTEGER, code INTEGER, "
                    "PRIMARY KEY(tx,ty)) WITHOUT ROWID")
        con.commit()
    return con


class Bucket:
    """Token bucket. The corpus is 8M objects from someone else's CDN, which already answers 503 SlowDown
    under our normal load, so the fetch is paced deliberately rather than run flat out."""

    def __init__(self, rps):
        self.rps = float(rps)
        self.t = time.monotonic()
        self.tokens = 0.0
        self.lock = threading.Lock()

    def take(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.rps, self.tokens + (now - self.t) * self.rps)
                self.t = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rps
            time.sleep(min(wait, 0.5))


def loose_tile(tx, ty):
    """Tiles already on disk from earlier on-demand fetching — ~427k of them. Free, so take them first."""
    for base in LOOSE:
        p = f"{base}/{tx}/{ty}.png"
        try:
            if os.path.getsize(p) > 500:
                with open(p, "rb") as fh:
                    return fh.read()
        except OSError:
            pass
    return None


def fetch(tx, ty, bucket, tries=4):
    bucket.take()
    for attempt in range(tries):
        try:
            req = urllib.request.Request(S3.format(x=tx, y=ty), headers={"User-Agent": "whg-gbstamp-corpus"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            return (data, None) if len(data) > 400 else (None, 204)   # tiny object = legitimately blank
        except Exception as e:
            code = getattr(e, "code", None)
            if code in (403, 404):
                return None, code                                     # out of series or sea: never retry
            if attempt == tries - 1:
                return None, None                                     # unknown failure: leave for a re-run
            time.sleep(1.5 * (attempt + 1))
            bucket.take()
    return None, None


def do_block(bx, by, tiles, rps, workers, log):
    con = open_block(bx, by, write=True)
    have = {t for (t,) in con.execute("SELECT tx*100000+ty FROM tile")}
    gone = {t for (t,) in con.execute("SELECT tx*100000+ty FROM absent")}
    todo = [(tx, ty) for tx, ty in tiles if (tx * 100000 + ty) not in have
            and (tx * 100000 + ty) not in gone]
    if not todo:
        con.close()
        return 0, 0, 0
    bucket = Bucket(rps)
    got = miss = seeded = 0
    pending = []

    def one(t):
        tx, ty = t
        d = loose_tile(tx, ty)
        if d is not None:
            return tx, ty, d, "loose"
        d, code = fetch(tx, ty, bucket)
        return tx, ty, d, code

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for tx, ty, data, how in ex.map(one, todo):
            if data is not None:
                pending.append(("t", tx, ty, data))
                got += 1
                seeded += (how == "loose")
            elif how in (403, 404, 204):
                pending.append(("a", tx, ty, int(how)))
                miss += 1
            if len(pending) >= 400:
                _flush(con, pending)
                pending = []
    _flush(con, pending)
    # Leave the block as a PLAIN file, not a WAL set. A reader opening mode=ro cannot see an
    # un-checkpointed WAL, so an interrupted block would look empty rather than partial — silently, which is
    # the worst way for a cache to fail.
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    print(f"  {log} block {bx}_{by}: {got} stored ({seeded} from local disk), {miss} absent, "
          f"{len(todo)-got-miss} unresolved", flush=True)
    return got, miss, len(todo) - got - miss


def _flush(con, pending):
    if not pending:
        return
    con.executemany("INSERT OR REPLACE INTO tile(tx,ty,data) VALUES (?,?,?)",
                    [(a, b, c) for k, a, b, c in pending if k == "t"])
    con.executemany("INSERT OR REPLACE INTO absent(tx,ty,code) VALUES (?,?,?)",
                    [(a, b, c) for k, a, b, c in pending if k == "a"])
    con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--centres", default="/vast/ishi/gb1900/probe/font/centres_all.txt")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--rps", type=float, default=4.0, help="requests/sec THIS task may make")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--ingest-loose", default=None,
                    help="absorb a loose tile tree (e.g. /vast) into the block store")
    ap.add_argument("--delete-after", action="store_true",
                    help="unlink each loose tile ONLY after it has been read back out of the store")
    ap.add_argument("--finalize", action="store_true",
                    help="checkpoint every block and drop it out of WAL mode, so read-only opens work")
    a = ap.parse_args()

    tiles = wanted_tiles(a.centres)
    blocks = {}
    for tx, ty in tiles:
        blocks.setdefault((tx // BLOCK, ty // BLOCK), []).append((tx, ty))
    keys = sorted(blocks)
    print(f"{len(tiles):,} tiles wanted, in {len(keys):,} blocks of {BLOCK}x{BLOCK}", flush=True)

    if a.ingest_loose:
        # Getting the loose tiles off /vast, which is a 1TB project quota shared with production ES and has
        # been driven to flood-stage read-only by this project before.
        #
        # Nothing is deleted on the strength of a successful INSERT. The row is read back out of the store,
        # through a fresh connection, and compared byte-for-byte with what is about to be unlinked; only
        # then does the source go. An insert that silently did not commit, or a block left mid-WAL, would
        # otherwise destroy the only copy.
        found = {}
        for dirpath, _dirs, files in os.walk(a.ingest_loose):
            try:
                tx = int(os.path.basename(dirpath))
            except ValueError:
                continue
            for fn in files:
                if not fn.endswith(".png"):
                    continue
                try:
                    ty = int(fn[:-4])
                except ValueError:
                    continue
                found.setdefault((tx // BLOCK, ty // BLOCK), []).append((tx, ty, os.path.join(dirpath, fn)))
        bl = sorted(found)
        mine = [k for i, k in enumerate(bl) if i % a.of == a.shard]
        print(f"shard {a.shard}/{a.of}: {sum(len(found[k]) for k in mine):,} loose tiles "
              f"in {len(mine):,} of {len(bl):,} blocks", flush=True)
        ing = ver = rm = skip = 0
        for bx, by in mine:
            con = open_block(bx, by, write=True)
            rows = []
            for tx, ty, path in found[(bx, by)]:
                try:
                    with open(path, "rb") as fh:
                        d = fh.read()
                except OSError:
                    continue
                if len(d) <= 500:
                    skip += 1
                    continue
                rows.append((tx, ty, d))
            if rows:
                con.executemany("INSERT OR REPLACE INTO tile(tx,ty,data) VALUES (?,?,?)", rows)
                con.commit()
                ing += len(rows)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("PRAGMA journal_mode=DELETE")
            con.close()
            # Verify through a NEW connection, so what is checked is what a later reader will actually see.
            chk = sqlite3.connect(block_path(bx, by), timeout=60)
            for tx, ty, d in rows:
                row = chk.execute("SELECT data FROM tile WHERE tx=? AND ty=?", (tx, ty)).fetchone()
                if row and row[0] == d:
                    ver += 1
                    if a.delete_after:
                        try:
                            os.unlink(dict(((x, y), p2) for x, y, p2 in found[(bx, by)])[(tx, ty)])
                            rm += 1
                        except OSError:
                            pass
            chk.close()
        print(f"INGESTDONE shard {a.shard}: {ing:,} inserted, {ver:,} verified by read-back, "
              f"{rm:,} removed from source, {skip:,} skipped as too small", flush=True)
        return

    if a.finalize:
        n = 0
        for bx, by in keys:
            pth = block_path(bx, by)
            if not os.path.exists(pth):
                continue
            con = sqlite3.connect(pth, timeout=120)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.execute("PRAGMA journal_mode=DELETE")
                n += 1
            except Exception as e:
                print(f"  {bx}_{by}: {e}")
            con.close()
        print(f"finalised {n} blocks\nFINALIZEDONE", flush=True)
        return

    if a.verify:
        done = short = 0
        missing_blocks = []
        for bx, by in keys:
            want = len(blocks[(bx, by)])
            if not os.path.exists(block_path(bx, by)):
                missing_blocks.append((bx, by, want, 0))
                continue
            con = open_block(bx, by)
            n = con.execute("SELECT count(*) FROM tile").fetchone()[0]
            m = con.execute("SELECT count(*) FROM absent").fetchone()[0]
            con.close()
            if n + m >= want:
                done += 1
            else:
                short += 1
                missing_blocks.append((bx, by, want, n + m))
        tot = sum(os.path.getsize(block_path(*k)) for k in keys if os.path.exists(block_path(*k)))
        print(f"complete blocks {done:,} / {len(keys):,}; short {short:,}; "
              f"never started {len(keys)-done-short:,}")
        print(f"store size {tot/1e9:.1f} GB")
        for bx, by, want, have in missing_blocks[:15]:
            print(f"  short: {bx}_{by} {have}/{want}")
        json.dump([[bx, by, want, have] for bx, by, want, have in missing_blocks],
                  open(os.path.join(STORE, "incomplete.json"), "w"))
        print("VERIFYDONE", flush=True)
        return

    mine = [k for i, k in enumerate(keys) if i % a.of == a.shard]   # one block has exactly one owner
    print(f"shard {a.shard}/{a.of}: {len(mine)} blocks, pacing {a.rps}/s", flush=True)
    G = M = U = 0
    for n, (bx, by) in enumerate(mine):
        g, m, u = do_block(bx, by, blocks[(bx, by)], a.rps, a.workers, f"[{a.shard}] {n+1}/{len(mine)}")
        G += g
        M += m
        U += u
    print(f"CORPUSDONE shard {a.shard}: {G} stored, {M} absent, {U} unresolved", flush=True)


if __name__ == "__main__":
    main()
