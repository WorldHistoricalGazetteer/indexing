"""Every Charsiu call site must bound generation, from one shared constant.

THE DEFECT THIS PINS. ByT5's generation config defaults to `max_length=20`, and
those are BYTE tokens — so `model.generate(**inputs)` with no length argument
silently truncates IPA at roughly 15 characters:

    首爾龍馬初等學校   ->  'ɕɯniɾjɯɯbaɕoto'      (want 'ɕɯniɾjɯɯbaɕotoɯgakːoɯ')
    南佛罗里达都会区   ->  'naɴbɯtsɯɾaɾitat'     (want 'naɴbɯtsɯɾaɾitatsɯtokaikɯ')

13 of 20 sampled long CJK names were affected. Nothing warns; the output is a
shorter string that still looks like an IPA transcription.

⚠ WHY IT SURVIVED. The repo had TWO Charsiu wrappers. `phonetics/ipa/backends.py`
was fixed during the Sept 2026 campaign — `CHARSIU_MAX_NEW_TOKENS = 256`;
`rebuild_toponyms_index` had its own copy and was not. That copy is the one the
corpus rebuild runs, so a re-extract would have NEWLY truncated every long name,
including the `yue` and Mandarin rows currently stored correctly — a repair that
makes the corpus worse while looking like progress: more rows, fresh
computation, no errors.

⚠ AND THE TEST IS DELIBERATELY NOT "does rebuild pass max_new_tokens". That
would pass against a third hard-coded copy, which is the same defect again. It
asserts every call site takes the value from the ONE shared constant, so a
future fix in one place cannot leave another behind.
"""
import ast
import pathlib
import unittest


#: Every module that constructs a CharsiuG2P call. There were THREE, not two:
#: the sweep that found the third was prompted by asking "is the rebuild the
#: only divergent copy?" rather than assuming the two known ones were all.
CALL_SITES = [
    "phonetics/ipa/backends.py",
    "phonetics/extraction/rebuild_toponyms_index.py",
    "phonetics/extraction/precompute_neural_phonetics.py",
]


class CharsiuLengthLimitTest(unittest.TestCase):

    def test_the_constant_exists_and_is_generous(self):
        from phonetics.ipa.backends import CHARSIU_MAX_NEW_TOKENS
        self.assertGreaterEqual(
            CHARSIU_MAX_NEW_TOKENS, 128,
            "ByT5 counts BYTES, so a CJK character costs ~3 tokens; a limit "
            "under ~128 truncates ordinary place names")

    def _generate_calls(self, path):
        """Every `.generate(...)` call in the file, via AST.

        Deliberately not a regex: the first draft of this test matched
        `max_new_tokens=128` inside backends.py's own DOCSTRING, where it is
        prose describing the history. A test that reads comments as code will
        fail on an accurate comment, which teaches people to delete comments.
        """
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "generate"):
                out.append(node)
        return out

    def test_every_generate_call_bounds_its_output(self):
        offenders = []
        for path in CALL_SITES:
            for call in self._generate_calls(path):
                kw = {k.arg for k in call.keywords if k.arg}
                if not kw & {"max_new_tokens", "max_length"}:
                    offenders.append(f"{path}:{call.lineno}")
        self.assertEqual(
            offenders, [],
            "unbounded .generate() — ByT5 defaults to max_length=20 and "
            "truncates silently: " + ", ".join(offenders))

    def test_no_call_site_hardcodes_its_own_limit(self):
        """A third hard-coded copy is the same defect wearing a fixed number."""
        offenders = []
        for path in CALL_SITES:
            for call in self._generate_calls(path):
                for k in call.keywords:
                    if k.arg == "max_new_tokens" and isinstance(k.value, ast.Constant):
                        offenders.append(f"{path}:{call.lineno} literal {k.value.value}")
        self.assertEqual(
            offenders, [],
            "max_new_tokens must come from CHARSIU_MAX_NEW_TOKENS, not a "
            "literal — two copies is how this defect survived: "
            + ", ".join(offenders))

    def test_rebuild_imports_the_shared_constant(self):
        src = pathlib.Path(
            "phonetics/extraction/rebuild_toponyms_index.py").read_text(encoding="utf-8")
        self.assertIn("CHARSIU_MAX_NEW_TOKENS", src)
        self.assertRegex(
            src, r"from phonetics\.ipa\.backends import \([^)]*CHARSIU_MAX_NEW_TOKENS",
            "the rebuild must import the constant rather than restate it")


if __name__ == "__main__":
    unittest.main()
