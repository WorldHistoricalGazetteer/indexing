#!/usr/bin/env python3
"""The copy-then-swap merge preserves what a CTAS would silently drop.

🛑 THE POINT OF THESE TESTS IS THE NEGATIVE CONTROL. Asserting that the merge
keeps a PRIMARY KEY proves nothing unless the alternative demonstrably loses it
-- otherwise the test passes on a DuckDB that preserves constraints through any
path, and would pass on the broken implementation too. `test_ctas_loses_the_pk`
runs the obvious wrong way and asserts the guarantee is GONE, so every other
assertion here has something to be different from.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import duckdb

from phonetics.ipa.panphon_repop import BUCKET_EXPR, merge

BUCKETS = 2


def _build_source(path: Path) -> None:
    """A miniature of the real inventory: one plain table, one constrained."""
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE toponyms(
            toponym_id VARCHAR, "name" VARCHAR, lang VARCHAR,
            lang_variant VARCHAR, script VARCHAR, ipa VARCHAR,
            panphon_features BLOB)
    """)
    con.execute("CREATE INDEX idx_toponyms_id ON toponyms(toponym_id)")
    con.execute("CREATE INDEX idx_toponyms_lang ON toponyms(lang)")
    con.execute("CREATE INDEX idx_toponyms_script ON toponyms(script)")
    # The untouched, CONSTRAINED table — the thing db's fresh-file plan had to
    # reproduce by hand and this plan preserves by never touching.
    con.execute("""
        CREATE TABLE script_stats(script VARCHAR PRIMARY KEY, count INTEGER)
    """)
    con.execute("INSERT INTO script_stats VALUES ('LATIN', 7), ('CYRILLIC', 3)")
    rows = [(f"n{i}@en", f"name{i}", "en", None, "LATIN", f"ipa{i}", None)
            for i in range(40)]
    con.executemany("INSERT INTO toponyms VALUES (?,?,?,?,?,?,?)", rows)
    con.close()


def _write_shards(src: Path, out_dir: Path) -> int:
    """Fake features for every row, bucketed exactly as `merge` will join."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"ATTACH '{src}' AS inv (READ_ONLY)")
    expr = BUCKET_EXPR.format(n=BUCKETS)
    total = 0
    for k in range(BUCKETS):
        ids = [r[0] for r in con.execute(
            f"SELECT toponym_id FROM inv.toponyms WHERE {expr} = {k}").fetchall()]
        total += len(ids)
        pq.write_table(
            pa.table({"toponym_id": pa.array(ids, pa.string()),
                      "panphon_features": pa.array([b"\x00" * 96] * len(ids),
                                                   pa.binary())}),
            out_dir / f"features.b{k:04d}.parquet")
    con.close()
    return total


class TestCopyThenSwapMerge(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        d = Path(self._td.name)
        self.src = d / "inventory.db"
        self.dst = d / "candidate.db"
        self.shards = d / "shards"
        _build_source(self.src)
        self.n_features = _write_shards(self.src, self.shards)
        self.src_bytes = self.src.read_bytes()

    def tearDown(self):
        self._td.cleanup()

    def _run(self):
        rc = merge(str(self.src), str(self.shards / "features.*.parquet"),
                   BUCKETS, str(self.dst),
                   temp_dir=str(Path(self._td.name) / "tmp"), max_temp="1GB",
                   verify_copy=True, execute=True)
        self.assertEqual(rc, 0)

    # ---- the negative control, first: the obvious way LOSES the guarantee ----

    def test_ctas_loses_the_pk(self):
        """Without this, every assertion below could pass on a broken merge."""
        con = duckdb.connect(str(self.dst))
        con.execute(f"ATTACH '{self.src}' AS s (READ_ONLY)")
        con.execute("CREATE TABLE script_stats AS SELECT * FROM s.script_stats")
        n = con.execute("""SELECT count(*) FROM duckdb_constraints()
                           WHERE table_name = 'script_stats'
                             AND database_name = 'candidate'""").fetchone()[0]
        con.close()
        self.assertEqual(n, 0, "CTAS kept the PK — the hazard this guards "
                               "against does not exist and these tests are moot")

    # ---- and now the merge, which must not ----------------------------------

    def test_untouched_table_keeps_its_primary_key(self):
        self._run()
        con = duckdb.connect(str(self.dst), read_only=True)
        n = con.execute("""SELECT count(*) FROM duckdb_constraints()
                           WHERE table_name = 'script_stats'""").fetchone()[0]
        con.close()
        self.assertGreater(n, 0, "the PK on the untouched table was lost")

    def test_indexes_are_recreated(self):
        self._run()
        con = duckdb.connect(str(self.dst), read_only=True)
        idx = {r[0] for r in con.execute(
            "SELECT index_name FROM duckdb_indexes() "
            "WHERE table_name = 'toponyms'").fetchall()}
        con.close()
        self.assertEqual(idx, {"idx_toponyms_id", "idx_toponyms_lang",
                               "idx_toponyms_script"})

    def test_features_land_on_the_right_rows(self):
        self._run()
        con = duckdb.connect(str(self.dst), read_only=True)
        rows, feats = con.execute(
            "SELECT count(*), count(panphon_features) FROM toponyms").fetchone()
        # ⚠ The bucketed insert joins bucket k against shard k. A mismatched
        # bucket expression would not error — it would join the wrong rows and
        # leave the rest NULL, which is exactly the silent shape we are guarding.
        orphan = con.execute("""SELECT count(*) FROM toponyms
                                WHERE ipa IS NOT NULL
                                  AND panphon_features IS NULL""").fetchone()[0]
        con.close()
        self.assertEqual(rows, 40)
        self.assertEqual(feats, self.n_features)
        self.assertEqual(orphan, 0, "rows with ipa left without features — "
                                    "the bucket join is misaligned")

    def test_original_is_byte_identical_afterwards(self):
        self._run()
        self.assertEqual(self.src.read_bytes(), self.src_bytes,
                         "the source inventory was modified — the entire point "
                         "of copy-then-swap is that it is not")

    def test_refuses_an_existing_candidate(self):
        self.dst.write_bytes(b"someone else's work")
        with self.assertRaises(SystemExit):
            self._run()

    def test_refuses_a_missing_shard(self):
        (self.shards / "features.b0001.parquet").unlink()
        with self.assertRaises(SystemExit):
            merge(str(self.src), str(self.shards / "features.*.parquet"),
                  BUCKETS, str(self.dst),
                  temp_dir=str(Path(self._td.name) / "tmp"), max_temp="1GB",
                  verify_copy=False, execute=True)


if __name__ == "__main__":
    unittest.main()
