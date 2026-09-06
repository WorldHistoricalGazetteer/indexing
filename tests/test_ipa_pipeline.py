#!/usr/bin/env python3
"""
Tests for phonetics.ipa.

These deliberately include NEGATIVE controls. A suite that only asserts the
happy path would pass just as well against a store that records nothing and a
merge that ignores missing shards, which is the failure this repository keeps
cataloguing. So each capability is tested by showing it both accept a good
input and REJECT a bad one.

Run package-qualified -- never `unittest discover -s tests`:
    python -m unittest tests.test_ipa_pipeline
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from phonetics.ipa import routes as R
from phonetics.ipa import store as S


class TestRouteTable(unittest.TestCase):
    """RouteTable is tested against a SYNTHETIC installed-mode set, so the
    results do not depend on which machine runs the suite."""

    MODES = {"ceb-Latn", "cat-Latn", "gle-Latn", "eng-Latn", "rus-Cyrl",
             "jpn-Hrgn", "jpn-Ktkn", "ast-Latn"}

    def table(self, **kw):
        return R.RouteTable(modes=set(self.MODES), **kw)

    def test_derives_route_from_installed_mode(self):
        t = self.table()
        route, status = t.resolve("ca", "LATIN")
        self.assertEqual(status, "ok")
        self.assertEqual(route.backend, "epitran")
        self.assertEqual(route.mode, "cat-Latn")
        self.assertEqual(route.reason, "installed-mode")

    def test_absent_mode_yields_no_route_not_a_guess(self):
        # 'sw' (swa-Latn) is deliberately NOT in MODES.
        route, status = self.table().resolve("sw", "LATIN")
        self.assertIsNone(route)
        self.assertEqual(status, "no_route")

    def test_ja_cjk_is_routed(self):
        """The hole this package exists to close: ja+CJK had no route at all."""
        route, status = self.table().resolve("ja", "CJK")
        self.assertEqual(status, "ok")
        self.assertEqual(route.backend, "charsiu")
        self.assertEqual(route.mode, "jpn")

    def test_ja_kana_still_goes_to_epitran_not_charsiu(self):
        """CharsiuG2P only handles Kanji; kana must stay on Epitran."""
        for script, mode in (("HIRAGANA", "jpn-Hrgn"), ("KATAKANA", "jpn-Ktkn")):
            route, status = self.table().resolve("ja", script)
            self.assertEqual(status, "ok")
            self.assertEqual(route.backend, "epitran", script)
            self.assertEqual(route.mode, mode)

    def test_quarantine_blocks_even_when_a_mode_exists(self):
        """ceb-Latn IS installed. The quarantine must still refuse it, or the
        Wikidata edition-label problem walks straight through."""
        self.assertIn("ceb-Latn", self.MODES)
        route, status = self.table().resolve("ceb", "LATIN")
        self.assertIsNone(route)
        self.assertEqual(status, "quarantined")

    def test_quarantine_is_liftable_deliberately(self):
        route, status = self.table(allow_quarantined=True).resolve("ceb", "LATIN")
        self.assertEqual(status, "ok")
        self.assertEqual(route.mode, "ceb-Latn")

    def test_empty_lang_is_its_own_status(self):
        for bad in ("", None, "   "):
            route, status = self.table().resolve(bad, "LATIN")
            self.assertIsNone(route)
            self.assertEqual(status, "no_lang")

    def test_non_language_tags_are_not_treated_as_languages(self):
        route, status = self.table().resolve("genitive", "LATIN")
        self.assertIsNone(route)
        self.assertEqual(status, "non_language_tag")

    def test_lang_subtags_are_normalised(self):
        route, status = self.table().resolve("en-GB", "LATIN")
        self.assertEqual(status, "ok")
        self.assertEqual(route.mode, "eng-Latn")

    def test_code_backed_modes_are_reachable_without_a_csv(self):
        """Regression: routes were derived by globbing epitran's CSV maps, but
        Epitran implements English in code -- there is no eng-Latn.csv. The
        end-to-end run reported 120 of 120 English rows as no_route, i.e. the
        single largest cell in the corpus silently dropped. Here the synthetic
        mode set deliberately contains NO eng-Latn, so the only way this can
        pass is via CODE_BACKED_MODES."""
        self.assertIn("eng-Latn", R.CODE_BACKED_MODES)
        t = R.RouteTable(modes={"cat-Latn"} | R.CODE_BACKED_MODES)
        route, status = t.resolve("en", "LATIN")
        self.assertEqual(status, "ok")
        self.assertEqual(route.mode, "eng-Latn")


class TestShardNaming(unittest.TestCase):
    """Regression: 431 of the corpus's `lang` values are not language codes.
    '1510/' turned a shard id into a path and killed an array task mid-run
    with FileNotFoundError; ' Acland St' and '20 Sukhumvit' are others."""

    def test_clean_codes_pass_through_unchanged(self):
        # Stability matters: a re-plan must not rename shards that already
        # computed, or --skip-existing silently recomputes the whole corpus.
        for code in ("en", "ca", "zh", "jpn-Hrgn", "LATIN", "CJK", "nan"):
            self.assertEqual(R.shard_token(code), code)

    def test_path_separators_cannot_survive(self):
        for bad in ("1510/", "a/b/c", "../etc", "x\\y"):
            tok = R.shard_token(bad)
            self.assertNotIn("/", tok, bad)
            self.assertNotIn("\\", tok, bad)

    def test_distinct_junk_tags_do_not_collide(self):
        # '1510/' and '1510:' both sanitise to '1510_' without the hash.
        self.assertNotEqual(R.shard_token("1510/"), R.shard_token("1510:"))
        self.assertNotEqual(R.shard_token(" Acland St"),
                            R.shard_token("_Acland_St"))

    def test_empty_lang_gets_a_name(self):
        self.assertEqual(R.shard_token(""), "NONE")

    def test_partition_component_matches_duckdb_encoding(self):
        """DuckDB percent-encodes PARTITION_BY directory names. Reading them
        back naively finds nothing, and finding nothing is indistinguishable
        from an empty cell -- which is why compute now raises instead."""
        self.assertEqual(R.partition_path_component("lang", "1510/"),
                         "lang=1510%2F")
        self.assertEqual(R.partition_path_component("lang", "1749:source"),
                         "lang=1749%3Asource")
        self.assertEqual(R.partition_path_component("lang", " 2"), "lang=%202")
        self.assertEqual(R.partition_path_component("lang", "en"), "lang=en")
        self.assertEqual(R.partition_path_component("lang", ""), "lang=")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ipa.duckdb")

    def tearDown(self):
        self.tmp.cleanup()

    def _shard(self, path, rows):
        import pyarrow as pa
        import pyarrow.parquet as pq
        cols = {k: [r.get(k) for r in rows] for k in
                ("toponym_id", "name_sha", "lang", "script", "ipa", "backend",
                 "mode", "status", "error", "run_id", "computed_at")}
        schema = pa.schema([
            ("toponym_id", pa.string()), ("name_sha", pa.string()),
            ("lang", pa.string()), ("script", pa.string()),
            ("ipa", pa.string()), ("backend", pa.string()),
            ("mode", pa.string()), ("status", pa.string()),
            ("error", pa.string()), ("run_id", pa.string()),
            ("computed_at", pa.timestamp("us", tz="UTC")),
        ])
        pq.write_table(pa.table(cols, schema=schema), path)

    def _row(self, tid, status="ok", ipa="x", sha="aaa", run="r1"):
        return {"toponym_id": tid, "name_sha": sha, "lang": "en",
                "script": "LATIN", "ipa": ipa, "backend": "epitran",
                "mode": "eng-Latn", "status": status, "error": None,
                "run_id": run, "computed_at": datetime.now(timezone.utc)}

    def test_coverage_always_carries_its_denominator(self):
        con = S.connect(self.db)
        p = Path(self.tmp.name) / "s1.parquet"
        self._shard(p, [self._row("a"), self._row("b", status="no_route", ipa=None),
                        self._row("c", status="no_lang", ipa=None)])
        S.merge_shards(con, [p], "r1")
        cov = S.coverage(con)
        self.assertEqual(cov["rows_in_store"], 3)
        self.assertEqual(cov["with_ipa"], 1)
        # The unroutable rows are RECORDED, not dropped -- that is what makes
        # the next run incremental and the coverage figure meaningful.
        self.assertEqual(cov["by_status"]["no_route"], 1)
        self.assertEqual(cov["by_status"]["no_lang"], 1)
        con.close()

    def test_upsert_replaces_rather_than_duplicates(self):
        con = S.connect(self.db)
        p1 = Path(self.tmp.name) / "s1.parquet"
        p2 = Path(self.tmp.name) / "s2.parquet"
        self._shard(p1, [self._row("a", ipa="old")])
        self._shard(p2, [self._row("a", ipa="new", run="r2")])
        S.merge_shards(con, [p1], "r1")
        S.merge_shards(con, [p2], "r2")
        rows = con.execute("SELECT toponym_id, ipa FROM ipa").fetchall()
        self.assertEqual(rows, [("a", "new")])
        con.close()

    def test_name_sha_changes_when_the_name_does(self):
        self.assertNotEqual(S.name_sha("Köln"), S.name_sha("Koln"))
        self.assertEqual(S.name_sha("Köln"), S.name_sha("Köln"))

    def test_python_and_sql_name_sha_agree(self):
        """plan.py computes name_sha in SQL; store.name_sha computes it in
        Python. Inside the pipeline the two never meet -- the planner compares
        a SQL-derived hash against a SQL-derived hash -- so a divergence would
        stay invisible until someone used the Python helper against a stored
        row, at which point EVERY row looks stale and the incremental design
        silently degrades into a full rerun. Pin them together."""
        import duckdb
        con = duckdb.connect()
        for n in ("Manchester", "Köln", "東京", "Навас", "O'Fallon",
                  "Ærøskøbing", "a b  c"):
            sql = con.execute("SELECT substr(sha256(?), 1, 16)", [n]).fetchone()[0]
            self.assertEqual(sql, S.name_sha(n), n)
        con.close()


class TestMergeRefusesIncompleteRun(unittest.TestCase):
    """The merge must FAIL on a missing shard. Without this the suite would
    pass against a merge that silently writes a partial store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self, shard_ids):
        plan = {"run_id": "r1", "work_dir": str(self.dir / "work"),
                "shards": [{"shard_id": s, "lang": "en", "script": "LATIN",
                            "backend": "epitran", "mode": "eng-Latn",
                            "status": "pending", "rows": 1, "terminal": False}
                           for s in shard_ids]}
        p = self.dir / "plan.json"
        p.write_text(json.dumps(plan))
        return p

    def test_missing_shard_raises(self):
        from phonetics.ipa import merge as M
        plan = self._plan(["s1", "s2"])
        sd = self.dir / "shards"
        sd.mkdir()
        # only s1 exists
        import pyarrow as pa, pyarrow.parquet as pq
        pq.write_table(pa.table({"toponym_id": ["a"], "name_sha": ["x"],
                                 "lang": ["en"], "script": ["LATIN"],
                                 "ipa": ["i"], "backend": ["epitran"],
                                 "mode": ["eng-Latn"], "status": ["ok"],
                                 "error": [None], "run_id": ["r1"],
                                 "computed_at": [datetime.now(timezone.utc)]}),
                       sd / "s1.parquet")
        with self.assertRaises(SystemExit):
            M.merge(plan, sd, str(self.dir / "store.duckdb"))

    def test_allow_partial_records_the_shortfall(self):
        from phonetics.ipa import merge as M
        plan = self._plan(["s1", "s2"])
        sd = self.dir / "shards"
        sd.mkdir()
        import pyarrow as pa, pyarrow.parquet as pq
        pq.write_table(pa.table({"toponym_id": ["a"], "name_sha": ["x"],
                                 "lang": ["en"], "script": ["LATIN"],
                                 "ipa": ["i"], "backend": ["epitran"],
                                 "mode": ["eng-Latn"], "status": ["ok"],
                                 "error": [None], "run_id": ["r1"],
                                 "computed_at": [datetime.now(timezone.utc)]}),
                       sd / "s1.parquet")
        out = M.merge(plan, sd, str(self.dir / "store.duckdb"),
                      allow_partial=True)
        self.assertEqual(out["missing_shards"], 1)
        self.assertEqual(out["merged_shards"], 1)


class TestInventoryStalenessGuard(unittest.TestCase):
    """A planner that returns ZERO WORK against a stale inventory is
    indistinguishable from one that is genuinely up to date -- Fault 12's
    shape. These prove the detector fires and, just as importantly, that it
    stays quiet when it should: a guard that always fires is as useless as one
    that never does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.inv = self.d / "inventory.db"
        self.inv.write_text("x")
        self.staged = self.d / "staged"
        (self.staged / "tgn" / "extract").mkdir(parents=True)
        (self.staged / "gn" / "extract").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _artefact(self, ns, offset_seconds):
        import os
        f = self.staged / ns / "extract" / "places.jsonl"
        f.write_text("{}")
        t = self.inv.stat().st_mtime + offset_seconds
        os.utime(f, (t, t))
        return f

    def test_fires_when_staged_extract_is_newer(self):
        from phonetics.ipa.plan import stale_inventory_namespaces
        self._artefact("tgn", +7200)          # two hours after the inventory
        stale = stale_inventory_namespaces(str(self.inv), str(self.staged))
        self.assertEqual([d["namespace"] for d in stale], ["tgn"])
        self.assertAlmostEqual(stale[0]["newer_by_hours"], 2.0, places=1)

    def test_silent_when_inventory_is_current(self):
        # The negative control. Without this the test above passes against a
        # detector that flags everything.
        from phonetics.ipa.plan import stale_inventory_namespaces
        self._artefact("tgn", -7200)          # two hours BEFORE the inventory
        self._artefact("gn", -60)
        self.assertEqual(stale_inventory_namespaces(str(self.inv),
                                                    str(self.staged)), [])

    def test_reports_every_stale_namespace_not_just_the_first(self):
        from phonetics.ipa.plan import stale_inventory_namespaces
        self._artefact("tgn", +3600)
        self._artefact("gn", +1800)
        stale = stale_inventory_namespaces(str(self.inv), str(self.staged))
        self.assertEqual({d["namespace"] for d in stale}, {"gn", "tgn"})

    def test_missing_inventory_or_staged_root_does_not_crash(self):
        from phonetics.ipa.plan import stale_inventory_namespaces
        self.assertEqual(
            stale_inventory_namespaces(str(self.d / "nope.db"), str(self.staged)), [])
        self.assertEqual(
            stale_inventory_namespaces(str(self.inv), str(self.d / "nostaged")), [])


if __name__ == "__main__":
    unittest.main()
