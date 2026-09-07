# The script vocabulary would bake the `OTHER` blackout into a v8 retrain

> **Determined 7 September 2026**, checked at the CONSUMER — what the training
> code reads, not what the detector exports.
> **Verdict: a retrain today WOULD reproduce the blackout, and regenerating the
> vocabulary is NOT a safe no-op.**

## 1. Where the vocabulary comes from

Both writers **derive it from the enum**, so it is not frozen in code:

```python
script_vocab = {s.value: i for i, s in enumerate(Script)}
```
`rebuild_vocab.py:194` and `rebuild_toponyms_index.py:1157`.

But **training reads a FILE, not the enum** — `phonetics/training/data_loading.py:64`
loads `vocab_dir/script_vocab.json` and takes `num_scripts` from its length.
`phonetics/inference/encoder.py:131` does the same at serving time. So "derived"
only helps if the file was regenerated *after* the split.

## 2. 🛑 Every vocabulary on disk is pre-split

The enum now has **37** members, all 11 post-split scripts present. Every
`script_vocab.json` that exists has **20** and **none** of them:

| file | entries | post-split |
|---|---:|---|
| `…/v7/vocab/script_vocab.json` | 20 | NONE |
| `…/vtemporal-20260731T160000Z/vocab/` | 20 | NONE |
| **`…/vundscript-20260906T160000Z/vocab/`** | **20** | **NONE** |
| `/ix1/…/v5`, `v6`, `v7`, `vpostbarrier-…` | 20 | NONE |
| `hf/vocab/script_vocab.json` (shipped) | 20 | NONE |

`hf/config.json` records `num_scripts: 20`.

⚠ **Including the one written yesterday.** The `vundscript` vocab was written
at 15:15:36 on 6 Sep, inside job 11170631's window (13:04 → 06:47), so STEP 2
did run and did derive from the enum — the enum simply had 20 members at that
moment. `aef25b7` (the split) reached the checkout afterwards. Nothing is
broken; the artefact is just older than the code.

**So a retrain pointed at any existing vocab dir inherits `num_scripts: 20` and
reproduces the blackout inside the trained model, where no rule fix reaches.**

## 3. 🛑 But regenerating is NOT purely additive — `OTHER` MOVES

Ids come from **enum declaration order**. The 17 new members were inserted
*before* `OTHER`, so:

```
ids PRESERVED : 19        (LATIN … KATAKANA, unchanged)
ids MOVED     : 1         OTHER  19 -> 36
ids ADDED     : 17        MYANMAR 19, GURMUKHI 20, … COPTIC 35
ids DROPPED   : 0
```

`OTHER` is not an ordinary member: `encode_script` falls back to it for every
unrecognised script (`tokenise.py:323`), so it is the single most-used id in the
table for exotic input. Moving it 19 → 36 means:

- a model trained with `num_scripts: 20` served a regenerated vocab looks up
  **index 36 in a 20-row embedding table**;
- any training artefact already encoded with script ids means something
  different when read against the new mapping.

**Regeneration and retraining must happen together, and no artefact encoded
under one numbering may be read under the other.**

## 4. The underlying design fault

`{s.value: i for i, s in enumerate(Script)}` **couples a model's embedding
indices to the textual order of an enum declaration.** Any insertion anywhere
above a member silently renumbers it, and nothing downstream can detect that —
the vocabulary file is self-consistent either way, and a model loading it gets
plausible ids for the wrong scripts.

**Recommendation:** stop deriving ids from declaration order. Either pin them
explicitly, or sort deterministically with `OTHER` fixed at a reserved index, so
adding a script is additive by construction. That is a small change now and an
unrecoverable one after a model ships against the new numbering.

⚠ Moving `OTHER` to the end of the enum would *also* make this instance
additive — but it fixes the instance, not the fault: the next insertion above
any member renumbers it again.

## 5. What to do before the PoC

1. **Regenerate `script_vocab.json` from the current enum** — cheap, it is
   derived.
2. **Regenerate any training data carrying encoded script ids in the same
   pass**, or confirm it stores script *names* rather than ids.
3. **Assert `num_scripts` at train time against the vocab actually loaded**, and
   fail rather than warn on mismatch.
4. Fix the ordering dependence (§4) so this cannot recur silently.

**Not done here** — items 1–3 change what a retrain trains on, which is SG's
call, and item 4 is a code change with cross-cutting effects.
