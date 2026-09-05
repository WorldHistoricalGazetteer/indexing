"""The Symphonym tokeniser contract — every entry point emits the same ids.

The `toponyms` index was embedded through
``CharacterVocabulary.encode`` (``phonetics/inference/update_es.py`` →
``encoder.py``). The **gateway** served queries through a second, unrelated
implementation in ``hf/inference.py`` that fed raw codepoints to the same
vocabulary. Same weights, different tokenisation: cos(indexed, queried) was
0.9691 for ``New York``, 0.9429 for ``Bury St Edmunds`` and **−0.3036** for
``東京``, and multi-word names retrieved their own document at rank 1 only
65.7% of the time (n=1,486). `developer/plan-symphonym-v8.md` §2.

These tests hold the two properties that keep that closed:

1. **Equivalence.** ``phonetics.tokenise`` reproduces the vocabulary classes
   bit for bit, so routing the index writer through it changes nothing about
   what gets written.
2. **Identity of the copies.** ``hf/inference.py`` ships to HuggingFace and
   cannot import the repo, so it carries a vendored copy. The copies are
   compared byte for byte AND behaviour for behaviour — either alone can pass
   while the other is false.

⚠ Run this package-qualified (``python -m unittest
tests.test_tokeniser_contract``) or with ``discover -s tests -t .``. **Never**
``discover -s tests`` — see ``tests/__init__.py``.

The suite is built to be able to fail. Every equivalence assertion has a
matching assertion that the *pre-change* implementation disagrees on the same
input (``TestTheFixIsLoadBearing``), so a test that passed because it compared
nothing to nothing would be caught by its own neighbour. Verified in the way
that actually settles it, per
``~/.claude/memory/feedback_measure_must_discriminate.md``: run against a clean
``git archive HEAD`` checkout of the pre-change tree and confirm it fails.
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
import unicodedata
import unittest
from pathlib import Path

from phonetics import tokenise as canonical
from phonetics.utils import script_detection as script_detection_module
from phonetics.utils.korean import decompose_text
from phonetics.vocab.char_vocab import (
    CharacterVocabulary, LanguageVocabulary, ScriptVocabulary,
)

REPO = Path(__file__).resolve().parent.parent

# The vocabulary the shipped model was trained with. `hf/vocab` is a symlink to
# this directory (and on CRC deployments a symlink into IX1_BASE); the tests
# read the tracked path directly so they neither depend on the symlink nor
# touch a network filesystem.
VOCAB_DIR = REPO / "zenodo" / "vocab"

BEGIN = "# --- BEGIN CANONICAL TOKENISER ---"
END = "# --- END CANONICAL TOKENISER ---"


def _vendored_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start, stop = text.index(BEGIN), text.index(END) + len(END)
    return text[start:stop]


def _load_hf_inference():
    """Import ``hf/inference.py`` as its own module, the way the gateway does."""
    hf_dir = str(REPO / "hf")
    if hf_dir not in sys.path:
        sys.path.insert(0, hf_dir)
    spec = importlib.util.spec_from_file_location(
        "hf_inference_under_test", REPO / "hf" / "inference.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fixture — every script the vocabulary was built over, plus the shapes the
# three divergences live in.
# ---------------------------------------------------------------------------

# One real toponym per script in char_vocab.json's `included_scripts`.
BY_SCRIPT = {
    "LATIN": "London",
    "CYRILLIC": "Санкт-Петербург",
    "GREEK": "Αθήνα",
    "ARABIC": "القاهرة",
    "HEBREW": "ירושלים",
    "DEVANAGARI": "मुंबई",
    "BENGALI": "কলকাতা",
    "TAMIL": "சென்னை",
    "TELUGU": "హైదరాబాద్",
    "MALAYALAM": "കൊച്ചി",
    "KANNADA": "ಬೆಂಗಳೂರು",
    "GUJARATI": "અમદાવાદ",
    "THAI": "กรุงเทพ",
    "GEORGIAN": "თბილისი",
    "ARMENIAN": "Երևան",
    "HANGUL": "서울",
    "CJK": "東京",
    "HIRAGANA": "とうきょう",
    "KATAKANA": "トウキョウ",
}

SHAPES = [
    "New York",                 # one space
    "Bury St Edmunds",          # two spaces
    "  leading",                # leading whitespace
    "trailing  ",               # trailing whitespace
    "Stratford-upon-Avon",      # punctuation
    "LONDON", "london", "LoNdOn",             # case
    unicodedata.normalize("NFC", "Åre"),      # NFC / NFD pair
    unicodedata.normalize("NFD", "Åre"),
    "ＴＯＫＹＯ",                # fullwidth Latin
    "北京 Beijing",             # mixed script
    "L",                        # single character
    "​London",             # zero-width space (not whitespace to strip())
    "London Bridge",       # non-breaking space INSIDE a name
]

# Inputs the canonical preprocessing reduces to nothing at all — empty, or
# whitespace that is not U+0020 (U+0020 survives as <SPACE>).
EMPTY_RESULT = ["", "\t", "\n", "\t\n", "\u00a0", "\u2003"]
SPACE_ONLY = [" ", "  ", " \t "]

CORPUS = list(BY_SCRIPT.values()) + SHAPES

# Names that both tokenisations agree on, so their 72.7M-document majority
# share of the index needs no re-embed. Single word, no CJK/Kana/Hangul, NFC.
UNAFFECTED = ["London", "Paris", "Москва", "Αθήνα", "القاهرة", "ירושלים",
              "กรุงเทพ", "თბილისი", "Երևան", "मुंबई"]

# Names the gateway was tokenising differently from the index.
AFFECTED = ["New York", "Bury St Edmunds", "東京", "北京", "서울",
            "トウキョウ", "とうきょう", "London Bridge"]


def _random_corpus(chars, n=3000, seed=0):
    """Strings drawn from the whole vocabulary, not just the fixture.

    The fixture says what a reader thought to write down; this says what the
    vocabulary can actually produce, including the 93,549 CJK characters and
    the 2,817 in OTHER that no hand-written fixture would reach.
    """
    rng = random.Random(seed)
    return [''.join(rng.choice(chars) for _ in range(rng.randint(1, 12)))
            for _ in range(n)]


class VocabFixture(unittest.TestCase):
    """Loads the shipped vocabularies once for the whole suite."""

    @classmethod
    def setUpClass(cls):
        cls.char_vocab = CharacterVocabulary.load(
            VOCAB_DIR / "char_vocab.json", allow_growth=False)
        cls.script_vocab = ScriptVocabulary.load(VOCAB_DIR / "script_vocab.json")
        cls.lang_vocab = LanguageVocabulary.load(VOCAB_DIR / "lang_vocab.json")
        cls.char_to_id = cls.char_vocab.char_to_id
        cls.lang_to_id = cls.lang_vocab.lang_to_id
        cls.script_to_id = {s.value: i
                            for s, i in cls.script_vocab.script_to_id.items()}
        cls.model_vocab_size = json.loads(
            (REPO / "hf" / "config.json").read_text())["vocab_size"]
        cls.random_corpus = _random_corpus(list(cls.char_to_id))


class TestVendoredCopyIsIdentical(unittest.TestCase):
    """`hf/` cannot import the repo, so the copies are checked, not assumed."""

    def test_blocks_are_byte_identical(self):
        repo = _vendored_block(REPO / "phonetics" / "tokenise.py")
        shipped = _vendored_block(REPO / "hf" / "inference.py")
        self.assertEqual(repo, shipped,
                         "hf/inference.py's vendored tokeniser has drifted from "
                         "phonetics/tokenise.py — re-vendor the block")

    def test_the_block_is_not_empty(self):
        # Byte-equality of two empty strings would pass the test above. This is
        # the presence assertion that makes that absence assertion mean
        # something.
        block = _vendored_block(REPO / "phonetics" / "tokenise.py")
        self.assertGreater(len(block), 4000)
        self.assertIn("def tokenise(", block)
        self.assertIn("def preprocess_text(", block)
        self.assertIn("def detect_script(", block)


class TestMatchesTheVocabularyClasses(VocabFixture):
    """Equivalence with the code that embedded the index, input by input."""

    def test_codepoint_map_matches_script_detection(self):
        self.assertEqual(
            canonical._CODEPOINT_MAP,
            {cp: script.value
             for cp, script in script_detection_module._CODEPOINT_MAP.items()},
            "the vendored script ranges have drifted from "
            "phonetics/utils/script_detection.py")

    def test_hangul_decomposition_matches_over_every_syllable(self):
        mismatched = [chr(cp) for cp in range(0xAC00, 0xD7A4)
                      if canonical.decompose_hangul(chr(cp)) != decompose_text(chr(cp))]
        self.assertEqual(mismatched, [])
        self.assertEqual(canonical.decompose_hangul("서울"), "ㅅㅓㅇㅜㄹ")

    def test_detected_script_matches(self):
        for text in CORPUS + self.random_corpus:
            with self.subTest(text=text):
                expected, _ = script_detection_module.detect_script(text)
                self.assertEqual(canonical.detect_script(text), expected.value)

    def test_char_ids_match(self):
        deviations = 0
        for text in CORPUS + self.random_corpus:
            with self.subTest(text=text):
                expected = self.char_vocab.encode(text)
                if not expected:
                    # The documented deviation, asserted where it happens
                    # rather than skipped: see TestZeroLengthGuard.
                    deviations += 1
                    self.assertEqual(
                        canonical.encode_chars(text, self.char_to_id),
                        [canonical.UNK_ID])
                    continue
                self.assertEqual(
                    canonical.encode_chars(text, self.char_to_id), expected)
        # Report the denominator. If every input had hit the deviation branch
        # this test would have asserted nothing about equivalence at all.
        self.assertLess(deviations, len(CORPUS + self.random_corpus) // 10,
                        f"{deviations} of {len(CORPUS + self.random_corpus)} "
                        f"inputs produced no ids — the corpus is not "
                        f"exercising the equivalence it claims to")

    def test_lang_ids_match(self):
        tags = list(self.lang_to_id)[:200] + [
            "en", "EN", "En", " en ", "zh-Hant", "", None, "xyz"]
        for tag in tags:
            with self.subTest(tag=tag):
                self.assertEqual(canonical.encode_lang(tag, self.lang_to_id),
                                 self.lang_vocab.encode(tag))

    def test_script_ids_match(self):
        for script in script_detection_module.Script:
            with self.subTest(script=script):
                self.assertEqual(
                    canonical.encode_script(script.value, self.script_to_id),
                    self.script_vocab.encode(script))

    def test_out_of_table_ids_degrade_to_unk(self):
        """The vocab file carries 7 characters whose id is >= its own size.

        Both paths already fold these to <UNK>, but by different rules: this
        one against ``len(char_to_id)`` (a property of the vocab FILE), and
        ``hf.inference._sanitize_vocab_ids`` against the checkpoint's embedding
        table. They agree only while those two numbers are equal — 113280
        today, by coincidence, not by construction.
        """
        oob = [c for c, i in self.char_to_id.items() if i >= len(self.char_to_id)]
        self.assertTrue(oob, "fixture assumes the shipped vocab has out-of-table ids")

        # Two of the seven are normalised away before the lookup ever happens
        # (U+0343 COMBINING GREEK KORONIS becomes U+0313, and U+0F43 TIBETAN
        # LETTER GHA decomposes), so they reach a valid row instead. The rule
        # under test is the lookup, so it is tested on the characters that
        # actually reach it — and both subsets are asserted to be non-empty,
        # because "no character reached the rule" would otherwise pass.
        survives_nfc = [c for c in oob if unicodedata.normalize("NFC", c) == c]
        self.assertTrue(survives_nfc)
        self.assertTrue([c for c in oob if c not in survives_nfc])
        for char in survives_nfc:
            with self.subTest(char=char):
                self.assertEqual(
                    canonical.encode_chars(char, self.char_to_id),
                    [canonical.UNK_ID])


class TestDivergencesClosed(VocabFixture):
    """D1, D2 and D3 from the plan, each asserted on its own."""

    def test_d1_cjk_and_kana_are_romanised(self):
        self.assertEqual(canonical.preprocess_text("東京"), "dongjing")
        self.assertEqual(canonical.preprocess_text("北京"), "beijing")
        self.assertEqual(canonical.preprocess_text("トウキョウ"), "toukiyou")

    def test_d1_hangul_is_decomposed_to_jamo(self):
        self.assertEqual(canonical.preprocess_text("서울"), "ㅅㅓㅇㅜㄹ")

    def test_d1_everything_else_is_nfc_normalised(self):
        nfd = unicodedata.normalize("NFD", "Åre")
        self.assertNotEqual(nfd, "Åre")  # the fixture is genuinely decomposed
        self.assertEqual(canonical.encode_chars(nfd, self.char_to_id),
                         canonical.encode_chars("Åre", self.char_to_id))

    def test_d2_space_is_the_space_token_not_id_12588(self):
        literal_space_id = self.char_to_id.get(" ")
        self.assertEqual(literal_space_id, 12588,
                         "fixture assumes the vocab's literal-space row")
        ids = canonical.encode_chars("New York", self.char_to_id)
        self.assertIn(canonical.SPACE_ID, ids)
        self.assertNotIn(literal_space_id, ids)

    def test_d3_lang_tags_are_lowercased_and_stripped(self):
        self.assertEqual(canonical.encode_lang("EN", self.lang_to_id),
                         canonical.encode_lang("en", self.lang_to_id))
        self.assertEqual(canonical.encode_lang(" en ", self.lang_to_id),
                         canonical.encode_lang("en", self.lang_to_id))
        self.assertNotEqual(canonical.encode_lang("en", self.lang_to_id),
                            canonical.LANG_UNK_ID)


class TestZeroLengthGuard(VocabFixture):
    """The one deliberate deviation from ``CharacterVocabulary.encode``."""

    def test_input_that_reduces_to_nothing_yields_one_unk(self):
        for text in EMPTY_RESULT:
            with self.subTest(text=text):
                self.assertEqual(canonical.encode_chars(text, self.char_to_id),
                                 [canonical.UNK_ID])

    def test_the_vocabulary_class_returns_nothing_for_the_same_input(self):
        # States the deviation rather than hiding it: these are exactly the
        # inputs on which the equivalence above is deliberately not claimed.
        for text in EMPTY_RESULT:
            with self.subTest(text=text):
                self.assertEqual(self.char_vocab.encode(text), [])

    def test_a_run_of_real_spaces_is_still_spaces(self):
        # U+0020 is not "nothing": it is <SPACE>, id 2. The guard must not
        # swallow it.
        for text in SPACE_ONLY:
            with self.subTest(text=text):
                ids = canonical.encode_chars(text, self.char_to_id)
                self.assertEqual(set(ids), {canonical.SPACE_ID})
                self.assertEqual(ids, self.char_vocab.encode(text))

    def test_the_guard_cannot_touch_a_name_that_produces_any_id(self):
        for text in CORPUS:
            with self.subTest(text=text):
                self.assertEqual(canonical.encode_chars(text, self.char_to_id),
                                 self.char_vocab.encode(text))


class TestEveryEntryPointAgrees(VocabFixture):
    """The property Package 1 exists to establish."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hf = _load_hf_inference()

    def test_hf_exposes_the_canonical_tokeniser(self):
        self.assertTrue(hasattr(self.hf, "tokenise"),
                        "hf/inference.py no longer carries the vendored "
                        "tokeniser — the gateway is back on its own copy")

    def _hf_ids(self, text, lang):
        """Tokenise the way ``hf/inference.py`` actually does it.

        If the vendored function is absent, fall back to reconstructing what
        that file did before it was vendored — raw codepoints, raw lang
        lookup — rather than erroring out. Otherwise this suite's only failure
        against pre-change code would be ``AttributeError``: proof that the
        function is new, not that the ids it emits are different. The absence
        itself is asserted separately, just above.
        """
        if hasattr(self.hf, "tokenise"):
            return self.hf.tokenise(text, lang, self.char_to_id,
                                    self.lang_to_id, self.script_to_id)
        unk_char = self.char_to_id.get("<UNK>", 1)
        unk_lang = self.lang_to_id.get("<UNK>", 0)
        return (
            [self.char_to_id.get(ch, unk_char) for ch in text],
            self.script_to_id.get(self.hf._detect_script(text), 0),
            self.lang_to_id.get(lang, unk_lang),
        )

    def _encoder_ids(self, text, lang):
        """``ToponymEncoder._prepare_input`` — the path that writes the index.

        Built without weights on purpose: tokenisation must not need a model,
        and a test that had to load one would not run in CI.
        """
        from phonetics.inference.encoder import ToponymEncoder
        encoder = object.__new__(ToponymEncoder)
        encoder.char_vocab = self.char_vocab
        encoder.script_vocab = self.script_vocab
        encoder.lang_vocab = self.lang_vocab
        encoder._script_name_to_id = self.script_to_id
        return encoder._prepare_input(text, lang)

    def test_all_three_entry_points_emit_the_same_ids(self):
        for text in CORPUS + EMPTY_RESULT + SPACE_ONLY:
            for lang in ("en", "EN", "ja", None):
                with self.subTest(text=text, lang=lang):
                    expected = canonical.tokenise(
                        text, lang, self.char_to_id, self.lang_to_id,
                        self.script_to_id)
                    self.assertEqual(self._hf_ids(text, lang), expected)
                    self.assertEqual(self._encoder_ids(text, lang), expected)

    def test_they_agree_across_the_whole_vocabulary_too(self):
        for text in self.random_corpus[:500]:
            with self.subTest(text=text):
                expected = canonical.tokenise(
                    text, "en", self.char_to_id, self.lang_to_id,
                    self.script_to_id)
                self.assertEqual(self._hf_ids(text, "en"), expected)
                self.assertEqual(self._encoder_ids(text, "en"), expected)


class TestTheFixIsLoadBearing(VocabFixture):
    """What the pre-change gateway did, and where it differed.

    Without this class the suite could pass by comparing an implementation
    with itself. These tests fail if the divergence was never real, and the
    regression test below fails if the fix reaches further than it should.
    """

    def _legacy_char_ids(self, text):
        """``hf.inference._tokenise`` as it stood before 5 September 2026.

        Raw codepoints against the same vocabulary, with
        ``_sanitize_vocab_ids``' clamping folded in.
        """
        unk = canonical.UNK_ID
        ids = []
        for char in text:
            cid = self.char_to_id.get(char, unk)
            ids.append(cid if 0 <= cid < self.model_vocab_size else unk)
        return ids

    def test_the_affected_names_really_did_tokenise_differently(self):
        for text in AFFECTED:
            with self.subTest(text=text):
                self.assertNotEqual(
                    self._legacy_char_ids(text),
                    canonical.encode_chars(text, self.char_to_id),
                    f"{text!r} is in the affected set but the two "
                    f"implementations agree on it")

    def test_single_word_names_are_untouched_so_the_index_stands(self):
        # This is what says the other 94.7% of the index needs no re-embed.
        for text in UNAFFECTED:
            with self.subTest(text=text):
                self.assertEqual(
                    self._legacy_char_ids(text),
                    canonical.encode_chars(text, self.char_to_id))

    def test_the_gateway_used_to_emit_the_literal_space_row(self):
        self.assertIn(12588, self._legacy_char_ids("New York"))


if __name__ == "__main__":
    unittest.main()
