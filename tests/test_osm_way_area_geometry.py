"""Unit tests for the way-area pass tag gate (place#145).

The geometry assembly needs osmium + a PBF and is covered by the Slurm
prototype (Isle of Man: 532 polygons, 0 errors, 98.3% match to prod docs). Here
we pin the pure tag gate, which decides *which* area-ways get a polygon — it
must exactly mirror ``authorities/osm-places.py`` so the pass only touches ways
that were actually indexed as places.
"""

import unittest

from processing.osm_way_area_geometry import keys_for, process_way_tags

_OSM = keys_for("osm")
_OHM = keys_for("ohm")


class _Tag:
    def __init__(self, k, v):
        self.k, self.v = k, v


class _Tags:
    """Minimal stand-in for an osmium TagList (membership + iteration)."""
    def __init__(self, d):
        self._d = dict(d)

    def __contains__(self, k):
        return k in self._d

    def __getitem__(self, k):
        return self._d[k]

    def __iter__(self):
        return iter(_Tag(k, v) for k, v in self._d.items())


class TestProcessWayTags(unittest.TestCase):
    def test_named_landuse_area_accepted(self):
        r = process_way_tags(_Tags({"name": "Middle River Industrial Estate",
                                    "landuse": "industrial"}), _OSM)
        self.assertIsNotNone(r)
        self.assertEqual(r["name"], "Middle River Industrial Estate")

    def test_named_natural_and_place_accepted(self):
        self.assertIsNotNone(process_way_tags(_Tags({"name": "Fleshwick Beach", "natural": "beach"}), _OSM))
        self.assertIsNotNone(process_way_tags(_Tags({"name": "Corvalley South Farm", "place": "farm"}), _OSM))

    def test_unnamed_area_rejected(self):
        # the bulk of from_way areas — unnamed buildings/landuse
        self.assertIsNone(process_way_tags(_Tags({"building": "yes"}), _OSM))
        self.assertIsNone(process_way_tags(_Tags({"landuse": "grass"}), _OSM))

    def test_named_but_no_place_key_rejected(self):
        # a named closed way with none of the indexed keys was never a place doc
        self.assertIsNone(process_way_tags(_Tags({"name": "Some Wall", "barrier": "wall"}), _OSM))
        self.assertIsNone(process_way_tags(_Tags({"name": "A Building", "building": "house"}), _OSM))

    def test_temporal_tags_carried(self):
        r = process_way_tags(_Tags({"name": "Old Pond", "natural": "water",
                                    "start_date": "1850", "end_date": "1900"}), _OSM)
        self.assertEqual(r["start_date"], "1850")
        self.assertEqual(r["end_date"], "1900")

    def test_gate_matches_osm_places_key_set(self):
        # the accepted key set must be exactly osm-places' set (minus admin-only
        # extras), so the pass never targets a way osm-places didn't index
        for key in ("natural", "water", "waterway", "historic", "landuse", "boundary"):
            self.assertIsNotNone(process_way_tags(_Tags({"name": "X", key: "v"}), _OSM), key)


class TestSourceSpecificGate(unittest.TestCase):
    """OHM indexes a broader key set than OSM — the gate must be source-aware
    (place#145 OHM canary: the OSM set silently dropped ~57k OHM ways)."""

    def test_building_rejected_for_osm_accepted_for_ohm(self):
        t = _Tags({"name": "The Old Fort", "building": "yes"})
        self.assertIsNone(process_way_tags(t, _OSM))
        self.assertIsNotNone(process_way_tags(t, _OHM))

    def test_ohm_extra_keys_accepted(self):
        for key in ("amenity", "man_made", "military", "building", "leisure", "tourism"):
            self.assertIsNotNone(process_way_tags(_Tags({"name": "X", key: "v"}), _OHM), key)
            self.assertIsNone(process_way_tags(_Tags({"name": "X", key: "v"}), _OSM), key)

    def test_osm_keys_are_subset_of_ohm(self):
        self.assertTrue(set(_OSM).issubset(set(_OHM)))


if __name__ == "__main__":
    unittest.main()
