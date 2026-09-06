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
was measured once, on 3,000 toponyms, and never again** — `panphon_features`
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

🛑 **The discrepancy is not scale.** At the *same* n = 3,000 this measures
**3.130**. So whatever produced 4.37 differs in the sample or the treatment,
not in the size. **Finding 2's direction is confirmed and strengthened** — the
input representation is *more* collapsed than recorded, so the causal chain
holds a fortiori — but the specific value 4.37 should not be requoted.

⚠ **A plausible reconciliation, offered as a hypothesis and not a result.** The
script breakdown below puts Arabic at **4.198**, the closest of any stratum to
4.37. §4.5 records that the evaluation testsets are "all Arabic/Hebrew/Latin
historical gazetteers". If the original 3,000 toponyms were drawn from that
corpus rather than from the index, an Arabic-heavy sample would land near 4.2.
That is consistent, unverified, and would make 4.37 a corpus property read as a
representation property — the pattern already logged four times in this
campaign. **It should be checked before anyone reconciles the two numbers.**

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
- **4.37 should be retired in favour of 3.12**, with the Arabic hypothesis
  checked before the two are reconciled.
- **The "cliff" description must be scoped to the student.** PanPhon is
  dominated, not truncated, and the distinction matters to any argument about
  what a teacher fitted to it could learn.
