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

    def test_every_name_lands_in_exactly_one_stratum(self):
        cases = [("東京", "CJK", "CJK"), ("서울", "HANGUL", "HANGUL"),
                 ("New York", "LATIN", "multi-word"),
                 (unicodedata.normalize("NFD", "Åre"), "LATIN", "not-NFC"),
                 ("London", "LATIN", "control")]
        for name, script, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(reembed.stratum_of(name, script), expected)


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

    def test_repinning_a_moved_tree_aborts(self):
        self._write_pin(tokeniser_block_sha256="d" * 64, hf_inference_block_sha256="d" * 64)
        with self.assertRaises(SystemExit) as ctx:
            reembed.cmd_pin(SimpleNamespace(out_dir=str(self.dir), model_dir=None))
        self.assertIn("NEW run directory", str(ctx.exception))

    def test_pinning_the_real_tree_records_the_shipped_tokeniser(self):
        reembed.cmd_pin(SimpleNamespace(out_dir=str(self.dir), model_dir=None))
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

    def test_a_missing_export_manifest_aborts(self):
        (self.dir / "export_manifest.json").unlink()
        with self.assertRaises(SystemExit) as ctx:
            reembed.cmd_apply(self._args())
        self.assertIn("did not finish", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
