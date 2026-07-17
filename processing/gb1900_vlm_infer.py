#!/usr/bin/env python
"""GB1900 Tier-1 VLM inference — typography (os_style) + text reading per crop.

Called by ``gb1900_vlm.sbatch`` (one array task per shard, against a local
``vllm serve`` OpenAI endpoint), or standalone. For each label crop it asks the
VLM to (a) read the label's text (``vlm_text`` — our own correction, replacing the
forgone curated fixes) and (b) classify its typography into the **documented OS
style code** (``os_style``); the feature type is then a table lookup against
``gb1900_os_lettering.json``. Output is provenance-carrying and keyed on
``gb:<pin_id>`` (identifiers preserved). Resumable: crops already in ``--out`` are
skipped.

Mirrors GOTW's ``triage_pages.py`` pattern: OpenAI-compatible chat/completions,
strict json_schema, temperature 0, ThreadPoolExecutor, base64 crops.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

_STYLES = ["RP", "RC", "IC", "EC", "OldEnglish", "GermanText", "Stump",
           "Ornamental", "illegible"]

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["vlm_text", "os_style", "case", "size_band", "tracking", "legible"],
    "properties": {
        "vlm_text": {"type": "string",
                     "description": "the label's text as printed on the map"},
        "os_style": {"type": "string", "enum": _STYLES},
        "case": {"type": "string", "enum": ["lower", "title", "caps", "smallcaps"]},
        "size_band": {"type": "string",
                      "enum": ["small", "medium", "large", "extra_large"]},
        "tracking": {"type": "string", "enum": ["tight", "normal", "wide"]},
        "is_water_or_antiquity": {"type": "boolean"},
        "legible": {"type": "boolean"},
    },
}

PROMPT = (
    "This is a crop from an Ordnance Survey six-inch (1:10560) County Series map, "
    "2nd edition (c.1900). A small RED RING marks where ONE map label begins (the "
    "bottom-left of its first letter). Read THAT label — follow it from the ring in "
    "whatever direction it runs: OS labels may be horizontal, sloped, or curved "
    "(e.g. along a river, coast, or ridge). Read ONLY the single, CONTIGUOUS label "
    "that starts at the ring, and STOP at its end — where its letters stop and a "
    "clear gap begins. Do NOT run on into a separate neighbouring label even if it "
    "is close (a different word after a gap is a different label). IGNORE every "
    "other label in the crop.\n"
    "Transcribe it VERBATIM — the exact characters printed on the map. PRESERVE "
    "Ordnance Survey abbreviations and punctuation exactly: e.g. give 'W', 'F.P.', "
    "'Ch.', 'B.M.', 'P' literally — do NOT expand them to 'Well', 'Foot Path', "
    "'Church', etc. Keep the printed hyphens and capitalisation. Report only what is "
    "actually inked, not your interpretation of it.\n"
    "Then classify the ring-marked label's typography using Ordnance Survey "
    "conventions:\n"
    "- RP = Roman Print (serif, mixed-case) — named buildings\n"
    "- RC = Roman Capitals — prominent settlements/administrative places\n"
    "- IC = Italic Capitals — water & designed-water features\n"
    "- EC = Egyptian Capitals (slab sans-serif) — ROMAN antiquities\n"
    "- OldEnglish = black-letter — pre-historic/Saxon antiquities\n"
    "- GermanText = black-letter — Norman/medieval antiquities\n"
    "- Ornamental = decorative — counties/county boroughs\n"
    "- Stump = the plain standard stamped hand — everything else (minor features)\n"
    "Also give case, size_band, letter-spacing (tracking), whether it is a water/"
    "antiquity label, and whether it is legible. Return JSON per the schema."
)


def load_style_map(lettering_path: Path) -> dict:
    """os_style -> coarse feature token (the curated crosswalk, not list[0])."""
    d = json.loads(lettering_path.read_text(encoding="utf-8"))
    s2t = d.get("style_to_type_token")
    if s2t:
        return {k: v for k, v in s2t.items() if not k.startswith("_")}
    s2f = d.get("style_to_features", {})
    return {k: (v[0] if v else None) for k, v in s2f.items()}


def infer_one(client: httpx.Client, endpoint: str, model: str, rec: dict) -> dict | None:
    try:
        b64 = base64.b64encode(Path(rec["crop_path"]).read_bytes()).decode()
    except Exception:
        return None
    # Pass the crowd transcription as a HINT: it helps the VLM locate the ring-marked
    # label and anchors the reading, while we still ask for an independent correction.
    txt = rec.get("text")
    hint = txt.get("value") if isinstance(txt, dict) else txt
    prompt = PROMPT
    if hint:
        prompt = PROMPT + (
            f"\nHINT (a GUIDE ONLY — it MAY CONTAIN ERRORS): a crowd volunteer "
            f"transcribed the ring-marked label as \"{hint}\". It can be misspelt, "
            f"wrong, or partial, so do not blindly copy it — use it to help locate the "
            f"label, then read the label yourself. Fix genuine mis-transcriptions "
            f"(wrong or missing letters), but give the VERBATIM printed text: do NOT "
            f"expand abbreviations or otherwise change what is actually inked.")
    body = {
        "model": model, "temperature": 0, "max_tokens": 300,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "os_label", "strict": True, "schema": SCHEMA}},
    }
    try:
        r = client.post(f"{endpoint}/chat/completions", json=body, timeout=120)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        return {"_error": str(e)[:120]}


def run(args) -> None:
    recs = [json.loads(l) for l in open(args.crops, encoding="utf-8")]
    recs = [r for i, r in enumerate(recs) if i % args.nshards == args.shard]
    done = set()
    if Path(args.out).exists():
        for l in open(args.out, encoding="utf-8"):
            try:
                done.add(json.loads(l)["pin_id"])
            except Exception:
                pass
    recs = [r for r in recs if r["pin_id"] not in done]
    style_map = load_style_map(Path(args.lettering)) if args.lettering else {}
    print(f"[vlm] shard {args.shard}/{args.nshards}: {len(recs):,} crops "
          f"({len(done):,} already done)")

    out = open(args.out, "a", encoding="utf-8")
    with httpx.Client() as client:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(infer_one, client, args.endpoint, args.model, r): r
                    for r in recs}
            n = 0
            for fut in as_completed(futs):
                r = futs[fut]
                v = fut.result()
                if v is None or "_error" in (v or {}):
                    continue
                token = style_map.get(v.get("os_style"))
                rec = {
                    "place_id": r.get("place_id"), "pin_id": r["pin_id"],
                    "tier0_token": r.get("token"),
                    "vlm": v, "vlm_type_token": token,
                    "model": args.model, "version": args.version,
                    "edits": [{"field": "os_style", "to": v.get("os_style"),
                               "method": "tier1-vlm", "version": args.version},
                              {"field": "text", "to": v.get("vlm_text"),
                               "method": "tier1-vlm", "version": args.version}],
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
                if n % 100 == 0:
                    out.flush()
                    print(f"[vlm] {n:,} done")
    out.close()
    print(f"[vlm] shard {args.shard} complete → {args.out}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--crops", required=True, help="crop manifest JSONL")
    p.add_argument("--endpoint", required=True, help="vLLM OpenAI base, e.g. http://localhost:PORT/v1")
    p.add_argument("--model", default="Qwen/Qwen2.5-VL-72B-Instruct-AWQ")
    p.add_argument("--out", required=True)
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--nshards", type=int, default=1)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--version", default="gbtype-v1")
    p.add_argument("--lettering",
                   default=str(Path(__file__).resolve().parent.parent
                               / "typesystem" / "data" / "gb1900_os_lettering.json"))
    run(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
