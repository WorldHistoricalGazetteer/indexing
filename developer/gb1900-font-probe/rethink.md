# GB-STAMP font-typing — rethink (crop-localized, sheet-grounded)

> Context: the crowd-point font-merge was validated and FAILED at production scale (see
> `../plan-gb1900-typing.md` §15 RESULT — over-fires rare styles because arbitrary crowd-point windows
> are out-of-distribution and the model runs at balanced priors). This rethink relocates font-typing onto
> **clean, MapReader-localized crops** and adds the two guardrails that failure demands: a **reject/ordinary
> class** and a **base-rate-realistic precision gate**.

**Design principle (applies throughout): the classifier predicts a typographic STYLE (the sheet's
letterform classes), NOT a feature-type.** One face can carry several meanings — road names use the
Antiquities>Roman font; italic = water *and* mineral-railway; caps = admin *and* towns *and* roads. A
separate fusion step maps (style × text × size × date-regime) → feature-type. Nothing wires style→type 1:1.

# Characteristic Sheets

- Start by fully analysing the Characteristic Sheet. We already have the types tabulated, but should check
  those (show a human for verification of the completed table, including the cropped examples). Carefully
  crop the examples of each (a single letter in many cases, several words in others). **These sheet crops
  are the CANONICAL training anchors** — everything harvested from the maps later is validated against them.
- **Antiquities has FOUR distinct fonts** — Roman / Prehistoric-Saxon / Norman / Subsequent — expect
  difficulty disambiguating the middle two (Prehistoric-Saxon vs Norman). Capture all four exemplars.
- There is no separate type specified for road names: the font is the same as Antiquities>Roman. (This is
  exactly why the classifier must emit *style*, and text/context disambiguate road vs Roman-antiquity.)
- **Per-exemplar attributes to record (SG):** letterform STYLE (upright/italic/blackletter/numeral) ·
  CASE (caps flag) · **DECOR** (serif / plain / **fancy** — the ornate engraved initials) · **HATCHING**
  (none / horizontal / diagonal engraved shading, if present) · size-variable · date-regime. The verification
  table carries first-pass values for decor/hatching; the human corrects them in the crop-modal JSON export.
- **PAPER-TONE normalization (SG — critical, ties to the domain-gap finding):** the Characteristic-Sheet
  scan and the OS map tiles sit in DIFFERENT photometric spaces. Apply the SAME flat-field / paper-tone
  correction to the Char-Sheet ANCHOR crops as to the map-tile crops (paper canonical, only ink varies) —
  otherwise we reintroduce the synthetic→real gap that sank the first classifier. This is the iteration-2
  lever from the earlier R&D; it must run on anchors + production crops alike before training/inference.
- **Yorks/Lancs regional exception (SG):** the bracketed rubric on Poor Law Unions / Urban Sanitary Districts
  ("Applied on old maps of Yorkshire & Lancashire to Registrars Districts") applies ONLY to those two counties
  and ONLY on OLD maps. The R *font* is identical; only the mark→MEANING differs. So: EXCLUDE old Yorks/Lancs
  sheets from the PRIMARY mark-meaning tuning, and route them to a separate tuned interpretation (R→Registrars
  Districts) using the WFS county+date per sheet. (Clarify: two additional models = Yorks + Lancs, or the two
  affected marks? — confirm with SG; the WFS makes the routing mechanical either way.)
- Tabulate, per type: caps-only (where only a single letter is given), size-variable vs fixed, and
  date-regime validity. **The two regimes are <1897 and >=1897, and BOTH are on the 1897 sheet**: the
  †-marked categories give the pre-1897 letterform, the "on the more recent maps" entries give the >=1897
  letterform. Only the †-marked categories are date-conditional; the rest are date-invariant.
- For the fixed-size types, measure carefully the height of the capital letters — the maximum height of
  characters above the base-line, excluding characters that extend below it. Baseline detection on worn
  engravings is noisy, so record a tolerance, not a single pixel value.
- The Characteristic Sheet also has examples of Boundaries. The line styles are not of interest; the fonts
  and abbreviations used to LABEL each boundary type are — tabulate, crop, and measure them.
- **The boundary abbreviations feed the TEXT abbreviation lexicon, a SEPARATE workstream from the font
  classifier** (they're `os_abbrev_types.json` items, not letterform classes). They are clearly not an
  exhaustive list of all the abbreviations used across the map series; tabulate them separately from the
  a–z NLS abbreviation HTML files we saw earlier.
- The c.1923 Characteristic Sheet (https://maps.nls.uk/view/128076894) simply provides MORE examples of the
  >=1897 regime. It also includes the fonts used for contour-height numerals and "B.M." bench-mark labels,
  and boundary abbreviations (e.g. "Co. Boro. Bdy."); tabulate these too. "M.S" is shown for "Mile Stone" —
  it's unclear whether the single period is intentional, so treat it as a weighted abbrev distribution.
- Display the results from both sheets in a SINGLE table for human review, including the cropped examples.
  Columns: type, style/letterform class, canonical exemplar crop, caps-only?, size-variable?, date-regime
  validity, measured cap-height (+tolerance).
- For careful single-letter cap-height measurement, fetch **high-resolution IIIF region requests** (the
  full-image IIIF request hit the server pixel cap; tight region requests get under it).

# OS Sheets

- Use z17 tiles only. Manage carefully to avoid overfilling /vast (tar each batch to /ix1, drop from /vast
  before the next). Work in batches of OS Sheets.
- **Dependency: the OS six-inch sheet index (sheet polygons) + each sheet's publication/survey date.**
  SOLVED — NLS GeoServer WFS `nls:OS_6inch_all_find` (EDITION='2' = GB1900, 16,450 sheets) gives sheet-line
  polygons + `PUB_STA`/`SUR_STA`/`REV_STA` per sheet in bulk GeoJSON. Use it to assign every sheet to a
  regime (<1879 / ≥1879) AND to batch by sheet.
- Calibrate font size relative to the Characteristic Sheets. Each sheet may have been scanned/rendered at a
  different resolution, so derive a distinct multiplier per sheet and add a TRUE-SIZE column to the table.
  - Cleaner alternative worth trying: at z17 the metres-per-pixel is known, and OS font sizes are specified
    on the 6-inch paper map — so true glyph size in mm is computable from tile geometry directly, without
    cross-referencing each Characteristic-Sheet scan's resolution.
  - Whichever route, anchor the calibration on a **known-fixed-size category measured on the real OS sheets**
    (the Characteristic-Sheet examples aren't on the sheets we measure).

# Train Font Classifiers

- **Two classifiers, split at 1879** (the sheet's own footnote: the †-marked characters appear only on
  six-inch maps published BEFORE 1879). Both regimes are documented on the 1897 sheet (the †-marked entries
  give the pre-1879 letterform; the current entry gives the ≥1879 one); the 1923 sheet supplements ≥1879.
- **Build the ≥1879 classifier FIRST** — it covers ~99.7% of GB1900 (EDITION 2: 99.7% published ≥1879,
  95.6% ≥1897; only 0.3% pre-1879 — per the NLS sheet-index WFS `nls:OS_6inch_all_find`, which gives
  per-sheet publication dates to assign every pin to a regime).
- **Then adapt/retrain the <1879 classifier FROM the ≥1879 one** (fine-tune / transfer, not from scratch):
  the pre-1879 set is small and would be data-starved on its own, but only the †-marked letterforms differ,
  so start from the ≥1879 weights and adjust for those categories. Do NOT smudge the 1879 change.
- For each regime, pick sample sheets representing (as far as possible) disparate areas — urban, rural,
  coastal, mountainous, etc. — for training. If classification proves weak or a font is under-represented,
  increase the sample size. Use the WFS publication dates to sample ≥1879 sheets across the 1879→1923
  convention range so the classifier is robust to in-regime evolution.
- Scan the GB1900 crowd-text corpus to identify likely examples of each font type. Use MapReader to snapshot
  them (clean, localized boxes — NOT crowd-point windows, which was the failure), and run basic tests to
  verify they're consistent within type (we may need to try different words from the corpus). Add the
  example images to the font table and present them to a human for review.
  - **Verify MapReader localization quality early**, on a sample: boxes must tightly bound the glyphs. The
    hard cases are multi-word labels and **curved / rotated water labels** — a horizontal CRNN handles
    curved labels poorly. Flag these before scaling.
  - Seed/anchor every style on the Characteristic Sheet's own exemplars; crowd-harvested examples only
    *expand* an anchored, human-verified set (raw auto-labels carry noise — that bit us before).

- **Guardrails (the specific fixes for the validated failure — do NOT skip):**
  - Include a well-represented **ORDINARY / none-of-the-above class + a reject option**, so the model is not
    forced to pick a rare style. The ordinary case is the overwhelming majority and must dominate training.
  - **Train and threshold at REALISTIC base rates, not balanced.** A balanced-trained model over-fires the
    rare styles at true frequencies (antiquity/admin ~1.5%).
  - **Go/no-go before any corpus run = per-style PRECISION on a held-out, base-rate-realistic, human-labelled
    set** — not balanced accuracy. (Last attempt scored 0.83 balanced and still failed in production.)

# Main Corpus Run

- Iterate over OS Sheets, not pins: load a sheet and its style-by-date-regime parameters, iterate over the
  pins that belong to that sheet. (Labels never spill across sheets — only across tiles within a sheet, which
  the crop window already handles — so no cross-sheet edge handling is needed.)
- Font output is a STYLE per label; the fusion step (style × text × size × date-regime) produces the
  feature-type and merges — additively, never overriding a confident text type — into the top-3 distribution.
