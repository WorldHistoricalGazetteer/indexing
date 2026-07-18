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

## 12. References

- Ordnance Survey, *Examples for the Characters of the Writing on the Engraved Six Inch Ordnance Maps of
  Great Britain* (1897). National Library of Scotland, https://maps.nls.uk/view/128076792 (CC-BY).
- R. Oliver, *Ordnance Survey Abbreviations*, hosted by NLS, https://maps.nls.uk/os/abbrev/.
- GB1900 project (Great Britain 1900 gazetteer), CC0.
- National Library of Scotland OS six-inch tile service.
