"""The places path must stream the staged corpus, not materialise it.

THE DEFECT THIS PINS. ``collect_attestations`` returned every place doc it had
walked past, as a list, so ``index_namespace`` held the whole namespace in RAM
before writing a single document. At ukhc's 92 docs that is free. At tgn's
2,991,143 it reached 9.7 GB RSS still climbing at 2.6 GB/min — on the VM that
also hosts production Elasticsearch, which is the standing "no heavy compute on
the pitt VM" prohibition, and the shape of an incident that once took production
down for about an hour. Nothing reported a problem: the run simply grew.

WHY THIS TEST CAN FAIL. Memory ceilings are awkward to assert and RSS is noisy,
so the test asserts on the OBSERVABLE that separates the two designs — WHEN the
source is read. Materialising reads every document before emitting the first
bulk action; streaming reads one. The probe counts documents pulled from the
source at the moment the first action appears: old code 1,000, new code 1. A
run against the previous implementation fails on that number alone.

It also pins the completeness check: a bulk that ends early reports no errors,
so ``ok`` is compared against a count taken on a SEPARATE pass.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from processing import index_namespace


def _docs(n):
    return [{"place_id": f"t:{i}", "toponyms": [{"toponym_id": f"N{i}@en"}]}
            for i in range(n)]


class PlacesPathStreamsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "places.jsonl"
        self.src.write_text("\n".join(json.dumps(d) for d in _docs(1000)) + "\n",
                            encoding="utf-8")

    def _counting_factory(self):
        """Yield docs while recording how many have been pulled so far."""
        state = {"pulled": 0}

        def factory():
            for doc in index_namespace.iter_place_docs(self.src):
                state["pulled"] += 1
                yield doc

        return factory, state

    def test_first_action_does_not_require_reading_the_corpus(self):
        factory, state = self._counting_factory()
        pulled_at_first_action = {}

        def fake_bulk(es, actions, **kw):
            first = next(actions)
            pulled_at_first_action["n"] = state["pulled"]
            self.assertEqual(first["_id"], "t:0")
            n = 1 + sum(1 for _ in actions)
            return n, []

        real_bulk = index_namespace.es_helpers.bulk
        index_namespace.es_helpers.bulk = fake_bulk
        try:
            with redirect_stdout(io.StringIO()):
                index_namespace.plan_and_index_places(
                    None, "places_x", "t", factory,
                    n_docs=1000, uncovered=0, replace=False,
                    execute=True, allow_missing_h3=False)
        finally:
            index_namespace.es_helpers.bulk = real_bulk

        self.assertEqual(
            pulled_at_first_action["n"], 1,
            "the corpus was read before the first bulk action was emitted — "
            "this is the materialising design the streaming rewrite removed")

    def test_collect_attestations_returns_no_place_docs(self):
        records, meta = index_namespace.collect_attestations(
            index_namespace.iter_place_docs(self.src))
        self.assertEqual(len(records), 1000)
        self.assertNotIn("place_docs", meta,
                         "collect_attestations is still materialising the corpus")

    def test_short_read_is_reported_despite_zero_errors(self):
        """A generator that ends early exits clean; the count must catch it."""
        def truncated():
            for i, doc in enumerate(index_namespace.iter_place_docs(self.src)):
                if i >= 400:
                    return
                yield doc

        def fake_bulk(es, actions, **kw):
            return sum(1 for _ in actions), []   # no errors at all

        real_bulk = index_namespace.es_helpers.bulk
        index_namespace.es_helpers.bulk = fake_bulk
        err = io.StringIO()
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                index_namespace.plan_and_index_places(
                    None, "places_x", "t", truncated,
                    n_docs=1000, uncovered=0, replace=False,
                    execute=True, allow_missing_h3=False)
        finally:
            index_namespace.es_helpers.bulk = real_bulk

        self.assertIn("PRE-SCAN COUNTED", err.getvalue())
        self.assertIn("400", err.getvalue())

    def test_scan_reports_its_denominator(self):
        n, uncovered = index_namespace.scan_places(
            index_namespace.iter_place_docs(self.src))
        self.assertEqual((n, uncovered), (1000, 0))


if __name__ == "__main__":
    unittest.main()
