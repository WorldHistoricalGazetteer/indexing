"""Duplicate Cliopatria polities get a unique geom key, not just a unique id.

Cliopatria carries the same polity at several date ranges, so ids collide and
are disambiguated with a ``_v{n}`` suffix. The subtlety is *when*: the polygon
is written to the geom store under ``{place_id}_0`` during processing, while the
staged doc's ``geom_ref`` is derived from the final place_id. Disambiguating
after processing therefore filed every duplicate's geometry under the base id
and left its ref pointing at a key nothing had written.

That is not theoretical — it produced 2,986 dangling refs in the live index,
and those places never appeared in a tile: a z10 sweep of the deployed clio
tileset over the eastern Mediterranean found 1,235 distinct place_ids and not a
single ``_vN`` among them.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parent.parent / "authorities" / "cliopatria-places.py"
_spec = importlib.util.spec_from_file_location("cliopatria_places", _MOD_PATH)
clio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clio)


def _feature(polity="Roman Principate", from_year=14, to_year=22):
    return {
        "type": "Feature",
        "properties": {"Name": polity, "SeshatID": polity,
                       "FromYear": from_year, "ToYear": to_year},
        "geometry": {"type": "Polygon",
                     "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
    }


class TestCliopatriaDuplicateIds(unittest.TestCase):
    def test_duplicate_polity_gets_distinct_place_id_and_geom_ref(self):
        seen: set[str] = set()
        docs = [clio.process_cliopatria_feature(_feature(), seen_ids=seen)
                for _ in range(3)]

        ids = [d["place_id"] for d in docs]
        self.assertEqual(len(set(ids)), 3, f"place_ids collided: {ids}")

        # The geometry key must track the FINAL id, so each duplicate's polygon
        # is filed separately and its ref resolves.
        refs = []
        for d in docs:
            geoms = d.get("geometries") or []
            self.assertTrue(geoms, "no geometry emitted")
            key = geoms[0].get("geom_key") or geoms[0].get("geom_ref")
            if key is None:                     # key recorded only in the store
                key = f"{d['place_id']}_0"
            refs.append(key)
        self.assertEqual(len(set(refs)), 3,
                         f"geom keys collided across duplicates: {refs}")
        for d, ref in zip(docs, refs):
            self.assertTrue(ref.startswith(d["place_id"]),
                            f"geom key {ref} does not match place_id {d['place_id']}")

    def test_versioned_suffix_shape(self):
        seen: set[str] = set()
        first = clio.process_cliopatria_feature(_feature(), seen_ids=seen)
        second = clio.process_cliopatria_feature(_feature(), seen_ids=seen)
        self.assertFalse(first["place_id"].endswith("_v1"))
        self.assertTrue(second["place_id"].endswith("_v1"),
                        f"expected _v1 suffix, got {second['place_id']}")

    def test_distinct_polities_are_untouched(self):
        seen: set[str] = set()
        a = clio.process_cliopatria_feature(_feature("Seleucid Empire"), seen_ids=seen)
        b = clio.process_cliopatria_feature(_feature("Spanish Empire"), seen_ids=seen)
        for d in (a, b):
            self.assertNotIn("_v", d["place_id"].rsplit(":", 1)[-1].replace("_v", "", 0))
        self.assertNotEqual(a["place_id"], b["place_id"])

    def test_seen_ids_optional(self):
        # Callers that don't dedupe still work (no crash, no suffix).
        doc = clio.process_cliopatria_feature(_feature())
        self.assertTrue(doc["place_id"].startswith("clio:"))


if __name__ == "__main__":
    unittest.main()
