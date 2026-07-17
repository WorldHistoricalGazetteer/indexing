# GB1900 boundary-extraction probe (Stage-1 / Stage-2)

Experimental scripts for the parish/admin-boundary extraction R&D
(`../plan-gb1900-parish-extraction.md`). Not production. Developed 2026-07-17 on a
z17 Conwy-valley test stitch (`Union & R.D. By.` boundary down the River Conwy).

| Script | Role | Result |
|--------|------|--------|
| `detect_v1.py` | classical density+cross detector (z16) | 852 false hits — NO-GO |
| `detect_v2.py` | classical bold-× stroke gating (z16) | no thickness gap at z16 — NO-GO |
| `synth_rf.py` | **Stage-1** RF on self-labelled synthetic (z16) | boundary detected but blob-confounded |
| `synth_rf_z17.py` | **Stage-1** RF at **z17** | boundary cleanly traced; text/arrows/buildings remain |
| `stage2_unet.py` | **Stage-2** U-Net line-enforcer (z17) | detects mereing/dotted lines; needs seed/×-anchor to reject hedge-dashes |

## Key findings
- **z17 is the resolution floor** (× arms ~3px, dots ~4-5px). z16 lacks the thickness
  gap; z18 doesn't exist on the NLS `os/6inchsecond` tileset.
- **Self-labelled synthetic data works** (real boundary-free crops + rendered mereing
  glyphs → free masks) — zero manual labelling, the answer to the labelling bottleneck.
- **Soft edges are essential.** cv2 hard-edged glyphs composited on soft real scan →
  the net keys on edge-sharpness → **0.01 everywhere on real** (total domain collapse).
  Blurring the ink layer (σ 0.6-1.3) + domain randomisation (dot size/spacing/darkness,
  mere-along-a-line, gamma/blur/contrast) fixed transfer (max prob 0.01 → 0.996).
- **Remaining challenge:** Stage-2 detects mereing-*style* lines but confuses the admin
  boundary with other dotted/dashed lines (hedgerows, drainage). Two levers, both in
  hand: the **× mereing-marks** (boundary-specific; hedges have none) and
  **label-seeding** from the 25.9k georeferenced boundary labels (tells us *which*
  dotted line is the boundary).

## Next
- Seed Stage-2 inference at the 25.9k `Union & R.D. By.`/`C.P.` label points (fetch z17
  locally there); trace only near seeds → the boundary-vs-hedge ambiguity dissolves.
- Add an ×-anchor requirement (periodic ×'s along the line) to sharpen specificity.
- Scale on a100 (CRC has torch+transformers; swap the U-Net for SegFormer-B0 if wanted).
- The z17 VLM tracer is the independent cross-check (label-seeded crops → polyline).
