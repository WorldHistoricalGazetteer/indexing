"""Script embedding ids are a model contract and must not follow enum order.

Regression test for the defect found 7 Sep 2026: `script_vocab.json` was built
with `{s.value: i for i, s in enumerate(Script)}`, tying a model's embedding
index to the LINE POSITION of an enum member. The blackout fix declared 17 new
scripts above `OTHER`, moving it 19 -> 36. `OTHER` is `encode_script`'s fallback
for every unrecognised script, so a model trained with 20 rows and served a
regenerated vocabulary would have indexed row 36 of a 20-row table — with
nothing able to detect it, because the vocabulary file is self-consistent under
either numbering.
"""
import unittest

from phonetics.utils.script_detection import (
    SCRIPT_ID,
    Script,
    _V7_SHIPPED_IDS,
    _validate_script_ids,
    build_script_vocab,
)


class TestScriptIdPinning(unittest.TestCase):

    def test_ids_are_NOT_declaration_order(self):
        """The discriminating test: this FAILS against the pre-fix code.

        Under `enumerate(Script)` every id equals its declaration index, so this
        assertion could not have passed. It is the only test here that the old
        implementation would not satisfy.
        """
        members = list(Script)
        decl_index = members.index(Script.OTHER)
        self.assertEqual(decl_index, 36, "enum layout changed; update this test's premise")
        self.assertEqual(
            SCRIPT_ID["OTHER"], 19,
            "OTHER must keep its shipped id 19, not its declaration index 36",
        )
        self.assertNotEqual(
            SCRIPT_ID["OTHER"], decl_index,
            "ids have been re-coupled to declaration order — the original defect",
        )

    def test_shipped_v7_ids_never_move(self):
        for name, expected in _V7_SHIPPED_IDS.items():
            self.assertEqual(SCRIPT_ID[name], expected, f"{name} moved from its shipped id")

    def test_every_member_has_an_id_and_ids_are_unique(self):
        vocab = build_script_vocab()
        self.assertEqual(set(vocab), {s.value for s in Script})
        self.assertEqual(len(set(vocab.values())), len(vocab), "duplicate script ids")

    def test_new_scripts_are_additive(self):
        """Everything added after v7 sits above the shipped range."""
        for name, sid in SCRIPT_ID.items():
            if name not in _V7_SHIPPED_IDS:
                self.assertGreaterEqual(
                    sid, len(_V7_SHIPPED_IDS),
                    f"{name} was given an id inside the shipped range",
                )

    def test_guard_rejects_a_missing_id(self):
        """Positive control — the guard must be capable of failing.

        Without this, a `_validate_script_ids` that returned unconditionally
        would satisfy every other test in this file.
        """
        import phonetics.utils.script_detection as sd
        original = dict(sd.SCRIPT_ID)
        try:
            sd.SCRIPT_ID.pop("MYANMAR")
            with self.assertRaises(RuntimeError):
                sd._validate_script_ids()
        finally:
            sd.SCRIPT_ID.clear()
            sd.SCRIPT_ID.update(original)
        _validate_script_ids()  # restored state still valid

    def test_guard_rejects_a_moved_shipped_id(self):
        import phonetics.utils.script_detection as sd
        original = dict(sd.SCRIPT_ID)
        try:
            sd.SCRIPT_ID["OTHER"] = 36  # exactly the defect
            with self.assertRaises(RuntimeError):
                sd._validate_script_ids()
        finally:
            sd.SCRIPT_ID.clear()
            sd.SCRIPT_ID.update(original)


if __name__ == "__main__":
    unittest.main()
