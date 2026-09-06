"""The EXTRACTION LOOP must keep source-declared romanisations (#250).

WHY THIS TEST EXISTS RATHER THAN THE ONE NEXT TO IT.
`tests/test_script_mismatch_declared.py` calls `is_script_mismatch("zh-Latn", …)`
directly and passes. It was passing while the production path dropped every
declared romanisation, because the extraction loop base-split the tag one line
ABOVE the call:

    parts = lang_part.split('-', 1)
    lang  = parts[0]                     # 'zh' — the declaration is gone
    ...
    if is_script_mismatch(lang, script): # never sees 'zh-Latn'

So `_has_script_subtag` received `'zh'`, returned False, and the #250 exemption
was unreachable code. The commit that added it repaired the test and left the
line above it — the same shape as the defect it was fixing.

⚠ It cost a 1h16m corpus rebuild (job 11170354, cancelled at 4.7% of STEP 3),
and the run's own mismatch breakdown could not reveal it: the counter keyed on
the base tag too, so a filtered `zh-Latn` was reported as `zh:LATIN`, identical
to a legitimately filtered bare `zh`. The breakdown read the same whether the
bug was present or not — a measurement that cannot discriminate, which was then
read as evidence the fix worked.

This test therefore drives `extract_toponyms_to_db` itself — the real loop, real
parsing, real DuckDB — and asserts on what reaches the table. A unit test of the
predicate cannot see this class of defect at all.
"""
import tempfile
import unittest
from pathlib import Path

try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False

from phonetics.extraction import rebuild_toponyms_index as R


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not installed")
class ExtractionKeepsDeclaredScriptTest(unittest.TestCase):

    #: (toponym_id, must_survive, why)
    #:
    #: ⚠ Names are distinct per case ON PURPOSE. The loop canonicalises
    #: `X@zh-Latn` to `X@zh` (base lang; the subtag moves to `lang_variant`), so
    #: a declared and an undeclared form of the SAME name collapse to one id and
    #: the two cases become indistinguishable. The first draft of this test used
    #: `Beijing` for both and reported the undeclared one as surviving when what
    #: it had actually found was the declared one.
    CASES = [
        ("北京@zh",                          True,  "native script, obviously kept"),
        ("Beijing@zh",                       False, "UNdeclared romanisation — the rule's real target"),
        ("Shanghai@zh-Latn",                 True,  "DECLARED romanisation (#250)"),
        ("Pei-ching@zh-Latn-pinyin-x-notone", True, "declared, with extensions after the subtag"),
        ("Tehran@fa-Latn",                   True,  "declared, non-CJK"),
        ("Tokyo@ja-Latn",                    True,  "declared, Japanese"),
        ("Moskva@ru",                        False, "undeclared Cyrillic romanisation"),
        ("London@en",                        True,  "Latin language, Latin script"),
        ("Lhasa@bo-Latn",                    True,  "bo absent from LANG_EXPECTED_SCRIPTS — the control"),
    ]

    def _run_extraction(self):
        docs = [{"place_id": f"tgn:{i}", "namespace": "tgn",
                 "toponyms": [{"toponym_id": tid}]}
                for i, (tid, _, _) in enumerate(self.CASES)]

        def fake_scan(namespaces):
            for d in docs:
                yield d["place_id"], {"namespace": "tgn", "toponyms": d["toponyms"]}

        orig_scan, orig_count = R.scan_places_staged, R._count_staged_places
        R.scan_places_staged = fake_scan
        R._count_staged_places = lambda ns: len(docs)
        try:
            tmp = Path(tempfile.mkdtemp()) / "t.db"
            conn = R.create_db(str(tmp))
            R.extract_toponyms_to_db(conn, ["tgn"], batch_size=100)
            rows = conn.execute(
                "SELECT toponym_id, lang, lang_variant, script FROM toponyms").fetchall()
            skipped = conn.execute(
                "SELECT toponym_id, reason FROM skipped_toponyms").fetchall()
            conn.close()
            self._rows = rows
            return {r[0] for r in rows}, {s[0] for s in skipped}
        finally:
            R.scan_places_staged, R._count_staged_places = orig_scan, orig_count

    def _run_extraction_rows(self):
        self._run_extraction()
        return self._rows

    def test_declared_romanisations_survive_extraction(self):
        survived, skipped = self._run_extraction()

        failures = []
        for tid, must_survive, why in self.CASES:
            name, _, tag = tid.rpartition("@")
            # the loop canonicalises to name@<base-lang>
            base = tag.split("-", 1)[0]
            canonical = f"{name}@{base}"
            present = canonical in survived
            if present != must_survive:
                failures.append(
                    f"{tid!r} ({why}): expected "
                    f"{'KEPT' if must_survive else 'FILTERED'}, got "
                    f"{'KEPT' if present else 'FILTERED'}")

        self.assertEqual(failures, [], "\n  " + "\n  ".join(failures) if failures else "")

    def test_declared_subtag_lands_in_lang_variant_not_lang(self):
        """Pins the id shape the acceptance test must use.

        `Shanghai@zh-Latn` is stored as lang='zh', lang_variant='Latn' — NOT as
        lang='zh-Latn'. So the post-rebuild check for recovered romanisations is
        `lang='zh' AND script=LATIN` (0 before the fix), not `lang='zh-Latn'`,
        which would be 0 whether the fix worked or not.
        """
        rows = self._run_extraction_rows()
        by_id = {r[0]: r for r in rows}
        self.assertIn("Shanghai@zh", by_id, "declared form should be canonicalised to base lang")
        _, lang, variant, script = by_id["Shanghai@zh"]
        self.assertEqual(lang, "zh")
        self.assertEqual(variant, "Latn")
        self.assertEqual(script, "LATIN")

    def test_the_rule_still_filters_undeclared_forms(self):
        """A test that only proved things survive would pass with the rule deleted."""
        survived, skipped = self._run_extraction()
        self.assertIn("Beijing@zh", skipped,
                      "undeclared zh romanisation should still be filtered — "
                      "otherwise this fix has simply disabled the rule")
        self.assertIn("Moskva@ru", skipped)


if __name__ == "__main__":
    unittest.main()
