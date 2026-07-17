#!/usr/bin/env python
"""GB1900 Tier-0 place typing — text-only, deterministic, no imagery/GPU.

Reads the **CC0 raw-dump** locations table
(``gb1900_locations.csv`` inside ``GB1900_final_raw_dump_july_2018.zip``;
2.67M pins, UTF-16, one row per pin) and assigns a coarse feature **type token**
to each label from its transcribed text, using the OS County Series abbreviation
+ keyword scheme in ``typesystem/data/gb1900_os_abbrev.json`` (transcribed from
OS 404, 1914).

Rule order (highest confidence first):
  1. illegible marker (``XXXX`` …)                → dropped (not a label)
  2. whole-label OS abbreviation (dot/space-insensitive: ``F.P.``→footpath) → typed
  3. keyword on a word boundary (``... Quarry``)   → typed
  4. ALLCAPS with no text tell                     → ROUTED to Tier-1 (not typed here)
  5. everything else                               → residual (Tier-1 candidate)

ALLCAPS is deliberately NOT a type on its own (see plan §4.1.2) — it is only a
routing flag, because on OS maps caps spans town/village/parish/water/antiquity
distinguished by font, which Tier-0 text cannot see.

Coordinates come as PostGIS EWKB hex (``g_point_wgs``) and are decoded to WGS84
lon/lat. The output record reserves fields for the later imagery tiers
(``bbox``, ``vlm_text``, ``os_style``) so Tier-1 can enrich in place.

Usage:
  python -m processing.gb1900_text_types --zip /path/GB1900_final_raw_dump_july_2018.zip \
      [--out typed.jsonl] [--limit N]
  python -m processing.gb1900_text_types --csv /path/gb1900_locations.csv --out typed.jsonl
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import struct
import sys
import zipfile
from collections import Counter
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "typesystem" / "data" / "gb1900_os_abbrev.json"
_LOC_MEMBER = "GB1900_final_raw_dump_july_2018/gb1900_locations.csv"

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub("", s).upper()


def load_dict(path: Path = _DATA) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    abbr = d["abbreviations"]
    # Build normalised lookup: uppercase-no-space, both with- and without-dots.
    lut: dict[str, dict] = {}
    for key, val in abbr.items():
        n = _norm(key)
        lut.setdefault(n, val)
        lut.setdefault(n.replace(".", ""), val)
    # Keyword regexes on word boundaries (longest first so "Old Quarry" beats "Quarry").
    kw = sorted(d["keywords"].items(), key=lambda kv: -len(kv[0]))
    kw_res = [(re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE), v) for k, v in kw]
    illegible = {_norm(x) for x in d["illegible"]}
    return {"lut": lut, "kw": kw_res, "illegible": illegible}


def ewkb_to_lonlat(h: str):
    """Decode PostGIS EWKB hex POINT → (lon, lat) rounded to 6dp, or (None, None)."""
    try:
        raw = bytes.fromhex(h)
        order = "<" if raw[0] == 1 else ">"
        gtype = struct.unpack(order + "I", raw[1:5])[0]
        pos = 5
        if gtype & 0x20000000:  # SRID flag
            pos += 4
        lon, lat = struct.unpack(order + "dd", raw[pos:pos + 16])
        return round(lon, 6), round(lat, 6)
    except Exception:
        return None, None


def classify(text: str, D: dict) -> tuple[str | None, str, bool]:
    """Return (token, rule, allcaps). token is None when not typed by Tier-0."""
    t = text.strip()
    n = _norm(t)
    letters = [c for c in t if c.isalpha()]
    allcaps = bool(letters) and all(c.isupper() for c in letters)
    if not t or n in D["illegible"]:
        return None, "illegible", allcaps
    # 2. whole-label abbreviation
    hit = D["lut"].get(n) or D["lut"].get(n.replace(".", ""))
    if hit:
        return hit["token"], "abbrev", allcaps
    # 3. keyword on a word boundary
    for rx, val in D["kw"]:
        if rx.search(t):
            return val["token"], "keyword", allcaps
    # 4. ALLCAPS with no text tell → route to Tier-1
    if allcaps:
        return None, "allcaps-router", allcaps
    return None, "residual", allcaps


def _open_locations(args) -> io.TextIOWrapper:
    if args.zip:
        z = zipfile.ZipFile(args.zip)
        member = args.member or _LOC_MEMBER
        return io.TextIOWrapper(z.open(member), encoding="utf-16", errors="replace")
    return io.TextIOWrapper(open(args.csv, "rb"), encoding="utf-16", errors="replace")


def run(args) -> None:
    D = load_dict(Path(args.dict) if args.dict else _DATA)
    reader = csv.DictReader(_open_locations(args))
    out = open(args.out, "w", encoding="utf-8") if args.out else None

    n = typed = illegible = allcaps_routed = residual = 0
    tokens = Counter()
    rules = Counter()
    residual_sample: list[str] = []

    for row in reader:
        n += 1
        text = row.get("first_transcription") or ""
        token, rule, allcaps = classify(text, D)
        rules[rule] += 1
        if rule == "illegible":
            illegible += 1
        elif token is not None:
            typed += 1
            tokens[token] += 1
        elif rule == "allcaps-router":
            allcaps_routed += 1
        else:
            residual += 1
            if len(residual_sample) < 40:
                residual_sample.append(text.strip())
        if out is not None:
            lon, lat = ewkb_to_lonlat(row.get("g_point_wgs") or "")
            # Provenance-carrying record (plan §11.1): source values are kept
            # untouched; every derivation is a layer with method + version, and
            # each change is logged in edits[] so the edition is fully traceable.
            edits = []
            type_layer = None
            if token is not None:
                type_layer = {"token": token, "method": "tier0-abbrev-keyword",
                              "rule": rule, "confidence": 1.0, "version": args.version}
                edits.append({"field": "type", "from": None, "to": token,
                              "method": "tier0-" + rule, "version": args.version})
            rec = {
                "place_id": f"gb:{row.get('pin_id','')}",
                "pin_id": row.get("pin_id", ""),
                "source": {
                    "dataset": "gb1900_final_raw_dump_2018", "licence": "CC0",
                    "first_transcription": text.strip(),
                    "classification_count": row.get("classification_count"),
                },
                "lon": lon, "lat": lat,
                "text": {"value": text.strip(), "source": "raw"},
                "type": type_layer,                 # None when Tier-0 didn't type it
                "tier0_rule": rule, "allcaps": allcaps,
                # reserved for the imagery tiers (Tier-1 fills these in place):
                "bbox": None, "os_style": None,
                "edits": edits,
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if args.limit and n >= args.limit:
            break

    if out is not None:
        out.close()

    typeable = n - illegible
    print("\n===== GB1900 Tier-0 coverage report =====")
    print(f"rows read:              {n:,}")
    print(f"illegible (dropped):    {illegible:,} ({100*illegible/n:.1f}%)")
    print(f"typed (Tier-0):         {typed:,} ({100*typed/n:.1f}% of all, "
          f"{100*typed/typeable:.1f}% of typeable)")
    print(f"ALLCAPS → Tier-1:       {allcaps_routed:,} ({100*allcaps_routed/n:.1f}%)")
    print(f"residual → Tier-1:      {residual:,} ({100*residual/n:.1f}%)")
    print(f"\nrules: {dict(rules)}")
    print(f"\ntop 30 tokens:")
    for tok, c in tokens.most_common(30):
        print(f"  {c:8,}  {tok}")
    print(f"\nresidual sample (non-caps, untyped): {residual_sample[:40]}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--zip", help="GB1900_final_raw_dump_july_2018.zip")
    src.add_argument("--csv", help="extracted gb1900_locations.csv (UTF-16)")
    p.add_argument("--member", help="csv member name inside the zip (default: locations)")
    p.add_argument("--dict", help="override abbreviation dict path")
    p.add_argument("--out", help="write per-record typed JSONL here")
    p.add_argument("--version", default="gbtype-v1",
                   help="classification version stamped on each derived field "
                        "(plan §11.2)")
    p.add_argument("--limit", type=int, default=None, help="stop after N rows")
    return run(p.parse_args(argv)) or 0


if __name__ == "__main__":
    sys.exit(main())
