#!/usr/bin/env python
"""Generate a Wikipedia-links update patch from the Wikidata dump.

Streams ``latest-all.json.gz`` once and emits one JSONL row per geographic wd
entity that carries Wikipedia sitelinks::

    {"place_id": "wd:Q84", "links": [{"type": "seeAlso",
        "identifier": "https://en.wikipedia.org/wiki/London"}, ...]}

The rows are produced by reusing the **production** place-doc builder
(``authorities/wikidata-places.py :: create_place_doc_fast``), so the emitted
``place_id``s and ``links`` are byte-identical to what a full wd rebuild would
write. The patch therefore targets exactly the docs already in the live
``places`` index — no orphan updates. Apply with ``apply_links_patch.py`` to
land the links in place without a full rebuild.

    python -m processing.wikidata_links_patch \
        --out /vast/ishi/staged/wd/update_patch/places.links.jsonl [--limit N]
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

import orjson

from processing.settings import DATA_DIR, STAGED_BASE_DIR

# The authority builder lives in a hyphenated, script-style file that is not a
# normal importable module. Load it by path so we reuse its dump streamer and
# place-doc builder verbatim rather than duplicating the geographic prefilter.
_WD_PATH = Path(__file__).resolve().parent.parent / "authorities" / "wikidata-places.py"


def _load_wd_module():
    spec = importlib.util.spec_from_file_location("wikidata_places", _WD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def generate(dump_path: str, out_path: str, limit: int | None = None) -> tuple[int, int]:
    wp = _load_wd_module()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    n_in = n_out = 0
    t0 = time.time()
    with out.open("wb") as f:
        for entity in wp.stream_wikidata_fast(dump_path):
            n_in += 1
            if n_in % 100_000 == 0:
                sys.stdout.write(
                    f"\r[links-patch] scanned {n_in:,} entities, "
                    f"emitted {n_out:,} link rows...")
                sys.stdout.flush()
            try:
                doc, _ = wp.create_place_doc_fast(entity, orjson.dumps(entity))
            except Exception:
                continue
            if not doc:
                continue
            links = doc.get("links")
            if not links:
                continue
            f.write(orjson.dumps({"place_id": doc["place_id"], "links": links}))
            f.write(b"\n")
            n_out += 1
            if limit and n_out >= limit:
                break

    print(f"\n[links-patch] done: {n_out:,} rows written to {out} "
          f"(scanned {n_in:,} entities in {time.time() - t0:.0f}s)")
    return n_in, n_out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--dump", default=f"{DATA_DIR}/wikidata/latest-all/latest-all.json.gz",
        help="Wikidata JSON dump (.json.gz)")
    ap.add_argument(
        "--out", default=f"{STAGED_BASE_DIR}/wd/update_patch/places.links.jsonl",
        help="output patch JSONL")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N emitted rows (for sampling/verification)")
    args = ap.parse_args()

    print(f"[links-patch] dump: {args.dump}")
    print(f"[links-patch] out:  {args.out}")
    generate(args.dump, args.out, limit=args.limit)


if __name__ == "__main__":
    main()
