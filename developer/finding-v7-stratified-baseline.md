# The v7 per-stratum discrimination baseline — and why the obvious gate cannot work

> **Measured 6 September 2026** on the existing corpus
> `/vast/ishi/symphonym-eval/20260905T2000Z` (148,410 pairs, built 5 Sep).
> **Reused, not rebuilt** — a rebuild runs against production, so this
> measurement is production-free only because the manifest matched.
> Code: `evaluation/stratified_baseline.py`. Raw:
> `/vast/ishi/ipa-v8/logs/stratified_v7_baseline.json`.

## ✅ Both corpus figures reproduce exactly

| scorer | this run | recorded in §8 |
|---|---:|---:|
| Symphonym v7 | **0.9324** | 0.9324 |
| `levenshtein_romanised` | **0.9002** | 0.9002 |

An unplanned end-to-end control on the whole pipeline — corpus loading, model
loading, embedding, scoring, metric. It is why the strata beside them are
trustworthy rather than provisional.

## The table

| stratum | N | v7 AUC | lev AUC | v7 margin | |
|---|---:|---:|---:|---:|---|
| CORPUS (all pairs) | 148,410 | 0.9324 | 0.9002 | +0.0322 | |
| **gain_v7zero** | 16,164 | **0.9560** | 0.9031 | +0.0529 | **FLOOR** |
| ↳ latin_involving | 14,154 | 0.9555 | 0.9058 | +0.0497 | FLOOR |
| ↳ non_latin_both | 2,010 | 0.9592 | 0.8912 | +0.0680 | FLOOR |
| **gain_improved** | 60,672 | 0.9212 | 0.8771 | +0.0441 | FLOOR |
| ↳ latin_involving | 52,042 | 0.9302 | 0.8900 | +0.0402 | FLOOR |
| ↳ **non_latin_both** | 8,630 | **0.8594** | 0.8008 | +0.0586 | FLOOR |
| **contamination (`sv`)** | 2,054 | 0.9749 | 0.8619 | **+0.1130** | |
| unchanged | 65,538 | 0.9348 | 0.9164 | +0.0184 | |
| excluded_quarantined | 3,982 | 0.9735 | 0.9594 | +0.0141 | |

🛑 **GAIN is a FLOOR, not a baseline, and is labelled so in the table.** A v7 AUC
over pairs whose languages had zero IPA at v7 training measures a model on
inputs it was never given a signal for. Under one caption it would be read as
commensurable with UNCHANGED however carefully prose hedged.

## 🛑 The finding: v7 scores *higher* on the stratum it knew nothing about

**0.9560 on `gain_v7zero`, against 0.9348 on `unchanged` and 0.9324 corpus-wide.**

**Verified mechanism, not inference.** `generator.py:152` gates the training
manifest on `WHERE t.ipa IS NOT NULL` (the ES fallback at `:175` requires
`panphon_embedding`, the same gate through a different store). Those twenty
languages had **zero** IPA under the 45-entry routing table, so not one of their
toponyms could enter a positive pair. **v7 never trained on them at all.**

So 0.9560 is **pure orthographic generalisation** from typologically similar
languages v7 *did* train on — `ca`, `gl`, `ast`, `oc`, `vec` resemble the
Spanish and French pairs it saw. Confirmed by v7 beating romanised Levenshtein
there by **+0.0529** while having had no phonetic signal for the stratum: the
advantage cannot be phonetic knowledge of those languages, because there was
none.

### What that does to the gate

A gate asking *"did v8 improve `gain_v7zero`?"* asks for a rise from 0.9560 with
**0.0440 of room**, on a metric measuring a capability **v8 does not change** —
v8's contribution there is to add training signal to languages already handled
by transfer. **v8 could deliver exactly the improvement it was built for and
this gate could not show it.**

This is the `gate_inherits_the_finding_order` failure in a new place: the
stratification is right about *where* to look and the metric is wrong about
*what* to look at.

## ✅ The primary cell: `gain_improved / non_latin_both`, 0.8594

Headroom (0.1406, the largest in the table) is the weaker argument for it. The
strong one is **falsifiability**: between two non-Latin scripts there is no
shared surface for a character model to exploit, so **orthographic transfer
cannot help and phonology is the only available mechanism.** It is therefore the
one cell where a real IPA improvement *must* show. Headroom alone would point at
any weak cell; this points at the cell where the hypothesis can fail.

It is also where the input actually moved: `ja` 357,943 → 949,493 (**2.65×**),
`zh` +276,524.

## ⚠ `sv` — gate on the margin, not the absolute

v7's **largest margin over the baseline anywhere in the table**: +0.1130,
against +0.0184 for `unchanged`. Whatever v7 learned about Swedish does real
work that edit distance cannot replicate.

v8 trains on ~109k **more** Swedish, 94.4% of it Wikidata labels whose tag
records a wiki edition rather than the name. So the stratum flagged for
degradation is the one with the most to lose, and what is at risk is a
*demonstrated* capability rather than a hypothetical one.

**Gate on the margin because the absolute hides the loss:** 0.9749 → 0.9600
reads as a 1.5% slip; the same movement as a margin is 0.1130 → 0.0981, **a 13%
loss of the model's entire advantage over the baseline.**

## Method points that are load-bearing

- **Quarantined takes PRECEDENCE** in stratum assignment, so a `ceb`↔`en` pair is
  excluded rather than credited to `en`'s gain. `excluded_quarantined` scores
  0.9735, so folding it anywhere would have inflated that stratum with material
  v7 had no IPA for on either side.
- **Latin-involving and non-Latin↔non-Latin are never averaged** — 35% of
  CJK↔Latin positives romanise to identical strings, which has already produced
  one misleading result in this project.
- ⚠ **Uncovered pairs are EXCLUDED, not zeroed.** An earlier version of this
  coerced `Scored.score = None` to `0.0`, scoring a pair the baseline *declined
  to answer* as maximally dissimilar — which would have credited v7 with a
  margin it had not earned, in exactly the stratum where the baseline is
  weakest. Caught before the reported run.
- **A stratum under 100 pairs reports N and `INSUFFICIENT`** rather than an AUC.

## Feasibility limit, stated rather than worked around

8 of the 20 v7-zero languages hold under 400 pairs — `gl` 138, `vec` 207, `oc`
222, `ast` 224, `sk` 247, `nn` 294, `cy` 295, `eu` 325, `sl` 326. The stratum
aggregate is sound at 16,164 pairs; **per-language AUCs for those eight are
not**, and the code says so. Targeted augmentation was declined: it needs a
corpus rebuild against production, the gate operates at stratum level where N is
ample, and no v8 decision depends on those eight numbers.
