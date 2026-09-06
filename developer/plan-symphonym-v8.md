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

**v7 is not measurably better than anyascii-romanised Levenshtein**, and the reason is not the
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
queries, on which v7 (R@1 0.852) is inside the noise band of romanised Levenshtein
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
| **Levenshtein** *(anyascii-romanised — see below)* | **0.815** | **0.885** |
| Symphonym v6 | 0.867 | 0.903 |
| **Symphonym v7** | **0.852** | **0.908** |

⚠ **"Levenshtein" here means ANYASCII-ROMANISED Levenshtein.**
`mehdie_benchmark.levenshtein_similarity` does `anyascii(s).lower()` internally
before measuring. That matters more than it looks: **raw edit distance scores
0.000 on every cross-script pair by construction**, since the two strings share
no characters. So the baseline v7 ties is not a naive algorithm — it is
*anyascii plus* edit distance, and anyascii is itself a transliteration system
doing the hard half of the work.

🛑 **And on some script pairs that baseline is already PERFECT.** Measured
(`indexing-9c`, 5 Sep): `London ~ Лондон` scores **1.000** under romanised
Levenshtein and 1.000 under romanised Jaro-Winkler. For Cyrillic↔Latin there is
nothing left for a model to win. Where the baseline actually fails is
**CJK/Kana/Hangul↔Latin** — `東京 ~ Tokyo` drops to **0.125**, because anyascii
gives Mandarin readings for Japanese kanji.

⚠ **That localises v8's whole value proposition, and it sits awkwardly with the
scoping decision.** A learned phonetic model can only beat anyascii+Levenshtein
where anyascii is weak — which is the CJK family. But D-B (a Japanese kanji
reading table) was explicitly ruled out by "cross-script only". **Worth
re-examining before any GPU is committed: if romanised Levenshtein is near-perfect
on the alphabetic cross-script pairs, "optimise for cross-script phonetic
matching" may in practice mean "optimise for CJK", which is a narrower and more
data-hungry target than it sounded.** The retrieval benchmark's per-script-pair
breakdown will answer this directly, which is one more reason it precedes D-D.

Binomial SE at n=137 is ≈3.1pp. v7, v6 and romanised Levenshtein are
**statistically indistinguishable**, and v7 is nominally *below* v6 on R@1. The five testsets
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

### 5.10b A correct number applied to the wrong comparison

Advising `indexing-04` on verifying the ES snapshot, this session wrote: *do not
use `_cat/indices`' 361,746,797 — the real figure is 51,187,900 places.* True for
**sizing** the corpus. **Wrong as a snapshot-verification predicate**, and
dangerously so: a snapshot counts Lucene documents exactly as `_cat/indices`
does, nested toponyms and geometries included, so it reports the 361.7M-shaped
figure too. Checking a good snapshot against 51,187,900 would have looked like
**catastrophic loss of 86% of the corpus**.

The correct comparison is snapshot against the *live index's* `docs.count` — like
against like. **The number is misleading in one direction and load-bearing in the
other**, and the advice inverted which was which.

🛑 **This is the campaign's signature fault committed inside advice about
verification**: a correct measurement attached to the wrong comparison, producing
a confident wrong conclusion that *looks like* diligence. It is distinct from the
self-confirming measurements in §5.10 — nothing here was mis-measured. The number
was right; the predicate was wrong.

**Related, and found the same way:** this session reported the latest snapshot as
covering the live indices. It covers **one** — `promote-h3ccode-20260805t120000z`
holds `places` only; `toponyms` was last captured by
`promote-temporal-20260731t160000z`. The conclusion (everything after 6 Aug
unprotected) stood, but anyone reaching for "the last snapshot" to recover
toponyms would have found none in it. **The `indices=1` was printed in this
session's own query output and not read** — the third time today evidence on
screen lost to a summary of it.

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

⚠ **SUPERSEDED — the laptop figure was flattering. Measured on CRC by
`indexing-9c`, same model, 200k real toponyms:**

Thread sweep, one `--exclusive` allocation on htc-n30 (Xeon Gold 6248R, 48
cores), varying only thread count. An earlier set was **discarded as
contaminated**: two submissions landed on one node together and measured each
other — 382/s and 341/s for a configuration that gives 2,101/s alone.

| threads | bs=256 | bs=1024 | bs=4096 |
|---|---|---|---|
| 1 | 902 | 665 | 528 |
| 4 | 2,073 | 2,106 | 1,826 |
| 8 | 2,631 | 3,193 | 3,069 |
| **16** | **3,451** | **4,302** | **4,430** |
| 32 | 2,704 | 4,291 | 4,406 |
| 48 | 846 | 1,701 | 3,499 |

| GPU | names/s |
|---|---|
| **L40S** | **49,057** |
| A100-PCIE-40GB | 33,211 |

🛑 **Using the whole node is 4× WORSE than using a third of it** — 846/s at 48
threads against 3,451/s at 16. The model is too small for the thread pool and
spends its time synchronising. **The CPU fan-out shape is three 16-thread tasks
per 48-core node, not one 48-thread task.** Peak RSS is set by batch size
(4.7 GB at ≤1024, 8.3 GB at 4096), not threads, so three fit in ~14 GB.

⚠ **The L40S beats the A100 by 1.5×** (forward 2.35 s vs 3.16 s, same 200k
names). A small sequential BiLSTM rewards clocks, not tensor cores — so
`--constraint=a100|l40s` is right, and prefer the L40S given a choice.

⚠ **htc nodes are heterogeneous by ~1.7×.** Size a fan-out for the low end.

**The ratio is 11× (L40S vs the best CPU configuration), not 6.5× and not 4.5×.**
72.7M names is ~25 min on one L40S or ~4.6 h on one 16-thread CPU task, so
**16 concurrent 16-thread tasks ≈ one L40S** — the number to hold when deciding
whether to wait for a GPU. This
session's laptop figure was 16 threads against 4, and its ~25,000/s GPU number
was diluted by per-shard model load and by the Python diff/quantise loop, which
is not embedding at all — steady state on an L40S is ~49,000/s.

The *mechanism* stands and still explains why the gap is 6.5× and not 50×.

🛑 **42% of GPU wall time is CPU tokenisation.** `_pad_batch` is pure Python and
runs on the host either way, so it is a hard floor on the GPU rate — if it were
free the L40S would be at ~85,000/s. **The next factor of two is in tokenisation,
not hardware.**

⚠ **CPU scales 4→32 threads by only 3.6× for 8× the cores**, and batch 4096 is
*slower* than 1024 at 4 threads while faster at 32. Peak RSS is set by batch size
(4.7 GB at 1024, 8.5 GB at 4096), not thread count — so a CPU fan-out wants
**many small-batch processes, not few large-batch ones**: ~16 tasks on a
64-core/240 GB htc node ≈ 34,000 names/s, about two-thirds of one L40S.

⚠ **A wrong figure is loose on `/vast`.**
`reembed/20260905T1455Z/reembed_cpu.sbatch` claims "~4,800 rows/s on 4 threads
… only ~4x slower" — a present-tense claim no run produced, on shared storage
where the next session reads it as measured.

**Why the gap is small, which determines whether it generalises:** the model is
8.3M parameters of which ~87% is a character embedding *table* (a lookup, not a
matmul), sequences are toponym-length (~10–20 tokens), and the encoder is a
2-layer BiLSTM — inherently sequential and unable to saturate a GPU. ⚠ **This is
about INFERENCE only. Do not generalise it to the training phases**, which are a
different problem.

✅ **Both have now been measured on CRC (above).** The instruction to re-measure
rather than act on the laptop figure was right, and it changed the answer by
between 1.4× and 5×.

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
slower than it needed to be. ✅ **Resolved (`indexing-9c`, 5 Sep): a `resolve_device` policy module, but NO
scheduler-aware router.** The reason is better than the one this document
proposed: *the input a router needs is the one input Slurm will not give you* —
`squeue` describes an instant, not your future priority, and a router that is
wrong is wrong invisibly because the job merely looks slow.

**Race the queues instead.** `reembed`'s shard design already makes estimation
unnecessary: unique temp names, atomic renames, `.done` gating, and
`cmd_compute` skipping a complete shard. Submit both a CPU and a GPU array and
let whichever the scheduler starts first win. ✅ **Checked before racing: double-counting does not arise** — one `.done` per
shard id, so a shard computed twice yields one meta and `examined_total` cannot
inflate.

🛑 **But racing breaks something subtler, now fixed (`8a7fb38`).** Both tasks
`os.replace` onto the same final path from unique temp names, so two can
interleave as `A.parquet, B.parquet, B.done, A.done` — leaving **a marker written
by one process beside data written by another**. Harmless while both computed the
same thing; a CPU task and a GPU task do **not**, since the run counts
one-int8-step differences as hardware quantisation noise. Every downstream count
would then describe a file other than the one applied. `cmd_apply` now asserts
each parquet's row count against its own marker.

⚠ **The fixture could not have caught it**: `_complete_shard` wrote `b""` as the
data file, so every test in that class was blind to a marker and its data
disagreeing.

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

🛑 **REVERSED 5 Sep by §6.1 — do not act on the preceding sentence.** SG added
historic orthography as a second target, so `Keang-su → Jiangsu` **is** in scope.
`gotw-de` was given the old answer and is owed the new one.

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

## 6.1 DECISIONS TAKEN AFTER THE BENCHMARK — 5 September 2026 (SG)

The benchmark §6 made a precondition has run (§8), so the four questions it
deferred were put to SG and answered. **Two of these SUPERSEDE lines in §6's
table. §6 is retained as the record of what was decided on the information
available before the benchmark, not as current scope.**

| | decision | supersedes |
|---|---|---|
| **Retrain?** | **YES — retrain to fix the geometry.** | §6 "benchmark first, then decide" — the benchmark decided. |
| **Scope** | **Add historic orthography as a SECOND target**, alongside cross-script phonetic matching. | 🛑 §6 "Historic romanisations … explicitly not v8's target". That line is now **WRONG** and is superseded here. |
| **Welsh data (`place#161`)** | **Cleared to use.** SG: *"I've had conversations with RCAHMW and they are very happy for us to use the data. I will formalise the licence later when v8 is ready for release."* | `place#161` "paused 30 Jul, outreach drafted and never sent". |
| **Release gating** | ✅ `indexing-9c`'s commits (already pushed) · ✅ published `config.json` fixed · ⏳ `path.repo` delegated to `indexing-04` · ✅ transcription pack funded (§6.2). | — |

### What "retrain for the geometry" commits us to

The target is the finding in §3 and the gate in §8: **11 of 128 directions carry
95.3% of the variation, and a vector rebuilt from 20 directions is cosine
1.00000 to the original.** So ~108 of the 128 dimensions are paid for in storage,
index size and KNN cost while carrying nothing. This is the one v8 objective that
the benchmark **positively supports**: v7 wins discrimination (AUC 0.9324 vs
0.9002, separated interval) and loses retrieval (R@10 0.294 vs 0.323), and §8.1
shows one mechanism behind both — a representation too dense to separate
neighbours at corpus scale. Spreading the representation over its available
directions is the direct attack on that mechanism.

⚠ **What it does NOT commit us to.** "Fix the geometry" is not "beat Levenshtein
on retrieval". §8.2 measured the density effect as scaling with n^−0.22, and
§9 states what cannot be claimed. The acceptance criterion is the geometry gate
plus **no regression** on discrimination — not a retrieval win, which may not be
available at 72.7M documents by any means.

### What adding historic orthography changes — this is not a free addition

🛑 **It invalidates the label design that "cross-script only" settled.** §6 fixed
D-D's positives as *co-attestation of the same `place_id` across gazetteers plus
the hard-link overlay*, and justified that precisely because it is the
cross-script signal. **Historic orthography is not in that signal.**
`Llanddona`/`Seynt Dona` and `Keang-su`/`Jiangsu` are same-script pairs that
co-attestation will not produce, because the historic form is usually **not in
the index at all** — that is the whole complaint. So D-D now needs a *second*
positive source, and it must be an independent one.

Three consequences to carry into D-D:

1. **The two named sources become load-bearing.** LHPN Welsh↔English clerk
   transliterations (§7.4: pass `generate_pairs.py`'s gate at 80.5–86.7% where
   the corpus's existing `cy`/`en` pairs score near zero) and GOTW's pinyin /
   Qing-transcription list. Neither existed as a v8 input before today.
2. ⚠ **`place#163`'s own warning still stands and is now the binding
   constraint**: only ~5.5% of sampled LHPN records carry a populated
   `HeadName`. *"Don't extrapolate 700k records → 700k useful pairs."* The
   usable yield, not the record count, is what must be quoted.
3. 🛑 **A retraction I owe `gotw-de`.** §6 states that `Keang-su → GANSU` is
   **not** a v8 acceptance criterion and says to tell `gotw-de` so. **That is
   now reversed** — historic transcription is in scope. `gotw-de` was told the
   old answer and must be told the new one.

### `place#161` / `place#163` — what the clearance does and does not do

✅ **Unblocked in substance.** `place#163` was gated on `place#161` (RCAHMW
licensing). SG has spoken to RCAHMW directly and reports they are happy for the
data to be used, with the licence to be formalised when v8 is ready for release.
So LHPN pairs may now be used as a v8 training input.

⚠ **The formalisation is a RELEASE gate, not a training gate, and it is now
someone's job.** The permission is verbal and forward-looking; the written
licence does not exist yet. Nothing may be *published* — model, deposit, or
paper — that is trained on LHPN data until it is in place. Record it against the
v8 release checklist, not as done.

## 6.2 ~~The historic-transcription packs~~ — 🛑 **CANCELLED by SG, 6 Sep**

🛑 **THE SPECIALIST-REVIEW INITIATIVE IS DROPPED.** SG asked whether there was a
strong reason to keep it; `gotw-eb`'s honest answer was no. The curated list will
not be produced and `docs/toponyms/` has been removed from the site (34 files;
2,391 Chinese and 3,147 Russian pick-lists).

**Two consequences, and they are unequal:**

* ✅ **The staging/production pick-list divergence (§6.10) is MOOT** — it has no
  consumer. The weaker, correct characterisation stands on record anyway: the
  divergence was **score-level, not content-level**, so a candidate staging
  offered *does* exist in production and was merely ranked below the cutoff.
* 🛑 **The 1856 → POSTAL rung now has NO acquisition route at all** — not a slow
  one, none. The specialist review was the only mechanism for it, and the 1908
  atlas (§6.11) supplies **postal → modern**, the *other* rung. **If v8 is ever
  evaluated on nineteenth-century orthography rather than postal-era, nothing
  currently in prospect closes that gap.**

### ⚠ What v8's second target now rests on — and it is less damaged than it looks

**The loss falls harder on GOTW than on v8.** `Keang-su → Jiangsu` is GOTW's
problem and is now unsolved. **v8's target was *historic orthography*, and
postal-era romanisation IS historic orthography** — it simply is not *1856*
orthography. Three legs remain, **none requiring a specialist**:

| leg | what it gives | status |
|---|---|---|
| **LHPN Welsh ↔ English** | the volume; clerk transliteration of Welsh sound | ✅ RCAHMW cleared (§6.1); ~5.5% `HeadName` yield is the binding constraint |
| **1908 Atlas** (§6.11) | postal ↔ modern, coordinate-anchored, **five language families under one convention** | ✅ on `main`, public domain |
| **TGN dated variants** (§6.8) | **labelled** European historic forms — `Dorchestre`/`Dorcic`/`Dorkecestre` | ✅ constructible from the **`toponyms`** index + the patch file, no production write |

🛑 **What is genuinely lost is the EVALUATION on 19th-century forms**, and the
hardest tail with it. Any v8 claim about historic orthography must therefore say
**which** historic orthography — postal-era and Welsh clerk forms, not 1856
gazetteer transcription. ⚠ **That distinction must survive into the paper**, or
the claim will be read as covering exactly the case it does not.

---

## ~~6.2b~~ The packs as they were specified — retained for the design, not as live work

`gotw-eb` has described the "pinyin list" in full (5 Sep). **It reframed it in a
way that simplifies v8's scope, and attached a warning that must not be lost.**

🛑 **It is NOT a third target. It is data for the second one.** Pinyin is simply
the modern side of a historic-orthography pair, so there is no separate pinyin
objective to budget for — `Keang-su → Jiangsu` is the same phenomenon as
`Llanddona → Seynt Dona` in another language. §6.1's two targets stand
unchanged.

**Shape.** Each row is a per-place adjudication of a real 1856 gazetteer entry,
so every pair is anchored to a place rather than floating free. Given: printed
headword, the book's variant spellings, printed admin hierarchy, printed
coordinates where usable, volume + page. To be filled by a specialist:
`modern_pinyin`, `chinese_characters`, `modern_province`, `confident (y/n/guess)`,
`notes`. A completed row yields a historic→modern Latin pair, a historic→hanzi
pair, and — for the 1,166 with usable coordinates — an anchor by which the pair
can be *checked* rather than trusted.

**Size offered: 2,413 Chinese rows** (30 sorted to the top as a pilot) and
**3,534 Russian** — *not* 2,414 / 3,535. Two rows were the same place recorded
twice, byte-identical in headword, text and hierarchy, differing only in a SQLite
rowid; the id below identified them as one thing.

### The join key, and why the obvious one is wrong

🛑 **Join on `id`. NEVER on `place_id`.** `place_id` is GOTW's SQLite rowid,
carried only so *they* can join to today's working DB — a re-parse renumbers it.

```
id = sha1(volume_filename | A-Z-only headword | page_start)[:16]
   + sha1(entry_text)[:6] + ":" + ordinal_within_entry      e.g. ee32a88eebf5cd8f23eb89:1
```

⚠ **The entry-text digest is not decoration.** The obvious key — the review
signature `sig(volume, headword, page)` — is stable across a re-parse but **not
unique**: the book prints the same headword twice on one page often enough to
matter (two different `CHANG-CHU-FU`, one in Kyang-su and one in Fokyen;
`ARCHANGEL` the city and `ARCHANGEL` the government). Measured: **13 Chinese and
34 Russian ids collided, 94 rows.** *A key that is right 98% of the time silently
merges two different places and nothing complains* — for a train/test split that
is worse than an unstable key, because the contamination is invisible.

### Return shape — a browser UI, not a CSV

SG asked for a review UI rather than a spreadsheet, live at
`worldhistoricalgazetteer.github.io/gazetteer-of-the-world/toponyms/`: the
specialist **picks from places the index actually holds** near each entry's
printed coordinates, with the 1856 entry text and a map alongside. Recognition
rather than typing, which should raise the return rate.

It exports **one JSON object with a `rows` array** (not JSONL — a browser
download is naturally one file). Per row: `id`, `place_id`, `headword`,
`printed_hierarchy`, `printed_variants`, `printed_lat`, `printed_lon`, `volume`,
`page`, `coordinate_flags` (per failure mode, empty when usable), `answered`,
`skipped`, `chosen_whg_id`, `confident`, and the three language fields. Every
offered row is present, including untouched and unanswerable ones.

⚠ **A corrupt coordinate produces a weaker row, not a wrong one.** The UI applies
the cleaning rule at generation time, so a flagged row gets no map pin and a
lexical-only candidate list — otherwise a bad coordinate would silently produce a
pick-list for the wrong part of the world, which the reviewer could not detect.
Those rows are still offered; the flags let us stratify them out.

### The override hazard — a resolvable anchor that names the wrong place

🛑 **Found by `gotw-eb` while checking a claim rather than asserting it
(`a17a4a6`).** A specialist picks *Changzhi* from the candidate list — so
`chosen_whg_id` is set — then types *Changzhou* into the romanisation field
because the option was close but wrong. **The id stays anchored to the place they
REJECTED.** The row arrives with an anchor that resolves cleanly, looks verified,
and names a different place from the one the answer is about.

⚠ **This is `indexing-9c`'s exclusion defect (§6.3) arriving by a different
door, and in its worse form.** Not *"no anchor"* but *"a confidently wrong
anchor"*: the exclusion closure would run on the wrong place's co-referents, so
the negative sampler could draw a genuine name of the query's own place **while
believing it had excluded exactly that**. The empty case is at least detectable;
this one is not.

**The export now carries information rather than a decision** — flags, not
filtering, the same rule asked for on coordinates:

| field | meaning |
|---|---|
| `chosen_whg_id` | the picked option's `place_id` (unchanged) |
| `chosen_whg_name` | the picked option's **name**, so it can be compared |
| `overridden` | `true` when the typed romanisation differs from that name |

**DECISION (ours to take, and taken here):**

* **`overridden: false` with a non-null id** — the clean case. Anchored; reuse the
  normal closure.
* **`overridden: true`** — **do not trust `chosen_whg_id`.** But do not simply
  discard the anchor either. **First attempt to resolve the typed modern form**,
  which is `indexing-9c`'s own preferred route (§6.3, decision 1) and is what the
  typed field is *for*: a modern romanisation is exactly the thing the index
  should contain.
* ⚠ **Accept a re-resolution only with a corroborating signal.** A name lookup
  that returns one candidate may mean *"the index holds one place of that name"*
  or *"our query was too narrow"* — and accepting it blindly re-creates the very
  hazard, one door further along. Constrain by the row's `modern_province` /
  `modern_region`, or by the printed coordinate where `coordinate_flags` is empty.
  **No corroboration, or an ambiguous result → treat as unanchored**, use the
  typed name as the pair, and let `allow_unanchored` handle it.
* **Never drop the row.** `gotw-eb` is right that an override is usually a
  *better* answer than the pick — it is a correction — just not an anchored one.

🛑 **THAT GUARD IS DESIGNED ON A SIGNAL THAT MOSTLY DOES NOT EXIST.** `gotw-eb`
measured it against the deployed packs rather than letting it ship:

```
administrative context on candidate places
  Chinese     0 of 20,682 options carry region/admin_unit/subregion   0.00%
  Russian    10 of 20,485                                             0.05%

rows with NO usable printed coordinate
  Chinese  1,247 of 2,413    52%
  Russian  3,333 of 3,534    94%
```

**Both corroboration routes are absent on the majority of Chinese rows and almost
all Russian ones.** The index supplies no administrative context for these places
at all — `region`, `admin_unit`, `subregion` null and `relations` empty on every
candidate sampled — so the UI cannot prefill `modern_province`, and a reviewer who
has just picked a name from a list has no reason to fill an optional-looking box.

⚠ **The failure mode is the one this document keeps describing.** Every override
on those rows falls back to unanchored, which is *safe* — but **the re-resolution
route would be dead precisely on the population it was designed to rescue, and it
would look like it was working, because nothing errors.** A guard that never fires
reports the same thing as a guard with nothing to catch.

**Mitigation shipped by `gotw-eb` (`2740c6f`):** on a row with no usable
coordinate the province field is highlighted, placeheld *"please fill this one
in"*, and carries its reason — that the entry has no usable coordinates so the
province is the only independent check available. Asked for on exactly the rows
that need it and nowhere else. It cannot be promised, only requested.

### A third corroboration route, to be measured before it is built

**Siblings.** Every row carries `printed_hierarchy` from the 1856 book — a signal
that is independent of the index, independent of the row's own coordinate, and
**present on rows that have neither**. Entries sharing a printed parent should
resolve to places near one another. So a re-resolution is corroborated when it
lands near the centroid of its **cleanly-anchored** siblings.

⚠ **Only clean siblings may vote** — `overridden: false` with a coordinate-backed
anchor. Never another re-resolution, or the corroboration chains and one bad
anchor propagates through a whole printed division.

**The measurement that decides whether this is worth building, and it is
`gotw-eb`'s to take:** *of the rows with no usable coordinate, how many share a
`printed_hierarchy` with at least one cleanly-anchored sibling?* If that is small,
the route is as dead as the other two and should not be written. **It is a
bootstrap, and bootstraps can have no seed.**

### Consequences of this spec inside the harness

**`unanchored_no_exclusion` was a scalar, and the spec above makes it a mixture**
(`indexing-9c`, `7ff1e74`). "Unanchored" now means at least three different
things: a row that never had an anchor; a row whose anchor the specialist
**rejected** and whose typed correction failed re-resolution for want of
corroboration; and a row that re-resolved ambiguously. 🛑 **The middle group are
corrections — the higher-quality answers in the pack.** One count averages the
best rows together with the ones nobody could place: the
two-populations-readable-as-one failure, occurring one level down, inside the
field added the day before to prevent exactly that. Now broken down by `source`,
with a test asserting two unanchored sources come back separated.

**A rule worth extracting from the key collision**, stated by `indexing-9c` as a
rule rather than a fact about GOTW:

> **A key that is stable is not thereby unique**, and a split on a non-unique key
> **trains and tests on the same entity while reporting a clean separation.**
> Nothing in the output distinguishes that from a real result.

### The ordering bias is invisible to per-script-pair reporting

🛑 **`indexing-9c`'s finding, and it lands on the benchmark rather than the
ingest.** If a partial return is systematically the places the book documented
best, then **any metric computed on it is an upper bound** — and the
per-script-pair breakdown *cannot see this*, because **difficulty is orthogonal to
script pair**. Every cell is uniformly optimistic, so nothing looks anomalous and
no comparison between cells reveals it.

⚠ **This is the same shape as the 35.1% romanisation artefact (§8.3): a corpus
property masquerading as a model property.** Both are invisible to the axis the
results are reported on, which is why both had to be found by reasoning about how
the corpus was built rather than by inspecting the numbers.

**Required mitigation:** report the denominator as **"n of N in the pack"** *and*
**where in the ordering the returned rows sit**, so a reader can see that recall
was measured on the easy half.

🛑 **This has a dependency that does not exist yet: the export must carry each
row's POSITION in the pack ordering.** Without it the returned subset's
composition is unrecoverable — we would know the ordering was easiest-first and
be unable to say where any particular return stopped. Requested from `gotw-eb`.

### Reporting requirement — the fire rate needs its denominator

⚠ **Report how often re-resolution fired, split by whether corroboration was
AVAILABLE at all.** Otherwise a low rate reads as *"specialists rarely
override"* when it may mean *"we had nothing to check against"* — and those
demand opposite responses. This is `filters must report their denominator`
arriving in the ingest path.

🛑 **A PARTIAL RETURN IS NOT A RANDOM SAMPLE, AND THAT NOW BITES THE TRAIN SIDE
TOO.** The pack leads with the rows most
likely to be answered quickly — usable coordinates, stated hierarchy, hyphenated
transcriptions — so **early returns are systematically the easy ones**. In the
Chinese ordering, everything before row ~1,200 is coordinate-bearing and
everything after is not. Any figure computed on a partial return must say so, and
must not be presented as a corpus estimate.

⚠ **`gotw-eb` sharpened this once told the pack is training data, and the
sharpening is correct:** a partial return is not merely a biased *measurement*,
it is a biased **training set** — systematically the places the book documented
best. **Training on that alone teaches v8 the easy half of the
historic-orthography problem and lets it look better than it is on the half that
actually fails.** So the ~1,200 boundary must constrain the **TRAIN** side, not
only the reported split.

⚠ **And stratifying cannot manufacture what never arrives.** If only the easy half
is ever returned, the honest response is not a cleverer split but a stated limit:
**report the train set's composition beside the test set's, and do not claim
corpus-level improvement from a corpus-biased sample.**

🛑 **THESE ARE NOT TWO CAVEATS. THEY ARE ONE, COUNTED TWICE** — `gotw-eb`'s
observation, and it is the sharpest structural point in this section. The
coordinate-bearing rows are **both** the ones the anchor guard can validate **and**
the ones ordered first for return. So:

> the anchored, corroborated, cleanly-resolvable subset is *the same population*
> as the well-documented easy half. **The rows where anchoring is hardest are the
> rows where the book is thinnest**, and they will be simultaneously
> under-returned, under-anchored and under-validated.

Writing them as separate bullets in a limitations section would understate the
problem by implying they might partially cancel. They compound. **State it as one
correlated deficit.**

*"Stratifying cannot manufacture what never arrives — nor can it manufacture the
ability to check what does."*

🛑 **THE DELIVERY IS NOT AGREED, AND V8's SCHEDULE MUST NOT ASSUME IT.**
`gotw-eb`'s own words: nothing has been sent, no specialist has committed, and
the return rate is unknown — *"It could be 30 rows or 2,414. If v8's schedule
depends on this arriving, that dependency is currently unfunded and Stephen
should be the one to decide it."* **That was correct at the time it was written.**

✅ **SG HAS SINCE TAKEN IT: the return is "in hand, completed, in a matter of
days" (5 Sep).** So the dependency is funded and the pack is expected before
training data is generated.

⚠ **It still blocks nothing, and the design must not start assuming it.** The
harness is source-agnostic by inspection (§6.3), so an LHPN-only run is a
first-class shape rather than a degraded two-source one — which means a shortfall
in the return rate costs coverage, not correctness. **What the pack buys is a
CJK historic stratum in the evaluation.** Without it, v8 could only claim
improvement on Welsh↔English historic orthography; with it, the claim extends to
Chinese transcription and can be *checked* against 1,166 coordinate-anchored
rows. Keep the target standing on the LHPN pairs and let the pack widen it.

### Four constraints on using it, from the party that built it

1. **Contamination protocol — settled.** `gotw-eb` proposes we reserve a split it
   never sees. A private split does not actually work, because it holds the whole
   pack and can derive the complement. **So the protocol is the other way round:
   we publish the TRAIN ids and keep nothing secret.** `gotw-eb` then evaluates
   its cascade on the complement, which is exactly the set v8 did not train on.
   Neither side is circular, and nothing depends on either side's discretion.
2. **`confident` is signal, and `guess` rows are excluded or heavily
   down-weighted.** The brief tells the specialist that an uncertain answer
   marked uncertain is useful and a confident-looking guess is not — so the
   column carries information only if we honour it.
3. ⚠ **The 30 pilot rows are chosen as cases GOTW currently gets WRONG.** They
   are right for measuring improvement and **wrong for estimating any base
   rate**. They are kept as a named stratum and reported separately — never
   pooled into a headline, and never fitted to.
4. ⚠ **Russian pairs are weakly verifiable.** Only 201 of 3,535 rows have a
   usable coordinate (5.7%) against Chinese 1,166 of 2,414 (48.3%), so roughly
   one Russian row in eighteen can be independently checked. Russian pairs do not
   get called gold without that caveat attached.

**Return format requested:** JSONL, one object per row, **every offered row
present including the ones with unusable coordinates**, carrying the source
fields, the filled fields, `confident`, provenance (volume, page), and explicit
per-row validity flags rather than pre-filtered content. We can drop rows; we
cannot un-drop them, and §7.1 above is what happens when a cleaning rule is
applied upstream of the people who need to see what it removed.

## 6.3 What the second target broke in the benchmark code — found before it ran

Adding historic orthography (§6.1) invalidated assumptions in three places in
`evaluation/`. `indexing-9c` found and fixed them (`b1cbbd3`) rather than
discovering them in a training run. **One of them is this campaign's signature
fault occurring inside the tool built to detect it.**

🛑 **The silent one.** `build_corpus.forbidden_for` does
`own_names.get(pos.place_id, ())` and `closure.get(pos.place_id, ())`. A pair with
**no `place_id`** — which is every externally-supplied historic pair — returns
empty from both, and **an empty forbidden set is indistinguishable from "this
place has no co-referents"**. Every negative drawn against an LHPN pair would
have been unfiltered, so a "negative" could be a genuine name of the query's own
place. That **penalises the model for being right**, and surfaces only as a lower
AUC: no error, no warning, a census that looks normal. *Absent input treated as
nothing-to-do.*

**Fixed:** `build_negatives` raises `ExclusionImpossible`, naming the first
offending pair and its source; `allow_unanchored=True` accepts them
*deliberately*; and the census reports `unanchored_no_exclusion` **beside** the
totals, so the two populations can never be read as one. `forbidden_for` also
now aborts when a place produced a positive but did not come back from the index,
rather than reading an `mget` miss as "no names".

⚠ **The one that would have produced nothing at all.** `cross_script_pairs`
hard-filtered `asc != bsc` inside a list comprehension, dropping **every
same-script pair**. Welsh↔English are both `LATIN`, so an LHPN pack routed
through it would have yielded **zero pairs and looked like a data problem**. Now
`require_cross_script`, a decision the caller makes.

🛑 **But no parameter substitutes for the external pack.** Co-attestation cannot
generate a pair whose historic half is not in the index, at any setting. That is
§6.1's point restated from the code side, and the docstring now says so.

**Bookkeeping:** `Positive` gains `source` and `has_place` (defaulted, so
positives written before the change still load); the manifest reports positives
by source; and `pairs_per_place` divides by **anchored pairs only** — counting
distinct `place_id`s would have folded every external pair into a single phantom
"place" and deflated the ratio.

### The generalisation, which is worth more than the three fixes

`indexing-9c` corrected its own account of how it found these, and the correction
is the useful part. It first said it found them *"by asking what a new input class
does to an old default"* — accurate, but flattering, because **it only asked
because it was told the scope had changed. The trigger was external.**

🛑 **The standing version:** *whenever a scope decision is reversed, re-read every
default that was chosen under the old scope.* **A default is an unstated
assumption about the input distribution, and a scope change silently invalidates
it** — without touching the line, without failing a test, and without appearing in
any output. `"I thought to ask"` does not generalise; that rule does.

Note what it would have cost here: `own_names.get(place_id, ())` is *correct* code
under co-attestation and *wrong* code the moment a positive can lack a
`place_id` — and the census looked normal either way.

⚠ **Ranking the two by cost of the resulting investigation, not by subtlety.** The
silent exclusion is the subtler bug; **the cross-script filter is the more
expensive one.** A wrong number gets argued with. *Zero pairs* from an LHPN pack
sends someone to the pack, the parser, the licence and the encoding before the
filter — a search that starts at the wrong end and can run for a day. Both are
"the absence looks like someone else's problem"; the filter version distributes
the cost onto whoever owns the data.

✅ **The harness is source-agnostic, checked rather than asserted.** `source` is a
free-form string aggregated with a `Counter` (`corpus.py:214` default only, `:292`
count, `:302` report, `negatives.py:135` names it in the abort). Nothing branches
on a source name. So **an LHPN-only run is a first-class shape, not a degraded
two-source one** — if the Chinese pack never arrives nothing needs changing and no
code path goes untested, and if it arrives late it is a new value in a string
column.

### Two decisions handed to whoever builds the LHPN pack

1. **What is the exclusion for an unanchored pair?** `allow_unanchored=True` is
   honest but weak: those negatives are drawn from the whole haystack with
   nothing filtered. **If LHPN rows carry a modern place name that resolves in
   the index, look it up and reuse the normal closure** — at which point the
   pairs stop being unanchored at all. Try that before the flag.
2. **The per-script-pair reporting axis stops discriminating.** Every LHPN pair
   lands in one `LATIN→LATIN` cell alongside ordinary same-script
   co-attestation, so historic orthography would be **averaged with whatever else
   is Latin-to-Latin**. `source` is now on the `Positive`, so the fix is to report
   by `(script_pair, source)`. Deferred deliberately: the pack's shape decides
   whether that is the right key.

### An addition to the retrain's acceptance criterion

⚠ **Measure the geometry at the corpus size it will be judged at, and state the
size.** `nn_gap` at 6,000 names passes comfortably for a model that is twice over
the threshold at 1M, so **a v8 evaluated on a small sample could clear the gate
and still saturate in production**. Effective rank is stable across sizes and is
safe to quote at any *n*; the neighbourhood statistics are not. This is the
practical form of §8.2's `n^-0.22` finding and it belongs in the gate, not just
in the discussion.

## 6.4 The published deposit is fixed — and the repo was never wrong

✅ **`docuracy/symphonym-v7` `config.json` now declares `num_scripts: 20`**
(committed via the HF web editor, 5 Sep; SG authenticated, the edit and commit
were made from this session).

**The repo was already correct**, which is worth stating because the obvious
assumption — that a published/repo divergence means the repo drifted — was wrong
here. Verified before touching anything:

```
hf/vocab/script_vocab.json   script_to_id      20 entries
hf/config.json               num_scripts       20
hf/model.safetensors         script_embed.weight  [20, 16]
PUBLISHED config.json        num_scripts       25   <- the only divergence
```

A **whole-file** diff, not a spot check: the published file differed from the repo
copy in **exactly one field**, and the published file **contradicted itself** —
its own `g2p.scripts_covered` already said `20`. Anyone building
`UniversalEncoder` from the published config alone would allocate a 25-row script
embedding and then fail to load the checkpoint, or silently mis-shape it.

**Verified after committing by re-fetching the raw file**, not by trusting the
editor: the published `config.json` is now **byte-identical** to `hf/config.json`
and parses. The commit changed no weights and no vocabulary.

🛑 **A SECOND DEFECT IS STILL LIVE IN BOTH COPIES, and was deliberately not fixed
in the same commit** (it was outside what was authorised). The `index` block
reports:

```
"total_toponyms": 66924548      live index: 72,703,777
"embedding_coverage": 1.0
```

Since the two files are now byte-identical, **this is wrong in the repo and on the
deposit equally**. It is §11's standing item — *date it or derive it* — and it
needs a decision rather than a value, because a hard-coded count is stale the
moment the next re-ingest runs.

## 6.5 CORRECTION — the historic forms are NOT all missing from the index

🛑 **This session claimed, repeatedly and to two peers, that "co-attestation
cannot produce a historic pair, because the historic form is usually not in the
index at all". Measured against production, that is HALF WRONG**, and the half
that is wrong changes what the specialist pack is for.

```
historic form     docs | modern form    docs | SHARED place_ids
  Peking            40 | Beijing          90 |  5   gn:1816670, gn:2038349
  Nanking           10 | Nanjing          62 |  7
  Canton            60 | Guangzhou        68 |  6
  Tientsin           9 | Tianjin          72 |  5
  Amoy               3 | Xiamen           46 |  2   gn:1790645, wd:Q68744
  Tiflis            25 | Tbilisi         112 |  5
  ---------------------------------------------------------------
  Keang-su           0 | Jiangsu          57 |  0   <- the 1856 pack's forms
  Chang-Che-Hyen     0 | Changzhi         29 |  0
```

**The well-known historic romanisations are already in the corpus and already
co-attested to the same `place_id` as their modern form.** What is absent is the
*obscure tail* — which is exactly what an 1856 gazetteer contains.

### What that changes

1. ⚠ **AMENDED — see §6.5b. There is a large free unlabelled corpus, but it is
   NOT a historic-orthography corpus**; it is a cross-romanisation one, in which
   historic forms are a small unmarked minority.
2. 🛑 **For TRAINING, the historic label is not required.** `(Peking, Beijing)` is
   a useful positive pair whether or not anything calls it historic. **The label
   matters for evaluation stratification, not for the objective.** This is the
   distinction the earlier framing missed entirely.
3. ⚠ **So the pack's value is narrower and sharper than "supplies the missing
   pairs":** it supplies (a) the **hard tail**, which genuinely is absent, and
   (b) **labelled evaluation data**, which nothing in the index provides.

### 6.5b The conclusion survives; the DESCRIPTION of it does not

🛑 **`indexing-9c` measured the population my probe pointed at (`d5bcd49`), and
"a free historic corpus" is the wrong name for it.** Over 20,000 sampled places,
**5.00% carry two or more distinct Latin names alongside a non-Latin one** —
~2.56 M places corpus-wide, 30,542 same-script pairs in the sample. Classified by
how far the two forms differ once case-folded and stripped of combining marks:

```
identical when folded    1.0%   'Lac à Robert' ~ 'lac à Robert'
near      (>=0.85)       5.8%   'Agía Marína' ~ 'Ayia Marina'
mid   (0.55-0.85)       35.3%   'Chŏm-ni' ~ 'Jeomni'          (M-R vs RR)
far       (<0.55)       57.9%   'Zhongzheng Village' ~ 'Tiong-chèng-lí'
```

**It is dominated by diacritic and case variants, competing romanisation systems
(McCune-Reischauer against Revised Romanization), different languages' readings of
the same characters (Mandarin against Taiwanese Hokkien), and full-name-against-
short-name pairs.** Historic forms are present and are a **small, unmarked
minority** — and **no edit-distance band isolates them**: `Peking`/`Beijing` lands
in the same band as `Chŏm-ni`/`Jeomni`. The 57.9% "far" bucket is almost entirely
truncations and cross-language readings, not history.

**So the claim splits, and both halves matter:**

* ✅ **Excellent unlabelled training data for cross-romanisation phonetic
  matching** — which *is* v8's objective. `(Chŏm-ni, Jeomni)` and
  `(Zhongzheng Village, Tiong-chèng-lí)` are exactly the positives the model
  should learn from, and the point that **the historic label is not required for
  training** is precisely why this works.
* 🛑 **NOT an evaluation set for historic orthography.** The label that would
  stratify it is the one thing it does not carry.

⚠ **THE FOURTH INSTANCE OF §8.3b, AND IT LANDS ON THIS SESSION'S OWN EVIDENCE.**
The probe behind §6.5 was **eight names already known to be historic** — Peking,
Nanking, Canton, Tientsin, Amoy, Tiflis. **Probing for what you already know finds
it and tells you nothing about the population**, and the population looks nothing
like the probe set.

> **Selection effects reach the evidence we gather ABOUT a corpus, not only the
> corpus itself.**

That is a different door from the previous three: instances 1–3 were confounds in
the corpus; this one is a confound in the *sampling of evidence*. The tell was
that a claim about a **population** rested on a hand-picked **probe** — and every
earlier instance today was a claim about a population that turned out to be a
claim about a filter.

🛑 **A WARNING ABOUT THE OBVIOUS NEXT STEP.** Isolating the historic minority needs
a definition of *"not the modern romanisation"*, and every obvious one is
contaminated:

| candidate filter | what it actually selects for |
|---|---|
| edit-distance threshold | **phonetic distance — the thing being measured** |
| "differs from the preferred name" | a *source* property, not a linguistic one |
| "≠ anyascii romanisation of the native form" | **where romanisation is lossy — the stratum v7 already wins on** (§8.3) |

**Any such filter needs a witness from outside the matching problem** — the same
corroboration principle as the GOTW anchor ladder (§6.2). Until one exists, use
this corpus for training and do not build an evaluation stratum out of it.

### GeoNames `isHistoric` — present, populated, and not what we mean

Measured over the full `alternateNamesV2` (19,036,500 rows):

```
isHistoric == 1        43,380   (0.23%)
  ...with a from/to     8,428
top languages   en 10,448 · (none) 6,750 · ru 3,763 · de 2,530 · fr 2,389
                zh: ABSENT from the top 20
```

🛑 **And it does not flag the forms we need.** Of 19 `Peking` rows, **0** are
flagged historic; `Tiflis` 0 of 12; `Nanking` 0 of 3; `Amoy` 0 of 2.

⚠ **`isHistoric` IS read by our ingest and then discarded** — `settings.py:564`
names the column, `geonames-toponyms.py` uses only `from`/`to`. **Wiring it up
would gain almost nothing**, which is worth recording so nobody spends a day on
it: the flag is real, populated, well-documented, and marks a different thing.
The `ru` entries are Cyrillic *renamings* (`Сталин`, `Кешишкенд`) — historic
**names**, not historic **romanisations**.

**This is `rewt-c7`'s warning arriving exactly as predicted**, and its framing was
better than mine: I was guarding against a field that is EMPTY; the field is
populated and wrong for the purpose. *"The question is not whether the flag is
there — it is whether the thing it flags is what you mean."*

> 🛑 **`place#244` IS CLOSED (not-planned, 5 Sep) — DO NOT ACT ON IT.** SG closed
> it because the premise did not survive measurement: `attested_at` is the
> deliberate, documented encoding and the 2026 attestation was intentional, so the
> "inert filter / placeholder" framing does not stand. The backfill ran against
> production with no measurable change and no damage, so **nothing needs
> reverting**. ⚠ This session edited `#244`'s title, banner and acceptance
> criterion publicly, and **those edits now sit on a closed issue** — everything
> load-bearing was carried into **[`place#246`](https://github.com/WorldHistoricalGazetteer/place/issues/246),
> which is the sole authority and does not reference `#244` at all.**
>
> **`place#246`, standalone, five items:** ① `osm-places.py` never reads
> `start_date`/`end_date` while `ohm-places.py` does · ② three independent
> `datetime.now().year` implementations · ③ `"in": null` written into stored
> timespans · ④ **`places` and `toponyms` disagree about TGN's name inventory —
> 1,277,683 of 2,991,143 tgn places (42.7%) have an EMPTY `toponyms[]` in
> `places`** · ⑤ Getty dates 9,450 concepts, the index holds 2,712, and the
> backfill structurally cannot land them because of ④.
>
> 🛑 **SG's directive, carried as a block above the audit so it cannot read as
> advice: every item must be resolved in the INGESTION CODE whether or not the
> data is also patched. The two are not alternatives, and no item closes on a
> patch alone.** Anchored to `postmortem-ingestion-faults.md` rather than asserted
> — eleven of sixteen registered faults recur because the data was repaired and
> the producer was not. ⚠ Item ⑤ carries a **verify-first exception**:
> `tgn_temporal.timespan` was already rewritten for place#164 and may be correct
> with the live index simply predating it, so the script must be *run* before it
> is called broken — and where a script fix proves unnecessary, that is closed on
> **evidence**, not assumption.

## 6.6 ~~`toponyms.timespans` is EMPTY~~ — 🛑 **RETRACTED: the zero was a query bug**

🛑 **STRUCK 5 Sep. `toponyms.timespans` is populated on 23,796,980 places, not 0.**
`indexing-9c` retracted its own measurement: `timespans` is itself **`nested`
inside** the `toponyms` nested field, and the query used a **single** `nested` on
`toponyms` with `exists` on `toponyms.timespans.start.in` — **the wrong nesting
depth, which matches nothing and returns 0 without error.** Correctly:

```
start.in       1,631,485      start.latest   23,796,980
start.earliest   118,310      end.earliest   23,622,440
end.in           436,371      end.latest         11,553
```

⚠ **THE THREE CONTROLS COULD NOT HAVE CAUGHT IT, AND THAT IS THE LESSON.**
`toponyms.toponym_id` (49,848,837) sits at the **`toponyms`** depth; place-level
`timespans.start.in` (82,508) is a **different field entirely**. **Both were
correctly addressed, so both passed while the query under test was
mis-addressed.**

> **A control at a different nesting depth from the failing query is not a
> control.** It testifies about a different query.

⚠ Worse: the audit never tested `start.latest` or `end.earliest` — **the only two
fields TGN actually populates** — so the two that mattered were exactly the two
omitted. *"A zero with three witnesses"* is what this session called it, and the
witnesses were testifying about something else.

**What survives is narrower, and true for a different reason:** a **uniform 2026
placeholder** is useless as a stratification axis — not through *absence* but
through *uniformity*. Everything below about the placeholder stands; the claim of
emptiness does not.

~~🛑 **`indexing-9c` measured the field that a historic-orthography evaluation
stratum would most naturally come from, and it holds nothing.**~~

```
toponyms.timespans.start.in          0    of 51,187,900 places
toponyms.timespans.start.earliest    0
toponyms.timespans.end.in            0
tgn places specifically              0    of 2,991,143

CONTROLS -- because a zero is a claim about a query as much as about data
  field mapped?          YES  six leaf fields, all `long`
  query shape works?     YES  same nested exists on toponyms.toponym_id -> 49,848,837
  can a populated
  timespan be seen this
  way at all?            YES  place-level timespans.start.in -> 82,508
```

**Not an unmapped field, not a broken query shape, not a field type the query
cannot see.** Per-toponym dating is mapped, universally empty, and would be
detectable if present. *(Three controls for one zero — the discipline
`filters must report their denominator` asks for.)*

✅ **This settles one thing outright, for everyone:** `toponyms.timespans` **cannot
be used as a stratification axis today**, by anyone, for any namespace. If the
historic-orthography evaluation stratum was going to come from per-toponym dates
already in the index, **it cannot**.

### But the code to fill it already exists and has never run

* `authorities/tgn-places.py:178` — *"Term-level temporal: `<term_uri>`
  estStart/estEnd YYYY → toponym timespan"*, builds a `term_dates` map, prints a
  dated-terms count.
* `processing/tgn_temporal_backfill.py` — written for exactly this, `--execute`
  gated, two-step extract/apply, patches the live index with no re-ingest. Its own
  docstring: *"TGN ingestion used a `[2025, 2025]` placeholder for every timespan.
  The source actually holds sparse real dates."*

⚠ **THIS HAS THE EXACT SHAPE OF `update_merge` AND THE POST-REBUILD ENRICHMENT
GAPS: code that exists, was written deliberately, and never ran.** So the
possibility that someone tried it and found the dates too sparse to matter is
live, and must not be assumed away.

🛑 **"TGN has dated variants" is currently a claim from a DOCSTRING, not a
measurement.** The docstring says *sparse*, and sparse could be 200 or 200,000 —
which is the difference between an evaluation stratum and a curiosity.

**Being settled now.** The `extract` step reads the release
(`/ix1/ishi/data/authorities/tgn/explicit.zip`, 1.15 GB) and **writes a patch file
without touching any index**, so it is a pure measurement. Submitted to Slurm as
job **11157269** (`htc`, not the pitt VM — a full streaming parse of a 1.15 GB
release is not VM work). **Only `extract` is being run. `apply` is NOT, and needs
its own authorisation.**

⚠ **Why this matters to the source survey handed to `gotw-eb`:** if TGN's dates
are real and numerous, **the lead is far shorter than an external acquisition** —
no licence conversation, no specialist time, no download. It is running an
existing module against a release already on disk. Worth knowing *before* anyone
opens a conversation with Getty.

## 6.7 THE SOURCE SURVEY — answered. Verdict: VALIDATE, barely REDUCE, cannot REPLACE

`gotw-eb`, measured on **staging** rather than production — a full copy of the
same indices, so the read-only constraint **dissolved rather than being worked
within**. Worth remembering as a technique: the cheapest way to satisfy a
constraint is sometimes to move off the resource it protects.

```
places with >=1 name variant dated before 2020   (track_total_hits, real totals)
  tgn          0 of  2,991,143   0.000%
  wd     174,500 of 11,459,393   1.523%
  chgis        0 of     81,292   0.000%
  gn       1,892 of 13,454,817   0.014%
```

🛑 **The premise of my own brief is false as deployed: TGN carries NO dated name
variants**, and this is the warned trap at one further remove. Not *"populated and
wrong"* — **supported end to end and empty.** `authorities/tgn-places.py` parses
Getty's per-term `estStart`/`estEnd` into `term_dates` (:153) and writes
`timespan(td[0], td[1])` when present (:255); the schema has nested
`toponyms.timespans`. **Every layer works. Getty's dump supplies nothing.** Every
sampled TGN toponym carries the identical degenerate stamp
`{start:{latest:2026}, end:{earliest:2026}}` — **100% populated, zero
discriminating information.**

### ✅ The one real signal, and it is not dates

**TGN tags Chinese romanisations by SYSTEM** in BCP-47: bare `zh-Latn` against
`zh-Latn-pinyin-x-notone` / `-x-hanyu`. **67,192 TGN places carry both**, and the
pairs are the target shape:

```
Hsia-ch'i Tao              -> Xiazhi Dao
Ch'eng-an-hsien            -> Cheng'an
Kuang-hsi Sheng            -> Guangxi
Chung-hua Jen-min-kung-ho-kuo -> Zhonghua
```

⚠ **Do NOT take 67,192 at face value.** Hand-adjudicating 20 pairs: roughly half
are genuine Wade-Giles↔pinyin; the rest are English conventional forms
(`New Taipei → Taibei`), **Japanese colonial-era romanisations**
(`Kashoto Island → Lü Dao`), or outright **renamings**
(`Anhua Xian → Dongping`). **~30–35k usable after filtering, and the filter is not
obvious — the tag marks *pinyin vs not-pinyin*, not *modern vs historic*.**

✅ **Its virtue is exactly what §6.5b demanded: a witness from OUTSIDE the matching
problem.** It is Getty's cataloguing, not our edit distance — so unlike every
filter in that section's table, it cannot select for the thing being measured.

### The other four, ranked

| | source | verdict |
|---|---|---|
| 2 | **GeoNames name-level dates** | Right kind, negligible volume — 1,892 places (0.014%). Genuinely name-in-use semantics: `gn:10426575` has `Lich@en` starting 2004 and `Գյոլջգին@hy` **ending** 2004. Renamings, not romanisations. |
| 3 | **Wikidata dates** | Populated, wrong thing. 174,500 places, but they date the **ENTITY**: `wd:Q1001069` stamps 1879 on its German, Albanian, English *and* Armenian names alike. **Cannot separate historic from modern by construction.** |
| 4 | **Russian** | Nothing. 0 TGN places carry both a bare `ru-Latn` and any `ru-Latn-<variant>` — TGN does not tag Russian romanisation systems. No in-house substitute exists; BGN/PCGN vs GOST must come from outside. |
| 5 | **CHGIS** | 0 dated terms across all 81,292 records. |

### 🛑 The decisive measurement — coverage of the actual tail

**Of 400 distinct 1856 Chinese headwords tested against the WHOLE index, 15.0%
match an indexed name exactly** — by namespace: `gn` 43, `wd` 9, `osm` 3, `gb` 1,
**`tgn` 4**.

**So TGN's 67k romanisation pairs, whatever their purity, barely touch our
corpus.** They are a decent unlabelled-to-weakly-labelled **training** resource
and they do not answer our questions. ✅ **The specialist spend is justified**,
which is the useful outcome.

### ⚠ Three of the survey's own numbers were wrong before they were right

Recorded because the failure modes are reusable:

* an `exists` query on a **nested subfield** returned 0.00% coverage — a query
  artefact, not a fact;
* totals of **exactly 10,000** across four namespaces were Elasticsearch's default
  `track_total_hits` cap, **not a coincidence**;
* a `term` query against the **analysed** `toponyms.label` returned **0 of 400**
  headwords — *"a clean, quotable, completely false zero"*, caught **only because
  Peking/Nanking/Canton/Beijing were run as a positive control and failed too.**
  On `.keyword` the real figure is 15%.

🛑 **That last one is §6.5b arriving at the other end of the conversation**: a
probe that could only return the answer half-expected. **The positive control is
the entire reason the reported number is 15% and not 0%** — and a 0% here would
have justified the specialist spend just as neatly, for entirely false reasons.

### The job I submitted is still worth completing — it asks a different question

`gotw-eb` measured **the INDEX**. Job **11157269** parses **Getty's RELEASE**. They
discriminate between two very different states: *Getty ships no dates* versus *we
lost them in ingestion*. The survey's evidence points at the former (`term_dates`
is parsed and written correctly), but that is an inference from reading the code,
not a measurement of the dump.

### ⚠ A ceiling that fixes how the result may be read — computed BEFORE it lands

`indexing-9c`, deliberately in advance: **TGN holds 3,167,601 toponym entries
across 2,991,143 places — a mean of 1.06 names per place.** The surplus over
one-name-per-place is **176,458**, and a *pair* requires a second name, so:

> 🛑 **At most 176,458 TGN places — ≤ 5.9% — can contribute a pair at all.**

That is a **rigorous ceiling, not an estimate**: it holds even if every surplus
name lands on a distinct place, and the true figure is lower wherever one place
holds three or more.

**So the dated-term count cannot be read as a yield.** The chain a dated term must
survive:

```
dated term                                  <- ALL that 11157269 measures
  -> on a place that has ANOTHER name       <= 176,458 places  (hard ceiling)
  -> the other name a DIFFERENT form, not a case/diacritic variant
  -> the date MARKING it historic, not merely recording currency
```

Each later step is multiplicative and none is measured. **The reading is fixed in
advance: a large count is NECESSARY AND NOT SUFFICIENT; a small count is
DECISIVE.** If it returns hundreds, that closes the question *and* explains why
`tgn_temporal_backfill` was written and never run — a result, not a
disappointment.

🛑 **What the ceiling does NOT bound, so nobody over-reads it.** It bounds
**pairs**, not the value of the dates. **Term-level dates on single-name places
are still worth having for the temporal search filter — which is what the backfill
was actually written for.** Its docstring names the search temporal filter and the
clustering `s.t` fuel; it names no benchmark. **The backfill's own purpose is
untouched by any of this.** What is being bounded is its usefulness as a source of
*evaluation pairs* — a use it was never designed for and that we invented today.
⚠ Do not let *"TGN dates are weak for v8 pairs"* propagate as *"the TGN backfill
is pointless"*.

## 6.8 JOB 11157269 LANDED — Getty ships the dates; WE lost them

🛑 **The survey's inference was wrong, and the difference is actionable.**
`gotw-eb` measured the INDEX (0 dated of 2,991,143) and inferred *"Getty's dump
supplies nothing"*. Job 11157269 parsed **the RELEASE** and found the opposite:

```
9,450 concepts with term-level dates
1,448 concepts with relation-level dates
9,623 patch rows written -> /vast/ishi/staged/tgn/temporal_patch.jsonl
```

**Getty ships the dates. They are absent from our index because the placeholder
overwrote them** — which is precisely what `processing/tgn_temporal_backfill.py`
was written to repair, and never ran. *An inference from correct-looking code is
not a measurement of the input.*

### The pair yield, against the pre-registered reading

`indexing-9c` fixed the interpretation before the number landed: a dated term
must survive four multiplicative steps. **All four survive.**

```
patch rows                          9,623
rows carrying toponym_spans         9,450
rows with >=2 GENUINELY dated names  3,565   <- pairable (placeholder 2026 excluded)
distinct within-place pairs         40,937

dated names per place:  1 -> 5,885   2 -> 2,408   3 -> 695   4 -> 258
                        5+ -> 176    (max 62)
```

**3,565 is comfortably inside the ≤176,458 ceiling**, so the ceiling was not
binding. And the pairs are the real thing — dated name-in-use spans on multiple
forms of one place:

```
tgn:7011929  Dorchestre 600-1500 · Dorcic 600-1000 · Dorkecestre 600-1500
             Dorocine -300-500
tgn:7009095  Mont'Olmo 500-1851 · Pausula 1851-1931 · Corridonia 1931-
tgn:7006796  Tuscana -500-1000 · Toscanella 1000-1850
tgn:7010588  Samarobriva -500-1000 · Samasobriva -600-400
```

### 🛑 What it is, and the two things it is NOT

```
pairable places                       3,565
...CROSS-SCRIPT internally               11   (0.3%)
scripts:  LATIN 3,565 · GREEK 4 · CYRILLIC 2 · ARABIC 2 · CJK 1 · HANGUL 1
```

* ✅ **This IS the labelled historic-orthography evaluation stratum §6.5b said we
  did not have.** ~40,937 dated same-script pairs, labelled by Getty's own
  cataloguing — **a witness from outside the matching problem**, satisfying the
  test that killed every filter in §6.5b's table. It is the only labelled
  historic-orthography data we have found anywhere.
* 🛑 **It does NOT serve the cross-script target.** 11 places of 3,565.
* 🛑 **It does NOT touch Chinese or Russian — CJK 1, Cyrillic 2.**
  **`gotw-eb`'s verdict stands unchanged: the specialist spend is justified.** This
  is European Latin-script historic orthography and answers a different question.

### 🛑 40,937 IS NOT A SAMPLE SIZE — do not quote it as one

`gotw-eb` flagged that within-place pairs are not independent observations; they
share a referent, a source, a cataloguer and usually an etymology.
`Dorchestre / Dorcic / Dorkecestre / Dorocine` is **one place's naming history seen
four ways, not four independent samples.** Measured concentration:

```
top  0.5% of places (   17)  ->  20,872 pairs   51.0%
top  1.0% of places (   35)  ->  30,914 pairs   75.5%
top  5.0% of places (  178)  ->  34,636 pairs   84.6%
top 50.0% of places (1,782)  ->  39,154 pairs   95.6%

largest single place -> 1,891 pairs (62 dated names)
```

🛑 **Seventeen places produce over half the corpus. One produces 1,891 pairs.**
The effective N is **3,565 places, not 40,937 pairs**, and even that is dominated
by a handful of exhaustively-catalogued entries.

**Required:** report **per-place** performance, or **cluster-bootstrap** the
confidence interval with the place as the unit. Never treat pairs as independent.
⚠ This is `rewt-c7`'s 32,850-rows/13,002-geometries trap in the form that is
harder to catch — it produces a **plausible** number rather than an obviously
inflated one, and 40,937 would have been quoted without hesitation.

### ✅ THE EVALUATION SET NEEDS NO `apply` — the two arguments decouple completely

🛑 **`indexing-9c`: the pairs are constructible from the live index TODAY, with no
production write.** Both halves of every example are already present as toponyms
and already co-attested to the same TGN place — checked over *all* matching docs,
`examined == total` in every case:

```
Dorkecestre / Dorchestre / Dorocine   all  -> tgn:7011929
Samarobriva (14 docs) / Samasobriva   both -> tgn:7010588
Toscanella (9) / Tuscana (4)          both -> tgn:7006796
Pausula (3) / Mont'Olmo (1)           both -> tgn:7009095
```

**The ingest did NOT drop the variant names. It only overwrote their DATES with
the 2026 placeholder.** So **the index supplies the pairs; the patch file supplies
the labels** — and the patch file already exists on `/vast`, written by a job that
touched no index.

🛑 **THEREFORE THE BENCHMARK ARGUMENT FOR `apply` DOES NOT EXIST.** This section
deliberately kept the two arguments separate so the newer could not absorb the
older; the separation is now *total* rather than merely disciplined. **`apply`
stands or falls on the temporal search filter and the clustering `s.t` fuel — the
purposes it was written for — and on nothing else.** No future reader can reach
for the evaluation stratum as a justification for a production write.

### What the patch actually fixes — the date filter is INERT on TGN today

## 🛑 SCOPE CORRECTED TWICE — the shape is not a bug, and the defect is ~7 k places

**The number moved 2.99 M → 22.3 M → ~7 k in three hours. Every intermediate
figure was published before it was checked, including by this session.**

🛑 **`{start:{latest:Y}, end:{earliest:Y}}` is NOT a placeholder shape.** It is
`processing.temporal.attested_at(Y)` — *verified by reading it*, not relayed:

> *"The source records the place as it was at `year` … Definitely alive at
> `year`; possibly alive at any time, since neither outer bound is asserted.
> **This is the correct encoding for a dated snapshot.**"*

**So the same bytes carry OPPOSITE verdicts depending on what the source means,
and the meaning is not visible in the data at all:**

* **`osm` 20,581,812 — CORRECT, and NOT part of this defect.** OSM is a
  current-map snapshot: every feature genuinely *is* attested at the dump year and
  its earlier bounds genuinely *are* unknown. `possibly[1500,1600]` passing is
  **the filter behaving correctly on uninformative data**, which is not the same
  as being inert on wrong data. Same for `nl` (4,362).
* **`tgn`'s undated branch is deliberate too.** `tgn_temporal.timespan(None,
  None)` → `attested_at(PLACEHOLDER_YEAR)`, commented *"Undated: all we know is
  Getty listed it in this release"* — itself a place#164 rewrite that fixed a
  **worse** encoding (the old one claimed each place "existed only in 2025",
  excluding the whole gazetteer from every historical filter). Whether that is
  right for a *historical* gazetteer of extinct places is a **design question**,
  not a defect.

### What is actually wrong, measured with a validated query

Doubly-nested (`toponyms` → `toponyms.timespans`), positive control `wd`
`start.in` = 1,177,263:

```
tgn total                       2,991,143
tgn attested_at(2026)           1,712,662   deliberate
tgn real start.in                   2,712   real dates that DID land
tgn start.latest != 2026                0
tgn start.earliest                      0   <- start-only/end-only branches never fired
Getty release, term-dated           9,450   concepts
```

🛑 **Getty dates 9,450 concepts; 2,712 places carry a real date. The gap is
6,738 places** — the defect, and what the patch addresses. Not 2.99 M, not
22.3 M, not 1.71 M.

✅ **Eligibility checked, because the subtraction assumed it.** `gotw-eb` pointed
out that `9,450 − 2,712` silently assumes every Getty-dated concept is *eligible*
to land — if any were filtered upstream for unrelated reasons the gap would be
smaller and the patch would appear to underperform against its own target. That is
this document's own rule aimed back at it, and it was checked rather than argued:

```
patch place_ids present in the index    9,623 of 9,623   100%
...of the toponym_spans subset          9,450 of 9,450   100%

of those 9,450:  already real  2,712  ·  2026 stamp  5,887  ·  neither  851
                 would gain a real date           6,738
```

`5,887 + 851 = 6,738`, reconciling exactly with the subtraction. **So the
subtraction was sound — but only because eligibility turned out to be total, which
was an assumption and is now a measurement.**

**This gives `place#244` a falsifiable acceptance criterion:** the dry run must
report **6,738** documents to modify. A materially different number is a reason to
**stop**, not to proceed.

### 🛑 THE APPLY RAN, REPORTED SUCCESS, AND CHANGED NOTHING — the criterion earned itself

SG authorised it. It reported `ok=9,623 not-indexed=0 errors=0` in 6 s, and **all
six field counts and all seven filter counts are identical before and after**,
with a refresh in between. **The write completing and the fix happening turned out
to be different events**, which is precisely what the criterion was written to
separate. Had the closing condition still been *"re-measure and see if it looks
better"*, this would have closed as fixed.

⚠ **No damage**, checked specifically — a patched doc (`_version 3`) is
byte-identical in its timespans to an unpatched control (`_version 2`), including a
pre-existing `"in": null` the original ingest wrote and this run did not introduce.
*"Reported success and changed nothing" and "reported success and quietly broke
something" look identical from outside.*

**ROOT CAUSE — and it is architectural, not a patch bug.** The patch keys spans by
toponym **name** and writes into `places`. Verified independently against
production:

```
tgn:7011929   places.toponyms[] (1): ['Dorchester']
              toponyms index    (9): Dorchecestre · Dorcic · Dorkecestre ·
                                     Dorchestre · Dorciccaestrae · Dorkecestr ...
tgn:7027627   places (2): Orton, Orton on the Hill
              toponyms (4): + Wortone
tgn:7026383   places (1): Aldworth        toponyms (2): + Elleorde
```

🛑 **The historic forms exist — in the `toponyms` INDEX, not in the `places`
nested array. "In the index" is two things, and they disagree about TGN's name
inventory.**

🛑 **And re-keying the patch cannot fix it.** `_temporal_filter` reads
`places.toponyms.timespans`, so the date filter can only ever see the names
`places` holds — the **modern** ones. A correctly-keyed patch would date
`Dorchester` and leave `Dorchestre` undated, **inverting the intent**: the whole
value of Getty's term-level dates is that they date the *historic* variants.
Also 2,816 of 9,623 targets have an **empty** `toponyms[]`, so there is nothing to
key against at all.

⚠ **This partly overturns §6.8's decoupling claim** — `indexing-9c`'s statement
that the 40,937 pairs are constructible from the live index **still holds, but
only via the `toponyms` index**. It had treated "the index" as one store; it is
two. The decoupling conclusion survives; the reason given for it was imprecise.

**`place#244` is NOT closed**, correctly: the instruction to close came with an
acceptance criterion attached, and the criterion fails. Closing would certify a fix
that does not exist. The structural choice — backfill `places.toponyms[]`, move
what the filter reads, or accept modern-names-only dating — is SG's.

📋 **`place#246`** raised for the ingestion inconsistencies found on the way: OSM
never reading `start_date`/`end_date` while OHM does (sized honestly as
**unmeasured**), three independent `datetime.now().year` implementations, the
`"in": null` writes, and this name-inventory mismatch — each tagged patchable or
needing re-ingestion.

⚠ **This also contradicts the audit's "0 of 1,712,662 carrying a real term
date"**, which was the evidence for widening the issue. It is 2,712, not 0.

### Filed separately, unmeasured

`authorities/osm-places.py` has **no reference to `start_date`/`end_date`** and
calls `_attestation_timespans()` unconditionally; `ohm-places.py` parses both (7
references). Public taginfo reports 19,338,154 OSM objects with `start_date` —
but across **all ~9 bn OSM objects**, not the ~20.6 M named place features we
ingest, **so the overlap with our corpus is unknown.** Settling it needs a PBF
scan on Slurm, not an inference from a global count.

⚠ ~~**SCOPE CORRECTED — 22.3 M places, and TGN is not the largest offender.**~~
*(superseded above; retained because it was published)*
`indexing-9c`'s namespace audit found **`osm` carrying 20,581,812 uniform
placeholders**, byte-for-byte the TGN shape. Total affected **22,298,836**:
`osm` 20,581,812 · `tgn` 1,712,662 · `nl` 4,362. Real dates: `gb` 1,174,027,
`wd` 187,802, `gn` 2,150. `whg` has none, legitimately. `chgis`/`pl`/`clio`/
`dgsd`/`ohm` looked anomalous and are **not** — they use the `start.in`/`end.in`
exact-year convention `_bound_clause` documents and handles.

⚠ **THIS BREAKS THE ISSUE'S ORIGINAL ACCEPTANCE CRITERION.** The patch is
**TGN-only**, so applying it fixes **1.71 M of 22.3 M — 7.7%**. A `tgn`-scoped
re-measure *"will show the filter discriminating beautifully and the filter will
still be inert for 20.6 M OSM places"* — **a label that has stopped describing its
set.** The closing evidence must be **unscoped, or scoped and labelled as
partial**. `place#244` has been corrected in place: title, scope table and
criterion.

✅ **Task 1 closed causally, not by consistency.** `indexing-9c` pre-registered the
prediction that `definitely` mode flips as soon as the window's end year reaches
2026, then measured `[1500,2025] → 2,383` against `[1500,2026] → 1,713,432`. **A
one-year change moves 1.7 M places, and the discontinuity sits exactly on the
placeholder's value.** The residual 2,126 are the fallback branch working
correctly on the few real dates.

📋 **Filed as [`place#244`](https://github.com/WorldHistoricalGazetteer/place/issues/244)
and handed to `indexing-9c`** (bug / API / coding-agent). Its tasks: check this
section's mode analysis adversarially, audit the other namespaces for the same
placeholder pattern, **report and pause for authorisation before applying**, then
re-measure the table below to demonstrate the filter discriminates — and close on
that evidence rather than on the write completing.

The remaining argument for `apply` is the temporal search filter, so here is what
that filter does on TGN **right now**, measured against production. Every TGN
toponym carries the placeholder `{start:{latest:2026}, end:{earliest:2026}}` —
`start.earliest`, `start.in`, `end.latest` and `end.in` are all **absent**.

`_temporal_filter` (`gateway/es_helpers.py:970`) reads
`start.earliest ?? start.in` and `end.latest ?? end.in` in **`possibly`** mode,
with `unbounded_passes=True`. **All four are missing, so both bounds take the
"unbounded" branch and the place passes unconditionally:**

```
window          mode          tgn places matching
1500-1600       possibly       1,713,388   (57.3%)
 800- 900       possibly       1,713,375   (57.3%)
-500- -400      possibly       1,712,676   (57.3%)

1500-1600       definitely         2,126   (0.1%)
 800- 900       definitely         2,300   (0.1%)
-500- -400      definitely           110   (0.0%)
```

🛑 **The count moves by 13 places between the 16th century and the 9th, and by
712 across two and a half millennia. In the DEFAULT mode the date filter is inert
on TGN: 1.71 M places pass every window ever asked.** Not a subtle degradation —
the filter is present, returns results, reports no error, and **discriminates
nothing.**

⚠ **And `definitely` mode fails in the opposite direction:** `start.latest` is
present and equals 2026, so `2026 <= end_year` is false for any historical window
and **2.99 M TGN places are silently excluded** from every query that asks for a
definite date. **Both modes are wrong, in opposite directions, and neither
announces it.**

**That is what the patch repairs**, for the 9,450 concepts Getty actually dates.
It does not make TGN fully datable — it replaces "uniformly meaningless" with
"correct where known, absent where not", which the `undated` flag already handles.

⚠ **`apply` HAS NOT BEEN RUN and is not mine to run** — it writes to the live
`places` index and needs SG's authorisation, now on a single ground rather than
two. **But that single ground is a live search-correctness defect, not a
nice-to-have.**

⚠ **`indexing-9c` nearly reported the exact opposite, via today's recurring trap.**
Its first pass used `size: 3` and returned *"Samarobriva → (no tgn attestation)"*
and *"Toscanella → (no tgn attestation)"* — **both false**: Samarobriva has 14 docs
and Toscanella 9, and it had looked at three. **A universal negative asserted from
a truncated result**, which would have declared two of the four example pairs
unconstructible. *"The fix that would have caught it automatically is the one I
keep advocating and did not apply to myself: print the denominator."*
`examined=14 total_docs=14` makes the flaw unmissable; `(no tgn attestation)` hides
it.

⚠ **The asymmetry, stated in the direction that is easy to miss:** because this
stratum is European Latin-script and nothing else, **it cannot be traded against
the cross-script work in any planning sense. It does not reduce what the
specialist spend has to buy.** *40,937 is a number that will look like it bought
more than it did* — twice over, once through the clustering above and once
through scope.

## 6.9 🛑 PRODUCTION: a wedged `/ix1` BLOCKS ES BOOT — an availability SPOF

**`indexing-04` closed §7.3b, and the answer is the bad one.** ES makes **three
sequential blocking filesystem calls on `path.repo` during startup**. Measured on
the same binary production runs, on `htc` compute nodes (jobs 11156750, 11157199):

```
control, good path              18s
path blocks 90s/op  -> boot    286s   = 16 + 3x90
path blocks 30s/op  -> boot    105s   = 16 + 3x30
```

✅ **The 30s run was a PREDICTION made before it ran (~105s), and it returned
105s** — which turns *"286 is consistent with three calls"* into a causal claim
rather than arithmetic that happens to fit.

**`/ix1/ishi` is mounted `hard`** (`retry forever`), and `path.repo` is
`/ix1/ishi/es/snapshots`. **So production ES cannot restart while `/ix1` is
wedged.** `/ix1` wedged **twice on 5 September alone**; the only reason this has
not bitten is that prod has not restarted since 31 August.

**The three conditions diverge, which is why they had to be separated:**

| condition | result |
|---|---|
| path does **not exist** | boots fine, and **self-heals** — ES creates it, `_verify` then succeeds |
| path **unreadable** (mode 000) | boots fine; fails only on use — *"path is not accessible on master node"* |
| path **hanging** | 🛑 **boot never completes** |

⚠ **Testing only "does not exist" — the obvious thing to try — returns the
reassuring wrong answer.**

🛑 **Registration in cluster state is IRRELEVANT.** A *fresh* cluster with
`path.repo` set and **no repository registered** blocked identically, at the same
105s and 286s. **It is the SETTING, not the replay of a registered repo — so
unregistering `prod_repo` would not help. Only changing `path.repo` does.**

**The trade-off is SG's call.** `/vast` is also a hard NFS mount, so moving
`path.repo` there does not remove blocking — but it **adds no new failure mode**,
because ES already cannot run without `/vast` (`path.data` lives there). *A
dependency you already have is free; a second one is not.* Against that: `/vast`
had 226 G free at 78%, is shared with prod ES, and keeping snapshots off the
serving path was deliberate. **The `/ix1` location buys isolation and costs
availability — and the price is now measured rather than guessed.**

Re-runnable in ~5 minutes from `/vast/ishi/pathrepo-probe/`.

⚠ **Two instrument failures, both today's fault class.** Run 1 reported `BOOTED`
for the hanging case **having tested nothing** — the FUSE mount silently failed to
mount over a non-empty directory, and the summary line said `BOOTED` while the
detail said otherwise. **Reporting it would have been the most consequential
possible wrong answer: "stop worrying about /ix1".** Run 2 then **deadlocked in its
own control**: `timeout 8 stat` cannot kill a process blocked in a FUSE call, which
is uninterruptible until the filesystem replies. **A control for blocking must
OBSERVE the block** — is the backgrounded `stat` still alive after 12s? — **not try
to interrupt it.**

## 6.10 STAGING IS NOT A FAITHFUL REPLICA — and the mechanism is tombstones

🛑 **Measured by `gotw-eb`, 6 Sep, with a control that is the reason it is
readable at all.** A staging gateway was stood up on a compute node and gated
against production with 299 real corpus queries across three request shapes:

```
staging vs production :  set 99.33%   order 97.66%
production vs ITSELF  :  set 100.00%  order 100.00%   <- control
```

**Without the same-endpoint control the 97.66% would have been written off as
Elasticsearch tie-breaking** — a fifteen-hour mistake in the other direction.
Production is perfectly reproducible against itself, so the residual is real.

Two of 299 return genuinely different documents, and not randomly: **staging
returns `tgn` ids where production returns `osm`/`wd`/`gn`.**

### Three hypotheses killed before the fourth

* **Index generation** — identical. Production serves
  `places_h3ccode-20260805t120000z` + `toponyms_temporal-20260731t160000z`, and
  **both `prod_repo` snapshots contain exactly those two concrete indices.**
* **Missing documents** — no. All eight divergent ids **exist in production**. It
  is ranking, not absence.
* **Stale embeddings** — no, and this was the leading hypothesis. `toponyms` shows
  exactly **100,960** writes since node start, precisely today's re-embed; the
  re-embed finished **15:19 EDT** and the snapshot was taken **15:31 EDT**.
  **Staging has the new vectors.**

### The mechanism: a restore purges tombstones, which changes per-shard IDF

```
places_h3ccode-20260805t120000z    docs 361,746,797   DELETED 14,169,759   (3.9%)
toponyms_temporal-20260731t160000z docs  72,703,777   DELETED    292,493
```

A snapshot stores **live documents only**, so a restore purges deletes. ES
computes **IDF per shard** under the default `query_then_fetch`, and
deleted-but-unmerged documents still contribute to shard-level term statistics.
Different IDF → different BM25 scores → different order, and occasionally a
different **set** where a boundary case crosses the top-N cutoff.

**It fits:** order agreement degrading faster than set agreement is what a small
uniform score perturbation produces, and production's self-consistency follows
because its own tombstones are constant within a run.

## What the 6 Sep TGN apply actually did — measured before/after, not inferred

**The question: would restoring the 5 Sep snapshot lose anything?** Staging *is*
the pre-apply state, so this is directly answerable — 400 of the 9,623 patched
`place_id`s fetched from **both** clusters and compared as canonical-JSON sha256.

```
byte-identical            0 of 400        <- every doc differs
fields differing:  indexed_at  400/400
                   toponyms    105/400
                   geometries   20/400
```

**But no date value changed.** The entire substantive diff is **explicit `null`
sub-fields being stripped**:

```
before (staging)  {"start":{"in":800,"latest":null},"end":{"in":1500,"earliest":null}}
after  (prod)     {"start":{"in":800},             "end":{"in":1500}}
```

The apply rewrote each doc through the temporal helper's `_clean()`, which drops
nulls. **Values identical; keys removed.** And `indexed_at` was bumped on all
9,623 to one batch timestamp, `2026-09-05T22:53:52.359526+00:00`.

✅ **So: nothing material. Restoring the 5 Sep snapshot loses no data.** It would
restore the `null` sub-fields — reverting a cosmetic cleanup — and restore honest
**August** `indexed_at` values. ⚠ Arguably an *improvement*: `indexed_at` now
claims 5 September for content last actually changed in August, so the field
currently misreports freshness on 9,623 docs.

⚠ **Incidental: this is `place#246` item 3 (`"in": null` in stored timespans)
partially self-repairing.** The apply cleaned it on the ~30% of docs that had it.
That is not a fix — the *writer* still emits nulls — and it makes the population
non-uniform, which is worth knowing before item 3 is measured.

### 🛑 Three attempts to answer one question, and the first two were wrong

1. **`indexing-9c` reported the apply "changed nothing", comparing a patched doc
   (`_version 3`) to a DIFFERENT unpatched doc (`_version 2`).** Comparing doc A
   to doc B cannot establish whether A changed. **Right conclusion, invalid
   method.**
2. **This session then said "only `indexed_at` differs" from TWO documents.**
   Wrong — `toponyms` differs in 105 of 400 and `geometries` in 20. *A claim about
   a population resting on a two-item sample, again, in the hands of the session
   that has spent the day cataloguing exactly that.*
3. **The valid method is the same doc before and after, across a real sample, with
   the differing FIELDS enumerated rather than a boolean.** Only then does the
   answer hold — and only then is it clear the conclusion was right all along.

⚠ **The conclusion survived all three attempts, which is precisely why nobody
would have re-examined it.** `gotw-eb`'s rule applies: *a call that still looks
right after its evidence is withdrawn gets no second look.*

## OSM re-ingest ⇒ RETILE (user-visible), but do NOT refresh the PBF in the same pass

**Three questions from SG, all checkable.**

### 1. Yes — tiles bake dating metadata per feature

`generate_tiles.py:1169` writes `start` / `end` on every dated feature, plus
`start_def` / `end_def` where a timespan pins both inner bounds.

### 2. Yes — a re-ingested `osm` requires a retile, and the effect is USER-VISIBLE

**Today every OSM feature tiles as unbounded.** `generate_tiles.py:1154`, in its
own words: an attestation `{"start": {"latest": 2026}}` *"now gets `start = -9999,
end = 9999` (unbounded: never hidden in possibly) plus `start_def = end_def =
2026`"*. The comment records why — pooling endpoints had stamped every
contemporary feature `(2026, 2026)`, which *"blanks `osm` / `osm_misc` / `tgn` /
`nl` on any historical range"*.

🛑 **So if item 1 gives OSM real `start_date`/`end_date`, dated features gain real
bounds and would correctly VANISH outside their period.** Without a retile the map
keeps showing them at every year. **That is a visible behaviour change, not
metadata hygiene** — and the `historic=*` features, where such tags cluster, are
exactly the ones a historical gazetteer's timeline should be hiding.

### 3. 🛑 NO — refreshing the PBF in the same pass destroys the verification

**The PBF's data date is `2026-07-20`** (`osmosis_replication_timestamp`;
downloaded 30 Jul, 94 GB). ~7 weeks old.

⚠ **`.pin-place233/planet-pinned.osm.pbf` is a HARDLINK to the same inode
(56867, 2 links).** So a refresh would *not* destroy place#233's reproducibility —
the pinned name keeps the old inode — and would cost a second 94 GB on `/ix1`
(1.8 TB free). **The pin is not the obstacle.**

**The obstacle is that it makes the check unable to fail.** `indexing-9c`'s
inverted verification requires asserting **both** that the intended differences are
present **and that nothing else changed**. On the **same** PBF, an `osm` re-ingest
should reproduce its output identically except for the new date fields — a strong
check with real power. **On a NEW PBF everything legitimately changes, and
"our fix worked" becomes indistinguishable from "upstream moved".**

> **That is this week's recurring failure re-created deliberately: a change that
> removes the check's ability to discriminate.**

⚠ **It also invalidates item 1's own measurement.** The PBF scan sizes
`start_date` coverage *on the pinned file*; a refresh makes that number describe a
file we are no longer ingesting, so the scan would have to be re-run.

### Recommended sequence

1. **PBF scan on the pinned file** → decide item 1 on it.
2. **Re-ingest `osm` on the SAME pinned PBF** → verification keeps full power.
3. **Retile the OSM buckets** → the dating metadata goes live.
4. **Refresh the PBF as its own campaign, later, if wanted** — at which point the
   diff is purely *"upstream moved"*, which is a clean and separately useful thing
   to measure.

✅ **Seven weeks is not stale for named places.** OSM's churn over that window is
marginal for our purposes, and the currency gain does not buy back the lost
ability to verify.

## ⚠ PBF TOOLING — the newer pyosmium API is installed and HALF ADOPTED

**SG's recollection is correct and the split is clean.** `pyosmium 4.x` is in the
`whg` env — `FileProcessor`, `filter`, `zip_processors`, `IdTracker`,
`BackReferenceWriter`, `BufferIterator` all present.

✅ **The retiling / boundary / water code adopted it:**
`osm_boundary_geometry.py:708`, `boundary_stage.py:315`,
`boundary_shard_planner.py:115,161`, `osm_way_area_geometry.py:406,420`,
`water233/{vertex_identity,assemble_inland_water,count_water}.py` — all
`FileProcessor`, with `.with_locations()` / `.with_areas()`, and
`count_water.py:73` even uses **`osmium.filter.KeyFilter`**, which filters in C++.

🛑 **The two AUTHORITY INGEST scripts did NOT — they predate it:**

```
authorities/osm-places.py:231   class OSMHandler(osmium.SimpleHandler)
authorities/ohm-places.py:262   class OHMHandler(osmium.SimpleHandler)

osm-places.py:415   handler.apply_file(pbf, locations=True, idx='flex_mem')
osm-places.py:245   def node(self, n):  if not n.tags: return        <- IN PYTHON
osm-places.py:258   def way(self, w):   if 'name' not in w.tags: return  <- IN PYTHON
```

**Under `SimpleHandler.apply_file`, EVERY object in the PBF crosses the C++→Python
boundary and the discard decision is made in Python.** The overwhelming majority
of nodes in an OSM extract are untagged geometry vertices, so nearly all of that
traffic is paid for and thrown away one object at a time.

`FileProcessor` with `osmium.filter.EmptyTagFilter()` / `KeyFilter(...)` makes the
same rejection **in C++**, so Python only ever sees survivors. The entity-type
selector (`osmium.osm.NODE | osmium.osm.WAY`) narrows it further before any
filter runs.

⚠ **No speedup figure is claimed here — this has not been measured on our data,
and the gain depends on the tagged fraction of the specific PBF.** The mechanism
is certain; the magnitude is not, and it should be measured rather than asserted.
`with_locations()` still indexes every node either way — that cost does not go
away, and it is C++ side.

### Why this is live rather than housekeeping

🛑 **`place#246` item 1 needs a PBF scan over OSM to size `start_date` coverage,
and may then need a full `osm` re-ingest — 20,622,228 places from the 92 GB PBF
on `/ix1`.** That is precisely the workload the old API is worst at. **The scan
should be written with `FileProcessor` + filters from the outset**, and doing so
gives a free comparison against the existing `osm-places.py` path if anyone wants
the magnitude.

⚠ **A trap for anyone reaching for the C++ CLI as a prefilter instead:**

```
osmium -> /ihome/ishi/stg135/.local/bin/osmium
  error while loading shared libraries: libboost_program_options.so.1.85.0
```

**`osmium-tool` is installed and NON-FUNCTIONAL.** `osmconvert`, `osmfilter` and
`osmcoastline` are absent from the env entirely. So `osmium tags-filter` is not
available as a prefilter, and the Python `filter` module is the route.

## `place#246` — items 2 and 3 CLOSED in code, and two findings worth more than the fixes

`indexing-9c`, 6 Sep (`8989825`, `378990d`). **Neither needed a rebuild, so the
rebuild set is unchanged: `tgn` alone, `osm` conditional on the PBF scan.**

### 🛑 Item 2 was SIX implementations, not three — and how the other three hid

The audit found `osm-places.py`, `tgn_temporal.py`, `nativeland-places.py`.
**`dplace-places.py`, `indexvillaris-places.py` and `wikidata-geoshapes.py` also
carried private copies.**

⚠ **They were invisible to the audit because their releases carry real dates, so
the fallback NEVER FIRES in production and leaves NO TRACE IN THE DATA.**
Inspecting the namespaces whose data looked suspicious could not have reached
them. They were found by a guard written to stop a *seventh* appearing — **which
failed the first time it ran.**

> ✅ **A grep for the PATTERN beats an inspection of the namespaces you already
> suspect, because a latent defect that has never fired leaves nothing to audit.**

That is the counterpart to §8.3b's rule: selection effects reach the evidence, and
here the evidence *could not exist* for half the instances.

### 🛑 Item 3's root cause is NOT a writer — it is parquet schema unification

**Every writer is clean.** A parquet **struct column has one schema per file**, so
reading it back **materialises every key any row used, as an explicit null on the
rows that lacked it.**

> **A document's null pattern is a fingerprint of what shared its staged file.**

Observed exactly so: `osm`/`nl`/`gb`/`iv`/`clio` clean (one shape per file); `tgn`
carrying `in: null` (`attested_at` + `lifespan` in the same file); `wd` all three;
`ohm` a wholly null `end`. `drop_nulls_for_parquet` was already applied on the
**write** side; the missing half was added at the two **readers** that feed ES.

⚠ **The mechanism was already documented** — in `refresh_staged_names._drop_nulls`,
fixed there for one comparison guard and **never at the writers it equally applies
to.** A known fault, solved once, locally.

### 🛑 An addition to the inverted verification, and it is load-bearing

Use `fidelity.py`'s **NORMALISED** hash for the untouched-namespace half, **not the
raw one.** A re-ingest **re-shuffles which documents share a staged parquet file**,
so **null patterns change corpus-wide with no content change** — the raw hash would
report *every* namespace as differing and bury the real signal.

⚠ **This session hit exactly that trap and did not generalise it.** Comparing the
TGN apply before/after, the raw canonical-JSON hash said **400 of 400 differ**;
only after normalising nulls away did the truth appear — **0 genuine value
changes.** `indexing-9c` turned the same observation into a standing requirement
for the rebuild.

### The staging cancellation was coordinated, not mysterious

Job 23990206 was stopped by `indexing-04` **after asking `indexing-9c` directly
whether it needed it, and being told no.** `gotw-eb`'s release and this session's
handover crossed with that decision. ✅ **And nothing was lost:**
`prod-manual-20260905t1931z` is a **manual** snapshot, and SLM prunes only what SLM
created — **so it never ages out.** The pre-apply state costs a restore to recover,
not a loss.

⚠ Earlier this session reported it could not tell who cancelled it, since all
sessions run as `stg135`. **True of the scheduler, and the wrong place to look —
the answer was available by asking.**

## ✅ `place#246` SHOULD BUILD ON STAGING AND SWAP — SG, 6 Sep

**Yes, and the dominant reason is not "it keeps load off production".**

🛑 **Doing #246's data work IN PLACE would inflict the exact damage we spent the
day characterising.** Item 4 requires re-ingesting `tgn` (2,991,143 places, of
which **1,277,683 — 42.7% — have an empty `toponyms[]`**); item 1, if the PBF scan
justifies it, requires `osm` (20,622,228). **Every in-place update is a delete
plus a reindex**, so that route would add up to **~23.6 M tombstones on top of the
existing 14,169,759** — roughly *tripling* the dead weight, on the volume
production serves from, with merge pressure and the `dense_vector` heap history
behind it.

✅ **A staging rebuild carries none of it: a freshly built index has ~0 deletes by
construction.** And it permanently removes the staging/production divergence,
because after the swap production *is* the rebuilt index.

✅ **It is also the project's documented pattern, not a new idea.**
`promote_to_production` is snapshot → restore → **one** `_aliases` swap of both
indices; full rebuilds already build in staging.

### ⚠ Scope it — most of #246 does NOT need this

| item | route |
|---|---|
| **1** `osm-places.py` never reads `start_date`/`end_date` | ⚠ **PBF scan FIRST.** Overlap with our 20.6 M ingested features is *unmeasured*; the 19,338,154 taginfo figure is across all OSM objects. Re-ingest only if sized and justified. |
| **2** three `datetime.now().year` implementations | **Code only.** No data work, no rebuild. |
| **3** `"in": null` in stored timespans | Writer fix; live rows patchable. Harmless to queries — ES does not index nulls. |
| **4** `places`/`toponyms` name-inventory mismatch | 🛑 **Needs re-ingest of `tgn`.** This is the one that justifies the route. |
| **5** Getty term dates cannot land | Blocked on 4; resolves with it. |

**So the rebuild set is probably `tgn` alone**, with `osm` conditional on the PBF
scan. A whole-corpus rebuild is not implied and should not be assumed.

### 🛑 The verification must INVERT after a rebuild — this is the subtle one

`indexing-04`'s `fidelity.py` and `corpussig2.py` were built to prove *sameness*.
**After a deliberate fix, `IDENTICAL` is a FAILURE report.** The harness must
assert **both** halves:

1. the **intended** differences ARE present — 42.7% empty `toponyms[]` gone, Getty
   dates landed on the 9,450 concepts; **and**
2. **nothing else** changed — every other namespace byte-identical, full-corpus
   nested counts moving only where predicted.

⚠ **A fidelity harness pointed at a changed index reports success when it has
failed**, and that is precisely the class of error this whole sequence has been
about. Say what should change *before* running it, then check both directions.

### Carried over from `place#247`, unchanged

* **Point-in-time gap** — restoring a staging-derived snapshot discards production
  writes made after the snapshot. Production is currently quiescent (0 indexing
  ops / 30 s). **Quiesce for the window and verify `index_total` unchanged
  immediately before the swap.**
* **`/vast` cannot hold both copies** — 226 GB free against 73.4 GB needed lands
  on ES's 85% low watermark. **Restore sequentially per index**, accepting that
  the two aliases then move at different moments.
* **The forcemerge on completion is right but its value differs**: a fresh build
  has ~0 deletes, so the merge buys **segment consolidation**, not delete
  expunging. The alignment benefit arrives free with the rebuild.

## ✅ `place#247` CLOSED (6 Sep) — and why, as a mechanism not a tolerance

SG closed it: the blocking question is answered and the residual does not touch
what is blocked. **The decisive form of the argument is not "the difference is
immaterial" — it is:**

> **The divergence is confined to SCORING. `place#246`'s audit reads CONTENT.**

🛑 **And that is a mechanism, not a hope** (`indexing-04`): **cosine similarity
does not consult corpus statistics; BM25 does.** Hence KNN at 98.33% set / 97.50%
order against BM25 at 90.00% / 36.67% on the same queries. #246 counts
per-namespace timespan populations via aggregations, counts and filters — none of
which consult `docCount`-derived term statistics.

⚠ **Stated boundary: if #246, or anything else, grows a RELEVANCE-ORDERING
assertion, this conclusion needs revisiting.** Comparing *ranked* results between
clusters is outside what was established.

**Deliberately dropped, not forgotten:** the forcemerge. 14,169,759 deleted docs
in `places` (3.9%) is real dead weight and would permanently remove the
divergence — but nothing is blocked on it and it is I/O against the serving
volume. ✅ **SG's sequencing is what established it was unnecessary: holding
Task 2 back did not cost time, it generated the evidence.** Re-raisable as
low-priority housekeeping.

**Harness preserved** at `/vast/ishi/verification/` — moved off `/tmp` on pitt,
which was one reboot from gone: `fidelity.py` (documents by canonical-JSON sha256
across both clusters, by `_id`), `corpussig2.py` (full-corpus aggregate signature
**with the VACUOUS flag**), `cmp_staging.py` (the BM25-vs-KNN split),
`diverge.py` (anatomy of one diverging query), `termstats.py` (`docFreq`/
`docCount` via `explain`).

🛑 **The `VACUOUS` flag is the reusable part, and it generalises past this
issue.** *Any comparison of two empty results reports agreement*, and an
aggregation on a **mis-pathed** field returns empty on **both** sides — so it
reads as a pass while testing nothing. Two top-level aggs on nested `geometries`
fields did exactly that and printed `IDENTICAL`.

⚠ **An attribution error worth recording, since it nearly caused an action.**
This session asked `indexing-04` to stop the staging Slurm job as "its" job. It is
not: it had only ever read from it, and the job predates its involvement. **All
sessions run as `stg135`, so the scheduler cannot distinguish us** — job ownership
is not recoverable from Slurm here, and must be established by asking. Both
sessions independently declined to cancel a job they did not start, which is the
right instinct: *an idle allocation is a cheaper mistake than destroying 73.4 GB
of restore someone still needs.*

## ✅ TASK 1 CLOSED — the `/ix1` snapshot IS faithful, demonstrated on content

`indexing-04`, 6 Sep. **Content, not counts; full corpus, not a sample.**

**Provenance asserted rather than assumed:** all **8 shards** report
`source.repository=prod_repo`, `source.snapshot=prod-manual-20260905t1931z` via
`_recovery`.

**Content, sampled:** 400 places + 400 toponyms drawn from production, fetched by
`_id` from **both** clusters, compared as **canonical-JSON sha256 over `_source`**
— **800/800 identical**, 0 differing, 0 missing either side. *Including the 128-d
embeddings.*

**Content, FULL CORPUS** — the part that makes it a demonstration:

```
places total          51,187,900  ==  51,187,900
namespace             27 buckets covering all 51.2M    identical
nested geometries     50,315,192  ==  50,315,192
  geom_class          3 buckets covering all 50.3M     identical
  has_geom            2 buckets covering all 50.3M     identical
  h3_cover values    100,348,433  == 100,348,433
nested types          54,108,146  ==  54,108,146
nested toponyms      119,277,282  == 119,277,282
```

**Every nested doc count matches to the unit**, on top of per-shard doc counts
matching shard for shard.

⚠ **TWO VACUOUS CHECKS NEARLY REPORTED AS PASSES.** The first signature pass ran
`terms` aggs on `geom_class` and `has_geom` at the **top level**. Both returned
**0 buckets on both clusters**, and the harness printed `IDENTICAL` — **a
comparison of nothing against nothing.** The fields live under the **nested**
`geometries` path. *Two of four places dimensions were testing nothing while
reporting agreement.* Fixed by flagging `VACUOUS: 0 buckets, tests nothing`, so an
empty comparison can never again read as a pass. **Same shape as everything else
this week.**

⚠ **Two honest limits, so this is not over-read.** `geometries.source` and
`geometries.approximation` are **sparse** — 3 and 4 buckets covering only
**32,353 of 50.3 M** nested geometries, so they discriminate weakly. And
`types.identifier` is the **top 250 of a long tail**, covering 2,181,625 of
54,108,146. **The load-bearing dimensions are the ones covering the full corpus:**
namespace, `geom_class`, `has_geom`, the three nested doc counts, and the
`h3_cover` value count.

✅ **So SG's question 1 — "do we truly have a faithful snapshot on `/ix1`?" — is
answered YES, on evidence.**

**The only real remaining difference is deleted-doc counts** (prod 292,493 vs
staging 305,969), which is the whole of the BM25 divergence below.

⚠ **And the route-2 caveat is now stronger than first stated:** expunging deletes
on **production alone would REVERSE the asymmetry rather than remove it** — and
would *look like an improvement in the aggregate while being the same defect*.
**Both sides or neither.**

## ✅ CAUSE FOUND AND MEASURED — and it is BM25, not KNN (6 Sep)

`indexing-04`, and the finding is the opposite of what it predicted.

**Live docs are identical** — 72,703,777 both sides, equal **shard for shard**
(all 8 checked). What differs is deleted-doc counts, and the reason is that
**production has MERGED since the snapshot while staging has not.** Deleted docs
inflate `docCount` until merged; `docCount` drives BM25 IDF; **so scores differ in
the 4th–6th decimal.**

```
MODE               SET IDENTICAL   ORDER IDENTICAL   MEAN JACCARD
bm25_default              90.00%            36.67%         0.9881
bm25_tiebroken            90.00%            36.67%         0.9881
knn                       98.33%            97.50%         0.9993
```
*(120 production toponyms, vectors and names reused as queries against both.)*

🛑 **THE DETERMINISTIC PATH DIVERGES MORE THAN THE APPROXIMATE ONE.**
`indexing-04` predicted the reverse — HNSW traversal over different segment
layouts (places 92 vs 90) as culprit, BM25 clean. **Wrong: cosine similarity does
not depend on corpus statistics at all. BM25 does.**

**Anatomy of one case**, so this is not only aggregates. Query `Bena`: 50 hits,
prod tail all tied at **14.899352**, staging tail all tied at **14.888472**. A
block tied *exactly at the top-K boundary* means **which** tied docs fall inside
the cut differs — that is the set divergence. Elsewhere `Faringdon West` rank 21:
prod `Faringdon@eo` 17.126087, staging `Faringdon@de` 17.125854 — same set,
different order, scores genuinely **different, not tied**.

⚠ **An explicit sort tiebreak does NOT fix it** — identical 90.00%/36.67% with and
without. A tiebreak only helps when scores are *equal*; here they genuinely differ
between clusters, so there is nothing to tie-break.

### 🛑 This may make Task 3's target unachievable — SG's decision

**100% set AND order agreement cannot be reached while production is live and
merging**, because prod's `docCount` drifts continuously as merges reclaim deletes
while staging's is frozen at snapshot time.

⚠ **And the prod-vs-itself 100%/100% control does not establish otherwise.** It
proves the **harness** is deterministic, not that a faithful restore would score
100%. *A control that compares a thing to itself cannot distinguish "faithful
copy" from "same physical segments"* — so the gate as written may be one **no
correct restore can pass**, and the residual would be chased as a defect forever.

**The practical routes:** quiesce production for the comparison; expunge deletes
on **both** sides so `docCount == docs.count` and term statistics become
comparable; or gate on **decisions** rather than exact ordering. ✅ **Note that
this is precisely the data SG asked for before deciding the forcemerge** — the
sequencing he chose produced it.

### 🛑 RETRACTED — my relay of the direction-of-travel objection

⚠ **This session put `indexing-04`'s objection in front of SG, and it is now
withdrawn by its author.** It argued that expunging deletes from production would
make *production resemble staging* — the wrong way round. **That rested on the
false claim that restore drops tombstones.** Restore *preserves* them, so the
argument does not hold, and **the correct framing is the opposite**: deleted docs
are the **moving part**, and expunging them from both sides makes `docCount ==
docs.count` and term statistics comparable. **That is forward, not backward.**

🛑 **AND A CORRECTION TO THIS SESSION'S OWN RELAY OF AUTHORISATION.** This session
told `indexing-04` that SG had authorised Task 2. **SG has since told it directly
that Task 2 is NOT authorised**: Tasks 1 and 3 come first, and the forcemerge
decision is to be made **with data**. `indexing-04` correctly held, checked with
SG directly rather than acting on a relay, and asked that its retraction not be
read as changing the permission. ⚠ **An inference from "the indices must be
cleanly optimised" is not an authorisation, and relaying it as one was an
over-reading.**

## ~~🛑 RETRACTED — THE TOMBSTONE EXPLANATION IS FALSE (6 Sep)~~

**Everything below about tombstones rests on a premise that was never checked at
the scale it was applied to, and it is wrong.** Measured directly on the live
staging instance:

```
index                              production   staging      delta
places_h3ccode-20260805t120000z    14,169,759   14,033,363   -136,396
toponyms_temporal-20260731t160000z    292,493      305,969    +13,476
```

🛑 **The restore did NOT purge tombstones.** Staging carries ~99% of production's
deletes, not zero — and **the deltas run in opposite directions**, which is what
independent background merging on two live clusters produces. The claim
*"production's 14.2 M deletes all vanish in staging"* is **false**, and any IDF
effect is of order **1%**, not 100%.

⚠ **And the provenance suspicion was also wrong** — SG's, and reasonable. ES's
`_recovery` API records what each shard actually recovered from, and **both**
staging indices came from the current production snapshot:

```
places_h3ccode-20260805t120000z    <- prod_repo / prod-manual-20260905t1931z
toponyms_temporal-20260731t160000z <- prod_repo / prod-manual-20260905t1931z
```

Doc counts match exactly on both sides. **So the divergence has neither of the
two explanations offered for it, and its cause is OPEN.**

🛑 **HOW THREE SESSIONS GOT THERE, because the shape matters more than the
error.** One proposed a mechanism; a second tested it on a **2,000-document**
throwaway and reported it confirmed (1,200 deletes → 0); a third ran a
pre-registered directional test and found the predicted 3.2× concentration
(p = 0.0397). Every step was sound. **Nobody checked the premise against the
361-million-document instance the argument was about.** The small-scale result did
not generalise — quite possibly because a tiny index merges on restore where a
23.6 GB one does not.

✅ **`gotw-eb`'s shape-split OBSERVATION stands; its EXPLANATION does not.** The
lexical concentration is real and pre-registered. What it means is now unknown.

📋 **Filed as [`place#247`](https://github.com/WorldHistoricalGazetteer/place/issues/247)**
— prove the `/ix1` snapshots faithful, optimise the live indices triaged, then
stand up a staging ES and *demonstrate* 100% alignment. **Sequenced ahead of
`place#246`, which is ahead of v8**, because #246's audit counts are worthless if
taken on an instance not shown to match production.

⚠ ~~**THE CAUSAL STEP IS NOW DEMONSTRATED — by a session trying to refute it.**~~
*(retained: it was published and acted on)*
`indexing-04` believed *"a snapshot stores live documents only"* was wrong: a
snapshot copies Lucene segment files, deletions live in `.liv` files inside those
segments, so tombstones *ought* to survive a restore. Plausible, and false.
Measured on a throwaway ES under Slurm (job 11159785, ~30 s) — 2,000 docs, 600
deleted, then snapshot → delete index → restore:

```
BEFORE   docs.count 1400   docs.deleted 1200   segments 3
AFTER    docs.count 1400   docs.deleted    0   segments 1
```

**Tombstones are gone on restore.** So a restored index genuinely carries
different term statistics: `docFreq`/`docCount` no longer count the deleted docs,
IDF differs per shard, scoring differs. Production's 14,169,759 deletes in
`places` all vanish in staging.

⚠ **THE BAD EVIDENCE IT NEARLY SENT IS THE MORE USEFUL HALF.** Before measuring,
it had what looked like support for the *opposite* conclusion: its snapshot of
`places` measured **23.6 GiB against a live `store.size` of 23.6gb** — an exact
match, read as *"the snapshot contains the deleted docs' data too"*. **Worthless.**
Store size does not track deletions at all; in the controlled test it **GREW**,
86.8kb → 133.8kb, *while* deletes went from 1,200 to zero. **A number that
matched, an explanation that fitted, and no causal connection between them** —
which would have talked this session off a correct mechanism using a coincidence.

⚠ **What it settles and does not.** It establishes the step: restore drops
tombstones, therefore term statistics differ. It does **not** establish that this
is the *whole* of the 99.33% / 97.66% gap — there may be more than one
contributor. **It removes an objection rather than closing the question**, and
`gotw-eb`'s shape-split remains the decider.

🛑 **AND IT CUTS AGAINST DOING ANYTHING — `indexing-04`'s point, which this
document had missed.** If restore drops tombstones, then expunging deletes from
**production** makes *production resemble staging*, not the reverse. **Aligning
them by merging production means I/O on the volume production serves from, in
order to make the source look like the replica.** That is the wrong direction of
travel, and belongs in front of SG *alongside* the triage guidance rather than
after it.

### The shape-split ran — the predicted concentration is there

`gotw-eb`, n=594, three request shapes, same production-vs-itself control:

```
exact/lexical   n=200   order 96.00%   set 99.00%
phonetic        n=200   order 99.00%   set 99.50%
phonetic+cc     n=194   order 98.45%   set 99.48%
production vs ITSELF    order 100.00%  set 100.00%

order disagreements:  lexical 8/200 (4.0%)  ·  vector-led pooled 5/394 (1.3%)
                      ratio 3.2x  ·  Fisher exact p = 0.0397
```

✅ **The prediction was directional and PRE-REGISTERED**, not found by looking, and
its causal step was demonstrated independently by `indexing-04`. That combination
is worth more than the p-value alone.

🛑 **RECORD IT AS `gotw-eb` FRAMED IT — "consistent, with the predicted
directional concentration observed", NOT established.** Its own words:
*"one test, 8 events against 5, and if it had come out at p = 0.07 I would be
telling you the same physical story with a different adjective."* That sentence is
the reason to trust the result, and it must travel with it.

⚠ **3.2x is a FLOOR, not an estimate.** This gateway's phonetic mode is *blended*
rather than pure KNN, so both vector-led arms retain a lexical component and the
contrast is diluted. `phonetic+cc` sitting between the two (1.55%) is the ordering
you would expect if the country filter changes which shard-level statistics
matter — offered as a **pattern, not a result**.

🛑 **WHAT IT CANNOT SETTLE, and this is the limitation that matters.** The four
set-level differences (2 lexical, 1 phonetic, 1 phonetic+cc) are far too few to
split by shape — **and set differences are the ones that actually change a
reconciliation decision.** So the shape evidence is about **ORDER**, and *"the
same cause drives the set differences"* is an **assumption, not a measurement**.
Whether tombstones account for the whole gap stays **unestablished**; the question
is now narrowed to *"does the ordering perturbation also explain the boundary
crossings"*, which n=594 cannot answer and which would need thousands of queries.

✅ **Settled practically:** `gotw-eb` stays on the production gateway (2.99
places/s, ~9.6 h projected), staging gateway stopped, both artefacts committed —
so if alignment ever becomes available this is one `sbatch` and one `srun`, not
another afternoon.

⚠ **Originally recorded as a MECHANISM WITH CONSISTENT EVIDENCE, not a proven
cause.** The
decisive test is cheap and uses the existing harness: **if this is IDF, divergence
concentrates in the request shapes with a lexical component and is near-absent in
pure phonetic KNN** — a per-vector cosine is unaffected by another document's
tombstone. Even spread across all three shapes refutes it.

### Consequences

* **The lever, if alignment is wanted — and my first description of it was
  wrong.** ⚠ **`es -forcemerge` CANNOT touch production**: `do_forcemerge`
  requires `$STAGING_INFO_FILE` and errors *"No staging ES instance running"*,
  then merges via `ES_NODE`/`ES_PORT` from that file. It is a **staging-only**
  tool, and merging staging does not help — staging already has zero tombstones.
  Aligning them means expunging deletes in **production**, which is a direct
  `_forcemerge` against prod ES, not this command.

  ✅ **Do it TRIAGED — SG's standing guidance, and the script already embodies
  half of it.** Iterative mode (the default, `--step-factor 2`) **halves the
  per-shard segment count each pass rather than jumping straight to 1**, so each
  pass rewrites less data and peak memory stays bounded. The script uses
  `max_num_segments`; for this purpose
  **`_forcemerge?only_expunge_deletes=true`** is the cheaper primitive again,
  rewriting only segments whose delete ratio exceeds the threshold instead of
  everything.

  🛑 **`toponyms` is the dangerous one, on record.** Production OOM/429s were
  traced to **`dense_vector` merges on the toponyms index** — see
  `~/.claude/memory/es_heap_hnsw_merge_root_cause.md`. Its 292,493 deletes are
  0.4% against `places`'s 3.9%, so it is simultaneously the **riskiest** target
  and the one with the **least to gain**. Merge `places` if anything; leave
  `toponyms` alone.
* **Recommendation: stay on the production gateway.** 0.67% set divergence is
  small but is not noise, and a re-reconciliation about *which parent regions are
  trusted* lands precisely on boundary-case flips.
* ⚠ **Anything validated against staging inherits this.** The specialist
  pick-lists at `docs/toponyms` (2,391 Chinese, 3,147 Russian) were generated
  from staging. **But the divergence is SCORE-level, not content-level** — a
  candidate staging offered *does* exist in production and is reachable there,
  merely ranked below the cutoff. That is materially weaker than "offers
  candidates production does not have", and choices keyed on WHG ids are
  unaffected. Not regenerating is the right call.
* ✅ Everything else about the staging gateway worked: aliases resolved, Symphonym
  loaded (`Kanterberry` → 0 exact, 10 phonetic), and the **geom store being live
  on shared `/vast` means exact containment was never at risk**. Reusable as
  `process/staging_gateway.sbatch` + `process/compare_gateways.py`.

## 6.11 A THIRD DATA SOURCE — the 1908 Atlas of the Chinese Empire (SG authorised)

Offered by `gotw-eb`, built by `gotw-b6`, **explicitly authorised by SG**. On
`main` as `data/atlas1908_*.json`, built by `process/atlas1908_index.py`.

**6,517 entries / ~6,113 distinct names, every one carrying a province and a
minute-precision lat/lon.** OCR'd with Surya from public-domain page images
(Stanford / China Inland Mission, 1908; Stanford d. 1904 — **public domain, and
the transcription is ours, not a third-party transcript**, which their CLAUDE.md
requires). Credit the China Inland Mission / Edward Stanford as source atlas.

### Why it matters more than its size suggests

It is **postal-era romanisation** — standardised at the 1906 Shanghai conference
from Giles's Nanking syllabary — a convention v7's corpus almost entirely lacks,
and **measurably closer to 19th-century orthography than pinyin**:

```
mean string similarity to our 1856 forms:  postal 0.786   pinyin 0.571
and the gain lands where pinyin collapses: Chih-le -> Chihli 0.77 vs Hebei 0.33
```

✅ **The coordinates are the point.** A coordinate join to a modern gazetteer
generates `(postal, modern)` pairs **at scale and independent of our corpus** —
GOTW's own intersection is 90 verified pairs; the derivable ceiling is thousands.

✅ **It is NOT China-only** (`gotw-b6` corrected that): Mongolia 414, Tibet 323,
Sinkiang 261, Korea 102, Kirin 87, Burma 81, Formosa 79, plus Russia, Annam,
Laos, Tongking, Siam. **So it is one consistent romanisation convention applied
across Mongolian, Tibetan, Turkic, Korean and Vietnamese toponyms** — for a
phonetic model that is worth more than a larger China-only set.

✅ **It satisfies §6.5b's test.** The convention is Stanford's cataloguing
decision, taken in 1908 for its own reasons — **a witness from outside the
matching problem**, which is what killed every filter in that section's table.

### 🛑 The caveat, which must not get lost

**This teaches POSTAL → MODERN. Our measured problem is 1856 → MODERN. That is
two rungs and this supplies one.**

⚠ **And the evidence that the missing rung is non-trivial is itself
selection-affected — `gotw-eb` caught this in its own offer.** Only **263 of
2,414 (10.9%)** of the 1856 Chinese headwords reach a postal form by exact match
after normalising hyphens, case and diacritics. Where they do, the difference
really is just hyphenation (`Shang-Hai → Shanghai`) — **but that observation is
selected on precisely the cases where the transformation was trivial.** For the
other ~89%, nothing here says how to get from `Keang-su` to `Kiangsu`.

**Acquiring 1856 → postal pairs remains open**, and if v8 is evaluated on
19th-century forms **this dataset will not by itself close that gap**.

### QA — filter on `clean`

`6,171 of 6,517 (94.7%)` unflagged. 🛑 **`suspect_name` (75 rows) is the flag that
matters**: two entries welded together where Surya rendered a row's coordinates as
LaTeX or substituted a glyph, so a **real coordinate sits under a name that has
swallowed its neighbour** — invisible in aggregate, poisonous in a training pair.
Province line-wraps (`Kwang- tung`, `Mon- golia`) are rejoined, which moved
Kwangtung 216→264 and Mongolia 334→414; that never affected GOTW's own numbers
because their join key normalises spaces, **but it would hit anyone grouping on
the province field.** Independent QA: 98.7% of China-proper coordinates fall
inside the province box the index itself prints; 0.94% alphabetical inversions.

## 7. What is now scheduled, and what is closed

✅ **D-C IS DONE — the benchmark ran on 5 Sep. Results in §8.** It returned a
split verdict: v7 wins discrimination with a separated interval and loses
retrieval, and one mechanism explains both. **D-D is therefore now live, and §8.6
is the recommendation.** Confirmed by SG 5 Sep:
*"the casefold problem can wait, let's press on with v8"*.

| | status after 5 Sep |
|---|---|
| **D-0** bundle the tokeniser fixes | ✅ **Resolved** — into v8's re-embed, no standalone pass |
| **D-A** NFKC + casefolding | ⏸ **Closed, waiting** — rides D-0; measured exposure ~194k of 60.1M Latin docs |
| **D-B** CJK/Japanese romanisation policy | 🛑 **Ruled out** — "cross-script only" does not buy a `ja` kanji reading table |
| **D-C** an evaluation that can fail | ✅ **DONE — §8** |
| **D-D** retrain, objective and labels | ▶ **LIVE — the gate is lifted; see §8.6** |
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

🛑 **RETRACTED BY ITS SOURCE, 5 Sep — the numbers below are WITHDRAWN, and the
argument is not.** `gotw-eb` found that the coordinates underlying every figure
in this section are substantially corrupt, and withdrew them unprompted. Its
first report said ~3.5%; **its own follow-up corrected that UPWARD to 13.7%**,
because the 3.5% counted only rows where *both* coordinates were present and so
could not see the largest defect at all:

```
Chinese  2,414 places, 1,351 with coordinate data
  half-coordinate (lat or lon, not both)   143   10.6%   <- invisible to the first count
  negative longitude (impossible for CN)    28    2.1%
  outside national bbox                     14    1.0%
  USABLE                                 1,166   86.3%

Russian  3,535 places,   351 with coordinate data
  half-coordinate                          138   39.3%
  negative longitude                        10    2.8%
  outside national bbox                      2    0.6%
  USABLE                                   201   57.3%
```

⚠ **The correction is the lesson, not the number.** A filter that requires both
fields to be present in order to test them is blind to the case where one is
missing — and that case was three times commoner than everything the filter
could see. It is `filters must report their denominator` in a new costume: the
denominator here silently became "rows complete enough to check".

**What this retracts:** the `150 → 86` leaf-hit figure and the `11/18 → 18/18`
parentage figure below, and with them this section's quantitative claim. **What
survives untouched:** the *qualitative* ceiling — query expansion cannot conjure
a document that is not in the index, and neither can a better embedding. That
argument never depended on a coordinate. It is why the ≥1M retrieval benchmark
measures against documents that demonstrably exist, which remains correct.

**Do not re-cite the numbers below.** They are kept only so that anything else
resting on them can be found.

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

🛑 **TESTED AND REFUTED, 5 Sep — do not carry this as an open recommendation.**
`gotw-eb` built and measured the rule-based mapping this section proposed, and it
does not survive:

* **It is not Wade-Giles.** WG renders Jiangsu *Chiang-su*; the 1856 book prints
  *Keang-su*. Wade's syllabary **postdates the edition**, so a standard WG table
  maps none of the corpus's forms.
* **The regular pattern is administrative generics, not syllables** — `-heen`/
  `-hyen` xian 368, `-chu` zhou 229, `-fu` 212, `-ting` 60 = **36% of Chinese
  headwords**. Stripping them measured **1 better / 8 equal / 0 worse** on a
  nine-case test, with nearly everything at confidence 22 in both columns. **It
  changes which wrong answer you get.**

⚠ **This was the only "cheap lever" this document identified, and a consumer
tested it to destruction within hours.** The text below is kept because the
*reasoning* about variant economics is still sound and may apply to a different
transformation — but the specific proposal is dead, and §7 decision C should be
read as "is there a lever at all", not "build this one".

**The original recommendation, retained for its reasoning:** a rule-based
transcription-convention mapping, not a model. `‑hyen`/`‑heen` → xian, `‑fu` → fu, `keang` → jiang, `‑pih` → bei,
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

### 7.3b The backup that the re-embed made urgent — done

✅ **`prod-manual-20260905t1931z`, SUCCESS, 22/22 shards, 0 failures** (31.7 min),
covering `places_h3ccode-20260805t120000z` (23.6 GiB) and
`toponyms_temporal-20260731t160000z` (50.1 GiB). Verified by listing the repo,
not by the PUT response. Latest prior snapshot was **6 August**, so the 2 Sep
`h3_cover` remediation, the 3 Sep MultiPoint fix and the 5 Sep re-embed of
100,960 documents had been unprotected.

**SG chose a third option neither this document nor `indexing-04` had listed**: a
new `prod_repo` at `/ix1/ishi/es/snapshots/prod`, inside the existing `path.repo`
allowlist so no ES restart was needed. `staging_repo` stays `readonly` and
untouched, `cluster_exchange` keeps its handoff purpose. The reasoning is the
good part: **SLM retention can only prune snapshots it created itself, so making
`staging_repo` writable would have put 53 snapshots and 401 GB within retention's
reach.**

SLM policy `prod-weekly` now exists — cron `0 0 2 ? * SUN`, keep 8 (min 4,
expire 120d), scoped to `prod_repo`, indices given as the **aliases** so it
follows future cutovers. ⚠ **The cron is UTC. First run is 02:00 UTC on 6 Sep —
22:00 EDT on the server's clock, 03:00 BST on SG's.** Three timezones in a
project where run-ids-UTC-versus-hosts-EDT has already misattributed a regression
once.

⚠ **SLM was never disabled** — `operation_mode` was already `RUNNING` with
`total_snapshots_taken: 0`. There was no fault to find; there was simply no
policy.

🛑 **STILL OPEN, and explicitly not answered:** *does ES validate `path.repo` at
start-up such that a wedged `/ix1` blocks boot?* ES has been up 5d11h and did not
restart during either of today's wedges, so the logs say nothing either way.
Settling it on prod means restarting during an outage. **It is cheaply answerable
on a throwaway ES with `path.repo` pointed at an unreachable path — a staging
experiment, not a production one.**

*Unrelated but recorded so it is not misread later: `toponyms` `store.size` fell
59.8 gb → 49.8 gb during the backup window. Not loss — `docs.count` identical at
72,703,777, `docs.deleted` 292,493, 38 background merges reclaiming deletes left
by the re-embed. Heap 33% of 28 gb, so not a repeat of the HNSW merge OOM.*

### 7.2b The real gap is evaluation, not transformation

`gotw-eb`'s conclusion after testing four China proposals — alias table, generic
stripping, radial pass, containment rejection — is worth more than any of them:
**every one was assessed with proxies (confidence, hit counts, containment
agreement), none of which measures correctness.** That is why it could not tell
whether its own 150 → 86 was a loss or a gain, and why `Tang-Tu-Heen → Gushu`
(Gushu being Dangtu's county seat, so arguably right) scored as a non-event.

It is building a **hand-adjudicated set of Chinese places labelled with a correct
WHG id or an explicit "absent from the index"**, with labels drawn from evidence
**independent of the index** — the book's own printed coordinates and printed
variant forms — so it cannot be circular.

**Design points worth stealing for our own benchmark** — this side fought the
same independence battle with Epitran and did not arrive at all of these:

* **Labels never consult `score` or `confidence`.** Both are recorded for every
  candidate and neither is used to decide — so **the set cannot confirm what it
  was built from**. That is the property we had to reject Epitran to get.
* **Asymmetric radii: 25 km to assert a match, 60 km to assert absence.** Both
  err toward `review`, because *a wrong gold label silently corrupts every
  experiment scored against it, whereas an unlabelled row costs only effort.*
* **`absent` is measured, not inferred.** It means: the book says where the place
  is, and a deliberately broad search — no containment, both modes, all printed
  variants, size 20 — finds nothing of that name within 60 km. **It explicitly
  does NOT mean "our cascade missed it".**
* **The `neither` tail is deliberately included** (stratified 60/20/rest across
  coords / variants / neither). *A set built only from coordinate-bearing places
  would measure the easy half and flatter everything scored against it.*
* **`review` is reported as a split, never silently counted as either.** Only
  `match` and `absent` are asserted; the remainder is the honest one and needs a
  human before this is a gold standard rather than a strong prior.

🛑 **The "absent" labels matter directly to v8.** §8's retrieval run found
**R@200 ≈ 0.48 for every method tested**, including a near-oracle baseline: half
the true partners unreachable in the top 200 of a million by *any* technique.
**We cannot currently separate model failure from absent documents.** That set
would convert §7.1's ceiling from an inference into a measurement, and it is the
single most useful external contribution to the benchmark on offer.

⚠ Note the shape it shares with our own labelling problem: we had to reject
Epitran as a positive-pair labeller because it is v7's own front end (§6 D-0
notes), i.e. the label would have sat inside the thing measured. Independence of
the labeller is the property both efforts had to fight for.

### 7.4 place#163 — and a reason to re-examine the scoping decision

**`place#163` (OPEN): "consider retraining Symphonym on LHPN Welsh/English
name-variant pairs", gated on `place#161` (RCAHMW licensing, paused 30 Jul,
outreach drafted and never sent).** It is a reminder issue, explicitly not a task
and not authorisation to acquire data. But its content bears directly on D-D's
label design and on the 5 Sep scoping decision.

What it found, from ~30k sampled LHPN records (2 of ~14 county filters, so
indicative not corpus-wide):

* Only **~5.5%** of records carry a populated `HeadName`, i.e. are clustered to a
  canonical form — *"don't extrapolate 700k records → 700k useful pairs."*
* Within that subset, candidate variant pairs pass `generate_pairs.py`'s own
  similarity gate at **80.5–86.7%**, against the existing `cy`/`en` co-attested
  pairs in the corpus which are **mostly literal translations** (`Efrog`/`York`,
  `Teyrnas Prydain Fawr`/`Kingdom of Great Britain`) and score near zero.
* The surviving pairs are **Welsh orthography ↔ English clerk transliteration**:
  `Llanddona`/`Seynt Dona`, `Llandegfan`/`Landegvan`, `Aberffraw`/`Abberfray`.
  Driven by *how the sound of a name got written down by non-Welsh-speaking
  scribes across centuries*, not by translation.

🛑 **THAT IS THE SAME PHENOMENON AS GOTW's `Keang-su`/Jiangsu.** Both are
historic-orthography variation — a name's sound recorded by a scribe working in
another convention. Both were de-scoped on 5 Sep by "cross-script phonetic
matching, and only that", because both are **same-script**.

**Three measurements taken since that decision point the same way:**

1. **GOTW: 16,486 of its corpus (14.2%)** sits in regions where 1856
   transcription conventions do not reach a modern index — an upper bound, but an
   order of magnitude above the 2.1% China figure the decision was taken against
   (§7.2).
2. **place#163: the LHPN pairs pass at 80.5–86.7%** where the corpus's existing
   Welsh pairs score near zero. A ready-made source of exactly this signal, if
   licensing ever clears.
3. **The cross-script alphabetic pairs are already solved.** `London ~ Лондон`
   scores **1.000** under anyascii-romanised Levenshtein (§4.5). Where the
   baseline fails is CJK↔Latin at 0.125 — and D-B, a Japanese reading table, was
   ruled out by the same decision.

⚠ **So "optimise for cross-script phonetic matching" may in practice mean
"optimise for Chinese and Korean", while ruling out the two problem classes that
have measured demand behind them and that most distinguish a HISTORICAL gazetteer
from a modern geocoder.** That is not an argument that the decision was wrong on
the information available on 5 Sep — it is an argument that three subsequent
measurements point one way and it is cheap to revisit **before** training data is
regenerated, which D-0 requires anyway.

**What would settle it:** the retrieval benchmark's per-script-pair breakdown.
If v7 already matches romanised Levenshtein on Cyrillic/Greek/Arabic↔Latin and
loses only on CJK, then the scoping question is not rhetorical — it is a choice
between a narrow data-hungry target and a broad one with two consumers waiting.

*(`place#163` remains gated on `place#161` regardless; nothing here authorises
acquiring LHPN data.)*

✅ **ANSWERED 5 Sep — this section's argument was accepted, and its gate is
lifted.** SG decided to **add historic orthography as a second v8 target**
(§6.1), which is the change the three measurements above were pointing at, and
separately reported that **RCAHMW are content for the data to be used**, with the
licence to be formalised at v8 release. So both halves of this card resolve: the
scoping question is settled in favour of the broad target, and `place#161` no
longer blocks using LHPN pairs as a training input.

⚠ **Two things this does NOT settle.** The **~5.5% `HeadName` yield** stands as
the binding constraint on how many usable pairs actually exist — the record count
is not the pair count. And the licence is **verbal and forward-looking**: it
gates *publication*, so it belongs on the v8 release checklist as an open item,
not in the done column.

## 8. THE BENCHMARK RAN — a split verdict, 5 September

All three gates, against **1,053,229 real names** with 8,713 queries and 148,410
balanced pairs (`indexing-9c`). The answer is more useful than "adequate" or
"inadequate".

| gate | result |
|---|---|
| **1 · geometry** | **FAIL** — effective rank **11.08 of 128**, stable at 11.00/11.07/11.07/11.08 across 6k→1.05M. A property of the space, not the sample. |
| **2 · discrimination** | **v7 WINS**, and the margin is real |
| **3 · retrieval** | **v7 LOSES** to romanised edit distance at every k except a tie at 200 |

**Discrimination** — 74,205 positives, balanced, corpus passes
`check_negative_matching` unmodified:

| scorer | AUC | AP | covered |
|---|---|---|---|
| **symphonym_v7** | **0.9324** | **0.9503** | 100.0% |
| double_metaphone_romanised | 0.9063 | 0.9249 | 99.0% |
| levenshtein_romanised | 0.9002 | 0.9257 | 100.0% |
| jaro_winkler_romanised | 0.8918 | 0.9218 | 100.0% |
| double_metaphone *(raw)* | 0.7160 | 0.7288 | **0.2%** |
| levenshtein_raw | 0.5482 | 0.5632 | 100.0% |

Paired bootstrap, 1,000 resamples: v7 − levenshtein_romanised **+0.0322, CI
[+0.0306, +0.0338] — SEPARATED**. Unlike MEHDIE's 0.852-vs-0.815 inside a 3.1pp
SE, **this margin exists**. It is also only ~3pp.

**Retrieval** — 8,713 queries, pool k=200:

| scorer | R@1 | R@10 | R@100 | R@200 | MRR |
|---|---|---|---|---|---|
| jaro_winkler_romanised | **0.0776** | 0.3146 | 0.4195 | 0.4491 | 0.1673 |
| levenshtein_romanised | 0.0729 | **0.3230** | **0.4414** | 0.4768 | **0.1674** |
| symphonym_v7 | 0.0662 | 0.2942 | 0.4359 | 0.4766 | 0.1461 |

### 8.1 One mechanism, not three complaints

**v7 separates a pair it is shown (AUC 0.932) and cannot rank a true partner out
of a million (R@10 0.294).** That is exactly what a rank-11-of-128 space with a
200th-neighbour cosine of 0.8627 predicts: enough structure for a pairwise
decision, not enough to order a large candidate pool. 🛑 **The gateway's k=200
KNN is the ranking case, so the failing metric is the operational one.**

### 8.2 ✅ The density hypothesis is confirmed, quantitatively

| n | 1st nbr | 200th nbr | gap |
|---|---|---|---|
| 6,000 | 0.8772 | 0.5666 | 0.3106 |
| 40,000 | 0.9187 | 0.7206 | 0.1981 |
| 200,000 | 0.9426 | 0.8036 | 0.1390 |
| 1,053,229 | 0.9623 | 0.8627 | 0.0996 |

Fitting: **gap ∝ n^−0.22, halving per ~23× of corpus.** Extrapolated to the live
72,703,777 the gap is 0.039, implying a 200th-neighbour cosine of **~0.923** —
against `knn_pass_quality`'s recorded *"the 200 nearest neighbours of anything sit
above cosine 0.93"* on the live index. **The two measurements were never in
conflict; they describe different densities.**

⚠ **Consequence for the gate: a PASS on `nn_gap_min` is evidence only at the n it
was taken at.** A small corpus understates this defect.

### 8.3 🛑 A THIRD OF ONE STRATUM IS SOLVED BY CONSTRUCTION — and it corrects §4.5

**35.1% of CJK↔LATIN positives (6,731 of 19,192) are byte-identical after
romanisation**, because the Latin label was itself produced by transliteration
upstream. On that stratum `levenshtein_romanised` scores **1.000 by
construction** — it is a **near-oracle, not a baseline**, and the comparison is
transliteration against transliteration.

| stratum | identical after romanisation | v7 R@10 vs lev_rom |
|---|---|---|
| CJK ↔ LATIN | **35.1%** | −0.33 |
| THAI ↔ LATIN | 0.3% | −0.30 |
| CYRILLIC ↔ ARABIC | 0.1% | **+0.26 … +0.35** |

**Where romanisation is LOSSY — an Arabic abjad dropping its vowels,
`kstnw-del-rwbledw` against `kastano-del-robledo` — v7 wins by +0.26 to +0.35.**
Its five biggest wins are CYRILLIC→ARABIC, DEVANAGARI→ARABIC, ARABIC→CYRILLIC,
LATIN→DEVANAGARI, LATIN→TELUGU. Its five biggest losses are LATIN↔CJK,
LATIN↔THAI, LATIN→HANGUL — **every one a script whose Latin partner label is a
romanisation.**

⚠⚠ **THIS REFUTES AN ARGUMENT THIS DOCUMENT MADE.** §4.5 inferred from a single
hand-picked pair (`London ~ Лондон` scoring 1.000) that *"cross-script in practice
narrows to CJK"*. **The benchmark says the opposite**: CJK is where the baseline
is a near-oracle by artefact and v7 is *worst*; v7's value is in non-Latin ↔
non-Latin, where romanisation loses information. A generalisation from one
example, contradicted by 8,713 queries. **The scoping decision of 5 Sep is
therefore better supported than §7.4 suggested — v7 demonstrably earns its place
on cross-script pairs the baseline cannot cheat.**

⚠ **Latin-involving and non-Latin↔non-Latin pairs must be reported separately.**
A corpus-wide retrieval average is dominated by an artefact of how the labels
were made.

### 8.3b THE CORPUS IS THE BEST-DOCUMENTED SIXTH OF THE INDEX — every §8 figure is an upper bound

🛑 **`indexing-9c`, `b327ac1`. This is the third instance of one pattern, and the
only one that is PERMANENT.** A place enters the evaluation corpus only if it
carries names in **two or more scripts** — which is a fact about **how well
documented it is**, not about the matching problem. Over 20,000 randomly sampled
live places:

```
qualifying (>=2 scripts)    3,179   mean 6.45 toponyms/place   median 3
not qualifying             16,821   mean 1.67 toponyms/place   median 1
qualify rate 15.90%                 prominence ratio 3.9x
```

**So every retrieval figure in §8 — R@200 ≈ 0.48 included — is measured on the
best-documented sixth of the corpus and is an UPPER BOUND.** The true rate over
all places is worse. ⚠ The namespace mix inverts as well (`osm` is 45% of
non-qualifying places against 22% of qualifying), so this is **not a uniform
thinning of one population**.

🛑 **It is not fixable by sampling differently, and nobody should try.**
Co-attestation cannot produce a cross-script pair for a place with one name, so
**the conditioning IS the positive source.** Unlike the romanisation artefact
(§8.3), which is confined to a stratum, and unlike the ordering bias (§6.2),
which a position column will let us measure — this one is **permanent and
reportable only**.

### The pattern, named — three instances in one evaluation

| # | Confound | Reporting axis it hides behind | Fixable? |
|---|---|---|---|
| 1 | 35.1% of CJK↔Latin positives romanise byte-identically (§8.3) | per-script-pair | stratum-confined |
| 2 | Pack ordered easiest-first (§6.2) | per-script-pair | measurable, given a position column |
| 3 | Corpus requires ≥2 scripts — the best-documented 15.9% | per-script-pair | **permanent** |

🛑 **All three are invisible for the SAME structural reason: the confound is
orthogonal to the reporting axis, so it shifts every cell in the same
direction.** No cell stands out, nothing looks anomalous, and **adding cells
cannot help** — the breakdown is not under-resolved, it is *structurally blind*.
**Inspecting output at any granularity fails.**

✅ **THE TECHNIQUE THAT DOES WORK, and it is the transferable result of this whole
exercise:**

> **Ask what had to be true of a record for it to ENTER the corpus at all, then
> ask whether that property correlates with difficulty. Selection criteria, not
> results.**

**Operationally:** for every filter, join and cap in the corpus builder, state
what it selects *for*, and measure the surviving population against the excluded
one on a difficulty proxy. `indexing-9c` found instance 3 exactly that way — it
had documented the ≥2-scripts requirement as a *mechanism* and never asked what it
*selected for*.

⚠ **This is why an approximate stratification axis is worse than none.** If the
`~1,200` coordinate-bearing boundary is only approximately derivable, a
stratification on it silently mixes the strata — and since coordinate-bearing
correlates with how well the book documented a place, that is **this same pattern
with a different filter**. An approximate axis used as an exact one *looks like
control*.

### 8.4 What must not be quoted without its caveat

* **R@200 is ~0.48 for EVERY method**, including the oracle-ish baseline. **Half
  the true partners are unreachable in the top 200 of a million by any technique
  tested.** Some is exonyms, included deliberately. Nobody should read v7's
  number as though 1.0 were attainable.
* **The retrieval gap is ~0.03 MRR and sits inside the artefact above.** Do not
  spend a GPU on "v7 loses retrieval" alone.
* **The geometry result is the strongest evidence and needs no corpus at all.**

### 8.5 A pipe turned SIGKILL into exit 0

`np.linalg.svd` on a 1,053,229 × 128 matrix was **OOM-killed at 15 GB and the run
reported success** — `python … | tail -50`, so the pipeline's status was `tail`'s.
Two of three results, no output file, "completed, exit code 0". **The
hash-of-nothing shape in a new costume**, and the fourth pipe-related failure of
the day. Fixed by taking eigenvalues of the 128×128 Gram matrix (the squared
singular values), verified bit-identical against the SVD at 6k and 40k rather
than assumed.

### 8.6 The recommendation D-C was built to produce

**Retrain — but for the geometry, not for the retrieval number.**

* The retrieval delta is ~0.03 MRR and sits inside the romanisation artefact
  (§8.3). **It is not a reason to spend a GPU.**
* The **rank-11-of-128** result is. It needs no corpus, it replicated across four
  sample sizes and two independent implementations, and it is the single
  mechanism that explains both gates: enough structure to score a pair, not
  enough to order a pool of a million. **It is also a defect that no amount of
  better training DATA fixes** — it is what the objective and the distillation
  produced, so only §4.2's changes address it.
* The measurable success criterion is therefore **effective rank ≥ 40 of 128 with
  R@10 at or above `levenshtein_romanised` on the non-Latin↔non-Latin strata** —
  the strata where the baseline cannot cheat. Not corpus-wide retrieval, which is
  dominated by an artefact.

⚠ **And the honest alternative, which the benchmark also supports:** if the
operational need is *pairwise scoring* rather than *ranking a pool*, v7 is
already ahead of every free baseline with a separated interval, and the money is
better spent on D-0's tokeniser fixes and a transcription-convention mapping
(§7.2) than on a retrain. **The benchmark was built to be able to say that, and
on discrimination it does.**

## 9. Scale of likely improvement — and what cannot be claimed

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

## 10. The pending IPA / PanPhon recomputation

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

## 11. Housekeeping found on the way

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

## 12. Two deployment faults found on 5 September, both silent

Neither is a Symphonym fault. Both are recorded here because they are the same
fault class as everything in `developer/postmortem-ingestion-faults.md` — *a step
reports nothing wrong because the step never ran* — and both cost real work today.

### 12.1 A tracked file was the only writable place for a per-host value

**Symptom.** `gotw-eb` ran `es -staging-start`, was told `STAGING ES READY`, and
was silently restored from the **6 August places-only snapshot** — no toponyms
index at all. The fix for exactly that (`d8a82f6` + `ba9a89c`, which aborts when
toponyms is absent and restores from `prod_repo`) had been committed and pushed
**hours earlier** and was not deployed.

⚠ **`prod_repo` is read-only in STAGING's cluster state only — never say "the
read-only repo".** In the **production** cluster it is registered **writable and
must stay so**: SLM `prod-weekly` writes to it. `indexing-04` flagged this
because someone tidying up on the strength of the shorthand would silently kill
the weekly backup, and a missing weekly backup is invisible until it is needed.

**Mechanism, which is the part worth keeping.** `gateway/config.py` read only
`.env`, and it is the gateway process's *sole* source of environment:
`scripts/_common.sh` sources both `.env` and `.env.local` but **without
`set -a`**, so nothing it reads is exported to the child python. A host needing to
pin `SYMPHONYM_MODEL_DIR` off the wedged `/ix1` mount (place#242) therefore had
**no choice but to edit the TRACKED `.env`**. From that moment, `git pull` on the
deployed checkout could not fast-forward past any commit touching `.env` — and
`ba9a89c` touches `.env`. The deployment did not fail loudly; it simply never
happened, and nothing anywhere said so.

**Fixed (`0db74a8`).** `gateway/config.py` now layers `.env.local` over `.env`
with `override=True`, matching `processing/settings.py` and
`clustering/config.py`. The layer is **guarded**: `load_dotenv` *propagates*
`PermissionError`, and `.env.local` on this host is mode 660 `stg135:ishi` while
the gateway runs as `gazetteer` — which reads only because gazetteer's primary
group *is* `ishi` (uid 11001, gid 16604, checked rather than assumed). Absent is
silent; present-but-unreadable warns to stderr and starts anyway, so a
permissions quirk can never become a gateway that will not boot.

**Deployed and verified on the host:** HEAD `e03d4cc` → `0db74a8` (34 commits),
working tree clean, `RESTORE_REPO_NAME` present in `.env` and referenced 7× in
`processing/es_staging.sbatch`, the pin preserved in `.env.local`, `_common.sh`
sourcing and resolving `JAVA_HOME` to the ES-bundled JDK on `/vast`. No gateway
restart was needed: the running process already held the correct value, and any
future restart now reads the same value from the per-host file.

⚠ **The generalisation to check elsewhere:** *any* local modification to a
tracked file on a deployed checkout is a silent embargo on every future
deployment of every commit that touches it.

### 12.1b The first fix left the same door open one room along

`indexing-04` verified the fix **behaviourally** rather than statically — running
the selection predicate against real repository contents — and confirmed it fires
on the input that caused the incident (6 Aug, places-only → abort) and passes on
the new one (5 Sep, places + toponyms → proceed). It then found the case the
guard did **not** cover: *no usable snapshot at all*.

Three mechanisms collaborated, all in `processing/es_staging.sbatch`: a bare
`except: pass` in the listing probe, a `2>/dev/null`, and a `|| true`. Any failure
to read the repository — unregistered, `/ix1` wedged, an ES error body, malformed
JSON — produced an empty `LATEST_SNAPSHOT`, fell through to `create_indices`, and
printed `STAGING ES READY`. **`set -e` does not catch it**, because the
registration was `curl … | python3 -m json.tool` and an ES *error* is valid JSON:
the pipeline's status is `json.tool`'s, and it exits 0.

🛑 **The first fix made this MORE likely, not less.** It moved the source from
`staging_repo` (53 snapshots, long established) to `prod_repo` — which holds
**one** snapshot, is ninety minutes old, and lives on `/ix1`, the mount that
wedged twice on 5 Sep.

**Fixed** in the same file: the registration response is checked for
`acknowledged: true`; the listing distinguishes *unreadable* from *readable and
empty* and reports its denominator (`53 snapshot(s) present, 0 SUCCESS with a
'places' index` is a different problem from `0 present`); and an explicitly
configured `RESTORE_REPO_NAME` that yields nothing is an **error**, not an
invitation to start a fresh site — `SKIP_SNAPSHOT_RESTORE=1` remains the way to
ask for an empty one, and it short-circuits earlier so the branch is unreachable
by accident.

**Demonstrated, not asserted**, against six payload shapes through the shipped
probe text. The new code returns `OK` / `EMPTY` / abort correctly for all six.
The old code returned **exit 0 and "empty indices" for five of the six** —
including the ES error body and the malformed response. That is the discriminating
comparison: the guard is load-bearing because the thing it replaces demonstrably
passed everything.

⚠ **`prod_repo` holds exactly one snapshot and staging now depends on it.** SLM
retention (`min_count 4`, `max_count 8`) only ever deletes snapshots SLM itself
created, so `prod-manual-20260905t1931z` is a permanent floor — deliberate, but
manual snapshots accumulate and nothing prunes them. Also: **SLM silently
discards a `timezone` field**, returning `acknowledged:true` without storing it,
so the schedule is UTC-only and will drift an hour when EDT ends on 1 November.
`indexing-04` caught that only by comparing `next_execution_millis` across two
policies identical but for that field.

✅ **The one-number backup health check, and it is portable.**
**`_slm/stats.total_snapshots_taken` distinguishes "backups are HAPPENING" from
"backups are CONFIGURED", and nothing else on the cluster does.** It reads **0**
for a cluster whose SLM is *running* but has no policy — exactly the state
production sat in for a month while every other indicator looked healthy. No
history needed, no interpretation.

**And `prod-weekly` is now proven rather than assumed.** `indexing-04` had put the
**aliases** `places`/`toponyms` in `config.indices` and had no evidence SLM
resolves them, so it executed the policy by hand instead of waiting for 02:00 —
the cost of being wrong being a weekly backup that captures nothing, discovered by
nobody. Result: `SUCCESS`, 22/22 shards, 0 failed, ~20 s, resolved to
`places_h3ccode-20260805t120000z` + `toponyms_temporal-20260731t160000z`,
**incremental cost ~0 GB** (the repo stayed at 66G, fully deduplicated), and
`total_snapshots_taken` 0 → 1. `prod_repo` therefore now holds **two** snapshots,
so staging's "latest by start_time" selects the weekly — no behavioural
difference, both carry places *and* toponyms.

🛑 **SNAPSHOTS DO NOT TOUCH `/vast`, and an earlier draft of this paragraph said
they bore on the `/vast` capacity constraint. They do not.** `prod_repo` is at
`/ix1/ishi/es/snapshots/prod`, read from the live registration rather than from
notes:

```
/ix1/ishi   5.0T  3.3T used  1.8T avail  66%   <- snapshots live HERE
/vast/ishi  1.0T   799G used   226G avail 78%   <- ES data; untouched by snapshots
```

⚠ **The drift is the lesson, not the fact.** This session stated it *correctly*
earlier the same day — *"Both repos are on /ix1, not /vast — so this does NOT
consume the /vast headroom that prod ES needs"* — and then restated it from
memory hours later as its opposite, carrying the authority of having been said
before. `/vast` at 78% with 226 G free **is** genuinely tight and worth watching;
it is just not what backup sizing bears on, and **pointing backup sizing at the
wrong volume is how a real `/vast` problem gets attributed to a harmless one.**
Caught by `indexing-04`, which re-read the registration instead of either
session's notes. See `~/.claude/memory/claims-in-transit.md`.

### 12.2 In a shared working tree, "I have not pushed yet" is not a durable fact

`indexing-9c` reported seven commits awaiting authorisation to push. SG
authorised it; on checking, **all seven were already on `origin/main`** — they had
gone out inside *this* session's `git push`, because we share one working tree and
a push carries whatever is committed in it. `indexing-9c` had told SG the same
thing an hour earlier and it was already false when said.

**Neither party is notified.** So in this repo, a statement about push state is
only true at the instant it is checked, and it must be checked against the live
remote (`git ls-remote`) rather than a local tracking ref, which can itself be
stale. Verify immediately before acting, never from memory of an earlier check.
