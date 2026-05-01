"""End-to-end staged-pipeline smoke test against a tiny synthetic corpus.

Exercises (in order) the modules that don't require Elasticsearch:
h3_stage → h3_merge → gazetteer_h3_coverage → ccode_enrichment →
ccode_merge → gazetteer_temporal_extent → run_global_barrier →
verify_aggregates.

Sandbox paths come from ``tests/__init__.py`` so that any test module
imported in any order sees a tempdir-rooted ``STAGED_BASE_DIR`` /
``GEOM_STORE_DIR``. This test writes its synthetic ``extract/places.parquet``
files **inside** that sandbox.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def _square(lon0, lat0, side):
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon0, lat0],
            [lon0 + side, lat0],
            [lon0 + side, lat0 + side],
            [lon0, lat0 + side],
            [lon0, lat0],
        ]],
    }


def _un_doc(place_id, ccode, name, lon0, lat0, side, geom_ref):
    poly = _square(lon0, lat0, side)
    return {
        "place_id": place_id,
        "namespace": "un",
        "title": name,
        "ccodes": [ccode],
        "dataset_status": "published",
        "dataset_id": "un",
        "toponyms": [{"toponym_id": f"{name}@en"}],
        "geometries": [{
            "geometry_index": 0,
            "geom_ref": geom_ref,
            "has_geom": True,
            "repr_point": {"lon": lon0 + side / 2, "lat": lat0 + side / 2},
            "hull": poly,
            "bounds": [lon0, lat0, lon0 + side, lat0 + side],
            "timespans": [{"start": {"in": 1900}, "end": {"in": 2025}}],
        }],
        "relations": [],
    }


def _place_doc(place_id, ns, name, lon, lat, ccode_hint=None):
    return {
        "place_id": place_id,
        "namespace": ns,
        "title": name,
        "dataset_status": "published",
        "dataset_id": ns,
        "ccodes": [ccode_hint] if ccode_hint else None,
        "toponyms": [{"toponym_id": f"{name}@en"}],
        "geometries": [{
            "geometry_index": 0,
            "geom_ref": None,            # Point, no geom_store entry needed
            "has_geom": False,
            "repr_point": {"lon": lon, "lat": lat},
            "hull": None,
            "bounds": [lon, lat, lon, lat],
            "timespans": [{"start": {"in": 1850}, "end": {"in": 1900}}],
        }],
        "relations": [
            {"relation_type": "sameAs", "related_place_id": "wd:Q90"}
        ],
    }


def _write_parquet(rows, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), str(out_path))


class StagedPipelineE2E(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Use the package-level sandbox from tests/__init__.py so we don't
        # contend with already-imported processing.* module-level constants.
        from processing.settings import (
            GEOM_STORE_DIR, STAGED_BASE_DIR, STAGED_RUNS_DIR,
        )
        cls.staged_base = Path(STAGED_BASE_DIR)
        cls.staged_runs = Path(STAGED_RUNS_DIR)
        cls.geom_store = Path(GEOM_STORE_DIR)
        cls.staged_base.mkdir(parents=True, exist_ok=True)
        cls.staged_runs.mkdir(parents=True, exist_ok=True)

        # Build a synthetic geom store with one polygon per UN country.
        from processing.geom_store import GeomStoreWriter, consolidate_geom_store
        staging = cls.geom_store / "staging"
        if staging.exists():
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        with GeomStoreWriter(staging, namespace="un") as w:
            w.write("un:fra_0", "8a", _square(2.0, 46.0, 1.0))
            w.write("un:esp_0", "8b", _square(-4.0, 40.0, 1.0))
        consolidate_geom_store(staging_dir=staging,
                               output_dir=cls.geom_store,
                               delete_staging=False)

        # Two staged extracts.
        un_rows = [
            _un_doc("un:fra", "FR", "France", 2.0, 46.0, 1.0, "un:fra_0"),
            _un_doc("un:esp", "ES", "Spain", -4.0, 40.0, 1.0, "un:esp_0"),
        ]
        # A non-global namespace with one point in France and one in Spain.
        nl_rows = [
            _place_doc("nl:1", "nl", "ParisLike", 2.5, 46.5),
            _place_doc("nl:2", "nl", "MadridLike", -3.5, 40.5),
        ]
        _write_parquet(un_rows, cls.staged_base / "un" / "extract" / "places.parquet")
        _write_parquet(nl_rows, cls.staged_base / "nl" / "extract" / "places.parquet")

        # Create the run manifest.
        from processing.staging_orchestrator import create_run_manifest
        cls.run_id = "e2e-run"
        cls.manifest_path = cls.staged_runs / f"{cls.run_id}.json"
        if cls.manifest_path.exists():
            cls.manifest_path.unlink()
        create_run_manifest(cls.manifest_path, cls.run_id,
                            selected_namespaces=["un", "nl"])

    @classmethod
    def tearDownClass(cls):
        # Leave the sandbox in place; tests/__init__.py created it and other
        # test modules may share it (this teardown only cleans up our own
        # subtree, not the sandbox root).
        import shutil
        for sub in ("un", "nl", "_aggregates"):
            shutil.rmtree(cls.staged_base / sub, ignore_errors=True)
        if cls.manifest_path.exists():
            cls.manifest_path.unlink()

    def test_pipeline(self):
        from processing import (
            aat_enrich, ccode_enrichment, ccode_merge, gazetteer_h3_coverage,
            gazetteer_temporal_extent, h3_merge, h3_stage,
        )

        for ns in ("un", "nl"):
            h3_stage.run_h3_stage(
                run_id=self.run_id, manifest_path=self.manifest_path,
                namespace=ns,
            )
            h3_merge.run_h3_merge(
                run_id=self.run_id, namespace=ns,
                manifest_path=self.manifest_path,
            )
            gazetteer_h3_coverage.run_h3_coverage(
                run_id=self.run_id, namespace=ns,
                manifest_path=self.manifest_path,
            )

        # ccode enrichment + merge — nl only (un supplies its own ccodes).
        ccode_enrichment.run_ccode_enrichment(
            run_id=self.run_id, namespace="nl",
            manifest_path=self.manifest_path,
        )
        ccode_merge.run_ccode_merge(
            run_id=self.run_id, namespace="nl",
            manifest_path=self.manifest_path,
        )

        # AAT enrichment — point at a synthetic data dir with empty vocab
        # files + an empty hierarchy so the stage runs end-to-end without
        # actually augmenting anything (the real lookups need data files
        # built from production ES, which the sandbox lacks).
        from tempfile import TemporaryDirectory
        aat_data_tmp = TemporaryDirectory()
        self.addCleanup(aat_data_tmp.cleanup)
        aat_data_dir = Path(aat_data_tmp.name)
        for vocab_file in ("osm.json", "ohm.json", "geonames.json",
                           "wikidata.json", "pleiades.json"):
            (aat_data_dir / vocab_file).write_text("{}", encoding="utf-8")
        (aat_data_dir / "aat_hierarchy.json").write_text("{}", encoding="utf-8")
        for ns in ("un", "nl"):
            # nl actually has a final/places.{jsonl,parquet} from ccode_merge;
            # un doesn't (ccode skipped) so we synthesise one from h3_merged.
            from processing.settings import STAGED_BASE_DIR
            final_dir = Path(STAGED_BASE_DIR) / ns / "final"
            if not (final_dir / "places.jsonl").exists() and not (final_dir / "places.parquet").exists():
                final_dir.mkdir(parents=True, exist_ok=True)
                src = Path(STAGED_BASE_DIR) / ns / "h3_merged" / "places.parquet"
                if src.exists():
                    import shutil
                    shutil.copy(src, final_dir / "places.parquet")
            aat_enrich.run_aat_enrich(
                run_id=self.run_id, namespace=ns,
                manifest_path=self.manifest_path,
                data_dir=aat_data_dir,
            )

        for ns in ("un", "nl"):
            gazetteer_temporal_extent.run_temporal_extent(
                run_id=self.run_id, namespace=ns,
                manifest_path=self.manifest_path,
            )

        # Patch the manifest so the global barrier sees the synthetic seed
        # as a completed extract (real ingestion does this in
        # ingest_all_authorities), and so boundary/ccode are legitimately
        # skipped for un and the non-OSM namespace.
        from processing.staging_orchestrator import update_namespace_stage_status
        for ns in ("un", "nl"):
            update_namespace_stage_status(self.manifest_path, ns, "extract", "completed")
            update_namespace_stage_status(self.manifest_path, ns, "boundary", "skipped")
            update_namespace_stage_status(self.manifest_path, ns, "boundary_merge", "skipped")
        update_namespace_stage_status(self.manifest_path, "un", "ccode", "skipped")
        update_namespace_stage_status(self.manifest_path, "un", "ccode_merge", "skipped")

        # Global barrier — runs via the public API.
        from processing.staging_orchestrator import (
            check_global_barrier,
            load_run_manifest,
            materialise_gazetteer_inventory,
            write_gazetteer_inventory,
        )
        manifest = load_run_manifest(self.manifest_path)
        is_complete, report = check_global_barrier(manifest)
        self.assertTrue(is_complete, f"barrier report: {report}")
        inventory = materialise_gazetteer_inventory(
            manifest, staged_base_dir=self.staged_base,
        )
        write_gazetteer_inventory(
            self.staged_runs / f"{self.run_id}.inventory.json",
            inventory,
        )

        # Aggregate verifier.
        from processing.verify_aggregates import verify
        v = verify(self.manifest_path)
        self.assertTrue(v["ok"], f"verifier report: {v}")

        # And spot-check the produced ccode patches.
        ccode_patch = (self.staged_base / "nl" / "ccode" / "places.ccode.jsonl")
        rows = [json.loads(l) for l in ccode_patch.read_text().splitlines() if l.strip()]
        # Expect both nl: places to get exactly one ccode (FR / ES).
        ccodes_by_id = {r["place_id"]: r["ccodes"] for r in rows}
        self.assertEqual(set(ccodes_by_id), {"nl:1", "nl:2"})
        self.assertEqual(ccodes_by_id["nl:1"], ["FR"])
        self.assertEqual(ccodes_by_id["nl:2"], ["ES"])

        # Final ccode_merge product.
        merged = pq.ParquetFile(
            self.staged_base / "nl" / "final" / "places.parquet"
        ).read().to_pylist()
        merged_ccodes = {r["place_id"]: r["ccodes"] for r in merged}
        self.assertEqual(merged_ccodes["nl:1"], ["FR"])
        self.assertEqual(merged_ccodes["nl:2"], ["ES"])


if __name__ == "__main__":
    unittest.main()
