# Plan — Parish (& admin) boundary extraction from OS six-inch maps (VLM/CV)

> **Status:** Design / experiment. Grounded in the real raster 2026-07-17. A
> follow-on to GB-STAMP (`plan-gb1900-typing.md`); shares its tile cache + VLM infra.
> **Goal:** derive **historical admin boundaries (esp. civil parishes) c.1900** by
> extracting them from the OS six-inch 2nd-ed raster itself — a **fully-open, our-own
> derivation** — to label each GB-STAMP point with its admin hierarchy, without the
> licence problems of CAMPOP (safeguarded) or GBHGIS (CC-BY-SA/commercial).

## Scope — ALL of parish, district, borough AND county (our own, co-registered)
This project extracts the **full admin hierarchy** off the six-inch raster: **civil
parishes, rural/urban districts, municipal/parliamentary boroughs, AND counties**. We
want **our own** boundaries for every level — even county, which *is* available
elsewhere (Historic Counties Trust / `ukhc`) — because a county line traced from the
**same raster, by the same pipeline** as the parishes and districts it contains is
**precisely co-registered** with them: parishes nest cleanly inside their district,
district inside county, with no cross-source sliver/gap artefacts. A borrowed county
polygon from a different survey/generalisation would not align pixel-for-pixel with our
extracted sub-units. Co-registration is the point.

## Why this route
- **County** is available open elsewhere (Historic Counties Trust / `ukhc`) and **modern**
  admin is open (OS Boundary-Line, OGL) — but those are *other people's geometries* on
  *other* surveys; we want county lines drawn from **our** raster so they nest exactly
  with our parishes/districts (see Scope). The genuinely un-sourced gap is **historical
  (c.1900) sub-county** admin — parishes, rural districts, unions — where the only rich
  source (**CAMPOP 1851**) is **safeguarded, non-redistributable**
  (see `markets/geodata/1851/LICENCE.md`).
- The OS six-inch sheets **draw these boundaries and label their type**. Extracting
  them ourselves off the NLS raster (which we're licensed to read for research) yields
  an **openly-publishable** admin layer. CAMPOP is then used only as **internal,
  registered-access ground truth for validation** — never republished.

## Grounding — what the raster actually shows (verified 2026-07-17)
Stitched OS six-inch near Dolgarrog (Conwy valley). Direct observations:
- **Admin boundaries are rendered distinctively** — a "pecked" line of **× marks and
  dots** ("mereing" symbols showing what the boundary follows), clearly **different
  from the thin solid field/enclosure lines**.
- **Boundary TYPE is inline-labelled and legible**: `Union & R.D. By.` (Union & Rural
  District Boundary), `Co.` (County), `C.P.` would mark a Civil Parish. Also `HW.M.O.T`
  (tidal), `B.M.` (bench mark). These follow the documented OS 404 boundary/mereing
  conventions we already hold (`gb1900_os_lettering.json` era).
- Boundaries **follow features** (rivers, roads, fences) — so their geometry ≈ the
  underlying feature's line + the × mereing marks.
- **Feasibility signal:** a capable VLM can *identify and read* these boundaries + type
  labels directly (confirmed by human view). The hard part is **precise geometry**, not
  recognition.

## Capitalise on the GB1900 transcriptions we ALREADY have (big shortcut)

The volunteers already transcribed the **boundary labels**, georeferenced to points —
so the *semantic* half (boundary TYPE + which units) is largely free from GB-STAMP
data itself; the VLM only fills gaps. Mined the 2.67M labels (2026-07-17):

| Boundary type | GB1900 labels | example |
|---|---|---|
| Rural/Urban **District** (`R.D./U.D. By.`) | **19,038** | `U.D. By.`, `Union & R.D. By.` |
| **Borough** (`Munl./Parly. Boro. By.`) | **4,401** | `Parly. Boro. By.` |
| **County** (`Parly. Co. By.`) | **710** | `Parly. Co. By. (Carn.) (Denb.)` ← names *both* counties |
| **Detached** parts (`(Det.)`) | 321 | `DOLGARROG (Det.)` |
| **Civil parish** (`C.P.`) | **only 57** | `C.P.` |

**Implications:**
- **District / borough / county boundaries are richly pre-labelled** — these georef'd
  points give us both *where* a boundary runs and *what type* (and, for counties, the
  pair it divides), which **bootstraps the CV detection + classification** for those
  levels. But we still trace the *geometry* ourselves off the raster (the labels are
  points, not lines) so that district/borough/**county** polygons are **co-registered**
  with the parishes — see Scope. All three are wanted deliverables, not throwaway.
- **Civil parishes are barely labelled (57 `C.P.`)** — the six-inch labels the parish
  line sparsely. So parish extraction specifically **cannot lean on the transcriptions**;
  it needs CV line-detection + naming from *enclosed GB-STAMP settlement labels* (and
  the many parish boundaries that coincide with district/county lines). Parishes are
  **the hardest and most essential** level; districts/boroughs/counties are easier
  (label-bootstrapped) but equally in-scope, extracted by the same pipeline for exact
  nesting.

  (Hundreds/wapentakes are out of scope — not drawn on the c.1900 six-inch, and not
  relevant to this project.)

## Method — HYBRID (CV geometry + VLM semantics)
VLMs hallucinate coordinates, so don't ask a VLM to trace precise polylines. Split:

1. **Line detection / segmentation (CV)** — detect boundary pixels (the ×-dot pecked
   line) and vectorise to polylines. Options, cheapest first:
   - **MapReader** (Living with Machines; the NLS-map ML toolkit) — patch classification
     / segmentation purpose-built for OS six-inch — the natural first tool to try.
   - A small **semantic-segmentation** model fine-tuned on boundary vs not (weak labels
     from CAMPOP/OS-BL rasterised onto tiles — *internal training only*).
   - Classical CV fallback: the ×-dot mereing pattern is a distinctive texture (template
     / morphology) — cheap baseline to gauge separability from field lines.
2. **Semantic labelling (VLM)** — on crops around detected boundary segments and their
   inline labels, the VLM reads the **boundary TYPE** (`C.P.`/`R.D.`/`Co.`/`Union`) and
   any name — reusing the GB-STAMP VLM infra (h200, marker-crop + verbatim recipe).
3. **Assembly** — link classified segments into **closed polygons** per admin level
   (topology/graph closure; boundaries share the same feature lines).
4. **Attribution** — name each polygon from: enclosed GB-STAMP settlement labels, any
   `C.P.` label, and cross-reference to `ukhc` county + (internal) CAMPOP.
5. **Validation** — score extracted polygons against **CAMPOP 1851** (internal,
   registered-access) and OS Boundary-Line — IoU / boundary-recall. Publish only our
   own geometry (open).

## Reuse / cost
- **Tiles already cached** on `/vast/ishi/gb1900/tiles` (the GB-STAMP fetch) — no new
  NLS load. Crop/stitch tooling exists (`gb1900_tiles.py`). VLM infra proven (h200).
- The VLM semantic pass is small (only boundary crops, far fewer than 1M labels).
- The CV/segmentation is the research unknown → do a **detection probe first**.

## P1 probe result — classical global detection (2026-07-17)
Ran a CPU classical probe on a stitched z16 tile set (Conwy valley, `parish_probe.png`).
Artefacts in scratchpad (`detect_v1.py`/`detect_v2.py`, `out_*.png`). Findings:
- **The boundary cue is real and specific:** a fine **dotted/pecked line + periodic bold
  `×` mereing-marks + direction arrows (`>`/`^`)**. Field lines, contours and even the
  *parallel* `H.W.M.O.T` tidal dotted-line carry **no ×** — so the ×/arrow glyph is the
  discriminative signal, the dotting alone is ambiguous.
- **But classical GLOBAL detection is a NO-GO at zoom-16.** Density+cross matching fires
  on every field-corner junction/text/stipple (852 false hits). Stroke-boldness gating
  can't separate either: at z16 the × arms are only **~1.5px**, so there is **no thickness
  gap** vs the field-line mesh — gate hard → miss the ×'s (9 hits, mostly estuary
  stipple); gate soft → drown in junctions (280+). No clean global threshold exists.
- **MapReader (patch classifier) would fare no better / worse** — the signal is a sparse
  subtle glyph among look-alikes; a coarse "is there a boundary in this patch" head can't
  localise it or reject the tidal-dot twin. (User's doubt validated. A quick MapReader
  baseline can still be run for empirical confirmation if wanted.)
- **Resolution lever found:** the NLS `os/6inchsecond` tileset serves a **genuine zoom-17**
  (crisp, real 2× detail; z18 404s). At z17 the × arms ≈3px and the dotting ≈4-5px —
  **separable**. Cost: z17 quadruples tiles, so fetch it **only along boundary corridors**.

**Verdict → pivot to label-SEEDED local tracing (P1b).** We hold **19k+ georeferenced
boundary-type label points** (`Union & R.D. By.`, `C.P.`, …). That reframes the task from
blind global segmentation to: *at each known boundary-label point, fetch z17 locally and
trace the specific ×-marked pecked line it annotates* — the label fixes location AND type,
and a VLM can reason "follow the ×-marked line, not the tidal dots" (which neither
classical CV nor a patch classifier can). This is the approach to build.

## P1b probe results — three methods compared (2026-07-17)
Artefacts under `/vast/ishi/gb1900/probe/` + scratchpad. All at z16 (Conwy valley).

**(A) MapReader patch classifier — coarse ROI only, NO localisation.** resnet18 on 1200
label-seeded patches: **92% held-out patch accuracy** (bnd precision 0.975 / recall 0.856)
— so it reliably says "a boundary passes *near here*". But the sliding-window P(boundary)
heatmap is a **~180 m-wide band** over the whole river/railway corridor, and fires as hard
on the **L&NWR railway**, rivers, and woodland stipple (`Coed Maenan`) as on the boundary.
Verdict: useful only as a region-of-interest proposer; cannot resolve the line. **User's
doubt validated.**

**(B) RF pixel classifier on SELF-LABELLED synthetic data (the Ilastik-style approach) —
real per-pixel signal, blob-confounded.** Composite training data = real boundary-free
crops + procedurally rendered mereing dots/×'s with free pixel masks; skimage multiscale
features (σ=1..8) → RandomForest; **zero manual labelling**. On the real sheet the boundary
×-dot line *does* light up — markedly better than hand-crafted detectors — but so do
buildings, text, field-corner ticks and tree stipple, because a per-pixel classifier keys
on "compact dark glyph" and can't see that the boundary is a **linear chain** while
buildings/text are **blobs**. Fixable: realistic glyph scale/sparsity + hard negatives
(buildings/text). **Confirms the two-stage need.**

**(C) Verdict → two-stage, as the user's road experiments found:** stage-1 pixel/patch
signal (B, or A as ROI) + **stage-2 structural model** that enforces line-continuity and
rejects blobs (SegFormer/U-Net on the same synthetic data, now runnable on a100), then chain
the surviving line via the ×-anchors **seeded by our 25.9k boundary-label points**. Since the
glyphs are OS-standardised (unlike roads, which defeated this approach), the synthetic-data
route should generalise nationally far better than it did for roads.

## Stage-1@z17 + Stage-2 results (2026-07-17, cont.) — see `gb1900-boundary-probe/`
Redid the pipeline at **z17** (the user's suggestion — correct): the resolution lever is
decisive.
- **Stage-1 RF @ z17:** the boundary dotted-line is now cleanly & continuously traced
  (vs lost-in-the-mesh at z16). Residual confounds = text, field drainage-arrows, a few
  buildings — all *blob/cluster* artefacts for Stage-2 to reject.
- **Stage-2 U-Net (structural line-enforcer):** target = the continuous boundary
  *corridor* (path as a thick line), so the net connects discrete mereing marks into a
  line. **In-domain perfect (0.93 vs 0.01); first real-transfer = 0.01 EVERYWHERE
  (total domain collapse).** Root cause: cv2 **hard-edged** synthetic glyphs on soft real
  scan → net keys on edge-sharpness. **Fix — blur the ink layer + domain randomisation**
  (dot size/spacing/darkness, mere-along-a-line, gamma/blur) → transfer restored (real
  max 0.01 → 0.996). This is the exact synthetic-to-real wall the road experiments hit;
  soft-edge realism is the lever.
- **Remaining challenge (well-defined):** Stage-2 now detects mereing-*style* lines but
  confuses the admin boundary with other dotted/dashed lines (hedgerows, drainage). Two
  discriminators, both in hand: **(a) the × mereing-marks** (boundary-only) and **(b)
  label-seeding** from the 25.9k georeferenced boundary labels (which dotted line IS the
  boundary). Label-seeded inference dissolves the ambiguity — this is the next step.

## Phased pilot (start ASAP; schedule GPU around the running GB-STAMP job)
- **P0 — grounding (DONE):** confirmed boundaries are visible, distinct, type-labelled,
  VLM-legible.
- **P1 — classical global probe (DONE 2026-07-17):** NO-GO at z16 (see result above);
  z17 is the native ceiling and makes glyphs separable; pivot to label-seeded tracing.
- **P1b — label-seeded VLM/CV tracing probe (NEXT, spare GPU):** at a sample of `Union &
  R.D. By.`/`C.P.` label points, fetch a z17 window and have the VLM emit the boundary
  polyline / control points, disambiguating the ×-marked line from tidal/field dotting.
  Optional classical `×`-anchor detector at z17 as a cross-check + a MapReader baseline.
- **P2 — VLM semantic probe (spare GPU):** on ~a few hundred boundary crops, the VLM
  reads boundary TYPE + name. (Confirms the human-level read scales; runs on spare h200
  *after* / alongside the GB-STAMP run — do NOT contend with it.)
- **P3 — one-parish end-to-end:** detect → classify → close one parish polygon → name →
  score vs CAMPOP. Go/no-go on the whole approach.
- **P4 — scale** to a county, then GB; join polygons to GB-STAMP points (same
  point-in-polygon tooling as `gb1900_dating.py`, `--src-epsg 27700`).

## Licensing
Extracted boundaries are **our derivation off the NLS raster** → openly publishable
(CC0/CC-BY, attribute NLS for the source imagery, per §5.3 of the typing plan). CAMPOP
and GBHGIS are used **only as internal validation ground truth**, never redistributed.

### Priors — open-only, verified + scoped (2026-07-17) — `priors.py`
Ethics gate (SG): a licence-bound boundary may **shape the output only if it's open** —
else hinting from it launders restricted data into a nominally-open layer (breach + defeats
the project's purpose). Policy:
- **CAMPOP / GBHGIS (safeguarded): validation-ONLY.** Never a prior / seed / training label.
  A model trained on them encodes them → contaminates every output.
- **HCT / `ukhc` historic counties: OPEN** — *verified* (county-borders.co.uk / Historic
  Counties Trust: free for personal/educational/non-commercial **and commercial** use,
  attribution requested; already WHG-vetted + ingested as `ukhc`). Usable as a prior **with
  attribution**. Correct period for counties.
- **OS Boundary-Line parishes (OGL): open** — usable as a *weak* prior, but modern civil
  parishes drifted substantially from c.1900 (1930s reviews, 1974 reorg, urban absorption),
  concentrated in populated areas → prior only, never output; **log hint↔raster divergence**.
- A prior only **reweights** the raster-traced probability (`apply_prior`) and logs
  disagreement (`divergence`); the published geometry stays the independent raster tracing.

### Admin tags without the boundaries — SOLVED via the GB1900 gazetteer (2026-07-18)
Re-examined CAMPOP for deriving admin *tags* (not geometry) for GB-STAMP: still not open for a
*published* edition (UKDS safeguarded EUL = non-commercial + database-right "substantial part" +
dense per-point tags reconstruct the boundaries). Checked **GBHGIS parishes** as the open
alternative — they are **NOT open**: historical *parish boundaries* are Vision of Britain's main
**commercial** product (non-commercial only by request to gbhgis@port.ac.uk); "all boundary data
on UKDS **except parish boundaries, limited commercial use**". So neither CAMPOP nor GBHGIS is
open for published parish tags.

**BUT the GB1900 gazetteer already carries the tags, published CC-BY-SA.** The complete/abridged
GB1900 gazetteer columns are `pin_id, final_text, nation, local_authority, parish, …` — VoB ran
the point-in-polygon against their (commercial) boundaries and **released the DERIVED per-pin tags
under CC-BY-SA**. Fill (400k sample): **nation 100%, local_authority 100%, parish 95%**. So a
trivial **`pin_id` join** (our `gb:<pin_id>` = raw-dump/gazetteer pin_id) gives GB-STAMP its
nation/district/parish tags **openly (CC-BY-SA)** — no geometry, no CAMPOP, no GBHGIS-commercial.
Caveats: CC-BY-SA (attribution + share-alike to GB1900/VoB, not CC0 → segment the edition or accept
CC-BY-SA on those fields); covers the ~2.55M complete-gazetteer subset of our 2.67M (~95%); we hold
the *abridged* (1.17M) locally — fetch the *complete* (CC-BY-SA, visionofbritain.org.uk) for full
coverage. This **removes the need to extract parish geometry ourselves for TAGGING** (boundary
extraction now only matters if we want the polygons themselves). See `plan-gb1900-typing.md` §0a.

**Scope reality (verified on the test region):** HCT priors help at the **county** level
only. The Conwy test boundary is `Union & R.D. By.` — a **sub-county** line **fully inside
Caernarfonshire** (county border 1.57 km away). Worse, **no open source maps to a 1900
Union/R.D. boundary** (Unions abolished 1930, RDs 1974; modern civil parishes ≠ that
geography). So for sub-county levels there is **no open prior at all** → they rest on the
**intrinsic mereing signature (CV)** + CAMPOP validation-only. Open priors are a genuine
help only where the target *is* a county (or ~persistent parish) line.

## Practical note
The GB-STAMP typing run currently owns the h200 workers. P1 (CV/MapReader detection) is
independent of that GPU and can start now; the P2 VLM probe should wait for spare h200
capacity (or run after the typing run finishes) so it doesn't slow GB-STAMP.

---

# IMPLEMENTATION (built 2026-07-17) — `developer/gb1900-boundary-probe/`

The two-stage CV/ML pipeline that came out of P1b. All code in
`developer/gb1900-boundary-probe/`; runs on the CRC **a100** cluster
(env `/vast/ishi/envs/boundary`, torch cu124 — cu13 was too new for the 12.9 driver).

## Architecture (two stages, chained)
```
z17 tile → preprocess → Stage-1 (multi-class RF) → {dot,cross,arrow} evidence
        → Stage-2 (U-Net line-enforcer) → boundary corridor → (seed w/ 25.9k labels)
```
- **Preprocess** (`boundary_pipeline.preprocess`): background-flatten (divide by a
  morphological paper estimate) + CLAHE → consistent ink contrast across faded/uneven scans.
- **Stage-1 — multi-class RF** (`stage1_multiclass.py` / `boundary_pipeline.train_stage1`):
  skimage multiscale features (Ilastik-style) → RandomForest classifying each pixel into
  **{bg, dot, dash, cross, arrow, solid}**. The component classes disambiguate feature
  types (hedge=dash, road=solid, footpath=double-dash, **boundary = dot+cross+arrow**).
- **Stage-2 — U-Net line-enforcer** (`boundary_pipeline.UNet`): consumes Stage-1's
  {dot,cross,arrow} evidence and emits the continuous boundary **corridor**, trained on
  **Stage-1's actual outputs** over synthetic images (faithful chain) with distractors
  (dot-only footpaths, text-crosses, stipple) so it requires the mereing signature and
  rejects look-alikes. Wide receptive field needed (×'s sit up to ~50px off the line).
- **Chaining**: the Stage-1 component labels **mask** the image so Stage-2 only sees
  boundary-relevant components (user's Ilastik-mask → refinement flow).

## Self-labelled synthetic data (the labelling answer)
`synth_glyphs.py` + `degrade.py`. Composite **real boundary-free crops** (realistic
negatives) + **procedurally rendered mereing glyphs** with **free pixel masks** — zero
manual labelling. Glyph geometry **grounded against the real z17 raster** (SG corrections):
- **Dots dominate**: big (r 2–3), round, **regularly** spaced (~18–26px pitch).
- **× marks**: base **parallel to the local boundary tangent** (rotate with the line,
  appearing as × on horizontal runs and + on 45° runs); scale ~6–8px half-arm; **offset
  12–40px to one consistent mere side** (not on the line); **rare** (~1 per 180–340px).
- **Arrows**: tangent-oriented, rare.
- **Footpaths**: **double parallel dashed** lines — a key *negative* (very numerous).
- **Print/scan degradation** (`degrade.py`): soft (blurred) edges — *essential*, hard
  cv2 edges caused total domain collapse (0.01 everywhere); uneven ink density, broken
  strokes, ink bleed, foxing, speckle, variable blur; **overlay ink darkness matched to
  the crop's existing ink** (else glyphs read too light).
- **Colour**: our `os/6inchsecond` sheets are monochrome sepia; *some* regionally-localised
  sheets are coloured (blue water / red buildings) — add colour features opportunistically
  later, greyscale is the baseline.

## Metric-driven tuning
`boundary_pipeline.py --sweep`. Hand-traced **boundary GT** on the z17 test stitch
(`gt_boundary.npy`, verified by overlay) → **boundary-F1** (predicted corridor vs GT line
within τ px, best threshold). First sweep varies RF (trees/leaf/feature-σ) and U-Net
(depth/base) — incl. a deeper U-Net for the far-offset ×'s. Slurm: `sweep.sbatch` on a100.
This replaces eyeballing with numbers, per SG's request to tune efficiently.

## Method comparison (P1b, z16→z17)
| Method | Verdict |
|---|---|
| Classical hand-crafted (density/bold-×) | NO-GO (no thickness gap at z16) |
| MapReader patch classifier | 92% patch acc but coarse ~180m corridor, no localisation |
| **RF multi-class @ z17** | separates boundary dots from hachures; text→cross residual |
| **U-Net line-enforcer** | connects mereing marks; needs seed/×-anchor vs hedge-dashes |

## Status / next
- Env + assets on CRC a100; first sweep running (`sweep.out`). **Synthetic geometry
  iterated with SG through grounding** (dots, tangent-× , mere-side offset, rarity,
  footpaths, matched ink density). **TODO**: cap ×-per-tile + ease foxing; find a genuine
  double-dashed footpath to calibrate; then re-sweep with corrected synth; label-seeded
  inference (25.9k boundary labels) to disambiguate boundary vs other dotted lines.
