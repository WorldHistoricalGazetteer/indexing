"""Keep the full-series spot output on /vast without letting it threaten production ES.

`/vast/ishi` is a 1 TB project quota SHARED with the production Elasticsearch cluster, and this project has
already driven it to flood-stage read-only once (see MEMORY `vast_capacity_and_crop_fragments`). The
35,514-region sweep writes ~30 G of `boxes_<tag>.jsonl` / `cover_<tag>.json` there over ~40 h. That fits
today (178 G free), but "fits today" is exactly the reasoning that filled it last time, and ES gets no say.

So: leave the output on /vast where the downstream globs expect it, and put a valve on the volume. When
usage crosses HIGH, sweep the OLDEST completed regions into gzipped tar batches on /ix1 until it is back
under LOW. Only whole finished regions move, verified before anything is deleted.

The resume contract is preserved by an *index*, not by the files: `spot_sheet.py` skips a region whose
`boxes_<tag>.jsonl` is non-empty, so removing that file would make a restarted shard re-spot 40 h of work.
Every swept tag is therefore appended to `swept.txt` in the archive, which `spot_sheet` also consults. The
index lives with the archive on /ix1 — durable, and it cannot be orphaned from the batches it describes.

    python vast_sweep.py --status                     # what a sweep would do right now; touches nothing
    python vast_sweep.py --sweep                      # one pass, only if above HIGH
    python vast_sweep.py --sweep --force              # one pass regardless of usage
    python vast_sweep.py --restore-to DIR             # materialise every swept region back out

`--restore-to` is how step 3 gets a complete corpus again: point it at `$SLURM_SCRATCH` on the compute node
that runs `join_train`, so the full set exists where it is read without ever going back onto /vast.
"""
import argparse, glob, json, os, shutil, subprocess, sys, tarfile, time

SPOT = os.environ.get("SPOT_OUT", "/vast/ishi/gb1900/edition/spot2")
ARCHIVE = os.environ.get("SPOT_ARCHIVE", "/ix1/ishi/gb1900/edition/spot2_archive")
SWEPT = f"{ARCHIVE}/swept.txt"

# Hysteresis, not a single trip point: sweeping to exactly the threshold would re-trigger on the next
# region written, and a tar of a few hundred megabytes every ten minutes is its own kind of I/O problem.
HIGH = float(os.environ.get("VAST_HIGH", "0.90"))   # start sweeping at 90% of the volume
LOW = float(os.environ.get("VAST_LOW", "0.86"))     # sweep until back under 86%
BATCH = int(os.environ.get("SWEEP_BATCH", "2000"))  # regions per tar; ~2 min to build, small enough to verify
# A region's boxes file is written once, at the end of do_region. A file still being written is therefore
# only ever the newest one — but `open(...,"w")` truncates on entry, so a half-written file is briefly
# non-empty and complete-looking. Age it out rather than race it.
MIN_AGE = int(os.environ.get("SWEEP_MIN_AGE", "600"))


QUOTA_ROOT = os.environ.get("VAST_QUOTA_ROOT", "/vast/ishi")


def usage():
    """Fraction of the /vast PROJECT QUOTA in use, from the filesystem rather than the quota tool.

    `df` is what goes read-only, and it is available on a compute node where `crc-quota` may not be.

    But it must be asked about the right path. VAST reports the quota of whatever directory you stat, and
    the bare mount is not the quota: `/vast` says 3.7 P total / 12% used (the whole array), while
    `/vast/ishi` and anything under it say 1.0 T / 83% (ours). Statting the mount makes this valve read
    11.9% forever and never fire — a monitor that cannot alarm, which is worse than no monitor. So walk up
    from SPOT only as far as a directory that exists, and refuse any answer that plainly is not the quota.
    """
    p = os.path.abspath(SPOT)
    while not os.path.isdir(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    st = os.statvfs(p)
    total = st.f_blocks * st.f_frsize
    if total > 100 * 2 ** 40:                         # escaped into the array-wide view
        st = os.statvfs(QUOTA_ROOT)
        total = st.f_blocks * st.f_frsize
        if total > 100 * 2 ** 40:
            raise RuntimeError(f"{p} and {QUOTA_ROOT} both report {total/2**40:.0f} T — that is the array, "
                               f"not the project quota; set VAST_QUOTA_ROOT")
    free = st.f_bavail * st.f_frsize
    return (total - free) / total, free, total


def load_swept():
    if not os.path.exists(SWEPT):
        return set()
    with open(SWEPT) as f:
        return {ln.strip() for ln in f if ln.strip()}


def candidates(swept):
    """Finished, settled regions, oldest first — oldest because they are the least likely to be re-read."""
    out = []
    now = time.time()
    for bf in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        tag = os.path.basename(bf)[len("boxes_"):-len(".jsonl")]
        if tag in swept:
            continue                                  # already archived; the file is a leftover, not work
        try:
            st = os.stat(bf)
        except OSError:
            continue
        if st.st_size == 0 or now - st.st_mtime < MIN_AGE:
            continue
        # Size must be everything the sweep will actually remove — boxes AND cover — or the target below
        # is measured against a smaller quantity than `freed` reports and the loop stops early, having
        # freed less than asked. That is a valve that opens briefly and closes while the volume is filling.
        size = st.st_size
        cv = f"{SPOT}/cover_{tag}.json"
        if os.path.exists(cv):
            size += os.path.getsize(cv)
        out.append((st.st_mtime, tag, bf, size))
    out.sort()
    return out


def sweep_once(force=False, dry=False):
    os.makedirs(ARCHIVE, exist_ok=True)
    frac, free, total = usage()
    if not force and frac < HIGH:
        print(f"/vast {frac:.1%} used ({free/2**30:.0f} G free) — below HIGH={HIGH:.0%}, nothing to do")
        return 0
    swept = load_swept()
    cands = candidates(swept)
    if not cands:
        print(f"/vast {frac:.1%} used but no settled unswept regions — the space is NOT this sweep's to free")
        return 0

    # How much has to go. Freeing to LOW is a volume-level target, but a sweep can only free what this
    # project wrote: if ES is what grew, say so rather than tarring the entire spot output to no effect.
    have = sum(c[3] for c in cands)
    want = have if force else max(0, int((frac - LOW) * total))
    if not force and have < want:
        print(f"WARNING: need {want/2**30:.1f} G to reach LOW={LOW:.0%} but only "
              f"{have/2**30:.1f} G of swept-able spot output exists — something ELSE is filling /vast")
    print(f"/vast {frac:.1%} used ({free/2**30:.0f} G free); sweeping toward {LOW:.0%} "
          f"= {want/2**30:.1f} G, from {len(cands)} candidate regions")

    freed = n = 0
    while cands and (force or freed < want):
        batch, cands = cands[:BATCH], cands[BATCH:]
        freed += archive_batch(batch, dry=dry)
        n += len(batch)
    frac2, free2, _ = usage()
    print(f"swept {n} regions, freed {freed/2**30:.1f} G; /vast now {frac2:.1%} "
          f"used ({free2/2**30:.0f} G free)")
    return n


def archive_batch(batch, dry=False):
    """Tar+gzip one batch to /ix1, VERIFY it, then delete the originals and index the tags.

    Order matters and is the whole point of the function: nothing is removed from /vast until the archive
    on /ix1 has been re-opened and found to contain every member. A sweeper that frees space by losing data
    is worse than a full volume, because a full volume announces itself.
    """
    # The name is derived from the oldest member's mtime, which is not guaranteed unique — and a collision
    # would overwrite an ALREADY VERIFIED archive whose tags are already in the index, destroying those
    # regions with no error anywhere. Cheap to make impossible; observed once in testing, where six files
    # shared an mtime and the second batch silently replaced the first.
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(batch[0][0]))
    tarp = f"{ARCHIVE}/spot2_{stamp}_{len(batch)}.tar.gz"
    dup = 1
    while os.path.exists(tarp):
        dup += 1
        tarp = f"{ARCHIVE}/spot2_{stamp}_{len(batch)}-{dup}.tar.gz"
    tags = [t for _, t, _, _ in batch]
    if dry:
        print(f"  would write {tarp} ({len(tags)} regions, "
              f"{sum(b[3] for b in batch)/2**30:.2f} G)")
        return sum(b[3] for b in batch)

    members = []
    tmp = tarp + ".tmp"
    with tarfile.open(tmp, "w:gz") as tf:
        for _, tag, bf, _ in batch:
            for p in (bf, f"{SPOT}/cover_{tag}.json"):
                if os.path.exists(p):
                    tf.add(p, arcname=os.path.basename(p))
                    members.append(os.path.basename(p))
    os.rename(tmp, tarp)

    with tarfile.open(tarp) as tf:
        got = set(tf.getnames())
    missing = [m for m in members if m not in got]
    if missing:
        print(f"  ABORT {os.path.basename(tarp)}: {len(missing)} members did not survive the write "
              f"(e.g. {missing[:3]}) — originals left in place")
        return 0

    freed = 0
    for _, tag, bf, size in batch:
        for p in (bf, f"{SPOT}/cover_{tag}.json"):
            try:
                freed += os.path.getsize(p)
                os.remove(p)
            except OSError:
                pass
    # Index LAST. If the process dies between the verified tar and this append, the worst case is a region
    # re-spotted — a minute of GPU. The reverse order risks a tag marked done whose data was never written.
    with open(SWEPT, "a") as f:
        for t in tags:
            f.write(t + "\n")
    print(f"  {os.path.basename(tarp)}: {len(tags)} regions, freed {freed/2**30:.2f} G")
    return freed


def restore_to(dest):
    """Materialise every swept region into `dest`, alongside whatever is still live on /vast."""
    os.makedirs(dest, exist_ok=True)
    tars = sorted(glob.glob(f"{ARCHIVE}/spot2_*.tar.gz"))
    n = 0
    for tarp in tars:
        with tarfile.open(tarp) as tf:
            tf.extractall(dest)
            n += len(tf.getnames())
        print(f"  {os.path.basename(tarp)} -> {dest}")
    live = 0
    for p in glob.glob(f"{SPOT}/boxes_*.jsonl") + glob.glob(f"{SPOT}/cover_*.json"):
        d = f"{dest}/{os.path.basename(p)}"
        if not os.path.exists(d):
            shutil.copy2(p, d)
            live += 1
    print(f"restored {n} archived files from {len(tars)} batches + {live} still-live files into {dest}")


def status():
    frac, free, total = usage()
    swept = load_swept()
    cands = candidates(swept)
    live = len(glob.glob(f"{SPOT}/boxes_*.jsonl"))
    arch = glob.glob(f"{ARCHIVE}/spot2_*.tar.gz")
    asz = sum(os.path.getsize(p) for p in arch)
    print(f"/vast          {frac:.1%} used, {free/2**30:.0f} G free  (HIGH {HIGH:.0%} / LOW {LOW:.0%})")
    print(f"live on /vast  {live} regions, {sum(c[3] for c in cands)/2**30:.2f} G settled + sweepable")
    print(f"archived /ix1  {len(swept)} regions in {len(arch)} batches, {asz/2**30:.2f} G compressed")
    print(f"would sweep    {'YES' if frac >= HIGH else 'no'} ({len(cands)} candidates)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--force", action="store_true", help="sweep even if below HIGH")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore-to", default=None)
    ap.add_argument("--watch", type=int, default=0, help="loop forever, checking every N seconds")
    a = ap.parse_args()
    if a.restore_to:
        restore_to(a.restore_to)
    elif a.watch:
        print(f"watching {SPOT} every {a.watch}s; HIGH={HIGH:.0%} LOW={LOW:.0%} -> {ARCHIVE}", flush=True)
        while True:
            try:
                sweep_once(force=a.force, dry=a.dry_run)
            except Exception as e:                    # a sweeper that dies is a valve that fails shut
                print(f"sweep error (continuing): {type(e).__name__}: {e}", flush=True)
            sys.stdout.flush()
            time.sleep(a.watch)
    elif a.sweep:
        sweep_once(force=a.force, dry=a.dry_run)
    else:
        status()
