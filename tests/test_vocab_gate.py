#!/usr/bin/env python3
"""Tests for the train-time script-vocabulary gate.

Run package-qualified: python -m unittest tests.test_vocab_gate
"""
import json
import tempfile
import unittest
from pathlib import Path

from phonetics.training.data_loading import (
    VocabularyStaleError, assert_vocab_current, load_vocab_limits,
)
from phonetics.utils.script_detection import Script


class TestVocabGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "vocab").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, n):
        names = [s.value for s in Script][:n]
        (self.d / "vocab" / "script_vocab.json").write_text(json.dumps(
            {"version": 1, "script_to_id": {v: i for i, v in enumerate(names)}}))

    def test_current_vocabulary_passes(self):
        # The positive control. Without it, a gate that raised unconditionally
        # would pass every other test here.
        self._write(len(list(Script)))
        assert_vocab_current(self.d, {"script": len(list(Script))})

    def test_stale_vocabulary_raises(self):
        self._write(20)
        with self.assertRaises(VocabularyStaleError) as cm:
            assert_vocab_current(self.d, {"script": 20})
        self.assertIn("stale", str(cm.exception))

    def test_missing_vocabulary_raises_rather_than_defaulting(self):
        """A missing file falls back to script=25, which is neither the old 20
        nor the current 37 -- an invented width that only logged a warning."""
        with self.assertRaises(VocabularyStaleError) as cm:
            assert_vocab_current(self.d, {"script": 25})
        self.assertIn("no script vocabulary", str(cm.exception))

    def test_the_gate_is_not_swallowed_by_the_loader_s_except(self):
        """REGRESSION. The gate was first placed INSIDE load_vocab_limits' try,
        whose `except Exception` would have turned the refusal into a warning --
        rebuilding the failure mode it exists to remove. It must propagate."""
        self._write(20)
        with self.assertRaises(VocabularyStaleError):
            load_vocab_limits(self.d, strict=True)

    def test_strict_false_still_returns_limits(self):
        self._write(20)
        limits = load_vocab_limits(self.d, strict=False)
        self.assertEqual(limits["script"], 20)
