# GB-STAMP: A Typed Edition of GB1900 — Methodology

> **GB-STAMP** = a place-**t**yped edition of the **GB1900** gazetteer, assigning each of the
> 2,666,341 crowd-transcribed labels a probabilistic feature-type derived from the label text and
> the Ordnance Survey lettering conventions. This document is the reference methodology (for
> reproduction and for a methodology paper). It is a clean writeup; the chronological R&D log is
> `developer/plan-gb1900-typing.md`.

**Status:** full-corpus text-based typing shipped (`gb_stamp_types.jsonl`, top-3 types + probabilities
per label); z17 font-refinement campaign in progress. Author: WHG / Stephen Gadd, with autonomous
assistance. Date: 2026-07.

---

## 1. Goal

GB1900 is a volunteer transcription of every text label on the Ordnance Survey County Series
six-inch-to-the-mile 2nd edition (England, Wales, Scotland; surveyed/revised c.1888–1914). It records
each label's **text** and **point location**, but carries **no feature type** — a "Well", a hamlet
name, a "Tumulus", and a road name are undifferentiated. GB-STAMP assigns each label a
**feature-type** with a calibrated confidence, turning the gazetteer into a type-filterable, analysable
dataset for Digital Humanities and for ingestion into the World Historical Gazetteer.

The core methodological claim: **the OS six-inch encodes feature type in its typography** — the
lettering *style*, *size*, and *case* of a label systematically signal what kind of feature it names —
and this, combined with the label text and a controlled abbreviation vocabulary, is sufficient to type
most labels. This is grounded in the OS's own specification (§4).

---

## 2. Data sources

| Source | Use | Licence |
|--------|-----|---------|
| **GB1900 gazetteer** (2,666,341 labels: text + lon/lat) | the corpus to be typed | CC0 (raw dump) |
| **OS six-inch 2nd ed. tiles** (NLS, `mapseries-tilesets` S3; z16 full-GB cached, z17 native ceiling) | imagery for font classification | NLS terms |
| **OS "Examples for the Characters of the Writing on the Engraved Six Inch Ordnance Maps of Great Britain" (1897)** — the *Characteristic Sheet* | authoritative font→feature-type key + size/case conventions | CC-BY (NLS, view/128076792) |
| **OS Abbreviations** (compiled by Dr Richard Oliver, hosted by NLS, `maps.nls.uk/os/abbrev/`) | controlled abbreviation → meaning vocabulary (776 entries) | courtesy NLS/Oliver |
| **GB1900 gazetteer admin join** (`gb_admin.jsonl`: parish/district per pin) | settlement-name gazetteer feature | derived |

---

## 3. Pipeline overview

```
GB1900 label (text, lon/lat)
      │
      ├─ TEXT signals ──────────────────────────────────────────────┐
      │   · tier-0 rules (abbrev / keyword / numeric)                │
      │   · OS abbreviation vocabulary (ambiguity → distribution)    │
      │   · head-noun word-semantics (House→building, Coppice→wood…) │
      │   · road/antiquity/boundary/tidal keyword patterns          ├─►  type_assign v2
      │   · settlement-name gazetteer match                         │    → top-3 (type, prob)
      │                                                              │
      └─ FONT signals (z17 imagery) ────────────────────────────────┤
          · style: CRNN real-domain encoder (blackletter/spaced/…)  │
          · size:  cap-height (canonical per category, §4)          │
          · case:  allcaps                                          ┘
```

Text signals are available for **every** label (full-corpus). Font signals require a crop and are
applied where z17 imagery is fetched (§6). The output for each label is a **top-3 distribution over
feature types**, descending, so a consumer takes `types[0]` as the best guess and has the alternatives
+ confidence for ambiguous cases.

---

## 4. The typographic ground truth (OS Characteristic Sheet, 1897)

The OS Characteristic Sheet specifies the writing style for each feature/administrative category on the
engraved six-inch maps. It is the authoritative key behind GB-STAMP and it **confirms the mappings we
had reverse-engineered empirically**:

- **Water** (Navigable Rivers & Canals; Small Rivers & Brooks) → *italic* serif.
- **Antiquities** (Roman; Pre-historic or Saxon; Norman or Subsequent) → *Gothic / black-letter*.
- **Administrative units** (Counties, Hundreds, Parishes, Townships, Boroughs, Cities, Wards,
  Liberties…) → distinct *capital* forms, each with a single-letter boundary mark (C, H, P, T, B, W…).
- **Settlements / land / buildings** (Villages, Parks & Demesnes, Gentlemen's Seats, Manufactories,
  Farms, Woods & Copses…) → Roman upright / stump forms.

Two spec facts are load-bearing for the method:

1. **Size = importance, and is otherwise fixed.** The sheet's size-variability note applies *only* to
   "Bogs, Moors and Forests" and "Ranges of Hills". Every other category has a **fixed canonical font
   size** → measured cap-height is a *reliable per-category discriminator*, not merely a soft cue.
2. **The 1879 character change.** A set of categories (Hundreds, Parishes, Divisions/Subdivisions of
   Townships, Divisions of Counties, Cities with/without MPs, Market/Other Towns, Extra-Parochial,
   Turnpike Trusts) were engraved in **different characters before vs after 1879**. The style→type
   mapping is therefore **edition-dependent** and must be keyed on each sheet's publication year.

The full category list, with per-category style, the 1879 (†) and size-variable (*) flags, and the
single-letter boundary marks, is captured in `developer/gb1900-font-probe/reference/os_categories.json`
— this is the crosswalk source to Getty AAT (the publishing step).

---

## 5. Font-style typing — R&D and findings

Determining a label's *style* from the pixels is the hard part. The full arc (with negative results,
which a methodology paper should report):

**5.1 VLM zero-shot (rejected).** A vision-language model (Qwen2.5-VL-72B) asked to classify `os_style`
was incoherent on clean crops (italic/upright conflated; outline vs filled confused). VLMs are weak at
fine typographic attributes, and the label taxonomy conflated orthogonal axes.

**5.2 Synthetic contrastive embedding (domain gap).** A CNN style-encoder trained with supervised
contrastive loss on *synthetic* renderings (typefaces spanning the OS axes, degraded onto real map
backgrounds) separated the classes almost perfectly *in synthetic* (kNN 0.99) but **collapsed on real
crops** — a classic synthetic→real domain gap. Iterating the realism (paper-tone flat-field correction,
curved baselines, linework, unsupervised real-crop consistency) only *partially* closed it.

**5.3 Real-domain recogniser (the fix for the gap).** Training a CRNN+CTC **text recogniser** on real
`(crop, transcript)` pairs — free labels, since every crop carries its GB1900 text — yields an encoder
whose features are *real-domain* with no synthetic gap, plus a working OS-six-inch OCR and per-glyph
embeddings via the CTC alignment as byproducts. Its encoder beat the synthetic one on every style class.

**5.4 The rare-font breakthrough (targeting + z17).** Rare styles (black-letter antiquities; spaced
parish capitals) were absent from any single region, so no classifier could learn them. The fix was
**data-driven targeting**: mine the crowd transcripts for antiquity terms (Tumulus, Cairn, Camp,
Earthwork…) and administrative density, fetch **z17** (native-resolution) tiles *where those styles
live* (barrow country; urban cores), and auto-label crops for free from the text. This took
black-letter from 2 anchors to 96% classification accuracy, and spaced-caps to 98% — z17 was essential
because at z16 the letterforms are upscaled, not resolved.

**5.5 The serif residual (an honest limit).** The upright-vs-italic serif distinction — *settlement
name* vs *descriptive/water feature* — plateaus at **~0.71** across every method (few-shot, fusion,
glyph-level, word-lexicon, settlement-name, and a dedicated cv2 **stroke-slant** feature). Diagnosis:
OS italic is only mildly slanted (~5° vs ~2°), so the distinction is carried by *letterform + semantic
role*, and the role (village vs farm) often isn't determinable from the isolated crop — a human needs
map context too. Word-semantics (§ below) is the strongest single signal here (.71–.86), so the serif
axis is typed primarily from the text, with font as a soft, confidence-gated enrichment.

**Net:** most OS lettering types classify at 0.80–0.98 by font; the one context-dependent serif nuance
stays soft and is carried by text semantics + (fixed) size.

---

## 6. Text-based typing (`type_assign` v2)

`processing/gb1900/type_assign.py` emits the top-3 `(type, probability)` distribution per label from:

- **OS abbreviations** (`os_abbrev_types.json`, from the NLS/Oliver list). Marks are matched on a
  normalised key (dots/spaces/plurals stripped) and — crucially — **ambiguity is preserved as a
  distribution**: `B.R.` → {benchmark, bridle-road, bridge, brow}; `P` → {pump, post, pole, …};
  `F.P.` → {footpath (lead), fire-plug, face-of-paling}. The top-3 output is exactly the right shape
  for this. (The list spans all OS scales/eras; not all appear on the six-inch, so these are candidate
  priors, not GB1900-frequency-calibrated.)
- **Head-noun word-semantics.** The last word (plural-aware) drives the type: "Bankfield **House**" →
  building, "Oak **Coppice**" → wood/land-cover, "Mill **Pond**" → water. This is the OS Characteristic
  Sheet's semantic distinction made operational, and it is the primary signal for the serif residual
  (settlement name vs descriptive feature).
- **Pattern rules:** road suffixes (→ road, corroborated by the font detector), antiquity terms
  (→ antiquity, font-validated at .96), boundary annotations, tidal/coastal marks, quarry/mine, trees,
  stone markers.
- **tier-0 rules** (abbrev/keyword/numeric) from the GB1900 pipeline, and a **settlement-name**
  gazetteer match (parish/district names) for the settlement/building split.

Probabilities are a normalised distribution; residual mass goes to `unknown`. The typer was iterated
against the actual corpus residual until the "settlement" bucket contained only genuine proper-name
places (49% → ~20%).

---

## 7. The z17 full-corpus campaign

> **Superseded for localisation — see §12.** The per-label crop-window campaign described here has been
> replaced by a complete z17 tile corpus on `/ix1` plus a full-series spotter sweep (all 35,514 regions,
> 16.77 M detections). §12.1 also records a defect that invalidates spotter output produced before
> 29 July 2026.


To apply *font* signals across the whole corpus, z17 imagery is fetched **per label crop-window** (only
tiles that actually cover labels — ~3.7 M unique tiles, not the 40 M-tile bounding box, since coverage
is sparse). Storage is bounded by **batching**: the corpus is split into 28 latitude bands; each band
**fetches its tiles → types → tars the tiles to `/ix1` → drops them from `/vast`** before the next band
(the "coordinates/params, not fragments" principle — tiles are transient working data, the durable
outputs are the typed results + the `/ix1` tar archive). Edge labels pull cross-band tiles via the
crop-window fetch, so none are clipped. The run is resumable (per-band `.done` markers) and additive —
a later spotter rescan will surface untranscribed labels, which are *added*, never dropping existing
ones. Code: `processing/gb1900/z17_batch.py` (worker) + `run_z17_campaign.sh` (driver).

---

## 8. Results (text-based pass)

All 2,666,341 labels typed → `gb_stamp_types.jsonl` `{place_id, pin_id, text, types:[[type,prob],…]}`.
~75% type to specific feature classes at good confidence; ~20–25% "settlement" are genuine proper-name
places (the serif residual), flagged low-confidence with alternatives. Leading types (best-guess):
settlement, footpath, well, water_feature, building, road, pump, signpost, named-land-cover, footbridge,
quarry/mine, farm, benchmark, admin/parish, antiquity, boundary, milestone/milepost, church, school, …

Font-validated per-class accuracies (z17): black-letter/antiquities **0.96**, spaced-caps/parish
**0.98**, road (font-only, no text) **0.80/0.80**, water/descriptive serif (word-rule) **0.86**;
serif upright/italic **~0.71** (the residual).

---

## 9. Confidence, ambiguity, and honesty

- Every label carries a **distribution**, not a single label — ambiguous marks (per the OS list) and
  the serif residual are represented as such, not overclaimed.
- The `unknown` mass is explicit.
- Font refinement is **confidence-gated** and marked (`font_refined`) where applied.
- Known unresolved marks are left low-confidence rather than guessed.

## 10. Limitations

- **Serif settlement-vs-farm** ~0.71 (context-dependent; §5.5).
- **Edition-dependence** (§4.2): admin categories' style changed in 1879; full correctness needs a
  per-sheet publication-year lookup (not yet wired).
- **Abbreviation priors** are from the full OS reference, not six-inch-frequency-calibrated.
- **Font coverage** follows z17 fetch; the campaign extends it corpus-wide.
- **AAT crosswalk** (os_categories → Getty AAT) is the remaining step to publish `types[]` on the live
  gazetteer records.

## 11. Reproducibility (code & data map)

- **Typer:** `processing/gb1900/type_assign.py` (+ `os_abbrev_types.json`, `admin_names.json`),
  runner `run_typing.py`.
- **z17 campaign:** `processing/gb1900/z17_batch.py`, `run_z17_campaign.sh`.
- **Font R&D:** `developer/gb1900-font-probe/` (synthetic probe, CRNN, fusion, per-glyph, slant, serif
  experiments, `reference/` = OS Characteristic Sheet strips + `os_categories.json`, `labels/` = HITL
  anchors, `fonts/SOURCES.md`).
- **Env locks:** `developer/gb1900-font-probe/env-locks/`.
- **Data (CRC):** corpus `/vast/ishi/gb1900/edition/national_typed.jsonl`; output
  `/vast/ishi/gb1900/edition/gb_stamp_types.jsonl`; z17 archive `/ix1/ishi/gb1900_tiles17/*.tar`.
- **Chronological R&D log:** `developer/plan-gb1900-typing.md`.

## 12. The corpus-fed campaign and national benchmarks (July–August 2026)

This section supersedes §7 for localisation. §§4–6 and 8 (the *text*-based typer) are unaffected; what
changed is how the imagery is read, how words are grouped into labels, and — for the first time — how the
result is measured against GB1900 across the whole series rather than a sample.

### 12.1 A silent defect invalidated all earlier spotter output

`spot_sheet.py` reads a mosaic's 64 tiles through a 16-thread pool. `sqlite3.connect` defaults to
`check_same_thread=True`, so the per-block connection cached by one thread raised in every other; the
exception was swallowed and counted as a missing tile, and the S3 fallback quietly served the run from the
network. Measured against the pre-fix code: **0 of 64** known-present tiles retrieved on the threaded path,
64/64 serially.

The consequences are not confined to speed. Under concurrent load the fallback drew `503 SlowDown`, gave up
after five retries, and dropped in-region tiles the corpus already held — so **every spotter output produced
before 29 July 2026 was generated on partially absent imagery**. On one region, same weights, same corpus:
742 s → 25 s, and 15 boxes → 93. The 33-hour corpus build had been sound throughout; nothing was reading it.

Two smaller faults were found with it: `store_tile` never consulted the corpus's `absent` table, so tiles
already proven to be upstream 404s were re-requested on every visit; and the connection liveness probe was
`SELECT count(*)`, which on a `WITHOUT ROWID` table with the PNG stored inline walks the whole b-tree
(~140 MB of NFS reads per connection). A `SPOT_NO_FETCH` mode now makes the corpus authoritative for full
sweeps, so the mosaic grid's overrun past a region edge cannot become live network traffic.

*Methodological note.* Each of these presented as a plausible performance characteristic rather than an
error. The tell was a measurement that did not fit — 742 s against a ~65 s budget — which is the same signal
that exposed the 33.9° curvature artefact (§9). An implausible number is evidence.

### 12.2 The full-series sweep

All **35,514 regions** spotted from the local z17 corpus (2,366/2,366 blocks verified complete, 329.6 GB):
**16,766,274 detections**, median 438 per region, `miss_frac` median 0.0000. 330 regions yield nothing;
all are genuine — every tile is recorded `absent` in the corpus, and they sit at Rockall, St Kilda and the
open Atlantic, where NLS holds no six-inch coverage. There are no starved regions in the earlier sense.

Against the superseded pass, on 1,229 comparable regions: **94.0%** of old detections reproduced exactly,
**1.3%** lost, and the new pass yields **2.54×** as many boxes, of which **62.9%** are additions. Regions the
old pass itself recorded as tile-starved gained most — one went from 1 box to 1,727 at `miss_frac` 0.998,
which is the dropout hypothesis confirmed by natural experiment rather than inference.

The re-spot also retains the spotter's own `pixel_line` (`gline`) per detection — present on 0 of 128,150
old boxes and 325,486 of 325,486 new ones. §12.3 shows this is the single most valuable signal in the
assembly.

### 12.3 Word → label assembly, measured nationally

Trained on 398,381 labelled pairs over 1,781 12-km blocks. One frozen held-out split throughout: 400
regions, 214,735 words, **62,745 GB1900 labels**, 534 blocks the model never saw. Metric is *exact*
reproduction of the volunteer's transcription.

| on the same split | exact | contains all | right *n* | over-join |
|---|---|---|---|---|
| nearest word alone | 0.286 | — | — | — |
| hand-set rules | 0.483 | 0.608 | 0.568 | 0.298 |
| **learned join + sequence constraint** | **0.578** | 0.694 | 0.682 | 0.267 |
| learned, end tangent ablated | 0.473 | 0.623 | 0.589 | 0.293 |

Both margins roughly doubled against the earlier sample-scale figures (0.219 / 0.381 / 0.425): learned over
nearest +0.206 → **+0.292**, learned over rules +0.044 → **+0.095**. The national split is *harder*
(nearest-word falls 0.357 → 0.286) yet the assembled score is unchanged, so the model generalises.

**The end tangent is worth +0.105 — the largest single effect in the work**, against +0.011 when the tangent
had to be reconstructed from the outline. Ablate it and the learned join (0.473) falls **below** the
hand-set rules (0.483). The model is not learning to group better in the abstract: its entire advantage
rests on reading direction from the spotter's own centre-line. This retrospectively explains why face
features and greedy topology were both null — the missing information was *direction, not typography* — and
it makes the assembly a downstream beneficiary of the §12.1 fix.

Over-join is now the leading error mode (26.7% of labels carry extra words; 68.2% have the right word
count). The join threshold is untuned at 0.50.

### 12.4 The reachable face inventory (a scope decision, not a gap)

The 15-face inventory is the Characteristic Sheet ideal. The instrument reaches **6**; the other 9 carry
zero anchors, and the reason is structural rather than a shortfall of labelling effort.

| face | anchor glyphs |
|---|---|
| Upright-Solid-Plain | 1,533 |
| Italic-Solid-Serif | 1,522 |
| Upright-Solid-Serif | 168 |
| Blackletter | 64 |
| Italic-Solid-Plain / Italic-Outline-Serif | 17 / 6 |
| **the remaining 9** | **0** |

Every unanchored face but one (Workhouses) is an **administrative** category set in outline, hatch or ornate
capitals at large cap heights — County Names (233 px), Divisions of Counties (191 px), Hundreds (130 px),
Ancient Parishes, Poor Law Unions, Urban Sanitary Districts, Municipal and County Boroughs, Liberties,
Divisions of Townships, Cities not returning Members, Other Towns. These are the letter-spaced capitals of
the abandoned BIGCAPS route: the word spotter never fires on them (the letter-spacing is why), Hi-SAM boxes
them without text, and the connected-component route stayed swamped on dense urban sheets. Anchors can only
come from crops the spotter produced, so the gap cannot be closed without reopening a route abandoned for
two independently sufficient reasons — the second being that the `vob_*` and `kain_par` gazetteers already
hold those units as **dated polygons**, strictly better evidence than a recovered point-with-a-name.

Independently, **6 of the 13 categories behind these faces are pre-1879 engravings**, and 95.6% of GB1900
sheets are published 1897 or later, so they are near-absent from this imagery in any case.

They are therefore recorded in `labels/face_inventory.json` as `scope: out-of-reach` **with the reason
attached**, not as `unknown`. The distinction is the honest one: we have not looked and failed to tell; the
instrument structurally cannot produce the evidence. "40% of the inventory is unknown" would misdescribe it.

This does not weaken the co-occurrence work, whose central case — *Camp*, *Castle*, *Cross*, *Stone* meaning
an antiquity in the antiquity hand rather than a modern feature in roman or italic — rests on Blackletter,
Upright-Solid-Plain and Italic-Solid-Serif, all reachable. The real weakness is the **imbalance among the
reachable six**: Blackletter at 64 glyphs and Upright-Solid-Serif at 168 against ~1,500 each for the two
dominant faces. That *is* closable from spotter output, by the lexical targeting that already took
blackletter from 2 anchors to 96%, now applied across the whole series instead of a sample.

### 12.5 Lexical weak supervision: works for blackletter, fails elsewhere (negative result)

The starved faces were harvested at scale from the corpus by lexical weak supervision — words whose OS
category is unambiguous auto-label their own lettering, no human input. Blackletter went 64 → 3,031 glyphs
and Upright-Solid-Serif 168 → 3,039. Before any pixel was read, the selected words' median cap heights
(33.0 px, 35.2 px) matched the 1897 specification for Antiquities-Norman (33.0) and Woods & Copses (36.0).

The test: type 192 human-labelled items (`font_testset_decisions_1.json`, coarse axis — italic 114,
upright 45, blackletter 33) from a reference of **harvested anchors only**, and compare with leave-one-out
within the human labels. Both in the backbone descriptor space, both on the same items.

| | |
|---|---|
| chance (majority class, italic) | 0.594 |
| baseline — human-reference LOO | **0.833** |
| harvested reference | **0.568** |

**The harvested reference performs below chance, and it is a real failure, not an artefact.** The space
control settles that: median nearest-neighbour similarity is 0.9881 test→test against 0.9886
test→harvested, a gap of −0.0005, so the harvested anchors occupy the same region of the embedding as the
test items. Crop convention is not the explanation.

The per-class breakdown is where the usable finding is:

| class | n | baseline | harvested |
|---|---|---|---|
| **blackletter** | 33 | 0.788 | **0.818** |
| italic | 114 | 0.947 | 0.544 |
| upright | 45 | 0.578 | 0.444 |

**Blackletter — the one starved face that mattered — is where harvested anchors match and slightly exceed
human labels. Italic collapses.** The mechanism follows from the design: a face's anchors come from a
vocabulary of ~10 words, and the descriptor is a whole-word crop squashed to 512². Blackletter letterforms
are unmistakable whatever word they spell; italic and upright must be inferred from subtler cues, so a
reference built from *{spring, well, ford, weir, sluice}* encodes those word shapes rather than the face.
It becomes a word detector. Per-term caps prevent one term dominating a face; they cannot fix a lexicon
that is itself the bottleneck.

Consequences: harvested anchors are used for **Blackletter only**; the human anchors remain the reference
elsewhere; and the lexicon is NOT widened to rescue italic, because the unambiguous vocabulary is already
exhausted and the remaining candidates (*camp*, *castle*, *bridge*, *chapel*) are exactly the
context-dependent words the co-occurrence analysis must settle on its own evidence.

Note also that the baseline's own upright score is 0.578 with human labels — a ceiling imposed by the
coarse axis conflating Upright-Solid-Serif with Upright-Solid-Plain, not by the harvest.

### 12.6 The face pass: mosaic ROI-align is context, not letterform (negative result)

Attaching a face to 8.8M labels needs a per-word descriptor, and there are two ways to get one that differ
by ~50x in cost: a 512² crop per word (measured 0.44 s each -> ~1,100 GPU-hours at label level) or one
backbone forward per 2048px mosaic with every box ROI-pooled from the same feature map (~39 GPU-hours).
`backbone_readout.py` names the second as the deployment intent. It does not work, and the way it fails is
worth recording.

Measured on 14 regions spread the length of the country (5,997 words), three classifier settings on the
identical sample:

| setting | italic | upright | blackletter | blackletter slant |
|---|---|---|---|---|
| unweighted, pooled reference | 0.80 | 0.18 | 0.02 | — |
| inverse-frequency balanced | 0.62 | 0.10 | 0.15 | **4.29°** |
| √-balanced, augmented reference | 0.55 | 0.29 | 0.15 | **5.77°** |

Blackletter is a Gothic **upright** hand, so its slant should sit near upright's ≈0°, not near italic's
6.2°. It never does. Raising a minimum box-width gate from 0 to 200 px *increases* the blackletter share
(0.15 → 0.37) and pushes its slant further into italic (5.77° → 7.35°), so this is not a resolution limit:
at 200 px a word spans ~12 feature cells. The explanation consistent with all of it is **context
contamination** — ViTAE stages 3–5 have receptive fields far larger than a word box, so pooling a
sub-rectangle of a deep feature map encodes the neighbourhood. Blackletter share varies 0.07–0.48 by region;
antiquity anchors sit in characteristic terrain and words in similar terrain inherit the label.

Two methodological points, both of which generalise beyond this project.

**An independent physical measurement can adjudicate a classifier without new labels.** Slant is measured
from the pixels (`slant_v2.slant_deg`, which deskews first, so it is stroke slant and not the label's
rotation on the map). It is the only reason the failure was visible: the inverse-frequency setting produced
a *more plausible* class distribution than the unweighted one and would have been accepted on that basis.
Class shares are tunable; a physical property of the ink is not.

**State what a test set cannot detect before reading a pass as a pass.** Mosaic ROI-align scored 0.655
against per-word 0.674 on the pooled labels and was accepted as equivalent. That set holds **11**
blackletter items, so the number was carried by italic and upright and the comparison was structurally
incapable of expressing the failure that mattered. The same shape recurred three times in this work: a
12-item face test that could not decide anything, a miss-rate whose headline swung 12x with the matching
radius, and this.

Italic-versus-upright *does* survive at mosaic scale — slant confirms it independently (6.2° vs −0.06°) —
so the cheap descriptor is usable for the coarse axis and unusable for the antiquity hand specifically.

### 12.7 How much do GB1900 and GB-STAMP each miss?

299 sheets, sampled evenly across pin density (0–34.1 pins/km²), every sheet 100% measurable. Neither side
is ground truth, so neither figure is an error rate; what is measured is the size of the disagreement.

| at the 48 px matching radius | median | pooled |
|---|---|---|
| GB-STAMP misses (pinned label not detected) | 0.281 | **0.287** over 52,303 non-numeric pins |
| GB1900 misses, word-adjusted | 0.349 | **0.357** over 149,574 non-numeric detections |
| GB1900 misses, strict | 0.581 | — |
| reading agrees exactly, on matched labels | 0.278 | — |

**The matching radius does most of the work** and must be quoted with any figure. A GB1900 pin sits at the
*start* of a label and often just off the ink:

| radius | GB-STAMP misses | GB1900 misses |
|---|---|---|
| 24 px | 0.616 | 0.500 |
| **48 px** | **0.281** | **0.349** |
| 96 px | 0.053 | 0.210 |

GB-STAMP's rate swings **12×** across that range: "GB-STAMP misses 5%" and "misses 62%" are both available
to anyone willing to choose a radius. All three are therefore reported. What survives every choice is that
both quantities are large.

Numerals are **26.1% of detections and 0.0% of pins** — excluded from both directions, but a quarter of what
GB-STAMP finds is real map content (spot heights, benchmarks) that the crowd never recorded and this
comparison structurally cannot score.

**Consequence.** Both sides miss substantially and miss different things, so the union is the product:
GB-STAMP carries genuinely new labels as first-class records *and* validates the crowd, rather than
annotating someone else's data. The disagreement set is itself an output
(`sheet_misses_national.jsonl`).

---

## 13. References

- Ordnance Survey, *Examples for the Characters of the Writing on the Engraved Six Inch Ordnance Maps of
  Great Britain* (1897). National Library of Scotland, https://maps.nls.uk/view/128076792 (CC-BY).
- R. Oliver, *Ordnance Survey Abbreviations*, hosted by NLS, https://maps.nls.uk/os/abbrev/.
- GB1900 project (Great Britain 1900 gazetteer), CC0.
- National Library of Scotland OS six-inch tile service.
