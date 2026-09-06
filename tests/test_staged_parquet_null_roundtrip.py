"""The parquet round-trip must not add null keys to documents bound for ES.

THE DEFECT THIS PINS. A parquet struct column carries ONE schema for the whole
file, so reading it back materialises every key any row used — as an explicit
null on the rows that lacked it. A timespan written `{"start": {"latest": 2026}}`
comes back as `{"start": {"in": None, "latest": 2026}}` whenever some other
document in the same file carried `in`. The live index shows exactly that
fingerprint: `osm` and `nl` clean (one shape per file), `tgn` carrying
`in: null` (attested_at + lifespan together), `wd` carrying all three,
`ohm` carrying a wholly null `end`.

⚠ The failure is INVISIBLE to queries — Elasticsearch does not index nulls, so
every count and filter is unaffected — and visible only in `_source`. It
therefore cannot be caught by the aggregations an audit would naturally use, and
it silently changes any canonical-JSON hash taken over `_source`.

The test writes the mixed-shape file that provokes it, rather than asserting on
a fixture that already contains nulls: a fixture built from one shape cannot
reach the divergence at all.
"""
import json
import tempfile
import unittest
from pathlib import Path

from processing.staged_parquet import drop_nulls_for_parquet, write_parquet_from_jsonl


class ParquetNullRoundTripTest(unittest.TestCase):

    def _mixed_shape_file(self, tmp: Path) -> Path:
        """Two docs whose timespans use DIFFERENT keys — the provoking case."""
        docs = [
            {"place_id": "x:1", "toponyms": [{"toponym_id": "A@en", "timespans": [
                {"start": {"latest": 2026}, "end": {"earliest": 2026}}]}]},
            {"place_id": "x:2", "toponyms": [{"toponym_id": "B@en", "timespans": [
                {"start": {"in": 1500}, "end": {"in": 1600}}]}]},
        ]
        jsonl = tmp / "places.jsonl"
        jsonl.write_text("\n".join(json.dumps(d) for d in docs) + "\n", encoding="utf-8")
        parquet = tmp / "places.parquet"
        write_parquet_from_jsonl(jsonl, parquet)
        return parquet

    def test_the_roundtrip_really_does_add_nulls(self):
        """Prove the defect exists before testing the fix, or the fix is decoration."""
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as d:
            rows = pq.ParquetFile(self._mixed_shape_file(Path(d))).read().to_pylist()
        start = rows[0]["toponyms"][0]["timespans"][0]["start"]
        self.assertIn("in", start, "the round-trip did not materialise the union — "
                                   "this test can no longer reach the defect")
        self.assertIsNone(start["in"])

    def test_drop_nulls_removes_them_and_changes_nothing_else(self):
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as d:
            rows = pq.ParquetFile(self._mixed_shape_file(Path(d))).read().to_pylist()
        cleaned = [drop_nulls_for_parquet(r) for r in rows]
        self.assertEqual(cleaned[0]["toponyms"][0]["timespans"][0],
                         {"start": {"latest": 2026}, "end": {"earliest": 2026}})
        self.assertEqual(cleaned[1]["toponyms"][0]["timespans"][0],
                         {"start": {"in": 1500}, "end": {"in": 1600}})
        for r in cleaned:
            self.assertEqual(r["place_id"], {"x:1": "x:1", "x:2": "x:2"}[r["place_id"]])

    def test_single_shape_file_is_clean_without_the_fix(self):
        """The control that explains the live pattern: one shape per file, no nulls.

        Without this, 'osm is clean' looks like luck rather than a consequence.
        """
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            jsonl = tmp / "p.jsonl"
            jsonl.write_text(json.dumps(
                {"place_id": "x:1", "toponyms": [{"toponym_id": "A@en", "timespans": [
                    {"start": {"latest": 2026}, "end": {"earliest": 2026}}]}]}) + "\n",
                encoding="utf-8")
            parquet = tmp / "p.parquet"
            write_parquet_from_jsonl(jsonl, parquet)
            rows = pq.ParquetFile(parquet).read().to_pylist()
        self.assertEqual(rows[0]["toponyms"][0]["timespans"][0],
                         {"start": {"latest": 2026}, "end": {"earliest": 2026}})

    def test_both_indexers_apply_the_strip(self):
        """Guards the actual fix site. Reading parquet without this is the bug."""
        import inspect
        from processing import index_from_stage, index_namespace
        for mod, fn in ((index_from_stage, "_iter_staged_docs"),
                        (index_namespace, "iter_place_docs")):
            src = inspect.getsource(getattr(mod, fn))
            self.assertIn("drop_nulls_for_parquet", src,
                          f"{mod.__name__}.{fn} reads parquet without stripping nulls")


if __name__ == "__main__":
    unittest.main()
