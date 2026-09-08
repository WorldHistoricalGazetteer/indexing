"""The canonical re-embed pipeline's gates — the parts that can be tested on a laptop.

`processing/reembed.py` recomputes 72.7M vectors on the preempt
partition and writes the differing ones back to a live index. Almost none of
that can be exercised here, so these tests cover the parts where being wrong is
silent: which names are candidates and which are controls, that the quantiser
matches the INDEX's writer rather than the gateway's, that a half-written shard
can never be adopted as complete, that a run cannot mix two tokenisers, and that
each gate refuses rather than reports.

Every gate below is asserted in BOTH directions — it fires on the bad input and
does not fire on the good one. A gate that only ever passes is not a gate.

⚠ Package-qualified (`python -m unittest tests.test_reembed_pipeline`) or
`discover -s tests -t .`. Never `discover -s tests` — see `tests/__init__.py`.
"""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from processing import reembed
from phonetics.inference.update_es import quantize_embeddings_to_bytes


class TestCandidateAndControl(unittest.TestCase):
    """Who gets re-embedded, and who is allowed to vouch for the weights."""

    def test_the_three_divergence_shapes_are_candidates(self):
        for name, script in [("東京", "CJK"), ("서울", "HANGUL"),
                             ("トウキョウ", "KATAKANA"), ("とうきょう", "HIRAGANA"),
                             ("New York", "LATIN"), ("Bury St Edmunds", "LATIN"),
                             (unicodedata.normalize("NFD", "Åre"), "LATIN")]:
            with self.subTest(name=name):
                self.assertTrue(reembed.is_candidate(name, script))

    def test_a_plain_single_word_name_is_not_a_candidate(self):
        # If this ever returns True the run embeds the whole index for nothing.
        for name in ["London", "Москва", "Αθήνα", "القاهرة", "ירושלים", "กรุงเทพ"]:
            with self.subTest(name=name):
                self.assertFalse(reembed.is_candidate(name, "LATIN"))

    def test_digit_heavy_single_word_names_are_not_controls(self):
        """The D4 trap: these look like the safest controls and are not.

        'S4630' is single-word Latin and already NFC, so every other test calls
        it a control — but the two SCRIPT detectors disagree about it (OTHER vs
        LATIN), so it does not reproduce its stored vector and would drag the
        control's pass rate down for a reason that is not a defect.
        """
        for name in ["S4630", "Q85423919", "GR-9408", "1-2-3"]:
            with self.subTest(name=name):
                self.assertFalse(reembed.is_control(name, "LATIN"))

    def test_ordinary_single_word_names_are_controls(self):
        for name in ["London", "Москва", "Αθήνα", "القاهرة"]:
            with self.subTest(name=name):
                self.assertTrue(reembed.is_control(name, "LATIN"))

    def test_candidates_and_controls_never_overlap(self):
        names = ["London", "New York", "東京", "서울", "S4630", "Åre",
                 unicodedata.normalize("NFD", "Åre"), "Bury St Edmunds"]
        for name in names:
            with self.subTest(name=name):
                self.assertFalse(reembed.is_candidate(name, "LATIN")
                                 and reembed.is_control(name, "LATIN"))

    def test_the_control_FAMILY_is_exactly_the_non_candidates(self):
        """⚠ TAUTOLOGICAL SINCE `is_control` DERIVES FROM `stratum_of`, and kept
        only to pin that it still derives. It cannot detect a wrong answer —
        both sides move together — so the real contract is the test below.
        """
        for name, script in (("London", "LATIN"), ("New York", "LATIN"),
                             ("SO-10731", "LATIN"), ("東京", "CJK")):
            with self.subTest(name=name):
                self.assertEqual(
                    reembed.stratum_of(name, script) in reembed.CONTROL_STRATA,
                    reembed.is_control(name, script))

    def test_majority_non_alphabetic_names_are_NOT_controls(self):
        """The guard `is_control` used to own privately, asserted on real rows.

        🛑 These are why the equivalence above was worthless before the two
        computations were merged: Burmese, Gujarati and Bengali carry enough
        combining marks, viramas and asat characters to cross 0.5, so they were
        `control` by stratum and not by predicate — and the old equivalence test
        passed anyway, because its corpus contained no name of this shape. An
        equivalence asserted over inputs that cannot disagree certifies nothing.
        Rows from indexing-04, against 22,000 real toponyms.

        A control vouches for the WEIGHTS, so a name whose script id the two
        detectors may disagree about must not be one.
        """
        for name, script in (("အရှေ့တောင်ရပ်ကွက်", "OTHER"),
                             ("બિક્કાવોલું", "GUJARATI"),
                             ("কারেয়াকু", "BENGALI"),
                             ("တောင်မင်းကျောင်း", "OTHER"),
                             ("ကော့ယောင်း", "OTHER")):
            with self.subTest(name=name):
                # ⚠ Guards the FIXTURE, not the code. It has already earned its
                # keep: an abbreviated version of these names sat at exactly
                # 0.50 and would have tested nothing while passing.
                self.assertGreater(
                    sum(not c.isalpha() for c in name) / len(name), 0.5,
                    "fixture no longer has the property under test")
                self.assertEqual(reembed.stratum_of(name, script), "punctuated")
                self.assertFalse(reembed.is_control(name, script))

    def test_bare_control_really_cannot_change_under_either_fold(self):
        """The label has to be true of the set, not merely conventional."""
        for name in ("london", "москва", "القاهرة", "ปทุมธานี"):
            with self.subTest(name=name):
                if reembed.stratum_of(name, "LATIN") != "control":
                    continue
                self.assertEqual(unicodedata.normalize("NFKC", name), name)
                self.assertEqual(name.casefold(), name)

    def test_the_split_names_WHICH_fold_would_move_it(self):
        self.assertEqual(reembed.stratum_of("London", "LATIN"), "control-case")
        self.assertEqual(reembed.stratum_of("\ufb01ord", "LATIN"), "control-nfkc")
        # Thai U+0E33: the 5.82% that were being reported as `control`
        self.assertEqual(reembed.stratum_of("\u0e01\u0e33", "THAI"), "control-nfkc")

    def test_every_name_lands_in_exactly_one_stratum(self):
        cases = [("東京", "CJK", "CJK"), ("서울", "HANGUL", "HANGUL"),
                 ("New York", "LATIN", "multi-word"),
                 (unicodedata.normalize("NFD", "Åre"), "LATIN", "not-NFC"),
                 ("SO-10731", "LATIN", "punctuated"),
                 ("London", "LATIN", "control-case"),
                 ("london", "LATIN", "control")]
        for name, script, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(reembed.stratum_of(name, script), expected)


class TestTheMaterialDifferenceCriterion(unittest.TestCase):
    """Byte-inequality is not difference, and measuring that mattered.

    The first real shard reported 1,364 documents "changed" of which 1,163
    differed by exactly one int8 step — the index was written on different
    hardware, so a component near a rounding boundary lands on either side of
    it. Byte-equality would have rewritten ~85% of the population for nothing
    and inflated the census sixfold.
    """

    def test_the_threshold_sits_in_a_measured_empty_gap(self):
        # max|delta| == 1: 1,163 docs, cos >= 0.999876
        # max|delta| == 2: 0 docs           <- the criterion lives here
        # max|delta| >= 3:   201 docs, cos <= 0.996868
        self.assertEqual(reembed.MATERIAL_DELTA, 2)

    def test_one_step_of_noise_is_not_a_difference(self):
        stored = np.zeros(128, dtype=np.int8)
        recomputed = stored.copy()
        recomputed[7] = 1
        delta = int(np.abs(recomputed.astype(np.int16) - stored.astype(np.int16)).max())
        self.assertLess(delta, reembed.MATERIAL_DELTA)

    def test_a_real_tokenisation_difference_is(self):
        # 'SO-10731' measured at max|delta| 11, cosine 0.938.
        stored = np.zeros(128, dtype=np.int8)
        recomputed = stored.copy()
        recomputed[7] = 11
        delta = int(np.abs(recomputed.astype(np.int16) - stored.astype(np.int16)).max())
        self.assertGreaterEqual(delta, reembed.MATERIAL_DELTA)

    def test_noise_on_every_component_is_still_noise(self):
        # The criterion is max, not sum: 128 components each off by one is the
        # same rounding story 128 times, not a different vector.
        stored = np.zeros(128, dtype=np.int8)
        recomputed = np.ones(128, dtype=np.int8)
        delta = int(np.abs(recomputed.astype(np.int16) - stored.astype(np.int16)).max())
        self.assertLess(delta, reembed.MATERIAL_DELTA)


class TestD4NamesAreCandidates(unittest.TestCase):
    """The predicate missed a class that `--scope all` caught in production."""

    def test_digit_heavy_names_are_candidates(self):
        # Measured differing at cosine 0.938 / 0.943 while the predicate called
        # them non-candidates.
        for name in ("SO-10731", "SZ-1555", "Q85423919", "GR-9408"):
            with self.subTest(name=name):
                self.assertTrue(reembed.is_candidate(name, "LATIN"))

    def test_any_punctuation_makes_a_name_a_candidate(self):
        # Deliberately over-inclusive: including a document that turns out to
        # match costs one comparison; excluding one that does not costs a defect
        # nobody looks for again.
        for name in ("Stratford-upon-Avon", "O'Brien", "St. Ives"):
            with self.subTest(name=name):
                self.assertTrue(reembed.is_candidate(name, "LATIN"))

    def test_combining_marks_do_not_make_a_name_a_candidate(self):
        """A Thai vowel sign is not alphabetic but IS Thai.

        It sits inside the Thai block, so the legacy detector counted it as
        THAI exactly as the canonical one reaches THAI from the letters — the
        two agree. Treating every non-alphabetic character as D4 would have
        pulled most of the Thai, Devanagari and Arabic corpus into the candidate
        set and shrunk the control that vouches for the weights.
        """
        # NB: no Gurmukhi here. 'ਅੰਮ੍ਰਿਤਸਰ' sat in this fixture until the census
        # found D5, and it is now a candidate for a reason that has nothing to
        # do with combining marks — which is exactly why it must not be the
        # example that proves marks are harmless.
        for name in ("กรุงเทพ", "मुंबई", "কলকাতা", "தமிழ்"):
            with self.subTest(name=name):
                self.assertFalse(reembed.is_candidate(name, "THAI"))

    def test_plain_names_are_still_not_candidates(self):
        # If this ever inverts, the run embeds the whole index for nothing and
        # the non-candidate control disappears.
        for name in ("London", "Москва", "Αθήνα", "Gherke"):
            with self.subTest(name=name):
                self.assertFalse(reembed.is_candidate(name, "LATIN"))

    def test_gurmukhi_names_are_candidates_d7(self):
        """The divergence only the full census could find.

        The pre-fix detector knew GURMUKHI; the canonical one does not, so
        Punjabi names score OTHER and a backfill-written document carries a
        script id the canonical tokeniser can never reproduce. Nothing about the
        name marks it: single word, NFC, no digits, and BOTH detectors agree on
        OTHER today, because only one of them ever disagreed.

        Twelve real ones turned up at 100% of the corpus having been absent at
        38% — the strongest direct evidence in the run that backfill-written
        documents exist.
        """
        for name in ("ਪਾਕਿਸਤਾਨ", "ਨੇਪਾਲ", "ਬੰਗਲਾਦੇਸ਼", "ਭੂਟਾਨ"):
            with self.subTest(name=name):
                self.assertTrue(reembed.is_candidate(name, "OTHER"))
                self.assertFalse(reembed.is_control(name, "OTHER"))

    def test_other_indic_scripts_are_not_swept_in_by_d5(self):
        # Devanagari and Bengali are in BOTH tables, so they never diverged and
        # must not be pulled into the candidate set by this clause.
        for name in ("मुंबई", "কলকাতা"):
            with self.subTest(name=name):
                self.assertFalse(reembed.is_candidate(name, "DEVANAGARI"))

    def test_a_d4_name_is_never_a_control(self):
        self.assertFalse(reembed.is_control("SO-10731", "LATIN"))


class TestQuantiserMatchesTheIndexWriter(unittest.TestCase):
    def test_identical_to_update_es_on_real_embeddings(self):
        rng = np.random.default_rng(0)
        # L2-normalised 128-d rows: max |component| in production is 0.284.
        vecs = rng.normal(size=(500, 128)).astype(np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        np.testing.assert_array_equal(reembed.quantize(vecs),
                                      quantize_embeddings_to_bytes(vecs))

    def test_it_is_the_unclipped_writer_not_the_clipping_gateway(self):
        """Shows the test knows the difference it is asserting away.

        `gateway.symphonym.quantize_to_byte` clips to [-128, 127]; the index's
        writer does not, and wraps. That cannot fire at |component| 0.284, but
        the point of matching the writer is to be byte-comparable with what is
        already stored, so the difference is asserted rather than assumed away.
        """
        out_of_range = np.array([[2.0] + [0.0] * 127], dtype=np.float32)
        self.assertNotEqual(int(reembed.quantize(out_of_range)[0][0]),
                            int(np.clip(np.round(2.0 * 127.0), -128, 127)))


class TestShardsAreAtomic(unittest.TestCase):
    """A killed preempt task must leave nothing that looks finished."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_temp_file_alone_is_not_complete(self):
        final, temp, done = reembed.shard_paths(self.dir, "shard", 7)
        temp.write_text("half a shard")
        self.assertFalse(reembed.shard_is_complete(self.dir, "shard", 7))

    def test_a_final_file_without_its_marker_is_not_complete(self):
        final, temp, done = reembed.shard_paths(self.dir, "shard", 7)
        final.write_text("looks finished")
        self.assertFalse(reembed.shard_is_complete(self.dir, "shard", 7))

    def test_two_processes_do_not_share_a_temp_path(self):
        """A requeued task can race the original on the same shard.

        With one temp path per shard they would interleave writes and both
        rename, leaving a file that is present, non-empty, marked done and
        corrupt — worse than either task failing.
        """
        import unittest.mock as mock
        _, temp_a, _ = reembed.shard_paths(self.dir, "diff", 7)
        with mock.patch.dict(os.environ, {"SLURM_JOB_ID": "9999999"}):
            _, temp_b, _ = reembed.shard_paths(self.dir, "diff", 7)
        self.assertNotEqual(temp_a, temp_b)

    def test_the_final_and_done_paths_are_stable_across_processes(self):
        # Only the temp may vary: a shard's identity must not depend on which
        # process wrote it, or resume would never find completed work.
        import unittest.mock as mock
        final_a, _, done_a = reembed.shard_paths(self.dir, "diff", 7)
        with mock.patch.dict(os.environ, {"SLURM_JOB_ID": "9999999"}):
            final_b, _, done_b = reembed.shard_paths(self.dir, "diff", 7)
        self.assertEqual(final_a, final_b)
        self.assertEqual(done_a, done_b)

    def test_finishing_renames_and_marks(self):
        final, temp, done = reembed.shard_paths(self.dir, "shard", 7)
        temp.write_text("payload")
        reembed._finish_shard(final, temp, done, {"rows": 3})
        self.assertTrue(reembed.shard_is_complete(self.dir, "shard", 7))
        self.assertFalse(temp.exists())
        self.assertEqual(final.read_text(), "payload")
        self.assertEqual(json.loads(done.read_text())["rows"], 3)


class TestPositiveControlGate(unittest.TestCase):
    def test_it_passes_at_the_quantisation_floor(self):
        result = reembed.check_positive_control([0.99971] * 400)
        self.assertEqual(result["rows"], 400)
        self.assertEqual(result["pass_rate"], 1.0)

    def test_it_aborts_when_the_weights_are_wrong(self):
        with self.assertRaises(SystemExit) as ctx:
            reembed.check_positive_control([0.62] * 400)
        self.assertIn("positive control failed", str(ctx.exception))

    def test_it_aborts_when_there_are_too_few_controls_to_mean_anything(self):
        # The dangerous direction: a control with no subjects passes silently.
        with self.assertRaises(SystemExit) as ctx:
            reembed.check_positive_control([0.99971] * 5)
        self.assertIn("not evidence", str(ctx.exception))

    def test_a_handful_of_stragglers_does_not_abort(self):
        # 0.058% of documents were written by the other encoder; a control that
        # demanded 100% would abort on a real, correct index.
        cosines = [0.99971] * 995 + [0.4] * 5
        self.assertGreater(reembed.check_positive_control(cosines)["pass_rate"], 0.99)


class TestTheControlSurvivesACaseFoldingTokeniser(unittest.TestCase):
    """Gate 1 asks "did these reproduce their stored vector?" — and a run with
    the fold silently absent answers yes to everything. Measured on 3,000 live
    toponyms: 94.55% of control rows change under casefold, so the old gate
    projects a 5.45% pass rate against a 99% floor and every shard aborts."""

    def test_the_current_tree_does_not_fold_so_nothing_changes(self):
        self.assertFalse(reembed.tokeniser_folds_case())

    def test_the_probe_catches_NFKC_WITHOUT_casefold(self):
        """D5 alone adds NFKC and no casefold.

        The first probe tested casefolding only, so a fold-only change answered
        False, every control fell into `stable`, and Gate 1b went inert exactly
        when the change it witnesses shipped. A gate that disarms itself on one
        of its two subjects is worse than none.
        """
        import unittest.mock as mock
        import phonetics.tokenise as tok
        with mock.patch.object(tok, "preprocess_text",
                               lambda x, s=None: unicodedata.normalize("NFKC", x)):
            self.assertTrue(reembed.tokeniser_folds_case())
        with mock.patch.object(tok, "preprocess_text",
                               lambda x, s=None: x.casefold()):
            self.assertTrue(reembed.tokeniser_folds_case())

    def test_the_routing_is_derived_from_the_stratum_and_the_regime(self):
        """One classifier, regime applied at the point of use."""
        both = dict(folds_case=True, folds_compat=True)
        self.assertTrue(reembed.control_must_change("control-case", **both))
        self.assertTrue(reembed.control_must_change("control-nfkc", **both))
        self.assertFalse(reembed.control_must_change("control", **both))
        # and each stratum answers to its OWN regime, not to either
        self.assertFalse(reembed.control_must_change(
            "control-case", folds_case=False, folds_compat=True))
        self.assertFalse(reembed.control_must_change(
            "control-nfkc", folds_case=True, folds_compat=False))

    def test_the_D5_sample_spans_MECHANISMS_not_rows(self):
        """A quota of N rows draws whatever clusters early in the shard.

        Document order is script-clumped — measured over the export's own
        slicing, one slice's first compatibility-only rows were 7 Thai, against
        a corpus-wide random draw of the same population spanning six scripts.
        Only 4 of Unicode's 17 compatibility classes fire in this corpus and
        they are different mechanisms, so an all-Thai sample validates one and
        reports coverage of D5: it cannot fail for the classes it never draws.
        """
        self.assertEqual(reembed.compatibility_classes("\u0e01\u0e33"), {"<compat>"})
        self.assertEqual(reembed.compatibility_classes("\uff34"), {"<wide>"})
        self.assertEqual(reembed.compatibility_classes("\u2116"), {"<compat>"})
        self.assertEqual(reembed.compatibility_classes("x\u00b2"), {"<super>"})
        self.assertEqual(reembed.compatibility_classes("London"), set())

    def test_only_compatibility_only_names_are_D5_subjects(self):
        """NFC-changing names belong to D1 and are already handled."""
        self.assertTrue(reembed.is_compatibility_only("\uff34"))
        self.assertTrue(reembed.is_compatibility_only("\u0e01\u0e33"))
        self.assertFalse(reembed.is_compatibility_only("London"))
        self.assertFalse(reembed.is_compatibility_only(
            unicodedata.normalize("NFD", "\u00c5re")))

    def test_the_two_probes_are_reported_separately(self):
        import unittest.mock as mock
        import phonetics.tokenise as tok
        with mock.patch.object(tok, "preprocess_text",
                               lambda x, s=None: unicodedata.normalize("NFKC", x)):
            self.assertEqual(reembed.tokeniser_folds(), (False, True))
        with mock.patch.object(tok, "preprocess_text",
                               lambda x, s=None: x.casefold()):
            self.assertEqual(reembed.tokeniser_folds(), (True, False))
        with mock.patch.object(tok, "preprocess_text",
                               lambda x, s=None: unicodedata.normalize("NFKC", x).casefold()):
            self.assertEqual(reembed.tokeniser_folds(), (True, True))

    def test_the_compat_probe_is_not_defeated_by_full_case_folding(self):
        """`str.casefold()` already decomposes U+FB01 to "fi".

        A ligature probe therefore answers True under casefold ALONE and cannot
        separate the regimes — which is also §40.3's interaction stated in one
        line: D-A relocates D5's motivating example without D5.
        """
        self.assertEqual("\ufb01".casefold(), "fi")
        self.assertEqual("\uff34".casefold(), "\uff54")   # stays fullwidth
        self.assertEqual(unicodedata.normalize("NFKC", "\uff34"), "T")

    def test_names_that_must_change_and_did_not_abort_the_run(self):
        with self.assertRaises(SystemExit) as ctx:
            reembed.check_negative_control([0.9999] * 300, 0)
        self.assertIn("PARTIAL", str(ctx.exception))

    def test_the_witness_must_be_thick_enough_to_witness(self):
        """A gate with too few subjects passes. Under D-A this stratum is the
        ONLY evidence the fold landed, so a thin one is a silent pass."""
        with self.assertRaises(SystemExit) as ctx:
            reembed.check_negative_control([0.2] * 10, 0)
        self.assertIn("only 10", str(ctx.exception))

    def test_a_genuine_fold_passes(self):
        got = reembed.check_negative_control([0.2] * 300, 0)
        self.assertEqual(got["unchanged_rate"], 0.0)


class TestFreeSpaceGuard(unittest.TestCase):
    """/vast is shared with production ES, which goes READ-ONLY at ~51 GB free.

    The guard's job is to make this job die before prod notices, so it is tested
    at a floor it must fail — asserting only that it passes on a healthy disk
    would be a check that cannot fail.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_it_passes_with_room(self):
        free = reembed.check_free_space(self.dir, 0.001, "test")
        self.assertGreater(free, 0)

    def test_it_aborts_when_the_volume_is_too_full(self):
        with self.assertRaises(SystemExit) as ctx:
            # A floor no real filesystem clears: the guard must fire on the
            # value, not on some property of the test environment.
            reembed.check_free_space(self.dir, 10 ** 9, "test")
        message = str(ctx.exception)
        self.assertIn("READ-ONLY", message)
        self.assertIn("test", message)

    def test_the_floor_sits_above_the_flood_stage_watermark(self):
        # ES floods at ~51 GB free on this 1 TB volume. A floor at or below that
        # would abort only after the outage it exists to prevent.
        self.assertGreater(reembed.DEFAULT_MIN_FREE_GB, 51)


class TestThePinStopsAMixedRun(unittest.TestCase):
    """Preempt requeues a task at an arbitrary later time, into whatever the
    working tree then holds. HEAD named three different tokenisers in 91 minutes
    on the day this was written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _write_pin(self, **over):
        pin = {"tokeniser_block_sha256": "a" * 64, "hf_inference_block_sha256": "a" * 64,
               "checkpoint": "model.safetensors:" + "b" * 64,
               "git_commit": "c" * 40, "pinned_at": "2026-09-05T00:00:00+00:00"}
        pin.update(over)
        (self.dir / reembed.PIN_FILE).write_text(json.dumps(pin))
        return pin

    def test_a_missing_pin_aborts_rather_than_defaulting(self):
        with self.assertRaises(SystemExit) as ctx:
            reembed.load_pin(self.dir)
        self.assertIn("pin.json", str(ctx.exception))

    def test_a_pin_round_trips(self):
        self.assertEqual(reembed.load_pin(self.dir) if (self.dir / reembed.PIN_FILE).exists()
                         else self._write_pin() and reembed.load_pin(self.dir),
                         self._write_pin())

    def test_a_pin_with_no_provenance_is_refused(self):
        """The staged tree has no .git, and a blank provenance field is worse
        than no file: the pin is the one thing the whole run is answerable by."""
        import unittest.mock as mock
        with mock.patch.object(reembed, "_git_commit", return_value="unknown"):
            with self.assertRaises(SystemExit) as ctx:
                reembed.cmd_pin(SimpleNamespace(out_dir=str(self.dir), model_dir=None, unicodedata_version=None))
        self.assertIn("records nothing", str(ctx.exception))

    def test_the_staged_commit_file_is_authoritative_over_any_repo_above_it(self):
        # An extracted archive unpacked underneath some other checkout must
        # report ITS OWN commit, not the unrelated HEAD it happens to sit under.
        (self.dir / "staged_commit.json").write_text(json.dumps({"commit": "e" * 40}))
        self.assertEqual(reembed._git_commit(self.dir), "e" * 40)

    def test_the_code_trees_marker_beats_a_stale_copy_beside_the_run(self):
        """A real defect, not a hypothetical: 98 shards recorded a superseded
        commit because the run directory held an early COPY of the marker while
        the code was re-staged past it. The commit belongs to the code, so the
        copy travelling with the code wins."""
        import unittest.mock as mock
        code = self.dir / "code"
        code.mkdir()
        (code / "staged_commit.json").write_text(json.dumps({"commit": "a" * 40}))
        (self.dir / "staged_commit.json").write_text(json.dumps({"commit": "b" * 40}))
        with mock.patch.object(reembed, "_repo_root", return_value=code):
            self.assertEqual(reembed._git_commit(self.dir), "a" * 40)

    def test_the_required_unicode_table_is_stated_not_sampled(self):
        """`pin` normally runs on pitt (13.0.0) and compute on CRC (14.0.0).

        `str.isalpha()` is the interpreter's Unicode table, and the tokeniser's
        script detection filters on it — 515 codepoints are alphabetic in 14.0.0
        and not in 13.0.0. Sampling the pinning host would pin 13.0.0 and abort
        every shard, so the required version is stated.
        """
        reembed.cmd_pin(SimpleNamespace(out_dir=str(self.dir), model_dir=None,
                                        unicodedata_version="14.0.0"))
        pin = reembed.load_pin(self.dir)
        self.assertEqual(pin["unicodedata_version"], "14.0.0")
        # and it records what it was pinned BY, so a wrong pin is diagnosable
        self.assertEqual(pin["pinned_by_unicodedata"], unicodedata.unidata_version)

    def test_a_shard_under_the_wrong_unicode_table_aborts(self):
        import unittest.mock as mock
        pin = {"tokeniser_block_sha256": reembed._canonical_block_hash(
                   Path(reembed.__file__).resolve().parents[1] / "phonetics" / "tokenise.py"),
               "git_commit": "c" * 40,
               "aux_tokeniser_sha256": reembed._aux_tokeniser_hashes(
                   Path(reembed.__file__).resolve().parents[1]),
               "unicodedata_version": "99.0.0"}   # never any real table
        with self.assertRaises(SystemExit) as ctx:
            reembed.verify_tokeniser(pin)
        self.assertIn("unicodedata", str(ctx.exception))
        self.assertIn("isalpha", str(ctx.exception))

    def test_the_matching_unicode_table_does_not_abort(self):
        repo = Path(reembed.__file__).resolve().parents[1]
        pin = {"tokeniser_block_sha256": reembed._canonical_block_hash(
                   repo / "phonetics" / "tokenise.py"),
               "git_commit": "c" * 40,
               "aux_tokeniser_sha256": reembed._aux_tokeniser_hashes(repo),
               "unicodedata_version": unicodedata.unidata_version}
        self.assertEqual(reembed.verify_tokeniser(pin),
                         pin["tokeniser_block_sha256"])

    # --- the two tokeniser files that carry no canonical block -------------
    #
    # The gate hashed phonetics/tokenise.py against hf/inference.py, so the
    # partial it could NOT see was the one that updated exactly those two and
    # missed the other half of the same patch. These four tests are the gate's
    # own falsification: the first must fail if the aux check is removed.

    def _pin_for_real_tree(self, **over):
        repo = Path(reembed.__file__).resolve().parents[1]
        pin = {"tokeniser_block_sha256": reembed._canonical_block_hash(
                   repo / "phonetics" / "tokenise.py"),
               "git_commit": "c" * 40,
               "aux_tokeniser_sha256": reembed._aux_tokeniser_hashes(repo),
               "unicodedata_version": unicodedata.unidata_version}
        pin.update(over)
        return pin

    def test_a_partial_that_misses_script_detection_aborts(self):
        """The exact partial the canonical-block gate cannot see.

        D5 changes script ASSIGNMENT, which is consumed outside the tokeniser
        (rebuild_toponyms_index, index_namespace, inference/search, ipa/routes),
        so this is the costlier half of the patch to drop.
        """
        pin = self._pin_for_real_tree()
        pin["aux_tokeniser_sha256"] = dict(pin["aux_tokeniser_sha256"])
        pin["aux_tokeniser_sha256"]["phonetics/utils/script_detection.py"] = "0" * 64
        with self.assertRaises(SystemExit) as ctx:
            reembed.verify_tokeniser(pin)
        self.assertIn("script_detection.py", str(ctx.exception))

    def test_a_partial_that_misses_char_vocab_aborts(self):
        pin = self._pin_for_real_tree()
        pin["aux_tokeniser_sha256"] = dict(pin["aux_tokeniser_sha256"])
        pin["aux_tokeniser_sha256"]["phonetics/vocab/char_vocab.py"] = "1" * 64
        with self.assertRaises(SystemExit) as ctx:
            reembed.verify_tokeniser(pin)
        self.assertIn("char_vocab.py", str(ctx.exception))

    def test_a_pin_predating_the_check_aborts_rather_than_skipping(self):
        """An absent key is not an empty one.

        Reading a missing `aux_tokeniser_sha256` as 'nothing to verify' would
        make every pre-existing pin silently exempt from the check written to
        catch it — the campaign's signature defect, an absent input treated as
        nothing to do.
        """
        pin = self._pin_for_real_tree()
        del pin["aux_tokeniser_sha256"]
        with self.assertRaises(SystemExit) as ctx:
            reembed.verify_tokeniser(pin)
        self.assertIn("predates", str(ctx.exception))

    def test_cmd_pin_records_all_four_tokeniser_files(self):
        reembed.cmd_pin(SimpleNamespace(out_dir=str(self.dir), model_dir=None,
                                        unicodedata_version=None))
        pin = reembed.load_pin(self.dir)
        self.assertIn("tokeniser_block_sha256", pin)
        self.assertIn("hf_inference_block_sha256", pin)
        self.assertEqual(sorted(pin["aux_tokeniser_sha256"]),
                         sorted(reembed.AUX_TOKENISER_FILES))

    def test_repinning_a_moved_tree_aborts(self):
        self._write_pin(tokeniser_block_sha256="d" * 64, hf_inference_block_sha256="d" * 64)
        with self.assertRaises(SystemExit) as ctx:
            reembed.cmd_pin(SimpleNamespace(out_dir=str(self.dir), model_dir=None, unicodedata_version=None))
        self.assertIn("NEW run directory", str(ctx.exception))

    def test_pinning_the_real_tree_records_the_shipped_tokeniser(self):
        reembed.cmd_pin(SimpleNamespace(out_dir=str(self.dir), model_dir=None, unicodedata_version=None))
        pin = reembed.load_pin(self.dir)
        repo = Path(reembed.__file__).resolve().parents[1]
        self.assertEqual(pin["tokeniser_block_sha256"],
                         reembed._canonical_block_hash(repo / "phonetics" / "tokenise.py"))
        # The whole point: the two copies agree, and the run says WHICH one.
        self.assertEqual(pin["tokeniser_block_sha256"], pin["hf_inference_block_sha256"])


class TestTheHashGuardNamesTheRightFailure(unittest.TestCase):
    """Three ways to have the wrong tokeniser; three different fixes.

    The one that matters most is empty input. `git archive HEAD file | sha256sum`
    prints e3b0c442... — the hash of nothing — when git fails, because the pipe
    swallows its exit code. That is the hash of an EMPTY producer, and two
    failed producers agree with each other perfectly, so a comparison of two
    such hashes passes while proving nothing. The guard against drift must not
    itself fail in the direction that passes.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_the_constant_really_is_the_hash_of_nothing(self):
        import hashlib
        self.assertEqual(hashlib.sha256(b"").hexdigest(), reembed.SHA256_OF_NOTHING)

    def test_an_empty_file_is_a_producer_failure_not_a_version_mismatch(self):
        empty = self.dir / "empty.py"
        empty.write_text("")
        with self.assertRaises(SystemExit) as ctx:
            reembed._canonical_block_hash(empty)
        message = str(ctx.exception)
        self.assertIn("hash of nothing", message)
        self.assertNotIn("pinned to", message)   # must NOT read as drift

    def test_a_file_with_no_block_is_reported_as_pre_fix_code(self):
        prefix = self.dir / "old.py"
        prefix.write_text("def _detect_script(t):\n    return 'LATIN'\n")
        with self.assertRaises(ValueError):
            reembed._canonical_block_hash(prefix)

    def test_a_real_block_hashes_to_something_that_is_not_nothing(self):
        repo = Path(reembed.__file__).resolve().parents[1]
        digest = reembed._canonical_block_hash(repo / "phonetics" / "tokenise.py")
        self.assertNotEqual(digest, reembed.SHA256_OF_NOTHING)
        self.assertEqual(len(digest), 64)


class _FakeES:
    """Enough of an ES client for cmd_apply's non-bulk calls."""

    def __init__(self):
        self.options_called = 0

    def options(self, **kw):
        self.options_called += 1
        return self

    def mget(self, index=None, ids=None, _source=None, **kw):
        return {"docs": [{"_id": i, "found": True,
                          "_source": {"embedding": [0] * reembed.EMBEDDING_DIM}}
                         for i in (ids or [])]}

    @property
    def indices(self):
        return self

    def refresh(self, index=None, **kw):
        return {"_shards": {"failed": 0}}


class TestApplyRefusesAPartialOrMixedRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.dir / "export_manifest.json").write_text(json.dumps(
            {"index": "toponyms", "slices": 3, "rows": 30, "skipped": 0,
             "index_total": 30}))
        (self.dir / reembed.PIN_FILE).write_text(json.dumps(
            {"tokeniser_block_sha256": "a" * 64, "hf_inference_block_sha256": "a" * 64,
             "checkpoint": "ckpt", "git_commit": "c" * 40}))

    def _complete_shard(self, i, tokeniser="a" * 64, checkpoint="ckpt",
                        examined=10, changed=1, non_candidate_changed=0,
                        parquet_rows=None):
        """Write a shard the way a real compute task does.

        The data file used to be `b""` — an empty file standing in for a
        parquet. That made every test here unable to reach a whole class of
        fault: a marker and a data file that DISAGREE. `parquet_rows` exists so
        one test can make them disagree deliberately; by default the file
        carries exactly the `changed_total` the marker claims.
        """
        import pyarrow as pa
        import pyarrow.parquet as pq
        final, _, done = reembed.shard_paths(self.dir, "diff", i)
        n = changed if parquet_rows is None else parquet_rows
        pq.write_table(pa.table({
            "toponym_id": pa.array([f"n{i}_{j}@en" for j in range(n)], pa.string()),
            "embedding": pa.array([[0] * reembed.EMBEDDING_DIM for _ in range(n)],
                                  pa.list_(pa.int8(), reembed.EMBEDDING_DIM)),
        }), final)
        done.write_text(json.dumps({
            "shard": i, "shard_id": i, "status": "complete",
            "changed_total": changed, "examined_count": examined,
            "changed_count": changed, "changed_candidate": changed - non_candidate_changed,
            "changed_non_candidate": non_candidate_changed,
            "attempt": 0, "tokeniser_sha256": tokeniser,
            "examined_by_stratum": {"control": examined},
            "changed_by_stratum": {"control": changed},
            "tokeniser_block_sha256": tokeniser, "checkpoint": checkpoint}))

    def _args(self, **over):
        base = dict(es_host="http://unused", es_password_file=None, index="toponyms",
                    in_dir=str(self.dir), batch_size=10, throttle=0,
                    allow_partial=False, execute=False)
        base.update(over)
        return SimpleNamespace(**base)

    def test_missing_shards_abort_before_any_write(self):
        self._complete_shard(0)
        with self.assertRaises(SystemExit) as ctx:
            reembed.cmd_apply(self._args())
        self.assertIn("incomplete", str(ctx.exception))

    def test_a_shard_computed_under_a_different_tokeniser_aborts(self):
        self._complete_shard(0)
        self._complete_shard(1, tokeniser="f" * 64)
        self._complete_shard(2)
        with self.assertRaises(SystemExit) as ctx:
            reembed.cmd_apply(self._args())
        self.assertIn("pinned to", str(ctx.exception))

    def test_a_shard_computed_against_a_different_checkpoint_aborts(self):
        self._complete_shard(0)
        self._complete_shard(1, checkpoint="some-other-checkpoint")
        self._complete_shard(2)
        with self.assertRaises(SystemExit):
            reembed.cmd_apply(self._args())

    def test_a_complete_consistent_run_reaches_the_dry_run(self):
        # The positive half: with all three shards present and consistent, the
        # gates above must NOT fire, or they would be refusing everything.
        for i in range(3):
            self._complete_shard(i)
        reembed.cmd_apply(self._args())   # dry-run: no ES client is built

    def test_a_marker_that_disagrees_with_its_own_parquet_aborts(self):
        """The race hazard, made concrete.

        `shard_paths` gives every process a unique temp name and both rename
        onto the same final path, so two tasks working one shard — a requeue
        racing its original, or a deliberate CPU-array/GPU-array race — can
        leave a marker written by one and data written by the other. That is
        benign while both computed the same thing and a CPU task and a GPU task
        do not: this run already counts one-int8-step differences as hardware
        quantisation noise. Every count downstream would then describe a file
        that is not the one being applied.
        """
        self._complete_shard(0)
        self._complete_shard(1, changed=3, parquet_rows=5)
        self._complete_shard(2)
        with self.assertRaises(SystemExit) as ctx:
            reembed.cmd_apply(self._args())
        self.assertIn("different processes", str(ctx.exception))

    def test_a_double_counted_shard_aborts(self):
        """Every shard present and the denominator still wrong.

        On preempt a requeued task is routine; a task that completed twice and
        was recorded once inflates `examined` while leaving `changed` correct,
        so the run reads MORE complete than it is. No ratio-based check can see
        that — only the absolute denominator.
        """
        for i in range(3):
            self._complete_shard(i, examined=20)   # 60 examined, export wrote 30
        with self.assertRaises(SystemExit) as ctx:
            reembed.cmd_apply(self._args())
        self.assertIn("counted twice", str(ctx.exception))

    def test_a_changed_non_candidate_is_reported_as_a_refuted_predicate(self):
        # A non-candidate CANNOT change: it tokenises identically under both
        # encoders. One that did means the predicate is wrong, not that a
        # document was repaired.
        for i in range(3):
            self._complete_shard(i, non_candidate_changed=1)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            reembed.cmd_apply(self._args())
        self.assertIn("NON-CANDIDATE", buf.getvalue())
        self.assertIn("predicate is", buf.getvalue())

    def test_a_smoke_test_export_cannot_be_applied(self):
        """--limit produces a deliberately partial export. Every internal check
        would pass on it, because it is internally consistent — it is simply a
        sample, and applying it would repair a sample and report a run."""
        manifest = json.loads((self.dir / "export_manifest.json").read_text())
        manifest["partial_limit"] = 20000
        (self.dir / "export_manifest.json").write_text(json.dumps(manifest))
        for i in range(3):
            self._complete_shard(i)
        with self.assertRaises(SystemExit) as ctx:
            reembed.cmd_apply(self._args())
        self.assertIn("smoke test", str(ctx.exception))

    def test_a_completed_shard_is_not_rewritten_on_a_rerun(self):
        """The write is the irreversible step, so a rerun must resume.

        Updates are idempotent, but silently repeating 100,960 writes against a
        live index is not free — and before this, a failure halfway through left
        no record of what had landed at all, because the ledger was written only
        on success.
        """
        for i in range(3):
            self._complete_shard(i)
        applied = self.dir / "applied"
        applied.mkdir()
        (applied / "applied_0001.json").write_text(json.dumps(
            {"shard": 1, "ok": 1, "errors": 0, "partial": False,
             "toponym_ids": ["x@en"]}))
        self.assertTrue((applied / "applied_0001.json").exists())
        # The marker is per shard and carries the ids, so "what landed" is
        # answerable from disk without the run having finished.
        meta = json.loads((applied / "applied_0001.json").read_text())
        self.assertEqual(meta["toponym_ids"], ["x@en"])
        self.assertFalse(meta["partial"])

    def test_a_partial_shard_is_REDONE_not_skipped(self):
        """`partial` was written on abort and read nowhere.

        The resume predicate was a bare `marker.exists()`, so a re-run skipped
        the shard it had stopped inside and left the remainder permanently
        unapplied — while the totals stayed coherent, because `ok` is carried
        over from the marker. The abort message promises "Re-run to resume" and
        the resume path did the opposite. Redoing is safe because the updates
        are idempotent.
        """
        import unittest.mock as mock
        for i in range(3):
            self._complete_shard(i, changed=2)
        applied = self.dir / "applied"
        applied.mkdir()
        (applied / "applied_0001.json").write_text(json.dumps(
            {"shard": 1, "ok": 1, "errors": 0, "partial": True,
             "toponym_ids": ["n1_0@en"]}))
        # 0002 finished cleanly and must NOT be redone.
        (applied / "applied_0002.json").write_text(json.dumps(
            {"shard": 2, "ok": 2, "errors": 0, "partial": False,
             "toponym_ids": ["n2_0@en", "n2_1@en"]}))
        seen = []

        def fake_bulk(es, actions, **kw):
            acts = list(actions)
            seen.extend(a["_id"] for a in acts)
            return len(acts), []

        import elasticsearch.helpers as esh
        with mock.patch.object(esh, "bulk", fake_bulk), \
             mock.patch.object(reembed, "_es_client", lambda *a, **k: _FakeES()):
            reembed.cmd_apply(self._args(execute=True, max_error_rate=1.0,
                                         read_back=0, canary=0))

        self.assertIn("n1_0@en", seen, "the PARTIAL shard was skipped — the bug")
        self.assertIn("n1_1@en", seen, "the partial shard's remainder was lost")
        self.assertNotIn("n2_0@en", seen, "a COMPLETE shard was redone")

    def test_a_failed_document_is_NOT_recorded_as_applied(self):
        """The ledger recorded ATTEMPTED, not WRITTEN.

        `s_ids` took the whole chunk regardless of `c_errs`, so a sub-threshold
        error drip left documents on their old vectors while the ledger — the
        one artefact you would use to repair that — listed them as applied.
        """
        import unittest.mock as mock
        import elasticsearch.helpers as esh
        for i in range(3):
            self._complete_shard(i, changed=4)

        def fake_bulk(es, actions, **kw):
            acts = list(actions)
            bad = [a["_id"] for a in acts if a["_id"] == "n0_1@en"]
            return len(acts) - len(bad), [{"update": {"_id": b, "status": 429}}
                                          for b in bad]

        with mock.patch.object(esh, "bulk", fake_bulk), \
             mock.patch.object(reembed, "_es_client", lambda *a, **k: _FakeES()):
            with self.assertRaises(SystemExit) as ctx:
                reembed.cmd_apply(self._args(execute=True, max_error_rate=1.0,
                                             read_back=0, canary=0))
        # a partial write must not exit 0 — the corpus is now mixed
        self.assertIn("NOT written", str(ctx.exception))
        led = json.loads((self.dir / "ledger.json").read_text())
        self.assertEqual(led["failed_ids"], ["n0_1@en"])
        self.assertNotIn("n0_1@en", led["toponym_ids"],
                         "a document ES refused was recorded as applied")

    def test_a_bare_error_COUNT_stops_the_run_rather_than_being_read_as_none(self):
        """A count cannot name its ids, and treating it as 'none failed' is how
        the half-updated corpus arrives."""
        with self.assertRaises(SystemExit) as ctx:
            reembed._failed_ids(7)
        self.assertIn("cannot be named", str(ctx.exception))
        self.assertEqual(reembed._failed_ids([]), set())
        self.assertEqual(reembed._failed_ids(None), set())

    def test_read_back_reports_a_mismatch_rather_than_trusting_the_bulk_ok(self):
        """A bulk `ok` means ES accepted the request, not that the stored vector
        is the one intended. This whole pipeline exists because a stored vector
        was not what everyone assumed."""

        class FakeES:
            def __init__(self, stored):
                self._stored = stored

            def mget(self, index, ids, _source):
                return {"docs": [{"_id": i, "_source": {"embedding": self._stored.get(i)}}
                                 for i in ids]}

        rows = [("a@en", [1, 2, 3]), ("b@en", [4, 5, 6])]
        good = reembed._verify_written(FakeES({"a@en": [1, 2, 3], "b@en": [4, 5, 6]}),
                                       "toponyms", ["a@en", "b@en"], rows)
        self.assertIn("2 of 2 match", good["summary"])
        self.assertNotIn("MISMATCH", good["summary"])

        bad = reembed._verify_written(FakeES({"a@en": [1, 2, 3], "b@en": [9, 9, 9]}),
                                      "toponyms", ["a@en", "b@en"], rows)
        self.assertIn("MISMATCH", bad["summary"])
        # and now it STOPS the run rather than filing the sentence
        with self.assertRaises(SystemExit):
            reembed.enforce_read_back(bad, "test", wrote_this_run=True)

    def test_read_back_compares_ids_against_their_own_vectors(self):
        """The bug this replaces: sample ids from one shard, expected vectors
        from another. The two sets never intersected, so a completely successful
        write of 100,960 documents reported "0 of 20 match, 20 unreadable".

        A verifier whose sample and whose expectations come from different
        places cannot report anything but failure — and it failed on a correct
        run, which is the right direction for the bug and still costly.
        """

        class FakeES:
            def mget(self, index, ids, _source):
                return {"docs": [{"_id": i, "_source": {"embedding": [1, 2, 3]}}
                                 for i in ids]}

        # ids present in `rows`: verifiable
        rows = [("a@en", [1, 2, 3]), ("b@en", [1, 2, 3])]
        self.assertIn("2 of 2 match",
                      reembed._verify_written(FakeES(), "t", ["a@en", "b@en"],
                                              rows)["summary"])
        # ids absent from `rows`: NOT a match — and now named as OUR bookkeeping
        # bug rather than merged with "the document has no embedding". Both used
        # to print as "unreadable", which is why the incident above cost an hour:
        # the label could not say which of the two it was.
        out = reembed._verify_written(FakeES(), "t", ["zz@en"], rows)
        self.assertEqual(out["no_expected"], 1)
        self.assertEqual(out["no_vector"], 0)
        self.assertIn("sample/rows disagree", out["summary"])
        self.assertNotIn("1 of 1 match", out["summary"])

    def test_a_document_with_no_embedding_is_not_the_same_as_an_unknown_id(self):
        class FakeES:
            def mget(self, index, ids, _source):
                return {"docs": [{"_id": i, "_source": {}} for i in ids]}

        out = reembed._verify_written(FakeES(), "t", ["a@en"], [("a@en", [1, 2])])
        self.assertEqual(out["no_vector"], 1)
        self.assertEqual(out["no_expected"], 0)
        self.assertIn("NO EMBEDDING", out["summary"])

    def test_a_short_mget_is_named_rather_than_read_as_a_partial_match(self):
        """A truncated mget under load is exactly what a 65M-document run
        creates, and it otherwise reads as a partial match."""

        class FakeES:
            def mget(self, index, ids, _source):
                return {"docs": [{"_id": ids[0], "_source": {"embedding": [1]}}]}

        out = reembed._verify_written(FakeES(), "t", ["a@en", "b@en"],
                                      [("a@en", [1]), ("b@en", [1])])
        self.assertEqual(out["not_returned"], 1)
        self.assertIn("NOT RETURNED", out["summary"])

    def test_the_CHECKER_failing_now_stops_the_run_TOO(self):
        """⚠ THIS REVERSES A DELIBERATE EARLIER DECISION, on purpose.

        The old contract was "never fail the run on the check's own error", on
        the reasoning that a transient ES hiccup should not fail a 20-hour job
        that wrote correctly. That reasoning is still sound about the WRITE —
        and it is the wrong conclusion about the EXIT CODE, because the write is
        irreversible and already done: aborting cannot undo it, it can only
        signal that nobody verified it. Exiting 0 tells downstream automation
        the corpus is good on the strength of a check that did not run.

        So the error is kept out of `_verify_written` (it still returns rather
        than raising, so the caller decides) and the decision is made once, in
        `enforce_read_back`, where every other verdict is enforced. The message
        says the write may well be fine and the CHECK did not run — a re-read is
        idempotent, so it can simply be repeated.
        """
        class BrokenES:
            def mget(self, **kw):
                raise RuntimeError("cluster busy")

        out = reembed._verify_written(BrokenES(), "toponyms", ["a@en"], [("a@en", [1])])
        self.assertIn("could not read back", out["summary"])
        self.assertEqual(out["error"], "cluster busy")
        with self.assertRaises(SystemExit) as ctx:
            reembed.enforce_read_back(out, "main sample", wrote_this_run=True)
        self.assertIn("could not run", str(ctx.exception))

    def test_a_fully_resumed_run_says_it_verified_NOTHING(self):
        """"No sample because we resumed" and "no sample because the sampler is
        broken" must not both print as a bland "no sample". Resuming is now a
        routine path, so the first is the normal ending for the LAST invocation
        of a campaign — the one whose ledger is the record."""
        empty = {"sampled": 0, "summary": "no sample"}
        reembed.enforce_read_back(empty, "main sample", wrote_this_run=False)
        with self.assertRaises(SystemExit) as ctx:
            reembed.enforce_read_back(empty, "main sample", wrote_this_run=True)
        self.assertIn("bug in the sampler", str(ctx.exception))

    def test_a_missing_export_manifest_aborts(self):
        (self.dir / "export_manifest.json").unlink()
        with self.assertRaises(SystemExit) as ctx:
            reembed.cmd_apply(self._args())
        self.assertIn("did not finish", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
