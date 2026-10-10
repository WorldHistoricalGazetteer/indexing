"""GeoNames alternate-name ``from``/``to`` dates reduce to a year, not a huge integer (place#288).

``parse_year`` was a bare ``int()``, so ``20260927`` became the year 20,260,927
and ``196411`` the year 196,411. The live ``gn`` toponyms carry 601 start years
above 2027 and 245 end years (241 above 100,000) from exactly this.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from processing.temporal import lifespan

_spec = importlib.util.spec_from_file_location(
    "geonames_toponyms",
    str(Path(__file__).resolve().parent.parent / "authorities" / "geonames-toponyms.py"),
)
gnt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gnt)


class ParseYearTest(unittest.TestCase):
    def test_bare_years_unchanged(self):
        self.assertEqual(gnt.parse_year("1950"), 1950)
        self.assertEqual(gnt.parse_year(" 1950 "), 1950)
        self.assertEqual(gnt.parse_year("-500"), -500)
        self.assertEqual(gnt.parse_year("800"), 800)

    def test_yyyymmdd_reduces_to_year(self):
        self.assertEqual(gnt.parse_year("20260927"), 2026)
        self.assertEqual(gnt.parse_year("19641105"), 1964)

    def test_yyyymm_reduces_to_year(self):
        self.assertEqual(gnt.parse_year("196411"), 1964)

    def test_hyphenated_dates_reduce_to_year(self):
        self.assertEqual(gnt.parse_year("1964-11"), 1964)
        self.assertEqual(gnt.parse_year("1964-11-05"), 1964)

    def test_empty_and_junk_are_none(self):
        for v in (None, "", "   ", "abc", "1964-13", "19641305", "196413", "12345", "20260927x"):
            self.assertIsNone(gnt.parse_year(v), v)

    def test_no_result_is_implausibly_large(self):
        for v in ("20260927", "196411", "19001231", "1999", "2026-09-27"):
            self.assertLess(abs(gnt.parse_year(v)), 3000, v)

    def test_alternate_name_line_dates_a_name_by_year(self):
        # alternateNamesV2 columns: id, geonameid, isolang, name, preferred, short, colloquial, historic, from, to
        line = "1\t12345\ten\tSome Name\t0\t\t\t1\t196411\t20260927"
        kind, lst, ts, _pref, pid = gnt.parse_alternatename_line(line)
        self.assertEqual(kind, "toponym")
        self.assertEqual(ts, lifespan(1964, 2026))
        self.assertEqual(pid, "gn:12345")


if __name__ == "__main__":
    unittest.main()
