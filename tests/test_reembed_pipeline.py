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

    def test_the_control_stratum_contains_only_things_that_cannot_change(self):
        """A stratum called "control" must be exactly the non-candidates.

        It was not: D4 names fell into it, and the first partial census showed
        184 changes in a bucket whose whole meaning is that it cannot change.
        """
        for name in ("London", "Москва", "Gherke", "SO-10731", "New York",
                     "東京", "O'Brien"):
            with self.subTest(name=name):
                in_control = reembed.stratum_of(name, "LATIN") == "control"
                self.assertEqual(in_control, not reembed.is_candidate(name, "LATIN"))

    def test_every_name_lands_in_exactly_one_stratum(self):
        cases = [("東京", "CJK", "CJK"), ("서울", "HANGUL", "HANGUL"),
                 ("New York", "LATIN", "multi-word"),
                 (unicodedata.normalize("NFD", "Åre"), "LATIN", "not-NFC"),
                 ("SO-10731", "LATIN", "punctuated"),
                 ("London", "LATIN", "control")]
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
        for name in ("กรุงเทพ", "मुंबई", "কলকাতা", "ਅੰਮ੍ਰਿਤਸਰ"):
            with self.subTest(name=name):
                self.assertFalse(reembed.is_candidate(name, "THAI"))

    def test_plain_names_are_still_not_candidates(self):
        # If this ever inverts, the run embeds the whole index for nothing and
        # the non-candidate control disappears.
        for name in ("London", "Москва", "Αθήνα", "Gherke"):
            with self.subTest(name=name):
                self.assertFalse(reembed.is_candidate(name, "LATIN"))

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
               "unicodedata_version": unicodedata.unidata_version}
        self.assertEqual(reembed.verify_tokeniser(pin),
                         pin["tokeniser_block_sha256"])

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
                        examined=10, changed=1, non_candidate_changed=0):
        final, _, done = reembed.shard_paths(self.dir, "diff", i)
        final.write_bytes(b"")
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

    def test_a_missing_export_manifest_aborts(self):
        (self.dir / "export_manifest.json").unlink()
        with self.assertRaises(SystemExit) as ctx:
            reembed.cmd_apply(self._args())
        self.assertIn("did not finish", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
