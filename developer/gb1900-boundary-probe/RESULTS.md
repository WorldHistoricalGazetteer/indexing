# Boundary-detection experiments — results & conclusions (2026-07-17)

Write-up of the attempt to extract **historic administrative boundaries** (parish / district /
union / county) from the OS six-inch 2nd-edition raster (NLS `os/6inchsecond`), to build an
**openly-publishable** admin layer (CAMPOP is safeguarded; GBHGIS restricted). Code:
`developer/gb1900-boundary-probe/`; plan: `plan-gb1900-parish-extraction.md`.

## TL;DR
- **Pixel/patch CV plateaus at boundary-F1 ≈ 0.2** on the Conwy test sheet, unmoved by
  architecture tuning **or** by heavily-grounded synthetic realism. Same wall SG hit
  extracting *roads* from these maps two years ago.
- **The disambiguation problem is the crux**: telling the dotted *mereing* boundary from
  hedges, footpaths, river banks and field lines. Pixel CV can't (fires everywhere →
  precision 0.09–0.17).
- **A VLM *can* disambiguate** (grid-cell tracing probe: **9/10 negatives correct,
  precision 0.64, recall 0.45**) — a qualitatively better, *precise-but-incomplete* failure
  mode. **This is the promising route.**
- **No external open help exists for sub-county boundaries**: GB1900 boundary labels are too
  sparse to seed (1.24% of labels; **0** in the test region), and no open boundary source
  maps to a 1900 Union/R.D. line. County level *is* helped by open HCT polygons.

## What the raster shows (grounding)
The boundary is OS **mereing**: a line of round **dots**, punctuated by **rare bold ×
cross-marks set offset to one (the mere) side**, plus occasional **arrows**. Look-alikes:
thin solid field lines, single-dashed hedges, **double-dashed footpaths** (delimited by a
single dash + solid line when alongside a wall), hachured river banks, contours, text.
- **z16 is below the resolution floor** (× arms ~1.5px, no thickness gap); **z17 is the
  native ceiling** (real 2× detail; z18 404s) and makes the glyphs separable. All real work
  is at z17.

## Methods & results (in sequence)
| # | Method | Result |
|---|--------|--------|
| 1 | Classical hand-crafted (ink-density + bold-× template) @ z16 | **NO-GO** — 852 false hits, or misses the ×'s; no clean threshold |
| 2 | **MapReader** patch classifier (label-seeded, resnet18) | 92% patch acc but **no localisation** — a ~180 m corridor band; fires on railway/river/woodland as hard as boundary. ROI proposer only |
| 3 | **RF pixel classifier** on self-labelled synthetic (Ilastik-style), binary @ z16 | Boundary detected but **blob-confounded** (buildings/text/stipple) — per-pixel can't see the line is a *chain* |
| 4 | **RF multi-class** {dot/dash/cross/arrow/solid} @ z17 + CLAHE preprocess | Separates boundary **dots** from parallel **hachures**; residual **text→cross** confusion. Real component signal |
| 5 | **U-Net Stage-2** line-enforcer (corridor target), chained on Stage-1 evidence | In-domain perfect (0.93); **first real transfer = 0.01 everywhere** (total domain collapse) |
| 6 | + **soft-edge synthetic + degradation** (the domain-gap fix) | Transfer restored (real max 0.01 → **0.996**); now detects mereing-*style* lines but confuses with hedge/footpath dashes |
| 7 | **Metric-driven sweep** (boundary-F1 vs hand-traced GT), round 1 | Best **F1 0.211** (deep+wide U-Net, RF 300/σ12); deeper/wider helps (far-offset ×'s) |
| 8 | Sweep round 2, **fully-grounded synthetic** (× scale/tangent/mere-offset/rarity, footpaths, matched ink) | Best **F1 0.209** — **no improvement**; simplest config won, high variance |
| 9 | **VLM grid-cell tracing** (Qwen2.5-VL-72B-AWQ, a100) | **pos_recall 0.45, pos_precision 0.64, neg_false_alarm 1/10** — disambiguates, rarely false-alarms |

### The domain-gap lesson (method 5→6)
The single biggest CV lever was **not** architecture but **synthetic realism**: cv2 hard-edged
glyphs on soft scanned ink made the net key on edge-sharpness → 0.01 everywhere on real.
Blurring the ink layer + print/scan degradation (uneven ink, broken strokes, foxing, speckle,
**overlay ink matched to the crop's existing ink darkness**) fixed it. Self-labelled synthetic
(real boundary-free crops + rendered glyphs → free masks) removed the manual-labelling
bottleneck entirely — the glyph geometry was **grounded against the raster through SG review**
(× is ~6–8px, base parallel to the local tangent, offset 12–40px to the mere side, rare;
dots dominate; footpaths double-dashed).

## Why the CV plateaus — and why external hints don't rescue it
- **Labels too sparse to seed.** 33,071 boundary-type labels nationally = **1.24%** of labels;
  the test region has **14 labels, 0 of them boundary** despite a clear `Union & R.D. By.`
  running through it (volunteers never transcribed it). A boundary can run through a region
  with no label at all → label-seeded *tracing* is impossible.
- **No open prior for sub-county boundaries.** HCT/`ukhc` historic counties are open + correct
  period, **but county-level only** — the test boundary is a *sub-county* Union/R.D. line
  **fully inside Caernarfonshire** (county border 1.57 km away). Unions/RDs were abolished
  (1930/1974) and modern civil parishes are a different geography, so **nothing modern maps
  to a 1900 Union/R.D. line**. CAMPOP would, but it's safeguarded → validation-only (using it
  as a hint would launder restricted data into a nominally-open layer — see plan §Licensing).
- So sub-county extraction rests entirely on the **intrinsic mereing signature**, and the
  pixel CV reads it only to F1 ≈ 0.2.

## Conclusions
1. **Pixel/patch CV is not good enough** for reliable sub-county boundary extraction here —
   the same wall as roads, for the same reason (no strong hints; disambiguation is intrinsic).
2. **The VLM route is qualitatively better** — it *reasons* about which line is the boundary
   and rarely false-alarms (the CV's fatal weakness). Its recall (~0.45) and occasional
   adjacent-line drift are improvable (finer grid, overlap-voting, seeding at the ~33k label
   anchors, multi-crop consensus).
3. **Scope the open deliverable honestly**: **counties** are tractable (open HCT + CV/VLM);
   **districts/boroughs** have ~24k label anchors; **civil parishes** are the research
   frontier — neither labels, nor priors, nor a legal CAMPOP hint — pursue via the VLM route,
   don't *promise* them as output.
4. **Ethics held**: only open boundaries (HCT, OSBL) may act as *priors* (reweight, never
   train/seed/output; log divergence); CAMPOP/GBHGIS stay **validation-only**.

## Recommended next steps
- **Develop the VLM-tracing route** (highest value): finer grid + overlapping crops with
  cell-vote fusion; anchor at the 33k boundary labels; assemble cells → polylines → score vs
  GT and (internally) vs CAMPOP; test on a cleaner inland boundary too.
- **County layer now**: HCT county attribution already built
  (`processing/gb1900_county_attribution.py`) — assigns each GB-STAMP label its HCS 3-char
  code by point-in-polygon of the label centre, with a near-border **uncertainty flag** whose
  work-list feeds the VLM for true-bbox refinement.
- Keep CV as a **candidate proposer** feeding the VLM (recall) rather than the final decider.

## Artefacts
Code in `developer/gb1900-boundary-probe/` (`synth_glyphs.py`, `degrade.py`,
`stage1_multiclass.py`, `boundary_pipeline.py` [+`--sweep`], `priors.py`, `vlm_trace_infer.py`);
CRC env `/vast/ishi/envs/boundary`; GT `gt_boundary.npy`; sweep + VLM results under
`/vast/ishi/gb1900/probe/boundary/`. County attribution: `processing/gb1900_county_attribution.py`.
