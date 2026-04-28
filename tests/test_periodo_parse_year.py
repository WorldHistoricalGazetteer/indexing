"""Unit tests for authorities/periodo-places.py::_parse_year.

Confirms the parser handles every shape PeriodO actually emits under
``period.start.in`` / ``period.stop.in``:

* ``{'year': '<signed-zero-padded-str>'}`` — the common single-point shape.
* ``{'earliestYear': X, 'latestYear': Y}`` — the uncertain-range shape, which
  must be resolved with ``prefer='earliest'`` for ``start`` nodes and
  ``prefer='latest'`` for ``stop`` nodes so the recorded timespan covers the
  period's actual extent.
* Bare strings/ints (defensive — older snapshots or hand-written tests).

Regression target: prior to the fix _parse_year called ``int(str(value))`` on
the raw dict and silently returned None for every PeriodO record, leaving
``timespans`` empty everywhere.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PERIODO_PATH = _REPO_ROOT / "authorities" / "periodo-places.py"

_spec = importlib.util.spec_from_file_location("periodo_places", _PERIODO_PATH)
_periodo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_periodo)
_parse_year = _periodo._parse_year


class TestParseYear(unittest.TestCase):
    def test_year_dict_signed_zero_padded(self):
        self.assertEqual(_parse_year({"year": "-0585"}), -585)
        self.assertEqual(_parse_year({"year": "-3499"}), -3499)
        self.assertEqual(_parse_year({"year": "1492"}), 1492)
        self.assertEqual(_parse_year({"year": "+0500"}), 500)

    def test_range_dict_prefers_earliest_for_start(self):
        node = {"earliestYear": "-1799", "latestYear": "-1749"}
        self.assertEqual(_parse_year(node, prefer="earliest"), -1799)

    def test_range_dict_prefers_latest_for_stop(self):
        node = {"earliestYear": "-1499", "latestYear": "-1400"}
        self.assertEqual(_parse_year(node, prefer="latest"), -1400)

    def test_bare_values(self):
        self.assertEqual(_parse_year("1492"), 1492)
        self.assertEqual(_parse_year(1492), 1492)
        self.assertEqual(_parse_year("-585"), -585)
        self.assertEqual(_parse_year("+500"), 500)

    def test_none_and_empty(self):
        self.assertIsNone(_parse_year(None))
        self.assertIsNone(_parse_year(""))
        self.assertIsNone(_parse_year({}))

    def test_unparseable(self):
        self.assertIsNone(_parse_year("not-a-year"))
        self.assertIsNone(_parse_year({"year": "garbage"}))
        self.assertIsNone(_parse_year(object()))


if __name__ == "__main__":
    unittest.main()
