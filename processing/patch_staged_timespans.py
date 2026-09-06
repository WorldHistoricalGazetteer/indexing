#!/usr/bin/env python
"""Apply a timespans patch to a namespace's staged ``final/`` snapshot.

WHY THIS EXISTS. A `_bulk` patch repairs Elasticsearch and leaves the staged
snapshot untouched, so the two then disagree — and **tiles are built from the
staged snapshot, not from ES** (`generate_tiles._iter_namespace_docs`;
`TILE_ES_DOC_NAMESPACES` is an explicit opt-in that defaults to empty). Retiling
after an index-only patch therefore produces a tileset timestamped *after* the
fix and containing data from *before* it — which is worse than leaving the stale
one, because nothing re-examines an artefact whose date says it is current.

Demonstrated for #246 item 1: after patching 216,744 osm documents in ES,
`staged/osm/final/places.parquet` still returned
``{"start": {"latest": 2026}, "end": {"earliest": 2026}}`` for `osm:n54` where
the index held ``{"start": {"in": 1893}, ...}``.

⚠ The divergence matters beyond tiles. The staged snapshot is what a re-ingest,
a freshness gate or a later stage reads, so an index-only repair is a repair
with an invisible expiry.

HOW. The JSONL is the canonical artefact and is patched by streaming; the
parquet sidecar is then REGENERATED from it with `write_parquet_from_jsonl`,
which is the function that built it originally and handles the three
schema-stability problems (empty nested lists, variable-depth hull coordinates,
explicit nulls inside struct fields). Rewriting the parquet directly would mean
re-deriving that schema by hand.

⚠ Both writes are `.tmp` + `os.replace`. Superseded artefacts are never renamed
*within* the stage directory: every resolver prefers Parquet within a stage, so
a `places.parquet.bak` left beside the real one is one `mv` away from silently
outranking it — and the realistic trigger is somebody tidying up.

    python -m processing.patch_staged_timespans \\
        --namespace osm \\
        --patch /vast/ishi/staged/osm/temporal_patch.jsonl \\
        [--execute]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--patch", required=True,
                    help="JSONL of {place_id, timespans}")
    ap.add_argument("--stage", default="final")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    from processing.settings import STAGED_BASE_DIR
    from processing.staged_parquet import write_parquet_from_jsonl

    base = Path(STAGED_BASE_DIR) / args.namespace / args.stage
    jsonl, parquet = base / "places.jsonl", base / "places.parquet"
    if not jsonl.exists():
        raise SystemExit(f"ABORT: {jsonl} does not exist. This tool patches the "
                         f"canonical JSONL and regenerates the sidecar from it; "
                         f"with no JSONL there is nothing authoritative to patch.")

    patch = {}
    for line in Path(args.patch).open(encoding="utf-8"):
        row = json.loads(line)
        patch[row["place_id"]] = row["timespans"]
    print(f"[patch-staged] {len(patch):,} patch rows for {args.namespace}/{args.stage}")

    if not args.execute:
        print("[patch-staged] DRY-RUN: pass --execute to write. Nothing changed.")
        return 0

    tmp = jsonl.with_suffix(".jsonl.tmp")
    seen = patched = 0
    t0 = time.time()
    with jsonl.open(encoding="utf-8") as fin, tmp.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            seen += 1
            ts = patch.get(doc.get("place_id"))
            if ts is not None:
                for t in doc.get("toponyms") or []:
                    t["timespans"] = ts
                for g in doc.get("geometries") or []:
                    g["timespans"] = ts
                patched += 1
            # ⚠ ensure_ascii=True to MATCH THE PIPELINE, not because it is
            # better. Every other staged writer emits \uXXXX escapes
            # (`h3_merged/places.jsonl` carries \u0426 for Ц); writing UTF-8
            # here re-encodes all 20.6M lines and shrinks the file ~2% —
            # semantically identical, byte-different, and therefore exactly the
            # kind of meaningless difference that makes a later hash comparison
            # report a change that is not one. That is the trap flagged for
            # `fidelity.py`'s raw hash, committed by the tool written after
            # flagging it.
            fout.write(json.dumps(doc, ensure_ascii=True) + "\n")

    # The count is the check. A patch whose ids do not occur in the snapshot
    # rewrites 20.6M lines and changes nothing, which looks identical to success
    # from the outside — the same failure the TGN backfill committed against ES.
    missing = len(patch) - patched
    print(f"[patch-staged] {seen:,} docs read, {patched:,} patched "
          f"({missing:,} patch ids not present in the snapshot) "
          f"in {(time.time()-t0)/60:.1f} min")
    if patched == 0:
        tmp.unlink(missing_ok=True)
        raise SystemExit("ABORT: 0 documents matched. The snapshot is not the one "
                         "the patch was built for; nothing written.")

    os.replace(tmp, jsonl)
    print(f"[patch-staged] {jsonl} replaced atomically")

    ptmp = parquet.with_suffix(".parquet.tmp")
    t1 = time.time()
    write_parquet_from_jsonl(jsonl, ptmp)
    os.replace(ptmp, parquet)
    print(f"[patch-staged] {parquet} regenerated in {(time.time()-t1)/60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
