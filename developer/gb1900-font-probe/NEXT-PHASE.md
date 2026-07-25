# GB-STAMP — Next-Phase Handoff

**Purpose:** brief a fresh agent/session to build out the validated GB-STAMP feature-typing pipeline.
Written 2026-07-24 at the end of the instrument-validation + Hi-SAM-feasibility work; **revised 2026-07-24**
after pin-prompted localisation was built and validated (§1.1, §2). The auto-loaded memory files (`gbstamp_*`)
carry the fuller story; this doc is the build plan + hard-won tacit knowledge.

---

## 1. Where we are — proven, do NOT re-derive

**The pipeline has a settled shape** (each stage backed by measurement, not guess):

| Stage | Tool | Evidence |
|---|---|---|
| **Localise** each label | **Hi-SAM prompted at its GB1900 pin** | 408/408 Oxford pins returned a mask; 89% of detections ≥80% covered by MapReader's boxes. See §1.1. |
| **Transcript** | **GB1900 gazetteer** — attached by construction | The prompt IS the gazetteer entry, so there is no join, no match radius, no ambiguity. MapReader's recog head is font-invariant anyway. |
| **Discriminate** font | **MapReader ViTAEv2 backbone**, on the *original* crop | Hook `detection_transformer.backbone.0.backbone`; feed 512² grayscale→3ch crop; concat stage3/4/5 mean-pool (896-d). Imbalance-robust maxsim-LOO **0.596** on Hi-SAM line crops (§1.3). |

**Key insight:** the instrument (backbone) always worked; the bottleneck was DATA — the distinctive-font
categories (admin/antiquities/water), where typography beats content, were absent from the MapReader pool
because MapReader can't detect disjoint letter-spaced labels. That gap is now closed at the source: we no
longer *detect* labels at all, we *localise known ones*.

### 1.1 Phase A is settled: pin-prompted localisation, not an AMG sweep

The corpus is the GB1900 pinned label (~2.55M usable transcription pins), so GB1900 **is** the inventory —
nothing needs discovering. Hi-SAM's decoder takes `oracle_point_prompts`
(`auto_mask_generator.py:177,250`), so we prompt the hierarchical decoder at each pin. The AMG sweep in the
feasibility test was a *capability check* (can Hi-SAM see disjoint labels at all — yes), never the production
mode. Consequences, all in our favour:

- no 1500-point AMG grid: the decoder runs once per pin, and the ViT-L encode is the only real cost;
- no cross-window merge/dedup for letter-spaced labels, and no nearest-match radius to tune;
- **~0.42 s per 1024² window** on an A100 — 408 Oxford pins in 71 windows in 30 s, end to end.

**Measured on Oxford (`gb_4338_2896`, 408 pins vs MapReader's 1268 boxes) — `validate_pins.py`:**

| Quantity | Value | Reading |
|---|---|---|
| pins yielding a detection | **408/408** | no recall loss from prompting |
| detection area covered by MapReader's boxes | median **1.00**, ≥0.8 for **89%** | the masks land on real label ink |
| best-single-box IoU vs MapReader | median **0.31** (0.32 even single-token) | a **crop-convention** difference, not disagreement — Hi-SAM's masks are tighter and sit *inside* MapReader's looser boxes. This is precisely why the two must never be mixed (§1.2). |
| line mask / word mask area | **3.4×** | the line level spans the whole label |
| mask swallows a neighbouring label's pin | word **1.7%**, **line 3.2%** | over-merge is negligible → **crop from the LINE mask** |
| line mask truncated at window edge | **0.7%** | a 4-tile (1024 px) window is big enough |
| volunteer's pin lands on its label's ink | **74.8%** | ~1 in 4 pins sits in white space; the mask is still right (see dead ends) |

**Crop unit = the LINE mask.** 70% of GB1900 entries are multi-token ("ST. ALDATE'S STREET"), and one prompt's
*word* mask covers only the word under the pin, so a word crop under-covers its own transcript. The line mask
covers the label and almost never reaches into the neighbouring one.

**GB1900 does contain the distinctive categories** (`pin_category_coverage.py`, corpus-wide, ~10 km cells) —
the fact that decided Phase A's shape:

| category | pins | spread | | category | pins | spread |
|---|---|---|---|---|---|---|
| water | 102,810 | 3,341 cells | | works | 122,781 | 2,886 |
| antiquities | 44,066 | 2,950 | | hills | 59,486 | 2,666 |
| woods | 80,090 | 2,459 | | roads (35.5k ALLCAPS) | 112,891 | 2,235 |
| seats | 94,182 | 2,940 | | boundaries | 31,180 | 2,058 |
| churches | 40,497 | 2,758 | | ALLCAPS proper nouns (admin-ish) | 29,996 | 2,815 |

Tens of thousands per category, nationally spread — three orders of magnitude more than the 30–50 per
signature the paper needs. **Caveat, stated honestly:** this is *prevalence*, not *recall against what is
printed on the sheets*. Measuring how many printed labels no volunteer ever pinned needs an AMG sweep as
ground truth on sample sheets — worth doing as a stated limitation, not as a blocker.

### 1.2 One box authority — and it is Hi-SAM, so 0.63 is superseded

Descriptors only compare under one crop convention, and §1.1 shows the two conventions differ systematically
(IoU 0.31 while coverage is 1.00). A hybrid — MapReader boxes for the descriptive majority, Hi-SAM for the
rest — would let the discriminator key on *crop convention* instead of font: a confound fatal to the paper.
So: single authority, Hi-SAM. The 189 anchors, the 113k descriptor bank and the **0.63 are all legacy
MapReader-crop numbers**; re-derive the anchors on Hi-SAM line masks under the fixed convention and re-measure.
Cost is one job.

### 1.3 The discriminator survives the box change — and the imbalance is the real problem

Phase B re-cropped the 189 anchors from Hi-SAM masks and re-measured (`anchor_recrop.py` →
`anchor_recrop_readout.py`). All three columns pass through the *same* `derotate` geometry, so the only
variable is which polygon it is handed — any difference is attributable to the box, not to a reimplementation.
188/189 anchors survived (one had no Hi-SAM mask); line masks on all 188.

| crop convention | maxsim-LOO | kNN5 |
|---|---|---|
| MapReader box (control) | **0.622** | 0.585 |
| Hi-SAM word mask | 0.596 | 0.638 |
| **Hi-SAM LINE mask** (production) | **0.596** | 0.628 |

The control reproduces the legacy 0.63, so the comparison is sound. Switching box authority costs ~0.026 on
maxsim-LOO and *gains* ~0.04 on kNN5 — five anchors either way at n=188, i.e. no real change. **The box switch
is free.** The pipeline's number is now **0.596 maxsim-LOO / 0.628 kNN5**.

The per-signature breakdown is the finding that actually matters:

| signature | n | maxsim-LOO |
|---|---|---|
| blackletter·solid·fancy | 14 | 0.786 |
| upright·solid·plain | 37 | 0.703 |
| italic·solid·plain | 30 | 0.700 |
| italic·solid·serif | 88 | 0.602 |
| **upright·solid·serif** | **14** | **0.071** |
| **numeral·solid·plain** | **5** | **0.000** |

Two classes are collapsing, and only one of them is excusable by size. `upright·solid·serif` at n=14 scores
0.071 — it is being swallowed by `italic·solid·serif`, the 47% majority class, which is exactly the imbalance
pathology the last measurement warned about. Blackletter at the same n=14 scores 0.786, so this is not a
small-n artefact: **upright-vs-italic within the serif family is the discriminator's actual weak axis.** Phase C's
balanced set must prioritise `upright·solid·serif` and `numeral·solid·plain`, and the headline number should
not be quoted without this table. Only 6 of the 16 signatures appear in the anchors at all.

### 1.4 GB1900 has no numerals — so numerals leave the target set

`weak_label_report.py` over the first 21 sampled regions (10,680 detections): 18.0% carry a type word and are
weakly labelled, the rest are bare place names and are correctly left unlabelled. Supply for the two failing
signatures of §1.3 could not be more different:

- `upright·solid·serif` — **606 candidates from 21 regions** (~3,500 extrapolated to the full sample), from
  `parish_churches` / `woods_copses` / `ranges_hills` / `Bogs, Moors and Forests`, all of which resolve to it.
  The weak axis is well supplied; Phase C can fix it.
- `numeral·solid·plain` — **zero. Not scarce: absent.** GB1900 asked volunteers to transcribe *names*, so spot
  heights, bench-mark values and contour numbers were never pinned. This is visible in the Oxford overlay too:
  `B.M. 203·` carries a MapReader box and no pin.

This is the inherited-gap risk landing, for exactly one category. The resolution is scope, not effort: the
classifier only ever has to type things GB1900 pinned, and GB1900 has no numeral entries, so
**`numeral·solid·plain` is out of scope for the pin-prompted corpus.** (Its 0.000 in §1.3 came from 5 anchors
that were MapReader boxes, never GB1900 pins.) If numerals are ever wanted — a height-model or contour study —
they need the AMG sweep, not this pipeline.

That leaves **`upright·solid·serif` as the single weak axis Phase C must fix**, and it is well supplied.

### 1.5 First verified round: the weak axis moves, and a crop defect surfaces

A labelling round on `upright·solid·serif` returned **231 verified signatures and 9 cards the labeller could
not judge**. Three results, in order of importance:

**Weak labels are ~48% precise.** Of 240 cards the lexicon proposed as `upright·solid·serif`, the human
confirmed 116; 73 were `italic·solid·serif` and 39 `upright·solid·plain`. Exactly why weak labels seed
sampling and never supply a number (§2).

**The weak axis improves 5× with data but is still the binding constraint:** `upright·solid·serif` goes
**0.071 (n=14) → 0.382 (n=131)**. The imbalance diagnosis was right, but 131 examples is no longer starvation,
so upright-vs-italic *within* the serif family is a real limit of the descriptor. Note the verified set is
**not a random sample** — it is concentrated in one confusable neighbourhood by construction, so its 0.517 is
a hard-case number, not the pipeline's expected accuracy. Future rounds draw across all categories with an
even per-signature quota (`build_label_ui --mode weak` without `--weak-sig`).

**Slant fusion is refuted.** The obvious explanation — that mean-pooling discards the lean a human reads —
fails at the first diagnostic: shear median `upright·solid·serif` **+0.005** vs `italic·solid·serif` **−0.026**,
IQRs almost entirely overlapping. Slant alone scores 0.377 (below the 0.50 majority) and fusing it *lowers*
the descriptor from 0.515 to 0.472. This re-confirms in the new convention what `gbstamp_size_angle_signals`
already recorded — check that memory before re-running it a third time.

**A crop defect the automated pipeline never noticed.** Re-presenting the 9 unclear cards with context showed
several masks sitting on blank ground or building hatching, with the transcript's label nowhere inside the
crop, and one (`HEATH TOWN`) cropped upside down — `minAreaRect` is 180°-ambiguous and nothing resolves it.
Those descriptors carry no font information while being indexed under a real GB1900 transcript, so they are
worse than missing. **The human was the only detector that caught this.** Two attempts to automate it both
fall short, and are recorded so nobody re-runs them expecting better:

| measure | result |
|---|---|
| Otsu ink fraction / contrast (`crop_qc.py`) | **no signal at all** — 0% flagged corpus-wide; hatching and road casing have the same ink statistics as lettering |
| Hi-SAM text-foreground fraction (`crop_qc_text.py`) | **weak and confounded** — unclear median 0.018 vs labelled 0.024, and three cards the human read fine score exactly 0.000. Small crops are upscaled ~30× to the model's 1024 input, far out of distribution |
| aspect-per-character (`crop_audit.py`) | **best available proxy** — unclear median 0.224 vs clear 0.521, corpus 0.528 |

So: do **not** filter the bank on either QC measure — the false-positive rate on `crop_qc_text` alone would
discard good data. The rate of no-label crops corpus-wide is **not yet known**, which is a live caveat on
every number in §1.3 and this section, since such crops sit in the bank unflagged. Getting it needs either a
text-detector run at native crop scale (no upscaling) or a small human audit of a random sample — the latter
is cheap, unambiguous, and the only method so far demonstrated to work.

**DEAD ENDS — do not retry** (all measured and rejected):
- Unsupervised clustering of the spot pool → fails (segmentation noise, near-chance).
- ROI-align / map-context pooling of the backbone → hurts (0.42→lower).
- Per-letter decomposition / single-letter 512² crops → OOD, worse.
- **Cleaning crops for the backbone** (stroke-mask + whiten background) → HURTS (0.63→0.55): the backbone is
  text-detection-trained so already ignores map background, and imperfect masks clip serif/terminal detail.
  (Clean strokes only help a *dumb* raster: 0.19→0.44.)
- SAM ViT-L features as the descriptor → 0.53, below the backbone.
- VLM font recognition → documented to fail (2025 papers); os_style route was incoherent.
- Size/cap-height as discriminator for the serif family → cap-heights overlap (county_bridges 36 == woods_copses 36).
- Grow-by-nearest-neighbour labelling → redundant (near-duplicates).
- **Snapping the prompt to the nearest ink** (`--snap 24`, i.e. "fix" the 25% of pins in white space) → HURTS:
  moved 58% of prompts and dropped on-ink 74.8%→71.8%, because the nearest ink is often a map line or building
  hatch rather than the label. Hi-SAM handles an off-ink prompt fine on its own. Default `--snap 0`.

---

## 2. The build (remaining phases)

**Phase A — pin-prompted localisation over a stratified sample. DONE.**
- **122 region files, 40,694 detections, zero failures** — essentially 100% of the 40,321 predicted entries.
- Descriptor bank rebuilt on Hi-SAM LINE crops: **39,001 descriptors, 12 tile misses (0.03%)** versus ~32% on
  the legacy bank, because the tiles are still hot from localisation.
- Weak labels at full scale: **7,986 (19.6%)**; `upright·solid·serif` **2,575**, `numeral·solid·plain` **0**
  (§1.4 confirmed at scale).
- Sample chosen by `build_sample.py`: **121 regions, 40,321 entries, 58 distinct 100 km cells**, every weak
  category above its 600-entry floor (roads 8412 … stations 600). Written to
  `probe/font/centres_sample.txt` in the same `lon lat tag count` format as `centres_all.txt`.
- Selection is **cluster-stratified**: sampling regions rather than scattered pins keeps windows dense so z17
  tiles amortise across many labels (~44k tiles total, versus ~10M for the old full-GB MapReader pass), and
  greedy-on-category-deficit with a per-100 km-cell diminishing return stops the sample from being all city
  centre — which would over-represent roads and starve antiquities.
- `pins_array.sbatch` runs it: 8-way resumable Slurm array, skips any region with a non-empty
  `pins_<tag>.jsonl`, `--fetch` on (GPU nodes have outbound net), each region wrapped in `timeout 1800`
  because S3 sockets hang past urlopen's timeout. **Tile-fetch bound, not GPU bound** — keep the array narrow.
- Temporal axis is deliberately not stratified: 95.6% of GB1900's sheets published ≥1897 (memory
  `os_sheet_index_wfs`), so the 1879 font-change regime is effectively absent and one classifier suffices.
- Full corpus (2.55M pins) stays a later deployment pass, once the method's number is locked.

**Phase B — DONE, see §1.3.** The box switch is free (0.596 vs 0.622 control) and the real finding is the
per-signature table: `upright·solid·serif` collapses to 0.071 against the 47% `italic·solid·serif` majority.

**Phase C — weak-label bootstrap, then a balanced verified set.**
- **Priority is set by §1.3/§1.4, not by category size:** the balanced set must fix `upright·solid·serif`
  (upright vs italic *within* the serif family). Ten more antiquities anchors would not move the number; ten
  upright serifs would. Numerals are out of scope (§1.4).
- **Pipeline in place:** `extract_descriptors_pins.py` builds the Hi-SAM-convention descriptor bank over the
  pin detections (same field names as the legacy bank, so `build_label_ui.load_bank` needs only a path change),
  carrying the weak signature per row. `weak_sig.py` is the one shared lexicon; `weak_label_report.py` tracks
  supply per signature as the sample fills.
- **The circularity rule, non-negotiable:** the hypothesis under test is that TYPOGRAPHY carries feature type,
  so a weak label derived from WORD CONTENT is not evidence about the font. Weak labels may only choose which
  crops a human sees and pre-fill the answer they correct. Every reported number comes from verified labels.
- **The first round is built and waiting on a human:** `label_ui/label_ui_upright_serif.html` (240 cards, drawn
  from the 2,574 `upright·solid·serif` weak candidates, farthest-point-diversified). Export to
  `probe/font/labels/pool_labels_hisam.json` — a NEW pool file, because the existing `pool_labels.json` is
  MapReader-convention. Regenerate with:

  ```
  python build_label_ui.py --mode weak --weak-sig "upright·solid·serif" --n 240 --min-score 0 \
    --bank "/vast/ishi/gb1900/edition/pins/desc/shard_*.npz" \
    --boxes "/vast/ishi/gb1900/edition/pins/pins_*.jsonl" --poly-field line_gpoly \
    --pool /vast/ishi/gb1900/probe/font/labels/pool_labels_hisam.json \
    --anchors-npz /vast/ishi/gb1900/edition/spot/anchor_desc_hisam.npz
  ```

  `--mode weak` exists because `grow` is a recorded dead end: growing a rare signature by nearest neighbours
  returns near-duplicates of what is already labelled, whereas the lexicon supplies varied words from the whole
  country. Each card shows the lexicon's proposal *and* the descriptor's own nearest-anchor opinion.
- **Expect disagreement, and do not read it as a bug:** the descriptor contradicts the lexicon on **228/240**
  cards, which is what a class scoring 0.14 kNN5 while being absorbed by the italic majority looks like. This
  round exists precisely to adjudicate it — and if the human finds the OS actually set these features in
  italic, then the *lexicon* was wrong, which is a real finding and exactly why weak labels never count as
  ground truth.
- Target **~30–50 verified per distinguishable signature** (~8–10 of the 16 in practice) → ~300–500 verified
  labels. Empirically blackletter went 0/5 → 0.85 at ~13 anchors, big classes were stable at 30–88, and
  *balance mattered more than raw count* (the 0.47 skew hurt).
- **The HITL budget is far smaller than that number**, because GB1900 transcripts are free weak labels:
  Tumulus/Cairn/Camp → antiquity (blackletter), River/Brook/Canal → water (italic), digits → numerals. The
  lexicons already exist (`make_alphabet_ui.LEX`, `make_font_testset_v2` ANTIQ/WATER, and the grouped copy in
  `pin_category_coverage.CATS` — keep ONE vocabulary, don't fork a second). Bootstrap thousands of weak labels
  lexicon→category→signature, then have the human *verify/correct a stratified subset* rather than label from
  scratch. Reuse the active-learning UI (`build_label_ui.py`, modes auto|uncertainty|grow|novelty), seeded
  from weak labels instead of cold.
- Then the fusion: signature × content/gazetteer → feature-type (AAT).

---

## 3. Environment & gotchas (hard-won — will bite a fresh agent)

**CRC access:** `ssh crc0` (login node) for `sbatch`/`squeue`/`scp` ONLY — no compute on login nodes (even
short network tasks → Slurm). GPU jobs: `-M gpu --partition=a100 --account=ishi --qos=gpu-a100-l --gres=gpu:1`.
CPU/network jobs: `-M htc --partition=htc --account=ishi --qos=htc-htc-s`. The ishi account's
`MaxGRESPerAccount` caps GPUs, so a `%8` array starves short probes — pause arrays or drop concurrency for
quick jobs. Conda: `source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh`.

**Hi-SAM env** `/vast/ishi/envs/hisam` (py3.10): torch 2.2.2+cu121, **MUST pin `numpy<2` +
`opencv-python-headless==4.10.0.84`** — torch 2.2 breaks on numpy 2 (`from_numpy` dies); opencv 5 forces
numpy 2. This trio is the only consistent combo. Also needs `einops scipy pyclipper shapely tqdm timm skimage`.
It has **no pandas** — hence `build_pin_index.py` parses the UTF-16 CSV with the stdlib, so the whole
pin pipeline runs in this one env.
- Repo: `/vast/ishi/gb1900/probe/hisam/Hi-SAM`. Weights in `.../hisam/weights/`: `hi_sam_l.pth` (139 MB,
  heads-only — **had to be browser-downloaded**, OneDrive `1drv.ms` defeats headless curl/wget/API) +
  `sam_vit_l_0b3195.pth` (1.25 GB, Meta-direct wget). Build reads the SAM backbone from the **hardcoded relative
  path** `Hi-SAM/pretrained_checkpoint/sam_vit_l_0b3195.pth` (symlinked), so it only works with **CWD at the
  repo** — `hisam_pins.build_model` chdirs for the build and restores CWD after (`sys.path` alone is not
  enough; that failure is a bare `FileNotFoundError` on a relative path).
- **Mask-token layout, easy to mirror wrongly:** `HiDecoder.forward(multimask_output=True)` already drops
  token 0 from *both* masks and iou preds, so `forward_hi_decoder` returns **(word, line, para)** and scores in
  the same order. `AutoMaskGenerator.predict` slices `[:, 1:]` a *second* time, which is why it addresses
  line/para as `[-2]`/`[-1]` while still reading `scores[:, 1]`. Copying its indices into fresh code is wrong.
- The finer word mask is the separate `word_masks_logits` (384² → `postprocess_masks`), not a hierarchy
  channel. Hierarchy masks are **always 256²** whatever the window size, so scale their pixel areas by
  `(window/256)²` before comparing with word-mask areas — otherwise every line/word ratio is 16× off.
- Stroke/foreground mask = `modal_aligner(features) → mask_decoder(...) → (high_res_mask > model.mask_threshold)`;
  SAM encoding = `amg.features` after `amg.set_image`.
- **Do not use `AutoMaskGenerator.predict` for pin prompts** — it score-thresholds and NMSes the prompt set,
  silently breaking the 1:1 pin↔detection correspondence the whole design rests on. Call `forward_hi_decoder`.
- **`score` is field-compatible in name but NOT in scale.** MapReader's is a detection confidence, thresholded
  at 0.55 all over this codebase; the pin corpus's is Hi-SAM's predicted mask IoU, which tops out near 0.42.
  Any inherited `>= 0.55` filter silently empties the corpus (it emptied the labelling UI's candidate pool —
  hence `--min-score`). There is no detection decision to be confident about when you prompted at a known
  label; filter on `on_ink` / mask area instead.

**MapReader env** `/vast/ishi/envs/mapreader`: MapTextPipeline. cfg
`.../mapreader_text/install/MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml`, weights
`.../install/weights/rumsey-finetune.pth`. NOTE the SAM/MapReader models live in SEPARATE envs — two-stage
(save intermediates to npz) to combine them. Still needed for the **descriptor**, no longer for the box.

**Tiles:** `/vast/ishi/gb1900/tiles17` (many `--cleanup`'d) + `/ix1/ishi/gb1900/tiles17` archive.
`hisam_pins.read_tile` reads both and only fetches from S3 with `--fetch` (GPU nodes have outbound net).
`make_font_testset_v2.derotate(box_record)` crops+de-rotates a word from cache (no fetch). The legacy
`FCTILES` fetch path uses fragile urllib (hangs on dead S3 sockets) — prefer `prefetch_tiles.py`.

**Misc:** npz not parquet (no pyarrow in either env). `/vast` is SHARED with prod ES and was at 84% — never
fill it (store descriptors/coords, not crop pixels). `find` over `/ix1` hangs (huge NFS) — don't.

---

## 4. Data & scripts

**Under `/vast/ishi/gb1900/`:**
- **Pin index:** `pins_z17.npz` — 2,552,458 GB1900 pins with text + z17 global px, sorted by tile key
  (`build_pin_index.load_pins` / `pins_in_box`). 1.79M z17 tiles carry pins, median 1/tile.
- **Pin detections:** `edition/pins/pins_<tag>.jsonl` — one record per pin, field-compatible with the
  MapReader `boxes_*.jsonl` records (`text`/`score`/`gpoly`/`gcx`/`gcy`/`lon`/`lat`) so existing crop and
  descriptor tooling reads it unchanged, plus `line_gpoly`/`para_gpoly`, areas, and the QC flags
  `on_ink`/`truncated`/`snapped`. Validation JSON + overlays alongside.
- **Category prevalence:** `probe/font/pin_category_coverage.json`.
- **Legacy (MapReader-crop convention — see §1.2):** `edition/spot/desc/shard_*.npz` (113k × 896-d),
  `probe/font/labels/pool_labels.json` (189 sig-anchors), `edition/spot/boxes_*.jsonl` (1,525 regions spotted
  of 35,514 centres; the paused full-coverage run is no longer on the critical path).
- **Hi-SAM feasibility:** `edition/spot/hisam_test/` — mosaics + AMG overlays (Oxford/Shetland).

- **Descriptor bank (Hi-SAM convention):** `edition/pins/desc/shard_*.npz` — 39,001 × 896-d, carrying
  `pin_id`, `text`, `score`, `weak_sig`, `weak_rule`. **Never mix with the legacy `edition/spot/desc/` bank.**
- **Phase B outputs:** `edition/spot/anchor_crops_hisam.npz` (188 anchors × {mr, word, line} crops),
  `edition/spot/anchor_desc_hisam.npz` (896-d descriptors per column + sigs — doubles as the anchor set for
  the labelling UI, since the anchors' own regions are not in the sample).
- **Sample:** `probe/font/centres_sample.txt` + `centres_sample.json` (selection provenance).
- **Weak-label supply:** `edition/pins/weak_label_report.json`.

**In the repo (`developer/gb1900-font-probe/`), current path:** `build_pin_index.py` + `pin_index.sbatch`,
`hisam_pins.py` + `hisam_pins.sbatch`, `validate_pins.py`, `pin_category_coverage.py` + `coverage.sbatch`,
`build_sample.py` + `sample.sbatch`, `pins_array.sbatch`, `anchor_recrop.py` + `anchor_recrop_readout.py` +
`anchor_recrop.sbatch`. Deploy target is `/vast/ishi/gb1900/probe/hisam/` (scp; the pitt git-deploy is broken).
**Do not put `set -u` in a script that activates the mapreader env** — its activate hook dereferences
`SYS_SYSROOT` unset and the job dies after the GPU work is already done.
**Legacy/still-useful:** `mapreader_backbone_probe.py`, `extract_descriptors.py`, `build_label_ui.py`,
`train_readout.py`, `hisam_font_probe.py`, `run_hisam.py` (AMG sweep — keep for the recall limitation study),
`prepare_mosaics.py`. **Taxonomy:** `font_taxonomy.json` (49 faces → 16 signatures base_style·fill·decor);
`reference/exemplars/*.jpg`.

**Read the memory files first:** `gbstamp_hisam_recovers_disjoint_labels`, `gbstamp_labelling_pipeline`,
`gbstamp_backbone_style_embedding`, `gbstamp_size_angle_signals`, `gbstamp_mapreader_setup`,
`gbstamp_full_coverage_spotting`, `feedback_academic_rigor_gbstamp` — they carry the why behind each decision.
This is an academic methodology project: validate/characterise error before generating numbers; use the
principled instrument, not shortcuts.
