"""Both-directions proof for ``generate_tiles.publish_gate``.

The gate is the structural replacement for the longitude-span assertion that
was **withdrawn 2 Sep 2026** — a naive ``max(lon) - min(lon) > 180`` flags six
legitimate ``un`` countries (``ata``/``rus``/``fji`` at 360.00, ``usa`` 358.93,
``nzl`` 355.47, ``kir`` 348.57) and *also* misses the real 232.63° Spanish
Empire smear once longitudes are normalised. It failed in both directions,
which is worse than no check because it manufactures confidence.

The gate that replaced it shipped in ``007a870`` and ran across the whole
3 Sep retile — 73 tilesets, 0 refusals — but **it had no test**, so it had
only ever been observed to PASS. A check proved only to pass is exactly the
7 August failure it was written to prevent: every check in place that day
passed, because each was satisfied equally by the broken world.

So each test below pairs a known-bad input the gate MUST refuse with a
known-good one it MUST admit. Proving only the refusal half is how a check
that cannot discriminate gets adopted (see ``developer/postmortem-ingestion-faults.md``,
Class C).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from processing.generate_tiles import publish_gate


def _mbtiles(path: Path, tile_count: int) -> Path:
    """Write a minimal mbtiles carrying ``tile_count`` rows."""
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
            "tile_row INTEGER, tile_data BLOB)"
        )
        con.executemany(
            "INSERT INTO tiles VALUES (?,?,?,?)",
            [(0, 0, i, b"x") for i in range(tile_count)],
        )
    return path


class PublishGateTests(unittest.TestCase):
    """Every refusal must fire on known-bad AND stay silent on known-good."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.good = _mbtiles(self.tmp / "good.mbtiles", 128)
        self.addCleanup(self._tmp.cleanup)

    # -- refusal 1: total geom-store miss (the 7 August failure exactly) ----

    def test_total_store_miss_is_refused(self) -> None:
        ok, reasons = publish_gate(
            "clio", {"store_attempt": 2986, "store_hit": 0}, self.good
        )
        self.assertFalse(ok)
        self.assertTrue(any("geom store returned nothing" in r for r in reasons))

    def test_store_hits_are_admitted(self) -> None:
        """The same shape with hits must PASS — the half that is usually skipped."""
        ok, reasons = publish_gate(
            "clio", {"store_attempt": 2986, "store_hit": 2986}, self.good
        )
        self.assertTrue(ok, reasons)

    def test_point_only_namespace_cannot_trip_the_store_refusal(self) -> None:
        """A genuinely point-only bucket makes no attempt, so 0/0 is not a miss.

        Without this the gate would refuse every point-only gazetteer, which is
        the false-positive half that killed the span assertion.
        """
        ok, reasons = publish_gate(
            "dp", {"store_attempt": 0, "store_hit": 0, "point": 4363}, self.good
        )
        self.assertTrue(ok, reasons)

    # -- refusal 2: a store miss that degraded to hulls, not to points ------

    def test_unexpected_hull_is_refused(self) -> None:
        ok, reasons = publish_gate(
            "clio",
            {"store_attempt": 2986, "store_hit": 2900, "hull": 86},
            self.good,
        )
        self.assertFalse(ok)
        self.assertTrue(any("inline hull fallback" in r for r in reasons))

    def test_hull_is_admitted_on_whg_buckets(self) -> None:
        """``whg-*`` approximations are deliberate, so hulls there are expected."""
        ok, reasons = publish_gate(
            "whg-1052",
            {"store_attempt": 100, "store_hit": 90, "hull": 10},
            self.good,
        )
        self.assertTrue(ok, reasons)

    def test_zero_hulls_is_admitted(self) -> None:
        ok, reasons = publish_gate(
            "clio",
            {"store_attempt": 2986, "store_hit": 2986, "hull": 0},
            self.good,
        )
        self.assertTrue(ok, reasons)

    # -- refusal 3: empty or unreadable tileset (tile-join drops + exits 0) --

    def test_empty_tileset_is_refused(self) -> None:
        empty = _mbtiles(self.tmp / "empty.mbtiles", 0)
        ok, reasons = publish_gate("clio", {"store_attempt": 1, "store_hit": 1}, empty)
        self.assertFalse(ok)
        self.assertTrue(any("0 tiles" in r for r in reasons))

    def test_unreadable_tileset_is_refused(self) -> None:
        missing = self.tmp / "nope.mbtiles"
        ok, reasons = publish_gate("clio", {"store_attempt": 1, "store_hit": 1}, missing)
        self.assertFalse(ok)
        self.assertTrue(any("unreadable" in r for r in reasons))

    # -- the withdrawn span assertion must NOT come back -------------------

    def test_circumpolar_extent_is_not_itself_a_refusal(self) -> None:
        """A legitimately antimeridian-crossing bucket must still publish.

        ``un`` contains six countries spanning ~360° of longitude. The
        withdrawn span assertion refused all six. The gate reasons about the
        TIER that produced each feature, not about the extent of the result,
        so a correct circumpolar tileset passes.
        """
        ok, reasons = publish_gate(
            "un", {"store_attempt": 247, "store_hit": 247, "hull": 0}, self.good
        )
        self.assertTrue(ok, reasons)

    def test_all_three_refusals_can_fire_together(self) -> None:
        empty = _mbtiles(self.tmp / "e2.mbtiles", 0)
        ok, reasons = publish_gate(
            "clio", {"store_attempt": 10, "store_hit": 0, "hull": 10}, empty
        )
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 3, reasons)


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
