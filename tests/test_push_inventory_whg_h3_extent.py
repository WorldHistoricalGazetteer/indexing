"""Registry inventory push: per-dataset H3 on the FULL-RUN path (indexing#2) and
the published ``temporal_extent`` override for snapshot sources (place#288).

indexing#2: ``build_inventory_payload`` (the ``--run-id`` path that produced the
live registry) called ``_expand_whg_dataset_entries`` without
``per_dataset_h3``, so all 48 ``whg:*`` rows carried the identical
whole-namespace blob. The incremental ``--namespace whg`` path passed it. The
test drives the full-run builder against a staged sandbox in which two datasets
have DIFFERENT footprints, and asserts each row gets its own.

Writes only into a private tempdir (patched over the module's
``STAGED_BASE_DIR``); ``assert_sandboxed`` additionally refuses to run if the
settings resolved to a real path.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from ._sandbox import assert_sandboxed
except ImportError:  # pragma: no cover
    from _sandbox import assert_sandboxed

import processing.push_gazetteer_inventory as pgi

try:
    import h3  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"h3 unavailable: {exc}")

import h3 as _h3

# Two well-separated res-5 cells: London and Beijing.
LONDON = _h3.latlng_to_cell(51.5, -0.12, 5)
BEIJING = _h3.latlng_to_cell(39.9, 116.4, 5)


class _StagedSandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_sandboxed()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pgi-test-"))
        p = mock.patch.object(pgi, "STAGED_BASE_DIR", str(self.tmp))
        p.start()
        self.addCleanup(p.stop)
        agg = self.tmp / "_aggregates"
        agg.mkdir(parents=True)
        self.agg = agg

    def write_agg(self, ns, extent, record_count=10, coverage=None):
        (self.agg / f"{ns}.temporal_extent.json").write_text(json.dumps(
            {"namespace": ns, "record_count": record_count, "temporal_extent": extent}))
        if coverage is not None:
            (self.agg / f"{ns}.h3_coverage.json").write_text(json.dumps(
                {"namespace": ns, "coverage": coverage}))


class TestFullRunPathUsesPerDatasetH3(_StagedSandbox):
    def setUp(self):
        super().setUp()
        # Namespace-wide union — what every row used to receive.
        self.write_agg("whg", [-100, 2000], 3, coverage=sorted([LONDON, BEIJING]))
        (self.agg / "whg.datasets.json").write_text(json.dumps({"namespace": "whg", "datasets": [
            {"id": "whg:1", "name": "London set", "record_count": 2, "dataset_status": "published"},
            {"id": "whg:2", "name": "Beijing set", "record_count": 1, "dataset_status": "published"},
            {"id": "whg:3", "name": "No geometry", "record_count": 1, "dataset_status": "published"},
        ]}))
        h3dir = self.tmp / "whg" / "h3"
        h3dir.mkdir(parents=True)
        rows = [
            {"place_id": "whg:1:a", "geometries": [{"h3_centroid": LONDON, "h3_cover": [LONDON]}]},
            {"place_id": "whg:1:b", "geometries": [{"h3_centroid": LONDON}]},
            {"place_id": "whg:2:c", "geometries": [{"h3_centroid": BEIJING, "h3_cover": [BEIJING]}]},
        ]
        (h3dir / "places.h3.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def _payload(self):
        return pgi.build_inventory_payload({"per_gazetteer": [{"namespace": "whg"}]})

    def test_each_dataset_row_gets_its_own_footprint(self):
        rows = {r["id"]: r for r in self._payload()}
        self.assertEqual(set(rows), {"whg:1", "whg:2", "whg:3"})
        london3 = _h3.cell_to_parent(LONDON, 3)
        beijing3 = _h3.cell_to_parent(BEIJING, 3)
        self.assertEqual(rows["whg:1"]["h3_coverage"], [london3])
        self.assertEqual(rows["whg:2"]["h3_coverage"], [beijing3])
        # A dataset with no renderable geometry claims nothing, not the union.
        self.assertEqual(rows["whg:3"]["h3_coverage"], [])
        self.assertNotEqual(rows["whg:1"]["h3_coverage"], rows["whg:2"]["h3_coverage"])
        self.assertEqual(rows["whg:1"]["h3_coverage_coarse"], [_h3.cell_to_parent(LONDON, 2)])

    def test_full_run_and_incremental_paths_now_agree(self):
        full = {r["id"]: r["h3_coverage"] for r in self._payload()}
        incremental = {r["id"]: r["h3_coverage"] for r in pgi._expand_whg_dataset_entries(
            pgi._read_h3_coverage("whg"), [-100, 2000], per_dataset_h3=pgi._whg_per_dataset_h3())}
        self.assertEqual(full, incremental)

    def test_regression_guard_the_old_call_would_have_blobbed(self):
        # Proves the fixture CAN distinguish the bug: without per_dataset_h3 every
        # row takes the namespace union.
        old = pgi._expand_whg_dataset_entries(pgi._read_h3_coverage("whg"), [-100, 2000])
        self.assertTrue(all(r["h3_coverage"] == sorted([LONDON, BEIJING]) for r in old))


class TestPublishedTemporalExtent(_StagedSandbox):
    AUTHS = [
        {"namespace": "snap", "dataset_name": "Snapshot", "coverage_extent": []},
        {"namespace": "aligned", "dataset_name": "Aligned", "coverage_extent": [None, 1851]},
        {"namespace": "plain", "dataset_name": "Plain"},
    ]

    def setUp(self):
        super().setUp()
        p = mock.patch.object(pgi, "AUTHORITIES", self.AUTHS)
        p.start()
        self.addCleanup(p.stop)
        self.write_agg("snap", [2025, 2025], coverage=[LONDON])
        self.write_agg("aligned", [1851, 1851], coverage=[LONDON])
        self.write_agg("plain", [1888, 1914], coverage=[LONDON])

    def test_override_and_omission_and_passthrough(self):
        self.assertEqual(pgi._published_temporal_extent("snap"), [])
        self.assertEqual(pgi._published_temporal_extent("aligned"), [None, 1851])
        # A genuine short span is NOT touched (place#288: width is no heuristic).
        self.assertEqual(pgi._published_temporal_extent("plain"), [1888, 1914])

    def test_both_builders_publish_it(self):
        payload = pgi.build_inventory_payload({"per_gazetteer": [
            {"namespace": "snap"}, {"namespace": "aligned"}, {"namespace": "plain"}]})
        got = {e["id"]: e["temporal_extent"] for e in payload}
        self.assertEqual(got, {"snap": [], "aligned": [None, 1851], "plain": [1888, 1914]})
        with mock.patch.object(pgi, "_assert_aggregates_fresh", lambda ns: None):
            self.assertEqual(pgi.build_single_authority_entry("aligned")["temporal_extent"],
                             [None, 1851])
            self.assertEqual(pgi.build_single_authority_entry("plain")["temporal_extent"],
                             [1888, 1914])

    def test_real_settings_cover_the_three_snapshot_sources(self):
        from processing.settings import AUTHORITIES
        by_ns = {a["namespace"]: a for a in AUTHORITIES}
        self.assertEqual(by_ns["kain_par"]["coverage_extent"], [None, 1851])
        self.assertEqual(by_ns["un"]["coverage_extent"], [])
        self.assertEqual(by_ns["nl"]["coverage_extent"], [])
        self.assertNotIn("coverage_extent", by_ns["gb"])


class TestEveryAuthorityHasAName(unittest.TestCase):
    def test_no_acronym_fallback(self):
        from processing.settings import AUTHORITIES
        nameless = [a["namespace"] for a in AUTHORITIES
                    if not (a.get("dataset_name") or "").strip()]
        self.assertEqual(nameless, [])
        self.assertGreater(len(AUTHORITIES), 20)


if __name__ == "__main__":
    unittest.main()
