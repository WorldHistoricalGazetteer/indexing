"""Unit tests for the year-clamp behaviour in
``processing.gazetteer_temporal_extent`` (finding 4 from the OHM smoke run).

The clamp catches obvious upstream typos (e.g. OHM's ``end_date=20222``
for 2022) without distorting legitimate historical/contemporary content.
It is applied to **individual year readings** before they aggregate, so
a single bogus reading on one record can't poison the namespace extent.

Per-namespace overrides exist for sources that legitimately go deeper in
time — most importantly ``po`` (PeriodO), whose corpus contains
geological-epoch records (Hadean ~4.568 billion years ago).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from processing.gazetteer_temporal_extent import (
    DEFAULT_CLAMP_MIN,
    _NAMESPACE_CLAMP_OVERRIDES,
    _collect_extent_for_doc,
    _default_clamp_max,
    clamp_range_for,
)


def _doc_with_years(start: int | None = None, end: int | None = None) -> dict:
    ts = {}
    if start is not None:
        ts["start"] = {"in": start}
    if end is not None:
        ts["end"] = {"in": end}
    return {"toponyms": [{"toponym_id": "X@en", "timespans": [ts]}]}


class TestClampRangeFor(unittest.TestCase):
    def test_default_min_is_minus_10000(self):
        self.assertEqual(DEFAULT_CLAMP_MIN, -10_000)

    def test_default_max_is_current_year_plus_100(self):
        expected = datetime.now(timezone.utc).year + 100
        self.assertEqual(_default_clamp_max(), expected)

    def test_unknown_namespace_uses_defaults(self):
        lo, hi = clamp_range_for("unknown_ns")
        self.assertEqual(lo, DEFAULT_CLAMP_MIN)
        self.assertEqual(hi, _default_clamp_max())

    def test_po_uses_geological_override(self):
        lo, hi = clamp_range_for("po")
        # Must be wide enough to cover the Hadean (~4.568e9 years ago).
        self.assertLessEqual(lo, -4_600_000_000)
        # Upper bound stays modest — po doesn't need futuristic forecasts.
        self.assertLess(hi, 100_000)

    def test_po_override_present_in_module_constant(self):
        self.assertIn("po", _NAMESPACE_CLAMP_OVERRIDES)


class TestClampInDocCollector(unittest.TestCase):
    def test_in_range_years_pass_through(self):
        doc = _doc_with_years(start=1500, end=1900)
        lo, hi, rejected = _collect_extent_for_doc(
            doc, clamp_min=-10_000, clamp_max=2200,
        )
        self.assertEqual((lo, hi), (1500, 1900))
        self.assertEqual(rejected, 0)

    def test_typo_year_rejected(self):
        # OHM's actual outlier: end_date=20222 (typo for 2022).
        doc = _doc_with_years(start=1700, end=20222)
        lo, hi, rejected = _collect_extent_for_doc(
            doc, clamp_min=-10_000, clamp_max=2200,
        )
        self.assertEqual(lo, 1700)
        # The bogus end is dropped → no max_end remains for this doc.
        self.assertIsNone(hi)
        self.assertEqual(rejected, 1)

    def test_negative_outlier_rejected(self):
        doc = _doc_with_years(start=-99_999, end=1500)
        lo, hi, rejected = _collect_extent_for_doc(
            doc, clamp_min=-10_000, clamp_max=2200,
        )
        self.assertIsNone(lo)
        self.assertEqual(hi, 1500)
        self.assertEqual(rejected, 1)

    def test_geological_year_rejected_by_default_clamp(self):
        # po-style data WOULD be rejected if the default clamp were used —
        # this is precisely why po needs its override.
        doc = _doc_with_years(start=-4_567_998_050, end=3000)
        lo, hi, rejected = _collect_extent_for_doc(
            doc, clamp_min=-10_000, clamp_max=2200,
        )
        self.assertIsNone(lo)
        # 3000 is also out of [-10000, 2200], so end_max is also None.
        self.assertIsNone(hi)
        self.assertEqual(rejected, 2)

    def test_geological_year_accepted_under_po_clamp(self):
        po_min, po_max = clamp_range_for("po")
        doc = _doc_with_years(start=-4_567_998_050, end=3000)
        lo, hi, rejected = _collect_extent_for_doc(
            doc, clamp_min=po_min, clamp_max=po_max,
        )
        self.assertEqual(lo, -4_567_998_050)
        self.assertEqual(hi, 3000)
        self.assertEqual(rejected, 0)

    def test_multiple_timespan_locations_all_scanned(self):
        doc = {
            "geometries": [{"timespans": [{"start": {"in": 1500}}]}],
            "toponyms": [{"timespans": [{"start": {"in": 1400}}]}],
            "relations": [{"timespans": [{"end": {"in": 1900}}]}],
        }
        lo, hi, rejected = _collect_extent_for_doc(
            doc, clamp_min=-10_000, clamp_max=2200,
        )
        self.assertEqual(lo, 1400)  # earliest start across all three locations
        self.assertEqual(hi, 1900)
        self.assertEqual(rejected, 0)

    def test_no_timespans_returns_none_none_zero(self):
        doc = {"toponyms": [{"toponym_id": "X@en"}]}
        lo, hi, rejected = _collect_extent_for_doc(
            doc, clamp_min=-10_000, clamp_max=2200,
        )
        self.assertIsNone(lo)
        self.assertIsNone(hi)
        self.assertEqual(rejected, 0)

    def test_range_endpoint_shape_with_clamp(self):
        # Range-shape endpoint: {"start": {"earliestYear": X, "latestYear": Y}}
        # _iter_year_ints yields BOTH X and Y; clamp must drop only the
        # individual readings that are out of range.
        doc = {
            "toponyms": [{
                "timespans": [{
                    "start": {"earliestYear": -50_000, "latestYear": 1700},
                }]
            }],
        }
        lo, hi, rejected = _collect_extent_for_doc(
            doc, clamp_min=-10_000, clamp_max=2200,
        )
        # -50_000 rejected; 1700 accepted as the only in-range start reading.
        self.assertEqual(lo, 1700)
        self.assertIsNone(hi)
        self.assertEqual(rejected, 1)


if __name__ == "__main__":
    unittest.main()
