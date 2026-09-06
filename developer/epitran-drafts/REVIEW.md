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
3. **Silently TRUNCATED IPA — 36 shipped rows.** PanPhon does not error; it returns a
   shorter segment list and the distinction vanishes:
   `dʒʰ → dʒ` (7 files), `ɡʱ → ɡ`, `ʈʳ → ʈ`, `r̩ː → r̩`. **The aspiration and
   breathy-voice contrasts in the Indic-derived maps do not survive to the consumer.**

⚠ **This bears directly on Q1 below.** If PanPhon discards the aspiration anyway, then
mapping `ဃ` to `gʰ` rather than `ɡ` buys nothing downstream — **the Pali-vs-modern
question may be moot for the voiced-aspirate series specifically**, whatever the right
answer is for `သ` and `ရ`.

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

### Tifinagh — `zgh-Tfng.csv` (10,683 rows) — NEW

**Q9 — which variety?** Drafted against the **IRCAM Neo-Tifinagh** standard, tagged
`zgh` (Standard Moroccan Amazigh). Tuareg Tifinagh differs substantially. If the
corpus is largely Kabyle (`kab`), Tachelhit (`shi`) or Tuareg, this is the wrong
target and the file should be renamed and revalued.

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
- **Canadian Aboriginal Syllabics** (2,733 rows). Not one script but a family — Cree,
  Inuktitut and Ojibwe assign different values, and glyph *orientation* encodes the
  vowel. A flat map can work but **only per language**. ⚠ **Blocking question: which
  language are those 2,733 rows?** That must be answered before anything is drafted.

## Returning corrections

Edit the `Phon` column and return the CSV, or annotate the `.NOTES.tsv` companions,
which list only the newly drafted rows with the reasoning for each. Partial returns
are welcome — one language corrected properly is worth more than six skimmed.

## Before this goes out

These drafts should first be run through the mechanical residue harness that produced
the table above, so reviewers are not asked to find gaps a machine can. **Machine
check first, human judgement second.**
