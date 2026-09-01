# Spec — atomic stage *directories* via symlink swap

> **Status:** specification only. Not implemented, and deliberately not
> implemented during the completion campaign. Written by S9, 1 September 2026,
> as the residual left open by §2.8.
>
> **Prerequisite:** §2.8 (`staged_parquet.atomic_staged_snapshot`) is landed
> and in use at all four writers. This proposal *replaces* the mechanism there;
> it does not sit alongside it.

---

## 1. The problem §2.8 could not solve

§2.8 made each staged merge publish its two output files by writing temps and
renaming them into place. That closed the real hazard — a zero-byte
`places.jsonl` outranking the complete upstream stage for the hours a merge
runs — and it is sufficient for every failure this campaign actually met.

It leaves one residual. A stage is **two files**, and two renames are two
syscalls:

```
final/places.parquet     ← hull-STRIPPED, null-stripped; what resolvers prefer
final/places.jsonl       ← canonical; keeps hull and explicit nulls
```

They are not interchangeable. `write_parquet_from_jsonl` strips `hull` before
conversion (`staged_parquet.py:116-121`), so hull-consumers —
`ccode_enrichment`, `generate_tiles` — read the **JSONL**, while the priority
chain prefers the **parquet**. A crash between the two renames therefore
leaves two live consumers disagreeing about the same stage, whichever order
is used. §2.8 chose parquet-first so the *authoritative* file is the correct
one in the state that persists, but that chooses which file is stale; it does
not make the pair consistent.

**Rename order cannot fix this.** Any scheme that renames two files has a
window between them. The fix has to make the *pair* the unit of publication.

## 2. Design

Make the stage directory a symlink, and swap the link rather than the files.

```
staged/<ns>/
    final -> final.v7          # the symlink every resolver actually reads
    final.v7/                  # complete, immutable once published
        places.jsonl
        places.parquet
    final.v6/                  # previous version, retained for rollback
```

Publication becomes:

1. write `final.v8/` complete — both files, no temps needed inside it, since
   the directory is invisible to resolvers until step 3;
2. `os.symlink("final.v8", "<ns>/final.tmp")`;
3. `os.replace("<ns>/final.tmp", "<ns>/final")`.

Step 3 is **one syscall** and replaces the symlink itself (`os.replace` does
not follow the final component). Readers see `final.v7` complete, then
`final.v8` complete, and never a mixed pair. `Path.exists()` and `open()`
follow symlinks, so every existing resolver keeps working unchanged.

Rollback becomes re-pointing the link, which is also atomic — materially
better than today, where reverting a bad stage means re-running it.

## 3. What has to change

| site | change |
|---|---|
| `staged_parquet.atomic_staged_snapshot` | publish a directory, not a file pair; keep the signature so call sites are untouched |
| the four writers | none — they already go through the helper (this is why §2.8's shared-helper shape matters) |
| `_STAGED_SOURCE_PRIORITY` × 5 (`index_from_stage:71`, `generate_tiles:145`, `aat_enrich:67`, `gazetteer_temporal_extent:54`, `hard_links_staged:42`) | none *functionally* — but see §5 |
| retention/GC | new: something must delete `*.vN` beyond the last two |
| `consolidate_geom_store`, operator scripts, `du`/`rsync`/`find` usage | audit — see §4 |

## 4. Risks, and why this is not a small change

1. **It changes an on-disk shape that five resolver copies and every operator
   script assume.** None of them *break* — symlinks are transparent to
   `open()` — but anything that walks, sizes, copies or archives
   `staged/<ns>/` sees different structure. `rsync` without `-L` copies the
   link, not the tree. `du` double-counts or under-counts depending on flags.
   This is the real cost, and it is why the change is not landable mid-campaign.

2. **Disk.** Retaining `vN-1` doubles a stage's footprint. On `/vast` — 1.0 TB
   shared with production ES, and this campaign has already had to rule on
   headroom against a 100 GB stop-line — that is a live constraint, not a
   theoretical one. Retention policy must be decided *before* implementation,
   not after.

3. **NFS.** `/ix1` is NFSv4 and hard-mounted, and client-side caching of
   symlink targets is not guaranteed to be immediate across hosts. The staged
   tree lives on `/vast`, so this is not blocking today, but a reader on
   another host may briefly resolve the old target. That is *safe* — the old
   target is a complete stage — but it means the swap is atomic per-host, not
   globally instantaneous, and nobody should claim otherwise.

4. **Partial adoption is worse than none.** If some stages are symlinks and
   others are directories, `consolidate_geom_store` and the retention job have
   to handle both. Migrate all five stage kinds or none.

## 5. The change I would *not* bundle with it

Hoisting the five duplicate `_STAGED_SOURCE_PRIORITY` definitions into one
module is tempting here, since a reader-side change is where you would notice
them. Keep it separate. It is already a queued row of its own, it touches
`h3_merge`/`ccode_merge`/`index_from_stage`, and bundling a refactor with an
on-disk format change makes a bisect useless when something goes wrong.

## 6. Verification

Per this campaign's standing rule, each check must be run first against a
known-bad input and confirmed to fail there.

1. **Pair consistency under a crash between publish steps.** Kill the process
   between writing `vN` and the `os.replace`; assert the resolver still gets
   `vN-1` and that its parquet and JSONL agree. Run against the §2.8
   implementation first, where it must FAIL — that is the whole point of the
   change, and if it passes there, the change is not needed.
2. **No mixed pair is ever observable.** A reader loop sampling
   `(parquet_mtime, jsonl_mtime)` across a publish must never see a pair from
   two different versions. This one genuinely needs concurrency, unlike §2.8's
   tests — an acceptable exception because the property *is* about
   simultaneity.
3. **Every resolver still resolves.** All five copies, against a symlinked
   stage, returning the same paths as before.
4. **Retention.** `vN-2` is removed and nothing holds an open handle to it.

## 7. Recommendation

Worth doing, after the campaign. The residual it closes is real but narrow: it
requires a crash in the sub-millisecond window between two renames, and the
state it leaves is two *complete* files of different generations rather than
anything truncated. §2.8 removed the hours-long window that actually bit us.

The reason to do it anyway is that the current guarantee is hard to state
correctly — "the stage is atomic" is what people will remember, and it is not
quite true. A guarantee that needs a footnote tends to lose the footnote.
