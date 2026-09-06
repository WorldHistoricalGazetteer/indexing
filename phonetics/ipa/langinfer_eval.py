#!/usr/bin/env python3
"""
Measure the error rate of inferring `lang` from a place's country BEFORE
letting 18.5M toponyms depend on it.

THE ARGUMENT FOR MEASURING RATHER THAN CHOOSING
-----------------------------------------------
25.51% of the corpus (18,543,146 toponyms) carries no language tag, so no G2P
route can be selected and they contribute nothing. The tempting fix -- default
Latin script to English -- would inject 16.2M confidently-wrong IPA strings,
which is the Cebuano label problem an order of magnitude larger.

But "infer it from the country" is not obviously better; it is just less
obviously worse. The way to tell them apart is to measure, and the instrument
is already in the corpus: the toponyms that DO carry a lang. Hide it, infer it
back from the attested place's ccodes, and compare. That yields a real error
rate on real data, per country and per script, and it is a check that can fail.

Two numbers matter and they are different:
  APPLICABILITY  what share of no-lang toponyms could be given ANY lang --
                 i.e. resolve to at least one place with a ccode. If this is
                 small the option is moot however accurate it is.
  ACCURACY       of those, how often the inferred lang equals the true one,
                 measured on rows where the truth is known.

Inference uses CLDR likely-subtags (langcodes) rather than a hand-built
country-to-language table, for the same reason routes are derived from
installed modes rather than listed: a hand table encodes today's guesses and
silently rots.

⚠ This reports; it decides nothing and writes no lang anywhere. Any inferred
value that is later stored must carry its own provenance column so a reader can
weight, exclude or re-examine it -- it must never be indistinguishable from an
attested lang.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Optional

from phonetics.ipa.routes import SCRIPT_TAG, normalise_lang

STAGED_GLOB = "/vast/ishi/staged/*/final/places.parquet"


def build_infer(langcodes_mod):
    cache: Dict[tuple, Optional[str]] = {}

    def infer(script: str, ccode: str) -> Optional[str]:
        key = (script, ccode)
        if key in cache:
            return cache[key]
        tag = SCRIPT_TAG.get(script)
        out = None
        if tag and ccode and len(ccode) == 2:
            try:
                out = langcodes_mod.Language.get(
                    f"und-{tag}-{ccode.upper()}").maximize().language
            except Exception:
                out = None
        cache[key] = out
        return out
    return infer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory-db", required=True)
    ap.add_argument("--staged-glob", default=STAGED_GLOB)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", type=int, default=2_000_000,
                    help="labelled rows to evaluate accuracy on")
    a = ap.parse_args()

    import duckdb
    import langcodes

    infer = build_infer(langcodes)
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='90GB'")
    con.execute(f"ATTACH '{a.inventory_db}' AS inv (READ_ONLY)")

    # place_id -> single ccode, only where the place is UNAMBIGUOUS about its
    # country. A place spanning two countries cannot vote for one language.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE place_cc AS
        SELECT place_id, ccodes[1] AS ccode
        FROM read_parquet('{a.staged_glob}', union_by_name=true)
        WHERE ccodes IS NOT NULL AND len(ccodes) = 1
    """)
    n_places = con.execute("SELECT count(*) FROM place_cc").fetchone()[0]

    rep: Dict[str, object] = {"places_with_single_ccode": n_places}

    # --- APPLICABILITY: could a no-lang toponym be given anything at all? ---
    row = con.execute("""
        SELECT count(*) AS no_lang_total,
               count(*) FILTER (WHERE cc IS NOT NULL) AS resolvable
        FROM (
            SELECT t.toponym_id, max(p.ccode) AS cc
            FROM inv.toponyms t
            LEFT JOIN inv.toponym_attestations a USING (toponym_id)
            LEFT JOIN place_cc p ON p.place_id = a.place_id
            WHERE t.lang IS NULL OR trim(t.lang) = ''
            GROUP BY t.toponym_id
        )
    """).fetchone()
    rep["applicability"] = {
        "no_lang_total": row[0], "resolvable_to_a_ccode": row[1],
        "pct": round(100.0 * row[1] / row[0], 3) if row[0] else None,
    }
    print(f"APPLICABILITY: {row[1]:,} of {row[0]:,} no-lang toponyms resolve "
          f"to a single-country place ({rep['applicability']['pct']}%)")

    # --- ACCURACY: on rows where the truth is known ---
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE labelled AS
        SELECT t.lang AS true_lang, t.script, max(p.ccode) AS ccode
        FROM inv.toponyms t
        JOIN inv.toponym_attestations a USING (toponym_id)
        JOIN place_cc p ON p.place_id = a.place_id
        WHERE t.lang IS NOT NULL AND trim(t.lang) <> ''
        GROUP BY t.toponym_id, t.lang, t.script
        LIMIT {a.sample}
    """)
    rows = con.execute(
        "SELECT true_lang, script, ccode, count(*) FROM labelled "
        "GROUP BY 1,2,3").fetchall()

    tot = correct = 0
    per_cc = defaultdict(lambda: [0, 0])       # ccode -> [n, correct]
    per_script = defaultdict(lambda: [0, 0])
    confusion = Counter()
    for true_lang, script, ccode, n in rows:
        got = infer(script, ccode)
        if got is None:
            continue
        ok = (got == normalise_lang(true_lang))
        tot += n
        correct += n if ok else 0
        per_cc[ccode][0] += n; per_cc[ccode][1] += n if ok else 0
        per_script[script][0] += n; per_script[script][1] += n if ok else 0
        if not ok:
            confusion[(ccode, normalise_lang(true_lang), got)] += n

    rep["accuracy"] = {
        "evaluated": tot, "correct": correct,
        "accuracy_pct": round(100.0 * correct / tot, 3) if tot else None,
        "error_pct": round(100.0 * (tot - correct) / tot, 3) if tot else None,
    }
    print(f"ACCURACY: {correct:,} of {tot:,} = "
          f"{rep['accuracy']['accuracy_pct']}%")

    rep["by_country"] = sorted(
        [{"ccode": c, "n": v[0], "correct": v[1],
          "accuracy_pct": round(100.0 * v[1] / v[0], 2)}
         for c, v in per_cc.items() if v[0] >= 500],
        key=lambda d: -d["n"])
    rep["by_script"] = sorted(
        [{"script": s, "n": v[0], "correct": v[1],
          "accuracy_pct": round(100.0 * v[1] / v[0], 2)}
         for s, v in per_script.items()], key=lambda d: -d["n"])
    rep["top_confusions"] = [
        {"ccode": c, "true": t, "inferred": g, "n": n}
        for (c, t, g), n in confusion.most_common(40)]

    # Countries where inference is safe enough to be worth doing at all.
    for thresh in (0.90, 0.95, 0.99):
        good = [d for d in rep["by_country"] if d["accuracy_pct"] >= thresh * 100]
        rep[f"countries_at_{int(thresh*100)}pct"] = {
            "n_countries": len(good),
            "rows_covered": sum(d["n"] for d in good),
            "share_of_evaluated_pct": round(
                100.0 * sum(d["n"] for d in good) / tot, 2) if tot else None,
        }

    Path(a.out).write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    print("\n-- by script --")
    for d in rep["by_script"][:10]:
        print(f"   {d['script']:<12} n={d['n']:>10,}  acc={d['accuracy_pct']}%")
    print("\n-- largest countries --")
    for d in rep["by_country"][:15]:
        print(f"   {d['ccode']:<4} n={d['n']:>10,}  acc={d['accuracy_pct']}%")
    print("\n-- worst confusions --")
    for d in rep["top_confusions"][:12]:
        print(f"   {d['ccode']:<4} true={d['true']:<6} inferred={d['inferred']:<6} "
              f"n={d['n']:>9,}")
    print("\n-- how much is safe --")
    for t in (90, 95, 99):
        k = rep[f"countries_at_{t}pct"]
        print(f"   >={t}% accurate: {k['n_countries']:>3} countries, "
              f"{k['rows_covered']:>10,} rows ({k['share_of_evaluated_pct']}% of evaluated)")
    print("->", a.out)


if __name__ == "__main__":
    main()
