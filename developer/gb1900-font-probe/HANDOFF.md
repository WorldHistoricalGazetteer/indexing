# GB-STAMP — handoff, 28 July 2026

Read `MEMORY.md` first; the notes `gbstamp-label-assembly`, `gbstamp-starved-regions` and
`gbstamp-tile-corpus` cover most of what follows. **Start with "START HERE" immediately below**, then read
the scope and the traps before running anything.

---

## START HERE — what the incoming session should do

**1. Wait for the corpus fetch to finish.** Do not poll in a loop in the foreground; start a background
wait and get on with reading the rest of this file:

```bash
until ssh crc0 'test -z "$(squeue -M htc -u stg135 -h -o %j | grep corpus)"'; do sleep 300; done
```

**2. Then work through "The running order" below, in order.**

- **Step 1 (verify + finalise the corpus)** — no decisions needed, just run it. If `--verify` reports short
  blocks, re-run `corpus.sbatch`; it is resumable and will pick up only what is missing.
- **Step 2 (spot all 35,514 regions, ~40 h)** — launch it, then **stop and report**. It cannot be chained to
  the corpus job, so it needs a manual `sbatch`. When it finishes, the first thing to check is whether the
  new pass reproduces the old one: identical weights on identical imagery should give the same boxes and
  merely ADD `gline`. If it does not, say so before anything downstream is built on it.
- **Steps 3–4** — depend on what step 2's comparison shows. Do not run them blind.

Report progress to SG at each of those points rather than running the whole chain silently: step 2 alone is
nearly two days of GPU time, and a wrong turn at step 1 would waste it.

### Progress check (correct version)

```bash
ssh crc0 'cd /vast/ishi/gb1900/probe/font/cov &&
  echo "shards done: $(grep -l CORPUSDONE corpus_*.log 2>/dev/null | wc -l)/16" &&
  for k in stored absent unresolved; do
    grep -h "block " corpus_*.log | grep -o "[0-9]* $k" |
      awk -v k="$k" "{s+=\$1} END{print \"  \"s\" \"k}"
  done &&
  echo "  blocks: $(grep -h "block " corpus_*.log | wc -l)/2366"'
```

**The `grep -h "block "` filter is load-bearing.** A shard that finishes writes a `CORPUSDONE shard N: X
stored, Y absent, Z unresolved` summary line carrying its own totals, so a naive `grep -o "[0-9]* stored"`
sums those ON TOP of the per-block lines and inflates the count. That produced a reading of 11.2M stored
against a target of 8.06M — impossible, since blocks tile the plane exactly and no tile is stored twice.
If the total ever exceeds 8,055,356, this is why.

**Completion is `shards done: 16/16`**, or an empty `squeue`. Blocks reaching 2,366 is not sufficient — the
last block of a shard still has to be written.

**Watch `unresolved`.** It should stay at 0. It is the counter that silently absorbed a total network
failure for three hours before the certifi fix, because `CERTIFICATE_VERIFY_FAILED` is neither a 403 nor a
404 and so fell into the "leave it for a later run" bucket. A rising `unresolved` means something is wrong
with the fetch, not that it is merely slow.

### State at handover (28 July, 32h42m elapsed)

7,245,769 stored · 104,278 absent · **0 unresolved** · 2,297/2,366 blocks · 9/16 shards done · 292 G.
Roughly 91% resolved; 2–3 hours remaining, set by whichever of the 7 running shards drew the densest blocks.
Resumable throughout — a block already holding everything it wants is skipped, so a kill costs only the
block in flight.

---

## Scope (SG, 27 July)

**GB1900 / OS six-inch only.** Generalisable-method framing is dropped — accuracy on our own maps matters
more than generality across others. The three goals are:

1. label boxes, so users can see what has been transcribed;
2. fonts as hints to entity type;
3. checking GB1900 crowd transcriptions at scale, and spotting/transcribing additional labels.

### Why BIGCAPS was dropped

The letter-spaced administrative capitals were pursued hard and are now abandoned, for two independent
reasons — **either would be sufficient, and together they close it**:

1. **Every method tried proved unreliable.** MapReader's word spotter never fires on them at all (the
   letter-spacing is why). Hi-SAM boxes them but emits no text. The connected-component route — each capital
   is its own component on a cleaned sheet, grouped by strict collinearity — worked on rural sheets and
   stayed swamped on dense urban ones (43.9% hatch against 3.1% rural), where a row of hatched buildings is
   collinear, equal-height and regularly spaced and so passes every arrangement test. Overlay clustering of
   the extracted components *did* work (162 pure letterform clusters), which is why this took a while to
   concede: the pieces each worked somewhere, and none worked everywhere.
2. **The labels add nothing we do not already hold.** They name administrative units — counties,
   registration and local-government districts, parishes — and the **`vob_*` gazetteers**
   (`vob_rd`, `vob_rc`, `vob_cty`, `vob_lgd`) plus `kain_par` already cover those as *polygons*, with
   boundaries, multi-snapshot dates and provenance. A recovered point-with-a-name is strictly worse evidence
   than a dated polygon for the same unit.

So the effort was buying a degraded version of data already built and validated. Do not restart it because
the component route "nearly works" on a rural sheet — that was known, and it is not the reason it stopped.

Per the PESOSE technical brief (`~/PESOSE-technical-brief-2026-07-27.pdf`), **GB-STAMP is a capability
demonstration, not a funded deliverable** — it publishes before any award starts, so it belongs in "current
status" as evidence the team can do this work.

---

---

## The running order, once the corpus lands

### 1. Verify the corpus
```bash
python build_tile_corpus.py --verify     # per-block completeness; writes incomplete.json
python build_tile_corpus.py --finalize   # checkpoint any block left in WAL mode
```
`--finalize` matters: a `mode=ro` reader cannot see an un-checkpointed WAL, so an interrupted block reads as
**empty rather than partial** — silently. Re-run `corpus.sbatch` for anything `--verify` reports short.

### 2. Spot the WHOLE series — all 35,514 regions (~40 h on 16 GPUs)
```bash
sbatch spot_all.sbatch        # 16 shards, centres_all.txt, writes to spot2/
```
**Not a re-spot of the 1,307 already done — those are 3.7% of Great Britain.** The sample was a sample only
because on-demand tile fetching made a region cost minutes to an hour. From the local corpus a mosaic is ~7s,
so a region is ~65s and the full sweep is 641 GPU-hours: **40 h across 16 shards**, inside the 6-day a100
QOS. The corpus was sized for exactly this — its 8,055,356 tiles cover all 35,514 centres, not just the
spotted ones.

`spot_sheet.py` now takes `--centres/--shard/--of` and **loads the model once per shard**. Loading
MapTextRunner per region cost 20–40s, which was noise when tiles took minutes and would have been a large
share of the total now that they do not.

Writes to `spot2/`, so the existing pass survives for comparison — identical weights on identical imagery
should reproduce the boxes and merely *add* the model's own baseline (`gline`). **Check that before trusting
the new pass.** The 94 starved regions are subsumed and need no special handling.

**Cannot be chained** to the corpus job: Slurm silently ignores cross-cluster dependencies
(`sbatch -M gpu --dependency=afterany:<htc job>` reports success, shows `(null)`, and starts immediately).

**Launch the /vast valve alongside it** (`sbatch vast_watch.sbatch`). `/vast/ishi` is a 1 TB quota shared
with production ES and stood at **83%** when the sweep was launched; `spot2/` adds ~30 G. The watcher checks
every 10 min and, above 90%, tars the oldest *finished* regions to `/ix1/ishi/gb1900/edition/spot2_archive`
until back under 86% — write → verify → delete → index, in that order, so nothing leaves `/vast` until it
has been read back off `/ix1`. Swept tags are appended to `swept.txt`, which `spot_sheet.py` now reads at
startup: without it a restarted shard would re-spot everything the sweeper had archived, because the resume
rule keys on the boxes file being present.

Consequence for step 3: if anything was swept, `…/spot2/boxes_gb_*.jsonl` is no longer the whole series.
Run `python vast_sweep.py --restore-to $SLURM_SCRATCH/spot2` on the node first and glob that instead —
node-local, so the full set is never reassembled on `/vast`. `--status` says whether any of this applies;
if it reports 0 archived, the plain glob is complete.

### 2b. Verify completeness — the sweep does NOT guarantee it

A shard can end having spotted less than its share, and the count of `boxes_*.jsonl` is the only thing that
proves otherwise. Two ways it happened on 29 July:

- **Locale.** `boxes_<tag>.jsonl` was written through a plain `open()` while `json.dumps` emitted non-ASCII
  (`ensure_ascii=False`). Python encodes text files with the *locale* charset, and one preempt node
  (`gpu-n61`) had no UTF-8 locale — so identical code that ran clean on fifteen shards killed **34%** of
  regions on that one, after paying for all nine mosaics of inference each time (`ö`, `°` — recogniser noise
  as much as real names). Not a property of the data; a property of the machine. Fixed twice over:
  `encoding="utf-8"` in `spot_sheet.py`, and `LC_ALL=C.UTF-8 LANG=C.UTF-8 PYTHONUTF8=1` in both sbatch
  scripts so it cannot depend on where a shard lands.
- **Dead GPU.** rtx6k nodes have no kernels in this torch build; those shards logged `FAIL` per region and
  exited *reporting success*. `spot_sheet` now aborts after 10 consecutive failures.

Because the resume rule skips only regions that already have a boxes file, **a failed region is retried
automatically by any later run of the same shard** — restarting a broken shard recovers its losses; no
separate mop-up pass is needed. What is needed is the check:

```bash
ls /vast/ishi/gb1900/edition/spot2/boxes_*.jsonl | wc -l          # must reach 35,514
grep -h "^FAIL" cov/spotall_*.log | sed 's/.*: //' | sort | uniq -c
sbatch spot_all.sbatch    # if short: resumes, touching only the gaps
```

### 3. Retrain and re-evaluate the join on `spot2/`
```bash
python join_train.py --boxes '/vast/ishi/gb1900/edition/spot2/boxes_gb_*.jsonl' \
    --max-files 4000 --sample-per-region 200 --out join_rf5.joblib
python assemble_labels.py --boxes '…/spot2/boxes_gb_*.jsonl' --validate \
  --blocks-from join_rf5.test_blocks.json --model join_rf5.joblib --model-thr 0.5 \
  --max-lines 3 --centre-tol 0.25
```
**Sizing, measured 29 July — read before submitting.** `join_train` is the cheap half: 4,000 files with
`--sample-per-region 200` gave 305,931 pairs over 700 blocks in ~12 min inside 64 G. `assemble_labels` is
the expensive half, because it holds every word *and* every candidate pair in memory at once:

| held-out files | words | RSS | wall per stage |
|---|---|---|---|
| 506 | 122 k | 27 G | ~33 min — fine |
| 2,290 | 412 k | 64 G → OOM; 400 G → **182 G, TIMEOUT at 8 h** | never finished one stage |

**Violently superlinear — 3.4× the words cost >13× the time and 6.8× the memory.** 412 k words is already
past the practical ceiling: with 400 G it still could not complete a single stage inside 8 h. The full series
(~35.5 k regions → ~10 k held-out files → ~1.8 M words) therefore **cannot be evaluated whole.** Either keep
the bounded frozen sample (~500 held-out files, which produced the table below), or profile and fix the
assembly's scaling first — suspect candidate-pair generation, not the load.

Do **not** extrapolate stage time from load time: loading 2,290 files took 45 min and the assembly then ran
7 h without finishing. There is no progress output inside that phase — add one before trusting an estimate.

Four traps, each of which cost a run:

- **Never `--out /dev/null`.** `assemble_labels` derives `<out>.validate.json` from `--out`, so it dies with
  `PermissionError` *after* completing all the work — and `set -e` then takes the later stages with it.
- **Always `python -u`.** Otherwise stdout block-buffers into the Slurm log and a healthy job looks identical
  to a hung one; also to an OOM-killed one.
- **Freeze the evaluation region list.** While the sweep runs the boxes directory GROWS (9,567 → 11,830 in
  two hours), so "every Nth file" picks a different set on each computation and two runs cannot share a
  table. Frozen at `/ix1/ishi/gb1900/edition/join_eval_regions.txt`.
- **Run every comparison stage in ONE job**, sharing one snapshot and one split.

**Re-measure the baselines on every split.** On the new split nearest-word alone rose 0.219 → 0.363, so the
learned join's 0.582 is *not* a +0.157 gain on the published 0.425 — most of that gap is a different, easier
test set. Only the rule baseline scored on the same held-out blocks supports a claim.

**Sample broadly, not deeply.** 60 regions currently give 31,779 pairs, so the full series would give
~18.8M — far more than a 19-feature model needs, and taking *everything* from a few dense regions is exactly
what produced the selection bias. `join_train` now shuffles the region list (largest-first would take every
pair from the densest towns and none from the countryside) and `--sample-per-region` caps the draw from any
one region, spreading the same budget across far more of the country, which is where the rare faces live.

With real `gline` the reconstruction fallback stops being exercised — re-run the ablation
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
contaminated by the starved regions and was not quoted. After the full sweep this becomes answerable
**nationally** rather than on a handful of sheets — which is what was originally asked for. The measurable
footprint stops being scattered squares and becomes the series.

---

## Envisaged downstream processing

**Gated on step 4.** The two miss-rates decide which way the weight falls, so do not commit to a shape
before they are known:

- if **GB-STAMP misses a lot**, the font-derived hints cover only part of the corpus and GB1900 remains the
  backbone, with typography an enrichment where it exists;
- if **GB1900 misses a lot**, GB-STAMP is contributing genuinely new labels and the pipeline has to carry
  them as first-class records, not as annotations on someone else's;
- if **both miss substantially but different things**, the union is the product and the disagreement set is
  itself a deliverable (goal 3 — checking the crowd at scale).

### D1. Metadata onto the joined labels, as W3C Web Annotation

Already designed and implemented — `gbstamp_records.py`, `gb-stamp/docs/data-model.md`. A joined label is an
annotation whose target is an `oa:List` of its member words, in reading order.

**Extend each label with what the lettering physically is**, via `typography_body()`:

- **cap height**, in map pixels *and* in metres on the ground. Pixels are only comparable within one zoom
  level; the ground figure is what any later analysis actually wants.
- **face** — one of the 15-face inventory, as a URI, with confidence and ranked alternatives (several OS
  categories are engraved in an identical face and are inseparable by design).
- **slant**, and the line count for multi-line labels.

Height is deliberately *not* folded into the face. On the six-inch series the typeface encodes feature TYPE
and the size encodes IMPORTANCE, per the 1897 Characteristic Sheet — a parish name and a county name can
share a face and be separable by height alone. Collapsing them would discard half the signal.

### D2. Co-occurrence analysis — the hint layer

Two passes over the whole corpus, both cheap once D1 exists, and both needing the national sweep rather than
a sample to be worth anything:

1. **Word ↔ face.** Which words habitually appear in which face. This is what disambiguates the text:
   *Camp*, *Castle*, *Cross* and *Stone* mean an antiquity in the antiquity hand but a modern feature in
   roman or italic. The lexicon alone gets these wrong; the face settles them.
2. **Name-class ↔ (face, size).** Take known name lists — parishes, districts, settlements, hills,
   railways, counties — and measure which face *and which cap height* their names are set in across the
   corpus. Size is not decoration here: it is the second axis of the OS's own encoding, and a class that
   shares a face with another may be separated by height.

Note this replaces the earlier intention to bring in **independent records of civic status** (market towns,
parliamentary representation, administrative areas). We are no longer planning to capture those. The signal
is derived from co-occurrence within the corpus instead — cheaper, self-contained, and it does not inherit
another dataset's coverage gaps.

### D3. Map to AAT

Recovered types aligned to the Getty Art & Architecture Thesaurus, carried as a `classifying_body` with the
AAT URI, confidence and ranked alternatives. This is the step that makes the corpus thematically searchable
and linkable, and it is where the `alternatives` list earns its place: an inseparable face pair degrades to
two candidate types rather than one false verdict.

---

## Where things stand

**Assembly** — the largest validated piece. Held out by 12-km blocks, exact reproduction of the volunteer's
transcription.

Published figures, on the network-fed pass (kept for the record; **not** comparable to the table below):

| | |
|---|---|
| nearest single word | 0.219 |
| hand-set rules | 0.381 |
| learned join + sequence constraint | 0.425 |
| …+ hard negatives, on a later split | 0.442 → **0.453** with end tangents |

Four attempted improvements there: **hard negatives +0.009**, **end tangent +0.011**, face features
**null**, greedy topology **null**.

**Retrained on the corpus-fed pass, 29–30 July** — 305,931 pairs over 700 blocks (vs 31,779 over 60
regions). One frozen split throughout: 506 held-out region files, 122,468 words, 25,080 GB1900 labels,
210 blocks the model never saw.

| on the same split | exact | contains all | right *n* | over-join |
|---|---|---|---|---|
| nearest word alone | 0.357 | — | — | — |
| hand-set rules | 0.517 | 0.651 | 0.618 | 0.325 |
| **learned join + sequence constraint** | **0.576** | 0.691 | 0.696 | 0.300 |
| learned, end tangent ablated | 0.509 | 0.651 | 0.631 | 0.333 |
| *control: same model, OLD pass, 3,765 labels* | *0.546* | *0.743* | *0.648* | *0.327* |

**The whole table sits ~0.14 above the published one, baselines included** (nearest-word 0.219 → 0.357), so
the level reflects an easier split — far-northern, sparser, shorter labels — not a 0.425 → 0.576 improvement.
Read the gaps, not the absolutes:

- **learned over rules: +0.059** (was +0.044). The learned advantage survives 10× the training data and
  widens slightly.
- **the end tangent is now worth +0.067, six times the +0.011 it scored before** — and it is the single
  largest effect measured. This is the question `gline` made answerable: with the model's *own* centre-line
  rather than one reconstructed from the outline, direction is the dominant signal. Ablate it and the
  learned join (0.509) falls **below** the hand-set rules (0.517): the model's edge over the rules depends
  entirely on having a real baseline to read direction from.
- the learned join joins more (28.3% of words vs 21.4%) *and* is right more often, with less over-join.

**Over-join is now the leading error mode**: 30% of labels carry extra words, and only 69.6% have the right
word count. That, not pairwise precision, is where the next gain is.

Caveat on the control: it necessarily scores different labels (only the 297 old-pass files inside the
held-out blocks, 3,765 labels), and those files average 47.6 words against 242 in the new pass — so its
higher nearest-word baseline (0.393) partly reflects sparser detections making single-word labels trivially
matchable. Indicative, not a like-for-like control.

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

**The spotter never actually read the tile corpus until 29 July, and the fallback hid it.** `mosaic()`
reads its 64 tiles through a 16-thread pool; `sqlite3.connect` defaults to `check_same_thread=True`, so the
connection cached by one thread raised in every other, `store_tile` swallowed the exception as a miss, and
S3 silently served the whole sweep. A test against the pre-fix code retrieves **0 of 64** known-present
tiles threaded, 64/64 serially. Consequences, all of which have to be assumed of any output made before
that date:

- Regions cost minutes to an hour because they were network-bound — the corpus bought nothing because
  nothing consulted it. The 33-hour build was sound; the reader was not.
- Under 8 concurrent shards the fallback drew `503 SlowDown` and gave up after 5 retries, so **in-region
  tiles were dropped even where the corpus held them** (`mosaic 1/9 empty (miss=15)` on a block that is
  64/64 present). The 94 "starved regions" are the visible tail of this, not a separate accident.
- Same region, same weights, before and after: **742s → 25s, and 15 boxes → 93.** So the old pass was
  spotting on largely absent imagery. **Do not treat the `spot/` → `spot2/` comparison as a regression
  test** — the imagery was not identical, and reproduction is not the expected outcome. Measure the old
  pass's dropout with it instead.

Two smaller faults found with it, both fixed: `store_tile` never consulted the corpus's `absent` table, so
every recorded 404 was re-asked of S3 on every visit; and the connection liveness probe was
`SELECT count(*) FROM tile`, which on a `WITHOUT ROWID` table with the PNG inline walks the whole b-tree —
~140 MB of NFS reads per connection. (Same property makes `--verify` a ~1.5 h job.) `SPOT_NO_FETCH=1` now
makes the corpus authoritative for the sweep, so the mosaic grid's overrun past a region edge cannot become
live S3 traffic: harmless inland where the neighbour cached those tiles, ruinous at the coast where there
is no neighbour.

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
| `spot_all.sbatch` | staged: spot ALL 35,514 regions from the corpus into `spot2/`, one model load per shard |
| `vast_sweep.py` | the `/vast` valve: archives finished regions to `/ix1` above 90%; `--status`, `--sweep`, `--restore-to` |
| `vast_watch.sbatch` | runs the valve every 10 min for the duration of the sweep (htc, 1 core) |
| `respot_all.sbatch` | superseded by `spot_all.sbatch`; covered only the 1,307 already spotted |
