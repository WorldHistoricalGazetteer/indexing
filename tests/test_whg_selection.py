"""The one dataset-selection rule (``processing.whg_selection``) and its three
consumers: the whg extract, the retention sweep's row mapper, and the
per-dataset staging layout (place#341).

Contract confirmed by whg3, 30 Oct 2026: enum ``uploaded, validating,
validation_failed, draft, submitted, in_review, changes_requested, declined,
accepted, published, withdrawn``; NULL / absent = legacy, no backfill;
``draft`` is a real state; a published dataset is NOT ``authority=True``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from processing import whg_selection as sel
from processing.whg_selection import is_indexable, partition, summarise


def _ds(**kw):
    """A discovery-endpoint record with the legacy fields defaulted to the
    shape of a live public accessioning dataset; override what the case needs."""
    base = {"id": 1, "title": "t", "authority": True, "public": True,
            "ds_status": "accessioning"}
    base.update(kw)
    return base


class TestIsIndexable(unittest.TestCase):

    # -- v4 lifecycle --------------------------------------------------------

    def test_published_public_selected(self):
        ok, reason = is_indexable(_ds(lifecycle_state="published"))
        self.assertTrue(ok)
        self.assertEqual(reason, sel.REASON_PUBLISHED)

    def test_published_non_authority_selected(self):
        # Published v4 datasets are not authority=True; requiring it would
        # reject every one of them.
        ok, reason = is_indexable(_ds(lifecycle_state="published",
                                      authority=False, ds_status="uploaded"))
        self.assertTrue(ok, reason)

    def test_published_not_public_rejected(self):
        ok, reason = is_indexable(_ds(lifecycle_state="published", public=False))
        self.assertFalse(ok)
        self.assertEqual(sel.reason_bucket(reason), sel.REASON_PUBLISHED_NOT_PUBLIC)

    def test_non_published_states_never_selected_even_when_accessioning(self):
        # ds_status=accessioning, authority, public all say yes; the v4 state
        # outranks them.
        for state in ("withdrawn", "accepted", "draft", "submitted",
                      "in_review", "changes_requested", "declined",
                      "uploaded", "validating", "validation_failed"):
            with self.subTest(state=state):
                ok, reason = is_indexable(_ds(lifecycle_state=state,
                                              ds_status="accessioning"))
                self.assertFalse(ok, state)
                self.assertEqual(sel.reason_bucket(reason), sel.REASON_NOT_PUBLISHED)

    def test_draft_is_a_real_state_not_a_legacy_marker(self):
        self.assertNotIn("draft", sel.LEGACY_MARKERS)
        self.assertEqual(sel.LEGACY_MARKERS, frozenset())
        ok, _ = is_indexable(_ds(lifecycle_state="draft", ds_status="accessioning"))
        self.assertFalse(ok)

    def test_every_known_state_is_decided(self):
        # Nothing in the confirmed enum may fall to the unknown bucket.
        for state in sel.KNOWN_LIFECYCLE_STATES:
            _, reason = is_indexable(_ds(lifecycle_state=state))
            self.assertNotEqual(sel.reason_bucket(reason),
                                sel.REASON_UNKNOWN_LIFECYCLE_STATE, state)

    def test_unknown_state_refused(self):
        # "pending" is deliberately here: it is NOT in the confirmed whg3 enum,
        # so it must be refused as unknown rather than quietly mapped.
        for state in ("archived", "pending", "PUBLISHED!"):
            with self.subTest(state=state):
                ok, reason = is_indexable(_ds(lifecycle_state=state))
                self.assertFalse(ok)
                self.assertEqual(sel.reason_bucket(reason),
                                 sel.REASON_UNKNOWN_LIFECYCLE_STATE)

    def test_state_is_case_and_space_insensitive(self):
        self.assertTrue(is_indexable(_ds(lifecycle_state=" Published "))[0])

    # -- legacy (lifecycle_state absent / null) ------------------------------

    def test_legacy_public_accessioning_selected(self):
        ok, reason = is_indexable(_ds())
        self.assertTrue(ok)
        self.assertEqual(reason, sel.REASON_LEGACY_INDEXABLE)

    def test_legacy_null_state_is_legacy(self):
        self.assertTrue(is_indexable(_ds(lifecycle_state=None))[0])
        self.assertTrue(is_indexable(_ds(lifecycle_state=""))[0])

    def test_legacy_public_indexed_selected(self):
        self.assertTrue(is_indexable(_ds(ds_status="indexed"))[0])

    def test_legacy_private_accessioning_not_selected(self):
        # The three private accessioning datasets in prod.
        ok, reason = is_indexable(_ds(public=False))
        self.assertFalse(ok)
        self.assertEqual(sel.reason_bucket(reason), sel.REASON_LEGACY_NOT_INDEXABLE)
        self.assertIn("public", reason)

    def test_legacy_non_authority_not_selected(self):
        self.assertFalse(is_indexable(_ds(authority=False))[0])

    def test_legacy_other_ds_status_not_selected(self):
        for st in ("reconciling", "wd-complete", "uploaded", "seed", None):
            with self.subTest(ds_status=st):
                self.assertFalse(is_indexable(_ds(ds_status=st))[0])

    # -- the check that can fail ---------------------------------------------

    def test_same_dataset_selected_then_rejected_once_submitted(self):
        ds = _ds(id=99)
        self.assertTrue(is_indexable(ds)[0], "precondition: selected as legacy")
        ds["lifecycle_state"] = "submitted"
        ok, reason = is_indexable(ds)
        self.assertFalse(ok)
        self.assertEqual(sel.reason_bucket(reason), sel.REASON_NOT_PUBLISHED)
        # And the brief's literal "pending": rejected too, as unknown.
        ds["lifecycle_state"] = "pending"
        self.assertFalse(is_indexable(ds)[0])

    def test_rejects_non_dict(self):
        with self.assertRaises(TypeError):
            is_indexable(["published"])


class TestPartitionAndSummary(unittest.TestCase):

    def setUp(self):
        self.datasets = [
            _ds(id=1, lifecycle_state="published", authority=False),
            _ds(id=2),                                          # legacy public
            _ds(id=3, public=False),                            # legacy private
            _ds(id=4, lifecycle_state="submitted"),
            _ds(id=5, lifecycle_state="mystery"),
            _ds(id=6, lifecycle_state="published", public=False),
        ]

    def test_partition_preserves_order_and_reasons(self):
        selected, rejected = partition(self.datasets)
        self.assertEqual([d["id"] for d in selected], [1, 2])
        self.assertEqual([d["id"] for d, _ in rejected], [3, 4, 5, 6])
        buckets = [sel.reason_bucket(r) for _, r in rejected]
        self.assertEqual(buckets, [
            sel.REASON_LEGACY_NOT_INDEXABLE, sel.REASON_NOT_PUBLISHED,
            sel.REASON_UNKNOWN_LIFECYCLE_STATE, sel.REASON_PUBLISHED_NOT_PUBLIC,
        ])

    def test_summary_counts_unknown_and_carries_denominator(self):
        s = summarise(self.datasets)
        self.assertEqual(s["examined"], 6)
        self.assertEqual(s["selected"], 2)
        self.assertEqual(s["rejected"], 4)
        self.assertEqual(sum(s["by_reason"].values()), s["examined"])
        self.assertEqual(s["unknown_lifecycle_state"], 1)
        self.assertEqual(s["unknown_lifecycle_state_ids"], [5])

    def test_summary_of_nothing_is_zero_of_zero(self):
        s = summarise([])
        self.assertEqual((s["examined"], s["selected"], s["rejected"]), (0, 0, 0))


# ---------------------------------------------------------------------------
# retention_sweep: the row mapper
# ---------------------------------------------------------------------------


class TestRetentionSweepRowMapping(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from processing.retention_sweep import _map_dataset_status_row
        cls.map_row = staticmethod(_map_dataset_status_row)

    def test_legacy_query_shape_keeps_old_behaviour(self):
        # The legacy query selects id, ds_status, public only.
        self.assertEqual(self.map_row({"id": 1, "ds_status": "accessioning",
                                       "public": True}), "published")
        self.assertEqual(self.map_row({"id": 1, "ds_status": "indexed",
                                       "public": True}), "published")
        self.assertEqual(self.map_row({"id": 1, "ds_status": "accessioning",
                                       "public": False}), "pending")
        self.assertEqual(self.map_row({"id": 1, "ds_status": "reconciling",
                                       "public": True}), "submitted")
        self.assertEqual(self.map_row({"id": 1, "ds_status": "wd-complete",
                                       "public": False}), "submitted")
        self.assertEqual(self.map_row({"id": 1, "ds_status": "uploaded",
                                       "public": True}), "pending")

    def test_v4_published_is_exempt_without_authority(self):
        self.assertEqual(self.map_row({
            "id": 1, "ds_status": "uploaded", "public": True,
            "authority": False, "lifecycle_state": "published"}), "published")

    def test_v4_withdrawn_accessioning_is_not_exempt(self):
        row = {"id": 1, "ds_status": "accessioning", "public": True,
               "authority": True, "lifecycle_state": None}
        self.assertEqual(self.map_row(row), "published")   # legacy row → exempt
        row["lifecycle_state"] = "withdrawn"
        self.assertEqual(self.map_row(row), "pending")     # same row, v4 state set

    def test_v4_null_lifecycle_uses_legacy_rule_with_real_authority(self):
        # With the column present, authority is a real value, not assumed.
        self.assertEqual(self.map_row({
            "id": 1, "ds_status": "accessioning", "public": True,
            "authority": False, "lifecycle_state": None}), "pending")


# ---------------------------------------------------------------------------
# whg-places.py: client-side selection and per-dataset staging
# ---------------------------------------------------------------------------


def _load_whg_places():
    path = Path(__file__).resolve().parent.parent / "authorities" / "whg-places.py"
    spec = importlib.util.spec_from_file_location("whg_places_sel", str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _feature(pk, src_id=None):
    props = {"title": f"Place {pk}"}
    if src_id is not None:
        props["src_id"] = src_id
    return {"type": "Feature", "id": pk, "geometry": None, "properties": props}


class TestExtractSelection(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = _load_whg_places()

    def test_client_check_only_restricts(self):
        datasets = [
            _ds(id=1, dataset_status="published"),                        # legacy ok
            _ds(id=2, dataset_status="published", lifecycle_state="submitted"),
            _ds(id=3, dataset_status="pending"),                          # Django says no
            _ds(id=4, dataset_status="published", lifecycle_state="published",
                authority=False),                                         # v4 published
        ]
        to_stage, rejected = self.m.select_datasets(datasets)
        self.assertEqual([(d["id"], s) for d, s in to_stage],
                         [(1, "published"), (4, "published")])
        self.assertEqual({r["id"]: sel.reason_bucket(r["reason"]) for r in rejected},
                         {2: sel.REASON_NOT_PUBLISHED, 3: "django_dataset_status"})

    def test_include_pending_stages_rejects_as_pending(self):
        datasets = [_ds(id=1, dataset_status="published"),
                    _ds(id=2, dataset_status="published", lifecycle_state="submitted")]
        to_stage, rejected = self.m.select_datasets(datasets, include_pending=True)
        self.assertEqual([(d["id"], s) for d, s in to_stage],
                         [(1, "published"), (2, "pending")])
        self.assertEqual(rejected, [])

    def test_named_dataset_not_indexable_is_refused(self):
        discovered = [_ds(id=7, dataset_status="published", lifecycle_state="draft")]
        to_stage, rejected = self.m.select_datasets(discovered)
        with self.assertRaises(SystemExit) as cm:
            self.m._refuse_if_named_dataset_not_indexable(
                [7, 8], discovered, to_stage, rejected)
        self.assertEqual(cm.exception.code, 2)
        text = str(cm.exception)
        self.assertIn("whg:7 not indexable", text)
        self.assertIn("whg:8 not returned", text)

    def test_named_indexable_dataset_passes(self):
        discovered = [_ds(id=7, dataset_status="published")]
        to_stage, rejected = self.m.select_datasets(discovered)
        self.m._refuse_if_named_dataset_not_indexable([7], discovered, to_stage, rejected)


class TestPerDatasetStaging(unittest.TestCase):
    """A ``--dataset`` run writes under ``whg/datasets/<id>/extract/`` and
    never touches another dataset's files or the full run's ``whg/extract/``."""

    @classmethod
    def setUpClass(cls):
        from tests._sandbox import assert_sandboxed
        assert_sandboxed()
        cls.m = _load_whg_places()

    def _run(self, td, *, dataset_filter, discovered, features_by_id,
             include_pending=False):
        m = self.m

        def fake_open(session, *, base_url, dataset_id):
            return dataset_id

        def fake_iter(stream):
            return iter(features_by_id[stream])

        import processing.settings as settings
        with mock.patch.dict(os.environ, {"STAGED_BASE_DIR": td}), \
                mock.patch.object(settings, "STAGED_BASE_DIR", td), \
                mock.patch.object(m, "discover_authority_datasets",
                                  return_value=discovered), \
                mock.patch.object(m, "_open_lpf_stream", side_effect=fake_open), \
                mock.patch.object(m, "iter_features", side_effect=fake_iter), \
                mock.patch.object(m, "_resolve_token", return_value=None):
            return m.stage_all_authority_datasets(
                base_url="http://whg.invalid", dataset_filter=dataset_filter,
                include_pending=include_pending)

    def _read_jsonl(self, path):
        return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]

    def test_per_dataset_run_does_not_touch_other_dataset_or_full_run(self):
        from processing.whg_id_map import default_id_map_path, load_id_map
        discovered = [_ds(id=10, dataset_status="published"),
                      _ds(id=20, dataset_status="published")]
        feats = {10: [_feature(1, "a"), _feature(2, "b")],
                 20: [_feature(3, "c")]}
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # Pre-existing artefacts that a per-dataset run must leave alone.
            full_extract = base / "whg" / "extract"
            full_extract.mkdir(parents=True)
            (full_extract / "places.jsonl").write_text("FULL-RUN\n")
            (full_extract / "id_map.jsonl").write_text("FULL-RUN-MAP\n")
            other = base / "whg" / "datasets" / "20" / "extract"
            other.mkdir(parents=True)
            (other / "places.jsonl").write_text("OTHER\n")
            (other / "id_map.jsonl").write_text("OTHER-MAP\n")
            sidecar = base / "_aggregates" / "whg.datasets.json"
            sidecar.parent.mkdir(parents=True)
            sidecar.write_text(json.dumps({"namespace": "whg", "datasets": [
                {"id": "whg:20", "name": "kept", "record_count": 1},
                {"id": "whg:10", "name": "stale", "record_count": 999},
            ]}))

            summary = self._run(td, dataset_filter=[10], discovered=discovered,
                                features_by_id=feats)

            self.assertEqual(summary["layout"], "per-dataset")
            self.assertEqual(list(summary["per_dataset"]), ["10"])
            # Dataset 10 landed in its own directory, docs still say whg.
            mine = base / "whg" / "datasets" / "10" / "extract"
            docs = self._read_jsonl(mine / "places.jsonl")
            self.assertEqual([d["place_id"] for d in docs], ["whg:10:a", "whg:10:b"])
            self.assertTrue(all(d["namespace"] == "whg" for d in docs))
            self.assertTrue(all(d["dataset_status"] == "published" for d in docs))
            idmap = load_id_map(default_id_map_path(td, dataset=10))
            self.assertEqual(idmap.resolve(10, 1), "whg:10:a")
            self.assertEqual(len(idmap), 2)
            self.assertEqual(summary["id_map_entries"], 2)
            # Nothing else moved.
            self.assertEqual((full_extract / "places.jsonl").read_text(), "FULL-RUN\n")
            self.assertEqual((full_extract / "id_map.jsonl").read_text(), "FULL-RUN-MAP\n")
            self.assertEqual((other / "places.jsonl").read_text(), "OTHER\n")
            self.assertEqual((other / "id_map.jsonl").read_text(), "OTHER-MAP\n")
            # Sidecar was upserted, not replaced.
            side = json.loads(sidecar.read_text())
            by_id = {e["id"]: e for e in side["datasets"]}
            self.assertEqual(set(by_id), {"whg:10", "whg:20"})
            self.assertEqual(by_id["whg:20"]["name"], "kept")
            self.assertEqual(by_id["whg:10"]["record_count"], 2)

    def test_per_dataset_rerun_replaces_wholesale(self):
        from processing.whg_id_map import default_id_map_path, load_id_map
        discovered = [_ds(id=10, dataset_status="published")]
        with tempfile.TemporaryDirectory() as td:
            # First run: two records with a duplicate src_id — the second takes
            # the four-segment id, by stream order.
            self._run(td, dataset_filter=[10], discovered=discovered,
                      features_by_id={10: [_feature(1, "x"), _feature(2, "x")]})
            m1 = load_id_map(default_id_map_path(td, dataset=10))
            self.assertEqual(m1.resolve(10, 2), "whg:10:x:2")
            # Second run in the other order: a map that appended would still
            # resolve place 2 to the suffixed id; a truncated one does not.
            self._run(td, dataset_filter=[10], discovered=discovered,
                      features_by_id={10: [_feature(2, "x"), _feature(1, "x")]})
            m2 = load_id_map(default_id_map_path(td, dataset=10))
            self.assertEqual(m2.resolve(10, 2), "whg:10:x")
            self.assertEqual(m2.resolve(10, 1), "whg:10:x:1")
            self.assertEqual(m2.run_ids, ["ad-hoc"], "one meta line: file was truncated")
            docs = self._read_jsonl(
                Path(td) / "whg" / "datasets" / "10" / "extract" / "places.jsonl")
            self.assertEqual(len(docs), 2, "places.jsonl was truncated, not appended")

    def test_non_indexable_named_dataset_writes_nothing_and_exits_2(self):
        discovered = [_ds(id=10, dataset_status="published", lifecycle_state="submitted")]
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._run(td, dataset_filter=[10], discovered=discovered,
                          features_by_id={10: [_feature(1, "a")]})
            self.assertEqual(cm.exception.code, 2)
            self.assertFalse((Path(td) / "whg").exists())

    def test_include_pending_stages_non_indexable_as_pending(self):
        discovered = [_ds(id=10, dataset_status="published", lifecycle_state="submitted")]
        with tempfile.TemporaryDirectory() as td:
            summary = self._run(td, dataset_filter=[10], discovered=discovered,
                                features_by_id={10: [_feature(1, "a")]},
                                include_pending=True)
            docs = self._read_jsonl(
                Path(td) / "whg" / "datasets" / "10" / "extract" / "places.jsonl")
            self.assertEqual([d["dataset_status"] for d in docs], ["pending"])
            self.assertEqual(summary["per_dataset"]["10"]["dataset_status"], "pending")

    def test_full_run_keeps_shared_paths(self):
        from processing.whg_id_map import default_id_map_path, load_id_map
        discovered = [_ds(id=10, dataset_status="published"),
                      _ds(id=20, dataset_status="published", lifecycle_state="withdrawn")]
        with tempfile.TemporaryDirectory() as td:
            summary = self._run(td, dataset_filter=None, discovered=discovered,
                                features_by_id={10: [_feature(1, "a")], 20: [_feature(2, "b")]})
            self.assertEqual(summary["layout"], "full")
            self.assertEqual(list(summary["per_dataset"]), ["10"])
            self.assertEqual([r["id"] for r in summary["rejected"]], [20])
            docs = self._read_jsonl(Path(td) / "whg" / "extract" / "places.jsonl")
            self.assertEqual([d["place_id"] for d in docs], ["whg:10:a"])
            self.assertEqual(len(load_id_map(default_id_map_path(td))), 1)
            self.assertFalse((Path(td) / "whg" / "datasets").exists())


if __name__ == "__main__":
    unittest.main()
