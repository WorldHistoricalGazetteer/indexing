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
