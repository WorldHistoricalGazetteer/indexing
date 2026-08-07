"""A tileset with holes over land must fail the build.

place#160: `ohm` shipped missing `0/0/0` (the entire world), both z1
northern-hemisphere tiles, and central Europe at z3. `tile-join` drops an
over-large tile and still exits 0, so every stage reported success and the
voids were found by eye in the Atlas.

The two things worth pinning here are the ones that would make the check
useless rather than merely imperfect:

* MBTiles stores `tile_row` in TMS order (flipped). Getting that conversion
  backwards reports every tile missing — a check that always fails is ignored
  as fast as one that never does.
* Ocean and polar tiles are legitimately absent from a boundary tileset, so
  the check must key on land.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path


def _make_mbtiles(path: Path, tiles: set[tuple[int, int, int]]) -> None:
    """Write an mbtiles holding exactly `tiles`, given as XYZ (z, x, y)."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER,"
                 " tile_row INTEGER, tile_data BLOB)")
    conn.executemany(
        "INSERT INTO tiles VALUES (?,?,?,?)",
        [(z, x, (2 ** z - 1) - y, b"x") for z, x, y in tiles])
    conn.commit()
    conn.close()


class TileRowOrdering(unittest.TestCase):

    def test_tms_row_is_converted_to_xyz(self):
        from processing.verify_tileset_coverage import present_tiles
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.mbtiles"
            _make_mbtiles(p, {(1, 0, 0), (1, 1, 1)})
            self.assertEqual(present_tiles(p, 1), {(1, 0, 0), (1, 1, 1)})

    def test_zero_zoom_is_its_own_inverse(self):
        from processing.verify_tileset_coverage import present_tiles
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.mbtiles"
            _make_mbtiles(p, {(0, 0, 0)})
            self.assertEqual(present_tiles(p, 0), {(0, 0, 0)})


class LandCoverage(unittest.TestCase):

    def test_a_complete_tileset_passes(self):
        from processing.verify_tileset_coverage import required_tiles, verify
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "full.mbtiles"
            _make_mbtiles(p, set(required_tiles(3)))
            self.assertEqual(verify(p, 3), [])

    def test_the_actual_place160_holes_are_caught(self):
        """0/0/0 missing is the real symptom: OHM drew nothing at all at z0."""
        from processing.verify_tileset_coverage import required_tiles, verify
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "holed.mbtiles"
            tiles = set(required_tiles(3))
            tiles.discard((0, 0, 0))
            _make_mbtiles(p, tiles)
            failures = verify(p, 3)
            self.assertTrue(failures)
            self.assertIn("0/0/0", failures[0])

    def test_a_missing_european_tile_is_caught(self):
        from processing.verify_tileset_coverage import (
            _tile_for, required_tiles, verify,
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "holed.mbtiles"
            tiles = set(required_tiles(3))
            berlin_z3 = (3,) + _tile_for(13.40, 52.52, 3)
            tiles.discard(berlin_z3)
            _make_mbtiles(p, tiles)
            failures = verify(p, 3)
            self.assertTrue(any("Berlin" in f for f in failures), failures)

    def test_an_absent_file_is_a_failure_not_a_crash(self):
        from processing.verify_tileset_coverage import verify
        with tempfile.TemporaryDirectory() as d:
            failures = verify(Path(d) / "nope.mbtiles", 2)
            self.assertEqual(len(failures), 1)
            self.assertIn("does not exist", failures[0])

    def test_land_points_span_every_populated_continent(self):
        """A mask that only covered Europe would pass a globally holed set."""
        from processing.verify_tileset_coverage import _LAND_POINTS
        lons = [lon for _n, lon, _lat in _LAND_POINTS]
        lats = [lat for _n, _lon, lat in _LAND_POINTS]
        self.assertLess(min(lons), -70, "no western-hemisphere point")
        self.assertGreater(max(lons), 140, "no far-eastern point")
        self.assertLess(min(lats), -30, "no southern-hemisphere point")
        self.assertGreater(max(lats), 50, "no northern point")


class TileJoinSkipDetection(unittest.TestCase):
    """tile-join reports a dropped tile on stderr and exits 0 regardless."""

    def test_real_skip_messages_are_detected(self):
        from processing.generate_tiles import _tile_join_skips
        stderr = "\n".join([
            "For layer 0, using name \"boundaries\"",
            "Tile 3/4/2 size is 612345 with detail 12, >500000. "
            "Skipping this tile",
            "tile 0/0/0 size is 3471465, >500000, skipping",
        ])
        skips = _tile_join_skips(stderr)
        self.assertEqual(len(skips), 2)

    def test_ordinary_output_is_not_flagged(self):
        from processing.generate_tiles import _tile_join_skips
        stderr = ("For layer 0, using name \"boundaries\"\n"
                  "  99.9%  4/8/5  \n")
        self.assertEqual(_tile_join_skips(stderr), [])

    def test_empty_stderr_is_safe(self):
        from processing.generate_tiles import _tile_join_skips
        self.assertEqual(_tile_join_skips(""), [])
        self.assertEqual(_tile_join_skips(None), [])


class RegionalTilesetsAreNotFailedForBeingRegional(unittest.TestCase):
    """`ukhc` is 92 English and Welsh counties. Demanding tiles over Lagos and
    Tokyo from it produced "34 missing land tile(s)" — a check that cannot pass
    is a check that gets ignored, taking the global assertion down with it."""

    def _mbtiles_with_bounds(self, path, tiles, bounds):
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column "
                     "INTEGER, tile_row INTEGER, tile_data BLOB)")
        conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        conn.executemany("INSERT INTO tiles VALUES (?,?,?,?)",
                         [(z, x, (2 ** z - 1) - y, b"x") for z, x, y in tiles])
        conn.execute("INSERT INTO metadata VALUES ('bounds', ?)", (bounds,))
        conn.commit()
        conn.close()

    def test_a_uk_only_tileset_passes_when_it_covers_the_uk(self):
        from processing.verify_tileset_coverage import _tile_for, verify
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ukhc.mbtiles"
            # London only — the sole land point inside GB bounds.
            tiles = {(z,) + _tile_for(-0.13, 51.51, z) for z in range(0, 5)}
            self._mbtiles_with_bounds(p, tiles, "-8.6,49.9,1.8,60.9")
            self.assertEqual(verify(p, 4), [])

    def test_a_uk_only_tileset_still_fails_if_it_misses_the_uk(self):
        """Scoping must not neuter the check inside the tileset's own area."""
        from processing.verify_tileset_coverage import _tile_for, verify
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ukhc.mbtiles"
            tiles = {(z,) + _tile_for(-0.13, 51.51, z) for z in range(1, 5)}
            self._mbtiles_with_bounds(p, tiles, "-8.6,49.9,1.8,60.9")
            failures = verify(p, 4)
            self.assertTrue(failures)
            self.assertIn("London", failures[0])

    def test_a_global_tileset_is_still_held_to_global_coverage(self):
        from processing.verify_tileset_coverage import required_tiles, verify
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ohm.mbtiles"
            tiles = set(required_tiles(3))
            tiles.discard((0, 0, 0))
            self._mbtiles_with_bounds(p, tiles, "-180,-85,180,85")
            failures = verify(p, 3)
            self.assertTrue(failures)
            self.assertIn("0/0/0", failures[0])


if __name__ == "__main__":
    unittest.main()
