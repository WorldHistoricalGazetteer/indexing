"""The un-BNDA backfill must not skip a namespace whose ``final/`` is JSONL-only.

``backfill_uncoded_ccodes`` read ``final/places.parquet`` and nothing else.
The parquet sidecar is **best-effort**: ``write_parquet_from_jsonl`` returns
False rather than raising when pyarrow's schema inference fails (ragged
GeoJSON ``coordinates``, mixed LPF timespan types), leaving a complete
canonical JSONL and no sidecar. Every other consumer in the repo reads
"parquet if it exists, else jsonl" for that reason — this one did not.

The cost, from the 31 August audit §3b: ``ukhc`` is the one namespace in that
shape, so it was excluded from the backfill **entirely** — not discovered by
``_namespaces`` (which filtered on the parquet), and contributing zero rows
with no error when named explicitly. ``ukhc`` is a historic-county boundary
authority: coastal polygons are exactly the population tier 2 exists to
resolve, since a representative point a few metres seaward is what leaves a
place uncoded in the first place.

Two distinct failures are covered, because fixing only the first would leave
the more dangerous one:

1. **Silent exclusion** — the JSONL-only namespace is neither discovered nor
   read. Fixed by reading either file.
2. **Silent zero** — a namespace named explicitly with no readable ``final/``
   contributed nothing and said nothing, because the per-namespace log line
   is gated on ``uncoded > 0``. A namespace that scanned nothing and one that
   scanned everything and found no work printed identically. That is this
   campaign's signature defect (absent input treated as nothing-to-do), and
   it is the one that turns a missing input into a clean bill of health.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

try:                            # package-qualified run (tests/__init__.py ran)
    from ._sandbox import assert_sandboxed
except ImportError:             # `discover -s tests` puts tests/ on sys.path
    from _sandbox import assert_sandboxed


def _doc(place_id: str, ccodes: list[str]) -> dict:
    return {
        "place_id": place_id,
        "namespace": place_id.split(":")[0],
        "title": place_id.split(":")[-1].upper(),
        "ccodes": ccodes,
        "geometries": [{
            "geometry_index": 0,
            "repr_point": {"type": "Point", "coordinates": [-1.5, 53.0]},
        }],
    }


class BackfillReadsJsonlOnlyFinal(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        assert_sandboxed()
        from processing.settings import STAGED_BASE_DIR
        cls.staged_base = Path(STAGED_BASE_DIR)

    def setUp(self):
        # ukhc: JSONL-only final/, the real shape.
        self.ukhc = self.staged_base / "ukhc"
        if self.ukhc.exists():
            shutil.rmtree(self.ukhc)
        (self.ukhc / "final").mkdir(parents=True)
        self.ukhc_rows = [_doc("ukhc:1", []), _doc("ukhc:2", ["GB"])]
        with (self.ukhc / "final" / "places.jsonl").open("w", encoding="utf-8") as fh:
            for row in self.ukhc_rows:
                fh.write(json.dumps(row, ensure_ascii=True) + "\n")

        # pl: an ordinary namespace with both files, as a control.
        self.pl = self.staged_base / "pl"
        if self.pl.exists():
            shutil.rmtree(self.pl)
        (self.pl / "final").mkdir(parents=True)
        pl_rows = [_doc("pl:1", ["GR"])]
        pq.write_table(pa.Table.from_pylist(pl_rows),
                       str(self.pl / "final" / "places.parquet"))

        # nothing/: a namespace with no final/ at all.
        self.nothing = self.staged_base / "nothing"
        if self.nothing.exists():
            shutil.rmtree(self.nothing)
        self.nothing.mkdir(parents=True)

    def tearDown(self):
        for path in (self.ukhc, self.pl, self.nothing):
            if path.exists():
                shutil.rmtree(path)

    # -- 1. silent exclusion -------------------------------------------------

    def test_jsonl_only_namespace_is_read(self):
        """Pre-change this yields nothing at all, silently."""
        from processing.backfill_uncoded_ccodes import _iter_final

        got = list(_iter_final("ukhc"))
        self.assertEqual(
            len(self.ukhc_rows), len(got),
            "a JSONL-only final/ must still be read — the parquet sidecar is "
            "best-effort and ukhc has none",
        )
        self.assertEqual({"ukhc:1", "ukhc:2"}, {d["place_id"] for d in got})

    def test_jsonl_only_namespace_is_discovered(self):
        """Pre-change ukhc is absent from the default namespace list."""
        from processing.backfill_uncoded_ccodes import _namespaces

        discovered = _namespaces(None)
        self.assertIn(
            "ukhc", discovered,
            "default discovery filtered on final/places.parquet, so the one "
            "JSONL-only namespace was never even considered",
        )
        self.assertIn("pl", discovered, "control namespace should still appear")
        self.assertNotIn("nothing", discovered,
                         "a namespace with no final/ at all is correctly skipped")

    def test_parquet_is_still_preferred_when_both_exist(self):
        """The sidecar stays the fast path; this must not become JSONL-first."""
        from processing.backfill_uncoded_ccodes import _final_source

        with (self.pl / "final" / "places.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(_doc("pl:1", ["GR"]), ensure_ascii=True) + "\n")
        self.assertEqual("places.parquet", _final_source("pl").name)

    def test_absent_final_still_resolves_to_nothing(self):
        from processing.backfill_uncoded_ccodes import _final_source

        self.assertIsNone(_final_source("nothing"))

    # -- 2. silent zero ------------------------------------------------------

    def test_named_namespace_without_a_snapshot_is_an_error(self):
        """Refuse to report a zero contribution for something never read.

        The per-namespace log line is gated on uncoded>0, so pre-change a
        namespace that scanned nothing was indistinguishable in the output
        from one that scanned everything and found no work.
        """
        import sys
        from unittest import mock
        from processing import backfill_uncoded_ccodes as mod

        out = self.staged_base / "patch.jsonl"
        argv = ["backfill", "--namespaces", "nothing", "--out", str(out)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(mod, "load_full_bnda_tier",
                                  lambda: [object()]), \
                mock.patch.object(mod, "BndaFallbackIndex", lambda e: e):
            rc = mod.main()

        self.assertEqual(1, rc,
                         "a namespace named explicitly but never read must be "
                         "an error, not a silent zero contribution")


if __name__ == "__main__":
    unittest.main()
