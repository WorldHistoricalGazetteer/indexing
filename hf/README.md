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

# Symphonym v8 — Universal Phonetic Embeddings for Cross-Script Toponym Matching

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22767194.svg)](https://doi.org/10.5281/zenodo.22767194)

Symphonym maps toponyms (place names) from **36 writing systems** into a unified
**128-dimensional phonetic embedding space**, enabling direct cross-script similarity
comparison without runtime phonetic conversion or language identification.

> *"London" / "Лондон" / "伦敦" / "لندن" → [0.12, -0.34, …] (all nearby)*

**v8 supersedes [v7](https://huggingface.co/docuracy/symphonym-v7)**, which remains
available unchanged. v8 is a retrain from scratch, not a fine-tune: the vocabulary,
the script coverage and the IPA lineage all differ, so **v8 weights and v7
vocabularies are not interchangeable** (see *Migrating from v7*).

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

🛑 **A similarity score measures the NAME and nothing else.** It carries no
geographic term and cannot separate two places that share a name: Newcastle in
Australia scores exactly as well as the one in England. Anything auto-accepting a
match on this score needs a second, non-name signal.

## Quick Start

```python
from inference import SymphonymModel

model = SymphonymModel()   # loads weights from this directory

sim = model.similarity("London", "en", "Лондон", "ru")
print(f"London / Лондон: {sim:.3f}")

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

model_dir = snapshot_download("docuracy/symphonym-v8")

from inference import SymphonymModel
model = SymphonymModel(model_dir=model_dir)
```

## Model Architecture

Symphonym uses a **Teacher–Student knowledge distillation** framework.

### Teacher (PhoneticEncoder) — training only
- Input: IPA transcriptions via Epitran (+ 102 extensions), Phonikud, CharsiuG2P
- Representation: PanPhon192 — 24-dim articulatory feature vectors,
  8-bin positional pooling → 192-dim fixed-length input
- Architecture: BiLSTM → Self-Attention → Attention Pooling → 128-dim projection

### Student (UniversalEncoder) — deployed model
- Input: raw Unicode characters + script ID + language ID + length bucket
- Vocabulary: **114,845 characters**, **37 script ids** (36 named scripts plus an
  `OTHER` catch-all), **2,438 language codes**
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
| 2 | Student–Teacher distillation: α·MSE + (1−α)·cosine | 50 | α=0.5, Student–Teacher cosine 0.942 |
| 3 | Hard negative fine-tuning (triplet, margin=0.3) | 30 | val_loss 0.02122 |

⚠ **Do not select a checkpoint by training loss.** Four v8 candidates were trained;
the one with the best validation loss at *all three* phases was the worst of the
four on every downstream measure. Phase loss orders candidates within a fixed batch
size and does not compare across recipes. Selection criteria and the four candidate
scorecards are in `models/PROVENANCE.md`.

## Evaluation

### MEHDIE Hebrew–Arabic Historical Benchmark (Sagi et al., 2025)

Independent evaluation on medieval Hebrew and Arabic geographical sources — **not in
training data**. Unweighted mean across the five testsets (137 queries), which is the
benchmark's own convention; the string baselines were re-run in the same pass rather
than quoted, and reproduce their published values to within 0.1.

| Method | R@1 | R@5 | R@10 | MRR |
|--------|-----|-----|------|-----|
| PanPhon192 (ablation) | 41.1% | 48.2% | 52.3% | 45.0% |
| Levenshtein + AnyAscii | 81.5% | 97.6% | **99.4%** | 88.5% |
| Jaro-Winkler + AnyAscii | 78.5% | 96.3% | 97.8% | 86.3% |
| Symphonym v7 | 85.2% | 97.0% | 97.6% | 90.8% |
| **Symphonym v8** | **89.3%** | 97.0% | 98.2% | **92.8%** |

**R@1 +4.1 points over v7, MRR +2.0.** Note the *shape*: R@5 is unchanged and R@10
moves +0.6, while R@1 and MRR move substantially. **v8 is not finding answers v7
could not find — it is ranking the right answer first more often.**

⚠ **Levenshtein still wins R@10** (99.4 vs 98.2), as it did for v7.

⚠ **This is a small benchmark.** +4.1 points on R@1 is about five or six queries
changing rank. The comparison is paired (same queries, both models), which is
stronger than independent samples, but it will not carry more weight than that.

### Cross-Script Pair Validation (11,723 pairs, 179 script combinations)

Systematically sampled from training data (up to 10 pairs per script-pair bin);
this tests embedding retrieval quality over the full 73.5M-toponym index, not
generalisation to unseen sources. 11,535 pairs scored; 188 had an endpoint absent
from the current index.

| Metric | v7 | v8 |
|--------|----|----|
| Pass rate (≥0.75 cosine) | 90.7% | **90.8%** |
| Cyrillic–Latin (n=1,306) | 0.923 | **0.937** |
| Arabic–Latin (n=786) | 0.898 | **0.916** |
| Devanagari–Telugu | 0.976 | 0.975 |
| Hiragana–Katakana | 0.981 | **0.993** |

**Strongest pairs at scale:** Gujarati–Latin 0.957 (n=228), Georgian–Latin 0.947
(n=210), Armenian–Latin 0.943 (n=236), Devanagari–Latin 0.943 (n=681).

## v8 Changes

### 1. The Chinese training signal was contaminated in v7 — this is a correction

v7's card attributed low CJK–Hiragana similarity to "a genuine phonological
mismatch, not a model deficiency". **That explanation was wrong.** v7's IPA for
Chinese Han toponyms was produced under a defective language tag, so the model
learned Chinese characters with **Japanese readings** attached. The similarity
structure that resulted looked like a phonological fact and was an artefact of the
G2P dispatch.

v8 fixes the tag and retrains. CJK–Hiragana mean similarity **falls from 0.437 to
0.320** — and that fall is the *correct* direction: Chinese Han and Japanese
Hiragana genuinely do not sound alike, and v7's higher score was the contamination.

### 2. Script coverage nearly doubled

20 script ids → 37 (36 named + `OTHER`); 113,280 → 114,845 characters;
1,944 → 2,438 language codes.

⚠ **The vocabularies are not additive.** v8 is not v7 plus new entries: ids are
reassigned, and a v8 checkpoint paired with a v7 vocabulary **does not raise an
error — it produces plausible garbage.** See *Migrating from v7*.

### 3. Ranking improved; reach did not

Measured over one fixed 1,053,229-name haystack with 4,843 queries, v7 against v8:

| | v7 | v8 |
|---|---|---|
| recall@1 | 0.0593 | 0.0690 |
| recall@10 | 0.2893 | 0.3176 |
| recall@200 | 0.4792 | 0.4908 |
| MRR | 0.1383 | 0.1557 |

Every metric improved, but **recall@200 rose only +0.0116 while MRR rose +0.0173**.
Three independent measurements — this one, MEHDIE, and order-sensitivity bands —
agree that v8 orders better rather than reaching further.

## Limitations

- **Phonetic similarity only**: no geographic coordinates, semantics or entity
  types. Phonetically similar but geographically unrelated names (Austria/Australia:
  0.883) score highly.
- 🛑 **Discrimination fell slightly.** Separating true pairs from false ones,
  **AUC 0.9324 (v7) → 0.9270 (v8)**. v8 retrieves and ranks better while separating
  marginally worse. If your use is thresholding rather than ranking, measure before
  assuming v8 is an upgrade for it.
- **More than half the correct answers are beyond rank 200.** At recall@200 =
  0.4908 on the corpus above, no amount of downstream re-ranking can reach them;
  that is a property of the embedding space, not of the scoring layer.
- **Training bias**: sources over-represent populated places with official names in
  high-resource languages. Under-represented scripts and mundane places are weaker.
- **Tonal languages**: PanPhon encodes segmental articulatory features but not tone.
- **IPA coverage is partial**: 54% of training-namespace toponyms received an IPA
  transcription; the rest contribute through character-level distillation only.

## Migrating from v7

🛑 **Weights and vocabularies must be replaced together.** Pairing v8 weights with
v7 vocabularies raises no error and silently produces meaningless embeddings.

```
char_vocab.json    16a4d41e868fecc16e1b701363e1bda8   114,845 chars
lang_vocab.json    17ce7fe69a836ddd32c9577cdd60f389     2,438 langs
script_vocab.json  18fd9410e1c543429ec8327a29ee3225        37 script ids
```

**Embeddings are not comparable across versions.** A v7 query vector scored against
a v8 index returns confident nonsense rather than an error. Re-embed the corpus, and
if a client generates query vectors, upgrade the client in the same change.

## Training Data

Trained on 73.5 million unique toponyms from:

| Source | License |
|--------|---------|
| GeoNames | CC BY 4.0 |
| Wikidata | CC0 |
| Getty TGN | ODC-By 1.0 |

## Repository Contents

```
model.safetensors               Student (UniversalEncoder) weights
config.json                     Architecture hyperparameters + measured index stats
inference.py                    Self-contained inference module
requirements.txt                Dependencies
PROVENANCE.md                   Checkpoint lineage, candidate selection, vocab md5s
symphonym-v8.onnx               ONNX export (batch 1) for browser/edge inference
symphonym-v8.onnx.provenance.json
vocab/
  char_vocab.json               114,845-character vocabulary
  lang_vocab.json               2,438 language codes
  script_vocab.json             37 script ids
evaluation/                     MEHDIE, cross-script and candidate-arm reports
training_stats/                 IPA coverage, phase{1,2,3} metrics
epitran_extensions/             102 custom CSV G2P files
```

`config.json` carries `total_toponyms`, `embedding_coverage`, `as_of` and
`measured_from`, each **measured from the live index at publish time** rather than
remembered — an earlier release shipped a hard-coded 66,924,548 at 100% coverage
against a live index of 72,703,777.

## Citation

```bibtex
@misc{symphonym2026,
    author        = {Gadd, Stephen},
    title         = {Symphonym: Universal Phonetic Embeddings for Cross-Script Name Matching},
    year          = {2026},
    eprint        = {2601.06932},
    archivePrefix = {arXiv},
    primaryClass  = {cs.CL},
    url           = {https://arxiv.org/abs/2601.06932},
    doi           = {10.48550/arXiv.2601.06932}
}

@dataset{symphonym_v8_zenodo,
    author  = {Gadd, Stephen},
    title   = {Symphonym v8 — Universal Phonetic Embeddings for Cross-Script Toponym Matching},
    year    = {2026},
    doi     = {10.5281/zenodo.22767194},
    url     = {https://doi.org/10.5281/zenodo.22767194}
}
```

⚠ **The arXiv preprint describes v7**, including the two claims corrected above
(the Chinese contamination, and the weight carried by letter order). A revision is
in preparation. Cite the Zenodo DOI for v8 specifically.
