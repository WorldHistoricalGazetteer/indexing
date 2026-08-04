"""Merging staged documents that share a place_id.

Bulk indexing keys on ``place_id`` as ``_id``, so several staged rows for one
place are several *successful writes* and one surviving document. Two extracts
did this, for different reasons, and neither was visible from the indexer's own
report:

* **chgis** — TGAZ ``placename`` carries up to 11 byte-identical rows per
  ``sys_id`` (differing only in a surrogate key) and the extract keys documents
  on ``sys_id``. Harmless as duplication; *not* harmless in effect, because
  ``h3_stage`` enriched only one copy and the null-``h3_cover`` copy was the one
  that survived. 127 chgis places lost their H3 index and with it any chance of
  matching a fuzzy containment query.
* **hgis** — 47 duplicate *features within* a file (46 lugares, 1 territorios):
  45 exact duplicates, and 2 pairing a geometry-less stub with the real record.
  Last-write-wins could therefore keep the copy with no geometry at all. (The
  two files share no ``src_id``, so this is not a cross-file collision.)

The case that must NOT break is the third one: a single place legitimately
carrying several geometries attested over *different* timespans. `vob_lgd` has
1,968 such documents, `pl` 844 — there the hierarchical shape is doing its job
and a merge must preserve every geometry.
"""

from __future__ import annotations

import unittest


def _geom(**kw):
    base = {"has_geom": False, "geom_class": "point",
            "repr_point": {"lon": 1.0, "lat": 2.0}, "geometry_index": 0}
    base.update(kw)
    return base


class MergeIdenticalDuplicates(unittest.TestCase):
    """chgis: N identical rows collapse to one document, one geometry."""

    def test_collapses_to_single_geometry(self):
        from processing.helpers import merge_place_docs
        doc = {"place_id": "chgis:X", "title": "T",
               "toponyms": [{"toponym_id": "a@bo"}],
               "geometries": [_geom(timespans=[{"start": {"in": 780}}])]}
        merged = merge_place_docs([doc] * 11)
        self.assertEqual(len(merged["geometries"]), 1)
        self.assertEqual(len(merged["toponyms"]), 1)
        self.assertEqual(merged["title"], "T")

    def test_h3_is_salvaged_regardless_of_order(self):
        """The exact chgis loss: the surviving copy had a null h3_cover.

        h3_stage populates one copy; first-seen-wins would keep whichever the
        extract happened to emit first. Both orders must yield the populated
        value, or the fix depends on luck.
        """
        from processing.helpers import merge_place_docs
        null = {"place_id": "chgis:X",
                "geometries": [_geom(h3_cover=None, h3_centroid=None)]}
        full = {"place_id": "chgis:X",
                "geometries": [_geom(h3_cover=["8740c1858ffffff"],
                                     h3_centroid="8740c1858ffffff")]}
        for order in ([null] * 10 + [full], [full] + [null] * 10):
            merged = merge_place_docs(order)
            self.assertEqual(len(merged["geometries"]), 1)
            self.assertEqual(merged["geometries"][0]["h3_cover"],
                             ["8740c1858ffffff"])
            self.assertEqual(merged["geometries"][0]["h3_centroid"],
                             "8740c1858ffffff")


class MergeDistinctGeometries(unittest.TestCase):
    """Distinct geometries for one place all survive the merge.

    No hgis place currently has two genuine geometries — the 47 duplicates are
    45 exact copies plus 2 stub/real pairs. This pins the behaviour anyway,
    because it is the property that makes the merge safe to apply blind: a
    source that later ships a point and a polygon for one place must end up
    with both, not with whichever was written last.
    """

    def test_point_and_polygon_both_survive(self):
        from processing.helpers import merge_place_docs
        empty = {"place_id": "hgis:2000963", "title": "Rio Tinto",
                 "geometries": []}
        point = {"place_id": "hgis:2000963", "title": "Rio Tinto",
                 "geometries": [_geom(geom_class="point")]}
        poly = {"place_id": "hgis:2000963", "title": "Rio Tinto",
                "geometries": [_geom(geom_class="area", has_geom=True,
                                     geom_ref="hgis:2000963_0")]}
        merged = merge_place_docs([empty, point, poly])
        self.assertEqual(len(merged["geometries"]), 2)
        self.assertEqual([g["geom_class"] for g in merged["geometries"]],
                         ["point", "area"])
        self.assertEqual([g["geometry_index"] for g in merged["geometries"]],
                         [0, 1])

    def test_types_and_relations_union(self):
        from processing.helpers import merge_place_docs
        a = {"place_id": "hgis:1",
             "types": [{"identifier": "lugar", "label": "hgis"}],
             "relations": [{"relation_type": "within",
                            "related_place_id": "hgis:9"}]}
        b = {"place_id": "hgis:1",
             "types": [{"identifier": "territorio", "label": "hgis"}],
             "relations": [{"relation_type": "within",
                            "related_place_id": "hgis:9"}]}
        merged = merge_place_docs([a, b])
        self.assertEqual(len(merged["types"]), 2)
        self.assertEqual(len(merged["relations"]), 1, "identical relation")


class TemporallyDistinctGeometriesSurvive(unittest.TestCase):
    """The scenario that must not be flattened.

    `vob_lgd` has 1,968 documents whose geometries carry different timespans —
    the same unit with different boundaries at successive census snapshots. A
    merge that deduplicated on anything coarser than full content would silently
    destroy the boundary sequence, which is far worse than the bug it fixes.
    """

    def test_same_shape_different_timespans_kept_separate(self):
        from processing.helpers import merge_place_docs
        g1911 = _geom(geom_class="area", geom_ref="vob:1_0",
                      timespans=[{"start": {"latest": 1911},
                                  "end": {"earliest": 1911}}])
        g1921 = _geom(geom_class="area", geom_ref="vob:1_1",
                      timespans=[{"start": {"latest": 1921},
                                  "end": {"earliest": 1921}}])
        merged = merge_place_docs([
            {"place_id": "vob_lgd:1", "geometries": [g1911]},
            {"place_id": "vob_lgd:1", "geometries": [g1921]},
        ])
        self.assertEqual(len(merged["geometries"]), 2)
        starts = [g["timespans"][0]["start"]["latest"]
                  for g in merged["geometries"]]
        self.assertEqual(starts, [1911, 1921])

    def test_identical_geometry_differing_only_in_timespan_is_kept(self):
        """Byte-identical boundary re-attested later is still two attestations."""
        from processing.helpers import merge_place_docs
        a = _geom(geom_class="area", geom_ref="vob:1_0",
                  timespans=[{"start": {"latest": 1911}}])
        b = _geom(geom_class="area", geom_ref="vob:1_0",
                  timespans=[{"start": {"latest": 1921}}])
        merged = merge_place_docs([{"place_id": "v:1", "geometries": [a]},
                                   {"place_id": "v:1", "geometries": [b]}])
        self.assertEqual(len(merged["geometries"]), 2)


class Degenerate(unittest.TestCase):

    def test_single_doc_returned_unchanged(self):
        from processing.helpers import merge_place_docs
        doc = {"place_id": "x:1", "title": "T"}
        self.assertIs(merge_place_docs([doc]), doc)

    def test_empty_raises(self):
        from processing.helpers import merge_place_docs
        with self.assertRaises(ValueError):
            merge_place_docs([])

    def test_absent_lists_are_not_invented(self):
        from processing.helpers import merge_place_docs
        merged = merge_place_docs([{"place_id": "x:1"}, {"place_id": "x:1"}])
        self.assertNotIn("relations", merged)
        self.assertNotIn("links", merged)


if __name__ == "__main__":
    unittest.main()
