"""Per-stage timing for /api/reconcile is opt-in, accurate, and inert when off.

Pure-function / stubbed tests: no live Elasticsearch.

What is asserted, and why each matters:

* ``_Stages`` attributes elapsed time to the named stage, accumulates repeats,
  and keeps the MAX of parallel ES ``took`` values (the stage waited on the
  slowest).
* ``emit`` writes ONE parseable JSON line with the documented keys, and a failure
  inside instrumentation never reaches the caller.
* The wrapper is inert at the default sample rate (0): no stage object, no log
  line, same return value.
* Sampling is every Nth request; an exception in the handler is re-raised
  unchanged AND logged with its type, because the slow/failed requests are the
  ones worth seeing.
* Every stage the endpoint is supposed to mark IS marked (AST walk of the real
  function), so deleting an instrumentation call fails here rather than silently
  leaving a hole in the breakdown.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import pathlib
import unittest
from unittest import mock

try:
    from gateway import reconcile as rc
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"gateway serving deps unavailable: {exc}")


def _lines(cm):
    return [json.loads(r.getMessage().split("reconcile_timing ", 1)[1]) for r in cm.records]


class StagesUnit(unittest.TestCase):
    def test_mark_attributes_time_and_accumulates(self):
        clock = iter([0.0, 0.010, 0.030, 0.035])
        with mock.patch.object(rc.time, "perf_counter", lambda: next(clock)):
            st = rc._Stages(rc.ReconcileRequest(query="x"))   # t0 = 0.000
            st.mark("a")                                       # +10 ms
            st.mark("b")                                       # +20 ms
            st.mark("a")                                       # +5 ms  -> a = 15
        self.assertAlmostEqual(st.ms["a"], 15.0)
        self.assertAlmostEqual(st.ms["b"], 20.0)

    def test_took_keeps_max_and_ignores_garbage(self):
        st = rc._Stages(rc.ReconcileRequest(query="x"))
        st.took("discovery", {"took": 40})
        st.took("discovery", {"took": 90})
        st.took("discovery", {"took": 10})
        st.took("places", {})          # no `took` -> ignored
        st.took("places", None)        # not a dict -> ignored
        st.took("places", {"took": "n/a"})
        self.assertEqual(st.took_ms, {"discovery": 90})

    def test_emit_is_one_json_line_with_documented_keys(self):
        st = rc._Stages(rc.ReconcileRequest(query="Sarum", ccodes=["GB"]))
        st.mark("prep")
        st.n("surviving", 200)
        with self.assertLogs("gateway.reconcile.timing", "INFO") as cm:
            st.emit()
        (rec,) = _lines(cm)
        for key in ("total_ms", "stages_ms", "unmarked_ms", "es_took_ms", "n",
                    "mode", "size", "qlen", "variants", "client_vector", "scoped"):
            self.assertIn(key, rec)
        self.assertIs(rec["scoped"], True)
        self.assertEqual(rec["qlen"], 5)
        self.assertEqual(rec["n"], {"surviving": 200})
        self.assertNotIn("error", rec)

    def test_emit_failure_never_propagates(self):
        st = rc._Stages(rc.ReconcileRequest(query="x"))
        with mock.patch.object(rc.json, "dumps", side_effect=RuntimeError("boom")):
            st.emit()   # must not raise

    def test_null_stages_accepts_every_call(self):
        n = rc._NULL_STAGES
        n.mark("x"); n.n("k", 1); n.took("s", {"took": 1}); n.emit(); n.emit("E")


class WrapperBehaviour(unittest.TestCase):
    def _call(self, stub):
        req = rc.ReconcileRequest(query="Sarum")
        with mock.patch.object(rc, "_reconcile_search", stub):
            return asyncio.run(rc.reconcile_search(req))

    def test_off_by_default_logs_nothing_and_returns_the_result(self):
        async def stub(req, st):
            self.assertIs(st, rc._NULL_STAGES)
            return "RESULT"
        with mock.patch.object(rc, "_STAGE_SAMPLE", 0):
            with self.assertNoLogs("gateway.reconcile.timing", "INFO"):
                self.assertEqual(self._call(stub), "RESULT")

    def test_sample_of_one_logs_every_request(self):
        async def stub(req, st):
            st.mark("prep")
            return "R"
        with mock.patch.object(rc, "_STAGE_SAMPLE", 1):
            with self.assertLogs("gateway.reconcile.timing", "INFO") as cm:
                self._call(stub); self._call(stub)
        self.assertEqual(len(cm.records), 2)

    def test_sample_of_n_logs_every_nth(self):
        async def stub(req, st):
            return "R"
        with mock.patch.object(rc, "_STAGE_SAMPLE", 3), \
                mock.patch.object(rc, "_stage_counter", iter(range(1, 100))):
            with self.assertLogs("gateway.reconcile.timing", "INFO") as cm:
                for _ in range(9):
                    self._call(stub)
        self.assertEqual(len(cm.records), 3)

    def test_exception_is_reraised_unchanged_and_logged_with_its_type(self):
        class Boom(Exception):
            pass

        async def stub(req, st):
            raise Boom("x")
        with mock.patch.object(rc, "_STAGE_SAMPLE", 1):
            with self.assertLogs("gateway.reconcile.timing", "INFO") as cm:
                with self.assertRaises(Boom):
                    self._call(stub)
        (rec,) = _lines(cm)
        self.assertEqual(rec["error"], "Boom")

    def test_openapi_description_survives_the_wrapper(self):
        """The route description was the endpoint's docstring; moving the body into
        a helper must not blank it in /docs."""
        route = next(r for r in rc.router.routes if getattr(r, "path", "").endswith("/reconcile"))
        self.assertIn("Reconciliation-style search", route.description)
        self.assertEqual(route.name, "reconcile_search")


class EveryStageIsMarked(unittest.TestCase):
    EXPECTED_MARKS = {"prep", "region", "embed_build", "build_queries", "es_discovery",
                      "discovery_score", "es_places", "containment", "es_enrich",
                      "enrich_loop", "format", "hardlinks"}
    EXPECTED_TOOK = {"discovery", "places", "enrich"}

    def test_the_endpoint_still_calls_each_instrumentation_point(self):
        src = pathlib.Path(rc.__file__).read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reconcile_search")
        marks, tooks = set(), set()
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "st"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                (marks if node.func.attr == "mark" else tooks if node.func.attr == "took"
                 else set()).add(node.args[0].value)
        self.assertEqual(self.EXPECTED_MARKS - marks, set(), "stage marks removed")
        self.assertEqual(self.EXPECTED_TOOK - tooks, set(), "ES took capture removed")


if __name__ == "__main__":
    unittest.main()


class PerSearchTimingEndToEnd(unittest.TestCase):
    """Drive the REAL endpoint against a stubbed ES and read what it logs."""

    def _run(self, **req_kw):
        from tests._es_stub import FakeES
        fake = FakeES(n_places=30)
        req = rc.ReconcileRequest(query="Sarum", mode="fuzzy", size=10, **req_kw)
        with fake.installed(), \
                mock.patch.object(rc, "_build_phonetic_knn", lambda *a, **k: {"knn": {"stub": 1}, "size": 10}), \
                mock.patch.object(rc, "_STAGE_SAMPLE", 1):
            with self.assertLogs("gateway.reconcile.timing", "INFO") as cm:
                res = asyncio.run(rc.reconcile_search(req))
        return fake, res, _lines(cm)[0]

    def test_each_discovery_search_reports_its_own_server_time_and_yield(self):
        fake, res, rec = self._run()
        for kind in ("knn", "near", "exact"):
            self.assertIn("discovery_" + kind, rec["es_took_ms"], f"no took for {kind}")
            self.assertGreater(rec["n"].get("hits_" + kind, 0), 0, f"no hit count for {kind}")
        self.assertEqual(rec["es_took_ms"]["discovery"], 11)   # the max across them, as before
        self.assertEqual(len(res.hits), 10)

    def test_all_stages_fire_on_the_real_fuzzy_path(self):
        _, _, rec = self._run()
        for stage in ("prep", "embed_build", "es_discovery", "discovery_score",
                      "es_places", "es_enrich", "enrich_loop", "format"):
            self.assertIn(stage, rec["stages_ms"], f"stage {stage} never fired")
