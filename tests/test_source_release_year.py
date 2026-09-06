"""One implementation of the release-year convention, and its fallback must speak.

There were three private copies — `authorities/osm-places.py`,
`processing/tgn_temporal.py`, `authorities/nativeland-places.py` — and they are
exactly the three namespaces whose live records carry a uniform attestation
year. Three copies of a convention is three places for it to drift, and the
drift is silent: a wrong year here does not fail, it attests every record of a
namespace to a year the source never claimed.
"""
import re
import unittest
from datetime import datetime
from pathlib import Path

from processing.temporal import attested_at, source_release_year

REPO = Path(__file__).resolve().parents[1]


class SourceReleaseYearTest(unittest.TestCase):

    def test_reads_the_release_mtime(self):
        import tempfile, os, time
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as fh:
            path = fh.name
        os.utime(path, (0, time.mktime((2019, 6, 1, 0, 0, 0, 0, 0, -1))))
        self.assertEqual(source_release_year(path, quiet=True), 2019)
        os.unlink(path)

    def test_fallback_announces_itself(self):
        """MUTATION: the dangerous path must not be silent.

        Falling back to today asserts 'attested now' for a release that may be
        years old — wrong in the direction nobody checks, because it makes the
        data look fresher than it is.
        """
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            year = source_release_year("/nonexistent/release.zip", label="test")
        self.assertEqual(year, datetime.now().year)
        self.assertIn("falling back", buf.getvalue())
        self.assertIn("test", buf.getvalue())

    def test_quiet_suppresses_only_the_message_not_the_answer(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            year = source_release_year(None, quiet=True)
        self.assertEqual(year, datetime.now().year)
        self.assertEqual(buf.getvalue(), "")

    def test_output_is_consumable_by_attested_at(self):
        self.assertEqual(attested_at(source_release_year(None, quiet=True)),
                         [{"start": {"latest": datetime.now().year},
                           "end": {"earliest": datetime.now().year}}])

    def test_no_private_current_year_fallbacks_remain(self):
        """The point of the exercise. Fails if a fourth copy is ever added.

        `authorities/*.py` are scripts with hyphens in their names and cannot be
        imported, so this reads source — which is also the right level, because
        the thing being asserted is that no module rolls its own.
        """
        offenders = []
        for path in list((REPO / "authorities").glob("*.py")) + \
                    list((REPO / "processing").glob("*.py")):
            if path.name == "temporal.py":
                continue                      # the one legitimate home
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"datetime\.now\(\)\.year|date\.today\(\)\.year", text):
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(REPO)}:{line}")
        self.assertEqual(offenders, [],
                         "private current-year fallback(s) outside "
                         "processing/temporal.py: " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
