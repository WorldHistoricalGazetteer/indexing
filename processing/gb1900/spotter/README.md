# GB-STAMP spotter stage (MapReader / MapTextPipeline)

Text-spotting over the OS six-inch tiles. The **spotter is the box authority** — it detects and
transcribes label boxes; the VLM's own bounding boxes were deprecated as unreliable (visual-grounding
weakness). See `developer/plan-gb1900-typing.md` §0b / §12.

Runs on CRC in the **`mapreader` conda env** (`~stg135/.conda/envs/mapreader`; `detectron2` and
`MapTextPipeline` are **local source builds** — see `../env-locks/mapreader.pip.lock` and
`../mapreader_setup.sh`). Paths in these scripts are hardcoded to the CRC probe tree
(`/vast/ishi/gb1900/probe/mapreader_text/`); they are captured here as the authoritative record of
the stage, not as portable modules.

## Files

| File | Role |
|------|------|
| `tiling.py` | tile bbox / global-pixel helpers for a region |
| `region_common.py` | shared region bbox + lon/lat↔global-pixel conversion (`region_bbox`, `lonlat_to_global_px`) |
| `region_mask.py` | build region tile mosaic + box/pin stats (`stats.json`) |
| `spot_worker.py` | per-worker MapTextPipeline inference → `region/boxes/worker*.jsonl` |
| `pipeline.py` | 4-tile smoke test (town/rural/coast/moor) → `out/summary.json`, `out/run*.log` |
| `build_hitl_manifest.py` | spotter boxes ⋈ GB1900 pins ⋈ VLM `os_style` → clean tile crops → `hitl/manifest_clean.json` |

Then inject the manifest into the in-repo viewer (see below), **not** a claude.ai artifact.

## Results (2026-07-17/18)

Region run: **8,081 spotter boxes** over **3,784 GB1900 crowd pins** — ~2.1× expansion.
- **902 untranscribed word-labels (11.2%)** — new place labels the crowd never captured (384 isolated).
- 1,538 numeric (spot-heights etc.), 543 other.
- **70% recall on crowd pins** (2,645/3,784 matched a spotter box) — the ~30% crowd-only gap
  (detection miss vs. too-strict box↔pin match) is an open item before relying on the spotter alone.

4-tile smoke (spotter-only ≫ crowd-only everywhere; strongest on sparse maps — Dartmoor 62 new vs 4 crowd).

## HITL font review (browser, local)

```bash
# 1. build the manifest on CRC (mapreader env)
python build_hitl_manifest.py            # -> hitl/manifest_clean.json

# 2. inject into the viewer template -> self-contained HTML (runs anywhere)
python -m processing.gb1900.hitl_build \
    processing/gb1900/hitl/manifest_clean.json \
    processing/gb1900/font_hitl_review.html \
    processing/gb1900/hitl/hitl_review_clean.html

# 3. open it locally in a browser (file://) — decisions export as JSON
```

Generated artifacts (`processing/gb1900/hitl/`, the manifest + built HTML) are gitignored — regenerable.
