#!/usr/bin/env python
"""GB1900 edition — reconcile the crowd transcription with the VLM read.

The VLM (Tier-1) reads each residual label off the map and can correct genuine
crowd mis-transcriptions, but has a small tail (early-stops, dropped leading
chars, occasional over/under-read). Rather than over-tune the prompt, we keep
BOTH readings with provenance and pick a final text conservatively — defaulting to
the crowd transcription (verified, often 3+ agreement) and accepting the VLM only
when it is a confident, plausible correction. See plan §11.5.

    reconcile(hint, vlm_text) -> (final_text, source, rule)
      source in {"hint", "vlm", "agree"}; rule explains the decision.

Also exposes build_edition() to merge a Tier-0 typed JSONL + a VLM-output JSONL
into a provenance-carrying edition JSONL (§11.1).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _lev(a: str, b: str) -> int:
    """Levenshtein edit distance (small strings)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_illegible(s: str) -> bool:
    """Crowd placeholder for text they couldn't read (XXXX / ??? / ---- / ...)."""
    u = s.strip().upper()
    return bool(u) and (set(u) <= {"X"} or set(u) <= {"?"} or set(u) <= {"-"}
                        or set(u) <= {"."})


def reconcile(hint: str | None, vlm_text: str | None) -> tuple[str, str, str]:
    """Return (final_text, source, rule). Conservative: crowd hint wins unless the
    VLM is a confident small correction. Both readings are retained by the caller."""
    h = (hint or "").strip()
    v = (vlm_text or "").strip()
    if not v:
        return h, "hint", "vlm-empty"
    if not h:
        return v, "vlm", "no-hint"
    if _is_illegible(h):
        return v, "vlm", "resolved-illegible"    # crowd couldn't read it; VLM did
    hl, vl = h.lower(), v.lower()
    if hl == vl:
        return h, "agree", "match"            # keep crowd casing (steadier)
    if vl in hl:
        return h, "hint", "vlm-truncated"     # VLM dropped part of the label
    if hl in vl:
        return h, "hint", "vlm-overread"      # VLM ran into a neighbour
    if _lev(hl, vl) <= 1 and abs(len(h) - len(v)) <= 1:
        return v, "vlm", "correction"         # confident single-char fix only
    return h, "hint", "divergent"             # larger divergence → keep hint, flag QA


# ---------------------------------------------------------------------------
# Edition build: Tier-0 typed JSONL + VLM output JSONL → provenance edition
# ---------------------------------------------------------------------------

def _text_value(t) -> str:
    return (t.get("value") if isinstance(t, dict) else t) or ""


def build_edition(tier0_path: str, vlm_path: str | None, out_path: str,
                  version: str = "gbtype-v1") -> dict:
    """Merge the Tier-0 typed records with the VLM residual output into the final
    provenance-carrying edition (§11.1). Tier-0-typed pins keep their crowd text +
    Tier-0 type; residual pins with a VLM read get reconciled text + os_style type;
    residual pins without a VLM read stay text=crowd, type=null (pending)."""
    vlm = {}
    if vlm_path and Path(vlm_path).exists():
        for line in open(vlm_path, encoding="utf-8"):
            r = json.loads(line)
            vlm[r["pin_id"]] = r

    stats = {"total": 0, "tier0_typed": 0, "vlm_typed": 0, "pending": 0,
             "recon": {}}
    out = open(out_path, "w", encoding="utf-8")
    for line in open(tier0_path, encoding="utf-8"):
        rec = json.loads(line)
        stats["total"] += 1
        pid = rec["pin_id"]
        crowd = _text_value(rec.get("text"))
        edits = list(rec.get("edits") or [])
        text_layer = {"value": crowd, "source": "raw", "raw": crowd}
        type_layer = rec.get("type")           # Tier-0 type (or None for residual)

        if type_layer is not None:
            stats["tier0_typed"] += 1          # abbrev/keyword typed; crowd text kept
        elif pid in vlm:
            v = vlm[pid]["vlm"]
            vt = v.get("vlm_text")
            final, source, rule = reconcile(crowd, vt)
            stats["recon"][rule] = stats["recon"].get(rule, 0) + 1
            text_layer = {"value": final, "source": source, "rule": rule,
                          "raw": crowd, "vlm": vt}
            token = vlm[pid].get("vlm_type_token")
            if token:
                stats["vlm_typed"] += 1
                type_layer = {"token": token, "method": "tier1-vlm",
                              "os_style": v.get("os_style"),
                              "legible": v.get("legible"), "version": version}
                edits.append({"field": "type", "to": token, "method": "tier1-vlm",
                              "os_style": v.get("os_style"), "version": version})
            if source == "vlm":
                edits.append({"field": "text", "from": crowd, "to": final,
                              "method": "tier1-vlm", "rule": rule, "version": version})
        else:
            stats["pending"] += 1              # residual not yet VLM-processed

        rec["text"] = text_layer
        rec["type"] = type_layer
        rec["edits"] = edits
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out.close()
    return stats


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tier0", required=True, help="Tier-0 typed JSONL (gb1900_text_types)")
    p.add_argument("--vlm", help="VLM output JSONL (gb1900_vlm_infer)")
    p.add_argument("--out", required=True, help="edition JSONL out")
    p.add_argument("--version", default="gbtype-v1")
    args = p.parse_args(argv)
    stats = build_edition(args.tier0, args.vlm, args.out, args.version)
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
