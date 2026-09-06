# Two silent CharsiuG2P defects — a missing route and a truncated output

> **Measured 6 September 2026** against the live corpus
> (`/vast/ishi/data/toponyms-temporal-20260731T160000Z.db`, 72,703,552 toponyms)
> and the shipped `phonetics/extraction/rebuild_toponyms_index.py`.
> Raw results: `/vast/ishi/ipa-v8/logs/ja_probe.json`, `trunc_probe.json`.
>
> Neither defect is a Symphonym modelling fault. Both are in the G2P plumbing,
> both are silent, and both sit **precisely where §4.5 and §8.3 of
> `plan-symphonym-v8.md` locate v8's entire value proposition**: CJK↔Latin,
> the one stratum where the anyascii+Levenshtein baseline is weak and a learned
> phonetic model has something to win.

---

## 1. Japanese Kanji has no IPA route at all — 465,177 toponyms

`IPAConverter.to_ipa` (`rebuild_toponyms_index.py:311`) dispatches in four
branches. Branch 0 handles `ja` for **HIRAGANA and KATAKANA only**. Chinese is
branch 2, Korean branch 3. `ja` + `CJK` — Kanji — matches none of them, falls
through to the default Epitran branch, finds no `('ja', Script.CJK)` entry in
`EPITRAN_LANG_MAP`, and returns `None`.

**Verified through the shipped helper, not a reimplementation.** Twelve real
`ja`+`CJK` names from the corpus, with the Katakana path as a positive control
in the same run:

```
to_ipa(name, 'ja', Script.CJK)          to_ipa(name, 'ja', Script.KATAKANA)
  下宅          -> None                   ポーイヤック   -> 'poːijakkɯ'
  厳原町豆酘内院  -> None                   トーベイ      -> 'toːbeː'
  千代田町渡瀬    -> None                   タール火山    -> 'taːɾɯ火山'
  大浦          -> None                   エベレスト南峰 -> 'ebeɾesɯto南峰'
  … 12 of 12 None
```

The control matters: it shows the helper works and the dispatcher is reachable,
so `None` is the routing decision, not a broken test.

**Corpus exposure**, `lang='ja'` by script:

| script | toponyms | routed today |
|---|---:|---|
| **CJK (Kanji)** | **465,177** | **no** |
| KATAKANA | 335,158 | yes (`jpn-Ktkn`) |
| HIRAGANA | 149,167 | yes (`jpn-Hrgn`) |

### The fix is already in the file, and nothing reaches it

`CHARSIU_LANG_MAP` (line 181) contains `'ja': 'jpn',  # Japanese (fallback if
Epitran fails)`. No branch ever routes `ja` to CharsiuG2P, so that entry has
never executed. Calling it directly does produce output for all 12 samples.

⚠ **But "produces output" is not "is correct", and for Kanji place names the
gap is wide.** Japanese place-name readings are idiosyncratic and not
derivable from the characters:

```
屋宜            -> 'jagi'                      plausible
大浦            -> 'ooɯɾa'                     plausible
千代田町渡瀬     -> 'seɴdaitamatɕiɰᵝatase'      WRONG — 千代田 is "Chiyoda",
                                               not "sendaitama"
厳原町豆酘内院   -> 'geɴgeɴtɕoɯtoɯtoɯnaiiɴ'     WRONG — 厳原 is "Izuhara"
```

So routing `ja`+`CJK` to CharsiuG2P closes a total absence with partial
accuracy. That is an improvement over `None` — the current state contributes
**zero** teacher signal for 465k Japanese toponyms — but it should not be
recorded as "Japanese is solved". A reading dictionary for place-name Kanji is
the real answer and is out of scope here; §4.5's note that D-B (a Japanese
kanji reading table) was ruled out by "cross-script only" deserves revisiting
in that light.

---

## 2. CharsiuG2P output is silently truncated — up to 80% of a route

`_CharsiuWrapper.transliterate` (line 259) calls:

```python
outputs = self.model.generate(**inputs)
```

with no length argument. HuggingFace defaults `max_length` to **20 tokens**,
and the tokenizer is **ByT5 — byte level**. IPA is heavily multi-byte (`ɯ`,
`ɕ`, `ɴ`, `ː`, tone marks are 2 bytes each), so 20 tokens is roughly **13–15
IPA characters regardless of input length**.

**Measured**: same model, same 60 real corpus names per route, default
`generate()` vs `max_new_tokens=128`. "Truncated" = the two outputs differ.

| route | tested | truncated | rate |
|---|---:|---:|---:|
| `yue` + CJK | 60 | 48 | **80.0%** |
| `ko` + HANGUL | 60 | 43 | **71.7%** |
| `ja` + CJK | 60 | 20 | 33.3% |
| `zh` + CJK | 60 | 10 | 16.7% |

```
[yue] 澎湖列島      shipped='pʰa:ŋ˨˩wu:˨˩li'   (14)  full='pʰa:ŋ˨˩wu:˨˩li:t˨tou˧˥' (22)
[ko] 노스웨스트 준주  shipped='no̞sʰɯwe̞sʰɯt'    (13)  full='no̞sʰɯwe̞sʰɯtʰɯt͡ɕund͡ʑup̚' (26)
[zh] 卡里埃         shipped='kʰa⁵³⁻⁴⁴li'      (10)  full='kʰa⁵³⁻⁴⁴li⁵³⁻⁴⁴i⁴⁴' (18)
```

`澎湖列島` loses two entire syllables. Every truncation lands at 13–15
characters, which is the signature of a fixed token budget rather than of the
inputs.

**Corpus exposure** — rows on CharsiuG2P routes today:

| lang | script | toponyms |
|---|---|---:|
| zh | CJK | 1,583,722 |
| ko | HANGUL | 309,470 |
| wuu | CJK | 49,249 |
| gan | CJK | 37,197 |
| yue | CJK | 33,612 |
| ko | CJK | 13,515 |
| **total** | | **2,026,765** |

plus **465,177** more once `ja`+`CJK` is routed.

### Why it never surfaced

A truncated IPA string is well-formed. It is not empty, not an error, and not
obviously short — downstream it is indistinguishable from the IPA of a shorter
name. Nothing in the pipeline compares output length to input length, and the
only place the defect is visible is a side-by-side rerun with a different
generation budget. This is the standing pattern in
`~/.claude/memory/a-check-that-cannot-fail.md`: the failure produces a
plausible value rather than a missing one.

**Fix**: pass an explicit `max_new_tokens`. Implemented in
`phonetics/ipa/backends.py` (`CHARSIU_MAX_NEW_TOKENS = 256`, chosen against a
longest-observed IPA of ~30 characters / ~60 bytes).

---

## 3. Timing — this is cheap to fix now and expensive to fix later

Both defects would ordinarily have corrupted existing data. They did not,
because **the corpus currently holds no IPA at all** (measured the same day:
`ipa` is NULL across all 72,703,552 rows in the current DuckDB and all
67,465,428 in the May one — see `plan-ipa-recompute-2026-09-06.md` §1). So
there is nothing to remediate: both are fixed *before* the first real
computation rather than found in it afterwards.

Had the recompute run first, 2.49M CJK toponyms would have been written with
confidently truncated IPA, and the model trained on them would have been
evaluated on the CJK stratum that motivated v8 in the first place.
