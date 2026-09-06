# PanPhon192 effective rank, re-measured at scale

> **Measured 6 September 2026** on the IPA store built the same day
> (`/vast/ishi/ipa-v8/store/ipa.duckdb`, 49,749,377 rows carrying IPA).
> Estimator: `evaluation.geometry.measure_geometry` — the shipped one that
> produced the 10.83 figure. Pooling: `IPAConverter.to_embedding` — the shipped
> 8-bin positional pooling, i.e. the vector positives were actually clustered
> in. Neither was reimplemented; reimplementing either would have made the
> control meaningless. Raw: `/vast/ishi/ipa-v8/logs/rank_full.json`.

## Why this needed doing

§3 of `plan-symphonym-v8.md` is a causal chain: PanPhon input rank **4.37 of
192** → a teacher fitted to it → a student distilled to rank **10.83 of 128** →
a retrain is justified. The student's 10.83 was measured twice, on two
implementations, and shown stable from 6k to 1.05M names. **The teacher's 4.37
was measured once, on 6,000 toponyms, and never again** — `panphon_features`
has been NULL in every surviving store since. It was the most load-bearing
unverified number in the plan, and the IPA store is the first time the input to
recompute it has existed.

## ✅ The control passed first — everything below is gated on it

The same estimator was run over **299,524 v7 student embeddings** (from
`embeddings_v7.parquet`, sampled by id hash, not by `LIMIT` — the parquet is
ordered and a head sample measures the head):

```
v7_student_128d   n=299,524  d=128   effective rank = 11.067
```

against a known **10.83** from two independent implementations. That is a third
reproduction, at a scale between the two prior ones. Had it returned something
else, the estimator would have been wrong and any PanPhon number from it
uninterpretable — the script exits without computing one in that case.

## The curve — 4.37 does not reproduce, and the finding survives anyway

| n | effective rank of 192 |
|---:|---:|
| 3,000 | **3.130** |
| 29,768 | **3.104** |
| 299,998 | **3.121** |
| 2,999,994 | **3.122** |

**Stable to ±0.5% across three orders of magnitude.** The question the peer
posed — does 4.37 survive at scale, or climb — has a third answer: it is flat,
and it is flat at **3.12, not 4.37**.

🛑 **The discrepancy is not scale.** The plan's measurement was at n = 6,000;
this run brackets it with **3.130** at 3,000 and **3.104** at 29,768. So whatever produced 4.37 differs in the sample or the treatment,
not in the size. **Finding 2's direction is confirmed and strengthened** — the
input representation is *more* collapsed than recorded, so the causal chain
holds a fortiori — but the specific value 4.37 should not be requoted.

### 🛑 The MEHDIE hypothesis — TESTED AND REFUTED, 6 Sep

I proposed that the original 6,000 might have been drawn from the MEHDIE
testsets (Arabic/Hebrew/Latin historical gazetteers), since ARABIC measured
4.198, closer to 4.37 than any other stratum. **Tested directly by running the
MEHDIE toponyms themselves through the same shipped pipeline. It is wrong.**

One circumstantial detail is a near-perfect match. The five testsets hold eight
distinct gazetteer files, 6,914 title rows, **6,013 distinct titles** — against
the plan's "6,000 distinct real toponyms". Script mix: ARABIC 3,280, HEBREW
2,654, LATIN 79; IPA produced for 6,013 of 6,013.

**But the rank does not match, and it is not close:**

| sample | n | effective rank |
|---|---:|---:|
| MEHDIE, all | 6,013 | **7.247** |
| MEHDIE, Arabic | 3,280 | 7.196 |
| MEHDIE, Hebrew | 2,654 | 5.726 |
| *plan's recorded figure* | *6,000* | *4.37* |
| corpus-wide (this run) | 3,000–3M | 3.12 |

MEHDIE gives **7.25**, nearly double 4.37. If the original had been measured
there it would have recorded ~7.2. **The hypothesis is refuted**; 4.37's
provenance remains unresolved, and the 6,013 ≈ 6,000 coincidence is exactly
that until something else corroborates it.

### ✅ What the test found instead, which matters more

**PanPhon's effective rank is a property of the corpus and the representation
jointly, not of the representation.** Holding SCRIPT constant and changing only
the corpus moves it as much as changing script does:

| script | corpus-wide store | MEHDIE | ratio |
|---|---:|---:|---:|
| ARABIC | 4.198 | 7.196 | **1.71×** |
| HEBREW | 3.753 | 5.726 | **1.53×** |

against a 1.55× spread across all scripts *within* the store (CJK 2.703 →
ARABIC 4.198). So the two effects are comparable in size, and my own
by-script framing above understates it: script is not the dominant driver,
**corpus composition is**.

⚠ **This also qualifies the stability result.** "Flat at 3.12 across three
orders of magnitude" is stability across sample SIZE *within one population*.
It is not robustness of the quantity, and the MEHDIE figure proves it: two
6,000-scale samples of real toponyms differ by 2.3×. **Any PanPhon rank must be
quoted with the corpus it was measured on**, and 4.37, 3.12 and 7.25 are not
competing estimates of one number — they are three different measurements.

### 🛑 The redundancy hypothesis — TESTED AND REFUTED ON THREE GROUNDS

I proposed that the index's lower rank reflects near-duplicate redundancy
(millions of similar GeoNames/OSM settlement names) against MEHDIE's curated
distinct places. **I tested it and it is wrong three times over.**

**1. The mechanism is impossible in its naive form.** The participation ratio is
*invariant to uniform replication*: duplicating every vector k times scales the
Gram matrix by k, scales every eigenvalue by k, and leaves the normalised
spectrum unchanged. Checked empirically rather than trusted — tripling every row
of a 19,812-vector index sample moved the rank by **δ = −0.000000** (3.1190 →
3.1190). So "there are duplicates" cannot by itself lower a rank. Only
*non-uniform* concentration could.

**2. The premise is false, and backwards.** Measured duplicate rates:

| corpus | n | distinct | duplicate rows |
|---|---:|---:|---:|
| index sample | 300,000 | 299,072 | **0.309%** |
| MEHDIE | 6,013 | 5,853 | **2.661%** |

**MEHDIE is 8.6× more duplicated than the index**, not less. The premise was
asserted from plausibility about what gazetteers contain, and the data says the
opposite.

**3. The intervention does nothing.** Deduplicating — exactly, then collapsing
vectors identical to one decimal place — leaves every stratum where it was, and
where it moves anything it moves it *down*, not up as predicted:

| stratum | n | raw | exact-dedup | near-dedup |
|---|---:|---:|---:|---:|
| index / ARABIC | 300,000 | 4.198 | 4.194 | 4.197 |
| MEHDIE / ARABIC | 3,280 | 7.196 | 7.196 | 7.169 |
| index / HEBREW | 143,149 | 3.753 | 3.477 | 3.448 |
| MEHDIE / HEBREW | 2,654 | 5.726 | 5.614 | 5.608 |

**The within-script gap survives dedup intact** — Arabic 1.72×, Hebrew 1.61×,
essentially unchanged from the raw figures.

**Controls.** An isotropic Gaussian at the same shape returns **190.15 of 192**,
so the estimator does report near-maximal rank when the data is genuinely
full-rank and the low numbers above are not an artefact of it.

### What this leaves

The gap is real, it is not redundancy, and it is **a property of what the index
CONTAINS rather than of how often it repeats it**. The index's Arabic and Hebrew
toponyms are phonetically less varied than MEHDIE's historical ones in some way
that survives deduplication.

⚠ I am not proposing a replacement mechanism. I proposed one story from
plausibility, it was wrong in its premise, its mechanism and its prediction, and
substituting a second untested story would be the same error. What is
established is the negative: **redundancy does not explain it, and the corpus
dependence of PanPhon rank still needs an explanation.**

## 🛑 The spectra have opposite shapes, and "cliff" belongs to only one of them

The plan says "the v7 spectrum does not taper, it falls off a cliff". That is
right about the student and **wrong about PanPhon**, which has a *lower*
effective rank by a different mechanism:

| σᵢ/σ₁ | v7 student (128-d) | PanPhon192 (3M) |
|---|---:|---:|
| σ₅/σ₁ | 0.739 | 0.182 |
| σ₁₀/σ₁ | 0.651 | 0.131 |
| **σ₂₀/σ₁** | **0.0059** | **0.103** |
| σ₅₀/σ₁ | 0.0058 | 0.067 |
| σ₁₀₀/σ₁ | 0.0058 | 0.037 |

| variance explained | v7 student | PanPhon192 |
|---|---:|---:|
| top 1 | 0.153 | **0.562** |
| top 10 | 0.875 | 0.718 |
| top 20 | **0.9995** | 0.790 |
| top 50 | 0.9997 | 0.909 |

**The student has ~10 real directions and then a cliff** — components 20–128
carry 0.05% between them. **PanPhon has one enormous direction** (σ₁ alone is
56.2% of all variance) **and then a long, slow taper** — components 20–192
still carry 21%.

So the two low ranks mean different things. The student's is dimensional
collapse: capacity that provably carries nothing. PanPhon's is *dominance*: one
direction explains over half the variance and the rest is a genuine tail. A
participation ratio of 3.12 with a 56% first component is not the same object
as a participation ratio of 11.07 with a 15% first component, and **the two
should not be compared as if the smaller number simply meant "worse"**.

⚠ Consequence for §3's argument: "~85% of the index's storage is spent on
dimensions that carry no information" is a claim about the STUDENT and survives
untouched. The corresponding claim cannot be made about PanPhon.

## By script — a real spread, and no averaging artefact

300,000 rows each:

| stratum | effective rank | rows available |
|---|---:|---:|
| ARABIC | **4.198** | 1,837,656 |
| non-LATIN | 3.380 | 9,682,040 |
| CYRILLIC | 3.076 | 3,525,475 |
| LATIN | 3.053 | 40,067,337 |
| CJK | **2.703** | 2,182,499 |

A 1.55× spread between the extremes, so the geometry genuinely differs by
script. But every stratum sits in the same regime, and Latin — 80.5% of the
rows with IPA — is within 2% of the corpus-wide figure. **The corpus number is
not an average over incompatible populations**, which was the thing worth
ruling out.

## What this changes

- **Finding 2 is confirmed on its weakest leg**, at 1000× the original scale,
  with a passing positive control. The retrain justification stands.
- **4.37 should not be requoted**, but neither should 3.12 replace it as a
  bare number: the MEHDIE test showed the quantity is corpus-dependent by
  2.3×, so it must always travel with the corpus it was measured on.
- **The MEHDIE hypothesis is refuted** (7.247, not ~4.37). 4.37's
  provenance is still unknown, and no code in the repository computes it.
- **The "cliff" description must be scoped to the student.** PanPhon is
  dominated, not truncated, and the distinction matters to any argument about
  what a teacher fitted to it could learn.
