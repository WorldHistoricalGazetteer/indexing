# The two artefacts that make §50 reproducible — and how to tell if they are gone

**Captured 8 September 2026, read-only, against the live CRC paths.**

§50 is the anchor the whole v8 case is measured against: v7 R@200 0.4815 against
lexical 0.4799, the crossover near k=200, the +0.1456 union gain, `both_nonlatin`
at n=5,237. **Reproducing any of it after v8 ships depends on two artefacts that
are currently protected by nothing but a directory convention.** This file is the
second witness: it does not protect them, it makes their loss *detectable*.

## 1. The v7 checkpoint

```
/vast/ishi/models/phonetic/checkpoints/v7/
  final_model.pt    sha256 f2493fd62a07afeac49553c6c5f482fb761f26998fa3ef222ffc85874f1038b6
  phase3_best.pt    sha256 f2493fd62a07afeac49553c6c5f482fb761f26998fa3ef222ffc85874f1038b6
```

`hf/` is a symlink farm into this directory (`hf/final_model.pt` →
`checkpoints/v7/final_model.pt`, `hf/vocab` → `data/v7/vocab`), so **a v8
checkpoint landing in `checkpoints/v8/` and a repointed symlink is reversible**,
and overwriting `v7/` is not. That property is load-bearing and was not written
down anywhere until now; a well-meaning tidy-up of `hf/` breaks it silently.

### 🛑 `phase3_best.pt` IS NOT A BACKUP — it is the same file

```
inode=11432750908411103777  links=2   final_model.pt
inode=11432750908411103777  links=2   phase3_best.pt
```

**One inode, two names**, which is why both hash identically and why `du` reports
60 M for two nominally 95 M files. ⚠ **`torch.save` opens `wb`, which truncates
in place — so writing to EITHER name destroys BOTH.** The redundancy is
apparent, not real, and anyone reasoning "there are two checkpoints, so there is
a spare" is wrong. Preserve by **copying to a new path**, never by relying on the
second name.

### 🛑 AND `final_model.pt` DENOTES DIFFERENT CONTENT ON THE TWO FILESYSTEMS

**The obvious recovery installs the wrong model and every filename still looks
right.** Verified independently, 8 Sep:

```
name              filesystem   sha256
final_model.pt    /vast        f2493fd62a07afea…   <- what production LOADS
phase3_best.pt    /vast        f2493fd62a07afea…   <- same inode as the above
final_model.pt    /ix1         24c82b67310cf877…   <- a DIFFERENT MODEL
phase3_best.pt    /ix1         f2493fd62a07afea…   <- the TRUE spare
```

✅ **A genuine independent spare exists** — `/ix1/…/v7/phase3_best.pt` is
production's content at `links=1` on a different filesystem, so the recovery path
is real and a protective copy into `/vast` is belt-and-braces rather than the only
defence. `/ix1` also holds the full epoch series (`phase2_epoch5…50`,
`phase3_epoch5…30`) that `/vast` does not.

🛑 **RESTORE FROM `/ix1/…/v7/phase3_best.pt`, AND VERIFY BY HASH, NEVER BY NAME.**
Copying `/ix1/…/v7/final_model.pt` — the obvious move, matching name to name —
silently installs `24c82b67…`, which is not the model production has been serving
or the one every measurement in this plan was taken against.

⚠ **`hf/` points at the shared inode, so the exposure is production and not an
archive.** `hf/final_model.pt` is a symlink to `/vast/…/checkpoints/v7/final_model.pt`;
`torch.save` truncates through both the symlink and the hardlink, so a write to
the *deployed* path destroys the weights the gateway is serving.

✅ **Checked and clear: `hf/vocab`.** It symlinks to `/vast/…/data/v7/vocab`,
which is a plain directory (`links=2` is the normal self-plus-parent count) whose
three files are all `links=1`. **No hardlink trap on the vocabulary.**

⚠ **The near-miss is part of the finding.** `indexing-04` first measured
`/ix1/…/checkpoints/v7` — three distinct inodes, all `links=1`, three different
hashes — and was one report away from reporting the hardlink finding as false.
**Same directory name, two filesystems, different structure**, and only one of
them is what production resolves to.

## 2. The evaluation corpus

```
/vast/ishi/symphonym-eval/20260905T2000Z/
  manifest.json      sha256 ad27c7660b198d772c72d651195bf2fd52f482b6bec647d60deba3e367e30ec4
  build.log          sha256 a865bb0461a07a8b42187b27790fbee9467996f52d52cf2b86d950860c543666
  haystack.jsonl     65,911,123 bytes   1,053,229 lines
  positives.jsonl    15,362,910 bytes      74,205 lines
  pairs.jsonl        28,641,182 bytes     148,410 lines
```

✅ **Immune to the re-extract**, which is the threat that prompted this check: the
harness reads these files and never touches the index, so rebuilding `toponyms`
cannot move the sample. ✅ **And nothing can dangle** — pairing is string-to-string
(`name_to_idx[partner]`), with no `toponym_id` anywhere, so the dangling rate is
zero *by construction* rather than by luck.

⚠ **The mirror of that property is a real limit:** it cannot be joined back to
the post-re-extract index, so "which toponym is this now" is unanswerable. Right
for a like-for-like v7↔v8 comparison; wrong for anything that must follow a row
into the new corpus.

⚠ `name_to_idx.setdefault` keeps the FIRST index for a repeated name, so
duplicate surface forms collapse to one slot. Consistent across runs and across
v7/v8, so it cannot bias a comparison — but **1,053,229 is SLOTS, not distinct
strings**, and must not be quoted as the latter.

## 3. What is NOT recoverable, and what is

🛑 **§50's per-pair ranks do not exist anywhere.** `recall_ceiling.py` writes
aggregates only; `rank_curve.py` writes per-query ranks but §50 came from
`recall_ceiling`. Per-pair is the level at which "the curves cross" lives, so the
structure behind the campaign's central finding is currently unmaterialised.

✅ **It is recoverable rather than perishable** — v7's outputs are a pure function
of (corpus, weights), and both survive — **conditional entirely on the two
artefacts above.** `indexing-8b` is adding a per-pair `ranks.jsonl` dump so the
next run materialises it; that cannot run until a login node returns.

**So the rule is: `checkpoints/v7/` and `symphonym-eval/20260905T2000Z/` must
survive until §50's per-pair ranks have been materialised. After that they are
merely valuable.**


## 4. The generalisation this file exists to serve

Two rules, both earned today rather than assumed:

* **Ask every irreversible step what measurement it makes impossible, before it
  runs.** The answer is usually capturable cheaply and read-only — often exactly
  when everything else is blocked.
* 🛑 **Ask every identifier whether it denotes ONE object.** Four instances in a
  day where it did not: two names for one inode (`final_model.pt` /
  `phase3_best.pt` on `/vast`); one name for two models (`final_model.pt` across
  `/vast` and `/ix1`); two computations for one population (`stratum_of` and
  `is_control`, §54.1); and one directory name for two different structures
  (`checkpoints/v7` on the two filesystems). **`indexing-04`'s formulation, and
  it is the sibling of the first rule rather than a separate lesson.**
