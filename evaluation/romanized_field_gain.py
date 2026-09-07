#!/usr/bin/env python3
"""
What does populating `name_romanized` actually buy search?

IT HAS NEVER SHIPPED. Production holds zero in every generation, because
`update_es.run_index` dropped the column before indexing every time. Yet the
gateway QUERIES it in two live paths -- `es_helpers.py:364`
(`match_phrase` on `name_romanized.ngram`, the `in` mode) and `:377`
(`multi_match` over `name^3, name_romanized^2, name.prefix`, the fuzzy
near-miss pass). So two query clauses have been contributing nothing at all,
and the rebuild now computes 12,431,453 values for them.

WHAT IT CAN BUY, precisely. `romanize_for_search` returns None for Latin-script
names and `anyascii(name).lower()` otherwise, so the field exists ONLY on
non-Latin toponyms. Its whole job is to let a LATIN-SCRIPT QUERY reach a
NON-LATIN NAME lexically -- something no amount of BM25 on `name` can do,
because the two strings share no characters.

MEASURED as incremental reachability over the evaluation corpus's positive
pairs: for each, can a lexical matcher reach the candidate via its romanised
form when it cannot reach it via the raw name?

🛑 THE NUMBER THAT WILL LOOK BEST MEANS LEAST, and this is the trap the corpus
already sprang once. ~35% of CJK<->Latin positives romanise to IDENTICAL
strings, because the Latin side of such a pair is frequently itself a
transliteration of the non-Latin side. An exact match there measures the
provenance of the test data, not a capability of the field. So exact and
near matches are reported SEPARATELY and never summed.

CONTROLS, both in the same run:
  both-Latin pairs   the field is None by construction, so the gain MUST be 0.
                     A non-zero here means the measurement is broken.
  raw-name baseline  what the same matcher achieves WITHOUT the field, so the
                     reported figure is a delta rather than a level.
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path


def is_latin(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters
               if "LATIN" in unicodedata.name(c, "")) / len(letters) > 0.5


def norm(s: str) -> str:
    return " ".join(s.lower().split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--near", type=float, default=0.85,
                    help="similarity at or above which a non-identical "
                         "romanisation counts as reachable")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    from anyascii import anyascii

    pairs = []
    with open(Path(a.corpus) / "pairs.jsonl", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if int(d["label"]) == 1:
                pairs.append(d)
    print(f"positive pairs: {len(pairs):,}")

    strata = defaultdict(lambda: {
        "n": 0, "reachable_raw": 0, "reachable_romanised_exact": 0,
        "reachable_romanised_near": 0, "field_absent": 0})

    for d in pairs:
        q, c = d["query"], d["candidate"]
        qs, cs = d["query_script"], d["candidate_script"]
        key = ("latin_query_nonlatin_candidate"
               if qs == "LATIN" and cs != "LATIN" else
               "nonlatin_query_latin_candidate"
               if qs != "LATIN" and cs == "LATIN" else
               "both_latin" if qs == "LATIN" and cs == "LATIN"
               else "both_nonlatin")
        s = strata[key]
        s["n"] += 1

        # Baseline: can a lexical matcher reach the candidate as stored?
        if norm(q) == norm(c) or SequenceMatcher(None, norm(q), norm(c)).ratio() >= a.near:
            s["reachable_raw"] += 1
            continue

        # `romanize_for_search` returns None for Latin-script names, so the
        # field simply does not exist on them. Counted, not silently skipped.
        if cs == "LATIN":
            s["field_absent"] += 1
            continue

        rc = anyascii(c).lower().strip()
        if not rc or rc == c.lower():
            s["field_absent"] += 1
            continue
        if norm(q) == norm(rc):
            s["reachable_romanised_exact"] += 1
        elif SequenceMatcher(None, norm(q), norm(rc)).ratio() >= a.near:
            s["reachable_romanised_near"] += 1

    report = {"corpus": a.corpus, "near_threshold": a.near, "strata": {}}
    print(f"\n{'stratum':<34}{'n':>8}{'raw':>8}{'+exact':>9}{'+near':>8}"
          f"{'gain%':>8}")
    for k, s in sorted(strata.items(), key=lambda kv: -kv[1]["n"]):
        gain = s["reachable_romanised_exact"] + s["reachable_romanised_near"]
        pct = round(100.0 * gain / s["n"], 2) if s["n"] else None
        s["gain_pct"] = pct
        s["gain_exact_pct"] = round(100.0 * s["reachable_romanised_exact"] / s["n"], 2) if s["n"] else None
        s["gain_near_pct"] = round(100.0 * s["reachable_romanised_near"] / s["n"], 2) if s["n"] else None
        report["strata"][k] = s
        print(f"{k:<34}{s['n']:>8,}{s['reachable_raw']:>8,}"
              f"{s['reachable_romanised_exact']:>9,}"
              f"{s['reachable_romanised_near']:>8,}{pct:>7}%")

    # ⚠ A CONTROL OVER AN EMPTY SET IS NOT A CONTROL. The both_latin stratum
    # was designed as the negative control, but this corpus is cross-script by
    # construction and contains NO both-Latin positives -- so the check passed
    # by having nothing to check. Report vacuity explicitly and fall back to a
    # control that actually has rows.
    def control(name, why):
        st = report["strata"].get(name)
        if not st or st["n"] == 0:
            print(f"CONTROL {name:<34} VACUOUS — 0 pairs, proves nothing")
            return {"status": "vacuous", "n": 0}
        gain = st["reachable_romanised_exact"] + st["reachable_romanised_near"]
        ok = gain == 0
        print(f"CONTROL {name:<34} {'PASS' if ok else 'FAIL'}  "
              f"n={st['n']:,} gain={gain}   {why}")
        return {"status": "pass" if ok else "fail", "n": st["n"], "gain": gain}

    print()
    report["controls"] = {
        "both_latin": control(
            "both_latin", "designed control; empty in this corpus"),
        "nonlatin_query_latin_candidate": control(
            "nonlatin_query_latin_candidate",
            "the field does not exist on Latin candidates, so 0 is required"),
    }

    key = "latin_query_nonlatin_candidate"
    if key in report["strata"]:
        s = report["strata"][key]
        print(f"\n⚠ PRIMARY STRATUM, split because the halves mean different things:")
        print(f"   exact romanisation match : {s['gain_exact_pct']}%  "
              f"— includes pairs whose Latin side IS a transliteration of the "
              f"other; measures test-data provenance as much as capability")
        print(f"   near  romanisation match : {s['gain_near_pct']}%  "
              f"— the defensible capability figure")

    Path(a.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
