"""The response models must accept an unbounded temporal side (place#169).

``temporal_range`` / ``temporal_core`` carry ``None`` for a side nothing bounds.
Declared as ``list[int]`` those nulls fail pydantic validation and FastAPI turns
the whole response into a 500 — which is exactly what happened live: every search
whose hits included an open-start authority (``ukhc``, ``kain_par``, ``un``,
``vob_*``) died on serialisation while the query itself was perfectly fine.

The payload tests assemble the fields; only this one puts them through the model.
"""

from __future__ import annotations

import unittest

try:
    from gateway.search import SearchHit
    from gateway.reconcile import CandidateHit
except ModuleNotFoundError as exc:  # pragma: no cover - dev machines without the API deps
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")


UNBOUNDED = [
    ([None, 1974], "open start — a boundary with no datable origin (ukhc)"),
    ([1707, None], "ongoing — a boundary still in force (un)"),
    ([None, None], "defensive: both sides unknown"),
    (None, "wholly undated"),
    ([1400, 1550], "fully bounded, the ordinary case"),
]


class TestHitModelsAcceptUnboundedSides(unittest.TestCase):
    def test_search_hit(self):
        for span, why in UNBOUNDED:
            with self.subTest(why=why):
                hit = SearchHit(place_id="ukhc:KNT", temporal_range=span, temporal_core=span)
                self.assertEqual(hit.temporal_range, span)
                self.assertEqual(hit.temporal_core, span)

    def test_candidate_hit(self):
        for span, why in UNBOUNDED:
            with self.subTest(why=why):
                hit = CandidateHit(place_id="ukhc:KNT", temporal_range=span, temporal_core=span)
                self.assertEqual(hit.temporal_range, span)

    def test_serialises_through_the_response_model(self):
        # The failure was on the way OUT, not in construction.
        hit = SearchHit(place_id="ukhc:KNT", temporal_range=[None, 1974], temporal_core=None)
        self.assertEqual(hit.model_dump()["temporal_range"], [None, 1974])


if __name__ == "__main__":
    unittest.main()
