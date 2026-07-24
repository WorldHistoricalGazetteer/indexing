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
| **Discriminate** font | **MapReader ViTAEv2 backbone**, on the *original* crop | Hook `detection_transformer.backbone.0.backbone`; feed 512² grayscale→3ch crop; concat stage3/4/5 mean-pool (896-d). Imbalance-robust maxsim-LOO **0.63** on 189 anchors — but see §1.2: that number is now legacy. |

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

**Phase A — pin-prompted localisation over a stratified sample. DONE as an instrument; needs scaling.**
- `hisam_pins.py` works and is validated. What remains is choosing the sample and running it.
- **Scope is a stratified sample, not full GB.** Full-corpus typing is deployment, not the paper: size the
  sample for per-signature error bars across signature/feature categories plus geographic and temporal spread
  — order 10–50k entries. At the measured 0.42 s/window this is hours, and tile fetch is a targeted prefetch
  rather than a full-GB crawl. Reserve the 2.55M-pin run for a later deployment pass once the number is locked.
- Sampling knobs already in hand: `pin_category_coverage.py` gives per-category counts and ~10 km-cell spread;
  the NLS sheet-index WFS (memory `os_sheet_index_wfs`) gives per-sheet survey/publication dates for the
  temporal axis.

**Phase B — re-derive the anchors and re-measure the discriminator.**
- Re-crop the 189 existing sig-anchors from Hi-SAM **line** masks under the fixed convention, re-extract the
  896-d backbone descriptor, re-run imbalance-robust maxsim-LOO. This replaces 0.63 with the pipeline's own
  number. Note the two models live in separate envs — stay two-stage (npz intermediates).

**Phase C — weak-label bootstrap, then a balanced verified set.**
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

**In the repo (`developer/gb1900-font-probe/`), current path:** `build_pin_index.py` + `pin_index.sbatch`,
`hisam_pins.py` + `hisam_pins.sbatch`, `validate_pins.py`, `pin_category_coverage.py` + `coverage.sbatch`.
Deploy target is `/vast/ishi/gb1900/probe/hisam/` (scp; the pitt git-deploy is broken).
**Legacy/still-useful:** `mapreader_backbone_probe.py`, `extract_descriptors.py`, `build_label_ui.py`,
`train_readout.py`, `hisam_font_probe.py`, `run_hisam.py` (AMG sweep — keep for the recall limitation study),
`prepare_mosaics.py`. **Taxonomy:** `font_taxonomy.json` (49 faces → 16 signatures base_style·fill·decor);
`reference/exemplars/*.jpg`.

**Read the memory files first:** `gbstamp_hisam_recovers_disjoint_labels`, `gbstamp_labelling_pipeline`,
`gbstamp_backbone_style_embedding`, `gbstamp_size_angle_signals`, `gbstamp_mapreader_setup`,
`gbstamp_full_coverage_spotting`, `feedback_academic_rigor_gbstamp` — they carry the why behind each decision.
This is an academic methodology project: validate/characterise error before generating numbers; use the
principled instrument, not shortcuts.
