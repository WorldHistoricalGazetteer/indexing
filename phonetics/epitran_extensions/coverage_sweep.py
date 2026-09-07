#!/usr/bin/env python3
"""Coverage sweep for Epitran Orth,Phon rule sets — the check the value lint cannot make.

The lint judges the rows that exist. It is silent about letters of the script that have no rule
at all, so a rule set can be 0 defects and 30% complete (cop-Copt, 2026-09-07).

Coverage is measured against the Unicode SCRIPT property, not against a block. That distinction
is the whole point: Unicode disunified the Greek-derived Coptic letters into U+2C80+ but left the
seven Demotic-derived ones (shei, fei, khei, hori, gangia, shima, dei) behind at U+03E2-U+03EF.
Script=Copt finds them; a walk of the Coptic block does not, and looks exhaustive while missing
exactly the letters that matter. Any script Unicode splits across blocks fails the same way.

Hence the report is by block PROVENANCE as well as percentage: gaps scattered inside the script's
main block are a reviewer's problem; a whole secondary block at zero is a mechanical error with a
mechanical fix, and the percentage alone cannot tell them apart.

Deliberately proposes no values. Which IPA a missing letter takes is a reviewer's call.

Reads only the CSVs given to it. No database, no /vast, no temp spill.

Usage:  python3 epitran_coverage.py DIR [DIR ...] [--json out.json]
Needs:  regex (Script property), fonttools (block names)
"""
import argparse, csv, json, sys, unicodedata as ud
from collections import defaultdict

import regex
from fontTools import unicodedata as fud

# Letters and the marks that spell with them. Digits, punctuation and modifier symbols are not
# part of "can this script be transcribed"; Mc is (Devanagari/Sinhala/Myanmar vowel signs are Mc).
LETTER_CATS = {"Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Mc"}
# Above this many characters a whole-script percentage stops being a completion measure: nobody
# intends to cover all of Latin. Reported, but never called incomplete on the strength of it.
OPEN_SCRIPT = 300
MAXCP = 0x110000

MIN_RUN = 4  # shorter than this is a scattered gap, not a category


def _shared_descriptor(chars):
    """The words the names of these characters agree on, e.g. 'SUBJOINED LETTER'.

    A whole functional category left out reads as one phrase repeated down the run; scattered
    omissions do not agree on anything. This is what separates "the independent vowels are all
    missing" from "four rare letters happen to be adjacent".
    """
    names = [ud.name(c, "").split() for c in chars]
    if not all(names):
        return ""
    common = []
    for i in range(min(len(n) for n in names)):
        w = names[0][i]
        if all(n[i] == w for n in names):
            common.append(w)
        else:
            break
    # drop the leading script name, which every character in a script shares by construction
    while common and len(common) > 1 and common[0] not in ("LETTER", "VOWEL", "SIGN", "SUBJOINED"):
        common.pop(0)
    return " ".join(common)


def run_gaps(missing):
    """Contiguous uncovered runs, with the descriptor their names agree on.

    Catches the same error as the block signal when it is made INSIDE one block, where a block
    comparison cannot see it: Georgian's 38 Asomtavruli capitals, Khmer's 19 independent vowels,
    Tibetan's 45 subjoined consonants are each one uninterrupted run in the primary block.
    """
    out, run = [], []
    for ch in sorted(missing, key=ord):
        if run and ord(ch) == ord(run[-1]) + 1:
            run.append(ch)
        else:
            if len(run) >= MIN_RUN:
                out.append((run[0], run[-1], len(run), _shared_descriptor(run)))
            run = [ch]
    if len(run) >= MIN_RUN:
        out.append((run[0], run[-1], len(run), _shared_descriptor(run)))
    return out


_universe_cache = {}


def script_universe(code):
    """Every letter/mark codepoint with Script=<code>, grouped by Unicode block."""
    if code in _universe_cache:
        return _universe_cache[code]
    try:
        pat = regex.compile(r"\p{Script=%s}" % code)
    except regex.error:
        _universe_cache[code] = None
        return None
    by_block = defaultdict(set)
    for cp in range(MAXCP):
        ch = chr(cp)
        if ud.category(ch) in LETTER_CATS and pat.match(ch):
            by_block[fud.block(ch)].add(ch)
    _universe_cache[code] = dict(by_block)
    return _universe_cache[code]


def read_rules(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if rows and rows[0][:2] == ["Orth", "Phon"]:
        rows = rows[1:]
    return [(r[0], r[1] if len(r) > 1 else "") for r in rows if r and r[0] != ""]


def analyse(path, fold=True):
    slug = path.name[: -len(".csv")]
    source = path.parent.name
    lang, _, code = slug.partition("-")
    rules = read_rules(path)

    # A character counts as reachable if it appears in any Orth, in either normal form; a rule
    # set that spells with digraphs is not incomplete for having no single-codepoint rule. But
    # reachable-only-inside-a-digraph is tracked apart: standing alone, it still has no rule.
    direct, anywhere = set(), set()
    for orth, _phon in rules:
        for form in (ud.normalize("NFC", orth), ud.normalize("NFD", orth)):
            anywhere.update(form)
            if len(form) == 1:
                direct.add(form)

    # Epitran case-folds before applying the map: a capital with NO rule of its own still
    # transcribes, via its lowercase rule ('Ⲍ' -> 'z' with only 'ⲍ' mapped). Measured directly by
    # indexing-17, 2026-09-07. So a capital is covered when its lowercase form is, and counting
    # capitals as gaps overstates every bicameral script — it was overstating Armenian by 38 and
    # Coptic by 24 here. Folding is a property of the PIPELINE, not of the file, so --no-fold
    # exists: if a future Epitran stops folding, every capital becomes a real gap at once.
    def is_covered(ch):
        if ch in anywhere:
            return True
        # Test the UNIVERSE character against the rules, not the other way round: 'Ⲍ' is covered
        # because 'Ⲍ'.lower() == 'ⲍ' is mapped. Folding the rule side instead is a no-op, which
        # is how the first version of this silently changed nothing at all.
        return fold and (ch.lower() in anywhere or ch.casefold() in anywhere)

    universe = script_universe(code)
    if universe is None:
        return {"slug": slug, "source": source, "lang": lang, "script": code,
                "rows": len(rules), "error": "unknown script code — not an ISO 15924 alias regex knows"}

    total = sum(len(v) for v in universe.values())
    blocks = {}
    for block, chars in universe.items():
        cov = {c for c in chars if is_covered(c)}
        blocks[block] = {
            "chars": len(chars), "covered": len(cov),
            "missing": sorted((c for c in chars if not is_covered(c)), key=ord),
        }
    covered_total = sum(b["covered"] for b in blocks.values())

    primary = max(blocks, key=lambda b: (blocks[b]["covered"], blocks[b]["chars"]))
    prim = blocks[primary]
    prim_pct = 100 * prim["covered"] / prim["chars"] if prim["chars"] else 0

    # The cop-Copt signature: the main block is genuinely worked on, and some OTHER block holding
    # real letters of the same script is untouched. That is a block walk, not a judgement call.
    #
    # The gate is an ABSOLUTE count of covered characters in the primary block, not a percentage.
    # A percentage gate (>=50%) was tried first and silently suppressed cop-Copt itself, the case
    # this check exists for: the Coptic block carries 77 Old Coptic / cryptogrammic / dialectal
    # variants nobody intends to transcribe, so a fully-worked rule set still scores 30% there and
    # fell under the threshold. Padded denominators make a percentage the wrong instrument; "did
    # someone do real work in this block" is a count.
    mechanical = []
    if total <= OPEN_SCRIPT and prim["covered"] >= 10:
        for block, b in blocks.items():
            if block != primary and b["covered"] == 0 and b["chars"] >= 2:
                mechanical.append(block)

    # Run gaps, like the block signal, are only meaningful on a closed script. On Latin or Han
    # every unused range is an unbroken uncovered run, and reporting them buries the real ones.
    all_missing = ([c for b in blocks.values() for c in b["missing"]]
                   if total <= OPEN_SCRIPT else [])

    return {
        "slug": slug, "source": source, "lang": lang, "script": code, "rows": len(rules),
        "run_gaps": run_gaps(all_missing),
        "total": total, "covered": covered_total,
        "pct": 100 * covered_total / total if total else 0,
        "open_script": total > OPEN_SCRIPT,
        "primary_block": primary, "primary_pct": prim_pct,
        "blocks": blocks, "mechanical_blocks": mechanical,
        "digraph_only": sorted((anywhere - direct) & set().union(*universe.values()), key=ord),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--json")
    ap.add_argument("--no-fold", action="store_true",
                    help="count capitals as gaps (only if Epitran stops case-folding)")
    args = ap.parse_args()

    from pathlib import Path
    paths = sorted(p for d in args.dirs for p in Path(d).glob("*.csv")
                   if not p.name.endswith(".NOTES.tsv") and ".NOTES" not in p.name)
    results = [analyse(p, fold=not args.no_fold) for p in paths]

    # Reviewed-complete exemptions. ⚠ These suppress the MECHANICAL label ONLY.
    # Every set is still measured and still printed with its numbers, so an
    # exemption cannot conceal a regression — only a gap someone has argued for
    # in writing, with a date and an author. Without this the standing check
    # re-flags cmn-Bopo for ever: it is 37/37 on Mandarin zhuyin and scores 49%
    # because Unicode's Bopomofo repertoire also holds Min Nan and Hakka. A check
    # that cries wolf on a correct file trains people to ignore it.
    reviewed_path = Path(__file__).parent / "coverage_reviewed.json"
    reviewed = {}
    if reviewed_path.exists():
        reviewed = {k: v for k, v in json.loads(reviewed_path.read_text()).items()
                    if not k.startswith("_")}
    for r in results:
        if r["slug"] in reviewed and r.get("mechanical_blocks"):
            r["reviewed_complete"] = reviewed[r["slug"]]
            r["mechanical_blocks"] = []

    mech = [r for r in results if r.get("mechanical_blocks")]
    exempt = [r for r in results if r.get("reviewed_complete")]
    print(f"{len(results)} rule set(s).  case-folding: {'OFF' if args.no_fold else 'ON'}.  "
          f"{len(mech)} with a whole secondary block uncovered (mechanical), "
          f"{len(exempt)} reviewed-complete.\n")
    for r in exempt:
        print(f"  reviewed-complete: {r['slug']:12s} {r['reviewed_complete']['reason'][:96]}")
    if exempt:
        print()

    if mech:
        print("=" * 78)
        print("MECHANICAL — main block worked on, another block of the same script untouched")
        print("=" * 78)
        for r in sorted(mech, key=lambda r: r["pct"]):
            print(f"\n{r['slug']} [{r['source']}]  Script={r['script']}  {r['covered']}/{r['total']} "
                  f"({r['pct']:.0f}%)  primary={r['primary_block']} ({r['primary_pct']:.0f}%)")
            for block in r["mechanical_blocks"]:
                b = r["blocks"][block]
                print(f"    {block}: 0/{b['chars']} covered")
                for ch in b["missing"]:
                    print(f"      U+{ord(ch):04X} {ch}  {ud.name(ch, '?')}")

    runs = [r for r in results if r.get("run_gaps")]
    print("\n" + "=" * 78)
    print("RUN GAPS — an unbroken run of letters, all uncovered, whose names agree")
    print("(the same omission made inside one block, where the block signal cannot see it)")
    print("=" * 78)
    for r in sorted(runs, key=lambda r: -max(g[2] for g in r["run_gaps"])):
        print(f"\n{r['slug']} [{r['source']}]  {r['covered']}/{r['total']} ({r['pct']:.0f}%)")
        for lo, hi, n, desc in sorted(r["run_gaps"], key=lambda g: -g[2]):
            print(f"    U+{ord(lo):04X}-U+{ord(hi):04X}  {n:>3} chars  {desc or '(names do not agree)'}")

    print("\n" + "=" * 78)
    print("ALL RULE SETS  (* = open script; a whole-script % is not a completion measure)")
    print("=" * 78)
    for r in sorted(results, key=lambda r: (r.get("open_script", False), r.get("pct", 0))):
        if "error" in r:
            print(f"  {r['slug']:<14} {r['source'][:9]:<9} {r['rows']:>4} rows   !! {r['error']}")
            continue
        star = "*" if r["open_script"] else " "
        flag = "  <-- MECHANICAL" if r["mechanical_blocks"] else ""
        print(f" {star}{r['slug']:<14} {r['source'][:9]:<9} {r['rows']:>4} rows  "
              f"{r['covered']:>4}/{r['total']:<5} {r['pct']:>5.1f}%  blocks={len(r['blocks'])}{flag}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2, default=str)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    sys.exit(main())
