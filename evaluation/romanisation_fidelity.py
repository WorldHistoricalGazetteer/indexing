#!/usr/bin/env python3
"""
Does English phonology over a romanised form land near the true pronunciation?

THE DECISION THIS REPLACES
--------------------------
722,044 recovered romanisations (fa/el/ru/ko/ar/ja/zh + LATIN) have no
same-language Latin Epitran mode, so the only way to give them IPA is to impose
a different language's Latin phonology -- in practice `eng-Latn`. That is a
change to corpus SEMANTICS, and the IPA is not for search (Symphonym embeds the
name string) but for TRAINING-PAIR selection and the PanPhon space. So a wrong
choice is a systematically wrong LABEL inside the corpus the next model is
measured with -- the hardest defect class here to detect.

Rather than argue it, measure it. Getty publishes native and romanised forms
for the same place, which is a ground truth: compute the native form through
its CORRECT mode, the romanised form through `eng-Latn`, and compare.

🛑 THE MEASUREMENT IS USELESS WITHOUT ITS BOUNDS, which is why this reports
three numbers and not one. A treatment distance of 0.4 means nothing alone.

    CEILING  two NATIVE-script forms of the same place, both through the
             correct mode. This is what "as close as two real variants of one
             name get" looks like, and it is the best the treatment could hope
             for.
    FLOOR    native form of place A vs romanised form of a RANDOM OTHER place,
             through eng-Latn. This is what "no relationship" looks like. If
             the treatment sits here, English phonology over a romanisation
             carries no signal about the actual pronunciation.
    TREATMENT native vs romanised, SAME place.

⚠ Reported PER LANGUAGE and never pooled: the languages behave differently and
a pooled mean would hide exactly the split the recommendation turns on. The
distribution SHAPE is reported too -- a tight spread means one dominant
romanisation scheme, a bimodal one means competing schemes, and that is itself
the answer for that language.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "/vast/ishi/ipa-v8/code")
sys.path.insert(0, "/vast/ishi/elastic")

# Inventory DBs move between /vast and /ix1 as capacity is managed -- the
# July one was relocated to /ix1 on 7 Sep and the September one is being
# compacted there. A hard-coded path presents that as "file not found", which
# reads like a corrupted artefact rather than a relocation, so resolve by NAME
# across the known roots instead.
#
# ⚠ Deliberately does NOT match `*.compact.db`: a compaction in flight is a
# file being written, and reading one mid-write is the same mid-flight hazard
# as reading an inventory between its two checkpoints.
DB_NAME = "toponyms-undscript-20260906T160000Z.db"
DB_ROOTS = ("/vast/ishi/data", "/ix1/ishi/data")


def resolve_db(name: str = DB_NAME) -> str:
    from pathlib import Path as _P
    tried = []
    for root in DB_ROOTS:
        c = _P(root) / name
        tried.append(str(c))
        if c.exists():
            return str(c)
    raise SystemExit("inventory DB not found under any known root; tried:\n  "
                     + "\n  ".join(tried))

# language -> its native script in this corpus
NATIVE = {"fa": "ARABIC", "el": "GREEK", "ru": "CYRILLIC", "ar": "ARABIC",
          "ko": "HANGUL", "ja": "KATAKANA", "zh": "CJK", "kk": "CYRILLIC"}


def cos_dist(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    A = A / np.maximum(np.linalg.norm(A, axis=1, keepdims=True), 1e-9)
    B = B / np.maximum(np.linalg.norm(B, axis=1, keepdims=True), 1e-9)
    return 1.0 - np.einsum("ij,ij->i", A, B)


def describe(d: np.ndarray) -> dict:
    if len(d) == 0:
        return {"n": 0}
    return {"n": int(len(d)), "mean": round(float(d.mean()), 4),
            "median": round(float(np.median(d)), 4),
            "p10": round(float(np.percentile(d, 10)), 4),
            "p90": round(float(np.percentile(d, 90)), 4),
            "sd": round(float(d.std()), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-lang", type=int, default=3000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--db", default=None,
                    help="inventory DB; resolved across known roots if omitted")
    ap.add_argument("--temp-dir", default="/ix1/ishi/tmp/ipa-duckdb",
                    help="DuckDB spill directory. Defaults to /ix1 because "
                         "/vast is shared with production Elasticsearch.")
    a = ap.parse_args()

    import duckdb
    from phonetics.extraction.rebuild_toponyms_index import IPAConverter
    from phonetics.utils.script_detection import Script
    from phonetics.ipa import routes as R

    conv = IPAConverter()
    conv._check_panphon()
    table = R.RouteTable()
    rng = np.random.default_rng(0)

    con = duckdb.connect()
    # ⚠ HARD SPILL CEILING. /vast/ishi is a 1 TB allocation shared with
    # production ES, which goes read-only at ~51 GB free. An earlier version of
    # this query spilled until it died, for the same reason as the langinfer
    # one: the LIMIT sat AFTER a four-way join, so it bounded the OUTPUT and
    # not the WORK.
    con.execute("PRAGMA memory_limit='32GB'")
    # Spill to /ix1, not /vast: /vast is shared with production ES and is
    # still recovering from a 148 GB DuckDB bloat incident. /ix1 has 1.8 TB.
    con.execute(f"PRAGMA temp_directory='{a.temp_dir}'")
    con.execute("PRAGMA max_temp_directory_size='16GB'")
    con.execute("SET preserve_insertion_order=false")
    db_path = a.db or resolve_db()
    print(f"inventory: {db_path}")
    con.execute(f"ATTACH '{db_path}' AS d (READ_ONLY)")

    def ipa_via(name, mode_lang, script_enum):
        try:
            return conv.to_ipa(name, mode_lang, script_enum)
        except Exception:
            return None

    def vec(ipa):
        if not ipa:
            return None
        try:
            return conv.to_embedding(ipa)
        except Exception:
            return None

    report = {"db": db_path, "languages": []}
    for lang, native_script in NATIVE.items():
        # Places attested by BOTH a native-script and a Latin form of this lang.
        # ⚠ THIRD ITERATION OF THIS QUERY, and the first two both died of
        # spill. The trap is `al.place_id = pl.place_id`: toponym_attestations
        # has no index on place_id, so joining BACK through it hashes ~200M
        # rows however small the seed is. Bounding the seed bounds the seed,
        # not the join.
        #
        # Both sides are bounded by LANGUAGE and are small -- fa+LATIN is ~97k
        # toponyms, fa+ARABIC likewise -- so materialise each as its own tiny
        # relation and join those on place_id. Nothing ever scans the full
        # attestations table as a probe side.
        con.execute("DROP TABLE IF EXISTS nat; DROP TABLE IF EXISTS lat")
        con.execute(f"""
            CREATE TEMP TABLE nat AS
            SELECT t.name AS native, ta.place_id
            FROM (SELECT toponym_id, name FROM d.toponyms
                  WHERE lang = ? AND script = ? AND name IS NOT NULL
                  LIMIT {a.per_lang}) t
            JOIN d.toponym_attestations ta ON ta.toponym_id = t.toponym_id
        """, [lang, native_script])
        con.execute("""
            CREATE TEMP TABLE lat AS
            SELECT t.name AS roman, ta.place_id
            FROM d.toponyms t
            JOIN d.toponym_attestations ta ON ta.toponym_id = t.toponym_id
            WHERE t.lang = ? AND t.script = 'LATIN' AND t.name IS NOT NULL
        """, [lang])
        rows = con.execute(f"""
            SELECT nat.native, lat.roman
            FROM nat JOIN lat USING (place_id)
            LIMIT {a.per_lang}
        """).fetchall()
        if len(rows) < 50:
            report["languages"].append(
                {"lang": lang, "pairs": len(rows),
                 "verdict": f"INSUFFICIENT: {len(rows)} native/roman pairs"})
            print(f"{lang}: only {len(rows)} pairs — skipping")
            continue

        sc = Script[native_script]
        nat, rom = [], []
        for nname, rname in rows:
            vn = vec(ipa_via(nname, lang, sc))
            vr = vec(ipa_via(rname, "en", Script.LATIN))
            if vn is not None and vr is not None:
                nat.append(vn); rom.append(vr)
        if len(nat) < 50:
            report["languages"].append(
                {"lang": lang, "pairs": len(rows), "scored": len(nat),
                 "verdict": f"INSUFFICIENT after IPA: {len(nat)}"})
            print(f"{lang}: only {len(nat)} scored — skipping")
            continue
        N, Rm = np.asarray(nat, dtype=np.float32), np.asarray(rom, dtype=np.float32)

        treatment = cos_dist(N, Rm)
        # FLOOR: same native forms against SHUFFLED romanisations.
        perm = rng.permutation(len(Rm))
        while np.any(perm == np.arange(len(Rm))):      # no accidental self-pairs
            perm = rng.permutation(len(Rm))
        floor = cos_dist(N, Rm[perm])
        # CEILING: two native forms of the same place, both correct mode.
        con.execute("DROP TABLE IF EXISTS nat2")
        con.execute(f"""
            CREATE TEMP TABLE nat2 AS
            SELECT t.toponym_id, t.name, ta.place_id
            FROM (SELECT toponym_id, name FROM d.toponyms
                  WHERE lang = ? AND script = ? AND name IS NOT NULL
                  LIMIT {a.per_lang}) t
            JOIN d.toponym_attestations ta ON ta.toponym_id = t.toponym_id
        """, [lang, native_script])
        crows = con.execute(f"""
            SELECT x.name AS a_name, y.name AS b_name
            FROM nat2 x JOIN nat2 y USING (place_id)
            WHERE x.toponym_id <> y.toponym_id
            LIMIT {a.per_lang}
        """).fetchall()
        c1, c2 = [], []
        for x, y in crows:
            vx, vy = vec(ipa_via(x, lang, sc)), vec(ipa_via(y, lang, sc))
            if vx is not None and vy is not None:
                c1.append(vx); c2.append(vy)
        ceiling = (cos_dist(np.asarray(c1, dtype=np.float32),
                            np.asarray(c2, dtype=np.float32))
                   if len(c1) >= 50 else np.array([]))

        d = {"lang": lang, "native_script": native_script,
             "treatment_native_vs_romanised": describe(treatment),
             "floor_native_vs_random_romanised": describe(floor),
             "ceiling_two_native_variants": describe(ceiling)}
        t, f = treatment.mean(), floor.mean()
        # How far of the way from "no relationship" to "identical" does the
        # substitution get? <=0 means it carries no signal at all.
        d["signal_fraction_vs_floor"] = round(float((f - t) / f), 4) if f > 0 else None
        report["languages"].append(d)
        cm = d["ceiling_two_native_variants"].get("mean")
        print(f"{lang:<4} n={len(treatment):>5}  treat={t:.4f}  floor={f:.4f}  "
              f"ceil={cm}  signal={d['signal_fraction_vs_floor']}")

    Path(a.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print("\n->", a.out)


if __name__ == "__main__":
    main()
