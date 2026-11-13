# Near-miss evaluation set

An end-to-end yardstick for the gateway's *near-miss* (fuzzy) retrieval: a frozen set of mis-spelt place
names with known answers, a runner that sends them through the real `/api/reconcile` pipeline in-process,
and stratified metrics. Its job is to make tuning decisions (and the TM1 accept rule) measurable. **It is a
tuning aid, not the labelled genealogy test set**; confirm any gain on that before claiming it.

## What is in it

`data/nearmiss_v1.jsonl` (1,497 items) and `data/nearmiss_v1.manifest.json` (seed, quotas, concrete index
names and document counts at build time).

| kind | n | what |
|---|---|---|
| `misspelling`, main | 1,200 | real GeoNames populated places (population 200 to 3M), country mix weighted towards family-history markets, real name replaced by a realistic mis-spelling |
| `misspelling`, `targeted` | 80 | classes a random sample barely contains: diacritic loss, Saint/St abbreviation. Small n: wide intervals. Reported separately |
| `misspelling`, `collides` | 100 | the mis-spelling happens to be a real name elsewhere. Ambiguous by construction; reported separately |
| `nonsense` | 117 | pronounceable strings that are not place names. No right answer exists; measures false acceptance |

Errors (`errors.py`): phonetic, vowel drift, doubled letters, diacritics, dropped/extra letters,
transposition, OCR confusions, keyboard slips, abbreviations; 1 to 3 per name by length. The weights are a
judgement, stated in the code. Each item carries its error classes, so results are reported per class.

**Truth.** `truth_cc`: places bearing the original name *in the sampled place's country* (the headline
reading of "right"); `truth_pids`: any place bearing that name; `place_id`: the exact sampled place. All are
valid only for the index recorded in the manifest. Items are split `dev`/`test` by hash of the place id.

## Protocol

1. Tune against `--split dev`. 2. Look at `--split test` rarely, at the end: every look spends it.
3. Report `n` and the 95% interval with every rate; strata overlap by design and are never summed.
4. Compare variants on the same items (the saved per-item results allow paired comparison).

## Running it (Pitt host, read-only, light load, ~2 minutes per variant)

The Pitt clone only has this code after a deploy merge. To run a working copy without deploying it:

    tar c evaluation/__init__.py evaluation/nearmiss | ssh pitt 'mkdir -p /tmp/nm_eval && tar x -C /tmp/nm_eval'
    # as gazetteer, in the `whg` conda env:
    cd /tmp/nm_eval && PYTHONPATH=/tmp/nm_eval python -m evaluation.nearmiss.run_eval --split dev --variant prod --out /tmp/nm_eval/dev_prod.json
    python -m evaluation.nearmiss.run_eval --rescore /tmp/nm_eval/dev_prod.json   # recompute metrics offline

Add a variant by registering it in `_variants()` in `run_eval.py`. `build_set.py` rebuilds the set (the same
seeds reproduce the same items on the same index).

## Baseline, 2026-11-13 (dev split, production clause; `results/`)

Headline hit@10 (right name in the right country), n=611: **unscoped 0.66, country-scoped 0.78**. The old
n-gram-fuzzy clause is statistically indistinguishable (0.66 / 0.79), which confirms the 2026-11-12 decision
to drop it for speed. Where it is weak:

* first two letters changed: hit@10 0.43 unscoped / 0.61 scoped, against 0.74 / 0.84 when intact (the clause
  requires the first two letters to match);
* OCR-style and keyboard-slip errors: 0.28 and 0.22 unscoped, though each is a single edit;
* edit distance 3: 0.45 unscoped; diacritic loss is fine (0.87); Saint/St abbreviation unscoped only 0.39.

**Confidence is a poor accept signal for mis-spelt input.** It measures name match, and almost nothing reaches
50. Unscoped, accepting top answers at confidence >= 30 covers 71% of items at 68% precision and still accepts
19 of 59 nonsense strings; >= 40 accepts none but covers only 5%. Scoped, >= 30 gives 91% precision at 62%
coverage. These are the figures an accept rule has to be built on, on real labelled data.

## Limits

Synthetic errors; GeoNames places only; English-centric error weights; an exact-name index check defines
`collides`; the `gb` namespace is excluded by default, as in production; the right-country truth uses the
`ccodes` field, which is patchy for some sources.
