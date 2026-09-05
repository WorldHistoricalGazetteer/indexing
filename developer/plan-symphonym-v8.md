# Plan — Symphonym v8

> **Status:** 5 September 2026. **Package 1 is COMPLETE and deployed.** What
> remains is the v8 question proper — the model, not its plumbing — and that is
> an agenda of decisions, **not a schedule, and not approved.**
> **Scope:** the phonetic embedding model (`phonetics/`, `hf/`,
> `gateway/symphonym.py`) and the training data behind it. Every figure below
> was measured locally against the shipped `hf/model.safetensors` +
> `hf/vocab/` — the same files the gateway loads — or read out of the code.
> Numbers carry their denominator; inferences are marked as inferences.

---

## 0. The short version

**v7 is not measurably better than Levenshtein**, and the reason is not the
architecture. Three things are wrong, in ascending order of cost to fix:

1. **The gateway tokenises queries differently from the way the index was
   embedded.** Multi-word toponyms retrieve their own indexed vector at rank 1
   only **65.7% of the time** (n=1,486 real names); CJK/Hangul/Kana queries are
   **anti-correlated** with their own documents (cos −0.30 for `東京`).
   **This was a bug, not a design limit, and closing it needed no retraining.**
   ✅ **Done** — Package 1, §5. It is not why you are reading this document.
2. **The teacher's input representation has effective rank 4.37 of 192.** The
   PanPhon 8-bin pooled vector — the thing positives are clustered in and the
   thing the student is distilled toward — throws away almost everything. The
   student inherits it: **effective rank 10.8 of 128**, with the singular-value
   spectrum falling off a cliff at component 20.
3. **The training objective is exhausted.** Phase-1 validation loss is 0.0056
   against a triplet margin of 0.3; phase-3 is 0.021. Almost every triplet is
   trivially satisfied and contributes no gradient.

Findings 2 and 3 are what v8 is *for*, and Package 1 did nothing about them.
They are **still not scheduled**, because the project still has no benchmark that
could resolve whether a retrain helped: the only ranking evaluation is 137
queries, on which v7 (R@1 0.852) is inside the noise band of plain Levenshtein
(0.815) — and v7 is *worse* than v6 (0.867).

🛑 **Independent confirmation arrived 5 Sep, from a consumer rather than from
this analysis.** The GOTW reconciliation project measured Symphonym against 1856
English transcriptions of Qing province names and found the printed forms
resolved to a usable container for **11 of 18**, one of them *wrongly* —
`Keang-su` (Jiangsu) resolved to **GANSU at score 99.5**. Those queries tokenise
**byte-identically** through the old and new paths (measured, 7 of 7), so
Package 1 neither caused nor cured it. A confident wrong answer at 99.5 on a
historic romanisation is finding 2 arriving in production: a rank-≈10 space
cannot separate them, and the confidence scale faithfully reports a match the
geometry cannot support.

§6 sets out the decisions that need taking before any of it starts, and §5.12
what Package 1 leaves behind that makes them cheaper.

---

## 1. Where the code actually is

`phonetics/` carries a **shadowed duplicate layer**: `phonetics/models.py`,
`training.py`, `vocab.py`, `extraction.py`, `inference.py` are all masked by
same-named *packages* and are therefore dead. Python resolves
`phonetics.models` to `phonetics/models/`, never to `phonetics/models.py`.
~2,900 lines of unreachable code that reads as live. This repo has already lost
a session to exactly that failure (`select_h3_cover_geometry`, CLAUDE.md).

The live chain:

```
rebuild_toponyms_index.py   → toponyms + IPA + PanPhon192   (54.0% IPA coverage)
extraction/generator.py     → positive pairs via HDBSCAN over panphon_embedding
                            → phase1/2/3 parquet
training/train.py           → teacher (triplet) → student (MSE+cos distill) → student (triplet)
inference/update_es.py      → ToponymEncoder  ─┐
inference/backfill_*.py     → hf SymphonymModel ├─ THREE encoders, TWO tokenisers
gateway/symphonym.py        → hf SymphonymModel ┘
whg3 browser                → a FOURTH implementation, sends query_vector
```

---

## 2. Finding 1 — the tokenisers disagree

**This is Package 1's whole justification.** No retraining required.

`CharacterVocabulary.encode` (`phonetics/vocab/char_vocab.py`) calls
`preprocess_text`, which romanises CJK via anyascii **and lowercases the
result**, decomposes Hangul to Jamo, NFC-normalises everything else, and maps
`' '` → `<SPACE>` (id 2). **This is the path training used**
(`training/data_loading.py:569`) **and the path `update_es.py` used to embed the
live index** (`inference/encoder.py` → `_prepare_input` → `char_vocab.encode`).

`hf/inference.py::_tokenise` — **the path the gateway serves from**
(`gateway/symphonym.py:239`) and the path `backfill_embeddings.py` writes
from — does none of it. Raw codepoints, no NFC, and `' '` resolves to id
**12588**, a row the training tokeniser can never emit.

Measured, same weights, both tokenisations, cosine between them:

| input | script | cos(indexed, queried) |
|---|---|---|
| `London`, `القاهرة`, `Αθήνα`, `Санкт-Петербург` | single-word | **1.0000** |
| `New York` | one space | 0.9691 |
| `Bury St Edmunds` | two spaces | 0.9429 |
| `トウキョウ` | KATAKANA | 0.1618 |
| `서울` | HANGUL | 0.0118 |
| `北京` | CJK | **−0.2629** |
| `東京` | CJK | **−0.3036** |

Self-retrieval over 5,000 real gazetteer names (MEHDIE corpora), each queried
against a corpus embedded the index way:

| set | n | rank-1 | top-10 | mean self-cosine |
|---|---|---|---|---|
| single-word | 3,514 | 100.0% | 100.0% | 1.0000 |
| **multi-word** | **1,486 (29.7%)** | **65.7%** | 90.0% | **0.9028** |

Read that against the repo's own measurement of the live index — *"the 200
nearest neighbours of anything sit above cosine 0.93"*
(`gateway/es_helpers.py::knn_pass_quality`). A multi-word toponym whose own
document sits at cosine 0.90 **is outside its own top-200 KNN pool**. That is
the documented `Newton with Scales` symptom — "indexed yet never entered the
200-candidate KNN pool" — and its cause is this, not a KNN limitation. The
lexical-exact pass added in place#199 is a correct and worthwhile feature; it
was also masking a bug.

### 2.1 The three divergences, enumerated

Package 1 must close all of these and **nothing else**.

| # | divergence | index path | gateway path | status |
|---|---|---|---|---|
| D1 | CJK/Kana romanisation, Hangul→Jamo, NFC | applied | **not applied** | must close |
| D2 | space → `<SPACE>` (2) | applied | resolves to **12588** | must close |
| D3 | lang tag `.lower().strip()` before lookup | applied | **not applied** | must close |
| D4 | script detection | `script_detection.detect_script` | `hf.inference._detect_script` | **must close** |
| D5 | script-range precedence + table contents | later-entry-wins | *(client)* first-match-wins | **client-side** |
| D6 | `str.isalpha()` tracks the INTERPRETER's Unicode version | index writer 14.0.0 | gateway 13.0.0 | hygiene |
| D7 | **GURMUKHI is absent from the script table** — Punjabi scores `OTHER` | absent, deliberately | a porter would ADD it | **client-side** |

D3 is new and narrow: `LanguageVocabulary.encode` lowercases and strips;
`_tokenise` does a raw dict lookup. The lang vocabulary contains **no** entries
with a subtag (`zh-Hant` is absent from both paths, so both give `<UNK>`), so D3
bites only on tags differing by case alone. Close it anyway — it costs one line
and it is exactly the sort of thing that gets rediscovered expensively.

**D4 was originally recorded here as "verified equivalent — Package 1 need not
touch it". That was WRONG, and the way it was wrong is worth more than the fact.**

The check behind it was real: 0 disagreements over 6,029 distinct MEHDIE
toponyms plus 17 hand-picked controls covering CJK, Hangul, Kana, fullwidth,
Thai, Armenian, Georgian, Hebrew and Greek. The measurement was correct. The
**conclusion drawn from it was stronger than the evidence** — "0 disagreements
on this corpus" was written up as "equivalent", and a caveat that the corpus was
"not exhaustive" sat directly above a sentence telling the next reader not to
look.

Measured against 4,000 **live** names (`indexing-57`, re-verified here
independently): **27 of 4,000 disagree — 0.68%** — and in **27 of 27 the
canonical detector matches the document's stored `script` field, hf's in none**.

Mechanism: `hf.inference._detect_script` counts **every** character, so a space,
a digit and a hyphen all fall to `OTHER` (0x20 and 0x30–0x39 sit below its LATIN
range 0x41–0x7A). `script_detection.detect_script` skips anything that is not
`isalpha()`. A name whose non-letters outnumber its letters therefore classifies
differently — mean non-alpha fraction is **0.495** among the disagreeing names
against **0.054** across the sample.

```
'2038年1月5日の日食'   hf=OTHER  canonical=CJK       stored=CJK
'1995년 칸 영화제'     hf=OTHER  canonical=HANGUL    stored=HANGUL
'マクリーン歴史博物館'    hf=CJK    canonical=KATAKANA  stored=KATAKANA
'Q85423919' 'GR-9408' 'S4630' 'U 221'
                    hf=OTHER  canonical=LATIN     stored=LATIN
```

Those last are **single-word Latin** — the population §5.1 calls unaffected.
Their char ids *are* identical under both paths; only the script id moves, 19
(`OTHER`) → 0 (`LATIN`). So "the control set is unchanged" stayed very nearly
true, which is exactly why this hid.

⚠ **Why the corpus could not have found it, quantified: 0 of 6,013 MEHDIE names
contain a digit (0.00%), against 8.18% of live names.** The disagreement needs a
Wikidata Q-id, a road number, or a date-formatted article title. None exist in
the fixture; they exist in quantity in the index. **The lesson is not "sample
more" — it is that an equivalence claim must derive its boundary from reading
the two implementations, and then go looking for inputs that straddle it.**
Recorded as `~/.claude/memory/equivalence_corpus_must_contain_the_disagreement.md`.

Closing D4 needs **no re-embed**: it moves the gateway onto the script id the
index already holds.

### 2.2 Who is affected, and the one thing that must be measured first

From `coverage_stats.json`: CJK 2,973,525 + HANGUL 393,996 + KATAKANA 340,555 +
HIRAGANA 151,980 = **3.86M documents (5.3% of the index)** where the query
vector is anti-correlated or near-orthogonal to the stored one. The multi-word
share of the live 72.7M index is **not measured** — 29.7% is from the
5,000-name sample and must not be quoted as an index figure until confirmed.

**The complication.** `backfill_embeddings.py` wrote its share of the index
through the *hf* tokeniser while `update_es.py` wrote the rest through the
canonical one. Both stamp `embedding_version` from the same CLI argument, so
**there is no marker in the index saying which encoder wrote a document.**

Do not try to reconstruct provenance from history — ask the structural question
instead (`~/.claude/memory/structural_beats_historical_discriminator.md`):
*does this document's stored vector match what the canonical tokeniser produces
for its name?* That is directly answerable by recomputation, and it is the
question that actually matters.

Consequence for rollout, stated plainly: fixing the gateway **fixes** every
affected document written by `update_es.py` and **regresses** every affected
document written by the backfill, which is currently matching the broken
gateway by accident. Single-word non-CJK names are byte-identical under both
paths and are unaffected either way. So the split must be sized before the
gateway is deployed — see Package 1 step 3.

---

## 3. Finding 2 — the representation is rank-collapsed

**Not scheduled. Background for the decisions in §6.**

Measured on 6,000 distinct real toponyms:

| representation | effective rank (participation ratio) | of |
|---|---|---|
| PanPhon192 (8-bin pooled — the teacher's input **and** the space positives are clustered in) | **4.37** | 192 |
| Symphonym v7 output | **10.83** | 128 |

The v7 spectrum does not taper, it falls off a cliff:
σ1 = 29, σ5 = 23.3, σ10 = 18.6, **σ20 = 0.0254**, σ128 = 5.0 × 10⁻⁹.
Components 20–128 carry nothing. The index stores 128 int8 per toponym across
72.7M docs and **~85% of that storage, and of every HNSW distance computation,
is spent on dimensions that carry no information.**

Located precisely, by hooking activations and by taking the SVD of the weights:

| stage | effective rank |
|---|---|
| `self_attention.q_proj` / `k_proj` (256×256) | 19.1 / 19.3 |
| `pooling.attention.0` (128×256) | **4.54** |
| pooled activations (256-d) | 15.4 |
| `output_proj.3` (128×128), σ_last/σ1 = 6 × 10⁻⁷ | **7.08** |
| final L2-normalised output | 10.8 |

The chain is causal and complete: an input representation of rank 4.4 →
a teacher fitted to it → a student distilled to that teacher (phase-2
student–teacher cosine plateaus at 0.9418) → a phase-3 objective too weak to
expand it. **The 128-d embedding is a rank-≈10 embedding in a 128-d costume.**

Consequence for search: 72.7M items packed into ~10 effective dimensions must
be dense, which is exactly why the 200 nearest neighbours of anything sit above
0.93 and why `Marsails → مارساليس` (0.9878, genuine) sits *below* a junk
ceiling of 0.9881. **The documented conclusion that "no cosine threshold
separates them" is correct, and it is a symptom of rank collapse, not an
intrinsic property of phonetic matching.** Restoring rank is what would make a
threshold exist.

### Negative finding — int8 quantisation is NOT a problem

Worth recording so nobody spends a sprint on it. Measured over 3,000 vectors:
`cos(float32, int8)` mean **0.99971**, worst 0.99961. Per-vector rescaling
(free, since the field is `similarity: cosine`) would raise that to 0.99998 —
an irrelevant gain. Only ~6.2 of 8 bits of range are used (mean max component
0.284 → 36 of 127) and it costs essentially nothing. **Do not touch it.**

---

## 4. Finding 3 — the labels and the objective

**Not scheduled. Background for the decisions in §6.**

### 4.1 The positive-pair label is drawn from the collapsed space

`es_knn_helper.find_similar_in_place` decides which co-attested toponyms count
as a positive pair by running **HDBSCAN over `panphon_embedding`** — the
rank-4.37 vector. The model is therefore trained to agree with PanPhon and
cannot learn any relation PanPhon does not already express.

`generate_pairs.py`'s alternative path is worse: it filters candidate pairs by
`phonetic_similarity()`, which is **anyascii + Levenshtein**, at a threshold of
0.6 same-script / 0.35 cross-script. `London`/`Londres` scores 0.571 and is
rejected. Rejected pairs are then **eligible as hard negatives**, because
`is_adjacent()` only excludes pairs that *survived* the filter — so the model is
actively trained to push apart genuine variants the filter happened to drop.

Measured on real exonym pairs (cosine, v7, raw tokenisation):

```
London ~ Londres  0.4243      Cologne ~ Köln   0.4849
Florence~Firenze  0.5806      Cairo ~ القاهرة  0.2936
```

against a p90 of **0.4085** for random unrelated pairs. Several true variants
score no better than noise. Some of these are genuinely not phonetically close,
and a phonetic model arguably should not match them — but that is a decision to
take explicitly, with exonyms routed to the hard-link overlay, rather than one
made accidentally by a Levenshtein threshold.

The free, correct supervision being discarded: **co-attestation of the same
`place_id`** across independent gazetteers, and the **hard-link overlay**
(`processing/submit_hardlinks_slurm.py`), which already encodes wd↔gn `sameAs`.

### 4.2 The objective is saturated

`phase1_metrics.json` best val loss **0.0056**; `phase3_metrics.json` **0.0212**
— both against `triplet_margin = 0.3` on L2 distance between unit vectors (max
possible distance 2). A mean hinge loss of 0.0056 means the overwhelming
majority of triplets are already satisfied and produce **zero gradient**. All
three curves are flat from about epoch 20. More epochs, more data and a bigger
model all buy nothing while the loss is this shape.

Structural waste: phase 3 runs at `batch_size: 1024` with **one** negative per
anchor. In-batch contrastive training would give 1,023 negatives per anchor at
identical cost. The phase-3 "hard" negatives are static — mined once, from a
**2-character romanised prefix index** — and orthographic prefix negatives are
not phonetically confusable negatives.

### 4.3 Capacity is in the wrong place

`hf/config.json` declares `vocab_size: 113280`. Composition, from
`char_vocab.json` stats: **CJK 93,549 + HANGUL 11,624 = 105,173 rows (92.8%)**.
`generate_vocabulary` enumerates *entire Unicode blocks* for every observed
script, not the observed characters.

At `char_embed_dim: 64` that is **6,731,072 parameters — 81.1% of the model's
8,300,481** — allocated to codepoints the training tokeniser **cannot emit**,
because `preprocess_text` romanises CJK and decomposes Hangul before lookup.
The encoder proper has **1,019,009 parameters**.

*(A row-norm test could not independently confirm those rows are untrained:
norms sit at ~7.97 for every block including ASCII, i.e. the test does not
discriminate. The claim rests on the call chain, which is unambiguous — not on
that measurement.)*

`char_vocab.py`'s module docstring still describes the romanising design while
`generate_vocabulary`'s docstring states the opposite — *"the character encoder
sees native script — no romanization or decomposition"*. Both are in the tree.
The vocabulary followed one, the tokeniser the other. **That contradiction is
the origin of §2.**

**The language vocabulary is polluted.** Of 1,943 entries (excluding `<UNK>`),
only **1,213 (62.4%) are well-formed 2–3 letter tags**. The remaining
**730 (37.6%)** include street fragments (`" Acland St"`, `" Airport Blvd"`,
`" Beale Street"`), 290 entries containing digits, and language *names* written
in their own scripts (`лезгинский`, `ערבית`, `瓦瑞语`). This is an upstream
data-quality problem in the `lang` field of the toponyms, propagated into the
model's conditioning signal. Small in parameters (1,944 × 16 = 31k) but it
means language conditioning is partly noise.

### 4.4 Coverage holes

- **IPA coverage 54.0%** (31,113,585 of 57,593,810 in-training-namespace
  toponyms). `_compute_phonetics_for_batch` does `if not ipa: continue`, so 46%
  of the corpus contributes no teacher signal at all.
- `MIN_BIN_SIZE = 500` **drops** any script:lang bin below 500 samples;
  `MAX_OVERSAMPLE_FACTOR = 3` duplicates small ones with `random.choices`.
  Low-resource languages are excluded, not just under-served.
- Noise augmentation (`apply_character_noise`) is a **QWERTY/OCR model for
  Latin only**. Non-Latin scripts get delete/insert/transpose and no
  substitution. There is **no case augmentation** and **no transliteration
  augmentation** — the single most natural augmentation for a cross-script
  phonetic model.

### 4.5 The evaluation cannot fail

`symphonym_v7_pairs_test_report.json` samples known positive pairs and checks
they clear 0.65. **There is no negative control.** In a space where random pairs
reach 0.93, a 100% pass rate is uninformative. This is the standing pattern in
`~/.claude/memory/a-check-that-cannot-fail.md`: an assertion of presence with no
absence in the same call.

The one benchmark that does discriminate — MEHDIE ranking, `n = 137` queries
over 5 testsets:

| method | mean R@1 | mean MRR |
|---|---|---|
| PanPhon192 (teacher's own space) | 0.411 | 0.450 |
| Jaro-Winkler | 0.785 | 0.863 |
| **Levenshtein** | **0.815** | **0.885** |
| Symphonym v6 | 0.867 | 0.903 |
| **Symphonym v7** | **0.852** | **0.908** |

Binomial SE at n=137 is ≈3.1pp. v7, v6 and Levenshtein are **statistically
indistinguishable**, and v7 is nominally *below* v6 on R@1. The five testsets
are all Arabic/Hebrew/Latin historical gazetteers — there is no CJK, Indic or
Cyrillic evaluation at all, which is precisely where §2 shows the model is
broken.

---

## 5. PACKAGE 1 — make every tokeniser agree with the index as it stands

> ✅ **COMPLETE, 5 September 2026.** All six steps closed. Gateway deployed and
> verified byte-exact; 100,960 of 72,703,777 documents re-embedded, 0 errors;
> whg3 client shipped and verified by table diff. Kept in full because the
> *method* is reusable and four of the findings below are open work for v8.

### 5.1 Scope discipline

The canonical behaviour is **exactly what `CharacterVocabulary.encode` does
today, bit for bit**. This package changes *no policy*:

- **No** NFKC. **No** casefolding. **No** change to the CJK romanisation choice.
- **No** change to script detection (verified equivalent, §2.1).
- **No** change to the vocabulary.

Every one of those would change what a *correct* index contains and therefore
force a re-embed of all 72.7M documents. They are §6 decisions, not this
package. **If a change would alter the token ids of a single-word Latin name, it
does not belong in Package 1.**

### 5.2 The work, and how each step ended

1. **`phonetics/tokenise.py`** — one canonical implementation, no dependency on
   torch or on the model, so every caller and every test can import it. It must
   reproduce `CharacterVocabulary.encode` + `LanguageVocabulary.encode` +
   `ScriptVocabulary.encode` exactly, closing D1/D2/D3.
2. **Rewire all four Python call sites** to import it:
   `phonetics/inference/encoder.py`, `phonetics/inference/backfill_embeddings.py`,
   `hf/inference.py`, `gateway/symphonym.py`. `hf/inference.py` ships to
   HuggingFace and must stay self-contained — vendor the function into it and
   have the contract test assert the two copies agree, rather than adding a
   repo import it cannot resolve.
3. ✅ **DONE 5 Sep — the split is measured, and it clears the gateway to deploy
   first.** 4,000 live documents sampled from prod, stratified by script, each
   recomputed both ways against its stored vector. Structural, not historical:
   all 4,000 carry `embedding_version: 7`, so **there is no provenance marker to
   read** and attribution had to come from recomputation.

   **Positive control passed** — 574 docs tokenise identically under both
   encoders and so carry no provenance signal; they reproduce the stored vector
   at mean cos **0.99971**, min 0.99963, exactly the independently measured int8
   quantisation floor. The local checkpoint is therefore the one that embedded
   the index. Had this failed the result would have been discarded, not reported.

   ```
   script      discriminating  canonical  gateway   both  NEITHER
   LATIN                  926        924        2      0        0
   CJK                    750        750        0      0        0
   HANGUL                 750        750        0      0        0
   KATAKANA               500        500        0      0        0
   HIRAGANA               500        500        0      0        0
   TOTAL                 3426       3424        2      0        0
   ```

   Backfill-written share: **2 of 3,426 = 0.058%**, exact Clopper-Pearson 95% CI
   **[0.007%, 0.211%]**. CJK+Kana+Hangul: 0 of 2,500, 95% upper bound 0.147%.
   Multi-word Latin: 2 of 926 = 0.216%, CI [0.026%, 0.778%].

   ⚠ **The `NEITHER` column is 0 across the board** — every discriminating
   document is explained by one of the two encoders, so there is **no third
   vector population** in the index. That was the main risk and it is excluded.

   The 2 backfill-written docs are `Rozlazłów - część` and `Jardim do Calvário`
   — both multi-word Latin, both already NFC, so the space token is the only
   thing separating them.

   *Two threshold errors made and corrected in reaching this, recorded so the
   post-fix re-run does not repeat them: classifying by "whichever cosine is
   higher by 0.02" produced 177 spurious "ambiguous" docs that were all
   canonical (0.9996 vs 0.982, a gap just under an arbitrary margin) — the right
   discriminator is the control-derived quantisation floor, because there is no
   genuine middle band; and a 2-event count needs an exact binomial interval, not
   the rule of three, which is for zero events.*
3b. ✅ **MEASURED 5 Sep — the candidate set is 46.5M docs (63.9% of the index),
   an order of magnitude larger than this plan previously implied.** Per-script
   `terms` agg complete (`sum_other_doc_count=0`, buckets sum exactly to
   72,703,777). Measured **twice, independently**: this session at n=800/script
   (union 47.38M) and `indexing-13` at n=10,000/script (union **46,483,973**,
   CI [45,912,424 – 47,055,523]). **Use the n=10,000 figure** — 12.5x the power,
   and it falls inside the n=800 interval. Two samples, two seeds, agreeing to
   1.9%.

   | | docs | 95% CI |
   |---|---|---|
   | ~~Candidate, OLD predicate (D1/D2 only)~~ | ~~46,483,973 (63.9%)~~ | superseded |
   | **Candidate, CORRECTED predicate (incl. D4)** | **~50.1M (~69%)** | measured in-run |
   | — of which D1 scripts, exact from the agg | 4,169,618 | exact |
   | — space-bearing or non-NFC, other 16 scripts | 42,314,355 | 41.74M – 42.89M |
   | **Not a candidate — bit-identical, untouched** | ~26,220,000 (36.1%) | |

   Overlap is real but small — the naive sum overcounts by 160,058, because CJK
   names rarely contain spaces (CJK 2.12%, Hiragana 0.84%). The **`OTHER` bucket
   (395,409 docs, 0.54%) was checked directly** rather than left as a caveat:
   0.97% carry CJK-family codepoints, but **96 of 97 are already counted under
   the space rule**, so rule-(a) leakage adds **~40 documents (95% CI 1–220)**
   and does not move the union. `OTHER` is mostly Myanmar, Gurmukhi, Tibetan,
   Sinhala, Khmer, Ol Chiki, Tifinagh and Ethiopic. **Non-NFC is a
   rounding error: 9,031 docs (0.012%)**, zero in 10,000 for fourteen of twenty
   scripts. Do not engineer for it separately.

   ⚠ **Two figures elsewhere in this document were WRONG and are corrected here.**
   * The space-bearing share is **58.41% index-wide** and **62.84% of Latin**
     (6,284/10,000, CI [61.88%, 63.79%]), not the **29.7%** quoted from the
     MEHDIE corpora — **the fixture understates the live rate by ~2x.** §2.2
     flagged 29.7% as "not measured"; it is now measured, and the MEHDIE figure
     should not be quoted even as a rough guide.
   * The D1-script population is **4,169,618**, not **3,860,056**. The old
     number came from `coverage_stats.json`, which describes a **66,924,548**-doc
     index generation — a stale source this document's own §9 flags for
     `hf/config.json` and then reused for a live figure.

   ⚠ **And the framing "no reindex of the majority" was wrong.** Single-word
   non-CJK names *are* bit-identical — but they are only **34.8%** of the index,
   not the majority. What survives is the narrower and still-decisive claim:
   the re-embed set is not the candidate set (see step 4).

4. ✅ **DONE — 100,960 documents rewritten, 0 errors** (`indexing-57`). Counts,
   not extrapolation, over all 72,703,777 examined:

   | stratum | changed | of examined | rate |
   |---|---|---|---|
   | multi-word | 99,767 | 42,408,064 | 0.2353% |
   | `control` *(D4 names under the stale label, §5.2d)* | 527 | 26,120,402 | 0.0020% |
   | CJK | 428 | 3,240,684 | 0.0132% |
   | KATAKANA | 49 | 358,111 | 0.0137% |
   | HANGUL | 41 | 416,894 | 0.0098% |
   | HIRAGANA | 0 | 153,929 | 0 |
   | **not-NFC** | **148** | **5,693** | **2.5997%** — worst by rate |
   | **TOTAL** | **100,960** | **72,703,777** | **0.1389%** |

   Positive control **22,623,343 rows, min pass rate 0.999957, zero failing
   shards**. Write verified by independent read-back, **300 of 300 across 12
   random shards**.

   🛑 **5,875,266 documents differed by exactly ONE int8 step and were NOT
   rewritten.** Without a materiality threshold the write would have been **58×
   larger and almost entirely quantisation noise**. Evidence it is set right: of
   231 rewritten rows in one shard, **zero** sat above cosine 0.999 — if noise
   were leaking through that band would be crowded; it is empty.

   ⚠ **And the verification would have caught its absence.** The no-threshold
   run projected **380,217** changed — above this document's own worst-credible
   ceiling of ~352,000 — so it would have tripped the independent
   stop-and-investigate rather than passing quietly. Both halves are worth
   keeping: the guard was necessary, *and* the check that would have caught a
   missing guard worked. A defect the reviewer would have caught is the only kind
   whose absence you can measure.

   *Estimation history, for the record: this quantity was predicted at 5,700 →
   26,960 → 113,646 → ~64k–91k. The answer is 100,960. Every revision moved on
   arithmetic rather than evidence, and the run measured what four estimates
   could not.*

   **Authorisation, recorded because a decision nobody can point to is a decision
   that gets reconstructed:** SG authorised twice and explicitly — dry run, then
   a question about whether `--execute` used the safest mode (which prompted the
   resumability, canary, error-ceiling and read-back work), then **"run the
   canary"**, then a reported result of 2,142 documents at 0 errors with a 20-of-20
   read-back, then **"run the rest"**. The intermediate stop is the point: it made
   the second authorisation a decision rather than momentum.

   🛑 **"THE RE-EMBED SET" HAS BEEN USED IN TWO INCOMPATIBLE SENSES ACROSS THIS
   CAMPAIGN — they differ by four orders of magnitude. Read this before acting on
   any number.**

   * **Candidate set — 46,483,973 docs.** Tokenises differently under the two
     encoders. This is what the **gateway fix** serves. **It does NOT need
     re-embedding**: ~99.94% already holds the correct canonical vector.
   * **Re-embed set — ~113,600 docs.** The subset *within* it whose stored vector
     was written by the **backfill** and is therefore wrong. **Estimated by
     stratum, not pooled** (see below):

     | stratum | population | observed | rate | → docs |
     |---|---|---|---|---|
     | D1 scripts (CJK/Kana/Hangul) | 4,169,618 | 0 / 2,500 | 0% | 0 *(≤6,147)* |
     | space-bearing / other scripts | 42,314,355 | 6 / 2,234 | 0.269% | **113,646** |
     | **total** | 46,483,973 | 6 / 4,734 | | **~113,600** *(42k–253k)* |

   ⚠⚠ **113,646 IS ITSELF UPWARD-BIASED — see the correction immediately below.
   Treat it as a budgeting ceiling input, not an expectation.**

   ⚠ **This supersedes ~26,960, which was this session's second arithmetic error
   on the same quantity.** That figure pooled 2/3,426 across strata — mixing 2,500
   CJK observations at a rate of zero into a Latin-and-other rate about five times
   higher, diluting it ~4.2×. **The re-embed set is stratified by construction
   (D1 docs diverge for a different reason than space-bearing ones) and must be
   estimated that way.** Same class of error as the pooled/stratified trap, one
   quantity later.

   ⚠ **Provenance, since this quantity has now been wrong three times, all mine:**
   "~5,700" = 3.86M × 0.147%, a D1-only population against a doc count from a
   stale index generation. "~26,960" = the right population, pooled across strata
   that have very different rates. **"~113,600" is the current figure and is
   stratified.** "46.5M" is a different set entirely — the candidate set, not the
   re-embed set. **A 113,600-doc job and a 46.5M-doc job are not the same
   operational proposition, and the labels made them read alike.**

   🛑 **FOURTH CORRECTION, AND THE LAST ONE WORTH MAKING — 113,646 is biased
   upward, and the right response is to STOP ESTIMATING.**

   The 6 events were pooled across samples with different designs. `gate_sample`
   (2/926) is random with respect to lang. `langlead` (4/674 no-lang + 0/634
   has-lang) is **stratified 50/50** — but the live population is **25.5% no-lang
   / 74.5% has-lang**, and no-lang carries the higher rate. Pooling therefore
   over-weights the high-rate stratum:

   | basis | rate | → docs |
   |---|---|---|
   | pooled 6/2,234 *(what the table above says)* | 0.269% | 113,646 |
   | `langlead` re-weighted to the true lang mix | 0.151% | **64,049** |
   | the random sample alone, 2/926, no bias at all | 0.216% | **91,391** |
   | **worst credible, for budgeting** | | **~352,000** |

   ⚠ **And an extrapolation nobody has measured: all 6 events are LATIN.** The
   rate for Cyrillic, Arabic, Greek, Hebrew, Devanagari and the rest — ~8.1M of
   the 42.3M population — is assumed equal to Latin's with **zero observations**
   behind it.

   **So: not a floor, not a clean centre — an upward-biased point estimate on a
   weakly-identified interval resting on 6 events, all from one script.** Size
   the run for ~352,000 and treat a result above that as a signal to stop and
   investigate rather than to keep writing.

   🛑 **Do not correct this number a fifth time.** Five versions — 5,700 →
   26,960 → 113,646 → ~64k–91k centre → ~352k ceiling — and **every one moved on
   arithmetic, not on new evidence.** The run itself measures the true value with
   real denominators across every script, which is precisely what `--scope all`
   buys and why that design decision closes the Latin-extrapolation gap as a side
   effect. **The estimate's only remaining job is to size a budget and set a
   stop-and-check threshold. It has done that. Let the run answer the question.**

   🛑 **A NEGATIVE RESULT WORTH KEEPING: "no lang" does NOT let us shrink the
   compute.** Both originally-found mis-stored docs had an empty lang tag, and
   there is a plausible mechanism (`update_es.py` computes from DuckDB, the
   backfill from ES, so a doc with no lang could be missed by the first and swept
   by the second). Tested on 1,308 multi-word Latin docs: **no-lang 4/674
   (0.593%, CI [0.162%, 1.513%]) vs has-lang 0/634 (0%, CI [0%, 0.580%])**.
   Has-lang's upper bound **exceeds** no-lang's point estimate, so has-lang cannot
   be excluded. The flagged docs also span four namespaces (`whg`, `osm`, `gn`,
   `gn`), so namespace is no filter either, and `indexed_at` is identical for a
   mis-stored doc and its correctly-stored neighbours. **There is no cheap
   discriminator: the full 46.5M compare pass is required.**

   Method (compute is unavoidable, the write is not): embed all ~46.5M candidates
   through the canonical tokeniser on GPU via Slurm, compare each against its
   stored vector, and **write back only where they differ**, reporting how many
   of how many. Identifying the divergent set requires the same embedding pass as
   re-embedding it, so there is no cheaper route — but the ES write drops from
   46.5M bulk updates to ~114k.

   The same pass re-embeds the affected candidate set through the canonical tokeniser,
   whichever encoder wrote it: script ∈ {CJK, HIRAGANA, KATAKANA, HANGUL}
   (3.86M docs) plus names containing a space (share of the live index still to
   be measured) plus names not already in NFC. Compare stored vs recomputed and
   write back only where they differ, reporting how many of how many.
   Single-word non-CJK names must come back byte-identical — that is the
   package's own correctness check.
5. **Deploy the gateway — FIRST, and the larger candidate set strengthens the
   case rather than weakening it.** Across the 46,483,973 candidates, deploying
   fixes ~46,370,000 and regresses ~113,600 (95% upper bound ~253,000). The re-embed of step 4 then closes
   the remainder and is **no longer on the critical path** — it can follow at
   leisure rather than gate the deploy.
6. **Flag the browser.** The whg3 Gazetteer Workbench computes its own
   `query_vector` (`developer/handoff-reconcile-query-vector.md`) and is a fourth
   implementation, outside this repo. It must adopt the same rules or its
   vectors will diverge from the fixed gateway. Until it does, consider having
   the gateway ignore a client `query_vector` for affected scripts.

### 5.3 Tests

- **Contract test:** token sequences byte-identical across all entry points, over
  a fixture covering every script in `included_scripts`, plus single/multiple
  spaces, leading and trailing whitespace, mixed case, NFC/NFD pairs, fullwidth
  forms, empty and single-character input, and lang tags differing only by case.
- **Prove it discriminates.** Run it against the pre-change code and confirm it
  *fails* before trusting a pass —
  `~/.claude/memory/feedback_measure_must_discriminate.md`.
  ⚠ Extract that copy with `git archive HEAD | tar -x -C <scratch>`, **not**
  `git stash`. Sessions share one working tree
  (`~/.claude/memory/concurrent_sessions_share_one_worktree.md`) and a stash
  would take another session's uncommitted work with it.
- **Regression test:** single-word Latin/Cyrillic/Arabic/Greek names produce
  identical ids before and after. This is what guarantees no reindex of the
  majority.
- ⚠ Run tests package-qualified (`python -m unittest tests.test_x`) or with
  `discover -s tests -t .`. **Never** `discover -s tests` — see CLAUDE.md.

### 5.4 Exit criteria — measured, not asserted

Against the 4,000 live stored vectors rather than against the new code: query
side = `hf/inference.py` (HEAD copy for *before*, working tree for *after*),
index side = the int8 vectors as they sit in prod. Rank-1 self-retrieval:

| stratum | before | after |
|---|---|---|
| all | 33.3% | **100.0%** |
| single-word | 20.2% | 100.0% |
| multi-word | 66.7% | 100.0% |
| **CJK / Kana / Hangul** | **0.3%** | **100.0%** |
| Latin single-word *(control)* | 100.0% | 100.0% |

Mean self-cosine after: **0.9997** in every stratum — the quantisation floor, so
the query vector now *is* the stored vector to within quantisation. The
multi-word 66.7% reproduces this session's 65.7% MEHDIE figure on live data.

🛑 **CJK/Kana/Hangul was 0.3% rank-1 and 8.5% top-200 before the fix. Those
3.86M documents were not mis-ranked — they were unreachable.**

### 5.5 Exit criteria as written before the run

- Contract test passes, and demonstrably fails against pre-change code.
- Multi-word self-retrieval rank-1 **≥ 99%** on a held-out real-name corpus, up
  from 65.7%.
- `北京`~`Beijing`, `서울`~`Seoul` and `トウキョウ`~`Tokyo` all match their own
  indexed documents.
- Step 3's split is measured and recorded with its denominator.

---

### 5.6 D5 and D6 — divergences found by the CLIENT, after D1–D4 closed

Both were found by `whg3-da` porting the canonical block to JavaScript — by
someone re-implementing it rather than reading it. Neither is closed.

**D5 — the script table's precedence is load-bearing, and hides a defect.**
`_build_codepoint_map` is **later-entry-wins**; a naive port is first-match-wins.
Exactly one block overlaps — `FB00–FB17`, Hebrew presentation forms shadowed by
the Armenian ligatures — so **`ﬁ` (U+FB01, a *Latin* ligature) scores
`ARMENIAN`.** That is wrong, and **none of the 27 golden cases reach it**: a
mutated first-match-wins port still passed 27/27 while failing 835 of a
15,853-case differential. Add an `FB00–FB17` case to the fixture. Do **not**
"fix" the precedence — the index was written with this behaviour, so *correct*
means *identical*; changing it is a v8 question with a re-embed attached.

**D6 — `str.isalpha()` is a property of the interpreter, not of our code**, and
D4's filter depends on it:

| | python | unicodedata |
|---|---|---|
| golden fixture (this session's laptop) | 3.10.12 | 13.0.0 |
| **the gateway** (pitt) | 3.9.25 | **13.0.0** |
| **the index writer** (CRC conda `whg`) | 3.11.13 | **14.0.0** |

Enumerating every codepoint under both: **515 are alphabetic to the index writer
and not to the gateway** (zero the other way); `strip()`/whitespace is
**identical**, so that rule is safe. The 515 are all Unicode 14 additions —
Cypro-Minoan 97, Tangsa 79, Vithkuqi 70, Latin Ext-G 31, Arabic Extended-B 30,
Toto 30, Ethiopic Ext-B 28, Old Uyghur 18, other 132. **Zero of 5,307 sampled
live toponyms contain any** — a denominator, not a proof, across 72.7M documents.

✅ **D6's PRACTICAL impact on the live corpus is ZERO, measured 5 Sep: 0 of
200,000 real names classify differently under 13.0.0 vs 14.0.0** (`indexing-13`).
That supersedes this session's 0-of-5,307. The 515-codepoint divergence is real
and the mechanism below is correct — but it is **hygiene, not a live defect**.
Do not delete this section: it is the reason the re-embed pins its interpreter,
and a pin whose justification has been deleted is a pin someone removes.

⚠ **A D6 test only discriminates if the codepoint is alphabetic at 14.0.0 AND
inside a named script range.** The obvious choice — a newly-added codepoint such
as U+0870 — is **inert**: Arabic starts at 0x08A0, so U+0870 falls outside every
range and resolves to `OTHER` whether the alpha filter counts it or not. The
golden fixture shipped exactly that case, complete with a warning and the
interpreter table, and a `\p{L}` mutation still passed all 33 cases. Documentation
that looks like a test is worse than either alone. The discriminating codepoints,
measured against the real 14.0.0 alpha set from the CRC env:

```
U+9FFD  OTHER -> CJK       ids [37574] -> [1]   moves script_id AND char_ids
U+08B5  OTHER -> ARABIC    ids unchanged        moves script_id only
U+A7C0  OTHER -> LATIN     U+0C5D -> TELUGU     U+0CDD -> KANNADA
U+0870  OTHER -> OTHER     ids [1] -> [1]       moves NOTHING  <- inert
```

U+9FFD is the one to use: landing in CJK switches romanisation on, so it
exercises the **D6→D1 interaction**, which is where the damage is rather than
where the mechanism starts.

⚠ **The fix is the one whg3 already applied: freeze the alpha table in code
rather than calling the runtime's classifier.** Until then every process that
tokenises must pin its interpreter — the re-embed's GPU compute must run under
the index writer's 14.0.0, asserted per shard alongside the tokeniser SHA, or a
mixed-Unicode array produces artefact "differences" and writes them back.
Freezing the table changes token→script mapping and implies a further re-embed,
so it is a **Package 1 follow-up**, not a mid-flight change.

### 5.7 D7 — the trap of a table that looks incomplete

**`GURMUKHI` is not in the canonical script table, and must not be.** Verified
against the working tree:

```
19 scripts: ARABIC ARMENIAN BENGALI CJK CYRILLIC DEVANAGARI GEORGIAN GREEK
            GUJARATI HANGUL HEBREW HIRAGANA KANNADA KATAKANA LATIN MALAYALAM
            TAMIL TELUGU THAI          GURMUKHI: ABSENT

'ਅੰਮ੍ਰਿਤਸਰ' Amritsar -> OTHER        'दिल्ली' Delhi -> DEVANAGARI
'ਲੁਧਿਆਣਾ'  Ludhiana -> OTHER        'ঢাকা'  Dhaka -> BENGALI
```

⚠ **D7 is not "Gurmukhi was never there" — it is "Gurmukhi WAS in the legacy
table and the canonical table dropped it".** `hf/inference.py` at `bb50f38`
line 179 carried `("GURMUKHI", [(0x0A00, 0x0A7F)])`. The canonical table has 19
scripts and no such entry. whg3's pre-fix table carried it too — a faithful copy
of the legacy one, not an independent mistake.

🛑 **The consequence is a second, independent defect that the tokeniser rewrite
repaired BY ACCIDENT.** The legacy lookup was `script_to_id.get(name, 0)` — and
**0 is LATIN's id, not a sentinel**. So a script the DETECTOR could name but the
20-entry VOCABULARY could not represent silently became **Latin**:

```
legacy    detect('ਅੰਮ੍ਰਿਤਸਰ') -> 'GURMUKHI' -> .get('GURMUKHI', 0) -> 0  == LATIN
canonical detect('ਅੰਮ੍ਰਿਤਸਰ') -> 'OTHER'    -> encode_script       -> 19 == OTHER
```

Twelve prod documents were identified as backfill-written by matching a
script-id-0 recomputation at **cosine 1.0000** — an identification, not a
similarity. Nothing in Package 1 was looking for this; it surfaced only because
`--scope all` embedded 26.2M documents nobody thought needed embedding and twelve
of them refused to behave. **The lesson is not that the rewrite was good — it is
that the corpus-wide census was.**

✅ **No un-rewritten population remains, verified 5 Sep: 237 of 237 real
Gurmukhi-bearing documents from the live index match canonical `OTHER(19)`; zero
match legacy `LATIN(0)`.** Structural, not lucky — `--scope all` examined every
document, and a script-id change is far above the one-int8-step noise band, so
none could have slipped past.

⚠ **The trap remains for re-implementers. `GUJARATI` is in the table and
`GURMUKHI` is not**, so a porter reads the absence as a bug and adds Punjabi.
The corpus was embedded with Gurmukhi scoring `OTHER`, so *correct* means
*identical*, as with D5.

⚠ **Only a direct table comparison detects it, and the reason is an asymmetry
worth internalising:**

* a script **missing** from the range table is behaviourally detectable — it has
  a vocab id, so text moves from `THAI(12)` to `OTHER(19)`;
* a script **extra** in the range table is **invisible** — it has no vocab id, so
  the fallback lands on `OTHER`, which is where it already was.

🛑 **Two proposed mutation tests for D7 were checks that could not fail**, and
both were proposed by people spending the day telling each other to prove a guard
fires. "Add GURMUKHI to a copy of the table and confirm the Punjabi cases fail"
produces **0 of 6,271 mismatches** — measured — because the vocabulary has no
GURMUKHI key, so the id is `OTHER` either way. Use the table diff; it is proven
to fire in both directions (`+GURMUKHI` → extra, `−THAI` → missing).

⚠ A differential corpus is also blind here, but by a subtler route than "it
generates from its own table": whg3's generated strings and ran them through the
*canonical* Python — its **character pools simply contained zero Gurmukhi**,
0 of 15,853 and 0 of 35. Seed such pools from the **union of both tables**.

*(Naming: `indexing-57` first called this "D5". D5 was already the FB00–FB17
precedence defect and D6 the interpreter Unicode version, so it is **D7**.)*

### 5.8 Two hazards found while porting

**H1 — the out-of-range-id rules agree by coincidence, not by construction.**
The vocab file carries 7 characters whose ids are ≥ `len(char_to_id)` (113,280):
`'̿'`→113280 and six others. Both paths already degrade them to `<UNK>`, but by
different rules reading different sources — `CharacterVocabulary.get_char_id`
tests `cid >= len(self.char_to_id)` (a property of the **vocab file**), while
`hf/inference.py::_sanitize_vocab_ids` clamps against
`char_embed.num_embeddings` (a property of the **checkpoint**). They agree only
while those two numbers are equal, and today they are, at 113,280.

⚠ **Forward hazard:** decision D-D proposes shrinking `char_embed` to the ~8,000
emittable characters. A v8 checkpoint with a trimmed table against an untrimmed
vocab file makes these two rules disagree **silently**, on exactly the rare
characters no fixture covers. Package 1 reproduces the canonical rule; whoever
takes D-D must make the two read one source.

**H2 — whitespace-only input crashes the encoder, and Package 1 widens it.**
Measured against today's unfixed code: `embed("")` already raises
`RuntimeError: Cannot pack empty tensors`, and a batch containing one empty item
raises `Length of all samples has to be greater than 0` — **one bad item poisons
a whole batch**. Package 1 widens the crash from `""` alone to any whitespace-only
input, because the canonical encoder *drops* non-`U+0020` whitespace where the
gateway path emitted an `<UNK>`:

```
""      canonical []       gateway []        already crashes on both
"\t"    canonical []       gateway [1]       OK today, crashes after
"\n"    canonical []       gateway [1]       OK today, crashes after
"\u00a0" canonical []      gateway [12589]   OK today, crashes after
" "     canonical [2]      gateway [12588]
```

**Guard it, in the shared tokeniser, inside Package 1**: return `[UNK_ID]` when
the id list is empty — `UNK`, not `SPACE`, because it means "input I cannot
represent". It does **not** breach §5.1's scope rule: a guard that fires only on
an empty result cannot alter the ids of any input producing ≥1 id, so it cannot
touch a single-word Latin name and cannot force a reindex. Nor can it mismatch
the index: `update_es.py` filters `name IS NOT NULL AND TRIM(name) != ''`, so no
indexed document is whitespace-only. *(Caveat: DuckDB's `TRIM` strips spaces
only, so a tab-only name would have survived that filter and crashed the run —
since the rebuild completed, empirically none exist.)*

Keep the deviation honest with two tests: canonical `==` `tokenise()` for every
input producing a non-empty result, and a separate test pinning the documented
deviation on empty-result input. Add `U+00A0` to the fixture — it diverges
*inside* a real name, not only standalone.

### 5.9 A stratum label that outlived its predicate

When **D4** names (digits/punctuation dominant) became candidates, `is_candidate`
was updated and `stratum_of` was not. So a single-word, already-NFC, non-romanised
name carrying a digit or hyphen — `SO-10731`, `SZ-1555`, `SMX-28308`, route and
parcel codes — is a **candidate** by the predicate while landing in the bucket
labelled **`control`**, whose whole meaning is "cannot change".

This surfaced as 184 rewritten rows in a stratum that should be zero by
construction, which is exactly the signal the verification was built to treat as
an immediate fail. **The counts were never wrong; the label was.** Two independent
routes proved it: `changed_non_candidate` is **0** across every shard, so every
changed document is a candidate; and an exhaustive name-level sweep found
**184 of 184 are D4 names, 0 ligatures, 0 unexplained**.

Three lessons worth more than the fix (`cc47c67`, a separate `punctuated`
stratum plus a test tying the stratum to the predicate so they cannot drift):

* **A bucket name is an assertion, and it decays silently.** Nothing failed when
  the predicate moved and the label did not — the data stayed correct and only
  the *description* went stale, which is the form no test catches.
* **Two candidate-set figures were in circulation for the same reason.** The
  46.5M in this document is the OLD predicate; the corrected one is ~50.1M, D4
  adding ~3.6M. A number computed before a predicate changed keeps being quoted
  after.
* ⚠ **Do not project the total from completed shards.** The change rate is not
  flat — 72.59 per 100k for shards <80 against 85.67 for shards ≥80, an 18%
  difference — and the completed set is `0..159 with gaps`, not a random subset.
  Candidate share is stable across the same shards, so it is not obvious
  composition drift. **The census must be a count, not an extrapolation**, and
  this session's ~74,300 projection is withdrawn.

### 5.10 Measurements that confirm the assumption they were made under

Three near-misses today shared a shape more dangerous than a check that returns
nothing: each **returned a value**, and a value is far harder to doubt than a
blank.

* **A probe default mistaken for the code's.** This session "verified" the
  script-id fallback by running `s2i.get(name, 0)` and reported that `GURMUKHI`
  resolves to 0 and `OTHER` to 19, therefore they differ. **The `0` was the
  probe's own default, not the code's** — which is `get(SCRIPT_OTHER, 0)`, i.e.
  19. It happened to reproduce the *legacy* behaviour exactly, so it read as
  confirmation of the very thing it had assumed, and it nearly overturned a
  correct finding *with a measurement attached*.
* **A grep's union read as a partition** (`whg3-5b`, its own report).
* **A caption believed over its own output.** Checking whether the legacy table
  contained `GURMUKHI`, this session wrote a "nothing above = it doesn't" caption
  beneath the grep — and the grep had printed the entry immediately above it.
  The cheapest failure of the day and the least excusable: the evidence was on
  screen.

The common defence is not more care. It is to make the check's *default* and its
*subject* impossible to confuse — read the source rather than reconstruct it, and
never write the interpretation of a command's output above the output itself.

### 5.11 Verification design — lessons that cost nothing to keep

**A ratio-preserving failure is invisible to any ratio-based check.** The
re-embed's expected-count band (40k–350k changed) cannot detect a truncated run.
Demonstrated on a fixture by `indexing-13`: **156 of 256 shards missing,
28,326,173 documents never examined — and the changed count sat comfortably
inside the band**, because truncation scales `examined` and `changed` together
and preserves their ratio. Only **set equality on the full shard range** and the
**absolute denominator** see it. The array is `0-255%100`, so a wave boundary is
exactly where a truncated run looks tidiest — the fixture's first missing shard
was 100, an exact multiple of the wave size.

Corollary for the ledger: `shard_id` set equality is *still* not enough on its
own, because a requeued shard that completed **twice** inflates the examined
denominator while its changed count stays correct — the run then reads *more*
complete than it is. The ledger must carry an **attempt counter** and a
**per-shard `tokeniser_sha256`**, both written at the time. Neither can be
reconstructed afterwards, and without the SHA a mixed-tokeniser run is
undetectable: its per-shard totals are perfectly consistent and its output is
worthless.

**Knowing about a fault is not protection against it; only a mechanical guard
is.** The `sha256`-of-nothing pipe fault (§9,
`~/.claude/memory/hash_of_nothing_is_a_valid_hash.md`) **recurred inside the
harness built to catch it, hours after it was written up, in the hands of the
person who found it** — exit codes checked through a `grep` pipe, so `$?` was
grep's and all three *failing* fixtures reported success. `set -o pipefail`
belongs in the code, not in anyone's memory.


### 5.12 What Package 1 leaves behind — and why v8 is now cheaper

Three assets, one liability, and a sequencing conclusion.

**ASSET 1 — a proven corpus-scale re-embedding pipeline** (`processing/reembed.py`,
`processing/reembed_canonical.sbatch`). Every v8 option ends in re-embedding
72.7M documents, and that step no longer has to be designed. It has: 256-way
sharding resumable under preemption; a `pin.json` recording tokeniser sha256,
checkpoint sha256, git commit, python and unicodedata versions, asserted per
shard **before a GPU is touched**; code staged out of the shared working tree so
it cannot move under a running job; a `/vast` free-space floor above ES's
flood-stage watermark; a materiality threshold; a positive control that aborts;
a per-shard ledger with attempt counters; and independent read-back
verification. It ran end-to-end at 72.7M for the first time on 5 Sep.

**ASSET 2 — one canonical tokeniser** (`phonetics/tokenise.py`, vendored
byte-identically into `hf/inference.py`, with a contract test). Four
implementations became one definition. **v8 must not fork it again** — the entire
cost of Package 1 was four copies drifting.

**ASSET 3 — measurement method that earned its keep.** Recorded across §5.6–5.11
and worth more than the findings: structural attribution over historical
(recompute and compare, don't reconstruct provenance); `--scope all` over a
predicate (it paid off three separate times, twice on findings nobody predicted);
table diffs over behavioural corpora for equivalence claims; positive controls
that abort rather than report; and the observation that a measurement returning a
*value* is far harder to doubt than one returning nothing.

**LIABILITY — four deferred fixes, each of which alone implies a full re-embed:**

| | what | why deferred |
|---|---|---|
| **D-A** | NFKC + casefolding | changes what a correct index contains |
| **D5** | `FB00–FB17` precedence — `ﬁ` scores ARMENIAN | fixing it changes token→script mapping |
| **D6** | freeze the alpha table so `isalpha()` stops tracking the interpreter | same |
| **D7** | the `GURMUKHI`/`.get(name, 0)` trap — see §5.7 | same |

🛑 **THE SEQUENCING CONCLUSION, AND IT IS NEW: BATCH THEM.** Done separately
these are **four** 72.7M re-embeds. Done inside v8's re-embed they are **zero**
extra passes — the re-embed is happening anyway, the pipeline exists, and every
one of them is a change to the same tokeniser the v8 model will be trained
against. Doing any of them standalone would be paying four times for a trip
already booked.

⚠ **The corollary is a constraint on v8, not a convenience:** the v8 model must be
trained on the tokenisation the v8 index will be written with. So D-A/D5/D6/D7
must be settled **before** training data is regenerated, not after the model
exists. That moves them from "open decisions someday" to **the first gate on v8**.

### 5.13 Device selection — GPU is only ~4.5x CPU for this model

**Measured 5 Sep, after the run, because nobody had checked:**

| | names/sec |
|---|---|
| CPU, 16 torch threads, batch 4096 *(laptop)* | **5,602** |
| A100, from today's real 72.7M run | **~25,000** |
| ratio | **4.5×**, not the 50× one assumes |

72,703,777 names is **3.6 h on one 16-thread CPU process**; minutes across a
modest CPU array.

**Why the gap is small, which determines whether it generalises:** the model is
8.3M parameters of which ~87% is a character embedding *table* (a lookup, not a
matmul), sequences are toponym-length (~10–20 tokens), and the encoder is a
2-layer BiLSTM — inherently sequential and unable to saturate a GPU. ⚠ **This is
about INFERENCE only. Do not generalise it to the training phases**, which are a
different problem.

⚠ **Both numbers need re-measuring on CRC before anyone acts on them.** 5,602/s
is a laptop, not a compute node; and the ~25,000/s includes per-shard model load,
so it understates steady-state GPU throughput.

🛑 **The question this raises is not "is CPU fast enough" but "is waiting for a
GPU slower than starting on CPU now?"** Today's array spent **0.81 h computing**
and most of its wall clock `PENDING (Priority)` / `PENDING (Resources)`, was
cancelled and resubmitted in fragments, and ran at `%16` rather than the designed
`%100`. The `htc` CPU partition has no GPU allocation to queue behind. That makes
it an *availability estimate*, not a capability test.

**The code is inconsistent, and the newest file is the one missing the check:**

```
processing/reembed.py:1272                  --device default "cuda"   NO check
phonetics/inference/backfill_embeddings.py  --device default "cuda"   NO check
phonetics/inference/update_es.py:675        'cuda' if torch.cuda.is_available() else 'cpu'
processing/reembed_canonical.sbatch         --gres=gpu:1 --cpus-per-task=4
```

A hard `default="cuda"` on a GPU-less node is a crash, not a fallback. And the
sbatch asks for **4** CPUs, so a naive CPU fallback there would run at roughly a
quarter of the measured rate — a fallback that "works" while being silently 4×
slower than it needed to be. *Referred to `indexing-9c` (5 Sep) to measure on CRC
and to argue both ways on whether a shared routing utility is worth one home.*

## 6. DECISIONS TAKEN — 5 September 2026 (SG)

Four settled in interview. They are recorded here as decisions, with what each
one closes and what it leaves open.

| | decision |
|---|---|
| **v8 scope** | **Benchmark first, then decide.** No GPU committed until an evaluation exists that could show a retrain helped. |
| **Optimise for** | **Cross-script phonetic matching** — and *only* that. Historic romanisations, typo robustness and exonyms are explicitly not v8's target. |
| **Evaluation** | **Full**: retrieval over ≥1M with recall@k per script pair against Levenshtein / Jaro-Winkler / double-metaphone; discrimination with matched negatives reporting AUC; plus a geometry gate on the checkpoint. |
| **D-A/D5/D6/D7** | **Bundle into v8's re-embed.** No standalone pass. |

**What "cross-script only" closes.** It settles D-D's label design before D-D is
taken: positives come from **co-attestation of the same `place_id` across
gazetteers plus the hard-link overlay**, which is exactly the cross-script signal.
It also rules out spending on a `ja` kanji reading table (D-B) and on
transliteration/typo augmentation beyond what cross-script needs. And it means
GOTW's `Keang-su → GANSU` is **not** a v8 acceptance criterion — worth telling
`gotw-de`, which is holding a design conclusion on it.

⚠ **A TENSION IN THESE ANSWERS, STATED SO IT IS DELIBERATE.** "Benchmark first"
means v8 is at minimum a month out and may not happen at all; "bundle into v8"
therefore leaves D-A/D5/D6/D7 unfixed for that whole period, and permanently if
v8 does not proceed. I flagged this and measured the cost before accepting it:

* **All-caps exposure is far smaller than I implied.** Of 2,796 sampled
  Latin-script names with ≥3 Latin letters, **9 are entirely upper-case
  (0.32%)** — and they are acronyms (`CICS`, `OPM`, `WNXT`), not gazetteer
  formatting. Extrapolated: ~194,000 of 60.1M Latin documents.
* My first attempt said 0.63% and was **an artefact of my own filter** — it
  counted CJK names containing a Latin fragment (`秋田空港TB`) as "all-caps".
* ⚠ **The residual risk is on the QUERY side and is not measurable from the
  index.** A user typing `LONDON` gets 0.2825 against the indexed `London`.
  Query casing is user behaviour; nothing in the corpus can size it.

**So the decision stands on a smaller index-side defect than my framing
suggested, and an unmeasured query-side one.** Revisit if v8 slips past a
quarter, or if a contributed dataset arrives in upper case.

## 7. What is now scheduled, and what is closed

**SCHEDULED — D-C, the benchmark. This is the next work and the only work.**
Nothing else in this section proceeds until it exists. Confirmed by SG 5 Sep:
*"the casefold problem can wait, let's press on with v8"*.

| | status after 5 Sep |
|---|---|
| **D-0** bundle the tokeniser fixes | ✅ **Resolved** — into v8's re-embed, no standalone pass |
| **D-A** NFKC + casefolding | ⏸ **Closed, waiting** — rides D-0; measured exposure ~194k of 60.1M Latin docs |
| **D-B** CJK/Japanese romanisation policy | 🛑 **Ruled out** — "cross-script only" does not buy a `ja` kanji reading table |
| **D-C** an evaluation that can fail | ▶ **SCHEDULED — full scope** |
| **D-D** retrain, objective and labels | ⏸ **Gated on D-C** — but its label design is now settled (below) |
| **D-E** what dimension v8 ships at | ⏸ **Unanswerable** until a v8 checkpoint exists |

**D-D's labels are settled even though D-D is not taken.** "Optimise for
cross-script phonetic matching, and only that" determines the positive-pair
definition: **co-attestation of the same `place_id` across gazetteers, plus the
hard-link overlay's `sameAs` edges**. That is the cross-script signal, it is free,
and it replaces the HDBSCAN-over-PanPhon clustering that made the current labels
circular (§4.1). What it does *not* buy: a Japanese reading table, transliteration
augmentation beyond cross-script, or exonym coverage.

⚠ **`Keang-su → GANSU` is therefore NOT a v8 acceptance criterion.** GOTW is
holding a design conclusion on that failure; it must be told that historic
romanisation was explicitly de-scoped, so it plans around the gap rather than
waiting for it to close.

### The cards below are kept for their measurements, not as live questions

Everything below needs discussion before it becomes work. Each is stated as the
question to answer, not as a task. **The order changed after Package 1**: what was
a loose set of independent questions now has a gate in front of it.

---

### D-0 · Settle the tokeniser BEFORE regenerating training data — the new first gate

**Question: do we take D-A, D5, D6 and D7 together, inside v8's re-embed?**
My recommendation: **yes, all four, and they must be decided first.**

Reasoning is in §5.12. In short: each alone implies a 72.7M re-embed; v8 implies
one anyway; and the v8 model must be *trained* on the tokenisation the v8 index
will be *written* with, so these cannot be retrofitted after a model exists.
Deciding them late is the one sequencing error that would force a second retrain.

What each buys, all measured:

* **D-A** — `London` vs `LONDON` is **0.2825** today, 1.0000 casefolded;
  `Ｔｏｋｙｏ` vs `Tokyo` is 0.0166, 0.9914 under NFKC. Gazetteer sources carry
  all-caps forms routinely.
* **D5** — `ﬁ` (a *Latin* ligature) scores `ARMENIAN`. Wrong, reproduced
  deliberately, and only fixable when the index is rewritten.
* **D6** — removes a class of defect rather than an instance: `str.isalpha()`
  makes the tokeniser's behaviour a property of whichever interpreter runs it.
  Practical impact today is **0 of 200,000 names**, so this is hygiene — but it is
  free hygiene if taken with the others.
* **D7** — the `.get(name, 0)` trap resolved to **LATIN**, not a sentinel, and
  embedded Punjabi as Latin. Already repaired by accident (§5.7); the durable fix
  is to make detector and vocabulary share one source so it cannot recur.

⚠ **Do NOT "fix" D5 or D7 by making the table more correct in isolation.** Both
are reproduced deliberately because the index was written with them. They become
fixable only in the same pass that rewrites the index.

---

**D-A · Do we adopt NFKC + casefolding?** *(folded into D-0 — kept for its
measurements, no longer a standalone decision)*
Measured: `London` vs `LONDON` scores **0.2825** today; casefolded, 1.0000.
`Ｔｏｋｙｏ` vs `Tokyo` is 0.0166; under NFKC, 0.9914. Gazetteer sources
routinely carry all-caps forms. **Cost:** it forces a re-embed of all
72.7M documents. ✅ **Answered by D-0: it waits for v8 and rides that re-embed.**
It was never worth a standalone pass, and Package 1 confirmed the pass will
exist.

**D-B · What is the CJK/Japanese romanisation policy?**
Applying romanisation at inference to the *current* weights, no retraining:
`北京`~`Beijing` −0.3405 → **+0.9889**; `서울`~`Seoul` −0.0274 → **+0.9857**;
and the false positive `北京`~`南京` correctly drops **0.9051 → 0.4291** (they
share the glyph 京 and share no sound — today the model scores glyph overlap).
But anyascii gives *Mandarin* readings, so `東京`~`Tokyo` only reaches +0.5126.
Japanese kanji needs a `ja` reading table (pykakasi or similar). *Question: is
Japanese worth a reading table, and does the Chinese/Korean result change how we
weight CJK in the corpus?* This is a data task, not a model one.

**D-C · Do we build a benchmark that can fail?**
My recommendation: **yes, and before anything else except D-0.**

🛑 **Two evaluation sets now exist that did not on 5 Sep morning, and neither
came from this analysis.** Use them rather than starting from nothing:

* **GOTW Qing provinces** — 1856 English transcriptions against modern forms,
  18 queries with known answers, currently **11 of 18** resolving to a usable
  container and **`Keang-su` → GANSU at 99.5** as a confident wrong answer. Small,
  but it is a real downstream task with a real failure, and it is exactly the
  historic-romanisation gap the model is supposed to close.
* **The re-embed census** — per-script change rates over all 72,703,777 documents
  (§5.2), and with them a positive-control pattern (22.6M rows, min pass
  0.999957) and a structural attribution method that both generalise to
  evaluating a *new* model against an old index. Nothing in
D-D or D-E can be evaluated without it, and §4.5 shows the current evaluation
would not have caught v7 shipping below v6. Minimum shape: a retrieval benchmark
over ≥1M real toponyms with recall@{1,10,100,200} and MRR *per script pair*
against Levenshtein / Jaro-Winkler / double-metaphone; a discrimination
benchmark with matched negatives reporting AUC and average precision, not pass
rate; and a **geometry gate on the checkpoint itself** (effective rank,
‖mean vector‖, σ20/σ1) — a threshold of "effective rank ≥ 40 of 128" would have
stopped v7, and it runs in seconds. *Question: how much of this, and who owns
the ≥1M corpus?*

**D-D · Do we retrain, and with what objective and labels?**
The candidates, from §3 and §4: positives from **co-attestation of the same
`place_id`** plus the hard-link overlay, replacing the HDBSCAN-over-PanPhon
clustering; **false-negative protection** so a negative never shares a
`place_id` or hard link with its anchor; hard negatives **re-mined each epoch**
against the current checkpoint; **InfoNCE / multi-similarity with in-batch
negatives** replacing the fixed-margin triplet loss; an explicit **uniformity**
term to target the rank collapse; and **dropping the teacher/student split**
altogether — the split exists to transfer phonetic knowledge and the measured
transfer is *negative* (PanPhon192 R@1 0.411 vs the student it teaches, 0.852).
Also: shrink `char_embed` to the ~8,000 characters that can actually be emitted
and spend the freed 6.7M parameters on the encoder, which has 1.02M.
*Question: does this happen, and does D-C gate it?*

⚠ **The cost model changed on 5 Sep, in v8's favour.** The step everyone flinches
at — re-embedding 72.7M documents — is now built, proven at full scale, resumable
under preemption and free of GPU-allocation queueing. What remains genuinely
expensive is regenerating training data and the three GPU training phases. **Do
not let the re-embed be quoted as a reason not to retrain; it is the part that is
solved.**

**D-E · What dimension does v8 ship at?**
Only answerable after a v8 checkpoint exists. Measure its effective rank first:
restored to ≳60, keep 128-d; still ≲20, ship 64-d and halve the HNSW cost.
Do **not** revisit int8 (§3, negative finding).

---

### 7.1 What no embedding can fix — a ceiling measured from outside

⚠ **Some of the apparent retrieval failure is MISSING DOCUMENTS, not bad
embeddings, and no v8 can recover it.** GOTW measured this while building a
transcription-alias table (`gotw-eb`, 5 Sep): constraining 150 Chinese places to
their correct province — i.e. *fixing* the parent resolution completely — cut
leaf hits from **150 to 86**, because the leaf places are largely absent from the
index. Their alias table takes the Qing provinces from **11/18 to 18/18** on
parentage and changes leaf geolocation not at all.

**Query expansion cannot conjure a document that is not there, and neither can a
better embedding.** Any v8 claim of "improved recall" must therefore be measured
against documents that *exist* — which is what the ≥1M retrieval benchmark does
by construction, and is one more reason it precedes the retrain.

### 7.2 The de-scoping's real exposure, and a cheaper lever than a model

`Keang-su → GANSU` was reported as a Chinese problem. It is not. Measured over
GOTW's corpus:

```
China                              2,414   2.1%
non-Latin-script country (proxy)  16,486  14.2%
  RU 3,535 · IN 3,210 · CN 2,414 · GR 1,167 · IR 767 · EG 524 · JP 483 · PK 403
```

The 19th-century transcription-convention problem applies equally to the book's
renderings of Russian, Indian, Greek, Persian, Arabic and Japanese names. **14.2%
is an upper bound on exposure, not a measured failure rate** — only the Chinese
admin names have been tested — but it is the number that belongs in a scope
conversation, and it is an order of magnitude above the figure that prompted the
de-scoping.

🛑 **The cheaper lever is a rule-based transcription-convention mapping, not a
model.** `‑hyen`/`‑heen` → xian, `‑fu` → fu, `keang` → jiang, `‑pih` → bei,
emitted as additional `variants`. The economics are the gateway's own
`derive_name_forms`: a variant is scored at `VARIANT_SCORE_WEIGHT` and folded
into the same pool, **so a wrong derived form costs almost nothing while a right
one rescues the query**. GOTW has ~30 hand-curated admin aliases working today
and reports that hand-curation does not scale past admin names (18 provinces
tractable, ~2,400 leaf toponyms not).

⚠ **If such a mapping were a GAZETTEER-side asset rather than a per-project one
it would serve every historical corpus WHG ingests.** That is a different piece
of work from Symphonym and nobody is asking for it — recorded here because it is
the only lever identified today that addresses a whole problem class more cheaply
than a retrain would address a fraction of it.

### 7.3 A correction to advice this session gave

Advising GOTW on which of its results needed re-running after the tokeniser fix,
this session wrote *"if any of your queries contain a space"*. That framing
understated it badly. Measured on their corpus: **2,121 of 8,713 processed places
(24.3%)** and **2,866 of 5,405 cached admin-parent resolutions (53%)**.

⚠ **Admin and container names are far more space-heavy than headwords** —
"canton of Amatrice", "prov. of Abruzzo Ultra". Anyone applying the
space/CJK/non-NFC re-run filter to a *container* population should expect a
majority, not a minority.

## 8. Scale of likely improvement — and what cannot be claimed

**Package 1, measured, no retraining:** multi-word self-retrieval goes from
65.7% to 100% by construction, and 3.86M documents move from anti-correlated to
matching. What that is worth **end-to-end** cannot be stated, because no
end-to-end retrieval benchmark exists — that is D-C. It is a bug fix with a
large measured local effect, and the honest characterisation is "the search
stops failing in a way it should never have failed", not a percentage.

**Package 1's own outcome, for calibration:** it fixed the plumbing and touched
nothing about model quality. 46.5M documents are now queried in the tokenisation
they were written in, and ~3.9M CJK/Kana/Hangul went from unreachable (0.3%
rank-1 self-retrieval) to 100%. That is a large, real, measured improvement in
*retrieval* — and it moved the `Keang-su → GANSU` failure not at all. **The two
are independent, and only the second is what v8 is about.**

**D-D (the retrain): no numeric forecast is defensible today.** Anyone quoting
"+X% recall" before D-C exists is quoting nothing. What *can* be said:

- The current objective produces **no gradient** on the overwhelming majority of
  its training examples. A saturated objective has a known fix, and in-batch
  contrastive training reliably restores effective rank in comparable settings.
  **Restoring rank is a prerequisite for any gain, not itself a gain** — the
  evidence will be R@k on D-C's benchmark and nowhere else.
- The ceiling is plausibly high *because the floor is so low*: on the only
  discriminating benchmark, a 33 MB three-phase neural model ties plain edit
  distance. That is an argument that headroom exists, not a measurement that it
  will be captured.
- **Risk to state plainly:** it is possible that after Package 1 and D-C the
  model turns out to be adequate and the retrain is not worth its GPU cost. D-C
  is what would tell us, which is why it is recommended first.

---

## 9. The pending IPA / PanPhon recomputation

The campaign deferred recomputing IPA and PanPhon "pending any retraining".
**Recommendation: do not recompute `panphon_embedding` at all — and do not
recompute anything until D-D is decided.**

`grep` puts `panphon_embedding`'s consumers entirely inside the training-data
pipeline (`extraction/generator.py`, `es_knn_helper.py`,
`rebuild_toponyms_index.py`, and a separate `schemas/toponyms-panphon.json`).
It is **not** in `schemas/toponyms.json` and nothing in the serving path reads
it. Its only real job is choosing positive pairs — which D-D would remove. It is
also the rank-4.37 bottleneck of §3.

So, when the recompute is eventually scheduled:

- **Recompute the IPA strings** — the useful artefact, needed for any teacher or
  auxiliary objective, and coverage is only 54%.
- **Recompute the per-segment PanPhon features** only if a teacher survives
  D-D's decision.
- **Retire the 8-bin pooled 192-d vector.**

Deciding D-D before scheduling the recompute avoids paying for it twice. This is
**not** a blocker on Package 1, which touches none of it.

---

## 10. Housekeeping found on the way

Not part of Package 1; do not fold these in.

- Delete the shadowed `phonetics/{models,training,vocab,extraction,inference}.py`
  (~2,900 lines, unreachable).
- **Packaging is about which HOSTS can run the code, not where a file tidily
  belongs.** `processing/reembed.py` began under `phonetics/inference/`, where it
  could not be imported on pitt at all — that package's `__init__` pulls in
  `ToponymEncoder` → torch, and pitt has system Python 3.9, no conda env and no
  torch, yet two of the pipeline's three phases run there.
- whg3 self-hosts an **8.36 MB int8 ONNX** export of the v7 model with **no
  recorded provenance** — nobody knows which commit produced the file that
  decides what every browser query means. Its `any-ascii` (npm) must also be
  bumped in lockstep with `anyascii` (PyPI): verified byte-identical over 94,624
  codepoints at 0.3.3/0.3.3, a guarantee that evaporates if either moves alone.
- `generate_pairs.py`'s `phonetic_similarity()` is anyascii + Levenshtein.
  Rename it or delete the module — the name asserts something it does not do,
  and a future reader will trust it.
- `symphonym_v7_pairs_test_report.json` has `script1`/`script2` transposed
  (`Գրինվիչ` labelled `ARABIC`, lang `hy`).
- `hf/config.json` reports `"total_toponyms": 66924548` and
  `"embedding_coverage": 1.0`; the live index is 72,703,777. Date it or derive
  it.
- `char_vocab.py`'s module docstring and `generate_vocabulary`'s docstring state
  opposite designs. One of them must go.
- The `lang` field of the toponyms carries street fragments and language names
  (§4.3). That is an upstream ingestion problem, not a Symphonym one.
