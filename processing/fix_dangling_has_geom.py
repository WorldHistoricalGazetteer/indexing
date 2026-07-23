#!/usr/bin/env python
"""One-off remediation (place#145): clear ``has_geom`` where the geom store has
no entry for the geometry, in place (no reindex / no alias swap → no downtime).

Background: ``has_geom: true`` is a promise that ``{place_id}_{geometry_index}``
is retrievable from the ``/vast`` geom store — ``enrich_geometry`` sets it from
``GeomStoreWriter.write()``. But ``write()`` returns True once the WKB is
appended to the *staging* file, so a staging set that never reached
``consolidate_geom_store`` leaves the flag set with nothing behind it. As of
2026-07-23 that is true of ~10.5M OSM/OHM **way** records (zero ``osm:w*`` /
``ohm:w*`` keys exist in the store), plus a small residue elsewhere.

Consumers that trust the flag: ``gateway/spatial.py`` (exact containment reads
the store, silently degrading to ``repr_point`` when it comes back empty),
``processing/generate_tiles.py``, and the ``has_geom`` flag shipped to clients by
``/api/reconcile`` to mark a candidate as usable for ``contained_in``.

Two phases, run separately so the result can be verified between them::

    # 1. Scan (read-only on the index; no writes). Sliceable via --slice/--of.
    python -m processing.fix_dangling_has_geom scan \
        --out /vast/ishi/h3fix/dangling.jsonl

    # 2. Apply the corrections (throttled in-place scripted bulk update).
    python -m processing.fix_dangling_has_geom apply \
        --patch '/vast/ishi/h3fix/dangling.jsonl' --rps 1500

**``apply`` uses scripted ``_bulk`` updates, never ``_update_by_query``.** The
places index carries the ``extract_namespace`` default_pipeline, and
``_update_by_query`` re-runs it: its toponym-normalisation processor rewrites
``label`` from the (already normalised) ``toponym_id``, so any label carrying a
parenthesis/bracket/comma would be silently truncated. Bulk *update* ops do not
run the pipeline — both behaviours verified against prod on 2026-07-23.

Scanning is resumable (per-slice cursor) and ``apply`` is idempotent, so either
phase can be re-run after an interruption.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Iterable

from processing.recompute_h3_index import (  # same PIT/bulk plumbing
    ES_URL,
    PLACES_ALIAS,
    _paginate,
    _session,
)
from processing.settings import GEOM_STORE_DIR


def _store_keys() -> set[str]:
    """The geom store's key set (the 84 MB index.json, reduced to its keys)."""
    with open(f"{GEOM_STORE_DIR}/index.json") as fh:
        return set(json.load(fh))


# ---------------------------------------------------------------------------
# Phase 1 — scan
# ---------------------------------------------------------------------------

def scan(args) -> int:
    keys = _store_keys()
    print(f"geom-store keys: {len(keys):,}", flush=True)
    sess = _session()

    query = {"bool": {"filter": [
        {"nested": {"path": "geometries",
                    "query": {"term": {"geometries.has_geom": True}}}}]}}
    source = ["place_id", "geometries.geometry_index", "geometries.has_geom"]

    cursor_path = f"{args.out}.cursor"
    after, mode = None, "w"
    try:
        with open(cursor_path) as fh:
            after = json.load(fh)
            mode = "a"
            print(f"[slice {args.slice}/{args.of}] resuming after {after}", flush=True)
    except (FileNotFoundError, ValueError):
        pass

    docs = dangling_docs = dangling_geoms = intact = 0
    by_ns: dict[str, int] = {}
    t0 = time.time()
    with open(args.out, mode, encoding="utf-8") as out:
        for hit, sort in _paginate(sess, query, source, args.batch,
                                   args.slice, args.of, after=after):
            docs += 1
            src = hit.get("_source", {})
            pid = src.get("place_id")
            bad: list[int] = []
            for idx, g in enumerate(src.get("geometries", []) or []):
                if not isinstance(g, dict) or not g.get("has_geom"):
                    continue
                gi = g.get("geometry_index", idx)
                if f"{pid}_{gi}" in keys:
                    intact += 1
                else:
                    bad.append(gi)
            if bad:
                dangling_docs += 1
                dangling_geoms += len(bad)
                ns = (pid or ":").split(":", 1)[0]
                by_ns[ns] = by_ns.get(ns, 0) + 1
                out.write(json.dumps({"place_id": pid, "geometry_indices": bad}) + "\n")
            if docs % 10000 == 0:
                out.flush()
                with open(cursor_path, "w") as cf:
                    json.dump(sort, cf)
            if docs % 200000 == 0:
                print(f"[slice {args.slice}/{args.of}] scanned={docs:,} "
                      f"dangling={dangling_docs:,} intact={intact:,} "
                      f"rate={docs/max(time.time()-t0,1e-6):.0f}/s", flush=True)
    try:
        import os
        os.remove(cursor_path)
    except OSError:
        pass
    print(f"[slice {args.slice}/{args.of}] DONE scanned={docs:,} "
          f"dangling_docs={dangling_docs:,} dangling_geoms={dangling_geoms:,} "
          f"intact_geoms={intact:,} in {time.time()-t0:.0f}s -> {args.out}", flush=True)
    print("dangling by namespace:", json.dumps(by_ns, sort_keys=True), flush=True)
    return 0


# ---------------------------------------------------------------------------
# Phase 2 — apply
# ---------------------------------------------------------------------------

# Mirrors the scan's index derivation exactly: geometry_index when present,
# else the position in the array.
_UPDATE_SCRIPT = (
    "if (ctx._source.geometries == null) { return; } "
    "int i = 0; "
    "for (g in ctx._source.geometries) { "
    "  def gi = g.geometry_index != null ? g.geometry_index : i; "
    "  if (params.gi.contains(gi)) { g.has_geom = false; g.remove('geom_ref'); } "
    "  i++; "
    "}"
)


def _iter_patch(patterns: list[str]) -> Iterable[dict]:
    import glob
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)


def apply(args) -> int:
    sess = _session()
    r = sess.get(f"{ES_URL}/_alias/{PLACES_ALIAS}")
    r.raise_for_status()
    index = next(iter(r.json().keys()))
    print(f"target index: {index}  (rps≈{args.rps}, batch={args.batch})", flush=True)

    patterns = [args.patch] if isinstance(args.patch, str) else list(args.patch)
    sent = ok = failed = 0
    t0 = time.time()
    batch_lines: list[str] = []

    def flush():
        nonlocal sent, ok, failed, batch_lines
        if not batch_lines:
            return
        resp = sess.post(f"{ES_URL}/{index}/_bulk?refresh=false",
                         data="\n".join(batch_lines) + "\n")
        resp.raise_for_status()
        body = resp.json()
        n = len(batch_lines) // 2
        sent += n
        if body.get("errors"):
            for item in body.get("items", []):
                res = item.get("update", {})
                if res.get("status", 200) >= 300:
                    failed += 1
                    if failed <= 5:
                        print("  update error:", json.dumps(res)[:200], file=sys.stderr)
                else:
                    ok += 1
        else:
            ok += n
        batch_lines = []
        elapsed = time.time() - t0
        target = sent / max(args.rps, 1)
        if target > elapsed:
            time.sleep(target - elapsed)
        if sent % (args.batch * 200) == 0:
            print(f"  applied sent={sent:,} ok={ok:,} failed={failed} "
                  f"rate={sent/max(time.time()-t0,1e-6):.0f}/s", flush=True)

    for rec in _iter_patch(patterns):
        pid = rec.get("place_id")
        gi = rec.get("geometry_indices") or []
        if not pid or not gi:
            continue
        batch_lines.append(json.dumps({"update": {"_id": pid}}))
        batch_lines.append(json.dumps({
            "script": {"source": _UPDATE_SCRIPT, "lang": "painless",
                       "params": {"gi": gi}}
        }))
        if len(batch_lines) >= args.batch * 2:
            flush()
    flush()
    print(f"APPLY DONE sent={sent:,} ok={ok:,} failed={failed} "
          f"in {time.time()-t0:.0f}s", flush=True)
    return 1 if failed else 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Clear has_geom where the geom store has no entry (place#145)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="find has_geom geometries with no store entry")
    s.add_argument("--out", required=True)
    s.add_argument("--slice", type=int, default=0)
    s.add_argument("--of", type=int, default=1)
    s.add_argument("--batch", type=int, default=2000)
    s.set_defaults(func=scan)

    a = sub.add_parser("apply", help="apply the corrections in place (throttled)")
    a.add_argument("--patch", required=True, help="patch file or glob")
    a.add_argument("--rps", type=int, default=1500, help="approx docs/sec cap")
    a.add_argument("--batch", type=int, default=500)
    a.set_defaults(func=apply)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
