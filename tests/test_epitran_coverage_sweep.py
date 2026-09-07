"""The coverage sweep must detect a whole uncovered block.

Regression test for the defect the tool exists to find, and for the one its own
first implementation had: 9d's initial mechanical heuristic gated on the primary
block being >=50% covered, which silently EXCLUDED cop-Copt -- the case it was
written for -- because the Coptic block is padded with 77 Old Coptic and
cryptogrammic variants nobody intends to transcribe. A padded denominator makes a
percentage the wrong instrument, so the gate is an absolute count.
"""
import subprocess, sys, tempfile, unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "phonetics" / "epitran_extensions" / "coverage_sweep.py"


def _sweep(d: Path) -> str:
    r = subprocess.run([sys.executable, str(TOOL), str(d)], capture_output=True, text=True)
    return r.stdout + r.stderr


class TestCoverageSweep(unittest.TestCase):

    def test_flags_a_wholly_uncovered_secondary_block(self):
        """Positive control: a Coptic set with ONLY the U+2C80 block must flag.

        The seven Demotic-derived letters live at U+03E2-U+03EF. A rule set that
        walks the Coptic block alone omits exactly them and looks exhaustive.
        """
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            rows = ["Orth,Phon"] + [f"{chr(c)},a" for c in range(0x2C80, 0x2CB0, 2)]
            (d / "cop-Copt.csv").write_text("\n".join(rows), encoding="utf-8")
            out = _sweep(d)
            self.assertIn("cop-Copt", out)
            self.assertIn("MECHANICAL", out,
                          "a set covering only the secondary block was not flagged")

    def test_does_not_flag_a_complete_set(self):
        """Negative control -- without this, a tool that flagged everything would pass.

        ⚠ The first version of this control was WRONG and the tool was right. It
        covered only U+0561-U+0586 and expected no flag, but Armenian's Script
        property also includes the five ligatures at U+FB13-FB17, in Alphabetic
        Presentation Forms -- a real gap 9d flagged independently in the live
        hye-Armn set. The tool correctly reported a wholly uncovered secondary
        block; my premise that the set was complete was the error. Covering by
        Script property rather than by block is the entire point of the tool, and
        this control failed to honour it.
        """
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            codepoints = list(range(0x0561, 0x0587)) + list(range(0xFB13, 0xFB18))
            rows = ["Orth,Phon"] + [f"{chr(c)},a" for c in codepoints]
            (d / "hye-Armn.csv").write_text("\n".join(rows), encoding="utf-8")
            out = _sweep(d)
            self.assertNotIn("MECHANICAL", out,
                             "a genuinely complete set was wrongly flagged")

    def test_runs_over_the_repo_and_reports_a_denominator(self):
        repo = Path(__file__).resolve().parent.parent
        out = _sweep(repo / "phonetics" / "epitran_extensions")
        self.assertRegex(out, r"\d+ rule set\(s\)", "no denominator reported")


if __name__ == "__main__":
    unittest.main()
