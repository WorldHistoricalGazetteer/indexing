# Licence — WHG Epitran rule sets

These files are grapheme→IPA rule sets in Epitran's `Orth,Phon` map format, written
at the World Historical Gazetteer to cover languages and scripts the upstream Epitran
distribution does not.

## Grant

**WHG proceeds on the basis that a single grapheme→IPA row attracts no rights at
all** — that there is essentially one way to express *"this letter makes this
sound"*, and that nothing in such a row is chosen rather than determined.

🛑 **But nothing here depends on that being right.** Contributions are dedicated to
the **public domain under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)**,
which waives *"Copyright and Related Rights"* **without enumerating what they are**
and supplies a fallback licence wherever a waiver is ineffective. ✅ **That is the
point of the instrument: it makes the question not need answering.**

**Why not a licence with conditions.** An Epitran map file is a bare two-column CSV
with **no comment convention** — there is physically nowhere in the file for an
attribution notice to live, so CC BY's single substantive condition cannot be
delivered by the artefact that travels. And Epitran operates no CLA and no DCO, so a
pull request's inbound licence is GitHub's Terms of Service §D.6 — *"you license that
Content under the same terms, and you agree that you have the right to license that
Content under those terms"* — which an attribution-conditioned row would make false
against an MIT project. **CC0 dissolves both: it permits everything MIT permits, and
imposes no condition the format cannot carry.**

⚠ **The published deposit and this dedication are not yet aligned, and that is
recorded rather than smoothed over.** The v7 Zenodo record (below) states **CC BY
4.0** at the record level, covering models, vocabulary and evaluation data as well as
these rule sets. **That statement stands for v7 and is to be revisited for v8**, where
the relationship between a CC0 dedication and a CC BY 4.0 deposit needs stating
carefully rather than asserting. CC0 is the more permissive of the two, so there is no
conflict for a recipient — but there is an ambiguity, and it should be resolved at the
deposit rather than left to be inferred.

## Attribution

> World Historical Gazetteer, *Symphonym: Universal Phonetic Embeddings for
> Cross-Script Name Matching — Models and Evaluation Data*.
> Zenodo. https://doi.org/10.5281/zenodo.18682017

This directory was published in that deposit on 2026-02-22 under CC BY 4.0. **The
licence stated here matches the published record; it does not create a new one.**

## Relationship to Epitran

Epitran (David R. Mortensen et al.) is MIT-licensed, and these files use its map
format and are loaded by it.

**They are not derived from Epitran's own map files.** Five share a filename with an
upstream map, because the name is determined by the language and script rather than
chosen — and comparison against Epitran 1.35.2 shows independent authorship rather
than copying:

```
             upstream rows   ours   identical rows
kat-Geor          33          66          23
khm-Khmr          33          55           0
mya-Mymr         136          48           4
pan-Guru          62          44           5
sin-Sinh          80          52          14
```

**`khm-Khmr` shares no row at all** — the strongest single fact — and `mya-Mymr` is a
third the size of the upstream file it would supposedly be copying. Neither is a
copy-with-edits shape in either direction.

Where values do agree, convergence is the better explanation than copying: the
compilation's selection (all letters of the script) and arrangement (codepoint order)
are both determined rather than chosen. ⚠ **The percentages are corroboration, not
the argument** — on that reasoning even complete agreement would not imply copying,
because there would be nothing chosen in the row to copy. Georgian is a poor case for
a percentage argument in particular: its orthography is close to phonemic, so
independent transcribers should converge heavily, and 35% is arguably *lower* than
independence predicts.

⚠ **This is WHG's working basis, not a legal opinion**, and the CC0 dedication above
exists precisely so that nothing turns on whether it is correct.

## Status of the content

⚠ **These rule sets are not authoritative.** Measurement in September 2026 found
that some convert only a small minority of real toponyms into usable transcriptions,
and that defects are present across a substantial minority of the files. Figures and
methods are recorded in the project's issue tracker rather than restated here, since
they change as the review proceeds.

They are under review by speakers of the languages concerned through the WHG review
interface, and **values here may be wrong**. Corrections are welcome via that
interface.

## Contributions

**Contributions are accepted under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)**
— a dedication to the public domain, waiving whatever rights may subsist without
requiring anyone to establish what they are.

**Credit is practice, everywhere, and not a licence condition.** Contributors are
credited by name where they wish to be, in three places: **in WHG's own records**,
where every correction is recorded against its contributor; **in the dataset**; and
**in the pull request** when a row goes upstream. ⚠ **CC0 imposes no attribution requirement**, so this is a
promise WHG makes rather than a term binding a recipient. Stated plainly so it is not
mistaken for either more or less than it is.
