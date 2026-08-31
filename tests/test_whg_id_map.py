"""Tests for the whg place-id map — the artefact that replaced a second
implementation of the id rule.

The map exists because ``whg:<dataset>:<src_id>`` is not derivable in SQL: the
duplicate-src_id case appends the place key and which record counts as the
duplicate turns on LPF stream order. So the extract writes down what it minted
and consumers join. See ``developer/plan-completion-2026-08-31.md`` §2.3.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from processing import whg_id_map as m


def _write(path: Path, lines: list[dict]) -> Path:
    path.write_text("".join(json.dumps(o) + "\n" for o in lines), encoding="utf-8")
    return path


def _meta(run_id: str = "run-1", schema: str = m.SCHEMA) -> dict:
    return {"_meta": {"schema": schema, "run_id": run_id,
                      "namespace": "whg", "generated_at": "2026-08-31T00:00:00+00:00"}}


def _rec(ds: str, key: str, pid: str) -> dict:
    return {"dataset_key": ds, "place_key": key, "place_id": pid}


class TestWriter(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "id_map.jsonl"
            with m.IdMapWriter(p, run_id="run-x") as w:
                w.record(1052, 6954931, "whg:1052:8")
                w.record(1052, 6954932, "whg:1052:9")
            self.assertEqual(w.count, 2)
            loaded = m.load_id_map(p)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded.run_ids, ["run-x"])
            # ints in, strings out — the join must not care which side it came from
            self.assertEqual(loaded.resolve(1052, 6954931), "whg:1052:8")
            self.assertEqual(loaded.resolve("1052", "6954931"), "whg:1052:8")

    def test_append_keeps_both_run_stamps(self):
        # A resumed or per-dataset extract appends. The file should then say it
        # was built by two runs rather than silently look like one.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "id_map.jsonl"
            with m.IdMapWriter(p, run_id="run-a") as w:
                w.record(1, 10, "whg:1:aa")
            with m.IdMapWriter(p, run_id="run-b") as w:
                w.record(2, 20, "whg:2:bb")
            loaded = m.load_id_map(p)
            self.assertEqual(loaded.run_ids, ["run-a", "run-b"])
            self.assertEqual(len(loaded), 2)

    def test_last_record_wins_on_repeated_key(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "id_map.jsonl"
            with m.IdMapWriter(p, run_id="run-a") as w:
                w.record(1, 10, "whg:1:old")
            with m.IdMapWriter(p, run_id="run-b") as w:
                w.record(1, 10, "whg:1:new")
            self.assertEqual(m.load_id_map(p).resolve(1, 10), "whg:1:new")

    def test_write_outside_context_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            w = m.IdMapWriter(Path(td) / "id_map.jsonl")
            with self.assertRaises(RuntimeError):
                w.record(1, 2, "whg:1:3")


class TestLoadFailsLoudly(unittest.TestCase):
    """Every failure is an exception, never a silent empty map — a consumer
    that carried on without the map would mint ids of its own, which is the
    defect the map removes."""

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(m.IdMapUnavailable):
                m.load_id_map(Path(td) / "nope.jsonl")

    def test_no_meta_line(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "f.jsonl", [_rec("1", "2", "whg:1:3")])
            with self.assertRaises(m.IdMapUnavailable):
                m.load_id_map(p)

    def test_no_records(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "f.jsonl", [_meta()])
            with self.assertRaises(m.IdMapUnavailable):
                m.load_id_map(p)

    def test_unknown_schema(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "f.jsonl",
                       [_meta(schema="whg-id-map/99"), _rec("1", "2", "whg:1:3")])
            with self.assertRaisesRegex(m.IdMapUnavailable, "schema"):
                m.load_id_map(p)

    def test_malformed_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "f.jsonl"
            p.write_text(json.dumps(_meta()) + "\n{not json\n", encoding="utf-8")
            with self.assertRaises(m.IdMapUnavailable):
                m.load_id_map(p)

    def test_record_missing_field(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "f.jsonl",
                       [_meta(), {"dataset_key": "1", "place_key": "2"}])
            with self.assertRaisesRegex(m.IdMapUnavailable, "place_id"):
                m.load_id_map(p)


class TestResolveLegacyId(unittest.TestCase):
    """The defensive path for ids minted elsewhere (Django's
    ``api_contributorattestation``). Asymmetric risk: failing to translate a
    legacy id leaves a dangling edge, but rewriting an already-correct id
    corrupts a good one. So 'is it already current?' is tested first."""

    def setUp(self):
        self.map = m.WhgIdMap(
            [{"schema": m.SCHEMA, "run_id": "r"}],
            {("1052", "6954931"): "whg:1052:8",
             ("1052", "6954932"): "whg:1052:9"},
        )

    def test_non_whg_passes_through(self):
        self.assertEqual(self.map.resolve_legacy_id("wd:Q90"), ("wd:Q90", "not_whg"))
        self.assertEqual(self.map.resolve_legacy_id("gn:2988507"),
                         ("gn:2988507", "not_whg"))

    def test_current_id_is_never_rewritten(self):
        self.assertEqual(self.map.resolve_legacy_id("whg:1052:8"),
                         ("whg:1052:8", "already_current"))

    def test_legacy_id_is_translated(self):
        self.assertEqual(self.map.resolve_legacy_id("whg:1052:6954931"),
                         ("whg:1052:8", "remapped"))

    def test_unknown_whg_id_is_dropped(self):
        self.assertEqual(self.map.resolve_legacy_id("whg:9999:1"),
                         (None, "unmatched"))

    def test_four_segment_duplicate_form_resolves(self):
        # whg:<ds>:<src_id>:<place_key> is what a duplicate src_id gets. As a
        # legacy-form lookup the place key is everything after the dataset.
        mp = m.WhgIdMap([{"schema": m.SCHEMA, "run_id": "r"}],
                        {("7", "8:99"): "whg:7:8:99"})
        self.assertEqual(mp.resolve_legacy_id("whg:7:8:99"),
                         ("whg:7:8:99", "already_current"))

    def test_dataset_of(self):
        self.assertEqual(m.WhgIdMap.dataset_of("whg:1052:8"), "1052")
        self.assertEqual(m.WhgIdMap.dataset_of("whg"), "?")


class TestDefaultPath(unittest.TestCase):
    def test_honours_staged_base_dir(self):
        p = m.default_id_map_path("/tmp/staged-x")
        self.assertEqual(p, Path("/tmp/staged-x/whg/extract/id_map.jsonl"))


if __name__ == "__main__":
    unittest.main()
