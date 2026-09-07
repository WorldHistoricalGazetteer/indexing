# Epitran rule drafts for network review

**Draft — not accepted rules.** Nothing here is installed. These are first drafts
for correction by people who read the languages.

## What is being asked

Correct the `Phon` column where it is wrong, and answer the numbered questions.
Anything you are unsure of is more useful marked uncertain than silently accepted.

## Why this matters here

These rules turn a place name into IPA, which is what a cross-script name-matching
model learns from. Where no rule fires, the model gets nothing — and measurement on
1.05M names shows it then performs **at chance**: 1 correct retrieval in 842 for the
affected scripts, against 0.37 for plain edit distance. **A control shows those names
are findable by methods that know no phonetics at all**, so this is a coverage gap,
not an intrinsically hard problem.

## The systematic diagnosis

Across all three existing rule sets examined, **the consonants are mapped and the
independent vowels are entirely absent**. That is a coherent gap — someone worked
through the consonant block and stopped — and it matters disproportionately because
**place names very often begin with a vowel**.

Measured conversion rates on 1,500 real toponyms each, before these drafts:

| script | rule | names fully converted | rows in corpus |
|---|---|---|---|
| Myanmar | `mya-Mymr` | **16.6%** | 79,705 |
| Gurmukhi | `pan-Guru` | **52.1%** | 23,359 |
| Sinhala | `sin-Sinh` | **71.7%** | 15,491 |
| Ol Chiki | `sat-Olck` | 83.1% | 12,991 |
| Khmer | `khm-Khmr` | 97.0% | 11,283 |
| Tibetan | `bod-Tibt` | 97.3% | 16,244 |

`က`, one of the commonest Burmese letters, was simply absent — which alone accounts
for much of the 16.6%.

## Two defects found mechanically, before any human review

Both are machine-checkable, and finding them first is deliberate: reviewer time
should go on judgement, not on things a script can catch.

1. **ASCII `g` (U+0067) instead of IPA `ɡ` (U+0261)** — 38 rows across 28 files,
   **including all six of the rule sets that matter most here**. PanPhon parses `ɡ`
   and rejects `g`, so every /g/ in those languages currently yields an unusable
   segment. Corrected in these drafts; **the shipped files still have it.**
2. **Literal `∅` (U+2205) instead of an empty field** — 15 rows across 10 files,
   including Myanmar, Khmer and Gurmukhi. Epitran's own 139 native maps use an empty
   field 22 times and `∅` **zero** times, so this is not the convention and the
   character is likely emitted into the output. Corrected in these drafts.

## Measured: the drafts work, and a third machine defect emerged

Run through a residue harness against real toponyms, and then against PanPhon:

| mode | shipped | drafted | letter residue |
|---|---|---|---|
| `mya-Mymr` | 16.6% | **98.7%** | 11.5% → 0.3% |
| `sin-Sinh` | 71.7% | **100.0%** | 3.2% → 0.0% |
| `zgh-Tfng` *(new)* | — | 90.5% → higher with 4 letters since added | 1.3% |
| `cmn-Bopo` *(new)* | — | 100.0% | 0.0% |
| `pan-Guru` | 52.1% | *was refusing to load — fixed, see below* | — |

**The independent-vowel diagnosis was the whole story for Myanmar**:
`(က)ရပ်ကွက်` went from `(က)rp∅ကwက∅` to `(k)rpkwk`.

### 🛑 Three defect classes, none of which a residue check can see

Residue asks *"did a rule fire?"*. PanPhon asks *"is the output usable?"*. For
Bopomofo those answers were **100.0% and 5.6%**.

1. **ASCII `g` (U+0067) for IPA `ɡ` (U+0261)** — 38 shipped rows, 28 files, all six
   priority rule sets. PanPhon rejects it.
2. **Literal `∅` (U+2205) for an empty field** — 15 shipped rows, 10 files. Epitran's
   own 139 native maps use `∅` **zero** times.
3. **Silently TRUNCATED IPA — 36 shipped rows** (of **108** total mismatches:
   36 truncated-but-non-empty, 57 parsing to nothing, 15 literal `∅`; three
   different questions, three correct numbers).** PanPhon does not error; it returns a
   shorter segment list and the distinction vanishes:
   `dʒʰ → dʒ` (7 files), `ɡʱ → ɡ`, `ʈʳ → ʈ`, `r̩ː → r̩`. **The aspiration and
   breathy-voice contrasts in the Indic-derived maps do not survive to the consumer.**

⚠ **This bears directly on Q1 below.** If PanPhon discards the aspiration anyway, then
mapping `ဃ` to `gʰ` rather than `ɡ` buys nothing downstream — **the Pali-vs-modern
question may be moot for the voiced-aspirate series specifically**, whatever the right
answer is for `သ` and `ရ`.

### 🛑 UNICODE NORMALISATION HAS BITTEN THIS WORK TWICE, IN OPPOSITE DIRECTIONS

**Store every `Phon` value NFD-normalised, and lint after normalising.** PanPhon
accepts the decomposed form and rejects the composed one for the *same glyph*:

```
'ẽ' = e + U+0303 (NFD)  -> segs ['ẽ']   PARSES
'ẽ' = U+1EBD     (NFC)  -> segs []      NOT PARSED
```

Same for `ĩ ã õ ũ`. ⚠ **A file stored NFC fails the lint while looking perfectly
correct in any editor**, so the next person will reasonably conclude the lint is
wrong.

**And the inverse trap is live in the same work.** `pan-Guru` would not load
because `ਸ਼` appeared twice — once **decomposed** (U+0A38 + U+0A3C), once
**precomposed** (U+0A36) — which render identically, and **Unicode's composition
exclusions mean NFC does NOT merge them.** So: NFC failed to unify two spellings
of one grapheme in the `Orth` column, and NFC is the rejected spelling in the
`Phon` column. **Normalise both columns to NFD, and compare graphemes NFD-wise
when checking for duplicates.**

### 🛑 ONE "STRUCTURAL LIMIT" WAS HALF WRONG — and the half that was wrong was a CORRECTNESS bug

⚠ **This section previously called both bare modifiers unfixable in a map file.
Measured over all 79,716 Myanmar-bearing names, that splits:**

```
modifier          occurrences   distinct preceding chars   after ASAT   top-10 coverage
ှ  ha-hto → ʰ           9,647                        14      0 (0.0%)            99.8%
း  visarga → ː         90,616                        29   53,868 (59.4%)          99.0%
```

🛑 **Ha-hto never follows the asat — not once in 9,647.** It follows ordinary
consonants (`ရ` 4,277, `လ` 1,583, medial `ွ` 1,363, `မ` 944…), so the base *does*
emit and the sequencing explanation cannot be why it fails.

**It fails because the value is WRONG.** PanPhon **accepts aspirated stops and
rejects aspirated sonorants** — which is phonetics, not a quirk:

```
rʰ -> ['r']   *** aspiration silently dropped      r̥ -> ['r̥']  parses
lʰ -> ['l']   ***                                   l̥ -> ['l̥']  parses
kʰ -> ['kʰ']  parses (a stop MAY be aspirated)
```

**Myanmar ha-hto on a sonorant marks DEVOICING, not aspiration.** So `ရ` + `ှ` is
not `rʰ`; it is `r̥` conservatively, `/ʃ/` in modern Burmese. ✅ **The shipped
output was not merely unparseable — it was wrong, and wrong under BOTH answers to
Q1**, so this one needs no register decision to be a defect. ⚠ It is also the
**fourth instance of silent truncation**, found in the place we had stopped
looking.

✅ **Redrafted as 10 two-codepoint rules** (`ရှ`, `လှ`, `မှ`, `နှ`, `ငှ`, `ညှ`,
`ဝှ`, `ယှ`, and the two medials) covering **99.8%** of occurrences, with the bare
`ှ` rule now emitting nothing rather than a wrong and unparseable `ʰ`.

**Q13 — which value for `ရှ` and `ယှ`?** Drafted `r̥` / `j̥`, consistent with this
file's conservative register. **Modern Burmese realises both as /ʃ/.** Same
decision as Q1 and it should be answered the same way.

**The visarga limitation stands, for 59.4% of it.** `aː`, `iː`, `kː`, `nː` all
parse, so `း` attaches fine whenever the preceding grapheme emits something; the
53,868 following the asat are the genuine sequencing case. ⚠ **But 36,748 follow
vowel signs that DO emit** (`ီ` 9,146, `ာ` 7,136, `ေ` 5,159…) — **a separate and
probably smaller problem, not yet chased, and it must not be folded into the
structural bucket without measurement.**

⚠ **THE GENERAL LESSON.** *"A character map cannot express it"* was true of the
**mechanism** and false of the **cardinality** — 14 and 29 contexts, both with
top-10 coverage above 99%. **A structural-impossibility claim needs a cardinality
check before it is published**, because a thing flagged as *inherent* is the one
nobody re-examines.

### The visarga case — genuinely a sequencing limit

Two shipped rules map a Myanmar sign to a **bare modifier**: `ှ` → `ʰ` and `း` →
`ː`. PanPhon parses `kʰ` and `aː` and `kʰaː`, but a modifier **alone** parses to
nothing — it has nothing to modify.

In normal use that is harmless: the modifier concatenates onto the preceding
segment. ⚠ **It becomes bare only when the preceding grapheme emits nothing** —
e.g. a modifier following `်` (asat), which maps to empty. **That is a sequencing
problem, and a character-to-character map cannot express it**; attaching a
diacritic to a variable base needs an Epitran pre/post-processor, not a map row.
**Flagged as a structural limit rather than a correction to make.** It accounts
for the bulk of Myanmar's residual (`ː` ×502, `ʰ` ×155 in real output).

### A method error worth passing on

`pan-Guru` would not load: `ਸ਼` was defined twice and Epitran rejects one-to-many maps.
The cause is instructive — the shipped file writes it **decomposed** (U+0A38 + U+0A3C)
and the draft added it **precomposed** (U+0A36). They render identically, and Unicode's
composition exclusions mean NFC does **not** merge them. **A codepoint-presence check is
not a grapheme-presence check**, and the two differ silently in exactly the scripts this
work targets. Fixed by comparing NFD-normalised graphemes.

## Questions, by language

### Myanmar — `mya-Mymr.csv` (79,705 rows, the largest prize)

**Q1 — which register is the target: modern spoken Burmese, or Pali/orthographic
values?** The existing map looks consistently like the latter, and that is a
decision rather than an error:
- `သ` → `s`, where modern Burmese is **/θ/**
- `ရ` → `r`, where modern Burmese has merged it with `ယ` **/j/**
- a full voiced-aspirate series (`ဃ`→gʰ, `ဈ`→zʰ, `ဎ`→dʰ, `ဘ`→bʰ) that **modern
  Burmese does not have** — these are Pali loans realised as plain voiced stops

For matching spoken place names, the modern values are probably right. **We have not
changed them** — this needs a Burmese speaker's judgement.

**Q2 — Shan and Mon letters.** The Myanmar block also encodes Shan (`ဢ`) and Mon
(`ဨ`, `ဳ`, `ဴ`). These belong to `shn-Mymr` and `mnw-Mymr`, not Burmese, and we have
**left them unmapped**. Should Burmese absorb them so Shan and Mon place names convert
at all, or should separate rule sets be made?

**Q3 — `ံ` ANUSVARA** drafted as `n`. Burmese realises it as a nasal coda /ɴ/ or as
vowel nasalisation. `n` was chosen for downstream compatibility; is that acceptable?

### Gurmukhi — `pan-Guru.csv` (23,359 rows)

**Q4 — `ੱ` ADDAK geminates the FOLLOWING consonant.** Not expressible in a
character-to-character map. Drafted as producing nothing. Is losing gemination
acceptable, or should this go in a preprocessor?

**Q5 — Punjabi is tonal**, and tone is carried by the historical voiced-aspirate
letters. A flat map cannot express it. Is that acceptable for name matching?

**Q6 — bearer letters** `ੳ` URA and `ੲ` IRI drafted as producing nothing, on the
grounds that they carry a vowel sign rather than a sound. Correct?

### Sinhala — `sin-Sinh.csv` (15,491 rows)

**Q7 — prenasalised consonants** (`ඟ ඦ ඬ ඳ ඹ`) — **the machine half is now settled and
the drafts changed.** `ⁿɡ` is the dangerous case: PanPhon does not reject it, it returns
`['ɡ']`, so **the prenasalisation vanishes with no error**. Redrafted as homorganic
nasal + stop (`ŋɡ`, `ndʒ`, `ɳɖ`, `nd`, `mb`), which PanPhon segments correctly and which
is arguably the better analysis anyway. **The linguistic question stands: is that the
right analysis for Sinhala?**

**Q8 — spoken vs literary Sinhala** differ in vowel realisation. Which should the
rules target?

### Tifinagh — `zgh-Tfng.csv` (10,620 rows) — NEW, EXTENDED TO 45 RULES

⚠ **The row count is 10,620, not 10,683**, censused over every Berber-tagged row in
the IPA store: `ber` 8,722, `zgh` 1,833, plus `tzm` 31, `shi` 18, `kab` 15, `rif` 1.
**Note `ber` is 82% of it** — the collective Berber code, not `zgh` — so this file
cannot be routed to by language tag until `ber` resolves to it.

**The existing 37 rules already covered 99.91% of Tifinagh occurrences and 99.2% of
rows.** Tifinagh was never a rule-writing gap; it is an *installation and routing*
gap. The eight rules added below reach 82 further rows.

**Q9 — which variety?** Drafted against the **IRCAM Neo-Tifinagh** standard, tagged
`zgh` (Standard Moroccan Amazigh). Tuareg Tifinagh differs substantially. If the
corpus is largely Kabyle (`kab`), Tachelhit (`shi`) or Tuareg, this is the wrong
target and the file should be renamed and revalued. ⚠ The census above answers part
of this: the language-tagged minority is Tamazight/Tachelhit/Kabyle in that order,
and nothing is tagged Tuareg — but 82% is the undifferentiated `ber`.

**Q13 — two values inferred from the series, not from a source.** `ⴺ` YADDH is
drafted `ðˤ` because the ya-d series runs `ⴷ` d, `ⴸ` ð, `ⴹ` dˤ, so the fourth cell
should be the emphatic interdental — the reflex of Arabic ظ. And `ⴶ` YAJ is drafted
`dʒ` because `ⴵ` (Berber Academy yaj) is already `ʒ` here, so a second yaj most
plausibly denotes the affricate. **If those two letters are variants of one phoneme,
`ⴶ` should be `ʒ` and the rule is wrong.** 12 and 4 rows respectively.

**Q14 — two letters left DELIBERATELY UNMAPPED, and one of them is this file's
largest single gap.** They are absent rather than guessed because **a missing rule
surfaces as residue and gets found, while a wrong one lints clean** — which is the
lesson `ှ → ʰ` taught this campaign.

* **`ⴴ` U+2D34 YAGHH — 37 rows, the biggest gap in the file.** The ya-g series gives
  `ⴳ` /ɡ/ and `ⵖ` YAGH /ɣ/; what the doubled-H form denotes could not be established.
  Plausibly /ʁ/, plausibly a geminate /ɣː/, plausibly a regional variant of the /ɣ/
  that `ⵖ` already covers. **Naming the sound settles 37 rows.**
* **`ⴿ` U+2D3F YAKHH — 9 rows.** `ⵅ` YAKH is already /x/ here; what the doubled-H
  form adds is unclear, and a duplicate value would be indistinguishable from a
  correct one.

⚠ These two appear here rather than in `zgh-Tfng.NOTES.tsv` on purpose: a notes row
keyed to a grapheme that is **absent from the CSV** would reference a row the review
UI has not got. A deliberate absence is a question for this file, not a row comment.

### Coptic — `cop-Copt.csv` (942 rows) — NEW

35 distinct characters attested across 937 `cop`-tagged toponyms; 33 mapped. Coptic
is the Greek alphabet plus seven Demotic letters, and the Greek-derived core carries
Greek values, so **confidence in the core is high and the whole file turns on one
axis**.

**Q15 — Sahidic or Bohairic?** One answer settles three rules:

| letter | Sahidic (drafted) | Bohairic |
|---|---|---|
| `ⲃ` vida | `b` | `v` |
| `ⲫ` fi | `pʰ` | `f` |
| `ⲭ` khi | `kʰ` | `x` |

⚠ **Bohairic is the liturgical standard**, so a reviewer reasoning from church usage
will expect the second column and may read the draft as simply wrong. It is a dialect
choice, not an error, and it should be made deliberately. `ⲃ` alone is 138 rows.

**Q16 — vowel length.** `ⲏ` is drafted `eː` and `ⲱ` `oː`, against short `ⲉ` /e/ and
`ⲟ` /o/. The contrast is conventional in reconstruction; whether it should be encoded
for *toponyms* is a separate question.

**Q17 — two Old Nubian letters left unmapped** (`ⳝ` 3 rows, `ⳟ` 1 row). Out of scope
for a Coptic map and too few to guess at.

### Thaana — `div-Thaa.csv` (1,858 rows) — NEW

46 distinct characters attested, 45 mapped. The 24 native consonants and 10 fili are
a closed, well-documented set with a one-to-one phonemic reading, so confidence is
high; the questions are about two deliberate deletions and a loan series.

**Q18 — alifu `އ` is mapped to NOTHING, and that is a decision.** Alifu is a vowel
*carrier*: it holds a fili at the start of a syllable and has no sound of its own, so
the vowel comes from the fili and the letter contributes nothing. **1,012 rows.**
⚠ If word-initial glottal stop should be represented, this must be `ʔ` instead. Not
obvious either way, and it is the single largest deliberate deletion in the file
after sukun.

**Q19 — the Arabic-derived loan letters** (`ޘ ޙ ޚ ޝ ޞ ޠ ޢ ޣ ޤ ޥ`) are drafted with
their Arabic values (θ ħ x ʃ sˤ tˤ ʕ ɣ q w). They are rare here — between 1 and 43
rows each — so they carry little weight and should be weighed accordingly against
the native set.

⚠ **Not a question, but worth stating because the romanisation misleads**: Dhivehi
writes *eebeefili* "ee" and it is **/iː/**, and *ooboofili* "oo" and it is **/uː/** —
English-style spellings, IPA values. The drafted values follow the IPA.

### Bopomofo — `cmn-Bopo.csv` (3,194 rows) — NEW

**Q10 — are these actually place names?** Bopomofo is a *pronunciation* notation, so
a mapping to IPA is nearly mechanical (high confidence in the values). But 3,194 rows
of it in a gazetteer is odd — they may be ruby annotations or pronunciation fields
rather than names. Worth checking what they are before investing further.

**Q11 — tone marks** (`ˊ ˇ ˋ ˙`) are separate codepoints and are **not** mapped here.

**Q12 — tone, now a decision rather than an omission.** All four tone marks were passing
through unparsed and **94% of Bopomofo output was unusable downstream**. They are now
mapped to nothing, matching this corpus's practice elsewhere (Myanmar and Punjabi are
tonal and neither map encodes tone). ⚠ **The alternative is available**: PanPhon *does*
parse IPA tone letters (`˥`, `˧˥`), so tone could be represented if it is wanted. Is
toneless right? And `ㄦ` is redrafted `ɚ` → `ər`, which parses.

## NOT drafted, deliberately

- **Mongolian traditional script** (3,715 rows). Letterforms are positional and the
  same glyph can represent several phonemes depending on context. **A
  character-to-character map would be actively wrong**, not merely incomplete. Needs
  either a real G2P or a decision to romanise first.
- **Canadian Aboriginal Syllabics** (3,667 rows). ✅ **The blocking question is now
  ANSWERED, by counting: 2,176 Inuktitut** (`iu` 2,108, `ike` 38, `iku` 30) against
  **501 Cree** (`cr` 485, `crk` 12, `crl` 4), plus 928 untagged. So the target is
  `iku-Cans`, Inuktitut only, at 81% of the identified rows — **not a pan-Cans map**,
  and Cree kept separate rather than averaged in.

  ⚠ **Still not drafted, and the reason has changed.** The corpus uses **192 distinct
  characters** spanning Inuktitut, Cree, West-Cree, Th-Cree, Naskapi, Sayisi and
  Carrier, and the top 80 cover only 95%. The map is *derivable from the Unicode
  names* rather than from memory — `ᑲ` is CANADIAN SYLLABICS KA, `ᖏ` is NGI — which
  makes it auditable. **But there is a trap that would poison it systematically:
  Unicode's "O" series is /u/ in Inuktitut** — `ᐅ` is named O and romanises *u*,
  `ᑐ` is TO and romanises *tu*. A naive name-derived map yields /o/ everywhere
  Inuktitut has /u/, across dozens of rules, every one of them linting clean. Cree
  genuinely has /o/ there, which is precisely why the file must be per-language.

## A REQUIREMENT for any romanisation rule set

⚠ **A file that romanises must say WHICH SCHEME it targets, in the file.** This is
not good practice, it is a precondition for review: Wylie and THL disagree on
Tibetan, ISO 15919 and Hunterian on Kannada, and a reviewer handed an unlabelled
file is checking values against a standard they have to guess. **They cannot tell a
wrong value from a different convention**, so the review returns noise.

The corpus sometimes says it outright — `kn:iso15919` names its own scheme in the
language tag, the only case so far where a row declares that it is a romanisation
rather than leaving it to be inferred from a script subtag. **Where the tag does
not say, the file must.**

This applies to `bod-Latn` (Wylie or THL?), `ota-Latn`, `kan-Latn` and the rest of
the romanisation family, which are otherwise among the *easiest* sets to draft — a
romanisation scheme is already a phonetic notation, so the map is a transcription
of a transcription rather than a phonological judgement.

## Returning corrections

Edit the `Phon` column and return the CSV, or annotate the `.NOTES.tsv` companions,
which list only the newly drafted rows with the reasoning for each. Partial returns
are welcome — one language corrected properly is worth more than six skimmed.

## Before this goes out

These drafts should first be run through the mechanical residue harness that produced
the table above, so reviewers are not asked to find gaps a machine can. **Machine
check first, human judgement second.**
