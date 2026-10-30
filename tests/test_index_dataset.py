"""``index_namespace --namespace whg --dataset <id>`` (place#341, plan items 3, 4, 7).

What is pinned here, and why each check can fail:

* **Prefix scoping with the trailing colon.** The fake Elasticsearch below
  EVALUATES prefix queries (it is not a recorder of calls), so a delete scoped
  to ``whg:1:`` is shown to leave ``whg:12:*`` standing and a strip scoped the
  same way to leave a ``whg:12`` attestation in place. A prefix built without
  the colon (``whg:1``) deletes ``whg:12:a`` in this fake, and the test fails.

* **Retraction refuses an indexable dataset — after first succeeding on a
  non-indexable one.** The success half is the control: without it a refusal
  proves only that the code raised, not that it raised for the stated reason.

* **Never-index-pending at the write step.** A pending dataset run with
  ``--replace --execute`` must leave the live index byte-identical: not only
  no bulk, but no delete either, because the delete precedes the add.

* **The Painless strip script is parametrised, not run.** The Python emulation
  in ``FakeES.update_by_query`` mirrors the script's stated semantics; the
  script text itself is pinned separately so the two cannot drift silently.

Writes only to a tempdir patched over ``index_namespace.STAGED_BASE_DIR``;
``assert_sandboxed`` refuses to run if the settings resolved to real storage.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

try:
    from ._sandbox import assert_sandboxed
except ImportError:  # pragma: no cover
    from _sandbox import assert_sandboxed

from processing import index_namespace as ixn


# ---------------------------------------------------------------------------
# A fake Elasticsearch that evaluates the queries index_namespace sends
# ---------------------------------------------------------------------------

class _FakeIndices:
    def __init__(self, es):
        self.es = es
        self.refreshed: list[str] = []

    def get_alias(self, name):
        return {f"{name}_x": {"aliases": {name: {}}}}

    def refresh(self, index):
        self.refreshed.append(index)


class FakeES:
    """Holds two indices as ``{_id: _source}`` and answers count /
    delete_by_query / update_by_query / mget by evaluating the query."""

    def __init__(self):
        self.docs: dict[str, dict[str, dict]] = {"places_x": {}, "toponyms_x": {}}
        self.indices = _FakeIndices(self)
        self.calls: list[tuple] = []

    # -- client surface -----------------------------------------------------
    def info(self):
        return {"version": {"number": "9.0.0-fake"}}

    def options(self, **kw):
        return self

    def _matching(self, index, query):
        store = self.docs[index]
        if not query or "match_all" in query:
            return list(store)
        (field, prefix), = query["prefix"].items()
        out = []
        for _id, src in store.items():
            val = src.get(field)
            if isinstance(val, list):
                if any(isinstance(v, str) and v.startswith(prefix) for v in val):
                    out.append(_id)
            elif isinstance(val, str) and val.startswith(prefix):
                out.append(_id)
        return out

    def count(self, index, body=None):
        q = (body or {}).get("query")
        return {"count": len(self._matching(index, q))}

    def delete_by_query(self, index, body, **kw):
        self.calls.append(("delete_by_query", index, body["query"]))
        ids = self._matching(index, body["query"])
        for _id in ids:
            del self.docs[index][_id]
        return {"deleted": len(ids)}

    def update_by_query(self, index, body, **kw):
        """Python rendering of ``_STRIP_NS_SCRIPT``'s documented semantics."""
        self.calls.append(("update_by_query", index, body["query"], body["script"]["params"]))
        params = body["script"]["params"]
        ids = self._matching(index, body["query"])
        for _id in ids:
            src = self.docs[index][_id]
            att = src.get("attestations")
            if att is not None:
                src["attestations"] = [a for a in att if not a.startswith(params["prefix"])]
            still = any(a.startswith(params["ns_prefix"]) for a in (src.get("attestations") or []))
            if not still:
                if src.get("namespaces") is not None:
                    src["namespaces"] = [n for n in src["namespaces"] if n != params["ns"]]
                if src.get("primary_namespace") == params["ns"]:
                    src["primary_namespace"] = (src["namespaces"][0]
                                                if src.get("namespaces") else None)
        return {"updated": len(ids)}

    def mget(self, index, body, _source=False):
        return {"docs": [{"_id": i, "found": i in self.docs[index]} for i in body["ids"]]}


def _fake_bulk(es, actions, **kw):
    """Apply index / scripted-update actions to the fake, as ES would."""
    n = 0
    for a in actions:
        store = es.docs[a["_index"]]
        if a["_op_type"] == "index":
            store[a["_id"]] = dict(a["_source"])
        elif a["_op_type"] == "update":
            p = a["script"]["params"]
            src = store.get(a["_id"])
            if src is None:
                store[a["_id"]] = dict(a["upsert"])
            else:
                src.setdefault("attestations", [])
                for pid in p["place_ids"]:
                    if pid not in src["attestations"]:
                        src["attestations"].append(pid)
                src.setdefault("namespaces", [])
                if p["ns"] not in src["namespaces"]:
                    src["namespaces"].append(p["ns"])
        es.calls.append(("bulk", a["_index"], a["_id"]))
        n += 1
    return n, []


# ---------------------------------------------------------------------------
# Fixture: staged tree + live-index contents
# ---------------------------------------------------------------------------

def _place(pid, *names):
    return {"place_id": pid, "toponyms": [{"toponym_id": f"{n}@en"} for n in names]}


class _DatasetSandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_sandboxed()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ixn-dataset-"))
        for target in (mock.patch.object(ixn, "STAGED_BASE_DIR", str(self.tmp)),
                       mock.patch.object(ixn.es_helpers, "bulk", _fake_bulk),
                       mock.patch.object(ixn, "_refresh_registry_aggregates", lambda ns: None)):
            target.start()
            self.addCleanup(target.stop)
        self.es = FakeES()
        self.live_places(
            _place("whg:1:a", "Alpha"), _place("whg:1:b", "Beta"),
            _place("whg:12:a", "Alpha"), _place("whg:2:x", "Xi"),
            _place("gn:1", "Alpha"),
        )
        # Toponyms as the live index holds them: Alpha attested by whg:1:a,
        # whg:12:a and gn:1; Beta by whg:1:b only.
        self.es.docs["toponyms_x"] = {
            "Alpha@en": {"name": "Alpha", "attestations": ["whg:1:a", "whg:12:a", "gn:1"],
                         "namespaces": ["whg", "gn"], "primary_namespace": "whg"},
            "Beta@en": {"name": "Beta", "attestations": ["whg:1:b"],
                        "namespaces": ["whg"], "primary_namespace": "whg"},
            "Xi@en": {"name": "Xi", "attestations": ["whg:2:x"],
                      "namespaces": ["whg"], "primary_namespace": "whg"},
        }

    def live_places(self, *docs):
        for d in docs:
            self.es.docs["places_x"][d["place_id"]] = dict(d)

    def stage(self, dataset, docs, *, status="published", where="dataset"):
        d = self.tmp / "whg" / "datasets" / dataset / "final"
        d.mkdir(parents=True, exist_ok=True)
        (d / "places.jsonl").write_text(
            "\n".join(json.dumps(x) for x in docs) + "\n", encoding="utf-8")
        self.sidecar(dataset, status, where=where)

    def sidecar(self, dataset, status, *, where="dataset", name="Set", **extra):
        entry = {"id": f"whg:{dataset}", "name": name, "dataset_status": status,
                 "record_count": 1, **extra}
        if where == "dataset":
            p = self.tmp / "whg" / "datasets" / dataset / "dataset.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(entry), encoding="utf-8")
        else:
            p = self.tmp / "_aggregates" / "whg.datasets.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"namespace": "whg", "datasets": [entry]}),
                         encoding="utf-8")

    def run_main(self, *argv, indexable_fn=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            ixn.main(["--es-host", "http://fake:1", *argv],
                     es_factory=lambda host: self.es, indexable_fn=indexable_fn)
        return out.getvalue(), err.getvalue()

    def place_ids(self):
        return sorted(self.es.docs["places_x"])


# ---------------------------------------------------------------------------
# Prefix scoping
# ---------------------------------------------------------------------------

class TestPrefix(unittest.TestCase):
    def test_dataset_prefix_carries_trailing_colon(self):
        self.assertEqual(ixn.dataset_place_prefix("1"), "whg:1:")
        self.assertTrue("whg:1:abc".startswith(ixn.dataset_place_prefix("1")))
        self.assertFalse("whg:12:abc".startswith(ixn.dataset_place_prefix("1")))

    def test_dataset_id_must_be_decimal(self):
        for bad in ("12;rm", "../x", "whg:12", "", " ", "1 2", "0x1"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                ixn.validate_dataset_id(bad)
        self.assertEqual(ixn.validate_dataset_id(" 12 "), "12")

    def test_strip_script_is_parametrised_on_both_prefixes(self):
        # The script cannot be executed here; pin the contract the fake emulates.
        self.assertIn("a.startsWith(params.prefix)", ixn._STRIP_NS_SCRIPT)
        self.assertIn("a.startsWith(params.ns_prefix)", ixn._STRIP_NS_SCRIPT)
        self.assertEqual(ixn.strip_script_params("whg", "whg:1:"),
                         {"prefix": "whg:1:", "ns_prefix": "whg:", "ns": "whg"})
        # A namespace-wide strip is the old behaviour: both prefixes equal.
        self.assertEqual(ixn.strip_script_params("ukhc", "ukhc:")["ns_prefix"], "ukhc:")


class TestReplaceOneDataset(_DatasetSandbox):
    def test_whg_1_replace_does_not_touch_whg_12(self):
        self.stage("1", [_place("whg:1:a", "Alpha"), _place("whg:1:c", "Gamma")])
        out, _ = self.run_main("--namespace", "whg", "--dataset", "1", "--replace", "--execute")

        # whg:1:b gone, whg:1:c added, whg:12:a / whg:2:x / gn:1 untouched.
        self.assertEqual(self.place_ids(),
                         sorted(["gn:1", "whg:1:a", "whg:1:c", "whg:12:a", "whg:2:x"]))
        dbq = [c for c in self.es.calls if c[0] == "delete_by_query"]
        self.assertEqual(dbq, [("delete_by_query", "places_x", {"prefix": {"place_id": "whg:1:"}})])
        self.assertIn("'whg:1:': 2 of 5 docs (before)", out)   # count with denominator
        self.assertIn("0 of 3 docs (after)", out)

        tops = self.es.docs["toponyms_x"]
        # Alpha keeps whg:12:a (so keeps 'whg'), loses the stale whg:1:a and
        # regains it from the re-add. Beta lost its only whg attestation, so
        # 'whg' left its namespaces. Gamma is new, created without embedding.
        self.assertEqual(sorted(tops["Alpha@en"]["attestations"]), ["gn:1", "whg:12:a", "whg:1:a"])
        self.assertIn("whg", tops["Alpha@en"]["namespaces"])
        self.assertEqual(tops["Beta@en"]["attestations"], [])
        self.assertNotIn("whg", tops["Beta@en"]["namespaces"])
        self.assertIsNone(tops["Beta@en"]["primary_namespace"])
        self.assertEqual(tops["Gamma@en"]["attestations"], ["whg:1:c"])
        self.assertNotIn("embedding", tops["Gamma@en"])
        self.assertEqual(tops["Xi@en"]["attestations"], ["whg:2:x"])   # other dataset untouched
        ubq = [c for c in self.es.calls if c[0] == "update_by_query"]
        self.assertEqual(ubq[0][3], {"prefix": "whg:1:", "ns_prefix": "whg:", "ns": "whg"})

    def test_dataset_requires_whg_namespace(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--namespace", "gn", "--dataset", "1")
        self.assertIn("only valid with --namespace whg", str(cm.exception))

    def test_dry_run_writes_nothing_but_reports_counts(self):
        self.stage("1", [_place("whg:1:a", "Alpha")])
        before = json.dumps(self.es.docs, sort_keys=True)
        out, _ = self.run_main("--namespace", "whg", "--dataset", "1", "--replace")
        self.assertEqual(json.dumps(self.es.docs, sort_keys=True), before)
        self.assertIn("'whg:1:': 2 of 5 docs (before)", out)
        self.assertIn("dry-run", out)
        self.assertEqual([c for c in self.es.calls if c[0] != "bulk"], [])


# ---------------------------------------------------------------------------
# Retraction
# ---------------------------------------------------------------------------

class TestRetract(_DatasetSandbox):
    def test_retract_succeeds_when_not_indexable_then_refuses_when_indexable(self):
        # CONTROL FIRST: a dataset the sidecar marks pending is retractable,
        # and the retraction really removes whg:1:* and only whg:1:*.
        self.sidecar("1", "pending")
        out, err = self.run_main("--namespace", "whg", "--dataset", "1", "--retract", "--execute")
        self.assertEqual(self.place_ids(), ["gn:1", "whg:12:a", "whg:2:x"])
        self.assertIn("RETRACT", out)
        self.assertIn("'whg:1:': 2 of 5 docs (before)", out)
        self.assertIn("deleted 2; prefix now matches 0 of 3 docs (after)", out)
        self.assertIn("'whg:1:': 2 of 3 toponyms (before)", out)
        self.assertIn("stripped from 2 toponyms; prefix now matches 0 of 3 (after)", out)
        self.assertEqual([c for c in self.es.calls if c[0] == "bulk"], [], "retract re-added")
        tops = self.es.docs["toponyms_x"]
        self.assertEqual(tops["Alpha@en"]["attestations"], ["whg:12:a", "gn:1"])
        self.assertEqual(tops["Beta@en"]["attestations"], [])
        self.assertNotIn("whg", tops["Beta@en"]["namespaces"])
        self.assertIn("whg", tops["Alpha@en"]["namespaces"])

        # NOW the refusal: whg:12 is published, so it is indexable, so it
        # must not be retracted — and nothing may change.
        self.sidecar("12", "published")
        snapshot = json.dumps(self.es.docs, sort_keys=True)
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--namespace", "whg", "--dataset", "12", "--retract", "--execute")
        self.assertIn("REFUSING to retract whg:12", str(cm.exception))
        self.assertEqual(json.dumps(self.es.docs, sort_keys=True), snapshot)

    def test_retract_refusal_honours_injected_is_indexable(self):
        # Sidecar says published but carries a v4 state; the selection
        # predicate says no → retractable (the v4 state outranks the status).
        self.sidecar("12", "published", lifecycle_state="withdrawn")
        out, _ = self.run_main("--namespace", "whg", "--dataset", "12", "--retract", "--execute",
                               indexable_fn=lambda ds: (False, "lifecycle_state=withdrawn"))
        self.assertIn("lifecycle_state=withdrawn", out)
        self.assertNotIn("whg:12:a", self.place_ids())

    def test_retract_without_sidecar_entry_is_allowed(self):
        # A deleted dataset leaves no sidecar; that is the case retract exists for.
        out, _ = self.run_main("--namespace", "whg", "--dataset", "2", "--retract", "--execute")
        self.assertIn("no staged sidecar entry", out)
        self.assertNotIn("whg:2:x", self.place_ids())

    def test_retract_requires_dataset_and_excludes_replace(self):
        with self.assertRaises(SystemExit):
            self.run_main("--namespace", "whg", "--retract")
        self.sidecar("1", "pending")
        with self.assertRaises(SystemExit):
            self.run_main("--namespace", "whg", "--dataset", "1", "--retract", "--replace")


# ---------------------------------------------------------------------------
# Never-index-pending at the write step
# ---------------------------------------------------------------------------

class TestWriteStepGate(_DatasetSandbox):
    def _assert_untouched(self, snapshot):
        self.assertEqual(json.dumps(self.es.docs, sort_keys=True), snapshot)
        self.assertEqual(self.es.calls, [], "something was written before the gate fired")

    def test_pending_dataset_refused_before_any_write(self):
        self.stage("1", [_place("whg:1:a", "Alpha")], status="pending")
        snapshot = json.dumps(self.es.docs, sort_keys=True)
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--namespace", "whg", "--dataset", "1", "--replace", "--execute")
        self.assertIn("REFUSING to index whg:1", str(cm.exception))
        self.assertIn("dataset_status='pending'", str(cm.exception))
        self._assert_untouched(snapshot)    # in particular: the --replace delete did NOT run

    def test_published_dataset_is_written(self):
        # The gate's positive control: the same run with status=published writes.
        self.stage("1", [_place("whg:1:a", "Alpha")], status="published")
        self.run_main("--namespace", "whg", "--dataset", "1", "--replace", "--execute")
        self.assertNotIn("whg:1:b", self.place_ids())
        self.assertIn("whg:1:a", self.place_ids())

    def test_no_sidecar_at_all_is_refused(self):
        d = self.tmp / "whg" / "datasets" / "1" / "final"
        d.mkdir(parents=True)
        (d / "places.jsonl").write_text(json.dumps(_place("whg:1:a", "Alpha")) + "\n")
        snapshot = json.dumps(self.es.docs, sort_keys=True)
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--namespace", "whg", "--dataset", "1", "--execute")
        self.assertIn("no staged sidecar entry", str(cm.exception))
        self._assert_untouched(snapshot)

    def test_aggregate_sidecar_is_consulted_when_no_per_dataset_one(self):
        self.stage("1", [_place("whg:1:a", "Alpha")], status="pending", where="aggregate")
        with self.assertRaises(SystemExit):
            self.run_main("--namespace", "whg", "--dataset", "1", "--execute")

    def test_per_dataset_sidecar_wins_over_aggregate(self):
        self.stage("1", [_place("whg:1:a", "Alpha")], status="published", where="aggregate")
        self.sidecar("1", "pending", where="dataset")
        meta = ixn.load_whg_dataset_meta("1", self.tmp)
        self.assertEqual(meta["dataset_status"], "pending")

    def test_sidecar_without_selection_inputs_is_judged_on_dataset_status_alone(self):
        """The production sidecar shape (no lifecycle_state / public / authority /
        ds_status) must NOT be fed to the real ``whg_selection.is_indexable`` —
        which, lacking its inputs, answers "no" for every dataset. This test
        runs with the real module importable and no injection; before the
        ``_carries_selection_fields`` guard it refused a published dataset
        with reason ``legacy_not_indexable: authority; public; ...``."""
        try:
            import processing.whg_selection  # noqa: F401
        except ImportError:
            self.skipTest("processing.whg_selection not present")
        self.stage("1", [_place("whg:1:a", "Alpha")], status="published")
        self.assertEqual(ixn.dataset_indexable(ixn.load_whg_dataset_meta("1", self.tmp)),
                         (True, "dataset_status='published'"))
        self.run_main("--namespace", "whg", "--dataset", "1", "--execute")
        self.assertIn(("bulk", "places_x", "whg:1:a"), self.es.calls)

    def test_real_predicate_consulted_when_entry_carries_lifecycle_state(self):
        try:
            import processing.whg_selection  # noqa: F401
        except ImportError:
            self.skipTest("processing.whg_selection not present")
        # dataset_status says published; a v4 state on the same entry says withdrawn.
        self.sidecar("1", "published", lifecycle_state="withdrawn", public=True)
        ok, reason = ixn.dataset_indexable(ixn.load_whg_dataset_meta("1", self.tmp))
        self.assertFalse(ok)
        self.assertIn("not_published", reason)
        # ...and a published v4 state that is public passes.
        self.sidecar("1", "published", lifecycle_state="published", public=True)
        self.assertTrue(ixn.dataset_indexable(ixn.load_whg_dataset_meta("1", self.tmp))[0])

    def test_injected_is_indexable_can_veto_and_a_raising_one_fails_closed(self):
        self.stage("1", [_place("whg:1:a", "Alpha")], status="published")
        self.sidecar("1", "published", lifecycle_state="in_review")
        snapshot = json.dumps(self.es.docs, sort_keys=True)
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--namespace", "whg", "--dataset", "1", "--execute",
                          indexable_fn=lambda ds: (False, "lifecycle_state=in_review"))
        self.assertIn("lifecycle_state=in_review", str(cm.exception))
        self._assert_untouched(snapshot)

        def boom(ds):
            raise ValueError("unknown lifecycle_state 'zzz'")
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--namespace", "whg", "--dataset", "1", "--execute", indexable_fn=boom)
        self.assertIn("is_indexable raised", str(cm.exception))
        self._assert_untouched(snapshot)

    def test_dry_run_announces_the_refusal_it_would_make(self):
        self.stage("1", [_place("whg:1:a", "Alpha")], status="pending")
        out, _ = self.run_main("--namespace", "whg", "--dataset", "1")
        self.assertIn("--execute would REFUSE", out)


if __name__ == "__main__":
    unittest.main()
