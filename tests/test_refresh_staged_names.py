"""`refresh_staged_names` moves NAMES into a staged snapshot and nothing else.

place#204: ukhc gained 1,017 alternative names from exactly the same 92
polygons. Re-running the whole stage chain to change two fields risks the
enrichment that is already correct — an h3_cover that came back subtly
different would be a silent spatial-containment regression. So the tool copies
the name fields across and refuses to run at all if anything else moved.

These tests pin the refusals, because the refusals are the safety.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from processing import refresh_staged_names as rsn


def _doc(pid, toponyms, geom_extra=None, **extra):
    g = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}
    g.update(geom_extra or {})
    return {"place_id": pid, "title": pid, "toponyms": toponyms,
            "geometries": [g], **extra}


class _Staged:
    """A throwaway {STAGED_BASE_DIR}/<ns>/{extract,final}/places.jsonl pair."""

    def __init__(self, extract_docs, final_docs, ns="ukhc"):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name) / ns
        for stage, docs in (("extract", extract_docs), ("final", final_docs)):
            d = base / stage
            d.mkdir(parents=True)
            (d / "places.jsonl").write_text(
                "".join(json.dumps(x) + "\n" for x in docs), encoding="utf-8")
        self.base = base
        self.patch = mock.patch.object(rsn, "STAGED_BASE_DIR", self.tmp.name)

    def __enter__(self):
        self.patch.start()
        return self

    def __exit__(self, *a):
        self.patch.stop()
        self.tmp.cleanup()

    def final(self):
        return [json.loads(l) for l in
                (self.base / "final" / "places.jsonl").read_text().splitlines() if l]


class TestRefresh(unittest.TestCase):
    def test_names_are_copied_and_enrichment_survives(self):
        # geom_ref is written at extract time and survives downstream, so both
        # stages carry it — as the real ukhc snapshots do.
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en", "Somersetshire@en"],
                  geom_extra={"geom_ref": "ukhc:SMS_0"})],
            [_doc("ukhc:SMS", ["Somerset@en"],
                  geom_extra={"h3_cover": ["8a1f"], "geom_ref": "ukhc:SMS_0"},
                  ccodes=["GB"], area_km2=4171.0)],
        ) as s:
            self.assertEqual(rsn.refresh("ukhc", execute=True), 0)
            doc = s.final()[0]
            self.assertEqual(doc["toponyms"], ["Somerset@en", "Somersetshire@en"])
            self.assertEqual(doc["ccodes"], ["GB"])          # untouched
            self.assertEqual(doc["geometries"][0]["h3_cover"], ["8a1f"])
            self.assertEqual(doc["geometries"][0]["geom_ref"], "ukhc:SMS_0")

    def test_dry_run_writes_nothing(self):
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en", "Somersetshire@en"])],
            [_doc("ukhc:SMS", ["Somerset@en"])],
        ) as s:
            self.assertEqual(rsn.refresh("ukhc", execute=False), 0)
            self.assertEqual(s.final()[0]["toponyms"], ["Somerset@en"])

    def test_links_are_carried_across(self):
        link = [{"type": "closeMatch", "identifier": "wd:Q67461071"}]
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en"], links=link)],
            [_doc("ukhc:SMS", ["Somerset@en"])],
        ) as s:
            rsn.refresh("ukhc", execute=True)
            self.assertEqual(s.final()[0]["links"], link)

    def test_links_removed_upstream_are_removed_here(self):
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en"])],
            [_doc("ukhc:SMS", ["Somerset@en"],
                  links=[{"type": "closeMatch", "identifier": "wd:Qold"}])],
        ) as s:
            rsn.refresh("ukhc", execute=True)
            self.assertNotIn("links", s.final()[0])

    def test_a_backup_of_the_previous_snapshot_is_kept(self):
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en", "Somersetshire@en"])],
            [_doc("ukhc:SMS", ["Somerset@en"])],
        ) as s:
            rsn.refresh("ukhc", execute=True)
            bak = s.base / "final" / "places.jsonl.bak"
            self.assertTrue(bak.exists())
            self.assertIn("Somerset@en", bak.read_text())
            self.assertNotIn("Somersetshire", bak.read_text())


class TestItRefusesWhenTheAssumptionIsFalse(unittest.TestCase):
    def test_a_new_place_stops_the_run(self):
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en"]), _doc("ukhc:NEW", ["New@en"])],
            [_doc("ukhc:SMS", ["Somerset@en"])],
        ):
            self.assertEqual(rsn.refresh("ukhc", execute=True), 2)

    def test_a_vanished_place_stops_the_run(self):
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en"])],
            [_doc("ukhc:SMS", ["Somerset@en"]), _doc("ukhc:GONE", ["Gone@en"])],
        ):
            self.assertEqual(rsn.refresh("ukhc", execute=True), 2)

    def test_a_changed_geometry_stops_the_run(self):
        # H3 would have to be recomputed, which this tool deliberately cannot do.
        other = {"type": "Polygon", "coordinates": [[[9, 9], [9, 8], [8, 8], [9, 9]]]}
        with _Staged(
            [{"place_id": "ukhc:SMS", "toponyms": ["Somerset@en"], "geometries": [other]}],
            [_doc("ukhc:SMS", ["Somerset@en"], geom_extra={"h3_cover": ["8a1f"]})],
        ) as s:
            self.assertEqual(rsn.refresh("ukhc", execute=True), 2)
            self.assertEqual(s.final()[0]["geometries"][0]["coordinates"],
                             [[[0, 0], [0, 1], [1, 1], [0, 0]]])

    def test_stage_only_geometry_keys_are_not_a_change(self):
        # h3_cover/h3_centroid exist only downstream, `hull` only upstream.
        # Neither asymmetry means the geometry moved.
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en", "Somersetshire@en"],
                  geom_extra={"hull": {"type": "Polygon", "coordinates": []}})],
            [_doc("ukhc:SMS", ["Somerset@en"],
                  geom_extra={"h3_cover": ["8a1f"], "h3_centroid": "8a1f"})],
        ) as s:
            self.assertEqual(rsn.refresh("ukhc", execute=True), 0)
            self.assertEqual(len(s.final()[0]["toponyms"]), 2)

    def test_explicit_nulls_are_not_a_change(self):
        # Downstream materialises absent optional fields as nulls; treating that
        # as a difference made the guard fire on all 92 real ukhc docs.
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en", "Somersetshire@en"],
                  geom_extra={"timespans": [{"start": {"latest": 1974}}]})],
            [_doc("ukhc:SMS", ["Somerset@en"],
                  geom_extra={"timespans": [{"start": {"latest": 1974, "in": None}}]})],
        ) as s:
            self.assertEqual(rsn.refresh("ukhc", execute=True), 0)

    def test_a_moved_geom_store_key_stops_the_run(self):
        # geom_ref IS compared — it is the identity of the stored polygon.
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en"], geom_extra={"geom_ref": "ukhc:SMS_1"})],
            [_doc("ukhc:SMS", ["Somerset@en"], geom_extra={"geom_ref": "ukhc:SMS_0"})],
        ):
            self.assertEqual(rsn.refresh("ukhc", execute=True), 2)

    def test_a_moved_repr_point_stops_the_run(self):
        with _Staged(
            [_doc("ukhc:SMS", ["Somerset@en"], geom_extra={"repr_point": [1.0, 2.0]})],
            [_doc("ukhc:SMS", ["Somerset@en"], geom_extra={"repr_point": [9.0, 9.0]})],
        ):
            self.assertEqual(rsn.refresh("ukhc", execute=True), 2)

    def test_a_missing_stage_is_an_error_not_a_silent_noop(self):
        with _Staged([_doc("ukhc:SMS", ["Somerset@en"])], []) as s:
            (s.base / "final" / "places.jsonl").unlink()
            self.assertEqual(rsn.refresh("ukhc", execute=True), 2)


if __name__ == "__main__":
    unittest.main()
