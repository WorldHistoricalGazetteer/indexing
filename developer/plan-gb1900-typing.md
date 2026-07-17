# Plan — GB1900 place typing (map-typography → AAT)

> **Status:** Design / research. Nothing built. This document is the concrete,
> staged plan requested off `plan-outstanding-2026-07.md` §2 and
> `developer/aat-typing-status.md`.
> **Author aid:** Claude (research pass, 2026-07-17).
> **Scope:** derive a coarse **place type** for each `gb:` record so they can be
> AAT-mapped like every other authority. GB1900 is the **only WHG source with 0%
> AAT coverage.**
>
> **SOURCE DECISION (SG, 2026-07-17): use the CC0 "Final Raw Dump"** (not the
> CC-BY-SA gazetteers) — CC0 avoids share-alike contaminating WHG's whole
> redistribution surface. Working source =
> `gb1900_locations.csv` (**2,666,341 pins**) inside
> `GB1900_final_raw_dump_july_2018.zip`. Trade-offs vs the curated gazetteer: it
> carries only `first_transcription` (the *unreconciled* initial text, not the
> majority-agreed `final_text`) and EWKB-hex coords — both handled (§2.4). We forgo
> the curated set's ~30k manual corrections, but **make our own via VLM readings**
> (§4.2/§4.3) and **record the VLM/OCR-detected bounding boxes** as durable data
> (§4.3, §10) since the source records none.
>
> **ULTIMATE AIM (SG, 2026-07-17): publish WHG's own place-typed EDITION** — a
> standalone, openly-published derived dataset in which **every edit is recorded and
> traceable**, **versioned**, and **re-runnable as user feedback accrues**. Built on
> the **CC0** raw dump, so WHG may license/publish it freely — but it **must NOT be
> named "GB1900" / "Great Britain 1900"**; it needs its own WHG name. Provenance,
> versioning, feedback loop, naming, and edition mechanics: **§11**.
>
> **BUILT SO FAR (2026-07-17, autonomous):**
> - **P0.5 DONE** — OS lettering scheme transcribed from OS 404 →
>   `typesystem/data/gb1900_os_lettering.json`.
> - **P0 DONE (Tier-0 text typing)** — `processing/gb1900_text_types.py` +
>   `typesystem/data/gb1900_os_abbrev.json`; run on the full **2.67M** pins:
>   **1,614,003 typed (60.5%)** zero-GPU, 1.5% illegible dropped, 4.5% ALLCAPS +
>   33.5% residual routed to Tier-1. Residual is dominated by proper settlement
>   names (Welsh/Gaelic) — exactly Tier-1's target.

---

## 1. Summary / goal / success criteria

**Goal.** Give every GB1900 label a `types[].identifier` drawn from a small
controlled vocabulary of OS feature kinds (e.g. `church`, `public-house`, `farm`,
`well`, `railway-station`, `parish-boundary`, `city`, `village`, `hill`, `river`,
`wood`…), then map that vocabulary to Getty AAT — reusing the **exact same
enrichment path every other small authority already uses**:

- Add a `"gb": { <token>: [<aat_id>, …], … }` block to
  `processing/manual_aat_maps.py` (`MANUAL_AAT_MAPS`).
- Re-index the `types[]` field on the live `gb:` docs (per-record token).
- Run `python -m processing.apply_aat_enrich --namespace gb --es-host … --execute`.
  `aat_enrich.augment_doc` reads `MANUAL_AAT_MAPS["gb"][identifier]`, injects
  `aat_ids`, and the AAT-hierarchy path-fill attaches `aat_paths`
  (`processing/aat_enrich.py:130-231`). No authority-script change is needed for
  the AAT step — the same table drives future ingestion too.

**What "typed" means here:** a record carries at least one AAT id resolvable in
the prod `types` index, so it becomes filterable/facetable by type
(`aat-typing-status.md` §"Why this matters").

**Success criteria (tiered — we do NOT need 100%):**
- **Tier 0 (text-only), target ≥ 55–70% of records typed** at a coarse level,
  zero imagery. Cheap, deterministic, shippable on its own.
- **Tier 1+2 (typography), target ≥ 90% typed** after the VLM/clustering pass +
  human cluster→type assignment on the residual.
- Every emitted token maps to a **validated** AAT id (validated against the prod
  `types` index, per the `manual_aat_maps` convention comment).
- Reversible: typing is a metadata overlay on existing docs; a bad batch can be
  re-patched (idempotent, like `wikipedia_sitelinks` was — see
  `developer/handoff-wikipedia-sitelinks.md`).

---

## 2. Data reality (VERIFIED)

### 2.1 The raw source

- **File (on CRC):**
  `/ix1/ishi/data/gb1900/GB1900_gazetteer_abridged_july_2018/GB1900_gazetteer_abridged_july_2018.zip`
  → inner CSV `GB1900_gazetteer_abridged_july_2018/gb1900_abridged.csv`,
  **UTF-16** encoded. This is the *abridged* public release
  (Vision of Britain / NLS Data Foundry, **CC-BY-SA** — *not* CC0; see §2.4
  licensing). Loaded by `authorities/gb1900-places.py` (`stage_gb1900`).

- **Exact column list (verified by opening the zip on `pitt`):**
  ```
  pin_id, final_text, nation, local_authority, parish,
  osgb_east, osgb_north, latitude, longitude, notes
  ```

- **Geometry recorded = a single anchor point only.** There is
  **`osgb_east`/`osgb_north`** (British National Grid, EPSG:27700) **and**
  `latitude`/`longitude` (WGS84) for **one point per label**. **There is NO
  bounding box: no width, no height, no other corner, no text angle, no font
  metadata.** `authorities/gb1900-places.py:76` builds a `Point` from
  `[lon, lat]` and keeps nothing else. → **The "single anchor point, no box
  extent" hypothesis is CONFIRMED by the data.**

- **Which point is the anchor — CONFIRMED and refined.** The GB1900 / NLS
  project documentation states each name was tagged with the coordinates of
  *"the **bottom-left of the first letter of its first word**."* (NLS OS1900 /
  GB1900 gazetteer docs, corroborated by the GB1900 Wikipedia entry). So it is
  **not** the box centroid and **not** even the box corner — it is the
  **baseline-left of the first glyph**. The docstring in
  `authorities/gb1900-places.py:25` ("south-west corner of the text label") is
  *approximately* right but should be corrected to "bottom-left of the first
  letter."

  **Implication for cropping (Tier 1):** we know where the label *starts*
  (baseline-left) and the reading direction is *rightward and slightly up/down*,
  but we know neither the text length in map units nor the font size nor the
  rotation. The crop window must be **estimated and over-sized**, then the label
  re-detected inside it (see §4.2).

### 2.2 Text signal already in hand

- **`final_text`** is the transcribed label string — our richest free signal.
- **ALLCAPS fraction ≈ 24.9%** (measured over a 200k-row sample of `final_text`
  on `pitt`). **ALLCAPS is NOT a standalone type prior.** On OS County Series maps
  ALLCAPS is a *case* that appears across many feature classes, and the actual
  type is carried by the **font family, size band, and letter-spacing** the caps
  are set in — e.g. large upright roman caps = town/city; small caps =
  village/parish; **wide-spaced antique/italic caps = seas, mountain ranges,
  regions, and antiquities**; boundary/administrative labels in yet another face.
  So ALLCAPS is best treated as a **routing signal into the typography pass**
  (§4.2), decisive on its own only when the text content also tells us the type.
- Abbreviations are pervasive and follow the **OS abbreviations convention**
  (sample rows: `"Parly. & Munl Boro. By."`, `"F.P."`, `"P.H."`, `"Ch."`,
  `"Sch."`, `"Sta."`, `"Well"`, `"Fm."`, `"Ho."`). These are near-deterministic
  type tells (see §4.1).
- `nation` ∈ {England, Scotland, Wales} → drives `ccodes=['GB']` today; also
  tells us which NLS coverage/edition applies.
- `parish` / `local_authority` are modern admin context (useful for sheet/era
  disambiguation, not typing).

### 2.3 Era

Survey period **1888–1914** (2nd-edition County Series 1:10,560 "six-inch"),
hard-coded in `authorities/gb1900-places.py:70`. This **matches the NLS "OS
Six-inch 2nd edition, 1888–1915" seamless layer** (see §5.1) — a clean era match,
which is the key to fetching the *right* map raster.

### 2.4 Abridged vs COMPLETE source — the abridgement dropped ~1.38M **rows**, not columns

WHG currently ingests the **abridged** GB1900 release
(`GB1900_gazetteer_abridged_july_2018.zip`, **1,174,450 rows**;
`processing/settings.py:536,549`). A **complete** release exists
(**2,552,460 rows**). **The two share the same columns** — the abridgement removed
**~1,378,010 rows**, being the commonest repeating feature labels:

| Removed label | Meaning | Count (complete) |
|---|---|---|
| `F.P.` | footpath | 306,583 |
| `W` | well | 190,979 |
| `P` | pump | 115,877 |
| … | other common OS-abbreviation features | remainder to ~1.38M |

**Why this matters *specifically for typing*:** the dropped rows are the ones Tier 0
types with **near-100% confidence** — they *are* pure OS abbreviations
(`F.P.`→footpath, `W`→well, `P`→pump). The abridgement threw away exactly the
low-hanging fruit for a *feature*-typing project (it was curated for a *place-name*
gazetteer, where a footpath crossing / well / pump isn't a "place" — a deliberate,
defensible choice for that goal, `settings.py:536` verified 2026-06-06).

**Consequences / options (a scope decision for SG):**
- **Fetch the complete set regardless.** Even if not all of it is ingested as
  searchable places, it is a **free, massive, self-labelling ground-truth** for
  Tier 0 (every `F.P.` is a labelled footpath, every `W` a well) and — with map
  crops — for validating the Tier 1 VLM typography classifier against known types.
  High value, ~zero cost, no product commitment.
- **Whether to *ingest* the extra ~1.38M as searchable places is a product call.**
  Options: (a) ingest all, with types, and **default-exclude** the generic-feature
  classes (footpath/well/pump) from search so they don't swamp GB results; (b)
  ingest them as a **separate feature layer / down-ranked**; (c) keep the abridged
  set for the gazetteer and use the complete set **only** as typing ground-truth.
- Ingestion cost is low: **same schema**, so it's a source-URL swap in
  `settings.py` + `gb1900-places.py` + a re-stage; the type work is *cheaper* per
  record than the abridged set (more of it is trivial-text-typeable).
- **Complete download URL — CONFIRMED (downloaded + inspected 2026-07-17):**
  `https://www.visionofbritain.org.uk/downloads/GB1900_gazetteer_complete_july_2018.zip`
  (140 MB zip → **703 MB** UTF-16 CSV `gb1900_gazetteer_complete_july_2018.csv`).
  Its README states **2,552,459 rows** and lists the **identical 10 columns**
  (`pin_ID, final_text, nation, local_authority, parish, osgb_east, osgb_north,
  latitude, longitude, notes`) — so the abridgement is purely row removal, as
  suspected. *(`pastplace.org` was unreachable this session; use the
  visionofbritain.org.uk host. From the CRC boxes both hosts returned HTTP 000 —
  the fetch must run from a networked host and be staged onto `/vast`/`/ix1`.)*
- **Bonus quality gain:** the complete set had **~30,000 points (c. 1.5%)
  manually checked & corrected against the historical maps** after the crowd-source
  phase — so overlapping records are also *cleaner*, not just more numerous.
- **A third release exists — the CC0 "Final Raw Dump"** (four tables, all raw
  crowd-sourcing data incl. every transcription/confirmation, minus volunteer PII).
  `.../downloads/GB1900_final_raw_dump_july_2018.zip`. **CC0** (no attribution/
  share-alike). Useful if per-transcription confidence/vote data ever helps
  (ambiguity flags), but messier than the reconciled Complete gazetteer.

### 2.5 Licensing & attribution (NOTE — surface when redistributing)

Per SG (2026-07-17), record and surface the GB1900 / Vision of Britain copyright
terms whenever relevant (esp. before any redistribution). From
`visionofbritain.org.uk/data/`:

- **Complete AND Abridged gazetteers → CC-BY-SA.** Commercial use allowed, but you
  **must acknowledge "the Great Britain Historical GIS, the GB1900 partners and
  volunteers"**, **must not imply endorsement** by the GB1900 project/partners,
  **must link the licence and note if changes were made**, and — **share-alike** —
  may **only redistribute under the same CC-BY-SA licence, without additional
  restrictions.** You **may not** call any derived work "GB1900 Gazetteer" (or
  similar); only *unmodified* files may carry that name.
- **Raw Dump → CC0 1.0** (no acknowledgement required; same name restriction).
- **Implication for WHG:** the SA clause reaches WHG's redistribution surface
  (API/tiles). This does **not** gate ingestion (cf. `feedback_defer_licensing`;
  sitewide attribution is being rebuilt separately) — but the attribution +
  share-alike obligation must be captured in that attribution system, and the CC0
  raw dump is the SA-free alternative if share-alike proves awkward. See
  memory `gb1900_visionofbritain_licensing`.

---

## 3. Approach — a three-tier pipeline

The insight: **most records can be typed from text alone; imagery is only for the
residual.** Do the cheap deterministic thing first, measure the gap, then spend
GPU only where it pays.

```
Tier 0  text-only heuristics (OS-abbreviation dict + keyword gazetteer)     → majority
   │        (deterministic, no imagery, no GPU; ALLCAPS is only a routing
   │         flag into Tier 1 — never a type on its own, see §4.1.2)
   ▼
Tier 1  typography signature via NLS map raster + VLM/CV, then CLUSTER      → residual
   │        (GPU Slurm; per-label crop → font-style descriptor → embedding)
   ▼
Tier 2  HUMAN assigns each font/typography cluster → one type token (once)  → propagate
            (a few hundred clusters, reviewed in a small notebook/UI)
```

All three tiers converge on the **same output**: a per-record type **token** from
one controlled vocabulary → `manual_aat_maps["gb"]` → AAT.

---

## 4. Tier detail

### 4.1 Tier 0 — text-only typing (do this first; ship independently)

Pure-Python, deterministic, runs on any host (no GPU, no imagery). Three signals,
highest-confidence first:

1. **OS-abbreviation dictionary.** Build `typesystem/data/gb1900_os_abbrev.json`
   mapping OS County Series abbreviations → type token. These are standard and
   published (NLS "OS map abbreviations" guide; Charles Close Society sheetlines).
   Examples:
   | abbrev / token in `final_text` | type token | AAT concept (to validate) |
   |---|---|---|
   | `Ch.`, `Chy.` | `church` | 300007466 churches |
   | `P.H.` | `public-house` | 300005141 public houses / inns |
   | `Sch.`, `Schl.` | `school` | 300005526 schools |
   | `P.O.` | `post-office` | 300005982 post offices |
   | `Sta.`, `Ry. Sta.` | `railway-station` | 300005815 railroad stations |
   | `F.P.`, `F.B.` | `footpath` / `footbridge` | 300055977 / 300007836 bridges |
   | `Well`, `Spr.` | `well` / `spring` | 300006860 / 300008698 |
   | `Fm.`, `Farm` | `farm` | 300000206 farms |
   | `Ho.`, `Hall` | `house` / `hall` | 300005425 houses |
   | `Sml.`, `Mill` | `mill` | 300004396 mills |
   | `Quy.`, `Bdy`, `Boro. By.` | `quarry` / `boundary` | 300000275 / 300387473 |
   | `Inn`, `Hotel` | `inn` | 300005141 |
   | `Br.`, `Bri.` | `bridge` | 300007836 bridges |
   Longest-match / word-boundary matching; a small ordered rule list, not ML.

2. **ALLCAPS — a router, not a type.** ≈25% of rows are all-caps, but caps alone
   is **ambiguous**: OS uses it for towns, villages, parishes, boundaries, seas,
   ranges, regions and antiquities, distinguished only by **font family / size /
   letter-spacing** (see §2.2, §4.2). So Tier 0 must **not** collapse ALLCAPS to a
   single "prominent place" token. Handle it in three ways, in order:
   - **Decisive text content wins:** an ALLCAPS label whose words are a type tell
     still types from §4.1.1/§4.1.3 (e.g. `... PARISH`, `... BORO`, `... DIVISION`,
     `CO. ...` → administrative; a caps river/range name → physical). Case doesn't
     override an explicit textual tell — it just co-occurs.
   - **Otherwise defer:** an ALLCAPS label with no textual tell is **routed to the
     Tier 1 typography pass** (family + size band + tracking resolve it), NOT
     assigned a coarse type here. Emit it as `residual` with an `allcaps=true`
     feature so Tier 1 prioritises it.
   - Record the ALLCAPS flag as a **feature**, never as a final answer, so the
     downstream clustering can combine it with the typographic descriptor.

3. **Gazetteer / keyword heuristics.** Suffix/keyword table on the descriptive
   words that OS uses in full: `Wood`, `Plantation`, `Common`, `Moor`, `Hill`,
   `Down`, `Fell`, `Point`, `Head`, `Bay`, `River`, `Brook`, `Burn`, `Lough`,
   `Reservoir`, `Colliery`, `Works`, `Quarry`, `Cottage`, `Cottages`, `Bridge`,
   `Wharf`, `Pier`, `Chapel`, `Cemetery`, `Castle` (antiquity), `Tumulus`,
   `Camp`, `Fort` (antiquities). Each → a token.

**Deliverable of Tier 0:** a script `processing/gb1900_text_types.py` that reads
`final_text` and emits `(place_id, token, confidence, rule)`; a coverage report
(what % typed, histogram over tokens, residual list). **This alone likely types
the majority** — the biggest single win, and it de-risks the whole project before
any GPU spend.

**Also cross-check against GOTW's `build_aat_shortlist.py` / `aat_resolve.py`**
(`/home/stephen/PycharmProjects/GOTW/process/`) — they already do string→AAT
shortlisting and may donate an abbreviation/label→AAT seed list.

### 4.2 Tier 1 — typography signature from the map raster

For records Tier 0 can't confidently type (and as a *cross-check* on a sample of
those it can): read the label off the georeferenced OS raster and characterize its
**typography**, because OS rendered feature classes in distinct, **documented**
type styles.

#### 4.2.0 This is grounded in the OS lettering specification — NOT guesswork

The core premise (type style ⇒ feature class) is **not** an assumption; OS
published formal lettering specifications and the style→feature scheme is
documented in primary sources (verified 2026-07-17):

- **"Character of Writing for Ordnance Survey Plans" (OS 404), 1881 & 1914
  editions** — the OS's *dedicated* lettering spec (digitised at NLS). The 1914
  edition is contemporaneous with GB1900's survey window.
- **"Conventional Signs and Writing Used on the Six Inch Maps of the Ordnance
  Survey" (Plate IV)**, in *A description of the large scale maps of Great Britain*
  (1920) — the six-inch writing plate (NLS `maps.nls.uk/view/128076894`).
- **"Notes on Archaeology for Guidance in the Field" (1921, O.G.S. Crawford)** —
  antiquities are lettered by font **by period**: pre-Roman, Roman, post-Roman
  (Saxon/medieval) each get a distinct style.
- **"Notes on Boundaries" (1914)** — boundary/mereing labelling.
- **Richard Oliver, "A few notes on map lettering", *Sheetlines* 95, pp. 33-36**
  (Charles Close Society; PDF read 2026-07-17) — secondary synthesis, with the
  OS's own style **names** (below), citing *Ordnance Survey alphabets* (OS, 1934)
  + internal type-specimen manuals.

**What the source actually says — TRANSCRIBED FROM OS 404 (June 1914), the
per-feature writing table (read 2026-07-17).** OS 404 lists the writing character
for *every* feature class, in two scale columns; **for GB1900 we read the
"1/10560 and 6-inch scales" column.** Its style abbreviations:

| Abbrev | Style | | Abbrev | Style |
|---|---|---|---|---|
| **R.P.** | Roman Print (serif, mixed-case) | | **E.C.** | Egyptian Capitals (slab sans) |
| **R.C.** | Roman Capitals | | **O.E.C.** | Open Egyptian Capitals |
| **I.C.** | Italic Capitals | | **O.R.C./O.I.C.** | Open Roman/Italic Capitals |
| **Stump** | the standard stamped hand (the *default*) | | **Old English / German Text** | black-letter (antiquities) |
| **Ornamental** | decorative (counties, county boroughs) | | | |

**Documented six-inch feature → style (selected, from OS 404 pp. 9–11):**

| Style (6-inch column) | Feature classes OS assigns it |
|---|---|
| **R.C.** (Roman Capitals) | cathedrals, county boroughs, barracks (large), forts, forests, cattle markets, cemeteries, colleges, courts of law (principal), harbours, havens, headlands (large), **hill ranges**, hospitals, hotels, large bays/beaches |
| **R.P.** (Roman Print) | churches, chapels, town halls, dispensaries, drill halls, dock buildings |
| **I.C.** (Italic Capitals) | **canals, canal basins, docks, "cuts" in navigable rivers, public gardens, deer parks** — i.e. **water & designed-water features** |
| **E.C.** (Egyptian Capitals) | **Roman** antiquities; parliamentary county divisions; courts & alleys |
| **Old English** | **pre-historic / Saxon** antiquities (+ ancient almshouses) |
| **German Text** | **Norman or subsequent (medieval)** antiquities |
| **Ornamental** | counties, county boroughs |
| **Stump** (default) | farms, brooks, fords, ferries, foot bridges/paths, single dwelling houses, collieries, coal pits, filter beds, cattle pens, caves, drying grounds, guide posts, recreation grounds, grave yards … (the **bulk** of minor features) |

Antiquities are thus lettered **by period** (Roman = E.C.; pre-historic/Saxon =
Old English; Norman+ = German Text), confirming the antiquity-font premise
precisely. **Process note (dating):** from **1882** the six-inch was
photo-lithographed with **stamped** lettering, standardised across the map by the
**1890s** — so GB1900's 1888–1914 sheets use a *consistent, stamped* style set,
which is what makes cluster-by-typography viable.

**⚠ CRITICAL CAVEAT — the six-inch differentiates FEWER classes than 1/2500.**
Note how many feature classes fall to **"Stump"** in the 6-inch column that carry
a *distinct* style at 1/2500 (e.g. banks: 1/2500 R.P. → 6-inch **Stump**; churches
stay R.P. but farms/ferries/fords are Stump at both). **So on the six-inch,
typography reliably separates only a *subset* of types:** prominent
settlements/administrative units (R.C. / Ornamental / caps size-hierarchy), **water
features (I.C.)**, and **antiquities by period (E.C. / Old English / German Text)** —
while the large mass of minor rural features is **undifferentiated Stump** and
**cannot** be told apart by font. Those must be typed from **text** (Tier 0
abbreviations), which is exactly Tier 0's strength. **This is the key finding: Tier
0 (text) and Tier 1 (typography) are complementary, not redundant — typography adds
water/antiquity/prominence that text often can't, and text resolves the Stump mass
that typography can't.**

**Consequence for the plan:** the scheme above is transcribed from the primary
source (P0.5 substantially DONE), so the VLM label set = the OS style names
(R.P./R.C./I.C./E.C./Old-English/German-Text/Stump/Ornamental) and clusters grade
directly against documented feature classes. What remains of P0.5 is transcribing
the full ~300-row OS 404 table into `typesystem/data/gb1900_os_lettering.json`
(feature → 6-inch style) so Tier 1's cluster→type step is a table lookup.

**Note the CAPS rows:** ALLCAPS spans at least five of these classes — the
discriminator is **family (roman vs antique/italic) + size band + letter-spacing**,
which is exactly why ALLCAPS must be resolved *here*, not pre-judged in Tier 0.

**Per-label steps:**
1. **lat/lon → tile + pixel.** Standard slippy-map math (Web Mercator, EPSG:3857):
   `n = 2^z; xtile = (lon+180)/360·n; ytile = (1 - ln(tan φ + sec φ)/π)/2·n`;
   pixel offset = fractional part × 256. Reuse a tiny `deg2num` helper (no such
   helper exists in `processing/helpers.py` today — add one; `h3` is imported
   there but that's a different grid). Choose **z ≈ 16–17** on the NLS six-inch
   layer so glyphs are legible.
2. **Crop an over-sized window** anchored at the label's baseline-left pixel,
   extending **rightward** (reading direction) and a generous margin up/down —
   sized from `len(final_text) × assumed_glyph_width` with a comfortable safety
   factor (we have no true extent, so **over-crop deliberately**). Fetch the 1–4
   covering tiles, stitch, crop.
3. **Re-detect the actual text inside the crop** (so we're not characterizing
   whitespace): a light OCR/text-detector (e.g. Surya — already in the `whg`
   conda env per GOTW `run_pipeline.sh`, or PaddleOCR) gives the tight glyph
   bbox + angle. This *also* yields a confidence that the transcribed
   `final_text` is what's actually there (era/sheet-mismatch guard).
4. **Characterize typography.** Two interchangeable back-ends:
   - **VLM (primary):** send the tight crop to a self-hosted Qwen2.5-VL with a
     strict JSON schema whose **primary field is the documented OS style code**
     (§4.2.0) — `{os_style: RP|RC|IC|EC|OldEnglish|GermanText|Stump|Ornamental,
     case: lower|title|caps|smallcaps, size_band: small|medium|large|extra_large,
     tracking: tight|normal|wide, legible: bool}`. `os_style` maps *directly* to a
     feature class via the OS 404 table (RC/Ornamental→prominent settlement/admin,
     IC→water, EC/OldEnglish/GermanText→antiquity by period, RP→named building),
     so clusters resolve to types by table lookup, not hope. `case`/`size_band`/
     `tracking` corroborate `os_style` and split the size hierarchy *within* caps
     (large RC = town/city vs small = village). **Because ~half of six-inch classes
     are `Stump` (§4.2.0 caveat), a `Stump` verdict means "typography can't tell —
     defer to Tier 0 text"**, not a type; the VLM's value is concentrated on the
     RC/IC/EC/OldEnglish/GermanText minority it *can* separate.
     **`case`, `size_band` and `tracking` are separate fields precisely so an
     ALLCAPS label is decomposed** (caps + family + size + spacing jointly pick the
     type) rather than flattened to one label. `tracking` (letter-spacing) is the
     tell for extended-area features — OS sets seas/ranges/regions in **wide-spaced
     caps**. This mirrors the GOTW schema'd-JSON pattern (§6), with fields chosen
     for OS typography.
   - **Classical CV (cheap alt / ensemble):** slant angle (skew), stroke
     contrast, x-height, cap-ratio, serif detection → a fixed-length feature
     vector. Much cheaper than a VLM at 1.2M scale; can pre-filter so the VLM
     only sees ambiguous crops.
5. **Embed & cluster.** Turn each label's typographic descriptor (VLM fields
   one-hot + CV features, or a small image-encoder embedding of the crop) into a
   vector; **cluster** (HDBSCAN / k-means) into a few hundred groups. The claim
   is that *type style is nearly discrete* on OS maps, so clusters should be
   tight and few.

### 4.3 Tier 2 — human cluster → type assignment, then propagate

- Surface each cluster to a human as **a contact sheet of ~25 example crops +
  their `final_text` values + the modal VLM descriptor**.
- Human picks **one type token** for the cluster (or "mixed → split / send back").
- Propagate the token to **every** record in the cluster. This is the whole
  point: **one human decision types thousands of records.**
- Realistically **a few hundred clusters** → a day or two of review, not 1.2M
  decisions.
- Map each chosen token → AAT id(s) in `manual_aat_maps["gb"]` (validate each id
  against the prod `types` index first).

**Review surface:** a static HTML contact-sheet generator (mirrors GOTW's
`process/review_ui.py` / `export_reader.py` approach — GOTW already builds static
review pages), or a Jupyter notebook. No live service needed.

---

## 5. NLS map tiles (research findings)

### 5.1 The right layer (era match)

- **Use: OS Six-inch to the mile, 2nd edition, 1888–1915** (1:10,560), seamless,
  GB-wide (England & Wales page: `maps.nls.uk/os/6inch-england-and-wales/`;
  Scotland has its own six-inch coverage). This is *the* GB1900 source era.
- MapTiler exposes it as layer id **`uk-osgb10k1888`** (and a seamless "~1900"
  composite `uk-osgb1888`). Projection **EPSG:3857 Web Mercator**, 256px tiles.
- The more legible **25-inch (1:2,500)** exists but is **not seamless GB-wide**
  (NLS: "not published for all areas" / not offered as a seamless MapTiler layer)
  → use it only opportunistically where available; six-inch is the workhorse.

### 5.2 Tile URL scheme

- **MapTiler (verified template):**
  `https://api.maptiler.com/tiles/uk-osgb10k1888/{z}/{x}/{y}.jpg?key=YOUR_KEY`
  (requires a MapTiler key; Web Mercator; JPG).
- **NLS direct (to confirm from the georeferenced viewer's "XYZ" box):** NLS
  serves georeferenced seamless layers as XYZ/TMS from S3
  (`https://mapseries-tilesets.s3.amazonaws.com/<layer>/{z}/{x}/{y}.png`, and
  older `nls-N.tileserver.com` hosts). **Action for the pilot:** open the
  1888–1915 six-inch seamless layer in `maps.nls.uk/geo/explore/`, click **XYZ**,
  and record the exact template + max zoom. (Prefer the direct NLS XYZ over
  MapTiler for bulk if terms allow — one fewer dependency/key.)
- **Max zoom:** NLS six-inch seamless typically maxes around z16; the 25-inch
  around z18. Confirm per layer at pilot time — typography legibility drives the
  zoom choice.

### 5.3 Licensing & rate constraints (IMPORTANT)

- **Non-commercial / research reuse is permitted with attribution**: *"you must
  display an attribution to the National Library of Scotland, together with a
  link to our website."* WHG is NEH-funded, non-commercial → fits.
- **The six-inch and 25-inch layers are "restricted for commercial purposes"**
  (third-party digitisation contracts). **We must not redistribute the tiles** —
  we fetch, run inference, and use the imagery for our own research processing
  only, not re-serve it. Per the project convention this is a *processing* input,
  not a published asset (cf. `feedback_defer_licensing` — don't gate on licence,
  but **do** respect no-redistribution).
- **Cache policy (SG, 2026-07-17): keep EVERY fetched tile permanently on `/vast`**
  — not just the transient per-label crops. Once a tile is fetched it is retained
  in a durable tile cache (`${IX3_BASE}/gb1900/tiles/`, i.e. `/vast/ishi/gb1900/…`)
  for **potential future research re-use**, so no NLS tile is ever fetched twice
  across this or any later project. This is an **internal research cache**, fully
  compatible with the no-redistribution term (the tiles are never re-served, only
  used for WHG research). Crops derived from tiles are cached alongside. Practical
  upside: the second pass (25-inch opportunistic reads, re-runs, other analyses)
  costs zero NLS traffic.
- **Rate limits / bulk fetch:** no published hard rate limit, but bulk-scraping
  ~1.2M label crops = many tile GETs. **Mitigations:** (a) Tier 0 first slashes
  how many labels need imagery; (b) **tile-level caching** — many labels share a
  tile (six-inch, z16), so dedupe requests to the covering-tile set and cache to
  `/vast`; (c) polite concurrency + backoff; (d) contact NLS for a bulk
  arrangement or, ideally, **fetch tiles once to a local cache** for the counties
  in scope. **Do NOT hammer from many Slurm array tasks in parallel** — stage the
  tile cache first (network fetch), then run GPU inference against the local
  cache (GPU nodes shouldn't be the ones fetching from NLS anyway).

---

## 6. Compute plan (adapting the GOTW VLM pattern)

The GOTW clone at `/home/stephen/PycharmProjects/GOTW` has a **working,
battle-tested CRC VLM pattern** we should copy rather than reinvent.

### 6.1 The reusable GOTW pattern (cited)

- **Model:** `Qwen/Qwen2.5-VL-72B-Instruct-AWQ`, served by **vLLM**
  (`process/submit_vlm_slurm.py:17`, `process/run_pipeline.sh` `stage_vlm`).
- **Serving pattern (`submit_vlm_slurm.py`):** a **GPU Slurm array**; each array
  task starts its **own** `vllm serve` on a unique port
  (`PORT=$((18900 + JOB%700 + T))`), waits for `"Application startup complete"`,
  then runs inference over its **image-index shard** → a **per-shard JSONL**
  (resumable — re-runs skip done work). Header:
  ```
  #SBATCH -M gpu  -p h200  --gres=gpu:1  --cpus-per-task=8  --mem=80G
  #SBATCH --time=04:00:00  --requeue  --array=0-<n_shards-1>
  source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
  conda activate /vast/ishi/envs/vllm      # vLLM env (NOT the whg env)
  module load cuda/12.8.0
  export HF_HOME=/vast/ishi/hf_cache
  ```
- **Inference call (`triage_pages.py`):** OpenAI-compatible
  `POST {VL_BASE}/chat/completions` with a **strict JSON schema**
  (`response_format: json_schema, strict:true` from a pydantic model),
  `temperature:0`, small `max_tokens`, image as base64 `data:image/jpeg` URL,
  **`ThreadPoolExecutor(concurrency≈32)`** (vLLM batches in-flight requests),
  low-res thumbnails (`maxpx≈1024`) for speed, plus a **regex `_salvage()`** for
  Qwen's truncated/whitespace-loop outputs. DB/JSONL is the **resume state**.
- **Orchestration (`run_pipeline.sh`):** submit-and-poll from a login node/tmux
  (`sacct` polling only — no heavy compute on login nodes); each stage is its own
  Slurm job.

### 6.2 Adaptation to THIS repo's conventions

- **GPU submission.** GOTW uses `-M gpu -p h200`. This repo's memory notes say
  htc/a100 needs `sbatch -M htc --account=ishi` and GPU work goes to a100/gpu —
  **use `--account=ishi`** and pick the partition that's actually schedulable
  (`gpu`/`h200` or `htc`/a100). **Never run inference on a CRC login node**
  (`feedback_no_jobs_on_login_nodes`); submit-and-poll only, from `crc0`
  (fall back `crc1/2/3`). Activate conda **before** any `set -u`
  (`reference_crc_slurm_htc_submit`).
- **Paths.** Repo root `/vast/ishi/elastic`; put the tile cache, crops, VLM env,
  and HF cache on **`/vast`** (small-file I/O — the whole reason `/ix1` is
  avoided; see CLAUDE.md). Reuse `/vast/ishi/envs/vllm` and `/vast/ishi/hf_cache`
  that GOTW already provisioned.
- **New scripts (mirror GOTW names):**
  - `processing/gb1900_text_types.py` — Tier 0 (CPU, any host).
  - `processing/gb1900_fetch_tiles.py` — **network** tile fetch → `/vast` cache
    (run on `pitt`/CRC login is OK for *network-bound* fetch per
    `feedback_long_running_hosts`, but throttle for NLS; better as an `htc` CPU
    job). Dedupe to covering-tile set.
  - `processing/gb1900_make_crops.py` — lat/lon→tile/pixel, stitch, over-crop,
    OCR-refine → per-label crop PNG on `/vast` (CPU array; `whg` env has Surya/PIL).
  - `processing/submit_gb1900_vlm_slurm.py` — **copy `submit_vlm_slurm.py`**;
    swap the schema/prompt to the *typography descriptor*; input = crop shards →
    per-shard JSONL.
  - `processing/gb1900_cluster_types.py` — embed descriptors + HDBSCAN → clusters
    + contact sheets.
  - `processing/gb1900_apply_types.py` — write per-record `types[].identifier`
    to the live `gb:` docs (idempotent update-by-query patch, following
    `wikipedia_links_patch.py` / `apply_links_patch.py` — see
    `handoff-wikipedia-sitelinks.md`), then hand off to `apply_aat_enrich`.

### 6.3 Rough scale & cost (order-of-magnitude)

- **1.17M labels.** After Tier 0 (say ~60% typed) → **~470k need imagery.**
- **Tiles:** at six-inch z16, one 256px tile covers a sizeable ground area;
  labels cluster densely per sheet → the covering-tile set is **far** smaller
  than the label count (plausibly low hundreds of thousands of unique tiles,
  many shared). Tile fetch is the network bottleneck, not the GPU.
- **VLM:** GOTW ran Qwen2.5-VL-72B-AWQ on single-GPU array tasks at ~hundreds of
  images/task/hour with concurrency 32. ~470k crops across, say, 8–16 concurrent
  GPU array tasks is on the order of **a few GPU-days** — tractable, and the
  classical-CV pre-filter (§4.2) can cut the VLM share sharply.
- **De-risk with a pilot** before committing (see §8).

---

## 7. Human-in-the-loop

- **Only Tier 2 needs a human**, and only **once per cluster** (~hundreds of
  decisions, not millions).
- Surface: static contact-sheet HTML (per cluster: sample crops + `final_text` +
  modal descriptor) generated on CRC, viewed locally — reuse GOTW's static-review
  approach (`process/review_ui.py`, `export_reader.py`). No server/DB service.
- Output of review: a `gb1900_cluster_types.json` mapping `cluster_id → token`,
  and each `token → [aat_id…]` folded into `manual_aat_maps["gb"]`.
- **AAT mapping** is the same curated step every small authority used
  (`manual_aat_maps.py`): validate each id against the prod `types` index, then
  `apply_aat_enrich --namespace gb --execute`.

---

## 8. Phased milestones

| Phase | Deliverable | Gate |
|---|---|---|
| **P0 — Tier 0 ship** | `gb1900_text_types.py` + OS-abbreviation dict + coverage report on the full 1.17M. Fold high-confidence tokens into `manual_aat_maps["gb"]`; patch live `types[]`; `apply_aat_enrich`. | GB moves from **0% → majority** typed with **zero GPU**. Biggest bang; do first. |
| **P0.5 — Typography ground-truth** *(substantially DONE 2026-07-17)* | The OS scheme is transcribed from the primary source into §4.2.0 (OS 404 1914 per-feature table + antiquity-by-period + the six-inch Stump caveat). Remaining: capture the full ~300-row OS 404 "1/10560 & 6-inch" column into `typesystem/data/gb1900_os_lettering.json` as `{feature → os_style}` and its inverse `{os_style → [feature classes]}`. | The VLM label set (`os_style`) + cluster→type lookup are fixed to the **documented** scheme before any GPU spend; Tier 1 isn't reverse-engineering published conventions. |
| **P1 — NLS tile recon** | Confirm the exact 1888–1915 six-inch **XYZ template + max zoom** from the NLS georef viewer; verify licensing note; fetch a **one-county** tile cache to `/vast`. | Tiles fetchable + legible typography at chosen zoom. |
| **P2 — Crop pilot (one county, ~N=2–5k labels)** | `gb1900_make_crops.py` end-to-end on one county: lat/lon→pixel, over-crop, OCR-refine. Manual eyeball of crop accuracy. | Crops reliably contain the right label despite no box extent. |
| **P3 — VLM + cluster pilot (same county)** | `submit_gb1900_vlm_slurm.py` (GOTW copy) → typography descriptors → cluster → contact sheets → human assigns the county's clusters. | Clusters are tight & few; human assignment is fast; typing agrees with Tier 0 where they overlap. |
| **P4 — Scale-out** | Run tile-fetch → crop → VLM → cluster across all counties (residual only). Global human review of clusters. | ≥90% typed. |
| **P5 — Land** | `gb1900_apply_types.py` patches live `gb:` docs; `manual_aat_maps["gb"]` complete; `apply_aat_enrich --namespace gb --execute`; update `aat-typing-status.md` (drop "the only remaining zero"). | GB has AAT coverage; type facets/filter work for GB. |

**Pilot county suggestion:** somewhere with dense, varied features and good NLS
six-inch coverage (e.g. a Welsh or Scottish county — the project's origin data is
richest there) so both settlement and physical-feature type styles are exercised.

---

## 9. Risks / open questions

- **Crop accuracy without box extent (biggest risk).** We only have the
  baseline-left of the first glyph and no length/size/angle. Mitigation:
  deliberate over-crop + OCR re-detection inside the crop. Curved/rotated labels
  (rivers, coastlines) and very long labels are the hard cases; the OCR-refine
  step must handle rotation.
- **Era / sheet matching.** GB1900 pins came from a specific edition/sheet; the
  NLS seamless "1888–1915" layer is a mosaic of sheets of *slightly* different
  revision dates. Usually fine (both are 2nd-edition six-inch), but a label near
  a sheet seam or from a re-revised area could land on the wrong-vintage raster.
  The OCR-vs-`final_text` agreement check flags these.
- **NLS rate limits / licensing.** No hard published limit but bulk fetch is
  heavy and tiles are **non-redistributable** (commercial restriction). Cache
  crops for processing only; consider contacting NLS for a bulk arrangement;
  throttle. Prefer a one-time county tile cache over live fetching in inference
  jobs.
- **VLM reliability on faint historical type.** 1888–1915 engraving is fine and
  sometimes faint/overprinted; low contrast italic vs roman is exactly the
  distinction we lean on. Mitigations: pick a high-enough zoom; ensemble the VLM
  with classical CV slant/serif features; the human cluster review is the safety
  net (a bad-signal cluster gets caught and split/dropped).
- **Multi-line / overlapping / shared labels.** OS maps stack labels; a crop may
  contain a neighbour's text. OCR-refine + "which glyphs start at the anchor
  pixel" disambiguation.
- **Type granularity vs AAT.** Some tokens map cleanly (church→300007466);
  others are coarse. **Do not shortcut ALLCAPS to one broad token** — as noted in
  §2.2/§4.1.2/§4.2, caps resolves to town / village / parish / boundary / sea /
  range / region / antiquity depending on family+size+tracking, so an
  ALLCAPS-without-a-text-tell label is a Tier-1 case, not a coarse `inhabited
  places` guess. Where a residual genuinely can't be resolved beyond "prominent
  place", coarse-but-correct is acceptable (the AAT hierarchy path-fill still makes
  it facetable) — but that is the fallback, not the ALLCAPS rule.
- **Does typography actually separate types cleanly?** No longer an open
  hypothesis in principle — OS *documented* the style→feature scheme (§4.2.0: OS
  404, the six-inch Conventional Signs plate, Notes on Archaeology 1921), and from
  the 1890s the six-inch used a **standardised stamped** style set, so the signal
  is real and consistent for GB1900's era. The residual risks are **legibility**
  (faint/overprinted engraving) and **the VLM/CV reliably recovering** the
  documented style — both empirical, tested at P3. If style recovery proves too
  noisy, fall back to a VLM *content* classifier (what is this thing?) rather than
  a typography classifier — but the ground truth (P0.5) exists to grade against
  either way.

---

## 10. Output contract (how GB typing lands, precisely)

1. Each `gb:` doc's `types[]` becomes
   `[{identifier: <token>, label: "gb1900", sourceLabel: <rule/cluster tag>}]`
   (replacing today's single generic `{identifier:"named-place", …}` at
   `authorities/gb1900-places.py:101`). Update both the **authority script**
   (future ingests) and the **live index** (a `gb1900_apply_types.py` patch,
   idempotent, mirroring the Wikipedia-sitelinks patch flow).
2. `MANUAL_AAT_MAPS["gb"] = { <token>: [<aat_id>, …], … }` added to
   `processing/manual_aat_maps.py` (all ids validated against the prod `types`
   index).
3. `python -m processing.apply_aat_enrich --namespace gb --es-host <URL> --execute`
   injects `aat_ids`/`aat_paths` on the live docs (`processing/aat_enrich.py`).
4. Re-run after any future `gb` rebuild — the same table drives ingestion's
   `aat_enrich` stage automatically.

---

## 11. Published WHG edition — provenance, versioning, feedback & naming

The end goal is not just to enrich the live `gb:` docs, but to **publish a
standalone, openly-licensed, fully-traceable WHG place-typed edition** of the
British ~1900 map labels, re-derivable and improvable over time. Requirements
(SG, 2026-07-17):

### 11.1 Full provenance — every edit recorded & traceable
Each record is an **append-only, provenance-carrying** object. Never overwrite a
source value; layer derivations on top with their evidence:

```jsonc
{
  "place_id": "gb:<pin_id>",
  "pin_id": "...",
  "source": {"dataset": "gb1900_final_raw_dump_2018", "licence": "CC0",
             "first_transcription": "F.P.", "g_point_wgs": "<ewkb>",
             "classification_count": 3},
  "lon": .., "lat": ..,                     // decoded from source EWKB
  "text": {"value": "F.P.", "source": "raw|vlm|user", "confidence": .., "version": ".."},
  "type": {"token": "footpath", "aat": [300008337],
           "method": "tier0-abbrev|tier1-vlm|user", "rule": "abbrev:F.P.",
           "confidence": .., "version": "gbtype-v1"},
  "bbox": {"px": [...], "geo": [...], "method": "surya-ocr|vlm", "version": ".."},
  "os_style": {"value": "Stump", "method": "tier1-vlm", "confidence": .., "version": ".."},
  "edits": [ {"field": "type", "from": null, "to": "footpath",
              "method": "tier0-abbrev", "version": "gbtype-v1", "ts": "<stamped>"} ]
}
```
- `text` keeps the **original** raw transcription untouched under `source`; any VLM
  or user correction is a *new* layer with its method, so every change is auditable.
- Every derived field (`type`, `bbox`, `os_style`, corrected `text`) records
  **method + confidence + version**; the `edits[]` log is the human-readable trail.
- **Corrections we make (VLM/OCR text reads, §4.2/§4.3) are first-class recorded
  edits**, standing in for the curated set's ~30k manual fixes we forwent.

### 11.2 Versioning — re-runnable as feedback accrues
- **Classification version** (`gbtype-vN`): every full re-derivation bumps it.
  Records carry the version that produced each field, so an edition is a snapshot
  and successive editions are **diffable** (what changed, why).
- **Reproducibility:** Tier-0 is deterministic (dict + rules, both versioned in
  `typesystem/data/`); Tier-1 pins the **VLM model + prompt/schema hash** so a
  re-run is repeatable and a model change is a visible version bump.
- **Feedback loop:** user corrections from the WHG UI (wrong type / wrong text)
  are captured as **high-priority ground truth** with their own provenance
  (anonymised who/when), stored durably, and **override lower-confidence auto-types**
  on the next re-run — never silently lost. Periodic re-runs fold in accumulated
  feedback + dict improvements + better tiles/models.

### 11.3 Publication & naming
- **Licence:** built on **CC0** source, so WHG may publish the derived edition
  under its own choice (recommend an open licence, e.g. CC-BY or CC0). **Not**
  encumbered by CC-BY-SA share-alike (the reason we chose the raw dump).
- **Naming — MUST differ from "GB1900" / "Great Britain 1900"** (both licences
  forbid naming a derivative "GB1900…"). The CC0 README also *welcomes*
  acknowledgment of the "GB1900 project" — so **credit them in docs** (goodwill,
  not required). Candidate WHG names *(SG to choose)*: e.g. **"WHG Historical
  Feature Layer of Britain (c.1900)"**, **"WHG British Map-Label Gazetteer,
  1888–1914"**, or a short codename. The internal WHG namespace stays `gb`
  (an identifier, not the published product name).

### 11.4 Storage
The provenance records + detected bboxes live in the durable research cache on
`/vast` alongside the tile cache (§5.3) — `${IX3_BASE}/gb1900/edition/` — so every
edition, edit log, and detected bbox is retained for re-use and audit.

---

## Appendix — key files & commands referenced

- Authority: `authorities/gb1900-places.py` (docstring §12–29 has the original
  VLM idea; line 25 "SW corner" → correct to "bottom-left of first letter").
- AAT path: `processing/manual_aat_maps.py`, `processing/aat_enrich.py`
  (`augment_doc`), `processing/apply_aat_enrich.py`
  (`--namespace gb --es-host … --execute`).
- Status docs: `developer/aat-typing-status.md` ("ZERO: gb 1.17M"),
  `developer/plan-outstanding-2026-07.md` §2.
- Idempotent live-patch precedent: `developer/handoff-wikipedia-sitelinks.md`
  (`wikipedia_links_patch.py` → `apply_links_patch.py`).
- **GOTW VLM pattern (copy this):** `/home/stephen/PycharmProjects/GOTW/`
  → `process/submit_vlm_slurm.py` (GPU array + per-shard vLLM serve),
  `process/triage_pages.py` (schema'd JSON inference, concurrency, salvage),
  `process/run_pipeline.sh` (submit-and-poll orchestration; env vars
  `VLLM_ENV=/vast/ishi/envs/vllm`, `HF_CACHE=/vast/ishi/hf_cache`,
  `VLM_MODEL=Qwen/Qwen2.5-VL-72B-Instruct-AWQ`), `process/aat_resolve.py` /
  `process/build_aat_shortlist.py` (string→AAT seed).
- **OS lettering ground-truth (§4.2.0):** OS 404 "Character of Writing for
  Ordnance Survey Plans" (1881 & 1914 eds, digitised at NLS); "Conventional Signs
  and Writing Used on the Six Inch Maps" Plate IV in *A description of the large
  scale maps of Great Britain* (1920), NLS `maps.nls.uk/view/128076894`; "Notes on
  Archaeology for Guidance in the Field" (1921, O.G.S. Crawford); "Notes on
  Boundaries" (1914); Richard Oliver, "A few notes on map lettering", *Sheetlines*
  95, 33-36 (Charles Close Society); *Ordnance Survey alphabets* (OS, 1934). NLS
  Characteristic Sheets index: `maps.nls.uk/os/characteristic-sheets/info.html`.
- NLS: layer `uk-osgb10k1888` (six-inch 2nd ed. 1888–1915, EPSG:3857);
  MapTiler template `https://api.maptiler.com/tiles/uk-osgb10k1888/{z}/{x}/{y}.jpg?key=…`;
  confirm the NLS direct XYZ + max zoom from `maps.nls.uk/geo/explore/`; six-inch
  is non-commercial-only + non-redistributable, attribution required.
- CRC conventions: `sbatch -M htc --account=ishi` (or `-M gpu`), conda before
  `set -u`, no jobs on login nodes, everything on `/vast`.
