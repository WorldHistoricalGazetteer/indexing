"""place#350: an adopted location in a contributed dataset is indexed with the namespace it
was adopted from as ``geometries[].source``, and /api/places passes it on.

Run package-qualified (never ``discover -s tests``; see tests/__init__.py):

    .venv/bin/python -m unittest tests.test_whg_adopted_geometry_source

Pure unit tests: no Elasticsearch, no network, no staging writes. Every absence is paired
with the same input carrying the one thing that makes it present.
"""

from __future__ import annotations

import copy
import unittest

from tests.test_whg_places import _load_whg_places

ADOPTED = {
    "type": "Point", "coordinates": [0.01, 51.76],
    "citation": {"id": "gn:8", "label": "GeoNames"},
    "provenance": {"kind": "adopted", "source": "gn", "source_label": "GeoNames",
                   "match": "gn:8", "terms": "CC-BY-4.0", "adopted": "2026-11-18"},
}
PLACED = {
    "type": "Point", "coordinates": [0.02, 51.77], "certainty": "less-certain",
    "provenance": {"kind": "placed", "method": "placed on the map", "approximate": True},
}


def feature(geometry, fid=3):
    return {"type": "Feature", "id": fid, "geometry": geometry,
            "properties": {"title": "Hoddesdon", "src_id": f"f{fid}"}}


class TestAdoptedGeometrySource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load_whg_places()

    def geom(self, geometry):
        doc = self.m.lpf_feature_to_staged_doc(feature(geometry), dataset_sub_id=42)
        return doc["geometries"][0]

    def test_an_adopted_point_names_its_source(self):
        g = self.geom(ADOPTED)
        self.assertEqual(g["source"], "gn")
        self.assertEqual(g["repr_point"], {"lon": 0.01, "lat": 51.76})
        # Nothing else of the provenance reaches the index document.
        self.assertNotIn("provenance", g)
        self.assertNotIn("citation", g)

    def test_the_contributors_own_point_names_no_source(self):
        own = {k: v for k, v in ADOPTED.items() if k not in ("provenance", "citation")}
        self.assertNotIn("source", self.geom(own))
        self.assertEqual(self.geom(ADOPTED)["source"], "gn")              # the pair

    def test_a_point_placed_by_hand_names_no_source(self):
        self.assertNotIn("source", self.geom(PLACED))

    def test_a_malformed_source_is_not_indexed(self):
        for bad in ("gn; drop", "GN", "", None, 7, "whg", "x" * 40):
            g = copy.deepcopy(ADOPTED)
            g["provenance"]["source"] = bad
            self.assertNotIn("source", self.geom(g), bad)

    def test_an_adopted_point_inside_a_collection_keeps_its_source(self):
        g = self.geom({"type": "GeometryCollection", "geometries": [ADOPTED]})
        self.assertEqual(g["source"], "gn")


try:
    from gateway.places import PlaceDetail, _format_geometries
except ModuleNotFoundError as exc:  # pragma: no cover - machines without the API deps
    PlaceDetail = None
    _SKIP = str(exc)


@unittest.skipIf(PlaceDetail is None, "gateway serving deps unavailable")
class TestPlacesPassesSourceOn(unittest.TestCase):
    """Through the response MODEL as well as the assembler (reference_gateway_response_models)."""

    RAW = {"has_geom": False, "repr_point": {"lon": 0.01, "lat": 51.76}, "source": "gn"}

    def test_source_survives_assembly_and_serialisation(self):
        geoms = _format_geometries([self.RAW])
        self.assertEqual(geoms, [{"repr_point": [0.01, 51.76], "source": "gn"}])
        dumped = PlaceDetail(place_id="whg:42:f3", title="Hoddesdon", geometries=geoms).model_dump()
        self.assertEqual(dumped["geometries"][0]["source"], "gn")

    def test_no_source_no_key(self):
        raw = {k: v for k, v in self.RAW.items() if k != "source"}
        self.assertEqual(_format_geometries([raw]), [{"repr_point": [0.01, 51.76]}])

    def test_a_source_alone_does_not_make_a_geometry(self):
        self.assertEqual(_format_geometries([{"source": "gn"}]), [])
