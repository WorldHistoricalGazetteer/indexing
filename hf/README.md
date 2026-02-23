---
language:
- multilingual
- ar
- zh
- ru
- ja
- ko
- he
- fa
- hi
- el
- ka
- am
- hy
- ur
- bn
- ta
- te
- th
tags:
- toponym-matching
- cross-script
- phonetic-embeddings
- geospatial
- named-entity
- information-retrieval
- teacher-student
- knowledge-distillation
license: cc-by-4.0
datasets:
- geonames
- wikidata
- getty-tgn
metrics:
- recall@k
- mrr
pipeline_tag: feature-extraction
---

# Symphonym v7 — Universal Phonetic Embeddings for Cross-Script Toponym Matching

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.18682017.svg)](https://doi.org/10.5281/zenodo.18682017)

Symphonym maps toponyms (place names) from **20 writing systems** into a unified
**128-dimensional phonetic embedding space**, enabling direct cross-script similarity
comparison without runtime phonetic conversion or language identification.

> *"London" / "Лондон" / "伦敦" / "لندن" → [0.12, -0.34, …] (all nearby)*

## Intended Use

- **Cross-script toponym matching** in geographic databases and gazetteers
- **Phonetic search** — retrieve results for a place name entered in any script
- **Historical record linkage** — match pre-standardisation spelling variants
- **Multilingual named entity linking** in NLP pipelines
- **Digital humanities** — reconciling place references across archival sources

The model operates on **phonetic similarity**, not semantic or orthographic similarity.
It is designed as a **candidate retrieval** component within a larger reconciliation
pipeline, where candidates are subsequently filtered by geographic proximity and
other constraints.

## Quick Start

```python
from inference import SymphonymModel

model = SymphonymModel()   # loads weights from this directory

# Single similarity score
sim = model.similarity("London", "en", "Лондон", "ru")
print(f"London / Лондон: {sim:.3f}")   # → 0.991

# Batch embeddings  (N × 128 numpy array)
embeddings = model.batch_embed([
    ("London",   "en"),
    ("Лондон",   "ru"),
    ("伦敦",     "zh"),
    ("لندن",     "ar"),
    ("ლონდონი",  "ka"),
])
```

### With HuggingFace `huggingface_hub`

```python
from huggingface_hub import snapshot_download

model_dir = snapshot_download("docuracy/symphonym-v7")

from inference import SymphonymModel
model = SymphonymModel(model_dir=model_dir)
```

## Representative Cross-Script Similarities

| Pair | Scripts | Similarity |
|------|---------|-----------|
| London / Лондон | Latin–Cyrillic | 0.991 |
| Athens / Αθήνα | Latin–Greek | 0.980 |
| Beijing / 北京 | Latin–CJK | 0.955 |
| Baghdad / بغداد | Latin–Arabic | 0.969 |
| Jerusalem / ירושלים | Latin–Hebrew | 0.892 |
| Tokyo / とうきょう | Latin–Hiragana | ~0.94 |
| London / Londres | Latin–Latin | 0.474 *(correct: phonetically distinct)* |

## Model Architecture

Symphonym uses a **Teacher–Student knowledge distillation** framework.

### Teacher (PhoneticEncoder) — training only
- Input: IPA transcriptions via Epitran (+ 102 extensions), Phonikud, CharsiuG2P
- Representation: PanPhon192 — 24-dim articulatory feature vectors,
  8-bin positional pooling → 192-dim fixed-length input
- Architecture: BiLSTM → Self-Attention → Attention Pooling → 128-dim projection

### Student (UniversalEncoder) — deployed model
- Input: raw Unicode characters + script ID + language ID + length bucket
- Vocabulary: **113,280 tokens** across 20 scripts, 1,944 language codes
- Architecture: Character/Script/Language/Length embeddings →
  Input projection → BiLSTM → Self-Attention (residual) →
  Attention Pooling → 128-dim projection → L2 normalisation
- Parameters: ~8.3M

The **length bucket embedding** (16 buckets, 8-dim) conditions every character
representation on sequence length, mitigating spurious matches between short
toponyms and long compound strings.

### Three-Phase Training Curriculum

| Phase | Objective | Epochs | Notes |
|-------|-----------|--------|-------|
| 1 | Teacher: triplet margin loss on PanPhon192 features | 50 | val_loss 0.0056 |
| 2 | Student–Teacher distillation: α·MSE + (1−α)·cosine | 50 | α=0.5, Student-Teacher cosine 0.942 |
| 3 | Hard negative fine-tuning (triplet, margin=0.3) | 30 | val_loss 0.02122 |

## Evaluation

### MEHDIE Hebrew–Arabic Historical Benchmark (Sagi et al., 2025)

Independent evaluation on medieval Hebrew and Arabic geographical sources — **not in training data**.

| Method | R@1 | R@5 | R@10 | MRR |
|--------|-----|-----|------|-----|
| PanPhon192 (ablation) | 41.1% | 48.2% | 52.3% | 45.0% |
| Levenshtein + AnyAscii | 81.5% | 97.5% | 99.4% | 88.5% |
| Jaro-Winkler + AnyAscii | 78.5% | 96.2% | 97.8% | 86.3% |
| **Symphonym v7** | **85.2%** | **97.0%** | **97.6%** | **90.8%** |

The PanPhon192 ablation (raw articulatory features, no neural training) achieves only
45.0% MRR — less than half Symphonym's score and below the string baselines —
confirming that performance derives from the training curriculum, not the phonetic
features alone.

### Cross-Script Pair Validation (11,723 pairs, 170+ script combinations)

Systematically sampled from training data (up to 10 pairs per script-pair bin); these
test embedding retrieval quality over the full 67M-toponym index, not generalisation
to unseen sources.

| Metric | v6 | v7 |
|--------|----|----|
| Pass rate (≥0.75 cosine) | — | **90.7%** |
| Embedding coverage | ~98% | **100%** |
| Hiragana↔Katakana mean similarity | 0.000 | **0.981** |

**Best-performing script pairs:** Hiragana–Katakana (0.981), Devanagari–Kannada (0.976),
Devanagari–Telugu (0.976), Cyrillic–Latin (0.923, n=1,334), Arabic–Latin (0.898, n=800).

## v7 Changes

v6 exhibited 0% IPA coverage for Hiragana (151,980 toponyms) and Katakana (340,555 toponyms)
despite both being natively supported by Epitran (`jpn-Hira`, `jpn-Kana`). The pipeline
was dispatching by language first (`lang=ja`), routing all Japanese toponyms to CharsiuG2P
which only processes CJK/Kanji. v7 fixes this by dispatching on detected script *before*
language code, restoring IPA coverage for 492,535 toponyms. The model was retrained from scratch.

## Training Data

Trained on 66.9 million unique toponyms from:

| Source | License |
|--------|---------|
| GeoNames | CC BY 4.0 |
| Wikidata | CC0 |
| Getty TGN | ODC-By 1.0 |

54.0% of training-namespace toponyms received IPA transcription; the remainder
contribute to the Student's character-level learning via distillation.

## Repository Contents

```
model.safetensors          Student (UniversalEncoder) weights
config.json                Architecture hyperparameters
inference.py               Self-contained inference module
requirements.txt           Dependencies
vocab/
  char_vocab.json          113,280-character vocabulary
  lang_vocab.json          1,944 ISO language codes
  script_vocab.json        20 script categories
evaluation/
  mehdie_results_v7_ranking.json
  symphonym_v7_pairs_test_report.json
training_stats/
  coverage_stats.json      IPA coverage by script and language
  phase{1,2,3}_metrics.json
epitran_extensions/        102 custom CSV G2P files
```

## Limitations

- **Phonetic similarity only**: The model does not use geographic coordinates,
  semantic information, or entity types. Phonetically similar but geographically
  unrelated names (Austria/Australia: 0.883) will score highly.
- **Training bias**: Sources over-represent populated places with official names
  in high-resource languages. Performance on under-represented scripts and
  mundane places may be weaker.
- **Tonal languages**: PanPhon encodes segmental articulatory features but not
  tone. Tonal minimal pairs in place names are rare in practice.
- **CJK–Hiragana pairs**: Mean similarity 0.437, reflecting that CharsiuG2P
  produces Mandarin phonetics for Kanji while Epitran produces Japanese readings
  for Hiragana — a genuine phonological mismatch, not a model deficiency.

## Citation

If you use Symphonym in your research, please cite the preprint and the Zenodo dataset:

```bibtex
@misc{symphonym2025,
    author       = {Gadd, Stephen},
    title        = {Symphonym: Universal Phonetic Embeddings for Cross-Script Name Matching},
    year         = {2026},
    eprint       = {2601.06932},
    archivePrefix = {arXiv},
    primaryClass = {cs.CL},
    url          = {https://arxiv.org/abs/2601.06932},
    doi          = {10.48550/arXiv.2601.06932}
}

@dataset{symphonym_v7_zenodo,
    title   = {Symphonym v7 — Universal Phonetic Embeddings for Cross-Script Toponym Matching},
    year    = {2026},
    doi     = {10.5281/zenodo.18682017},
    url     = {https://doi.org/10.5281/zenodo.18682017}
}
```



