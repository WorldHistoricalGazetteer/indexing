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

## 0. The short version — WHERE v8 STANDS (reassessed 6 Sep 2026)

🛑 **THE THREE LEGS HAVE BEEN REORDERED, AND THE OLD ORDER IS WHY THE
ACCEPTANCE GATE WAS WRONG.** This section previously led with the geometry
finding and treated the input data as a footnote. Everything measured since
inverts that. The legs, in their corrected order:

| | leg | status | what it now does for v8 |
|---|---|---|---|
| **1** | **The input data is materially better than what v7 trained on** | measured | **MOTIVATES the retrain, on its own** |
| **2** | **The training objective is exhausted** | measured, unchallenged | **DICTATES that the retrain change the loss** |
| **3** | The representation is rank-collapsed | measured, but corpus-conditional | **CONSTRAINS the architecture — no longer a motivation** |
| ~~4~~ | ~~The gateway tokenised queries differently from the index~~ | ✅ fixed, §5 | needed no retraining at all |

### Leg 1 — the input data. Promoted to primary.

✅ **MEASURED 6 Sep (`indexing-04`, 2,438 strata, full table at
`/vast/ishi/ipa-strata-11169066.out`), reconstructed against v7's own
`coverage_stats.json`.** The leg holds. Three corrections came with it, two of
which run *against* the version this section first published.

```
v7   31,113,585 IPA  = 46.49% of its 66,924,548 total corpus
v8   49,749,377 IPA  = 68.43% of    72,703,552
                       +21.9pp on a common denominator
```

⚠ **The headline this section first used — 54.02% → 68.43% — was NOT
like-for-like**, and understated the gain: 54.02% excluded non-training
namespaces while 68.43% is over everything. **46.49% → 68.43% is the comparable
pair.** The parenthesis carrying 46.49% was already here; the headline still
used the wrong one.

**Two figures, and they answer different questions — quote both:**

* **+18,635,792** — what v8 trains on beyond what v7 saw. The right number for
  *what the model gets*.
* **+15,949,102** — what better **routing** bought. 5,779,004 of the raw gain is
  **corpus growth** (66,924,548 → 72,703,552), which would have happened without
  any of this work. **This is the number the retrain earns**, and the honest one
  to quote for the leg.

**No stratum regressed** — the loss table is empty; not one language has fewer
IPA strings in v8 than v7. And the reclassification worry is clear: quarantined
rows carry `backend='(none)'`, so nothing moved from `ok` to `quarantined`.

**Twenty of the top thirty gain strata had v7 = 0 — genuinely new routes:**
`ga` 681,984 · `ca` 678,611 · `nb` 447,675 · `ce` 368,716 · `eu` 364,083 ·
`nan` 337,975 · `ast` 318,685 · `nn` 316,089 · `tt` 303,490 · `eo` 288,989 ·
`arz` 282,950 · `sh` 275,385 · `sl` 262,395 · `cy` 246,721 · `gl` 238,290 ·
`sk` 226,965 · `oc` 222,431 · `uz` 215,250 · `be` 200,279 · `vec` 179,507.
**Improved:** `en` +2,231,124 · `ja` +591,550 · `de` +329,273 · `nl` +329,248 ·
`ru` +296,246 · `zh` +276,524 · `sr` +266,566 · `fr` +259,790 · `id` +238,985 ·
`tr` +176,869. ⚠ v7 recorded only **49** strata against v8's 2,438, which is
itself part of the story.

**That is a retrain justification independent of geometry.** No architectural
work on v7's weights recovers strings it was never shown.

### ✅ AND THE LEG IS LARGER THAN "BETTER TEACHER INPUTS" — IPA GATES PAIR ELIGIBILITY

Raised by SG, 6 Sep, and it is not recorded anywhere else. `generator.py:155` is
`WHERE t.ipa IS NOT NULL` and `:179` requires `panphon_embedding` to exist.
**A toponym with no IPA cannot enter a positive pair at all.**

At v7's training that excluded roughly **46% of the corpus** — 31,113,585 of
66,924,548 — from the positive set entirely. Not mislabelled: **ineligible**,
never learned as anything.

So the recomputation does not only improve what the teacher is *shown*; **it
enlarges the set of toponyms eligible to be a training pair in the first
place.** That is plausibly the larger of the two effects.

### 🛑 THE CIRCULARITY QUESTION — asked by SG, and the answer is NOT "the AUC was good"

**The worry, stated fairly.** PanPhon selects the training pairs *and* is the
teacher's input representation, and the student distils from the teacher. So the
student could be nothing but a compression of PanPhon's own similarity
judgements, and a benchmark could confirm it while measuring nothing.

**Why it is narrower than it looks.** Training positives are **not** "PanPhon
says these two strings are close". `find_similar_in_place`
(`es_knn_helper.py:87`) runs HDBSCAN over PanPhon embeddings **scoped within a
single place** — so the *candidate set* is defined by the gazetteer's own
co-reference assertion and PanPhon acts only as a **filter** on it, removing the
exonyms (Ayers Rock / Uluru co-attest and are not phonetically related). The
external anchor does the heavy lifting.

✅ **And the benchmark's labels are phonetics-free — this is what actually
settles it.** `evaluation/build_corpus.py` builds positives as *"cross-script
name pairs of the same place"*, via `cross_script_pairs`, from the `places`
index. **There is no IPA, no PanPhon and no embedding anywhere in the label
construction**; the hard-link overlay is used only to stop true co-referents
being drawn as negatives. So AUC **0.9324** is not measuring "v7 reproduces
PanPhon" — it is measuring v7 against gazetteer-attested co-reference, which
PanPhon had no hand in.

⚠ **Stronger than mere independence:** the eval set *includes* pairs the training
filter would have **rejected** — co-attested pairs that are phonetically
distant. So it is if anything **harder** than the training distribution, not a
memorisation check. ⚠ This is the `labeller_inside_the_thing_measured` trap
(Epitran is v7's own teacher front end) and it does **not** apply here, because
the eval touches no IPA at all — but it must be re-checked for any future gate
that does.

### 🛑 DO NOT SUBSTITUTE v7 AS THE PAIR SELECTOR

Considered and **rejected**. The one-line reason: **v7 is not an alternative to
PanPhon — it is PanPhon seen through a rank-10.8 bottleneck.** The teacher's
input *is* PanPhon features and the student distils from the teacher, so
substituting v7 does not introduce a second, independent opinion; it swaps the
anchor for a lossy compression of the same one, and removes the only component
in the loop with a basis outside WHG (articulatory phonology, whose failure modes
are uncorrelated with the encoder's).

🛑 **And it would sabotage leg 3 specifically.** v7 occupies **10.8 of 128**
directions. As a selector it can only propose pairs that are close *within that
subspace*, so any distinction living in the 117 collapsed directions can never
enter the training set — **as a positive or as a hard negative**. v8 would be
trained on data blind in exactly the directions v8 exists to repair. v7's
confident errors (`Keang-su` → GANSU at 99.5) would arrive as **labels**, and
self-training amplifies systematic error with nothing left pulling back.

**The defect in PanPhon-based selection is COVERAGE, not circularity** — and leg
1 is what attacks it.

### ✅ WHAT TGN ACTUALLY PUBLISHES — censused, and `und` IS irreducible

SG asked whether Getty documents anything about term language or script that is
not obvious in the data as extracted. `indexing-04` censused the LOD export
(`explicit.zip`, 2026-01-04; 17 members, 45,280,094 triples in Terms alone) —
**not** recalled from a model of Getty's editorial database. Full census at
`/vast/ishi/tgn-census-11169092.out`.

⚠ **Scope, preserved deliberately:** every count below is *"present in the LOD
export on N of 5,315,747 terms"*. A field may exist editorially and be projected
away. **Absence here is not absence in Getty.**

🛑 **THE `und` POPULATION IS IRREDUCIBLE FROM THIS FILE.** There *is* a separate
Getty term-language assertion — `dcterms:language`, on 3,172,492 terms (59.68%),
which the extractor ignores. It recovers **nothing**:

```
terms (literalForm)         5,315,747   <- denominator
  WITH xml:lang             3,172,492   59.68%
  WITHOUT xml:lang          2,143,255   40.32%   <- becomes our `und`

terms with dcterms:language 3,172,492   59.68%
  ...also carrying xml:lang 3,172,492  100.00%
  ...with NO xml:lang               0   <- ZERO recoverable
```

Its values (`<http://vocab.getty.edu/language/zh>` mirroring `@zh`) are a
**redundant restatement of the same fact in URI form**. ✅ **Only language
identification can move this population** — the conclusion §0 already reaches,
now closed rather than assumed.

✅ **And Getty does NOT positively assert "undetermined"** — four matches across
45.3M triples. Our `und` is our own placeholder for an omitted tag, exactly as
`tgn-places.py:266` documents. **Confirms the plan's framing; does not correct
it.**

✅ **`gvp:historicFlag` — 22,225 terms (0.42%): 22,198 `historic`, 27
`currentAndHistoric`.** Directly usable as a **positive** marker for the
historic-orthography fine-tune, and against a pack whose *effective* N is 3,565
places, 22,198 Getty-attested historic terms is not small. ⚠ **But the dump
publishes only the two non-current values**, where Getty's documented model has
Current/Historical/Both/Unknown — so **absence of the flag does NOT mean
"current", it means not published.** Usable to find historic forms; **unusable**
to identify current ones.

➡ **`gvp:termFlag` — 4,058,205 terms (76.34%), of which 4,058,187 `Vernacular`.**
Getty tells us which term is the native-language form, and we discard it.
Vernacular-vs-not is the axis a Welsh-clerk-orthography fine-tune cares about.
**Recorded as available, not scheduled** — what it buys needs measuring.

🛑 **A DATA-QUALITY DEFECT: WE INDEX ADMINISTRATIVE CODES AS TOPONYMS.**
`gvp:termKind` on 902,219 terms (16.97%) — mostly `OfficialName` (896,278), but
also **FIPSCode 4,764 · ISOalpha3 246 · ISOalpha2 240 · ISOnumeric3 240 ·
USPSCode 50 · SiteName 365 · Pseudonym 18**. ~5,540 terms that are *codes, not
names* — `US`, `840`, `CA` — currently searchable as place names. ⚠ Unlike the
notation cases these are **cleanly identifiable from the source, because Getty
labels them**: a filter with an authoritative predicate behind it, not a
heuristic. Belongs in the toponym-hygiene issue.

🛑 **TRANSLITERATION IS MARKED BY GETTY — AND IT IS NOT IN OUR CORPUS.**

⚠ **This entry first said "we already capture it, since we take the whole tag."
That was WRONG** — read off the extractor and not checked against the store.
`indexing-04` withdrew it on measurement:

```
lang   total in store   LATIN-script rows   of which ok
zh          1,587,205                   0             0
ja            949,609                   0             0
fa            624,686                   0             0
ru          1,100,241                   0             0
ar            508,397                   0             0
el            192,873                   0             0
ko            323,097                   0             0
```

**Zero — not `no_route`, ABSENT.** Corpus-wide only **24,614 of 72,703,552**
rows (0.034%) carry any script subtag, and all are Wikidata-shaped
(`be:word_stress` 18,540, `ja_rm` 3,249, `zh_pinyin` 38). Getty's
`zh-Latn-pinyin-x-notone` **appears nowhere**.

✅ **The "absent" finding is sound, confirmed by a second route.** A pinyin row
would have surfaced in exactly that query: `rebuild_toponyms_index:867`/`:900`
splits the tag to its base, so `Beijing@zh-Latn-pinyin-x-notone` becomes
`Beijing@zh` with `lang_variant` beside it; and **script is derived from the
CHARACTERS** (`:1066`), the docstring at `:577` naming *"Beijing with lang=zh and
script=LATIN"* as a case it handles. So the row would read `zh` + LATIN — which
is what was queried, and found zero of.

⚠ **The routing hypothesis raised here was structurally right and empirically
moot.** `normalise_lang` is `lang.strip().split("-")[0].lower()`, so the script
subtag *is* discarded before the `(lang, script)` lookup — **but no rows reach
that lookup.** They are lost earlier than the router.

### ✅ FOUND — A DELIBERATE FILTER DROPS THEM, and the zero was the clue

`rebuild_toponyms_index.py:570` `is_script_mismatch`, called at `:893`. Its
docstring names this exact case as something to remove:

```
Examples of mismatches we want to filter:
- "Beijing" with lang=zh and script=LATIN (should be 北京)
- "Moskva" with lang=ru and script=LATIN (should be Москва)
```

`lang_base = lang.lower().split('-')[0]` → `zh-Latn-pinyin-x-notone` becomes
`zh`; LATIN is not in `LANG_EXPECTED_SCRIPTS['zh']`; returns True; `continue`.
**Every Getty pinyin form is skipped by design.**

⚠ **That is why the count is exactly ZERO rather than merely small.** A leaky
pipeline gives a trickle; a filter gives a zero. **The absoluteness of the result
was the diagnostic**, and it is worth keeping as a habit: an exact zero over a
population that should be large is evidence of a *rule*, not of attrition.

🛑 **The base-split destroys the evidence that would exempt them.** `-Latn` is
Getty **declaring** the romanisation; the filter discards that subtag and then
rejects the row *for being romanised*. It is `lang_variant`-blind at precisely
the point where the variant would justify the script.

### ✅ MEASURED — 8 of 8, with a positive control that turns it into mechanism

🛑 **The skip log does NOT survive, and that is a separate defect.** I proposed
querying `skipped_toponyms` — but `rebuild_toponyms_index:2150` builds into a
`tempfile.TemporaryDirectory`, so the table is written, used, and **destroyed
with the run**. `/ix1/ishi/data/toponyms.duckdb` does not exist (controlled: the
directory is readable, mode 775, 19 entries, no `.duckdb` at all). **A filter
discarding ~1.16M rows leaves no durable record of what it dropped.** That is
worth fixing independently of the romanisation question.

✅ **`indexing-04` used a natural control instead, and it is better than the skip
log would have been.** The hypothesis predicts *exactly which* languages are
zeroed: those in `LANG_EXPECTED_SCRIPTS` whose set excludes LATIN.

```
lang  in dict  expected scripts             store LATIN rows
zh    yes      CJK                                         0
ja    yes      CJK, HIRAGANA, KATAKANA                     0
ko    yes      HANGUL, CJK                                 0
ru    yes      CYRILLIC                                    0
el    yes      GREEK                                       0
ar    yes      ARABIC                                      0
fa    yes      ARABIC                                      0
bo    NO       (absent from the dict)                  8,431
```

🛑 **`bo` is the positive control that makes this mechanism rather than
correlation.** Getty publishes 7,904 `bo-Latn` forms and **they survived** —
purely because Tibetan is not one of the **34** languages enumerated. Had the cause
been anything upstream (ingest, dedup, normalisation), Tibetan romanisations
would have died with the Chinese ones. **They did not.**

### 🛑 THE FILTER IS ALREADY INCONSISTENT — which is what makes the exemption principled

The rule as implemented is not *"we filter romanisations"*. It is **"we filter
romanisations of the 34 languages someone enumerated"** — Tibetan, Ethiopic,
Khmer, Lao, Myanmar and Sinhala all keep theirs **by omission rather than by
decision**. Nothing about data quality, attestation, or the names themselves
separates `bo-Latn` from `zh-Latn-pinyin`.

✅ **And there is exactly ONE Latin exception in the dict — which is stronger
than a precedent.** `'sr': {Script.CYRILLIC, Script.LATIN}  # Serbian uses both`
is **1 of 34**, the only entry naming LATIN at all. So it is unambiguously an
**ad-hoc concession, not a policy**. The author already accepted that some
languages' Latin forms are legitimate and hand-maintained a single special case
for it. ⚠ **The exemption therefore does not generalise a policy — it REPLACES a
lone hardcoded special case with the data's own declaration.** Anyone defending
the status quo has to explain why Serbian earns by hand what Getty states
explicitly in the tag.

### THE FIX IS A NAMED EXEMPTION, NOT A REMOVAL

**Exempt from `is_script_mismatch` any form whose language tag explicitly
declares its script.** Where the tag says `-Latn`, the romanisation is
**asserted, not erroneous** — and `sr`, the dict's *only* LATIN entry, shows the
principle was already conceded for one language by hand, just never read from the
data. The filter stays correct for its real purpose — an
*unmarked* `Beijing@zh` genuinely is a data error that would pollute the "what
does Chinese look like" signal — and Getty's marked forms stop being collateral.
**This is what would deliver the attested cross-script pairs to the selector.**

⚠ **TWO NUMBERS THAT MUST NOT BE QUOTED**, both from an intermediate reading:

* **"48.5% of Getty's terms never reached the corpus" compares a DEDUPLICATED
  count to a RAW one.** Toponyms are globally deduplicated — one row, many
  `attestations[]` — so 5,315,747 *terms* mapping to 2,737,570 *distinct*
  `name@lang` ids is largely **dedup, not loss** (`San José@es` is one row across
  hundreds of places). The difference is not a loss rate.
* **"tgn's toponym count should approach 5.3M" therefore cannot happen**, dedup
  alone forbids it — so anyone testing that prediction would see it fall short
  and wrongly conclude the fix failed. **The sound test is `zh` + LATIN becoming
  non-zero**: binary, dedup-immune, and currently exactly 0. ⚠ **It will move
  only if the FILTER changes** — a places re-ingest cannot touch it, because
  `rebuild_toponyms_index` reads the staged tree and never passes through
  `extract_namespace` at all. **The ingest gap and the romanisation loss are TWO
  mechanisms, both live, independent of each other.**

⚠ **BOTH THINGS ARE TRUE AT ONCE.** The live index *is* still pre-fix — 1,277,683
tgn places with no toponyms at all (42.72%), exactly #246's figure — and that is
real and 9c's to act on. **But it is not the mechanism for the romanisations**,
which would still be filtered after that gap closes.

**Ruled out along the way, by measurement:** store enumeration (ipa store holds
72,703,552 of the index's 72,703,777 rows — **99.99969% reach**, so the zero is
about the corpus and not the planner) and normalisation in general (**932,739**
toponyms corpus-wide carry a working `lang_variant`), with the toponyms index
agreeing with the store at the reader.

**And eliminated from the code; do not re-propose:**

* 🛑 **NOT the empty-language drop.** `zh-Latn-pinyin-x-notone` is a *non-empty*
  tag, so `extract_namespace` has no reason to discard it.
* 🛑 **NOT dedup collapse.** `tgn-places.py` `make_doc` dedups on
  `toponym_id = f"{name}@{lang}"`, and the romanised and native forms are
  *different strings* — they cannot collide.
* 🛑 **NOT predicate selection.** `VALID_LABEL_PREDS = ("prefLabelGVP",
  "altLabel", "prefLabel")` covers the SKOS-XL links Getty uses, so a romanised
  term attached as `altLabel` **is** read, and `make_doc` applies no per-place
  cap.

🛑 **CONSEQUENCE FOR THE DESIGN DECISION.** Getty's ~1.16M attested romanisations
are **not currently available to the pair selector** — so they are not free, as
this entry first claimed. But they are **not an unbounded recovery task either**:
the cause is a single named filter with a small, defensible exemption. ⚠ **Do
not schedule against them until the exemption is written and measured**, and do
not treat the anyascii option below as superseded — it reaches the 18.5M
`no_lang` population, which this exemption does not.

**What Getty publishes (in the export, per the census):**
There is **no script-bearing predicate anywhere** in Terms — script stays
derivable only from the string or from the tag. But Getty encodes romanisation
**in the language tag itself** via `-Latn` subtags, and since the extractor takes
the whole tag we already hold them:

```
zh-Latn-pinyin-x-notone  632,401      el-Latn   56,193
zh-Latn                  231,563      ru-Latn   30,405
fa-Latn                  128,277      ar-Latn   11,338
ja-Latn                   63,919      bo-Latn    7,904
                                      ~1.16M total
```

🛑 **These are Getty-ATTESTED native↔romanised pairs on the same subject** — and
**we do not have them.** See the withdrawal above: recovering them is a task, not
a lookup.

⚠ **One thing to check before treating them as free:** `normalise_lang` presumably
bases `zh-Latn-pinyin-x-notone` to `zh`, which is then looked up as
`('zh','LATIN')` — and routes are keyed by the script a language is *normally*
written in. **If `('zh','LATIN')` is not a route, ~630k explicitly-marked pinyin
forms are landing `no_route` for a reason unrelated to being unroutable.**
Unverified; do not quote as established.

### A SECOND OPINION IN THE FILTER — worth measuring, and one candidate is free

Two things follow from the above, and neither is a recommendation to act yet.

**1. The threshold has never been measured, and it is doing real work.** Within a
place, co-attested names are either phonetic variants/transliterations or
genuinely different names (exonyms, renames). PanPhon separates them. ⚠ **So
"loosen it" is NOT safe advice** — loosen too far and the model learns
Uluru ≈ Ayers Rock, destroying the metric it exists to provide. The open question
is empirical and unasked: **at each threshold, how many true phonetic variants
are excluded, and how many exonyms admitted?**

**0. A better one exists in principle — but we do not currently hold it.**
Getty's ~1.16M `-Latn` forms are **attested rather than computed**, so they would
be gold labels rather than a heuristic. ⚠ **They are absent from our corpus**
(see the withdrawal above), so they are a **recovery task of unknown cost**. Do
not schedule against them, and do not treat the option below as superseded.

**2. There is one genuinely independent second opinion available, and it needs no
IPA.** **anyascii romanisation + edit distance** — the benchmark's own baseline
(AUC 0.9002). It is independent of PanPhon, independent of v7, and critically
**requires neither a language tag nor an IPA transcription**, so it can propose
positives for precisely the rows PanPhon can never reach: the 18.5M `no_lang`
population and the 1.4M tgn `und` rows. A pair passing **either** filter would
widen eligibility without adding circularity.

⚠ **Its bias is known and must be stated with it:** `romanised_baseline_measures_provenance`
— **35% of CJK↔Latin positives romanise to identical strings**, so edit distance
is a near-oracle in that stratum and weak in non-Latin↔non-Latin. That is
tolerable in a **union** second filter and would be disqualifying if it were
allowed to dominate. **Any trial must stratify Latin-involving from
non-Latin↔non-Latin before averaging anything.**

### 🛑 THE LSJBOT CLEANUP CLAIM IS WITHDRAWN — and the truth is worse

This section claimed the quarantine removed contamination from v8's training
data. **It removes nothing v7 ever learned from.**

```
lang    v7 ipa       v8 ok        v8 quarantined
ceb            0            0        2,786,505    QUARANTINED
mul            0            0          209,667    QUARANTINED
war            0            0          161,194    QUARANTINED
vo             0            0          141,241    QUARANTINED
min            0            0          112,829    QUARANTINED
sv     1,715,947    1,825,107                0    NOT quarantined
```

**v7 had zero IPA for all five quarantined languages** — they were never routed,
so they contributed nothing to v7's training data. The quarantine **formalises a
gap that already existed**; it does not clean anything.

🛑 **And `sv` is the sharper half.** It is *not* quarantined, and v8 carries
**1,825,107 against v7's 1,715,947** — **~109k MORE Swedish**. If `sv` carries
Lsjbot contamination, **v8's training data is more contaminated in that stratum,
not less.** The instinct to separate cleanup from gain was right; the
measurement says **the cleanup has not happened**, and the correct restatement is
*"prevents future contamination in five never-routed languages; `sv` remains
uncleaned and grows."*

✅ **`ja`+CJK — RECONCILED, and the decomposition is exact.** Not competing
measurements: **`465,177` is a proper subset of `+591,550`, and 78.6% of it.**

```
ja by script     v7        v8        delta
CJK               0     465,177   +465,177   <- the hole, closed entire
HIRAGANA     47,533     149,167   +101,634
KATAKANA    310,410     335,158    +24,748
total       357,943     949,609   +591,559 population
                        949,493   +591,550 ok-based (116 rows no_route/echoed)
```

**v7 had ZERO CJK-script `ja`** — no kanji-written Japanese name carried IPA at
all — and the whole population is now routed. Both figures carry their own
denominator; neither may be quoted as the other.

🛑 **"Closed the CJK hole" ships QUALIFIED — and the untagged half is the LARGER
one.** v7's CJK-script strata were `zh` 1,306,961 · `wuu` 48,883 · `gan` 37,097 ·
`yue` 31,345 · `ko` 2,060 **and nothing else**, against 2,973,525 CJK-script rows
in v7's corpus. So **~1.5M CJK-script rows had no IPA for want of a language**,
and **1,036,998 still do**. **The hole that closed was the `ja`-tagged part.**
The untagged part is the same `no_lang` wall — *"closed the CJK hole" is true
only of rows that had a language tag to close it with.*

### Leg 2 — the objective. Unchanged, unchallenged, and now the sharpest.

Phase-1 validation loss **0.0056** against a triplet margin of **0.3**: almost
every triplet contributes no gradient. **Nothing measured since has touched
this**, and unlike leg 3 it is a property of the training procedure rather than
of any corpus — so it does not move when the corpus does. It is the one leg that
says v8 must be *a different training run*, not merely the same run over better
data.

### Leg 3 — the geometry. Demoted from motivation to constraint.

Two things demoted it, and neither refutes it:

1. 🛑 **The PanPhon rank is corpus-conditional to a degree that makes a single
   number meaningless.** Re-measured at scale: **4.37**, **3.12** and **7.247**
   are three *different measurements over three different corpora*, not three
   estimates of one quantity. Holding script constant while changing corpus
   moves rank **1.71×** — as much as changing script does. **Any PanPhon rank
   must travel with the corpus it was measured on**, and "effective rank 4.37 of
   192" as a bare property of PanPhon is not a claim this document can make.
2. **Its remedy is already taken.** §10 retires the pooled 192-d vector outright.
   A finding whose fix is already decided constrains what v8 may be built from;
   it no longer argues for building it.

**What survives, and it is the part that matters:** the *student's* 10.8 of 128
was measured on the production corpus and reproduced **three times** (11.067 in
the third, against a known 10.83). That is a real property of the shipped model.
The constraint it imposes: **v8's teacher must not be the pooled 192-d PanPhon
vector**, whatever else it is.

### 🛑 WHAT THIS CHANGES ABOUT THE ACCEPTANCE GATE

The gate was **"geometry gate + no regression on discrimination"**. That was
derived from leg 3 being primary. With leg 1 primary it is **insufficient — it
cannot see the thing v8 is now mainly for.**

⚠ **A corpus-average cannot detect the input-data gain**, because the gain is
not uniform: it is concentrated in languages that previously had *no route at
all*. Averaged across a corpus where most strata are unchanged, +18.6M
disappears. This campaign has already been bitten by exactly this
(`romanised_baseline_measures_provenance`: 35% of CJK↔Latin positives romanise
to identical strings, making edit-distance a near-oracle in one stratum and
nonsense overall).

**The gate must therefore be stratified**, with three strata reported separately
and never summed:

* **GAIN** — the twenty new-route languages plus the improved ones, listed
  above. *This is where leg 1 either shows up or does not.*

  🛑 **The v7 figure for this stratum is a FLOOR, not a baseline, and must be
  LABELLED AS ONE IN THE TABLE — not in a footnote.** (Raised by `rewt-c7`,
  6 Sep.) A v7 AUC over pairs whose languages had **zero** IPA at v7 training is
  measuring a model **on inputs it was never given a signal for**. That is not
  "v7's discrimination on this stratum" in the sense the UNCHANGED figure is.
  ⚠ **Reported in one table under one caption, the two will be read as
  commensurable however carefully the prose hedges** — a caveat has to travel
  with the number, and a footnote does not.
* **CONTAMINATION RISK** — 🛑 **replaces the CLEANUP stratum, which was wrong on
  both halves.** `sv` is not quarantined and v8 trains on ~109k **more** of it,
  so this stratum is watched for **degradation**, not expected to improve. A
  flat result is a pass; an improvement is a surprise that needs explaining.
* **UNCHANGED** — everything else. **This is where "no regression" is tested**,
  and it is the only stratum where the old gate was ever measuring what it
  claimed.

### ✅ MEASURED 6 Sep — the per-stratum v7 baseline, and it BREAKS the gate it was built for

`indexing-8b`, commit `4160977`; AP alongside each figure at
`/vast/ishi/ipa-v8/logs/stratified_v7_baseline.json`. Existing corpus reused, no
rebuild.

✅ **Both corpus controls reproduce EXACTLY** — v7 **0.9324** and
`levenshtein_romanised` **0.9002** against the recorded §8 figures. An unplanned
control on the whole pipeline, and the reason the strata beside them are
trustworthy.

```
                                   N      v7     lev   v7 margin
CORPUS                       148,410  0.9324  0.9002    +0.0322
gain_v7zero        [FLOOR]    16,164  0.9560  0.9031    +0.0529
  / latin_involving           14,154  0.9555  0.9058    +0.0497
  / non_latin_both             2,010  0.9592  0.8912    +0.0680
gain_improved      [FLOOR]    60,672  0.9212  0.8771    +0.0441
  / latin_involving           52,042  0.9302  0.8900    +0.0402
  / non_latin_both             8,630  0.8594  0.8008    +0.0586   <- THE CELL
contamination (sv)             2,054  0.9749  0.8619    +0.1130
unchanged                     65,538  0.9348  0.9164    +0.0184
excluded_quarantined           3,982  0.9735  0.9594    +0.0141
```

### 🛑 v7 SCORES **HIGHER** ON THE LANGUAGES IT HAD NO IPA FOR — so the GAIN gate cannot see what v8 adds

**0.9560 on `gain_v7zero`, against 0.9348 on `unchanged`.** v7 discriminates
*better* on the twenty languages it had **zero** IPA for than on languages it had
IPA for.

**The mechanism, and it is not a paradox.** Those toponyms were never in a
positive pair — `generator.py:155` gates on `ipa IS NOT NULL` — so v7 **never
trained on them at all**. 0.9560 is therefore **pure orthographic
generalisation**: `ca`, `gl`, `ast`, `oc`, `vec` are Latin-script Romance
languages whose pairs look like the Spanish and French pairs v7 *did* train on.
⚠ **Its advantage there cannot be phonetic knowledge of those languages — it had
none.**

🛑 **THEREFORE A GATE ASKING "DID v8 IMPROVE `GAIN`?" IS INSENSITIVE TO WHAT v8
ADDS.** It asks for a rise from 0.9560 with 0.0440 of room, on a metric driven by
surface similarity rather than phonology. **v8 could deliver exactly the
improvement it was built for and this gate would not show it.**

⚠ **This is the gate-inherits-the-finding-order failure in a new place.** The
stratification was right about **WHERE** to look and wrong about **WHAT** to look
at. Stratifying by *which languages gained IPA* is not the same as stratifying by
*where IPA is the operative signal*, and only measurement separated them.

### ✅ THE GATE'S PRIMARY CELL IS `gain_improved / non_latin_both` — 0.8594

Lowest in the table, **0.1406 of headroom**, and consistent with §8.3 placing
v7's weakness in CJK↔Latin. But headroom is the weaker argument. **The principled
one: it is the stratum where orthographic transfer CANNOT help.** Between two
non-Latin scripts there is no shared surface for a character model to exploit, so
**phonology is the only available mechanism** — which makes it the one cell where
an IPA improvement must show if it is real. It is also where the input actually
moved: `ja` 357,943 → 949,493 (**2.65×**), `zh` +276,524.

**Choose the stratum where the mechanism v8 improves is the ONLY mechanism
available** — not merely the weakest cell.

### ⚠ AND THE CONTAMINATION STRATUM IS THE MOST INTERESTING CELL IN THE TABLE

`sv` shows v7's **largest margin over the baseline anywhere**: **+0.1130**,
against +0.0184 for `unchanged`. **Whatever v7 learned about Swedish is doing
real work that edit distance cannot replicate.** v8 trains on ~109k *more*
Swedish, **94.4% of it Wikidata labels whose tag records a wiki edition rather
than the name.**

🛑 **So the stratum flagged for degradation is the one with the most to lose, and
what is at risk is a DEMONSTRATED capability rather than a hypothetical one.**
⚠ **Gate it on the MARGIN, not the absolute.** 0.9749 → 0.9600 reads as a 1.5%
slip; the same movement as a margin is **0.1130 → 0.0981, a 13% loss of the
model's entire advantage.** The margin makes the loss legible; the absolute hides
it.

### ⚠ FEASIBILITY LIMIT, stated rather than worked around

**8 of the 20 `v7zero` languages hold under 400 pairs** — `gl` 138, `vec` 207,
`ast` 224, `oc` 222, `sk` 247, `nn` 294, `cy` 295, `sl` 326, `eu` 325. The
**stratum aggregate is sound at 16,164**; per-*language* AUCs for those eight are
not, and the code reports `N` and **`INSUFFICIENT`** rather than publishing a
number.

✅ **Targeted augmentation DECLINED (coordinator, 6 Sep).** It would require a
corpus rebuild, which runs against production; the gate operates at stratum
level, where N is ample; and per-language figures for eight small languages are
not needed for any decision v8 has to make.

✅ **Method notes worth keeping.** `quarantined` takes **precedence** in stratum
assignment, so a `ceb`↔`en` pair is excluded rather than credited to `en`'s gain
— `excluded_quarantined` scores **0.9735**, so folding it anywhere would have
inflated that stratum. Latin-involving and non-Latin↔non-Latin are never
averaged. Uncovered pairs are **excluded, not zeroed** — an earlier version
coerced a `None` score to `0.0`, **scoring a pair the baseline declined to answer
as maximally dissimilar**; caught and fixed before the reported run.

⚠ **The quarantined set is OUT of the gate entirely** — never routed in either
generation, so it can neither gain nor regress, and including it would credit
v8 with a 3.4M block it does not touch.

Plus the two the old gate had, retained: the geometry gate, and no regression on
discrimination (v7: AUC 0.9324 vs 0.9002, separated interval). **Retrieval
remains explicitly not a win condition** — §8.2's `n^−0.22` density scaling
suggests it may not be available at 72.7M by any means.

### 🛑 A PRE-FLIGHT GATE THAT DID NOT EXIST, AND WOULD HAVE CAUGHT A LIVE FAULT

**Assert IPA coverage as the TRAINING PIPELINE sees it, not as the store reports
it, before any training run starts.**

This is not hypothetical hygiene. As of today the 49,749,377 strings are real,
audited, and **reachable by nothing**: they live in a separate store, while both
training consumers `SELECT t.ipa` from the toponyms DuckDB where the column is
NULL across all 72,703,552 rows (§9e). **A v8 run started today would train on
0% IPA and report success.** Every check that certified the recomputation was a
check on the writer.

### 🛑 THE TRAINING CORPUS IS NOT THE CORPUS THE GATEWAY SERVES

Newly established, and it invalidates a natural assumption rather than any
existing measurement. `rebuild_toponyms_index` **never reads the `places`
index** — `_staged_namespace_source` (`:705`) walks `final/` → `h3_merged/` →
`boundary_merged/` → `extract/` and reads the staged file directly. So:

* `places` is ES-mediated and passes through the `extract_namespace` pipeline,
  which **discards any toponym with an empty language tag**.
* `toponyms` — and therefore the training corpus — is staged-direct and
  **unfiltered**.

⚠ **Anything measured on `places` does not describe what v8 trains on.** tgn
alone carries **59.9%** untagged toponyms (Getty publishes untagged terms
routinely; tgn was the only one of 28 namespaces doing so, now fixed at the
authority with `lang or "und"`). Those forms are *in the training corpus and
absent from the served index*.

### ⚠ SEQUENCING — the corpus moves before v8 trains, and it moves under the fine-tune

The `lang or "und"` fix does not *add* names to the inventory: `Dorkecestre@`
and `Dorkecestre@und` are **different `toponym_id`s**, so it **re-keys the
majority of tgn's toponyms**, each arriving as a new row with no embedding and
no IPA. (Absolute count owed by `indexing-9c`; 59.9% is of tgn's toponyms, and
must not be back-derived from the 2,991,143 *place* count.)

### 🛑 THE tgn FIX MAKES 1,398,790 ROWS PRESENT BUT NOT TRANSCRIBABLE

Measured by `indexing-9c`: **1,398,790 net-new inventory rows** — distinct
`toponym_id`s re-keyed `Name@` → `Name@und`, **zero colliding** with an existing
`@und` id (tgn's tagged terms carry real codes and never `und`, so nothing
merges and the delta cannot be sized down). ⚠ 40.1% here vs the 59.9% reported
earlier is **not** a contradiction: 59.9% is per-document toponyms, 40.1% is
**distinct ids**, and a name on many places is one inventory row. The distinct
figure is the one that sizes the top-up.

🛑 **But `und` does not route** — confirmed empirically by `indexing-8b` running
the router rather than reading it: `resolve('und', script)` returns a terminal
status in **all twenty scripts the corpus uses**; `und-Latn`, `und-Arab`,
`und-Cyrl` and `und-Hans` are all absent from the 218 installed modes. ✅ **With
positive controls in the same run** — `en`+LATIN → `eng-Latn`, `ca`+LATIN →
`cat-Latn`, `ja`+CJK → `jpn` — so the router is not simply refusing everything,
which is what makes the `und` result mean anything. **All 1,398,790 get an
inventory row and an embedding (~30 s of L40S at 49k/s) and no IPA.**

### ✅ AND `no_route` WAS THE WRONG BUCKET — a terminal status is a QUEUE LABEL

🛑 **This document briefly forecast a 2.6× jump in `no_route`. That was wrong**,
and the fix (`6c76e0b`) is better than the forecast it replaces. The two terminal
statuses name **different future work**:

```
no_lang     the queue for LANGUAGE IDENTIFICATION
no_route    the queue for ADDING A G2P BACKEND
```

`und` is ISO 639-2 for **undetermined** — semantically identical to an empty tag,
and **no Epitran mode will ever be written for it.** Filing 1.4M rows under
`no_route` puts them in a backend queue no backend can serve, and takes them out
of the LID queue where they belong. `UNDETERMINED_TAGS = {und, mis, zxx}` now
resolve to `no_lang`; `mul` stays quarantined for its own separate reason.

```
as traced        no_route     866,948 → ~2,265,738   (2.61×)
as shipped       no_lang   18,543,146 → ~19,941,936  (+7.5%)
                 no_route     866,948 → UNCHANGED
```

⚠ **The point is not tidiness.** The prose conclusion was *"these 1.4M join the
`no_lang` wall rather than crossing it"* — **with the right bucket the census
says that in numbers**, instead of needing a footnote to stop someone reading a
2.6× jump as damage. A +7.5% movement in the largest existing bucket needs no
defending. **File a terminal status by which future work could fix it, not by
where the code happened to fall through.**

✅ **32 tests pass, including the negative control that keeps it honest**: a
*real but unsupported* language (`sw`, no installed mode) must **still** be
`no_route` — without it the change would have collapsed the two buckets into one.

🛑 **Nobody should add an `und-Latn` Epitran mode to make these route.** That
manufactures a transcription from a **declared absence** of information — the
same move as interpolating the Danube, and the standing prohibition covers it.

**The fix is not wrong** — it stops `extract_namespace` discarding them, makes
them searchable, and gets them embeddings, none of which was true yesterday.
**It moves them from "silently dropped" to "present but untranscribable."**
Crossing the rest needs a real language tag: the language-identification project,
not a routing fix.

🛑 **And because IPA gates pair ELIGIBILITY, they cannot train the main
objective.** `generator.py:155` is `WHERE t.ipa IS NOT NULL`, so these 1.4M are
**ineligible**, not merely unlabelled. **Rebuild-and-index does not make them
trainable.** §6.2c's decision to harvest the tgn pairs from the **staged
extract** rather than from ES is what saves the fine-tune — ⚠ **keep it**;
harvesting from the inventory or ES would now silently lose exactly this
population.

🛑 **The forms being re-keyed are exactly the historic-orthography signal the
fine-tune depends on** — `Dorkecestre`, `Dorocine` → `tgn:7011929` are Getty's
untagged historic variants. **Do not train the fine-tune against the inventory
until the tgn rebuild has landed**, or it trains on the half of the signal that
happened to carry a tag. Required order: clean `final/` → index → **inventory
rebuild** → IPA top-up → backfill → train.

### 🛑 SETTLED: the historic-orthography target is a FINE-TUNE, not a second objective

It was added as a co-equal second target on 5 Sep. **The data does not support
that**, and this document is the reason it was oversold:

```
Welsh LHPN     14,863 pairs    (the 5.5% yield figure was 1.73% corpus-wide)
TGN dated      40,937 pairs    (effective N 3,565 places; 17 places = 51%)
Chinese             0          specialist review cancelled; 1908 Atlas dropped
                               ⚠ but see below — ~102,675 Wade-Giles terms may
                               be recoverable by #250's filter exemption
```

**Against a v7 trained on ~31M toponyms this is a fine-tune and an evaluation
stratum — not a co-equal training objective.** ⚠ **And it is EUROPEAN** — Welsh
clerk transliteration and dated European variants — **unless the Wade-Giles
population below proves out.** **Any v8 claim must say which historic
orthography, and at what scale it was trained.**

### ⚠ THE CHINESE HALF MAY HAVE A ROUTE AFTER ALL — pending one measurement

`indexing-04`, 6 Sep, sampling `explicit.zip` (2026-01-04) directly rather than
the staged tree. **The population the filter discards is TWO conventions, not
one:**

```
zh-Latn-pinyin-x-notone   1,264,794 solid      8 hyphenated  -> pinyin, as tagged
zh-Latn (bare)              257,776 solid  205,350 hyphenated -> TWO conventions
```

(Raw lines; each term appears under both `ontology#term` and `literalForm`, so
roughly **102,675 distinct** hyphenated terms.)

**The hyphenated forms are Wade-Giles** — `Hsü-jih-t'un`,
`Pai-chia-ts'ao-fang-tzu`, `Ch'en-chia-wo-p'u`, `Kao-chia-ying-tzu`. Aspiration
apostrophes, the `hs` digraph, and `-t'un` (屯), `-ts'un` (村), `-kou` (溝),
`-tzu` (子) suffixes. ✅ **Village and hamlet granularity.** Postal *province*
forms are in the release too — `Chihli`, `Kiangsu`, `Chekiang`, `Fukien`,
`Hunan`, `Honan`, `Shantung`, `Shansi` — so the head is there as well as the
tail.

🛑 **#245 WAS CLOSED not-planned ON THE PREMISE THAT THIS MATERIAL WAS
UNAVAILABLE.** It measured Chinese reachability at **18.0%** against a British
control of **76.0%**, identified the gap as *"our corpus is county-level, and
nothing suggests the tail's postal forms are indexed — that tail is the gap"*,
and proposed OCRing the 1908 Atlas for ~4,255 index entries. **The tail it wanted
appears to be sitting in TGN at ~102,675 terms — roughly 24× the Atlas — already
structured and attested rather than needing recognition from page images.**

✅ **This REINFORCES dropping the 1908 Atlas rather than reviving it.** More
material, attested, no OCR.

⚠ **WHAT IS NOT ESTABLISHED, and must not be skipped over.** #245 measured
*postal* 0.786 and *pinyin* 0.571 similarity against its 1856 forms.
**Wade-Giles was never measured.** It sits between the two historically, and that
licenses **no** figure. Nobody has re-run the reachability probe against a corpus
containing these forms. **So: the material exists, is the right granularity, is
attested, and is discarded. Whether it closes the 18%-vs-76% gap is an OPEN
MEASUREMENT, not a conclusion.**

✅ **The test is cheap and already built** — #245 left `process/probe_reachability.py`
and `process/probe_qing_provinces.py`, the second asserting a baseline that fails
loudly if it moves. **Re-run both after #250's exemption lands.** One filter fix
covers both populations, since bare `zh-Latn` and `zh-Latn-pinyin-x-notone` alike
declare `-Latn` in the tag.

⚠ **One trap for whoever measures it: the TAG DOES NOT SEPARATE THE TWO
CONVENTIONS.** Bare `zh-Latn` holds pinyin *and* Wade-Giles mixed. For training
that is harmless — both are romanisations. But **any Wade-Giles-specific claim
needs an orthographic classifier** (hyphenation + apostrophes), which is a
**heuristic** and must be characterised before numbers are quoted from it.

**#245 has NOT been reopened** — that is the closer's call, and `indexing-04`
flagged the caveat on the issue rather than acting on a connection it had not
fully measured. **The right call.**

### What is still open

* 🛑 **THE LARGEST BLOCK IS NOT ROUTING FAILURE — IT IS MISSING LANGUAGE TAGS,
  and v8 does NOT address it.** Named here as an explicit absence so no reader
  credits v8's gain with having touched it:

  ```
  ok                 49,749,377   68.43%
  no_lang            18,543,146   25.51%   <- ALL ok = 0
  quarantined         3,411,436    4.69%
  no_route              866,948    1.19%
  non_language_tag      126,394    0.17%
  echoed_input            6,240    0.01%
  empty_output               11    0.00%
  ```

  By script within `no_lang`: **LATIN 16,211,998 · CJK 1,036,998 · CYRILLIC
  640,825 · ARABIC 332,828 · HANGUL 107,038.** ⚠ Those 1.04M CJK rows are why
  "closed the CJK hole" must ship qualified. **Language identification is a
  bigger lever than any remaining backend work** — and country-based inference
  is **closed by arithmetic** (§9c: every script where it looked accurate is one
  where a constant beats it). A separate project, not a refinement.
* **D-B, a Japanese reading table**, dismissed in §6 as "not cross-script" under
  a scoping decision since superseded. §9b measured that **36.1%** of sampled
  kanji-bearing places already carry the kana reading by co-attestation.
* The **IPA→PanPhon** half: the pooled 192-d vector is **retired** (leg 3's
  constraint); per-segment features remain conditional on whether a teacher
  survives the v8 design.

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
rebuild_toponyms_index.py   → toponyms + IPA + PanPhon192   (reads STAGED, not ES)
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

⚠ **DEMOTED 6 Sep — read §0 leg 3 before this section.** The measurements below
stand; their *status* does not. The PanPhon rank is corpus-conditional (4.37,
3.12 and 7.247 are three measurements over three corpora, not three estimates of
one number, and corpus moves it 1.71×), and the remedy — retiring the pooled
192-d vector — is already taken in §10. **This finding now CONSTRAINS v8's
architecture rather than motivating the retrain.** What survives intact is the
*student's* 10.8 of 128, measured on the production corpus and reproduced three
times.

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

The chain is causal: an input representation of very low rank → a teacher fitted
to it → a student distilled to that teacher (phase-2 student–teacher cosine
plateaus at 0.9418) → a phase-3 objective too weak to expand it. **The 128-d
embedding is a rank-≈10 embedding in a 128-d costume.**

### ✅ RE-MEASURED AT SCALE (6 Sep) — PanPhon is **3.12**, not 4.37, and the spectra are OPPOSITE shapes

`indexing-8b`, `9e5dda9`, using the **shipped** `IPAConverter.to_embedding` and the
**shipped** `evaluation.geometry.measure_geometry` — reimplementing either would
have made the control meaningless.

✅ **Control first: 299,524 v7 student embeddings return 11.067 against the known
10.83** — a third reproduction, at a scale between the two prior ones, sampled **by
id hash rather than `LIMIT`** because the parquet is ordered and a head sample
measures the head. *The script exits without computing any PanPhon number if this
fails.*

```
effective rank of 192      3,000      3.130
                          29,768      3.104
                         299,998      3.121
                       2,999,994      3.122     <- flat to +/-0.5% across 1000x
```

🛑 **4.37 does not reproduce, and the discrepancy is NOT scale** — at the same
n the measurement is 3.130. **Finding 2's direction is confirmed**: the input is
*more* collapsed than recorded on this corpus.

⚠ **Sample size: the original was 6,000, not 3,000.** This session's brief said
3,000, `indexing-8b` took it on trust and propagated it into its finding document,
commit message and docstring before either of us opened line 252, which says
**"Measured on 6,000 distinct real toponyms"**. Corrected on both sides.

### 🛑 BUT THE MEHDIE HYPOTHESIS IS REFUTED — AND THE REFUTATION IS THE REAL FINDING

The guess above was that 4.37 came from the MEHDIE testsets. **Tested directly,
not argued from the Arabic stratum.** One detail matches almost too well — the five
testsets hold **6,013 distinct titles** against the plan's "6,000" — but:

```
MEHDIE all       6,013    7.247
MEHDIE Arabic    3,280    7.196
MEHDIE Hebrew    2,654    5.726
plan's figure    6,000    4.37
corpus-wide      3k-3M    3.12
```

**MEHDIE would have recorded ~7.2. So 4.37's provenance remains unknown, and no
code in the repository computes it.**

🛑 **AND THE TEST QUALIFIES THE FLATNESS RESULT.** Holding **script** constant and
changing only the **corpus** moves the rank as much as changing script does:

```
ARABIC   corpus 4.198  vs  MEHDIE 7.196   1.71x
HEBREW   corpus 3.753  vs  MEHDIE 5.726   1.53x
                          (against 1.55x across ALL scripts within the store)
```

> **"Flat at 3.12 across three orders of magnitude" was stability across sample
> SIZE within ONE population — not robustness of the quantity.** Two 6,000-scale
> samples of real toponyms differ by **2.3×**.

🛑 **4.37, 3.12 and 7.25 are not competing estimates of one number. They are three
different measurements, and any PanPhon rank must travel with its corpus.** That is
a stronger and more restrictive claim than *"retire 4.37 in favour of 3.12"*.

### ✅ The redundancy hypothesis is refuted too — `indexing-8b`'s own, on three grounds

It had offered, untested, that the index's lower rank reflects near-duplicate
redundancy against MEHDIE's curated distinct places. SG asked for it to be tested.

* **The mechanism is impossible in its naive form.** Participation ratio is
  **invariant to uniform replication** — duplicating every vector *k* times scales
  every eigenvalue by *k* and leaves the normalised spectrum unchanged. *Checked
  rather than trusted*: tripling every row of a 19,812-vector sample moved the rank
  by **Δ = −0.000000**. *"There are duplicates"* can never lower a rank.
* **The premise is false and backwards.** Index 300,000 rows / **0.309%**
  duplicates; MEHDIE 6,013 rows / **2.661%**. **MEHDIE is 8.6× MORE duplicated.**
* **The intervention does nothing.** Exact then near-dedup leaves every stratum
  where it was, and where it moves anything it moves it **down**.

✅ **Estimator control: isotropic Gaussian returns 190.15 of 192**, so it does
report near-maximal rank on genuinely full-rank data.

⚠ **No replacement mechanism is proposed, deliberately.** *"I offered one story from
plausibility and it was wrong in premise, mechanism and prediction; a second
untested story would repeat the error rather than correct it."* **What survives is
that the corpus dependence is real, is not redundancy, and is a property of what
the index CONTAINS rather than how often it repeats it.**

### 🛑 The two low ranks are DIFFERENT OBJECTS — and §3's language must not migrate

```
sigma_i / sigma_1        v7 student    PanPhon192
s5                          0.739         0.182
s10                         0.651         0.131
s20                         0.0059        0.103
s100                        0.0058        0.037
variance in top 1           0.153         0.562
variance in top 20          0.9995        0.790
```

* **The student is DIMENSIONAL COLLAPSE** — ~10 real directions then a cliff;
  components 20–128 carry **0.05%** between them.
* **PanPhon is DOMINANCE** — **one** direction carrying **56.2%** of all variance,
  then a genuine long tail; components 20–192 still carry **21%**.

🛑 **So "~85% of the index's storage is spent on dimensions that carry nothing" is
a claim about the STUDENT and survives untouched. The equivalent claim CANNOT be
made about PanPhon, and that sentence must not migrate.** A participation ratio of
3.12 with a 56% first component is not *"worse than"* 11.07 with a 15% first
component — **it is a different geometry.**

### ⚠ And "the student inherits it" is now too strong

**The student's effective rank (11.07) EXCEEDS its input representation's (3.12).**
So the input does not impose a rank ceiling that the student merely inherits — the
student *expands* past it and then hits a cliff that PanPhon does not have.

🛑 **The cliff is therefore at least partly ARCHITECTURAL, not inherited** — which
is consistent with the weight ranks already in the table below
(`pooling.attention.0` **4.54**, `output_proj.3` **7.08**, σ_last/σ₁ = 6 × 10⁻⁷).
**Design consequence for v8: fixing the input representation alone may not fix the
output.** The retrain's geometry target has to address the projection stack, not
only what feeds it.

### By script (300k each) — the averaging artefact is ruled out

```
ARABIC 4.198 · non-LATIN 3.380 · CYRILLIC 3.076 · LATIN 3.053 · CJK 2.703
```

**A 1.55× spread, so geometry genuinely differs by script — but every stratum is
in the same regime**, and `LATIN` (80.5% of rows carrying IPA) is within 2% of the
corpus figure.

⚠ **Only effective rank and the spectrum are reported**, because
`measure_geometry`'s neighbourhood statistics (`nn1`/`nn200`/`nn_gap`) are **not
comparable across sample sizes** — its own docstring says so. Rank and spectrum are
exact at every size.

Consequence for search: 72.7M items packed into ~10 effective dimensions must
be dense, which is why `Marsails → مارساليس` (0.9878, genuine) sits *below* a junk
ceiling of 0.9881.

⚠ **CORRECTED 6 Sep — the neighbour claim was CITED, never measured, and its
universal form is false.** This document repeated the repo's note that *"the 200
nearest neighbours of anything sit above 0.93"*. Measured directly against
production (40 random toponyms carrying an embedding, k=200, ES cosine score
de-normalised as `cos = 2·score − 1`):

```
              n    min      p10      median   p90      max
1st (self)   40   1.0000   1.0000   1.0000   1.0000   1.0000
10th         40   0.9397   0.9504   0.9768   0.9973   0.9989
200th        40   0.9025   0.9151   0.9495   0.9600   0.9785

200th neighbour above cosine 0.93:  34 of 40  (85%)
```

**The density is real — the median 200th neighbour is 0.9495 — but "of anything"
is wrong: 15% of queries have their 200th neighbour BELOW 0.93, as low as
0.9025.** ⚠ n=40, so this refines the claim rather than replacing it with a
precise one. **The argument survives** (a rank-≈10 space is extremely dense, and
that is what defeats a global threshold); **the universal quantifier does not.** **The documented conclusion that "no cosine threshold
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

✅ **PROMOTED 6 Sep — this is now v8's sharpest leg (§0 leg 2).** Nothing
measured since has touched it, and unlike finding 2 it is a property of the
training procedure rather than of any corpus, so it does not move when the
corpus does. It is the one finding that says v8 must be *a different training
run* and not merely the same run over better data.

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

🛑 **The Chinese side of the historic-orthography target has NO acquisition route
at all** — not a slow one, none. The specialist review was the only mechanism for
1856 → modern.

⚠ **The 1908 Atlas is NOT a substitute and has been dropped from this plan
(SG, 6 Sep).** It supplies **postal → modern**, which is the *other* rung, and
only 10.9% of the 1856 headwords reach a postal form by exact match at all —
a figure itself selected on the cases where the transformation was already
trivial. Its value was overstated when it was offered.

### ⚠ What v8's second target now rests on

**The loss falls on the Chinese half only.** v8's target was *historic
orthography*, and two legs remain, **neither requiring a specialist**:

| leg | what it gives | status |
|---|---|---|
| **LHPN Welsh ↔ English** | ~~the volume~~ — **14,863 pairs, harvested and counted** (§6.2b) | ✅ acquired; **the 5.5% yield was 3.2× too high** |
| **TGN dated variants** (§6.8) | **labelled** European historic forms — `Dorchestre`/`Dorcic`/`Dorkecestre`. ⚠ effective N **3,565 places**, not 40,937 pairs | ✅ **harvest from the STAGED EXTRACT, not ES** — see below |

🛑 **So v8's historic-orthography target is EUROPEAN.** Welsh clerk
transliteration and dated European name variants — **not** Chinese transcription,
and not nineteenth-century gazetteer orthography.

⚠ **That distinction must survive into the paper.** Any v8 claim about historic
orthography must say **which** historic orthography, or it will be read as
covering exactly the case it does not.

---

## 6.2b LHPN HARVESTED — 14,863 pairs, and the yield figure was 3.2× too high

`indexing-04`, all 14 counties, **nothing truncated**.

### 🛑 The denominator correction — exactly the failure predicted

```
total recorded-name rows      673,468
rows carrying a HeadName       11,649   =  1.73%   corpus-wide
Anglesey alone                          =  5.54%   3.2x the corpus rate
```

**July's 5.5% came from Anglesey plus a `q=llan` search.** Anglesey is a strongly
Welsh-speaking county and is not representative. **A convenience sample overstated
the density threefold** — the shape of claim this plan has retracted four times,
predicted in advance this once, and confirmed.

### What is actually there

```
distinct head-names                    2,022
head-names with >=2 normalised forms   1,369  (67.7%)  <- only these can pair

route                             unique pairs   substring-shortcut
A  within-head-name variants            13,475            7.4%
B  Prif Enw (cy) vs HeadName (en)          455           59.1%
C  Field co-location (vernacular)          968           15.5%
UNION after dedup                       14,863            9.5%
```

**90.5% pass on real edit distance rather than the substring shortcut.**

⚠ **The substring share is reported separately at every level for a reason:**
`phonetic_similarity` returns a **flat 0.85 for ANY substring match**, so trivial
suffix variants clear the bar **by rule rather than by phonetic content**. And the
gate for these is **0.6, not 0.35** — 0.35 is the *cross-script* threshold, and
Welsh/English are both Latin.

✅ **Route B was nearly missed and is not in any brief:** `Prif Enw` (Welsh) and
`HeadName` (English) differ on **41%** of head-name rows — `Cilgwrrwg`/`Kilgwrrwg`,
`Blaenafon`/`Blaenavon`, `Llandeilo`/`Llandilo`. Free structured cy/en pairs
needing no clustering. The gate correctly rejects the 238 that are *translations*
(`Y Drenewydd`/`Newtown`, 0.200).

✅ **Route C is qualitatively distinct and may matter more than its 968.** A and B
are settlement names dominated by `llan-`/`aber-`; **C is Welsh vernacular field
names** — `Weirglodd`/`Werglodd`, `Ffridd Ddu`/`Ffrydd ddu`, `Cae Gaseg`/`Cae'r
Gaseg`. Vowel orthography, `i`~`y`, article elision, `c`~`k`. **For phonetic
coverage that diversity is worth more than the count.**

### 🛑 CONSEQUENCE FOR v8: the second target is a FINE-TUNE, not a co-equal objective

**The historic-orthography target now rests on ~14,863 Welsh pairs and ~40,937 TGN
pairs** — and the TGN figure is itself heavily clustered (17 places produce 51% of
it; effective N is 3,565 places). Against a v7 trained on ~31 M toponyms, **this is
not "the volume leg". It was called that on the strength of the 5.5% figure, by
this document, and that figure was wrong.**

**Re-scope honestly:** enough for a **targeted fine-tune with oversampling** and for
an **evaluation stratum**; **not enough to make historic orthography a co-equal
training objective with cross-script matching.** ⚠ Any v8 claim must say which
historic orthography *and* at what scale it was trained.

### Two corrections to this session's brief, both of which would have cost the work

🛑 **1. `headnamesearchtocsv` does NOT return variants.** Its 23 columns contain no
variant field. **Variants live in `recordednamesearchtocsv`**, grouped by the
`HeadName` column. This session stated the wrong structure — over-reading
`place#161`'s summary — and building on it would have harvested the wrong endpoint
and found no pairs there.

🛑 **2. The cap is 50,000, NOT 4,000, and it is SILENT.** **Seven of fourteen
counties returned exactly 50,000 rows on the first pass**, and Carmarthenshire was
hiding **29%** of its Field records behind it. A yield computed from that would
have been a biased lower bound — *the same convenience-sample failure in a new
costume*, caught only by checking row counts against the cap.

✅ **No throttle workaround was needed.** Place-type selection (`pt=1,2,4,5`, then
`pt=3`) brought all but four counties under the cap; those four used parish-level
subdivision via `/mapdata/GetParishJson/?countyGuid=`. **497 parish requests,
serial, 4 s apart, User-Agent naming WHG with a contact, backoff on any non-200. No
errors, no retries.**

**Data at `/vast/ishi/lhpn`** (560 MB, 220 GB free), **outside any git repo**, with
a `LICENCE-NOTICE.txt` recording that acquisition and training use are cleared on
SG's verbal clearance, that **publication and redistribution are NOT**, and that it
must never be committed anywhere a release could reach.

⚠ **Process note, and the habit is right:** `indexing-04` did **not** act on this
session's relay that SG had authorised the work. It put it to him directly, because
`place#161`'s own Phase 0 says acquire nothing without written licensing **and the
route needs a forged `Referer`** — a detail this session did not know and which
would have mattered. **That is the second time today a relayed authorisation was
verified rather than acted on, and the first time one was wrong.**

## 6.2c HARVEST THE TGN PAIRS FROM THE STAGED EXTRACT, NOT FROM ES

**SG's suggestion, and it is better than the ES route this document specified.**
The `tgn` re-extract ran 6 Sep and its artefacts carry **both halves in one
record**:

```
staged/tgn/extract/places.jsonl        5.2 GB   06:17
staged/tgn/h3_merged/places.parquet    399 MB   07:26
staged/tgn/final/places.parquet        <- ccode_merge, in flight
```

**Shape, verified:** `toponyms[]` entries are `{toponym_id, timespans}` with the
name carried in the id —

```
{"toponym_id": "Dorcic@",                "timespans": [{"start":{"in":600},"end":{"in":1000}}]}
{"toponym_id": "Dorchecestre@",          "timespans": [{"start":{"latest":2026},"end":{"earliest":2026}}]}
{"toponym_id": "Dorchester@en",          "timespans": [{"start":{"latest":2026},"end":{"earliest":2026}}]}
```

**A real date is distinguishable from the `attested_at(2026)` placeholder by the
sentinel.** `tgn:7011929` carries **9 toponyms** here against the **one**
(`Dorchester`) the live `places` index holds.

### Why this beats the ES route

1. 🛑 **The ES route cannot reach the names.** `places.toponyms[]` is empty for
   **42.7%** of TGN and holds only modern forms for the rest — that *is* item 4.
   The extract has the full term inventory **before** any of it depends on the
   production write landing.
2. **One source instead of a join.** The ES route needed the `toponyms` **index**
   (names) joined to `temporal_patch.jsonl` (dates). Here name and date sit in the
   same record.
3. ✅ **The extract is RICHER than the patch.** The patch keys spans by name into
   `places` and covers **9,450 concepts**; the extract carries **16,384 dated
   terms** across the release. **The patch was a workaround for the ES route's
   limitation, and inherits it.**
4. **No 3M-document read against a production cluster serving search.**
5. **Independent of the write.** The pairs are available whether or not the
   `tgn` production ingest has landed, which decouples the v8 training input from
   `place#246`'s schedule entirely.

⚠ **Prefer `final/places.parquet` once `ccode_merge` completes** — `final/` is the
stage the indexer reads, so harvesting from it means the training input and the
indexed corpus came from the same artefact. Harvesting from `extract/` or
`h3_merged/` instead would be sound but would not carry that guarantee.

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

## 6.9 Operational work lives in the ISSUES, not here — trimmed 6 Sep

**~1,700 lines of `place#246` / `place#247` narrative were removed from this
document.** It was carrying both the *instructions* and the *story*, and the
instructions had moved to GitHub. **The issues are authoritative; this plan is
about Symphonym v8.**

| where it went | what it holds |
|---|---|
| [`place#246`](https://github.com/WorldHistoricalGazetteer/place/issues/246) **OPEN** | date-ingestion defects across authority scripts, **and the retiling of `tgn` / `osm` / `osm_misc`** that must follow. Items 1–3 fixed in code; 4 and 5 need a `tgn` re-ingest on staging. Rewritten 6 Sep as one self-contained issue with its comments deleted. |
| [`place#247`](https://github.com/WorldHistoricalGazetteer/place/issues/247) **CLOSED** | snapshot fidelity, the staging/production divergence, the forcemerge question. Closed because the divergence is confined to **scoring** while #246's audit reads **content**. |
| `place#161` / `#163` | RCAHMW LHPN licensing and the Welsh pair findings. |

**The generalisations worth keeping are in §11 and §12**; the operational detail
is not reproduced. ⚠ **A closed issue is still readable** — `place#247` retains
the tombstone/BM25 analysis, the verification harness inventory at
`/vast/ishi/verification/`, and the `/vast` watermark constraint.

⚠ **One production finding is NOT in either issue and has no owner:** a wedged
`/ix1` **blocks ES boot** — ES makes three sequential blocking filesystem calls on
`path.repo` at startup, `/ix1` is hard-mounted, and only changing `path.repo`
helps (unregistering the repo does not; a *fresh* cluster with no repo registered
blocks identically). Established by prediction — the 30 s case was predicted at
105 s before it ran and returned 105 s. **`/ix1` wedged twice on 5 September; the
only reason this has not bitten is that production has not restarted since 31
August.** Probe re-runs in ~5 minutes from `/vast/ishi/pathrepo-probe/`.

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

## 9b. THREE G2P FINDINGS THAT CHANGE v8's OWN PLAN (indexing-8b, `069ce95`)

Found while scoping the IPA recomputation. **All three bear on v8 directly, not
merely on the recompute.** Writeup: `developer/finding-charsiu-g2p-defects.md`.

### 🛑 1. `ja` + CJK has NO G2P route — and CharsiuG2P is not the fix

**465,177 Japanese Kanji toponyms return `None` from `to_ipa`.** `ja` is mapped
only for HIRAGANA and KATAKANA; `ja`+CJK falls to the Epitran default branch,
finds no entry, and returns nothing. `CHARSIU_LANG_MAP` already carries
`'ja': 'jpn'` commented as a fallback that `to_ipa` never reaches. Verified
through the shipped function — 12 of 12 Kanji names `None`, **with the Katakana
path as a positive control in the same run**, so `None` is the routing decision
and not a broken probe.

⚠ **DO NOT record this as "Japanese fixed".** Routing to CharsiuG2P replaces a
total absence with **partial accuracy**: `千代田町渡瀬` returns
`seɴdaitamatɕiɰᵝatase` where `千代田` is *Chiyoda*; `厳原町` (*Izuhara*) returns
`geɴgeɴtɕoɯ`. **Place-name Kanji readings are not derivable from the
characters.** Zero signal → partly-wrong signal is an improvement and is not a
reading dictionary.

🛑 **So D-B — a Japanese kanji reading table — is the ACTUAL fix, and §6's
dismissal of it needs revisiting.** It was ruled out as "not cross-script", on a
scoping decision since superseded. §8.3 measured CJK↔Latin as the stratum where
the romanised baseline is a near-oracle *by artefact* and v7 looks worst, and
`東京`~`Tokyo` reaches only 0.51 because anyascii returns the *Mandarin* reading.
**The hole and the weakness are in the same place.**

### ✅ …AND THE READING TABLE IS PARTLY DERIVABLE FROM OUR OWN CORPUS

**No CharsiuG2P update can fix the Kanji problem — but D-B may not need building
from scratch either.** Measured against production:

```
lang=ja  script=CJK        465,177
lang=ja  script=KATAKANA   335,158
lang=ja  script=HIRAGANA   149,167
lang=ja  script=LATIN            0

sampled 400 kanji toponyms -> 671 distinct place_ids
   of those places, 242 (36.1%) ALSO carry a KANA toponym
```

**The kana form attested to the same place IS the reading:**

```
字権現台      -> ごんげんだい            osm:n9166359786
下本郷町      -> しもほんごうちょう        osm:n8882910734
大字笠戸島     -> おおじかさどじま         osm:w131009536
南六条十丁目   -> みなみ6じょう10ちょうめ    osm:n8653797201
```

🛑 **So D-B is a CO-ATTESTATION harvest for at least a third of the population,
not a hand-built dictionary** — the same mechanism §6.5 established for historic
forms, applied to a different problem.

⚠ **Three caveats, none of which the 36.1% expresses.** It is a **sample of 400
kanji toponyms → 671 places**, indicative and not corpus-wide — the exact shape of
claim this document keeps having to retract, so treat it as a reason to measure
properly rather than as the measurement. **Not every pair is a Japanese reading of
a Japanese place**: `済州島 → チェジュ島` is Jeju, a Japanese rendering of a Korean
name — still a valid phonetic pair, but a different phenomenon from `下本郷町 →
しもほんごうちょう`, and a naive harvest conflates them. And **the remaining ~64%
would still need an external source** — Japanese morphological dictionaries
(UniDic, SudachiDict) carry place-name readings, which is a separate and larger
question.

### 🛑 2. ByT5 truncation — every Charsiu route silently capped at ~13–15 IPA chars

`_CharsiuWrapper` calls `model.generate(**inputs)` with **no length argument**.
HF defaults `max_length` to **20 TOKENS**, and the tokenizer is **ByT5 —
byte-level**. IPA is heavily multi-byte, so the cap is ~13–15 characters
*regardless of input length*. Measured, same model and inputs, 60 real names per
route:

```
yue+CJK    80.0% truncated        ko+HANGUL  71.7%
ja+CJK     33.3%                  zh+CJK     16.7%
```

`澎湖列島` → `pʰa:ŋ˨˩wu:˨˩li` — two syllables gone. **2,026,765 corpus rows sit on
these routes.**

⚠ **It never surfaced because a truncated IPA string is WELL-FORMED**, and nothing
compared output length to input length. Every truncation lands at 13–15
characters, which is a token budget rather than a property of the inputs.

### 🛑 3. `sv` contamination is INSIDE the existing 50.4% baseline

Wikidata carries one label per **Wikipedia edition**, and Lsjbot mass-generated
place articles worldwide. So a `ceb` or `sv` label is frequently not a
Cebuano or Swedish place name at all:

```
lang  toponyms    from wd   name also under another lang
ceb  2,786,505     98.4%    82.6%
sv   1,825,578     94.4%    82.7%   <- ALREADY ROUTED TODAY
```

⚠ **`sv` is not in the unlockable set — it is inside the 50.4% already routed, so
any v7 training data drawn from it carries this.** `ceb`/`war`/`min`/`vo`/`mul`
are quarantined by default (recorded, never dropped, reversible), taking the
verified unlockable from 15.8M to **~13.0M**.

⚠ **This is the one judgement call in the package a reviewer might make
differently.** A shared name is not by itself proof — *Paris* is legitimately
multilingual. The inference rests on the **conjunction** of ~98% `wd` provenance,
95–99% name-sharing, and languages with no plausible worldwide toponymic
footprint.

### ✅ And the capability was already built — nobody routed to it

`phonetics/epitran_extensions/` holds **115 hand-built CSVs**;
`scripts/install_epitran_extensions.sh` installs them; **they ARE installed on
CRC** — 254 modes across 203 ISO-639-3 codes against a **45-entry**
`EPITRAN_LANG_MAP`. **The 21.76% gap is not missing capability, it is capability
nobody routed to.** All 215 unlockable cells verified against real corpus names,
0 failures; preflight 218/218 modes load and do not echo.

⚠ **The end-to-end caught what 15 green unit tests could not.** Deriving routes
from Epitran's CSV directory **dropped ENGLISH** — Epitran implements `eng` in
*code* via flite/`lex_lookup`, so there is no `eng-Latn.csv`. Pass 1 reported
120 of 120 English rows as `no_route`: **the single largest cell in the corpus,
silently.** The unit tests were green throughout because they used a *synthetic*
mode set. The regression test now omits `eng-Latn` deliberately, so it can only
pass through the fix.

## 9d. ✅ IPA RECOMPUTED — 31.1M → 49.7M strings, and two near-misses worth more

🛑 **"From 0.00%" IS THE WRONG BASELINE AND THIS DOCUMENT USED IT.** v7 was
**demonstrably trained with IPA** — `coverage_stats.json` records **31,113,585**
strings, and its own provenance line (`from_db_cache: 31,113,562`) says that run
*inherited* them from a store that held them. **The 0.00% describes what survived
on disk before this run, not what v7 had.** The current DuckDBs are the
`temporal-20260731` generation and carry `ipa` NULL throughout, so the strings
were lost between v7's training and now.

**The honest comparison, with both denominators stated because the corpora
differ:**

```
v7 training   31,113,585 IPA   = 54.02% of 57,593,810 in-training-namespace
                               = 46.49% of 66,924,548 total corpus
now           49,749,377 IPA   = 68.43% of 72,703,552 total corpus
gain          +18,635,792 strings over what v7 actually trained on
```

**So the run RESTORED a capability that had been lost and EXTENDED it** — it did
not create one from nothing. ⚠ And the two percentages are not directly
comparable: 54.02% is of a *training-namespace subset* of a *smaller* corpus.

`indexing-8b`. Store at `/vast/ishi/ipa-v8/store/ipa.duckdb`.

```
rows in store        72,703,552
carrying IPA         49,749,377   68.428%   (v7 trained on 31,113,585)

ok           49,749,377      no_route            866,948
no_lang      18,543,146      non_language_tag    126,394
quarantined   3,411,436      echoed_input          6,240
                             empty_output             11
```

**4,015 of 4,015 shards present**, so the merge's completeness gate *passed* rather
than being waived. 333 array tasks, zero tracebacks. `/vast/ishi` ended at 219 GB
free.

✅ **The `ja`+CJK hole is closed** — 465,177 of 465,177 now carry IPA, against 12 of
12 returning `None` from the shipped helper that morning. **Quarantine held**: `ceb`
0 ok of 2,786,505. **English routed**: 10,270,813 of 10,271,605. ⚠ **Every audit
check carries a positive control in the same query**, so none of those zeros can
come from an empty predicate.

**Throughput, for costing:** Epitran 17k–44k names/s across CSV modes, `eng-Latn`
882/s through flite, CharsiuG2P 26.4/s on CPU against ~150/s on an L40S. **The
neural backend was 5% of the rows and ~90% of the cost** — which is what justified
splitting the GPU array out.

### 🛑 A passing test that would have gone green either way

`verify_store` asserted CharsiuG2P `max_len > 20` and got **255**. **That passes.**
But 256 was the new `max_new_tokens`, so **255 is one below the ceiling** — and
*"the maximum went up"* was the ByT5 bug's entire disguise.

> **A threshold test written against a bug you have just fixed tends to encode the
> bug's OLD signature.** It tested *"not 20-ish"* when it should have tested *"not
> at the current ceiling"*. The second form survives the next cap change; the first
> silently stops testing anything.

**Investigated rather than accepted**, and the answer is *not* truncation — 1,043
rows (0.0419%) sit at ≥250 bytes and are **autoregressive repetition loops on
out-of-distribution input**:

```
zh  'deɴneɴkakioɯɾiɴçikːokɯɯɴdoɯkaideɴkeisaidaɴɕidaɴɕidaɴɕidaɴɕid'
ko  'ɾjʌ̹nɦa̠ɡje̞o̞ɭʎimpʰik̚sʰa̠ikxɯɭna̠md͡ʑa̠na̠md͡ʑa̠na̠md͡ʑa̠n'
```

**A higher cap yields longer garbage, not better IPA.** The Epitran control has no
generation cap at all and reaches 343 bytes with 1,510 rows over 250, so genuinely
long IPA exists and 256 is real but rarely binding. ⚠ **Recorded rather than
fixed — those 1,043 rows still carry `status='ok'`. A known residual, not a clean
result.**

### 🛑 And a near-miss that nearly took PRODUCTION READ-ONLY

The stratified job's first attempt **spilled 198.5 GiB and took `/vast` to 86 GB
free — about 35 GB from putting production ES read-only** — because its `LIMIT`
sat *after* the join, so sampling reduced nothing. Rewritten to sample *before*
joining, with an explicit `max_temp_directory_size`.

⚠ **`/vast/ishi` is a 1 TB allocation shared with production ES, which goes
read-only at ~51 GB free.** A query plan is a disk-consumption decision on this
cluster, and a `LIMIT` in the wrong position is enough to make it one.

## 9c. LANGUAGE INFERENCE FOR THE 18.5M NO-LANG ROWS — a negative held back for the right reason

`indexing-8b` built the (c) instrument — hide a known `lang`, infer it from the
attested place's ccode via CLDR likely-subtags, compare — and got a **decisive
negative it is NOT yet reporting as the answer.**

```
applicability   17,888,708 of 18,543,146 (96.47%) resolve to a single-country place
accuracy           643,083 of  1,985,006  =  32.40%
countries >=90%          0        >=95%: 0        >=99%: 0
FR 13.19%   ES 13.91%   IT 16.03%   IN 7.89%   DE 24.36%
```

🛑 **But the confusion table says the measurement may be on the wrong
population.** The dominant error is `true=en, inferred=<local language>`:

```
IN true=en -> hi  21,256      DE true=en -> de  15,360
ID true=en -> id  20,618      JP true=en -> ja  14,487
CN true=en -> za  16,282      FR true=en -> fr  11,111
```

**Those are English EXONYMS.** Inferring "German" for a German place is not wrong
about the *place* — it is wrong about a *label that was never local*. So the
labelled rows may not be a random sample of the unlabelled ones.

🛑 **BUT THE SUPPORTING FIGURE WAS WRONG, AND ITS CORRECTION REVERSES THE
ARGUMENT.** The claim was *"the no-lang population is 71.35% `osm`"*, with OSM's
`name` tag being the local endonym — so the instrument would be **understating**
accuracy. **71.35% was OSM's INTERNAL rate** (the share of `osm`'s own rows
lacking a lang), **not the composition of the no-lang set.** By distinct toponym:

```
gn    10,598,144      <- the largest contributor, not osm
osm    9,659,290
tgn    1,398,787
ohm      307,224
whg      206,992      (overlapping; a toponym can attest in several namespaces)
```

⚠ **GeoNames' primary `name` field is frequently the international/English form**,
which would make the no-lang set **MORE exonym-heavy, not less** — so the 32.40%
may be **OVERSTATING** accuracy rather than understating it. `indexing-8b`
retracted this against its own objection, unprompted.

⚠ **The mechanism belongs in `postmortem-ingestion-faults.md`:** `count(*)` over a
toponym ⋈ namespace join **is not a count of toponyms** — the join multiplies, by
about 2× here — so a **per-namespace RATE was read as a POPULATION SHARE**. A
correct computation over the wrong set; the same shape as
`label_stopped_describing_the_set`.

⚠ **Fourth instance of §8.3b either way** — a corpus property read as a method
property, this time inside the *validation* rather than the corpus. The direction
of the bias is now open, and the stratified job (11168563) decides it.

⚠ **And it cuts both ways: `US true=ceb → en`, 17,686 rows.** The Lsjbot labels are
inside the GROUND TRUTH, so part of "truth" is the contamination we quarantined.
Not yet separated. Job 11168562 re-runs stratified by namespace with
English-labelled rows held out.

### 🛑 A deeper limit on (c) that stratification cannot remove

Even a clean stratified result is an **upper bound on confidence, not a
validation**. **Labelledness is itself non-random**: a row has a `lang` because
something supplied one, and rows lacking one may differ systematically *within the
same namespace*. **An imputation cannot be fully validated on the labelled subset
when being labelled is the thing that differs.**

🛑 **It is worse than a caveat: it partly invalidates the check's DESIGN.** The
stratification was built to decide between *"32.40% is real"* and *"32.40% is a
selection artefact"*. **Neither reading is available from this instrument** —
stratifying by namespace controls for one confound and leaves the one that does
the damage. `indexing-8b`'s own objection was **unfalsifiable by its own method**,
and it recorded that rather than reporting "measured 32.40%, refined per
namespace".

⚠ **And the bound is ONE-SIDED IN AN UNKNOWN DIRECTION.** If labelled rows are
systematically more exonymic — an English label got supplied *because* an
international form existed — true accuracy on the unlabelled population is
**HIGHER** than measured. If labelledness instead tracks better-documented places,
whose names are also more likely to be locally attested, it is **LOWER**. *"I
cannot tell which from inside the corpus, and I am not going to argue for the
flattering one."*

### 🛑 The deeper problem: the metric scores AGREEMENT WITH A LABEL, not correctness

> **`済州島 → チェジュ島` may carry a stored `lang` of `ja`. Inferring `ja` then
> scores CORRECT — while being wrong about the name, which is Korean.**

**So every accuracy figure in §9c is agreement-with-stored-label, not
correctness-of-name**, and the two diverge exactly where the corpus is already
wrong. `indexing-8b`'s 11.45% KATAKANA therefore **also absorbs cases where the
stored lang is simply wrong**, unseparated — so it corroborates §9b's *mechanism*
and **must not be quoted as the SIZE of it**.

⚠ **This is the exonym problem in a second costume**, and it is the second
independent reason to treat these as bounds rather than measurements.

✅ **Consequence, and it is unconditional:** any inferred `lang` ships with its
**provenance flag and the measured error rate stamped beside it**, whatever the
accuracy turns out to be — because the accuracy itself cannot be fully validated.
*An unvalidatable number that travels with its own uncertainty is usable; a bare
one is not.*

⚠ **One more sample bias, self-caught:** the `eng-Latn` throughput benchmark of
882/s came from **the first 1,500 English names — short and common**. Real name
lengths cost more through `lex_lookup`, and English is now the long pole of the
run. A benchmark drawn from the head of a corpus measures the head.

## 🛑 CLOSED BY ARITHMETIC — country-based inference should not be used ANYWHERE

**`indexing-8b` added the one column that settles it: the MAJORITY-CLASS rate.**

```
script      no-lang rows   agreement   majority-class   inference − constant
LATIN         16,211,998      26.53%    23.38% (en)          +3.15
CJK            1,036,998      76.48%    71.87% (zh)          +4.61
CYRILLIC         640,825      36.28%    30.61% (ru)          +5.67
ARABIC           332,828      44.27%    32.56% (fa)         +11.71
HANGUL           107,038      50.69%    99.88% (ko)         -49.19
THAI              37,988      91.23%    99.91% (th)          -8.68
GREEK             33,988      65.48%    91.15% (el)         -25.67
KATAKANA          22,521      11.45%    99.87% (ja)         -88.42
ARMENIAN           3,909      48.01%    96.28% (hy)         -48.27
HIRAGANA           4,536      99.40%    99.85% (ja)          -0.45
```

> 🛑 **EVERY SCRIPT WHERE INFERENCE LOOKED GOOD IS ONE WHERE A CONSTANT BEATS IT.**
> Where it is accurate, a constant is *more* accurate. Where it beats a constant,
> it is *not* accurate (26–44%). **So it should not be used anywhere.**

**HIRAGANA 99.40% is not skill** — 99.85% of hiragana toponyms are labelled `ja`,
so CLDR inference is fractionally **worse** than saying `ja` every time. Not
near-tautological: **worse than tautological.**

### 🛑 The rule this yields, and it is the most transferable finding of the campaign

> **Any accuracy figure needs the MAJORITY-CLASS RATE beside it, or you cannot
> tell skill from class imbalance.**

⚠ **Fourth instance of §8.3b, and the second inside `indexing-8b`'s own document.**
The script spread was reported twice as *"the most useful thing here"* and as
surviving all three caveats. **It did survive them** — and was still measuring
**script mono-nationality** rather than inference skill. *The caveats were about
the LABEL; this one is about the BASELINE, and no amount of thinking about label
quality would have surfaced it.*

### ✅ A smaller rule that DOES survive — and it reverses the katakana reading

**The majority-class column is itself the finding:** for mono-national scripts,
*"assign the script's modal language"* is 99%+ correct. **And for choosing a G2P
BACKEND that is the right question — whose phonology should read this string, not
whose name it originally was.**

🛑 **Katakana is the clean case and it inverts §9b's and §9c's earlier reading.** A
katakana toponym is often a *foreign* name — but **Japanese phonology is still what
should read it**, so `ja` is correct *for this purpose* even where it is wrong
about origin. **The 11.45% was scoring it against the wrong question.**

Scripts at ≥95% modal coverage (HANGUL, THAI, KATAKANA, HIRAGANA, ARMENIAN,
GUJARATI, TAMIL, MALAYALAM, KANNADA, TELUGU) cover **177,318 no-lang rows** =
0.956% of no-lang, **0.244% of corpus**. Coverage 68.428% → **68.672%**. Small,
safe, cheap. **Not implemented** — ships with a provenance column and its measured
rate if wanted.

### 🛑 The realistic headroom, and the number for SG

> **LATIN IS 87.43% OF ALL NO-LANG ROWS** (16,211,998 of 18,543,146).

**The 18.5M gap is a Latin-script problem, and no script- or country-conditioned
rule touches it.** Anything that moves it materially is **language identification
from the string itself** — a different project, not a refinement of this one.

### ~~The result that survives either way: accuracy is SCRIPT-dependent~~ — superseded above

```
HIRAGANA 99.40%   THAI 91.23%   CJK 76.48%   GREEK 65.48%   HANGUL 50.69%
ARABIC   44.27%   CYRILLIC 36.28%   LATIN 26.53%   KATAKANA 11.45%
```

**Effectively mono-national scripts infer almost perfectly; Latin — spread over a
hundred countries — is hopeless, and Latin is 16.2M of the 18.5M no-lang rows.**
That is the whole of the headline number. **So if any inference is defensible it
is script-first and narrow, not country-first and general.**

### 🛑 KATAKANA at 11.45% CAVEATS §9b's kanji→kana harvest

**Katakana is the Japanese script for FOREIGN loanwords**, so a katakana toponym
is disproportionately a *transliterated non-Japanese name* and the correct `lang`
is often not `ja` at all.

⚠ **That is the same phenomenon this document met from the other direction**: §9b
proposed harvesting kanji→kana pairs as Japanese readings, and flagged
`済州島 → チェジュ島` (Jeju) as a Japanese rendering of a Korean name rather than a
reading. **`indexing-8b` measured independently what that caveat guessed at.**
Two investigations, opposite directions, same finding — **the kana half of a
kanji/kana pair is not reliably a reading, and a harvest must separate readings
from transliterations rather than assume.**

### 🛑 9e. THE 49.7M IPA STRINGS ARE UNREACHABLE BY ANY CONSUMER, AND THE OBVIOUS FIX IS DESTROYED BY THE STEP THAT MUST FOLLOW IT

`indexing-8b` checked at the reader rather than the writer, and found the
recomputation succeeded into a place nothing reads. The strings live in
`/vast/ishi/ipa-v8/store/ipa.duckdb`, a **separate file**. Both training
consumers — `export_training_parquet` (`rebuild_toponyms_index.py:1181`) and
`dump_to_jsonl` (`:1528`) — `SELECT t.ipa` from the **toponyms** DuckDB, where
the column is NULL across all 72,703,552 rows.

**So the training pipeline today would still see 0% IPA coverage.** The 68.428%
is real, audited, and reachable by nothing. That is the fault class the
postmortem catalogues — *a producer that succeeded, verified at the writer* —
and it survived precisely because every check this campaign ran was a check on
the store.

⚠ **The obvious fix is correctly ordered only one way round, and the wrong way
round looks identical for a while.** `rebuild_toponyms_index` main
(`:2150-2175`) builds into scratch and ends `shutil.copy2(temp_db_path,
final_db_path)`; without `--resume` it starts from `create_db()`, where `ipa
VARCHAR` (`:618`) is created **empty** and filled only by the G2P stage's
`UPDATE toponyms SET ipa = u.ipa` (`:1717`). **A fresh rebuild copies a new file
over the old one — it does not merge.** And the rebuild is not optional: it is
the only way new `tgn` toponyms enter the inventory at all.

Backfilling before that rebuild therefore yields a DB that passes every check,
feeds training correctly, and **silently reverts to 0% IPA the moment the tgn
rebuild runs — with that run reporting success.** Required order:

1. ✅ **`tgn` re-ingest LANDED 6 Sep** — nameless tgn places **1,277,683 → 10**,
   `ok=2,991,143 errors=0` against an independent pre-scan, counts unchanged
   (51,187,900 corpus / 2,991,143 tgn) so no stale ids. See the filter-family
   note below.
2. ✅ **CACHE HYDRATION — ALREADY DONE, AND NEVER NEEDED.** The cache holds
   **73,657,964 rows, 100% under model_version 7 and the current checkpoint
   hash** — *more* rows than the 72.7M live toponyms. See below: the constraint
   was moot from the start.
3. `rebuild_toponyms_index` re-runs extract-to-DuckDB → **new** inventory
4. IPA top-up computes the toponyms that are new in that inventory
5. **then** backfill store → toponyms DuckDB, last

### 🛑 A STEP THAT MUST HAPPEN BEFORE THE REBUILD, AND WHOSE INPUT THE REBUILD DESTROYS

Raised by `indexing-9c`, 6 Sep. **The Symphonym embedding cache must be hydrated
from the live `toponyms` index.** `rebuild_toponyms_index` writes
`panphon_embedding` only — the 128-d Symphonym vectors come from the separate
stage 2 — so an existing index is the *only* place the existing vectors live.
Hydrating gives **~99% cache hits**; losing the source costs **~28h GPU** to
recompute what we already had.

### 🛑 AMENDED — THE DEADLINE IS THE DELETION, NOT THE REBUILD

⚠ **This section first said "the moment the rebuild lands the hydration source is
gone; there is no recovery step." That is FALSE once the rebuild targets a new
dated concrete index** — which the `ca07cfe` guard now requires. Verified in the
code rather than reasoned from the rebuild alone:

* `processing/hydrate_symphonym_cache.py` takes **`--index-pattern`** (default
  `toponyms_*`) and scrolls **whatever index it is pointed at**, in 5,000-row
  batches. It was never coupled to the alias.
* The rebuild's STEP 4 delete is guarded — `if es.indices.exists(index=args.toponyms_index)`
  (`:2404`). Against a **new dated name that does not exist yet**, the branch is
  not entered, **so the old index is untouched and survives.**

**So the binding constraint is "hydrate before the OLD INDEX IS DELETED", not
"before the rebuild".** That is materially different: the campaign has a
**recovery path** — hydrate late, from the still-present old index — rather than
a one-shot ordering with ~28h GPU as the penalty for mis-sequencing. **The ~28h
becomes real only if the old index is deleted while the cache is cold.** Hydrate
early regardless, because it is cheap and removes the question.

### ✅ AMENDED AGAIN — THE CONSTRAINT WAS MOOT ALL ALONG

🛑 **Third reading, and the last: the cache was ALREADY FULLY HYDRATED.**
`/vast/ishi/models/phonetic/symphonym_cache.duckdb` holds **73,657,964 rows,
100% under `model_version 7` and the current checkpoint hash** (`f2493fd6…` =
sha256 of `phase3_best.pt`; `final_model.pt` is byte-identical). More rows than
the live index has documents. **So the hazard was moot in a third way — not
"before the rebuild", not "before the deletion", but never binding at all.**

⚠ **`indexing-9c` found this by checking the cache BEFORE running the hydration
tool** — the only reason it did not become an unnecessary multi-hour scroll of
72.7M documents.

🛑 **THE LESSON, and it is now earned twice over.** The constraint went through
three readings:

1. *"before the rebuild"* — correct for the code as it stood that morning, when
   the rebuild's default target was the alias.
2. *"before the deletion"* — after `9c`'s own alias fix **changed the hazard out
   from under the constraint derived from it**. ⚠ **A constraint derived from a
   defect needs RE-DERIVING when the defect is fixed**, or the remedy outlives
   the disease.
3. *"never needed"* — because nobody asked **whether the thing being protected
   was already safe.**

⚠ **Two rounds of careful mechanism-reasoning about preserving a resource, and
the cheapest question — "is this already done?" — was asked third.** Check the
state before elaborating a constraint about preserving it.

### ✅ THE GPU COST OF THE REBUILD IS ~1h, NOT ~28h — and the reuse is proved, not sampled

The cache is keyed on `toponym_id`, and null-lang toponyms are keyed `name@`. The
`@und` fix makes them `name@und` — **a different key, so they miss**. For tgn:
**1,398,790 distinct `@und` ids, 0 under the new key, 1,398,779 under the old.**

✅ **The old vectors are CORRECT for the new key by construction.**
`hf/inference.py:390` `encode_lang` returns `LANG_UNK_ID` for `None`/`''`, else
`lang_to_id.get(...)` — and **`'und'` is absent from the 1,944-entry lang
vocabulary**, so it *also* returns `LANG_UNK_ID = 0`. All three inputs collapse
to one lang id, so the embedding **cannot** differ. A 10-name spot check
(Latin + CJK) agrees: 10/10 bit-identical, max|diff| 0.000000.

✅ **And `9c` declined to re-key the cache on the strength of that — the right
call.** ~35 min of GPU saved, against writing 1.4M rows into a shared 28.6 GB
cache that other pipelines read. **Recomputation is self-verifying; a re-key is
only as good as the reasoning behind it.**

**Planning number: tgn's 1.4M + #250's recovered romanisations ≈ 2.5M new ids ≈
~1h GPU**, everything else a cache hit. ⚠ Treat 2.5M as an **upper bound**:
toponyms are globally deduplicated, so any romanised form already present from
another namespace is not a new id.

### 🛑 THE `undscript` REBUILD WRITES DEFECTIVE IPA — bounded, but one artefact needs protecting

Job **11170354**, running 6 Sep. `indexing-8b` verified **in the file the job is
executing**, not inferred, that three known defects are live in its path — they
were fixed in `phonetics/ipa/backends.py` and `routes.py`, **different modules**
from `rebuild_toponyms_index.py`:

* `:260` is still `self.model.generate(**inputs)` with **no `max_new_tokens`** —
  the ByT5 20-byte-token truncation. Measured affected: **yue 80.0%, ko 71.7%,
  ja 33.3%, zh 16.7%.**
* `('ja', Script.CJK)` still absent from `EPITRAN_LANG_MAP` → **465,177 Kanji
  toponyms get `None`.**
* The map is still hand-written rather than derived from the 218 installed modes,
  topping out near **50% instead of 68.4%**.

✅ **Assessment: bounded and mostly reversible — let it finish.** It is a **new
generation** (`toponyms-undscript-20260906T160000Z.db`) with the old
`temporal-20260731` DB untouched; **stage-2 Symphonym embeddings are computed
from the NAME STRING, not from IPA**, so the ~1h of GPU is uncorrupted; the
structural point of the run (`@und` inventory, #250 romanisations) is unaffected;
the backfill was always going to overwrite `ipa`; and killing it mid-`shutil.copy2`
risks a **truncated** final file, which is worse than a complete defective one.
⚠ Note the DB on disk at 12:49 is the **STEP 1 checkpoint** — the file is copied
temp→final **twice** (`:2264`, then `:2327` after G2P).

🛑 **THE PART THAT NEEDS PROTECTING IS NOT THE IPA — IT IS `coverage_stats.json`
(`:2331`).** That artefact's staleness **already sent one brief out with a wrong
premise today**, and a replacement generated from defective IPA will look
authoritative *and* current, which is the worse failure. **Check whether `:2331`
writes to the same path as the 2026-05-01 file
(`/vast/ishi/elastic/zenodo/training_stats/coverage_stats.json`) that `04` used to
reconstruct v7's per-language baseline — if so it is the only surviving record and
must be copied aside first.** Either way the standing rule holds: **no IPA
coverage figure may come from anywhere but `8b`'s store.**

### 🛑 #250's FIX IS INERT AT THE CALL SITE — the function was repaired, the line above it was not

Found 6 Sep while checking a peer's evidence. **`2f093c4` added
`_has_script_subtag` and a `return False` guard INSIDE `is_script_mismatch`, and
changed NOTHING at the call site.** But the extraction loop base-splits first:

```
:905   lang = parts[0]                      ← subtag destroyed HERE
:933   if is_script_mismatch(lang, script): ← receives 'zh', never 'zh-Latn'
:934   mismatch_counts[f"{lang}:{script_value}"] += 1
```

So `_has_script_subtag('zh')` returns False and **the exemption is dead code.**
⚠ **This is the very defect the commit message diagnoses** — *"the distinguishing
information was thrown away on the line above the test"* — **the fix repaired the
test and left the line above it.**

🛑 **AND THE TESTS ARE GREEN.** `tests/test_script_mismatch_declared.py` calls
`is_script_mismatch("zh-Latn", …)` **directly**, with a full tag the production
path never delivers. A correct unit test of a function that is unreachable in
that state. **Any test for this must exercise the extraction loop, not the
function.**

### 🛑 AND THE MISMATCH COUNTER CANNOT DISCRIMINATE — a measure that returns the same output in both worlds

The counter at `:934` keys on the **base** `lang`, so a filtered `zh-Latn` is
recorded as `zh:LATIN` **exactly as a filtered bare `zh` is**. ⚠ **The absence of
`-Latn` from its breakdown is therefore guaranteed by the key format, not
evidence of anything** — it reads identically whether the declared forms passed
or were dropped. It was briefly taken as *positive* evidence that #250 was
working; it cannot be evidence either way.

⚠ **`zh:LATIN 969,391` is consistent with the LOSS, not the recovery** — the
commit measures ~864k declared `zh` romanisations in the staged tree (632,401 +
231,563), comfortably inside that count.

✅ **The acceptance test that DOES discriminate**, and it belongs to step 5.

🛑 **CORRECTED — the first version of this test WOULD HAVE READ 0 ON A PERFECT
RUN.** It said *"query for `lang: zh-Latn` > 0"*. **There is no such row.**
Extraction canonicalises `Shanghai@zh-Latn` to `Shanghai@zh` with `lang='zh'` and
`lang_variant='Latn'` (`:903-906`) — **the base IS the stored lang and the subtag
moves to `lang_variant`.** The fix changes what survives the filter, not how the
survivor is keyed. ⚠ **Another check that cannot fail**, written into this plan
while the correct premise sat in the sentence beside it.

**The real test:**

* **`lang='zh' AND script=LATIN` > 0** — measured **0** before the fix — and
  likewise `fa`, `ja`, `el`, `ru` with `script=LATIN`;
* equivalently **`lang_variant='Latn'` > 0**. ⚠ **Not "the corpus total went
up"** — dedup can mask that in either direction, and the observed total
(72,703,741 against a live 72,703,777) sits within 36 of the old one, which is
precisely why it looked inert and *was*.

### 🛑 RETRIEVAL IS TWO PROBLEMS, AND RERANKING ONLY REACHES THE SMALLER ONE

From §8's own published anchors (`indexing-17`, 6 Sep; full sweep running as
11170893):

```
R@200   v7  0.4766     levenshtein_romanised  0.4768
R@10    v7  0.2940     levenshtein_romanised  0.3230
```

* **Ordering within the pool** — reranker territory. Ceiling **R@10 → 0.477**,
  i.e. **+0.182 absolute, +62% relative**. Real and worth having.
* **Recall into the pool** — **52% of true partners never enter the top 200 at
  all**, and **no reranker can reach them.**

🛑 **The two R@200s agree to 0.0002**, which settles more than it looks. It rules
out a recall problem masked by v7's discrimination win — *the entire v7-vs-
baseline retrieval gap is ordering within the pool*. But it also says **the same
~52% is missed by edit distance and by v7 alike**, so those pairs are hard for
surface similarity *and* for whatever v7 encodes. ✅ **That is exactly the
population v8's real improvements would have to reach** — better IPA in
non-Latin scripts, an objective that produces gradient — **and it is unreachable
by reranking by construction.**

⚠ **Consequence for what to build first: the loss change outranks the reranker.**
A contrastive/InfoNCE objective with in-batch and ANN-mined hard negatives
improves **global geometry (recall into the pool) as well as fine ordering**,
where a reranker is **capped at 0.477 by construction**. *The ceiling on
reranking is bounded and known; the ceiling on fixing retrieval is not.*

### 🛑 MEASURED — `OTHER` IS A TOTAL RETRIEVAL BLACKOUT, AND v8 DOES NOT REACH IT

`indexing-17`, job 11170898, full ranks over all 1,053,229 haystack entries.
✅ **Confirmed NOT a rediscovery** — this document records `OTHER`'s
*composition* (§5.2 D7, §5.8, §5.9) and **nowhere its retrieval consequence.**

```
842 queries (9.7% of 8,713) involve the OTHER script bucket
v7   R@200 = 1 of 842      R@1000 = 4 of 842  (0.0048)
lev  R@1000 = 0.3872
v7 median rank ~400,000–1,000,000 of 1,053,229  — at or below chance
```

`OTHER` is Myanmar, Ol Chiki, Gurmukhi, Tifinagh, Sinhala, Khmer, Mongolian,
Tibetan, Oriya, Ethiopic, Lao, Bopomofo — **395,409 live toponyms.**

⚠ **NOT a tokenisation gap**, checked before being claimed: `OTHER` is script id
19 in `hf/vocab/script_vocab.json`, and the characters are in the 113,280-entry
char vocab. **The model has simply never learned these scripts.**

### 🛑 THE UNIFICATION — one mechanism, opposite outcomes, and the difference is SCRIPT SHARING

`generator.py:155` gates positive pairs on `ipa IS NOT NULL`, so **a language
with no IPA never enters a positive pair and is never trained on.** Two strata
share that cause and land at opposite ends:

* **`gain_v7zero`** — Latin-script European, no IPA, **0.9560**. Rescued by
  **orthographic transfer** from the Spanish and French pairs v7 *did* see.
* **`OTHER`** — non-Latin, no IPA, **~chance**. **Nothing to transfer from.**

🛑 **Same cause, opposite outcome, and the difference is entirely whether the
script was shared with trained data.** That is the sharpest available statement
of what v7 actually learned, and it **predicts** that adding IPA to a
previously-unrouted **non-Latin** language yields a large gain where adding it to
a Latin-script European one yields almost none.

🛑 **WHICH IS WHERE IT TURNS BAD FOR v8 AS PLANNED.** The twenty gain-stratum
languages are **all Latin or Cyrillic** — `ga ca nb ce eu nan ast nn tt eo arz sh
sl cy gl sk oc uz be vec`. **Not one is an `OTHER` script.** ⚠ **So the IPA
recomputation that is v8's primary justification lands almost entirely where
transfer already worked, and does not reach the 9.7% blackout at all.** And none
of `my km lo si bo am ti or pa mn shn dz new` has a `SCRIPT_FIRST` or
`NEURAL_ROUTES` entry (they may reach an installed Epitran mode via the `to_iso3`
fallback — `/vast/ishi/ipa-strata-11169066.out` settles it empirically).

### ✅ PREDICTION REGISTERED AND RESOLVED — TRANSFER IS **SCRIPT**-LEVEL

Tested on `seed20260905/ranks.jsonl`, no new compute. Aggregate by
script-coverage class (query side; partner side agrees, which is the check):

```
class                                     n     R@10   R@200   median rank   lev R@200   Δ@200
v7-zero language, script COVERED        306    0.379   0.562           74       0.533   +0.029
script NOT covered (OTHER)              346    0.003   0.003      544,551       0.367   −0.364
reference: all other queries          8,061    0.303   0.494          217       0.479   +0.015
```

🛑 **The zero-IPA-but-covered class is not merely surviving — it is ABOVE the
corpus reference** (R@200 0.562 vs 0.494; median rank **74** vs 217) and beats
Levenshtein. **Zero IPA, never trained as a language, and it outperforms the
average query.** The uncovered class sits at 0.003 with a median rank of
**544,551 of 1,053,229**. ⚠ **Two to four orders of magnitude in median rank
separate them on the same model, same haystack, same k. Language-level transfer
is refuted.**

**Per script, so it is not one lucky stratum** (R@200, all cells n ≥ 44):

```
script      trained control      v7-zero cell        Δ@200 (v7-zero)
ARABIC      0.420 (n=529)        0.364 (n=44)             +0.227
CYRILLIC    0.680 (n=691)        0.572 (n=152)            −0.026
LATIN       0.514 (n=704)        0.627 (n=110)            +0.027
OTHER       — none exists        0.003 (n=346)            −0.364
```

**The v7-zero cell tracks its script's control wherever a control exists, and
collapses only where none does.**

✅ **`arz` — the nominated discriminator — did more than survive.** Δ over
Levenshtein **+0.227** (query) / **+0.175** (partner), **among the largest v7
advantages in the whole benchmark**, on a language for which v7 saw **not one IPA
string**. §8.3 explains it: Arabic romanisation is lossy, so the baseline cannot
cheat and v7's *script-level* representation carries the load. **The hypothesis
made a quantitative prediction it did not have to satisfy.**

🛑 **THE CONTROL THAT RULES OUT THE OBVIOUS ALTERNATIVE, and it was already in
the data.** *"`OTHER` scripts are just intrinsically hard"* would explain the
collapse with no transfer story — but **Levenshtein gets R@200 = 0.367 on exactly
those queries**, in line with 0.479 on the reference. **Those partners are
retrievable in principle by a method that knows nothing about phonology.** So
this is a **COVERAGE HOLE, not a difficulty ceiling** — and the two imply
completely different work.

### 🛑 AND THE BLACKOUT IS A **SCHEMA** LIMIT, NOT A G2P LIMIT — 395,409 rows behind one enum

`Script` (`phonetics/utils/script_detection.py:15`) has **20 members**, of which
`OTHER` is a **single catch-all for every writing system not in the other 19** —
Myanmar, Khmer, Lao, Sinhala, Tibetan, Ethiopic, Oriya, Gurmukhi, Tifinagh,
Mongolian, Ol Chiki, Bopomofo, all one label. `SCRIPT_TAG` has **19 entries and
no `OTHER`**. So at `routes.py:208`:

```python
iso3, tag = self.to_iso3(base), SCRIPT_TAG.get(script)   # tag is None for OTHER
if iso3 and tag:                                          # never true
    ...
return None, "no_route"                                   # every time
```

🛑 **`resolve()` returns `no_route` for all 395,409 rows before Epitran is ever
consulted.** Epitran ships modes for several of these writing systems —
`amh-Ethi` and `pan-Guru` are the obvious candidates — and **the router cannot
even ask**, because the script was flattened before the lookup.
`EPITRAN_SUPPORTED_SCRIPTS` encodes the same assumption a second time.

### 🛑 AND SIX HAND-WRITTEN RULE SETS FOR THOSE SCRIPTS ALREADY EXIST, INSTALLED AND UNREACHABLE

Raised by SG, 6 Sep: he **hand-crafted custom Epitran CSV rules** for languages
outside the published set. `zenodo/epitran_extensions/` holds **115**, and six are
for exactly the blackout scripts:

```
bod-Tibt  Tibetan     khm-Khmr  Khmer      mya-Mymr  Myanmar
pan-Guru  Punjabi     sat-Olck  Santali    sin-Sinh  Sinhala
        ALL SIX fall in Script.OTHER — unreachable
(the other 109: Latn 83, Cyrl 8, Arab 7, Deva 3, Armn 2, and one each
 Knda/Hani/Gujr/Grek/Geor/Beng — all reachable)
```

🛑 **And they are not merely written — they are INSTALLED.**
`scripts/install_epitran_extensions.sh` puts all 115 into Epitran's map path
(`preflight.py:11`, `routes.py:10`), so `mya-Mymr` and `pan-Guru` are almost
certainly **already among the 218 installed modes**.

**Which completes the chain.** `resolve()` reaches `:208`, computes
`iso3 = 'mya'` correctly from the language, and then `SCRIPT_TAG.get(OTHER)`
returns `None`, `if iso3 and tag:` fails, and it returns `no_route`. **The mode
exists, is installed, and the router can never construct its name** — because the
script was flattened two steps earlier. ⚠ **Hand-written work, shipped, and
unreachable by one missing dictionary entry.**

⚠ **A consequence to plan for:** splitting `Script.OTHER` changes what
`detect_script` returns, so the stored `script` value goes **stale** for those
395,409 rows. Establish early what keys on `script` — the toponyms build writes
it and `is_script_mismatch` reads it.

✅ **So the four findings compose into a small job.** Transfer is script-level →
the fix needs **one route per script, not per language** (not 85 language tags) →
and the reason no such route exists is that **those scripts are not values in the
enum.** ⚠ **Splitting the enum and mapping the tags is a different and much
smaller project than writing new G2P** — do not let the second's cost attach to
the first's rows.

### 🛑 §8's "v7 LOSES RETRIEVAL" IS AN ARTEFACT OF TWO UNSEPARATED STRATA

| stratum | n | v7 R@10 | lev R@10 | Δ@10 | Δ@200 | Δ@1000 |
|---|---|---|---|---|---|---|
| latin-involving, excl OTHER | 3,276 (37.6%) | 0.3391 | 0.4380 | −0.0989 | −0.0611 | −0.0241 |
| **non-Latin↔non-Latin, excl OTHER** | **4,595 (52.7%)** | **0.3164** | 0.2664 | **+0.0501** | **+0.1058** | **+0.1171** |
| any OTHER | 842 (9.7%) | 0.0012 | 0.2150 | −0.2138 | −0.3230 | −0.3824 |
| all excl OTHER | 7,871 (90.3%) | 0.3259 | 0.3378 | −0.0119 | **+0.0363** | +0.0583 |

**Drop the 9.7% blackout and v7 OVERTAKES the baseline from k≈20 onward.**
Median partner rank v7 **275** vs lev 289, and v7 is far better in the tail.
**§8 measured only the half of the curve where v7 loses.**

🛑 **AND §8.6's ACCEPTANCE CRITERION IS ALREADY MET BY THE MODEL IT WOULD
REPLACE.** It reads *"effective rank ≥ 40 of 128 with R@10 at or above
`levenshtein_romanised` on the non-Latin↔non-Latin strata"* — **v7 is at 0.3164
vs 0.2664 there today.** ⚠ **An acceptance criterion the incumbent passes is a
check that cannot fail, inside the gate itself** — the third instance of that
shape today. Restate it against a **margin**, or at **R@1** where v7's lead is
thinnest (+0.0246). The geometry half remains the real gate.

✅ **Reranker ceiling, corpus-wide: R@10 0.294 → 0.482 (+0.187, +63%); on
non-Latin↔non-Latin 0.316 → 0.521 (+0.205, +65%).** Real, worth building, capped
by construction — **~48% of partners never enter the top 200 for any method
tested.**

### ✅ §8's SEED RECOVERED — and the spread makes §8's HEADLINE UNSUPPORTABLE

**`--queries-per-pair 100 --seed 20260905`** (the *corpus* seed, not
`run_benchmark`'s defaults of 40 and 0). 12 of 12 anchors now pass; the
deterministic baselines reproduce to **0.0000** and v7 to **0.0003**, which for a
deterministic scorer can only be CPU-vs-GPU float ordering near ties.

🛑 **At the documented defaults §8 is UNREPRODUCIBLE, and it fails SILENTLY** —
you get 4,843 queries and a plausible-looking table. **Record the invocation, not
just the result.**

🛑 **THE SEED SPREAD, and this is the finding to put in front of SG first.**
Across 11 seeds at fixed budget:

```
v7 overall R@10                spread 0.0090   sd 0.0023
v7 overall R@200               spread 0.0088   sd 0.0025
lev latin-involving R@10       spread 0.0247   sd 0.0077
lev non-Latin↔non-Latin R@200  spread 0.0059   sd 0.0020
```

**§8's "a tie at 200" was a gap of 0.0002 against sd 0.0025** — two orders of
magnitude inside the noise (and at the true seed it is exactly 0.0000). Its R@100
gap (0.0055) is ~2σ; its R@10 gap (0.0288) is ~12σ. ⚠ **So §8's four decimal
places support about two**, and the honest reading of its retrieval table is:
**v7 loses meaningfully at k ≤ 10, and is INDISTINGUISHABLE from k ≈ 50 upward.**

### ✅ WHAT v7 ACTUALLY IS — a clean three-way story

| where | v7 vs edit distance |
|---|---|
| **non-Latin↔non-Latin** (52.7%) | **WINS** — 0.3164 vs 0.2664 @10, +0.1021 @200 |
| **Latin-involving** (37.6%) | **LOSES** — 0.3382 vs 0.4319 @10, and this is the real deficit |
| **`OTHER`** (9.7%) | **DEAD** — 0.0012 flat from k=10 to k=1000 |

**v7 wins where phonology is the only signal, loses where surface similarity is,
and is at chance where it never trained.** ⚠ Excluding `OTHER`, v7 **crosses the
baseline at k=20** and leads at every k above; **+0.0343 at k=200 is ~14σ against
the seed sd**, so unlike §8's "tie" that one is real. ⚠ And `any OTHER` is
**0.0012 flat over two orders of magnitude of k** — *one query, unchanged*. Not a
weak score: **no signal.**

**So v8's job is not "fix retrieval".** It is (a) stop losing at the top of the
ranking on Latin-involving pairs, and (b) cover the dead scripts.

### 🛑 THE PAIR-ELIGIBILITY CENSUS — 31.56% excluded, and `OTHER` is only 1.7% of it

Measured against the IPA store directly (`indexing-17`):

* **`script='OTHER'`: 395,409 rows, 0 ok, 0.00%**, backend `(none)` for every
  one. **The only script in the store at exactly zero**; every other sits
  66.6%–99.9%.
* 🛑 **3,542 of 3,941 `(lang, script)` cells have zero IPA — 22,947,929 rows,
  31.56% of the store.** `script='OTHER'` is just **1.7%** of that. **98.3% sits
  inside WELL-COVERED scripts**: 18.5M with no language tag (16.2M of them
  Latin), `ceb`/LATIN 2,778,161 quarantined, `mul` 200,982, `war` 161,193,
  `vo` 141,234, `ota`/ARABIC 20,189, `grc`/GREEK 16,438. ⚠ And `mn`/CYRILLIC
  7,531 at 0.00% inside a script that is 83.25% covered — **language-shaped holes
  exist inside covered scripts, not only script-shaped ones.**

⚠ **A TRAP IN THE PER-LANGUAGE VIEW, worth more than the numbers.** `my` reads
**21.29% ok** at language level. Cross-tabulated it is `my`/LATIN **19,389 at
99.98%** and `my`/**OTHER 71,675 at 0.00%**. **Epitran's route is keyed on
LANGUAGE and fires only on the romanisation.** Anyone reading the language-level
figure concludes Myanmar is partly covered; **it is not covered at all in its own
script.** Same for `km` and `lo`. `si`, `bo`, `pa`, `am`, `or`, `sat` are 0.00%
on *every* script including Latin.

✅ **This completes the unification with denominators.** `generator.py:155`
excludes **31.6% of the corpus** from positive pairs. Where the excluded rows
**share a script** with trained data (the 16.2M untagged Latin) orthographic
transfer rescues them — that is the 0.9560. Where they share **nothing**
(395,409 `OTHER`) v7 sits at chance. **Same gate, opposite outcomes, script
sharing the whole discriminator.**

🛑 **AND THE EXCLUSION IS STRUCTURAL, NOT INCIDENTAL.** A language enters a gain
stratum only by *acquiring* IPA, and the routes that exist are overwhelmingly for
languages already well served. **The 395,409 `OTHER` rows cannot appear in any
gain stratum until a route exists at all** — so the IPA recomputation cannot
reach them **by construction**, not merely by accident of which languages were
picked.

### 🛑 §8's EVIDENCE NO LONGER EXISTS — only the three rows quoted in this document

There is **no `benchmark.json`, no `haystack_v7.npy` and no `[bench]` log
anywhere on `/vast`** (searched: the eval tree, the repo checkout,
`elastic/logs`, every top-level `*.out`). **A benchmark that gated an authorised
retrain survives solely as prose in a planning file that is not the artefact.**
Any further analysis is therefore a **re-run**, not a post-processing pass.

✅ **The standard this sets, and it should be the norm:** an evaluation that
decides something must leave a **re-derivable artefact**, not a paragraph.
11170893 caches `ranks.jsonl` (per-query, per-scorer, full rank) and
`haystack_v7.npy` so that no future `k` and no future stratification ever needs
compute again.

✅ **And it is being re-run with a PRE-REGISTERED check.** Twelve cells are known
in advance from §8's anchors — R@{1,10,100,200} × {v7, levenshtein, jaro_winkler}
— asserted to ±0.002, with the query count asserted at 8,713 before anything is
scored. ⚠ **Without it a drifted run would look exactly like a finer one.** If
the anchors fail, that is a finding about §8 rather than a broken job.

### 🛑 STEP 5 IS A STAGING CAMPAIGN, NOT A JOB

`scripts/symphonym.sh:34-50` **requires a staging ES and writes there.**
Production ES is localhost-only on the pitt VM and unreachable from a Slurm
compute node, and the rebuild is heavy multiprocessing that must not run on pitt.
So the real shape is:

```
start staging ES on Slurm → rebuild into staging → stage-2 embeddings into staging
                          → promote: snapshot → restore → ONE alias swap
```

**Multi-hour, needing a staging instance** — and the alias swap is the
*promotion's* swap, not a standalone one. ✅ This matches the recorded practice
(`rebuild_staging_default_and_promotion`): full rebuilds build in staging and
`promote_to_production` does snapshot → restore → one `_aliases` swap of both
indices.

⚠ **SG set a PRECONDITION on this** earlier in the campaign: *check the remaining
wall time on staging is adequate before starting.* The `htc` QOS tiers cap at
`htc-htc-ll` = 21 days.

⚠ **Stale comment to fix:** `symphonym.sh:34` says staging is required because
"rebuild reads from places". **It reads the STAGED tree** (`scan_places_staged`);
staging is needed as the **write target**. Correct it, or someone infers the
rebuild depends on ES `places`.

### 🛑 `extract_namespace` HOLDS FIVE SILENT TOPONYM FILTERS — TWO MISFIRED TODAY

The empty-language drop was not a lone defect. The ingest pipeline every
namespace passes through contains **five** silent `continue` filters:

```
origId == null || !origId.contains('@')      malformed id
atPos <= 0                                   empty name before '@'
origName.length() == 0 || lang.length() == 0 ← the EMPTY-LANG drop (fixed at the authority)
normalized.length() < 2                      ← the CJK SINGLE-CHARACTER drop
seen.contains(newId)                         dedup (legitimate)
```
(plus a `normalized.length() > 200` cap.)

🛑 **`normalized.length() < 2` is wrong for CJK, and that is a sharper objection
than "crude".** The 10 tgn places still nameless after the fix include **`麻`** and
**`多`** — ordinary Chinese names killed by a **character-count** rule. A
single character is a complete name in Chinese and Japanese; the rule encodes a
Latin assumption about what "too short to be a name" means. ⚠ **And it is
corpus-wide, not tgn-specific** — every namespace passes through this pipeline.

⚠ **The pattern is the finding.** Two of five filters were found to misfire in a
single day, both silent, both in the same place. **Audit the whole set rather
than fixing them one at a time** — the remaining three have never been examined,
and a filter that has not been questioned is not the same as one that has been
cleared.

✅ **But note which corpus this affects.** These filters are ES-side, and
`rebuild_toponyms_index` reads the **staged** tree, never the `places` index — so
they shape `places.toponyms[]` and **not** the v8 training corpus. ⚠ Which raises
a question worth settling before #246's impact is written up: if the dropped
names were in the `toponyms` index all along (staged-derived), those places were
**discoverable** all along, and the defect was in *enrichment* rather than
*findability*. `indexing-9c` observed exactly that asymmetry days ago
(`Dorkecestre` present in `toponyms`, absent from `places.toponyms[]`).

### ⚠ TWO REPO-WIDE DEFECTS FOUND STARTING THE tgn RE-INGEST (`indexing-9c`)

Both are fixed, and both bear on the corpus-wide rebuild SG has authorised.

🛑 **1. `index_namespace` materialised an entire namespace before writing
anything (`8d39818`) — on the VM that hosts production ES.**
`collect_attestations` returned every place doc it walked as a list. At `ukhc`'s
92 docs that is free, and **the tool's docstring — scoping it to "a single
(small) authority" — was the only guard in place.** At `tgn`'s 2,991,143 it
reached **9.7 GB RSS still climbing at 2.6 GB/min**, against 31 GB available
falling ~1 GB/25s. Killed at 9.7 GB. **Nothing reported a problem; the run simply
grew.**

⚠ **That is the "never run heavy compute on the pitt VM" prohibition being
violated by a tool that looks like an ordinary indexer** — the previous instance
of that rule cost ~1h of production outage. **A safe operating range recorded
only in prose is not a guard.** Now streams (peak ~420 MB, flat), and indexed
`ok` is compared against a pre-scan count, because `ok=N errors=0` cannot see a
short read.

🛑 **2. The full `rebuild_toponyms_index` was aimed at the ALIAS (`ca07cfe`).**
`--toponyms-index` defaults to `toponyms` and `scripts/symphonym.sh` does not
override it, so STEP 4's delete branch **is** entered. Measured against the live
cluster: `DELETE /<alias>` returns **400 `illegal_argument_exception`** in ES
9.0.0 and the concrete index survives — **so the corpus was never at risk.** ⚠
**But it was protected by an ES version's behaviour, not by design**, and the run
would have died at STEP 4 *after* scanning 51.2M places, building the vocabulary
and computing IPA + PanPhon — **hours discarded for a condition knowable in the
first second.** Now guarded at startup.

✅ **Consequence for the authorised rebuild: it must target a NEW DATED CONCRETE
INDEX and end with one `_aliases` swap** — never the alias itself.

**Decisions taken (coordinator, 6 Sep):**

* **Backfill via the shipped bridge at `:1717`, not a side-car Parquet.** A
  separate Parquet that consumers must be re-pointed at is the same fault in a
  different hat: it depends on someone editing two `SELECT t.ipa` sites, and
  until they do, coverage is still zero.
* 🛑 **REVISED 6 Sep — the backfill must NULL `panphon_features` wherever it
  writes `ipa`.** The original decision ("write `ipa` only, leave it NULL") rested
  on the premise that the column **is** NULL. The `undscript` rebuild destroys
  that premise: it **populates `panphon_features` from its own defective IPA**
  (`generate()` with no `max_new_tokens`, no `('ja', CJK)` route). Overwriting
  `ipa` and leaving the vector alone would give every row an `ipa` and a panphon
  vector from **different computations** — individually plausible, jointly wrong,
  and **undetectable**.

  ✅ **And it is not really a deletion**, which is what settles it:
  `panphon_features` is a **pure function of `ipa`**, recomputable at any time
  from the column being written. Nulling forfeits nothing recoverable; leaving it
  creates the one kind of damage a later correction cannot find. **A stale
  derivation beside a corrected source is worse than an absent one** — absence is
  legible, inconsistency is not. Consistent with §10, and it does not foreclose
  the per-segment features §0 keeps conditional.

  ⚠ **Record the nulling in the run's own output** ("N rows: `ipa` written,
  `panphon_features` nulled") so the next reader sees a *decision* rather than a
  gap — the same reason `no_lang` and `no_route` must not be one bucket.
* **The store remains the SYSTEM OF RECORD; the backfill is lossy by
  construction.** A NULL `ipa` in the toponyms DuckDB flattens **seven** states
  into one — `no_lang`, `quarantined`, `no_route`, `non_language_tag`,
  `echoed_input`, `empty_output`, and never-computed. ⚠ **No IPA coverage
  statistic may ever be computed from the toponyms DuckDB**; it comes from
  `ipa.duckdb` with its status breakdown, and the per-status counts (3,411,436
  quarantined, 18,543,146 no_lang) are recorded at backfill time as stated
  numbers rather than left to a later subtraction.

⚠ **A planner keyed on the inventory cannot distinguish "no work" from "the
inventory has not been rebuilt yet"** — Fault 12's shape again. The top-up
planner should assert the inventory's generation against the `tgn` extract it is
meant to cover and **refuse**, rather than return zero.

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
  auxiliary objective. ⚠ Written when coverage was believed to be 54%; it is now
  68.43% (§9d), and the 54% was v7's own training-time figure over a smaller
  corpus.
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
