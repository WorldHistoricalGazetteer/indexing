# GB-STAMP — handoff, 28 July 2026

Read `MEMORY.md` first; the notes `gbstamp-label-assembly`, `gbstamp-starved-regions` and
`gbstamp-tile-corpus` cover most of what follows. This document is the running order.

---

## Scope (SG, 27 July)

**GB1900 / OS six-inch only.** Generalisable-method framing is dropped — accuracy on our own maps matters
more than generality across others. **BIGCAPS chasing is dropped**: polygon datasets serve that information
better. The three goals are:

1. label boxes, so users can see what has been transcribed;
2. fonts as hints to entity type;
3. checking GB1900 crowd transcriptions at scale, and spotting/transcribing additional labels.

Per the PESOSE technical brief (`~/PESOSE-technical-brief-2026-07-27.pdf`), **GB-STAMP is a capability
demonstration, not a funded deliverable** — it publishes before any award starts, so it belongs in "current
status" as evidence the team can do this work.

---

## Running now

**Corpus fetch** — `sbatch corpus.sbatch`, 16 htc tasks at 4 req/s (~64/s aggregate).
At 18h30m: 4,164,121 stored, 77,723 absent, **0 unresolved**, 1,393 of 2,366 blocks, 182 G.
Expect completion around 26–30h total. Resumable; a block already holding everything it wants is skipped.

Watch `unresolved`. It is the counter that silently absorbed a total network failure for three hours before
the certifi fix, because `CERTIFICATE_VERIFY_FAILED` is neither a 403 nor a 404.

```bash
ssh crc0 'cd /vast/ishi/gb1900/probe/font/cov && for k in stored absent unresolved; do
  cat corpus_*.log | grep -o "[0-9]* $k" | awk -v k=$k "{s+=\$1} END{print s\" \"k}"; done'
```

---

## The running order, once the corpus lands

### 1. Verify the corpus
```bash
python build_tile_corpus.py --verify     # per-block completeness; writes incomplete.json
python build_tile_corpus.py --finalize   # checkpoint any block left in WAL mode
```
`--finalize` matters: a `mode=ro` reader cannot see an un-checkpointed WAL, so an interrupted block reads as
**empty rather than partial** — silently. Re-run `corpus.sbatch` for anything `--verify` reports short.

### 2. Re-spot everything (~3 h)
```bash
sbatch respot_all.sbatch      # 8 GPU shards, 1,307 regions, writes to spot2/
```
Prepared and staged. **Cannot be chained** — Slurm silently ignores cross-cluster dependencies
(`sbatch -M gpu --dependency=afterany:<htc job>` reports success, shows `(null)`, and starts immediately).

Why re-spot all of them: one code version across the whole corpus, and every detection carries the model's
own baseline (`gline`), which we were discarding. It also subsumes the 94 starved regions, so they need no
special handling. Output goes to `spot2/` so the old pass survives for comparison — identical weights on
identical imagery should reproduce the boxes and merely *add* the baseline. **Check that before trusting the
new pass.**

### 3. Retrain and re-evaluate the join on `spot2/`
```bash
python join_train.py --boxes '/vast/ishi/gb1900/edition/spot2/boxes_gb_*.jsonl' --out join_rf5.joblib
python assemble_labels.py --boxes '…/spot2/boxes_gb_*.jsonl' --validate \
  --blocks-from join_rf5.test_blocks.json --model join_rf5.joblib --model-thr 0.5 \
  --max-lines 3 --centre-tol 0.25
```
With real `gline` the reconstruction fallback stops being exercised — worth re-running the ablation
(`GBSTAMP_NO_TANGENT=1`) to see whether the model's own baseline beats our reconstruction.

**Produce one coherent table**: nearest-word / hand-set-rules / learned, all on the same held-out split, and
update `gb-stamp/docs/label-assembly.md` and `docs/index.md`. The published figures (0.219 / 0.381 / 0.425)
are correct for the split they were measured on; the newer 0.442 / 0.453 are on a different one and were
deliberately **not** published, because mixing splits produces a table that looks better and means less.

### 4. Then the original question, still unanswered
```bash
python bench_sheets.py --plan
python bench_sheets.py --sheets 16 --dump-misses sheet_misses.jsonl
```
**What proportion of non-numeric labels do GB1900 and GB-STAMP each miss?** The first attempt was
contaminated by the starved regions and was not quoted. Re-run only after step 2.

---

## Where things stand

**Assembly** — the largest validated piece. Held out by 12-km blocks, exact reproduction of the volunteer's
transcription:

| | |
|---|---|
| nearest single word | 0.219 |
| hand-set rules | 0.381 |
| learned join + sequence constraint | 0.425 |
| …+ hard negatives, on a later split | 0.442 → **0.453** with end tangents |

Four attempted improvements: **hard negatives +0.009**, **end tangent +0.011**, face features **null**,
greedy topology **null**.

**Recognition is not the bottleneck** — 93% of labels are read with no unknown-character marker, and exact
match on that clean subset is only ~0.48. Grouping is the limit.

---

## Things that will bite you

**A label is a sequence, not a clique.** Union-find over pairwise decisions at 93% precision welded 31
components swallowing 9,819 of 16,243 words, one "label" 1,548 words long, and end-to-end collapsed to 0.083
— *below* the hand-set rules. Each word has at most one predecessor and one successor. Do not relax this.

**The pairwise metric does not predict system behaviour.** Three times now. The threshold maximising pairwise
F1 (0.36) gives the *worst* end-to-end result (0.282); 0.5–0.7 is the useful range. The end tangent improved
end-to-end while pairwise AUC went marginally *down*. Tune on the assembled outcome.

**Threshold, training distribution and topology are not independent.** The optimum moved 0.70 → 0.50 when the
training data changed. Re-tune after any change to either.

**Evaluate on held-out blocks, and score the baseline on the same ones.** `--blocks-from` is deliberately
separate from `--model` so the rule baseline can be scored on identical regions without the model silently
doing the joining. I twice nearly reported a gain that was a change of denominator.

**Disbelieve implausible measurements.** The first curvature run said median end-tangent deviation 33.9° —
i.e. most OS labels strongly curved. They are not. Two bugs: contour sides paired by *arc length* rather than
by position along the reading direction, and cap tips dragging the average at exactly the ends where the
tangent is taken. Corrected: median 2.16°, but the axis is still wrong by >12° for 12.4% of words.

**A non-empty `boxes_<tag>.jsonl` does not mean the region was properly spotted.** `spot_sheet` writes once
at the end, so non-empty means *completed* — but a region that completed while tile fetches were failing
writes a near-empty file indistinguishable from empty countryside, and the resume rule then skips it forever
*because* it is non-empty. Gate on `cover_<tag>.json` `miss_frac`. The first re-spot of `gb_4318_2824` still
reported 0.309.

**Never cache tiles on `/vast`.** 1 TB quota shared with production ES, driven flood-stage read-only by this
project before. Both accessors now default to `$SLURM_SCRATCH`; the durable layer is `/ix1`.

**The mapreader env has no CA bundle.** Use `certifi`. A plain `urlopen` works from the login node's system
Python and fails in the job, which is why a smoke test passed and three hours of the array fetched nothing.

---

## Open, not scheduled

- **Selection bias in the join's training set.** It only sees labels the recogniser read *exactly* as the
  volunteer did, biasing toward legible conventional lettering. The held-out score cannot reveal this,
  because the test set is filtered identically. Unusual faces — blackletter, ornate, outline — are likely
  under-represented, and those are where the typographic reading is meant to earn its place.
- **The ceiling is unknown.** Some GB1900 transcriptions are wrong, so exact reproduction can never reach
  1.0. Needs human adjudication of a disagreement sample — which *is* goal 3, not merely a measurement.
- **The face signal remains untested at the level that matters.** The coarse upright/italic/blackletter axis
  was null because it is redundant with letter case, which the model already had free. The 15-face inventory
  (9 with anchors, 951 labels) has not been tried, and case cannot proxy Upright-Solid-Serif versus
  Upright-Solid-Plain.
- **Six of fifteen inventory faces have no verified anchors.**

## Deferred to after the dataset is final

Engagement with Maps as Data and Pelagios over the annotation profile — see `gb-stamp/TODO.md`. The profile
is **W3C Web Annotation on IIIF canvases, not a new schema** (`gbstamp_records.py`, `gb-stamp/docs/data-model.md`).
`creator` vs `generator` decides what may be scored against GB1900: a detection prompted by a pin has both,
and is therefore circular.

---

## Key files

| file | role |
|---|---|
| `build_tile_corpus.py` | z17 corpus → per-block SQLite on `/ix1`; `--verify`, `--finalize`, `--ingest-loose` |
| `spot_sheet.py` | MapTextRunner over a region; reads the corpus, keeps `gline`, writes `cover_<tag>.json` |
| `assemble_labels.py` | words → labels; centre-line direction, sequence constraint, GB1900 validation |
| `join_train.py` | learns the pairwise join from GB1900; held out by 12-km blocks |
| `bench_sheets.py` | GB1900 ↔ GB-STAMP miss rates per sheet, inside the measurable footprint |
| `bench_spotter.py` | scores any spotter against GB1900; refuses pin-prompted output as circular |
| `gbstamp_records.py` | W3C Web Annotation emitter + validator |
| `respot_all.sbatch` | staged: re-spot all 1,307 regions from the corpus into `spot2/` |
