"""Tile documents can be sourced from Elasticsearch instead of staged parquet.

Tiling only ever needed the *document* from staging — geometry has always come
from the geom store via ``geom_ref`` — and the places index carries every field
``_build_staged_feature`` reads. So a namespace whose staged snapshot is gone
can still be tiled from the index, without re-running a multi-hour extract to
rebuild a copy of what the index already holds.

The behaviour worth pinning is not just "it reads from ES" but that it does so
**only when asked**. An automatic fall back to ES whenever staging looked absent
would be the same class of bug as the priority chain that silently tiled a
1.4 KB test stub as though it were GeoNames.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from processing import generate_tiles as gt


class TestNamespaceDocSourceSelection(unittest.TestCase):
    def test_staged_is_the_default(self):
        with mock.patch.object(gt, "_staged_namespace_source",
                               return_value=Path("/x/places.parquet")) as staged, \
             mock.patch.object(gt, "_iter_staged_docs",
                               return_value=iter([{"place_id": "gn:1"}])), \
             mock.patch.object(gt, "_iter_es_docs") as es:
            docs = list(gt._iter_namespace_docs("gn"))

        self.assertEqual(docs, [{"place_id": "gn:1"}])
        staged.assert_called_once_with("gn")
        es.assert_not_called()

    def test_missing_staging_yields_nothing_it_does_not_reach_for_es(self):
        # The whole point: absent staging is not an invitation to guess.
        with mock.patch.object(gt, "_staged_namespace_source", return_value=None), \
             mock.patch.object(gt, "_iter_es_docs") as es:
            docs = list(gt._iter_namespace_docs("gn"))

        self.assertEqual(docs, [])
        es.assert_not_called()

    def test_es_used_only_for_opted_in_namespaces(self):
        from processing import settings
        with mock.patch.object(settings, "TILE_ES_DOC_NAMESPACES", {"gn"}), \
             mock.patch.object(gt, "_iter_es_docs",
                               return_value=iter([{"place_id": "gn:7"}])) as es, \
             mock.patch.object(gt, "_staged_namespace_source") as staged:
            docs = list(gt._iter_namespace_docs("gn"))

        self.assertEqual(docs, [{"place_id": "gn:7"}])
        es.assert_called_once_with("gn")
        # Opted-in namespaces must not touch staging at all — the snapshot may
        # be a stub, and consulting it is exactly what we are avoiding.
        staged.assert_not_called()

    def test_opt_in_is_per_namespace(self):
        from processing import settings
        with mock.patch.object(settings, "TILE_ES_DOC_NAMESPACES", {"gn"}), \
             mock.patch.object(gt, "_staged_namespace_source",
                               return_value=Path("/x/places.parquet")), \
             mock.patch.object(gt, "_iter_staged_docs",
                               return_value=iter([{"place_id": "ohm:1"}])), \
             mock.patch.object(gt, "_iter_es_docs") as es:
            docs = list(gt._iter_namespace_docs("ohm"))

        self.assertEqual(docs, [{"place_id": "ohm:1"}])
        es.assert_not_called()


class TestEsDocsCarryEverythingTheBuilderReads(unittest.TestCase):
    """A doc from ES must satisfy _build_staged_feature the same as a staged one.

    Guards against the index and the staged schema drifting apart: if a field
    the builder reads stops being in _source, this fails rather than silently
    producing features with missing names/types/dates.
    """

    def test_es_shaped_doc_builds_a_feature(self):
        # Shaped exactly as the live index returns it — note `repr_point` is an
        # object, not a [lon, lat] pair, and `geometry_index` is present. Both
        # matter: the builder reads repr_point as {"lon","lat"} and synthesizes
        # the geom-store key from geometry_index.
        doc = {
            "place_id": "gn:2988507",
            "namespace": "gn",
            "title": "Paris",
            "toponyms": [{"toponym": "Paris", "lang": "en"}],
            "types": [{"identifier": "PPLC", "label": "capital"}],
            "ccodes": ["FR"],
            "population": 2138551,
            "boundary": None,
            "geometries": [{
                "geometry_index": 0,
                "geom_class": "point",
                "has_geom": False,
                "h3_centroid": "872c8ed69ffffff",
                "bounds": [2.3522, 48.8566, 2.3522, 48.8566],
                "repr_point": {"lon": 2.3522, "lat": 48.8566},
                "timespans": [{"start": {"in": 1200}, "end": {"in": 2026}}],
            }],
        }

        class _Reader:
            def get(self, key):
                return None      # points resolve from repr_point, not the store

        feature = gt._build_staged_feature(
            doc, "gn", _Reader(), require_boundary=False)

        self.assertIsNotNone(feature, "ES-shaped doc produced no feature")
        props = feature["properties"]
        self.assertEqual(props.get("place_id"), "gn:2988507")
        self.assertEqual(props.get("name"), "Paris")
        # fcode is derived from types[0].identifier — there is no `fclasses`
        # field in the index, and the builder never used one.
        self.assertEqual(props.get("fcode"), "PPLC")
        self.assertEqual(props.get("population"), 2138551)
        # Temporal props come off the timespans the index carries.
        self.assertEqual(props.get("start"), 1200)
        self.assertEqual(props.get("end"), 2026)


if __name__ == "__main__":
    unittest.main()
