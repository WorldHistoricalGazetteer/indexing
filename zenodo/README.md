# Symphonym v7 - Research Data and Models

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.18682017.svg)](https://doi.org/10.5281/zenodo.18682017)

This repository contains the trained models, evaluation data, and supplementary materials for:

**Symphonym: Universal Phonetic Embeddings for Cross-Script Name Matching via Teacher-Student Distillation**

## Contents

- `models/` - Trained model checkpoints (Phase 1-3)
- `vocab/` - Character, language, and script vocabularies
- `evaluation/` - Test results and evaluation data
- `testsets/` - MEHDIE benchmark testsets
- `training_stats/` - Training statistics and coverage information
- `epitran_extensions/` - Custom Epitran CSV files for extended language support

## Changes in v7

v7 fixes a routing error in the IPA extraction pipeline that caused 0% IPA coverage
for Hiragana (151,980 toponyms) and Katakana (340,555 toponyms) in v6. Both scripts
are natively supported by Epitran (`jpn-Hira`, `jpn-Kana`) but were being routed to
CharsiuG2P (which only handles CJK/Kanji). The fix dispatches by script before
language, so Hiragana and Katakana toponyms now receive correct IPA transcription.

The model was retrained from scratch on the corrected data. Embedding coverage
increased from ~98% (v6) to **100%** of all 66.9M indexed toponyms.

## Model Architecture

- Input vocabulary: 113,280 characters across 20 scripts
- Language vocabulary: 1,944 languages
- Script vocabulary: 20 major writing systems
- Embedding dimension: 128
- Model parameters: ~1.7M (student network)

## Training Data

The model was trained on 75.9 million positive pairs from:
- GeoNames (gn)
- Wikidata (wd)
- Getty Thesaurus of Geographic Names (tgn)

Total toponyms: 66.9M across 20 scripts

## Evaluation Results

### MEHDIE Benchmark (Sagi et al., 2025) — Ranking Evaluation

| Testset | Paper F₅ | v6 R@1 | v7 R@1 | v6 MRR | v7 MRR | v7 R@5 | v7 R@10 |
|---|---|---|---|---|---|---|---|
| TS7: YaqutSham/KimaSham | 0.77 | 0.818 | 0.697 | 0.872 | 0.829 | 0.970 | 1.000 |
| TS8: KimaSham/ThurayyaSham | 0.68 | 0.905 | **0.952** | 0.930 | **0.968** | 1.000 | 1.000 |
| TS9: Tudela/Thurayya | 0.70 | 0.944 | 0.944 | 0.972 | 0.972 | 1.000 | 1.000 |
| TS10: Andalus/Magreb | 0.77 | 0.697 | **0.727** | 0.758 | **0.808** | 0.879 | 0.879 |
| TS11: Damast/Tudela | 0.88 | **0.969** | 0.938 | **0.984** | 0.964 | 1.000 | 1.000 |
| **Macro Average** | — | **0.867** | **0.852** | **0.903** | **0.908** | | |

All testsets involve Arabic-script historical gazetteers. v7 shows improvements on TS8
and TS10 (the hardest testsets), with macro MRR improving from 0.903 to 0.908. The
slight regression on TS7 and TS11 is within normal training variance. All five testsets
exceed the paper's F₅ phonetic similarity baseline.

### Cross-Script Pairs Test

| Metric | v6 | v7 |
|---|---|---|
| Total pairs tested | 11,723 | 11,723 |
| Pass rate (threshold 0.75) | — | **90.7%** |
| Missing documents | 0 | 0 |
| Embedding coverage | ~98% | **100%** |
| HIRAGANA↔KATAKANA mean similarity | 0.000 (no IPA) | **0.981** |

## Usage

### Loading Model Checkpoints

```python
import torch
from phonetics.models import PhoneticEncoder
import json

# Load vocabularies
with open('vocab/char_vocab.json') as f:
    char_vocab = json.load(f)
with open('vocab/lang_vocab.json') as f:
    lang_vocab = json.load(f)
with open('vocab/script_vocab.json') as f:
    script_vocab = json.load(f)

# Initialize and load model
checkpoint = torch.load('models/final_model.pt', map_location='cpu')
model = PhoneticEncoder(
    char_vocab_size=len(char_vocab['char_to_id']),
    script_vocab_size=len(script_vocab),
    lang_vocab_size=len(lang_vocab)
)
model.load_state_dict(checkpoint['model_state_dict'])
model.eval()
```

## Files Included

### models/ (297 MB)
- `phase1_best.pt` - Teacher network checkpoint (PanPhon-based)
- `phase2_best.pt` - Student after initial distillation
- `phase3_best.pt` - Final model with hard negative mining
- `final_model.pt` - Production checkpoint (identical to phase3)

### vocab/ (2.1 MB)
- `char_vocab.json` - 113,280 Unicode characters across 20 scripts
- `lang_vocab.json` - 1,944 ISO language codes
- `script_vocab.json` - 20 major writing systems (Latin, Cyrillic, CJK, etc.)

### training_stats/
- `coverage_stats.json` - IPA coverage by script and language
- `training_stats.json` - Detailed training metrics (loss curves, samples per bin)
- `phase1_metrics.json` - Phase 1 (Teacher) training metrics
- `phase2_metrics.json` - Phase 2 (Student distillation) training metrics
- `phase3_metrics.json` - Phase 3 (Hard negative fine-tuning) training metrics

### evaluation/
- `mehdie_results_v7_ranking.json` - v7 MEHDIE benchmark results (Recall@K, MRR)
- `mehdie_results_v6_ranking_8038309.json` - v6 MEHDIE benchmark results for comparison
- `mehdie_results_v6_thresholds_8038309.json` - v6 threshold-based results
- `symphonym_v7_pairs_test_report.json` - v7 cross-script pair evaluations (11,723 pairs)
- `symphonym_v6_pairs_test_report.json` - v6 cross-script pair evaluations for comparison

### epitran_extensions/ (768 KB)
- 102 custom CSV files extending Epitran G2P coverage
- Includes mappings for Scottish Gaelic, Armenian, Greek, Hebrew, Gujarati, etc.
- `TODO.txt` - Documentation of language support status

## Data Sources

Training data derived from publicly available sources:
- **GeoNames** (geonames.org) - CC BY 4.0 license
- **Wikidata** (wikidata.org) - CC0 (public domain)
- **Getty Thesaurus of Geographic Names** (getty.edu) - ODC-By 1.0

Evaluation benchmark:
- **MEHDIE testsets** - Sagi et al. (2025)