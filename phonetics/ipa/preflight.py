#!/usr/bin/env python3
"""
Prove every mode a plan intends to use actually loads and produces IPA, BEFORE
spending a cluster on it.

Two distinct failures this catches, both of which otherwise surface only as a
quietly smaller corpus hours later:

  load_failed  the mode name resolves to nothing on this host. Epitran ships
               some languages as CSV maps and some as code, and this project
               installs 115 more via scripts/install_epitran_extensions.sh, so
               "the route table says X" and "X works here" are different
               claims. yue-Hant, for instance, raises FileNotFoundError on the
               current CRC env.

  echo_only    the mode loads and hands the input straight back. cmn-Hans does
               exactly this for Latin text ('Manchester' -> 'Manchester'). An
               echo is worse than a failure: it is a plausible string that
               would train something on nothing.

Exits non-zero when any mode fails, so it can gate a submission script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

PROBES = {
    "Latn": "Manchester", "Cyrl": "Москва", "Grek": "Αθήνα",
    "Arab": "القاهرة", "Hebr": "ירושלים", "Deva": "दिल्ली",
    "Beng": "ঢাকা", "Taml": "சென்னை", "Telu": "హైదరాబాద్",
    "Mlym": "കൊച്ചി", "Knda": "ಬೆಂಗಳೂರು", "Gujr": "અમદાવાદ",
    "Thai": "กรุงเทพ", "Geor": "თბილისი", "Armn": "Երևան",
    "Hrgn": "とうきょう", "Ktkn": "トーキョー", "Hans": "北京",
}


def probe_modes(modes: List[str]) -> Dict[str, dict]:
    import epitran
    out: Dict[str, dict] = {}
    for mode in sorted(set(modes)):
        tag = mode.split("-")[-1]
        text = PROBES.get(tag, "Manchester")
        try:
            epi = epitran.Epitran(mode)
        except Exception as e:
            out[mode] = {"status": "load_failed",
                         "error": f"{type(e).__name__}: {e}"[:200]}
            continue
        try:
            got = epi.transliterate(text)
        except Exception as e:
            out[mode] = {"status": "transliterate_failed",
                         "error": f"{type(e).__name__}: {e}"[:200]}
            continue
        if not got:
            out[mode] = {"status": "empty_output", "probe": text}
        elif got == text:
            out[mode] = {"status": "echo_only", "probe": text, "ipa": got}
        else:
            out[mode] = {"status": "ok", "probe": text, "ipa": got}
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Verify the Epitran modes a plan will use actually work")
    ap.add_argument("--plan", help="only the modes this plan uses")
    ap.add_argument("--all-installed", action="store_true",
                    help="every mode the route table could reach")
    ap.add_argument("--json-out")
    a = ap.parse_args()

    from phonetics.ipa import routes as R

    if a.plan:
        plan = json.loads(Path(a.plan).read_text())
        modes = [s["mode"] for s in plan["shards"]
                 if s.get("backend") == "epitran" and s.get("mode")]
    elif a.all_installed:
        modes = sorted(R.installed_epitran_modes())
    else:
        raise SystemExit("need --plan or --all-installed")

    results = probe_modes(modes)
    by_status: Dict[str, int] = {}
    for v in results.values():
        by_status[v["status"]] = by_status.get(v["status"], 0) + 1

    print(f"modes probed: {len(results)}")
    for s, c in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"   {s:<22} {c:>4}")
    bad = {m: v for m, v in results.items() if v["status"] != "ok"}
    if bad:
        print("\n-- not usable --")
        for m, v in sorted(bad.items()):
            print(f"   {m:<14} {v['status']:<22} "
                  f"{v.get('error', v.get('ipa',''))[:70]}")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(
            {"by_status": by_status, "modes": results}, indent=2,
            ensure_ascii=False))
        print(f"\n-> {a.json_out}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
