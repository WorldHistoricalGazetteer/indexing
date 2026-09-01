"""`has_geom=True` must never be answered from the convex hull (§2.10).

``h3_stage.cover_geometry_for`` reads the authoritative polygon from the geom
store and, until 1 September 2026, fell back to the staged convex hull
whenever that lookup produced nothing — silently, for a feature explicitly
marked ``has_geom=True``.

What that cost, measured 31 August 2026. The ``un-final`` chain's sbatch
lacked the conda ``LD_LIBRARY_PATH`` export, so ``import sqlite3`` raised
inside ``geom_store``; three stacked bare ``except Exception`` blocks turned
that into ``reader=None``; and ``un``'s ``h3_cover`` was built from the hull.
``un:usa``'s hull spans Alaska to the Pacific territories and therefore
**crosses the antimeridian when its polygon does not**, so the fill went the
long way round: 278 cells covering 1.74× the country's area while containing
almost none of it — Denver, NYC, Anchorage and Honolulu all absent, Guam
present. That cover is byte-identical to the one found in ``staged/un/final``.

Downstream, ``build_un_prefilter`` produced no candidates for essentially
every place, so the ccode tier-1 (73,663-vertex geoBoundaries) path went
inert corpus-wide and everything fell through to tier 2's 232-vertex BNDA
outlines — the coarse outlines tier 1 exists to avoid. Every stage reported
success throughout.

So the property is not "prefer the polygon". It is that a **promise which
cannot be honoured must fail loudly**: ``has_geom=True`` asserts the real
polygon is retrievable, and a hull is not a coarser answer to that question
but sometimes a wrong one.
"""

from __future__ import annotations

import unittest


# A hull shaped like un:usa's: vertices either side of ±180, so it crosses the
# antimeridian even though a member polygon need not.
CROSSING_HULL = {
    "type": "Polygon",
    "coordinates": [[[179.0, 10.0], [-179.0, 10.0], [-179.0, 60.0],
                     [179.0, 60.0], [179.0, 10.0]]],
}
REAL_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-120.0, 30.0], [-75.0, 30.0], [-75.0, 48.0],
                     [-120.0, 48.0], [-120.0, 30.0]]],
}


class _Reader:
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc

    def get(self, key):
        if self.exc is not None:
            raise self.exc
        return self.result


class HasGeomIsNeverAnsweredFromTheHull(unittest.TestCase):

    def _geom(self, **over):
        g = {"has_geom": True, "geom_ref": "un:usa_0", "hull": CROSSING_HULL}
        g.update(over)
        return g

    def test_no_reader_raises_instead_of_returning_the_hull(self):
        """The exact 31 Aug shape: sqlite3 import died, reader is None."""
        from processing.h3_stage import cover_geometry_for

        with self.assertRaises(RuntimeError) as caught:
            cover_geometry_for(self._geom(), "un:usa", 0, None)
        self.assertIn("has_geom=True", str(caught.exception))

    def test_store_miss_raises(self):
        """Reader open, key absent — still a broken promise, not a fallback."""
        from processing.h3_stage import cover_geometry_for

        with self.assertRaises(RuntimeError):
            cover_geometry_for(self._geom(), "un:usa", 0, _Reader(result=None))

    def test_store_error_raises_and_keeps_the_cause(self):
        from processing.h3_stage import cover_geometry_for

        with self.assertRaises(RuntimeError) as caught:
            cover_geometry_for(self._geom(), "un:usa", 0,
                               _Reader(exc=ImportError("GLIBCXX_3.4.30 not found")))
        self.assertIsInstance(caught.exception.__cause__, ImportError)

    def test_healthy_store_returns_the_real_polygon(self):
        """The fix must not break the path that works."""
        from processing.h3_stage import cover_geometry_for

        got = cover_geometry_for(self._geom(), "un:usa", 0,
                                 _Reader(result=REAL_POLYGON))
        self.assertEqual(REAL_POLYGON, got)

    def test_point_features_without_has_geom_still_use_the_hull(self):
        """Where the hull is the ONLY geometry it remains the intended source.

        Guards against over-correcting: namespaces whose h3 reads hull-bearing
        JSONL must keep working.
        """
        from processing.h3_stage import cover_geometry_for

        plain_hull = {"type": "Polygon",
                      "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
        got = cover_geometry_for({"has_geom": False, "hull": plain_hull},
                                 "pl:1", 0, None)
        self.assertEqual(plain_hull, got)

    def test_antimeridian_hull_is_not_used_when_an_inline_geom_exists(self):
        """Defence in depth for the legitimate hull path.

        A hull that crosses ±180 when the geometry does not is not a coarser
        approximation, it is a wrong one — so prefer the inline geometry.
        """
        from processing.h3_stage import cover_geometry_for

        got = cover_geometry_for(
            {"has_geom": False, "hull": CROSSING_HULL, "geom": REAL_POLYGON},
            "x:1", 0, None)
        self.assertEqual(REAL_POLYGON, got,
                         "a dateline-crossing hull must not be preferred over a "
                         "non-crossing real geometry")


if __name__ == "__main__":
    unittest.main()
