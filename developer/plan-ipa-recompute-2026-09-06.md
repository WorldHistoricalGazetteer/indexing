# IPA recomputation — measurements, defects found, and the incremental design

> **Status: RUN COMPLETE, 6 September 2026.** 72,703,552 rows in the store,
> 49,749,377 carrying IPA — **68.428% coverage, from 0.00%**. All 4,015
> shards present, every array task COMPLETED, all store audit checks passed.
> Store: `/vast/ishi/ipa-v8/store/ipa.duckdb`. Raw artefacts in
> `/vast/ishi/ipa-v8/logs/`; nothing was written outside that directory and the
> new `phonetics/ipa/` package.
>
> Companion: [`finding-charsiu-g2p-defects.md`](finding-charsiu-g2p-defects.md)
> — the ja+CJK routing hole and the CharsiuG2P truncation bug, written up
> separately because they matter to v8 independently of this work.

---

## 0. The short version

Four things, in descending order of how much they change the plan:

1. **There is no IPA on disk anywhere. Coverage is 0.00%, not 54%.** The job is
   a full 72.7M computation, not a 46% top-up.
2. **The published 54.02% describes a corpus that no longer exists** (66.9M vs
   today's 72.7M) and was itself inherited, not computed.
3. **The existing pipeline could never have exceeded ~50% anyway.** Its
   45-entry routing table reaches 36,642,865 of 72,703,552 toponyms. The
   remaining half splits into 21.8% that needs *dict entries only*, 25.5% with
   no language tag, and 2.3% genuinely unsupported.
4. **115 custom Epitran G2P tables were built, installed, and never wired in.**
   Adding routing entries for them unlocks 15,823,375 toponyms — verified, all
   215 cells, zero failures — with no new backend and no GPU.

---

## 1. There is no IPA on disk — measured, not inferred

| store | rows | rows with IPA |
|---|---:|---:|
| `/vast/ishi/data/toponyms-temporal-20260731T160000Z.db` (Aug 4, 39.4 GB) | 72,703,552 | **0** |
| `/ix1/ishi/data/toponyms.db` (May 2, 35.0 GB) | 67,465,428 | **0** |
| `symphonym_cache.duckdb` (28.6 GB) | — | n/a — schema is `toponym_id, model_version, checkpoint_hash, embedding, computed_at`; **embeddings only** |
| `/vast/ishi/models/phonetic/data/**` | — | only `embeddings_v7.parquet` + vocab JSON; no training Parquet |
| toponym JSONL dumps under `/vast` or `/ix1` | — | none survive |
| live ES `toponyms` mapping | 72,703,777 | **no `ipa` field** (confirmed independently against the live mapping) |

⚠ **The zero is credible because the query that produced it also produced
nonzeros.** The same single `LEFT JOIN` reported `recoverable = 0` alongside
`id_present_in_old = 67,351,044` and `absent_from_old = 5,352,508`. A predicate
that can only ever return zero is worthless as evidence
(`~/.claude/memory/filters_must_report_denominator.md`); this one demonstrably
returns large numbers when the data warrants. Two independent formulations
(`count(ipa)` and `count(*) FILTER (...)`) agree.

⚠ **The join key is also sound.** 92.6% of current `toponym_id`s resolve in the
May store, so this is not the key-form trap of
`~/.claude/memory/toponym_id_key_forms_differ.md`.

### 1.1 The 54.02% figure describes a different corpus

`zenodo/training_stats/coverage_stats.json` reports `with_ipa: 31,113,585` over
`in_training_namespaces: 57,593,810`, of `total_toponyms: 66,924,548`. The
current corpus is **72,703,552**. Its own provenance block reads:

```
"from_db_cache":   31113562
"from_precomputed":       2
"from_epitran":          21
```

That run **computed twenty-three IPA strings** and inherited 31.1M from a store
that no longer holds them. It is a statistic about a corpus that no longer
exists, and it should not be quoted as current coverage. (Same class as the
`hf/config.json` staleness already logged in §11 of `plan-symphonym-v8.md`.)

---

## 2. The pipeline's ceiling was ~50%, and half the gap is a dict

`EPITRAN_LANG_MAP` lists **45** `(lang, script)` pairs. The installed Epitran
has **254 modes across 203 ISO-639-3 codes** — because
`scripts/install_epitran_extensions.sh` has already installed the **115 custom
CSV G2P tables** in `phonetics/epitran_extensions/`. Classifying all 72,703,552
toponyms against the real routes (Epitran map + Phonikud `he` + CharsiuG2P
`zh/ko/gan/wuu/yue`):

| class | toponyms | share | cells | what it needs |
|---|---:|---:|---:|---|
| already routable | 36,642,865 | 50.40% | 52 | nothing |
| **Epitran-unlockable** | **15,823,375** | **21.76%** | 215 | **routing entries only** |
| no `lang` at all | 18,543,146 | 25.51% | 20 | language identification |
| genuinely unsupported | 1,694,166 | 2.33% | 3,654 | new backends, or nothing |

### 2.1 The 21.76% is verified, not asserted

A mode *file* existing is not the same as the mode loading, and not the same as
it producing IPA — `testing/test_epitran_loading.py` exists because someone was
bitten by exactly that. So all **215** cells were tested against **real corpus
names**, requiring non-empty output that is not the input echoed back:

```
cells tested        : 215  (ALL unlockable cells)
VERIFIED unlockable : 15,823,375   (21.764% of corpus)
failed              : 0
```

```
ceb-Latn   Olperer            -> olpeɾeɾ
gle-Latn   Loch Mhiontráin    -> lox wiːnt̪ɾaːin
cat-Latn   Castell Rosselló   -> kastɛʎ ɾɔsɛʎo
che-Cyrl   Талды-Булак        -> taldɨ-bulak
eus-Latn   Karrikabürüa       -> karikabüɾüa
nan-Latn   Tiong-po͘           -> ti̯ɔŋ-pɔ˥
```

Largest cells: `ceb` 2,778,161 · `ga` 682,009 · `ca` 678,647 · `nb` 447,678 ·
`ce` 368,716 · `eu` 364,202 · `nan` 337,975 · `ast` 318,705 · `nn` 316,091 ·
`tt` 303,490.

### 2.2 🛑 But "the mode works" is the wrong question for a third of it

The verification above is sound and answers what it was asked. It **cannot**
see the following, because the mode is not broken — it is being asked the wrong
question.

Wikidata carries one label **per Wikipedia edition**. Lsjbot mass-generated
place articles worldwide in Cebuano, Waray, Swedish, Minangkabau and Volapük,
so a `ceb` label on an Austrian mountain records *which wiki has an article*,
not the language of the name. Measured:

| lang | toponyms | from `wd` | name also appears under another lang |
|---|---:|---:|---:|
| **ceb** | 2,786,505 | 98.4% | 82.6% |
| **sv** | 1,825,578 | 94.4% | 82.7% |
| nan | 341,121 | 89.0% | 73.1% |
| sh | 279,332 | 97.4% | 80.6% |
| mul | 209,667 | 99.9% | 88.6% |
| war | 161,194 | 91.5% | 95.3% |
| vo | 141,241 | 89.7% | 95.5% |
| min | 112,829 | 98.0% | 99.1% |

The `ceb` sample is unambiguous: `Olperer`, `Wattle Island`, `N'djili Airport`,
`Suupohja`, `Uniontown, Delaware`, `Kleinkastell Gündersbach`, `Navas del
Marqués`, `Piên`. Running Cebuano phonology over these yields well-formed,
confident, **wrong** IPA at 2.79M scale — 3.8% of the whole corpus, and the
single largest "unlockable" cell.

⚠ **`sv` is not in the unlockable set — it is already routed today**, and is
94.4% Wikidata with 82.7% shared names. So this contamination is already inside
the 50.4% baseline, not only in the new work.

⚠ **A shared name is not by itself proof of a bad label** — *Paris* legitimately
appears under many languages. The inference rests on the conjunction: ~98%
Wikidata provenance, 95–99% name-sharing, and languages (Volapük, Minangkabau)
with no plausible worldwide toponymic footprint.

**Decision taken here:** these languages are **quarantined by default** — a row
is written for every one of them with `status="quarantined"` and no IPA, so
they are recorded rather than silently dropped, and the decision is reversible
with one flag (`--allow-quarantined`) rather than a re-run. This is a judgement
about label provenance, not about the languages, and it is the one substantive
call in this package that a reviewer might reasonably make differently.

**Net verified unlockable, quarantine applied: ~13.0M (17.9%)**, from 15.8M
gross.

### 2.3 The `lang` field carries things that are not languages

Corroborating §4.3 of `plan-symphonym-v8.md` with counts, from the "genuinely
unsupported" tail: `mul` 200,982 · `lauc` 69,085 · `genitive` 34,665 ·
`be:word_stress` 18,540 · `ar1` 20,118 — plus 22,912 rows tagged `lang='en'`
whose script detects as `OTHER`. Upstream ingestion, not Symphonym's to fix,
but it inflates the unsupported bucket.

---

## 3. The 25.5% with no language tag

18,543,146 toponyms (20 cells) carry no `lang`. This is the largest single
bucket and it is **not** a G2P problem.

- The absence is **in the key**: all 18,543,146 `toponym_id`s end in `@`.
- `lang_variant` rescues **79** of them. It is not hiding there.
- It is concentrated, not uniform: `osm` 71.35% no-lang, `gn` 64.48%,
  `tgn` 52.66%, `ohm` 52.61%; `whg`, `clio` and `dp` are 100%; `gb` and `ukhc`
  are 0%.
- 16,211,998 of them are Latin script.

🛑 **No language was guessed for these, and none should be without a decision.**
The tempting move — default Latin-script to `eng-Latn` — would inject 16.2M
confidently-wrong IPA strings, which is the `ceb` problem an order of magnitude
larger. They are recorded with `status="no_lang"`.

Two honest options, both out of scope until someone chooses:

- **(a) Accept the ceiling.** Coverage tops out at ~72% (or ~68% with the
  quarantine).
- **(b) Infer `lang` from country code**, via each toponym's attested places.
  This is inference and it puts a guessed label into training data — but unlike
  interpolating geometry it uses evidence the corpus already holds. `gn`'s
  64.48% is the obvious first target since GeoNames' primary `name` field
  simply carries no language while its alternate names do.

---

### 3.1 🛑 The script accuracies were measuring the SCRIPT, not the inference

The per-script agreement spread (HIRAGANA 99.40%, THAI 91.23%, CJK 76.48%
against LATIN 26.53%) looked like it identified where inference is defensible.
It does not. Set each script's agreement beside the share of its LABELLED rows
carrying that script's single most common language — the accuracy a constant
would achieve:

| script | no-lang rows | agreement | majority-class | inference − constant |
|---|---:|---:|---:|---:|
| LATIN | 16,211,998 | 26.53% | 23.38% (`en`) | **+3.15** |
| CJK | 1,036,998 | 76.48% | 71.87% (`zh`) | **+4.61** |
| CYRILLIC | 640,825 | 36.28% | 30.61% (`ru`) | **+5.67** |
| ARABIC | 332,828 | 44.27% | 32.56% (`fa`) | **+11.71** |
| HANGUL | 107,038 | 50.69% | 99.88% (`ko`) | −49.19 |
| THAI | 37,988 | 91.23% | 99.91% (`th`) | −8.68 |
| GREEK | 33,988 | 65.48% | 91.15% (`el`) | −25.67 |
| KATAKANA | 22,521 | 11.45% | 99.87% (`ja`) | −88.42 |
| ARMENIAN | 3,909 | 48.01% | 96.28% (`hy`) | −48.27 |
| HIRAGANA | 4,536 | 99.40% | 99.85% (`ja`) | −0.45 |

**Every script where country inference looked good is one where a CONSTANT is
better.** HIRAGANA's 99.40% is not skill: 99.85% of hiragana toponyms are
labelled `ja`, so the figure measures the script's mono-nationality and the
inference is fractionally *worse* than saying `ja` every time. The scripts
where inference genuinely beats a constant are the multi-national ones — and
there it adds 3–12 points on top of 26–44% absolute, which is not usable.

⚠ This is the **fourth** instance in this campaign of a corpus property read as
a method property, and the second in this document. The measurement was correct
both times; what it measured was not what the number was taken to mean.

**Conclusion: country-based CLDR inference should not be used anywhere.** Where
it is accurate a constant is more accurate; where it beats a constant it is not
accurate.

### 3.2 What the same table DOES support — a smaller, safer rule

The majority-class column is itself a finding: for mono-national scripts,
"assign the script's modal language" is 99%+ correct. And for choosing a **G2P
backend** that is the right question — you want to know *whose phonology should
read this string*, not whose name it originally was. Katakana is the clean
case: a katakana toponym is often a foreign name, but Japanese phonology is
still what should read it, so `ja` is correct for this purpose even where it is
wrong about the name's origin.

Scripts whose modal language covers ≥95% of their labelled rows — HANGUL, THAI,
KATAKANA, HIRAGANA, ARMENIAN, GUJARATI, TAMIL, MALAYALAM, KANNADA, TELUGU —
account for **177,318 no-lang rows: 0.956% of the no-lang population and 0.244%
of the corpus.** Applying it would move coverage 68.428% → **68.672%**.

That is small, safe and cheap, and it is the whole realistic headroom: **LATIN
is 87.43% of all no-lang rows** and nothing here helps it. The 18.5M gap is a
Latin-script problem, and no script- or country-conditioned rule addresses it.

⚠ Not implemented. It is still an inference and would ship with a provenance
column and its measured rate, per §3.

---

## 4. Two G2P defects found on the way

Both written up in
[`finding-charsiu-g2p-defects.md`](finding-charsiu-g2p-defects.md). In brief:

- **`ja`+`CJK` has no route at all** — 465,177 Kanji toponyms return `None`
  from the shipped `to_ipa`, verified 12 of 12 with a working Katakana control.
- **CharsiuG2P output is truncated** — `generate()` with no length argument
  defaults to 20 ByT5 *byte* tokens. Measured truncation: `yue` 80.0%, `ko`
  71.7%, `ja` 33.3%, `zh` 16.7%, over 2,026,765 corpus rows.

Both are fixed in `phonetics/ipa/backends.py` **before** the first real run.
Because current coverage is zero, neither required remediation of existing data
— they would have, had the recompute been run first.

---

## 5. The design — `phonetics/ipa/`

Established pattern, per SG: **workers emit Parquet shards → one serial merge
into DuckDB**. Concurrent DuckDB writers do not work. Nothing goes into
Elasticsearch: 72.7M updates would be 72.7M tombstones for a field nothing
serves.

| module | role |
|---|---|
| `routes.py` | `(lang, script) → Route`, **derived from the installed mode set**, not a hand-written list |
| `backends.py` | Epitran / CharsiuG2P / Phonikud, with the generation-length fix |
| `plan.py` | inventory ⟕ store → work list + shard manifest |
| `compute.py` | one shard → one Parquet; every input row yields exactly one output row |
| `merge.py` | serial upsert; **refuses an incomplete shard set** |
| `preflight.py` | proves every mode a plan needs actually loads and does not echo |

### 5.1 Four decisions worth stating

**Routes are derived, not listed.** A hand-written table silently loses
capability whenever a mode is added — which is exactly how 115 installed G2P
tables went unused. The hand-maintained part is now only the exceptions.

**Every examined toponym gets a row, including the hopeless ones.** Recording
only successes makes "tried, no route exists" and "never looked" identical, so
every re-run would retry ~20M unroutable rows forever and no coverage figure
would have a denominator.

**Staleness is structural.** `name_sha` is compared, not the `toponym_id`
convention — per
`~/.claude/memory/structural_beats_historical_discriminator.md`.

**The merge checks units done against units expected.** A directory of Parquet
cannot distinguish a finished run from one whose array tasks were pre-empted;
both are "some files". The merge reads the plan's shard list and fails on a
gap, unless `--allow-partial` puts the shortfall on the record. This is Fault
class 1 of `postmortem-ingestion-faults.md`.

### 5.2 It is incremental — demonstrated, not asserted

End-to-end on 430 **real** corpus rows spanning six route classes, run three
times:

```
pass 1  rows needing work : 430   (expect 430)   full computation
pass 2  rows needing work : 0     (expect 0)     nothing changed
pass 3  rows needing work : 2     (expect 2)     one name edited, one row added
INCREMENTAL BEHAVIOUR: PASS

store by status:  ok 301 · no_lang 70 · quarantined 60
quarantined rows: 60 recorded, 0 carrying IPA
```

(Route classes exercised: `en`/`ca` Latin via Epitran, `ru` Cyrillic, `ja`+CJK
via CharsiuG2P, `ceb` quarantined, and 70 rows with no `lang`.)

Pass 2 returning 0 is the whole requirement: a design that recomputed
everything would be indistinguishable from this one on a single run. The
`tgn` re-ingest (place#246 items 4–5) therefore costs only its own delta.

### 5.3 The end-to-end caught a bug the unit tests could not

Pass 1 reported **120 of 120 English rows as `no_route`**. Epitran implements
English **in code** via `flite`/`lex_lookup` — there is no `eng-Latn.csv` — so
deriving routes by globbing the CSV directory dropped the single largest cell
in the corpus. Fixed via `CODE_BACKED_MODES`, with a regression test whose
synthetic mode set deliberately omits `eng-Latn` so it can only pass through
that path. After the fix the same run yields `ok 301` and no `no_route` at all.

`preflight --all-installed` now reports **218 of 218 modes ok**, zero
load failures and zero echoes. (218, not 254: the mode-name regex excludes
dialect-suffixed variants such as `deu-Latn-np`, which nothing routes to.)

⚠ Worth generalising: the unit tests used a *synthetic* mode set and were all
green while the real route table was silently wrong. Only running against real
rows exposed it.

### 5.4 Tests

15 tests, `python -m unittest tests.test_ipa_pipeline` (**never**
`discover -s tests`). Each capability is tested by acceptance **and**
rejection: `ceb` is quarantined *even though* `ceb-Latn` is installed; an
absent mode yields `no_route` rather than a guess; the merge raises on a
missing shard. A suite that only asserted the happy path would pass against a
store that records nothing.

---

## 5.5 The run — what actually landed

Submitted as three arrays, because the backends differ in cost by three orders
of magnitude. Measured throughput (job 11168319, 1,500 real names per mode):
`ceb-Latn` 44,235/s, `rus-Cyrl` 30,202/s, `cat-Latn` 30,100/s, `deu-Latn`
17,154/s, `eng-Latn` 882/s, CharsiuG2P **26.4/s on CPU**. So the neural
backend, at 5% of the rows, was ~90% of the cost and went to a GPU, where it
measured **~150/s on an L40S — about 6x the CPU rate**.

| array | where | tasks | shards |
|---|---|---|---|
| 11168459 | htc CPU | 60 | 454 epitran |
| 11168460 | htc CPU | 40 | 3,539 terminal |
| 3723706 | gpu l40s | 11 | 21 charsiu + 1 phonikud |

**180 + 120 + 33 array tasks, all COMPLETED, zero tracebacks.** Shard output
1.7 GB; `/vast/ishi` ended at 219 GB free of its 1 TB allocation.

### Merge and audit

```
shards expected : 4,015     shards present : 4,015     MISSING : 0
rows merged     : 72,703,552
```

| status | rows |
|---|---:|
| ok | 49,749,377 |
| no_lang | 18,543,146 |
| quarantined | 3,411,436 |
| no_route | 866,948 |
| non_language_tag | 126,394 |
| echoed_input | 6,240 |
| empty_output | 11 |

`verify_store` — **ALL CHECKS PASSED**, each with a positive control in the
same query so a zero cannot come from an empty predicate:

- every inventory row present (72,703,552 of 72,703,552, **0 missing**), no
  duplicate ids
- `ok` rows all carry IPA (0 of 49,749,377 without); terminal rows carry none
  (0 of 22,947,924 with)
- quarantine applied: `ceb` **0 ok of 2,786,505**
- ⚠ **the ja+CJK hole is closed: 465,177 of 465,177** — the population that
  returned `None` from the shipped helper now all carry IPA
- English routed: 10,270,813 of 10,271,605

### ⚠ The truncation fix worked, and the cap still binds on 0.042%

Charsiu output now has mean 22.5 bytes and max 256; the 13–15 character
pile-up is gone. But `max_bytes = 256` is exactly the `max_new_tokens` ceiling,
and "the maximum went up" is not evidence the cap stopped binding — that was
the original bug's whole disguise. Measured: **1,043 rows (0.0419%)** sit at
≥250 bytes.

Inspecting them shows they are **model degeneration, not truncated IPA**:

```
zh  256 bytes  'deɴneɴkakioɯɾiɴçikːokɯɯɴdoɯkaideɴkeisaidaɴɕidaɴɕidaɴɕidaɴɕid'
ko  256 bytes  'ɾjʌ̹nɦa̠ɡje̞o̞ɭʎimpʰik̚sʰa̠ikxɯɭna̠md͡ʑa̠na̠md͡ʑa̠na̠md͡ʑa̠n'
```

Autoregressive repetition loops on out-of-distribution input. Raising the cap
would yield longer garbage, not better IPA. The Epitran control (no generation
cap exists for it) reaches 343 bytes with 1,510 rows ≥250, so genuinely long
IPA does exist and 256 is a real but rarely-binding limit. **Recorded rather
than fixed: 1,043 rows of low-quality output, 0.042% of the neural share.**

---

## 6. What is NOT done

- **The 1,043 degenerate Charsiu rows are not flagged** in the store beyond
  their length; they carry `status='ok'`.
- **The `no_lang` 25.5% has no decision** (§3). Until it has, ceiling is ~72%
  gross / ~68% net of quarantine.
- **Per-segment PanPhon features are not built**, per §10 — conditional on
  whether a teacher survives D-D. The pooled 192-d vector stays retired.
- **`ja`+`CJK` accuracy is unmeasured** beyond "no longer None". CharsiuG2P
  gets Japanese place-name Kanji readings wrong in identifiable ways.
- **No IPA quality evaluation exists.** ⚠ And it cannot be built from Epitran
  output: Epitran is v7's own teacher front end, so scoring new IPA against
  anything Epitran-derived measures the system against its own labeller
  (`~/.claude/memory/labeller_inside_the_thing_measured.md`). The checks here
  deliberately test only that a backend *functions* — loads, and does not echo
  — never that its IPA is *right*.
