#!/usr/bin/env python3
"""
Is the 32.4% real, or an artefact of measuring on the wrong population?

The first pass evaluated country-based lang inference on every toponym that
carries a lang, and got 32.4% with no country above 90%. But look at what it
got wrong -- the dominant error is `true=en, inferred=<local language>`:

    IN  true=en inferred=hi  21,256      DE  true=en inferred=de  15,360
    ID  true=en inferred=id  20,618      JP  true=en inferred=ja  14,487
    CN  true=en inferred=za  16,282      FR  true=en inferred=fr  11,111

Those are English EXONYMS -- GeoNames and Wikidata English labels for places
worldwide. Inferring "German" for a German place is not wrong about the place;
it is wrong about a label that was never local to begin with.

⚠ And the labelled rows are NOT a random sample of the unlabelled ones. The
no-lang population is 71.35% osm, and OSM's `name` tag is the LOCAL name -- an
endonym. So the first measurement may understate accuracy on the population
that actually needs it: a corpus property read as a method property, the
pattern already logged three times in this campaign.

This measures the same inference on strata that differ in that respect:
per namespace, and with English-labelled rows separated out. If osm's labelled
rows behave like gn's, the negative result stands and the selection worry was
unfounded -- which is equally worth knowing.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from phonetics.ipa.routes import SCRIPT_TAG, normalise_lang

STAGED_GLOB = "/vast/ishi/staged/*/final/places.parquet"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory-db", required=True)
    ap.add_argument("--staged-glob", default=STAGED_GLOB)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample-pct", type=float, default=2.0,
                    help="percent of labelled toponyms to evaluate. Sampling "
                         "happens BEFORE the joins -- a LIMIT after a GROUP BY "
                         "does not reduce the work, which is how the first "
                         "version spilled 198.5 GiB.")
    ap.add_argument("--temp-dir", default="/vast/ishi/ipa-v8/duckdb-tmp")
    ap.add_argument("--max-temp", default="20GB")
    a = ap.parse_args()

    import duckdb
    import langcodes

    cache = {}

    def infer(script, ccode):
        k = (script, ccode)
        if k in cache:
            return cache[k]
        tag = SCRIPT_TAG.get(script)
        out = None
        if tag and ccode and len(ccode) == 2:
            try:
                out = langcodes.Language.get(
                    f"und-{tag}-{ccode.upper()}").maximize().language
            except Exception:
                out = None
        cache[k] = out
        return out

    con = duckdb.connect()
    # HARD CEILING ON SPILL. /vast/ishi is a 1 TB allocation SHARED WITH
    # PRODUCTION ES, which goes read-only at ~51 GB free. An earlier version of
    # this query spilled 198.5 GiB before dying, taking free space from 222 GB
    # to 86 GB -- a secondary measurement about 35 GB from causing a production
    # outage. DuckDB's default max_temp_directory_size is "whatever the disk
    # has", which on a shared volume means "whatever production needs".
    con.execute("PRAGMA memory_limit='60GB'")
    con.execute(f"PRAGMA temp_directory='{a.temp_dir}'")
    con.execute(f"PRAGMA max_temp_directory_size='{a.max_temp}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"ATTACH '{a.inventory_db}' AS inv (READ_ONLY)")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE place_cc AS
        SELECT place_id, ccodes[1] AS ccode
        FROM read_parquet('{a.staged_glob}', union_by_name=true)
        WHERE ccodes IS NOT NULL AND len(ccodes) = 1
    """)

    rep = {}

    # The no-lang population's namespace mix -- the weights that decide which
    # stratum's accuracy actually matters.
    rows = con.execute("""
        SELECT n.namespace, count(DISTINCT t.toponym_id) c
        FROM inv.toponyms t JOIN inv.toponym_namespaces n USING (toponym_id)
        WHERE t.lang IS NULL OR trim(t.lang) = ''
        GROUP BY 1 ORDER BY c DESC
    """).fetchall()
    rep["no_lang_by_namespace"] = [{"namespace": n, "n": c} for n, c in rows]
    print("no-lang population by namespace:")
    for n, c in rows[:8]:
        print(f"   {n:<8} {c:>12,}")

    # Labelled rows, carrying their namespace so accuracy can be stratified.
    # Sample the toponyms FIRST. Deterministic (hash of the id), so a rerun
    # evaluates the same rows and the figure is reproducible.
    bucket = max(1, int(round(10000 / a.sample_pct)))
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE seed AS
        SELECT toponym_id, lang AS true_lang, script
        FROM inv.toponyms
        WHERE lang IS NOT NULL AND trim(lang) <> ''
          AND (hash(toponym_id) % {bucket}) = 0
    """)
    n_seed = con.execute("SELECT count(*) FROM seed").fetchone()[0]
    rep["sample"] = {"pct_requested": a.sample_pct, "rows": n_seed,
                     "bucket_modulus": bucket}
    print(f"sampled {n_seed:,} labelled toponyms (1 in {bucket})")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE labelled AS
        SELECT s.toponym_id, s.true_lang, s.script,
               max(p.ccode) AS ccode, min(n.namespace) AS ns
        FROM seed s
        JOIN inv.toponym_attestations ta ON ta.toponym_id = s.toponym_id
        JOIN place_cc p ON p.place_id = ta.place_id
        JOIN inv.toponym_namespaces n ON n.toponym_id = s.toponym_id
        GROUP BY s.toponym_id, s.true_lang, s.script
    """)
    rows = con.execute(
        "SELECT ns, true_lang, script, ccode, count(*) FROM labelled "
        "GROUP BY 1,2,3,4").fetchall()

    strata = defaultdict(lambda: [0, 0])
    for ns, true_lang, script, ccode, n in rows:
        got = infer(script, ccode)
        if got is None:
            continue
        tl = normalise_lang(true_lang)
        ok = (got == tl)
        for key in (("ns", ns), ("ns_non_en", ns) if tl != "en" else None,
                    ("all", "all"),
                    ("all_non_en", "all") if tl != "en" else None,
                    ("script", script)):
            if key is None:
                continue
            strata[key][0] += n
            strata[key][1] += n if ok else 0

    def pack(prefix):
        return sorted(
            [{"key": k[1], "n": v[0], "correct": v[1],
              "accuracy_pct": round(100.0 * v[1] / v[0], 2)}
             for k, v in strata.items() if k[0] == prefix and v[0] >= 1000],
            key=lambda d: -d["n"])

    rep["overall"] = pack("all")
    rep["overall_excluding_english_labels"] = pack("all_non_en")
    rep["by_namespace"] = pack("ns")
    rep["by_namespace_excluding_english_labels"] = pack("ns_non_en")
    rep["by_script"] = pack("script")

    print("\n-- overall --")
    for d in rep["overall"]:
        print(f"   all rows           n={d['n']:>10,}  acc={d['accuracy_pct']}%")
    for d in rep["overall_excluding_english_labels"]:
        print(f"   excluding true=en  n={d['n']:>10,}  acc={d['accuracy_pct']}%")
    print("\n-- by namespace (all labelled rows) --")
    for d in rep["by_namespace"][:10]:
        print(f"   {d['key']:<8} n={d['n']:>10,}  acc={d['accuracy_pct']}%")
    print("\n-- by namespace, excluding true=en --")
    for d in rep["by_namespace_excluding_english_labels"][:10]:
        print(f"   {d['key']:<8} n={d['n']:>10,}  acc={d['accuracy_pct']}%")
    print("\n-- by script --")
    for d in rep["by_script"][:14]:
        print(f"   {d['key']:<12} n={d['n']:>10,}  acc={d['accuracy_pct']}%")

    Path(a.out).write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    print("->", a.out)


if __name__ == "__main__":
    main()
