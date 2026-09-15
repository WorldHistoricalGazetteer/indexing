#!/usr/bin/env python3
"""Prove the Python and JS Symphonym script-range tables agree.

The tokeniser exists twice: canonically in `phonetics/utils/script_detection.py`
(this repo) and as a hand-maintained port in whg3's
`whg/webpack/js/recon-symphonym-preprocess.js`. Nothing reconciles them. If they
disagree about a codepoint the script id differs, the model is conditioned
differently on each side, and BOTH sides still produce a perfectly valid id — so
the failure is silent and survives any amount of testing that only asks whether
a vector came back.

    A diff with no control is a check that cannot fail.

A parser that silently drops half a table reports maximum disagreement, which is
indistinguishable from a real defect and considerably more exciting. That is not
hypothetical: the first version of this comparison regexed the JS nested-bracket
table, captured 37 of 56 ranges, and confidently reported 2,523 disagreeing
codepoints headed "live v7 bug" — BENGALI's only range having gone missing. What
caught it was arithmetic (37 parsed against ~56 visible in the source), not
judgement. Judgement is no use once you already believe the result.

So this script refuses to report anything until the parse has independently
reproduced known values, including the DELIBERATE U+FB01 -> ARMENIAN defect.
That control is a better parity proof than the zero-disagreement count itself:
zero disagreements is also what two empty tables produce, whereas reproducing a
known wrong answer on both sides can only happen if both tables really loaded.

Exit status: 0 = tables agree on every shared script; 1 = real disagreement;
2 = the check could not be trusted (parse failed a control).

Usage:  python3 check_script_range_parity.py [--js PATH] [--baseline PATH] [-v]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_BASELINE = Path(__file__).with_name("symphonym-v8-script-ranges.json")

#: The JS half lives in ANOTHER REPOSITORY, so its path cannot be committed as a
#: fact. `WHG3_JS` overrides; the default is one developer's checkout and is a
#: convenience, not a contract. ⚠ In CI, set WHG3_JS explicitly and treat exit 2
#: as a FAILURE, never a skip — "I could not find the other table" reported as a
#: pass is the same silent-agreement bug this script exists to prevent, rebuilt
#: one layer up.
DEFAULT_JS = Path(os.environ.get(
    "WHG3_JS",
    Path.home() / "Documents/GitHub/whg3/whg/webpack/js/recon-symphonym-preprocess.js"))

# (codepoint, expected script, why it is worth pinning)
CONTROLS = [
    (0xFB01, "ARMENIAN", "Latin ligature U+FB01 'fi' — the DELIBERATE defect the "
                         "72.7M-doc index was built with; Armenian ligatures shadow "
                         "Hebrew presentation forms and the later entry wins"),
    (0x0995, "BENGALI",  "Bengali KA — the range the broken parser dropped"),
    (0x0041, "LATIN",    "LATIN CAPITAL A"),
    (0x0416, "CYRILLIC", "CYRILLIC ZHE"),
    (0x4E2D, "CJK",      "CJK U+4E2D"),
    (0x30A2, "KATAKANA", "KATAKANA A"),
    (0x0E01, "THAI",     "THAI KO KAI"),
]

# Ranges the JS table is known to contain. Arithmetic, not inspection: this is
# what catches a parser that silently drops entries.
EXPECTED_JS_SCRIPTS = 19
EXPECTED_JS_RANGES = 56


class Untrustworthy(Exception):
    """The comparison could not be trusted — never report a result after this."""


def parse_js_table(path: Path) -> list[tuple[str, list[tuple[int, int]]]]:
    """Read SCRIPT_UNICODE_RANGES, preserving order (later entry wins).

    Converts the JS literal to JSON rather than regexing nested brackets, which
    is what went wrong the first time.
    """
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Untrustworthy(
            f"cannot read the JS table at {path}: {exc}. Set WHG3_JS to the "
            f"whg3 checkout's recon-symphonym-preprocess.js") from exc

    marker = "const SCRIPT_UNICODE_RANGES = "
    if marker not in src:
        raise Untrustworthy(f"{marker!r} not found in {path} — has it been renamed?")

    block = src[src.index(marker) + len(marker):]
    if "\n];" not in block:
        raise Untrustworthy("could not find the end of the JS table")
    block = block[: block.index("\n];") + 2]
    block = re.sub(r"//.*", "", block)                                   # comments
    block = re.sub(r"0x([0-9A-Fa-f]+)", lambda m: str(int(m.group(1), 16)), block)
    block = block.replace("'", '"')
    block = re.sub(r",(\s*[\]\}])", r"\1", block)                        # trailing commas
    try:
        raw = json.loads(block)
    except json.JSONDecodeError as exc:
        raise Untrustworthy(f"JS table did not convert to JSON: {exc}") from exc
    return [(name, [tuple(p) for p in ranges]) for name, ranges in raw]


def parse_baseline(path: Path) -> list[tuple[str, list[tuple[int, int]]]]:
    """Read the committed Python table. Dict order is table order (py3.7+)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Untrustworthy(f"cannot read the baseline at {path}: {exc}") from exc
    if "ranges" not in doc:
        raise Untrustworthy(f"{path} has no 'ranges' key")
    return [(name, [(int(lo, 16), int(hi, 16)) for lo, hi in ranges])
            for name, ranges in doc["ranges"].items()]


def build_map(table) -> dict[int, str]:
    """codepoint -> script. LATER ENTRY WINS, on both sides. Do not 'fix' this."""
    out: dict[int, str] = {}
    for name, ranges in table:
        for lo, hi in ranges:
            for cp in range(lo, hi + 1):
                out[cp] = name
    return out


def run_controls(js_map, js_table, verbose) -> None:
    n_scripts, n_ranges = len(js_table), sum(len(r) for _, r in js_table)
    if (n_scripts, n_ranges) != (EXPECTED_JS_SCRIPTS, EXPECTED_JS_RANGES):
        raise Untrustworthy(
            f"JS table parsed as {n_scripts} scripts / {n_ranges} ranges, expected "
            f"{EXPECTED_JS_SCRIPTS}/{EXPECTED_JS_RANGES}. Either the table genuinely "
            f"changed (update the constants, deliberately) or the parser is dropping "
            f"entries — which is the failure this check exists to prevent.")
    failures = []
    for cp, want, why in CONTROLS:
        got = js_map.get(cp, "OTHER")
        if verbose or got != want:
            print(f"  control U+{cp:04X} -> {got:<10} (want {want:<10}) "
                  f"{'ok' if got == want else 'FAIL'}   {why}")
        if got != want:
            failures.append((cp, want, got))
    if failures:
        raise Untrustworthy(
            "positive controls failed: " +
            ", ".join(f"U+{cp:04X} want {w} got {g}" for cp, w, g in failures))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--js", type=Path, default=DEFAULT_JS)
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    try:
        js_table = parse_js_table(args.js)
        py_table = parse_baseline(args.baseline)
        js_map, py_map = build_map(js_table), build_map(py_table)
        print(f"JS  {len(js_table):>2} scripts, {sum(len(r) for _, r in js_table):>3} ranges  ({args.js})")
        print(f"PY  {len(py_table):>2} scripts, {sum(len(r) for _, r in py_table):>3} ranges  ({args.baseline})")
        print("\npositive controls (nothing below is reported unless these pass):")
        run_controls(js_map, js_table, args.verbose)
        print("  all controls pass — both tables really loaded.\n")
    except Untrustworthy as exc:
        print(f"\nCHECK NOT TRUSTWORTHY: {exc}\n"
              f"Reporting nothing: a comparison whose subject failed to load "
              f"reports agreement, not correctness.", file=sys.stderr)
        return 2

    shared = {n for n, _ in js_table} | {"OTHER"}
    real, takeover, upside = [], [], []
    for cp in range(0x110000):
        j, p = js_map.get(cp, "OTHER"), py_map.get(cp, "OTHER")
        if j == p:
            continue
        if j in shared and p in shared:
            real.append((cp, j, p))
        elif j == "OTHER":
            upside.append((cp, j, p))
        else:
            takeover.append((cp, j, p))

    def collapse(items):
        out = []
        for cp, j, p in items:
            if out and out[-1][1] == cp - 1 and out[-1][2] == j and out[-1][3] == p:
                out[-1][1] = cp
            else:
                out.append([cp, cp, j, p])
        return out

    print(f"disagreement on a shared script : {len(real):>6} codepoints   <-- must be 0")
    for lo, hi, j, p in collapse(real)[:40]:
        print(f"    U+{lo:04X}-U+{hi:04X}  JS={j:<10} PY={p}")
    print(f"v7 script lost to a new script  : {len(takeover):>6} codepoints   <-- must be 0")
    for lo, hi, j, p in collapse(takeover)[:40]:
        print(f"    U+{lo:04X}-U+{hi:04X}  JS={j:<10} PY={p}")
    print(f"OTHER -> new script (expected)  : {len(upside):>6} codepoints in "
          f"{len(collapse(upside))} ranges")
    if upside and args.verbose:
        print("    scripts:", ", ".join(sorted({p for _, _, p in upside})))

    if real or takeover:
        print("\nFAIL: the two tables disagree about a script both of them know. "
              "That is a LIVE defect affecting users now, not a migration issue.")
        return 1
    print("\nPASS: the tables agree on every shared script. Remaining differences "
          "are additions the JS cannot yet reach (lost upside, not corruption).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
