# GB-STAMP — interactive alphabet-building & face-classifier workflow

> **The live route is BIGCAPS connected components — see the section at the foot of this file.**
> The hand-boxed workflow described first is the earlier direct-supervision route. It produced the
> 951 face anchors that the face classifier still rests on, so it is kept, but per-letter alphabets
> are no longer built by drawing boxes on small mixed-case words.

Human-in-the-loop pipeline that builds a **face-labelled per-letter glyph set** from real GB1900 map
labels and trains a font-**face** classifier on it. This is the direct-supervision route: the earlier
methods (shape-fan seeded from single-letter Characteristic-Sheet exemplars, and the size/slant/fill
gates) topped out because the 44 faces had *no direct real-crop labels* — this workflow creates them.
See `../gbstamp_size_angle_signals` memory for why size/slant/fill failed as gates.

## The loop

```
 (A) generate candidates ──▶ (B) human annotation ──▶ (C) extract glyphs ──▶ (D) train ──▶ back to (B)
     make_alphabet_ui.py        alphabet_ui.html         extract_alphabet.py   train_faces_knn.py
```

### A. Generate candidates → `alphabet_ui.html`
`make_alphabet_ui.py` (run via `alphaui.sbatch` on CRC — needs the z17 tiles) surfaces candidate labels
from three sources and crops a map snippet for each:
- **admin** — gazetteer-confirmed admin labels from `detect_admin_harvest.py` (`harvest.jsonl`).
- **large** — the biggest ALLCAPS labels (true cap height via `minAreaRect`, horizontal aspect, England/
  Wales lat<56, OCR-garbage dropped) — the rare big single-letter-exemplar admin/town faces.
- **descriptive** — word-content-hinted labels (rivers, stations, churches, antiquities, woods…) for the
  non-caps faces, via the `LEX` lexicon.

Knobs: `--n-admin`, `--n-large`, `--large-minh`, `--n-desc-per`. Candidate ids are order-stable (admin +
descriptive first, large appended) so re-generating **preserves existing browser annotations**.

### B. Human annotation → `alphabet_labels.json`
Open `alphabet_ui.html` (pulled to `admin_probe/`). Two-column master–detail:
- **left** = candidate rows; **right** = the full 48-face taxonomy, each shown with its **exemplar
  specimen image** + style·fill·decor, so you match by letterform, not attribute strings.
- **Select** a row (click or start drawing) → **assign its FACE** from the right column (by the specimen).
  Label by the *font*, not the name (e.g. a borough-named label set in the county-borough font = county_boroughs).
- **Fix the transcription** if the spotter misread it (letters/boxes re-map to the correction).
- **↻ rotate** to level a curved river/canal label, **🔍 zoom** to enlarge (rotation clears boxes — set it
  first). The rotated snippet is exported directly, so pixels and boxes share one frame.
- **Draw a box per letter** (letters auto-fill from the transcription, in order). Skip non-labels.
- **Download** `alphabet_labels.json`. localStorage persists progress; each download is cumulative.

### C. Extract glyphs → `labels/alphabet_glyphs.npz`
```
python3 extract_alphabet.py --labels labels/alphabet_labels.json
```
Per letter box: crop → **line-erase** → **tangent de-rotate** → `norm_glyph` (44×36 binary). Both cleaning
steps are ON by default and both improved the face kNN in A/B tests:
- **LINE-ERASE** (`line_erase.erase_crossing_lines`, default; `--no-erase-lines` to disable, `--touch` for
  the looser criterion): removes thin straight map lines (roads/boundaries/contours/railways) that cross the
  character box edge-to-edge — discriminated from thick letter strokes by width (median distance-transform
  along the segment), erasing only thin pixels so a thick I/L/T stem survives where a line crosses it.
- **TANGENT DE-ROTATION**: each letter uprighted by the local baseline tangent of the ordered box centres
  (map-layout rotation removed, the font's own italic slant retained). Writes `angle` into the npz.

Outputs the npz + a QC montage (`admin_probe/alphabet_glyphs_qc.png`, glyphs grouped by face, tilt annotated).

### D. Train → face classifier
```
python3 train_faces_knn.py
```
Per-glyph **same-letter kNN, leave-one-WORD-out**, voting the FACE (the established 0.776-style method, now
with direct face labels). Reports per-glyph + per-word accuracy, and which faces have ≥2 words (the only
fairly-testable ones). A small CNN becomes worthwhile once the set is deep+balanced (the pixel-CNN scored
0.56 on scarce data — see the memory).

## Coverage / candidates
- **More regions** = more candidates: `spot_full.sbatch` (GPU) spots representative centres. It now wraps
  each region in `timeout` (a hung S3 tile fetch previously stalled a task for hours). Restrict to England
  with a `centres_english.txt` (empty lat<56 repr regions, densest-first) via `--export=ALL,CENTRES=…`.
- Re-run `detect_admin_harvest.py` + `make_alphabet_ui.py` after new regions land to grow the candidate set.

## Data-depth target
Same-letter kNN needs several examples of each (letter, face). Aim for **~8–10 words per face, balanced**,
across the faces of interest (≈700+ glyphs). At ~1–4 words/face it is near chance for most faces and
single-word faces can't be validated at all.

## Key files
| file | role |
|---|---|
| `make_alphabet_ui.py` → `alphabet_ui.html` | candidate generation + annotation UI |
| `detect_admin_harvest.py` → `harvest.jsonl` | gazetteer-confirmed admin candidates |
| `extract_alphabet.py` → `labels/alphabet_glyphs.npz` | glyph extraction (line-erase + de-rotate, default on) |
| `line_erase.py` | map-line erasure (`erase_crossing_lines`) |
| `train_faces_knn.py` | per-glyph same-letter kNN face classifier |
| `font_taxonomy.json` | 48 faces with human `style`/`fill`/`decor` (from `cs_decisions`) + exemplars |
| `spot_full.sbatch` | GPU region spotting (timeout-guarded) |

---

# The live route — BIGCAPS connected components

Admin lettering on the six-inch sheets is **letter-spaced by design**. That spacing is why MapReader's
word spotter never fired on it, and it is also why these labels need no segmentation: on a cleaned sheet
**each capital is already its own connected component**. The earlier per-letter failure does not apply
here and should not be cited as though it did — the 4.7% ink-gap figure was measured on MapReader's small
*kerned mixed-case* words, where a letter boundary has to be inferred because the ink does not mark it.

```
 rf_clean.py apply --out-labels ──▶ bigcaps_components.py ──▶ bigcaps_alphabet.py
   per-pixel class map               letters + labels + glyphs    overlay clusters = (face, letter)
                                            │                              │
                                     bigcaps_qc_*.html            bigcaps_alphabet_qc.html
                                     accept / reject / transcribe   name each cluster once
```

## 1. Clean the sheet → class map
```
python rf_clean.py apply --model rf.joblib --tag sheet_ENG_038_NE --bbox W S E N \
  --out-labels lab_ENG_038_NE.png
```
The class map is used to **filter whole components**, never to erase pixels: erasure can eat a letter,
whereas a mis-scored component is merely dropped. A component is kept when ≥`--min-text` of its ink is
classified text.

## 2. Extract letters and group them into labels
```
python bigcaps_components.py --tag sheet_ENG_038_NE --bbox W S E N \
  --labels-png lab_ENG_038_NE.png --out-glyphs bigcaps_glyphs_ENG_038_NE.npz
```
- **Windowing is invisible in the result.** Components are collected in *sheet* coordinates and grouped
  **once**, over the whole sheet. Grouping per window and discarding the second copy cannot work: a label
  straddling a window edge is seen not twice but in halves, and two halves are not duplicates of each
  other. A component clipped by a window edge is dropped there — the overlap is wider than `--max-h`, so
  it is present whole in the neighbour.
- **Grouping is by collinearity on a STRAIGHT line** — BIGCAPS carry no curvature (SG). That makes the
  residual a far sharper test than a curve fit: a quadratic can absorb three arbitrary points.
- **A dropped letter is bridged by pitch, not by slack.** One capital lost to the class map leaves a hole
  ~6 letter-heights wide at admin spacing, beyond any threshold that is safe to raise blindly. Two runs
  are rejoined only when the distance between their facing letters is a whole number of the label's own
  pitch — the hole is made of missing letters, so it must measure a whole number of them — and the merged
  run must still fit one baseline. `MID`+`LETO` → `MIDLETO`, `LANGBAR`+`MOOR` → `LANGBARMOOR`.
  The lost letter is *not* invented; only the grouping is repaired.
- Multi-line labels are merged **after** the line fit, as parallel equal-height stacks, so the
  straight-line residual stays strict on each line.
- `--out-glyphs` writes rasters scaled by the **group's** cap height, not each letter's own bbox: within a
  label the letters are set at one size, so an O's overshoot stays visible as the difference it is.

QC (`bigcaps_qc_*.html`): accept with a transcription, reject, or click a single component to drop it
(a group is often right except for one blob of map furniture). Decisions persist in localStorage and
download cumulatively.

## 3. Cluster the letters into the alphabet
```
python bigcaps_alphabet.py --glyphs 'bigcaps_glyphs_*.npz' --max-dist 0.28
```
An engraved sheet is not handwriting: the same letter in the same face is struck from the same punch, so
two instances should overlay almost exactly. Distance is `1 - IoU` maximised over a ±2px shift; linkage is
**average**, not single — single linkage would chain through the near-misses between C/G, O/Q, E/F and
collapse the alphabet into one blob.

**It works.** On two sheets (1,252 glyphs, 287 labels) the cut at 0.28 gives 162 clusters of ≥2 members
covering 899 glyphs, median within-cluster overlay 0.798. Every large cluster is one crisp letterform —
the cluster mean is a sharp letter, not a smear — and *the same letter in different faces lands in
different clusters*, which is exactly the (face, letter) cell the alphabet needs.

Each cluster records the `labels` its glyphs came from. Letters set in the same label are necessarily in
the same face, so those 191 shared labels are the evidence that groups clusters into **faces** without
anyone judging a face by eye. That step is not built yet.

## What is not solved
- **Dense urban sheets stay swamped.** 43.9% hatch vs 3.1% rural. Hatched building blocks survive the
  class map, and they survive the shape filters too: a row of buildings along a street is collinear,
  equal-height and regularly spaced. An "ink runs per horizontal scanline" test was tried and **does not
  separate them** (rural median 2.0, urban median 2.0 — the hatching is drawn with thick diagonal strokes
  a horizontal line rarely crosses). Rural sheets are the material for now.
- **Numerals dominate the clusters.** Spot heights and parcel numbers are the commonest repeated glyphs on
  a sheet, so the largest clusters are 8/0/6/9/7/2/4/3/5. They are not noise, but they are not place-label
  evidence either. (Distinct from the earlier finding that numerals are absent from the *GB1900 pin
  transcriptions* — that is about the pins, not about the sheet.)
- **The tail fragments.** 353 of 1,252 glyphs stay singletons and the median cluster is 2 — thin strokes
  (I, l, /, ]) split across many clusters. More sheets, not a looser cut, is the remedy to try first.
