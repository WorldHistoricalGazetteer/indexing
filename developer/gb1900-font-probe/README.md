# GB1900 typography — font-style embedding feasibility probe

**Question:** can a *learned style embedding* separate the OS six-inch label treatments that the
VLM's `os_style` classification confused (upright vs italic serif, slab/Egyptian, **outline caps**,
spaced caps, blackletter)? If yes, we retire the VLM for typing and cluster the whole corpus instead
(HITL then labels *clusters*, not crops). See `developer/plan-gb1900-typing.md` §0b.

## Method

1. **Synthetic supervised-contrastive pretrain** (`fonts.py` → `degrade.py` → `data.py` → `train.py`).
   Nine rendering recipes span the OS axes (serif upright/italic, slab, sans, spaced caps, **road caps**
   — small solid caps rendered *between parallel road-casing lines*, the real OS road-name context —
   outline caps, blackletter, engraved caps). **Every class is rendered with many random words/abbrev**,
   so the contrastive label is *style, never content* — this is what forces the encoder to encode the
   typographic "hand" and ignore which characters are present. Ink is composited onto **real cached OS
   tiles** with soft edges, variable opacity, broken strokes and foxing (the boundary-probe domain-gap
   lesson: train on clean glyphs → collapse on real scans).
2. **Encoder** (`model.py`): compact conv stack → global-average-pool → 128-d L2-normalised embedding
   (~1.2 M params). SupCon loss (Khosla 2020).
3. **Validate on synthetic** (`train.py`): 5-NN accuracy + silhouette on held-out synthetic crops. If
   it can't separate synthetic classes, the approach is dead — stop before the real corpus.
4. **Cluster the real corpus** (`embed_cluster.py`): embed real spotter crops (region boxes + the 78
   VLM-labelled HITL crops), HDBSCAN + KMeans, emit a montage per cluster and a cluster × VLM-`os_style`
   cross-tab to see whether clusters cut the styles more coherently than the VLM did.

## What the embedding weighs (and what it does NOT)

A CNN trained this way keys on **how strokes are drawn**, learned from data rather than hand-specified:
stroke-width and its *modulation* (serif contrast vs uniform slab), **serif shape / presence**,
**slant** (upright vs italic), **fill vs outline** (solid stroke vs thin ink ribbon — a local
ink-density signature), **letter tracking** (spaced caps), **weight** (light/bold), and blackletter
texture. "Ink density" and "local density" are part of this, but only as components of a richer
stroke-geometry fingerprint.

It does **NOT** learn character identity. Because positive pairs share a font but have *different
words*, keying on which letters are present cannot satisfy the contrastive objective — gradient descent
is driven toward content-invariant style features. Caveat: very short marks ("B.", "F.P.") carry few
glyphs, so their style fingerprint is inherently weaker — abbreviations may stay ambiguous regardless
of method.

## Run

```bash
sbatch -M gpu --account=ishi --partition=l40s --gres=gpu:1 run.sbatch   # from crc0
```
Outputs → `/vast/ishi/gb1900/probe/font/out/` (`synth_val.json`, `report.json`, `clusters/*.png`,
`hitl_by_cluster.png`). The font bank (`fonts/`) and `out/` are gitignored; see `fonts/SOURCES.md`.
