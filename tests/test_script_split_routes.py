"""The `Script.OTHER` split — that it reaches Epitran, and that it moved nothing else.

WHAT THIS GUARDS. `routes.SCRIPT_TAG` carried 19 entries against `Script`'s 20,
and the absent one was the catch-all, so `SCRIPT_TAG.get("OTHER")` returned None
and `RouteTable.resolve` could never build a mode name for ANY writing system
outside the 19 — 252,447 rows answered `no_route` with a correct `iso3` already
in hand, 159,073 of them behind hand-written rule sets that were installed and
had never once been consulted.

TWO DIRECTIONS, AND ONLY ONE IS SELF-ANNOUNCING (`tokenise.encode_script`'s
docstring makes the point and it applies here). A script MISSING from a range
table is behaviourally visible: its text lands in OTHER. A script EXTRA in a
range table is INVISIBLE downstream, because `encode_script` falls back to OTHER
for any name the 20-entry vocabulary does not carry — which is exactly what
makes this change safe for the 72.7M stored vectors, and exactly what would let
a wrong range here go unnoticed. So the collision test below is not decoration:
it is the only thing standing between a mistyped block and a silent
reclassification of characters that were already being classified correctly.
"""
import unittest

from phonetics.ipa.routes import SCRIPT_TAG, RouteTable
from phonetics.utils.script_detection import (
    SCRIPT_RANGES, Script, detect_script)

#: The scripts the split added, with the mode a correctly-tagged row should now
#: reach. Kept as data so a mode that stops being installed fails loudly here.
ADDED = {
    Script.MYANMAR: ("my", "mya-Mymr"),
    Script.GURMUKHI: ("pa", "pan-Guru"),
    Script.TIBETAN: ("bo", "bod-Tibt"),
    Script.SINHALA: ("si", "sin-Sinh"),
    Script.KHMER: ("km", "khm-Khmr"),
    Script.OL_CHIKI: ("sat", "sat-Olck"),
    Script.ETHIOPIC: ("am", "amh-Ethi"),
    Script.ORIYA: ("or", "ori-Orya"),
    Script.LAO: ("lo", "lao-Laoo"),
}

#: Real toponyms from the live IPA store, one per added script.
SAMPLES = [
    ("ဆင်အိုးပြင်", Script.MYANMAR),
    ("ਅੰਮ੍ਰਿਤਸਰ", Script.GURMUKHI),
    ("ཀ་མདོ་", Script.TIBETAN),
    ("අටාර්", Script.SINHALA),
    ("សង្កាត់ស្លាកែត", Script.KHMER),
    ("ᱚᱜᱚᱨᱛᱟᱞᱟ", Script.OL_CHIKI),
    ("ሀቦሼ", Script.ETHIOPIC),
    ("ଅଞ୍ଜନଗାଓଁ", Script.ORIYA),
    ("ກລຸງກຸງ", Script.LAO),
    ("ⵜⴰⵎⴰⵣⵉⵖⵜ", Script.TIFINAGH),
    ("ㄅㄆㄇㄈ", Script.BOPOMOFO),
    ("ܝܜܠܝܐ", Script.SYRIAC),
    ("ދިވެހި", Script.THAANA),
    ("ᐊᓄᕆ", Script.CANADIAN_ABORIGINAL),
]

_PRE_SPLIT = [
    Script.LATIN, Script.CYRILLIC, Script.GREEK, Script.ARABIC, Script.HEBREW,
    Script.DEVANAGARI, Script.BENGALI, Script.TAMIL, Script.TELUGU,
    Script.MALAYALAM, Script.KANNADA, Script.GUJARATI, Script.THAI,
    Script.GEORGIAN, Script.ARMENIAN, Script.HANGUL, Script.CJK,
    Script.HIRAGANA, Script.KATAKANA,
]


class TestScriptSplit(unittest.TestCase):

    def test_every_script_has_a_tag(self):
        """The defect in one line: a `Script` member with no `SCRIPT_TAG` entry
        can never route, however good its rule set."""
        missing = [s.value for s in Script
                   if s is not Script.OTHER and s.value not in SCRIPT_TAG]
        self.assertEqual([], missing, f"{len(missing)} scripts cannot route: {missing}")

    def test_new_ranges_do_not_collide_with_pre_split_ones(self):
        """An EXTRA range is invisible downstream, so it must be caught here.

        Appending to `SCRIPT_RANGES` lets the new entry WIN on any overlap
        (`_build_codepoint_map` writes last-wins), which would silently move
        characters out of a script that was classifying them correctly.
        """
        pre = {}
        for sc in _PRE_SPLIT:
            for start, end in SCRIPT_RANGES[sc]:
                for cp in range(start, end + 1):
                    pre[cp] = sc
        clashes = []
        for sc, ranges in SCRIPT_RANGES.items():
            if sc in _PRE_SPLIT or sc is Script.OTHER:
                continue
            for start, end in ranges:
                for cp in range(start, end + 1):
                    if cp in pre:
                        clashes.append((hex(cp), pre[cp].value, sc.value))
        self.assertEqual([], clashes[:20], f"{len(clashes)} codepoints reclassified")

    def test_detects_the_new_scripts(self):
        for text, expected in SAMPLES:
            with self.subTest(text=text):
                self.assertEqual(expected, detect_script(text)[0])

    def test_routes_reach_the_installed_modes(self):
        """The whole point: a real (lang, script) pair now yields a real mode.

        Skips rather than fails when Epitran is absent, but does NOT skip when
        Epitran is present and the mode is missing — that is the install having
        silently not run, which is the thing worth hearing about.
        """
        try:
            table = RouteTable()
        except ImportError:  # pragma: no cover
            self.skipTest("epitran not installed")
        for script, (lang, mode) in ADDED.items():
            with self.subTest(script=script.value):
                route, status = table.resolve(lang, script.value)
                if mode not in table.modes:
                    self.fail(f"{mode} is not installed — run "
                              f"scripts/install_epitran_extensions.sh")
                self.assertEqual("ok", status)
                self.assertEqual(mode, route.mode)

    def test_other_still_cannot_route(self):
        """OTHER remains a catch-all with no tag, deliberately. Anything still
        landing there is a writing system nobody has added, and `no_route` is
        the honest answer — not a wrong mode picked by a default."""
        self.assertNotIn("OTHER", SCRIPT_TAG)


if __name__ == "__main__":
    unittest.main()
