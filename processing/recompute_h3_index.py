#!/usr/bin/env python
"""One-off remediation: fix ``geometries[].h3_cover`` for area features in the
LIVE places index, in place (no reindex / no alias swap → no downtime).

Background: ``h3_stage`` historically read the hull-stripped parquet, so every
area feature whose h3 input was a parquet (osm/ohm via boundary_merge) got a
centroid-only ``h3_cover``. ``h3_stage`` is now fixed (computes the cover from
the real geom-store polygon); this tool back-fills the already-indexed docs.

Two phases, run separately so the result can be verified between them:

    # 1. Recompute (read-only on the index; no writes). Parallelisable via
    #    sliced scroll: run several with --slice 0..N-1 --of N.
    python -m processing.recompute_h3_index compute \
        --namespaces osm,ohm --out /vast/ishi/h3fix/osm_ohm.NN.jsonl \
        --slice 0 --of 8

    # 2. Apply the corrections (throttled in-place scripted bulk update).
    python -m processing.recompute_h3_index apply \
        --patch '/vast/ishi/h3fix/osm_ohm.*.jsonl' --rps 1500 --batch 500

The ``compute`` phase only emits a geometry when its recomputed cover actually
differs from what is indexed, so ``apply`` touches the minimum number of docs
and is safe to re-run (idempotent).
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from typing import Any, Iterable

import requests

from processing.helpers import compute_h3_fields
from processing.geom_store import GeomStoreReader
from processing.settings import GEOM_STORE_DIR

# The gateway/ES live on the same host; talk to the direct ES backend.
ES_URL = "http://localhost:9201"
PLACES_ALIAS = "places"
ELASTIC_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"

# A feature whose bounding box is smaller than ~one r7 cell (≈1.2 km / 0.01°)
# cannot produce a multi-cell h3_cover — its cover is just the centroid, which
# is already what is indexed, and it is matched in spatial containment via its
# repr_point regardless. The h3_cover bug only ever broke *multi-cell* features
# (regions / boundaries). So skip the expensive geom-store read + polyfill for
# sub-cell features entirely and recompute only the ones that were actually
# wrong. This avoids reading the millions of buildings / POIs / small ways.
SKIP_BBOX_DEG = 0.01


def _auth() -> tuple[str, str]:
    with open(ELASTIC_PASSWORD_FILE) as fh:
        return ("elastic", fh.read().strip())


def _session() -> requests.Session:
    s = requests.Session()
    s.auth = _auth()
    s.headers.update({"Content-Type": "application/json"})
    return s


def _bbox_maxdim_deg(geom: dict) -> float | None:
    """Largest bbox side (degrees) from the stored ``bounds`` [w,s,e,n].

    Returns ``None`` when bounds are absent, malformed, or antimeridian-spanning
    (w > e) — i.e. "unknown / treat as large", so such features are never
    skipped by the sub-cell gate.
    """
    b = geom.get("bounds")
    if not (isinstance(b, list) and len(b) == 4):
        return None
    try:
        w, s, e, n = (float(x) for x in b)
    except (TypeError, ValueError):
        return None
    dx = e - w
    if dx < 0:  # crosses the antimeridian → not sub-cell
        return None
    return max(dx, n - s)


def _repr_lonlat(geom: dict) -> tuple[float, float] | None:
    rp = geom.get("repr_point")
    if isinstance(rp, dict) and isinstance(rp.get("lon"), (int, float)):
        return float(rp["lon"]), float(rp["lat"])
    if isinstance(rp, list) and len(rp) == 2:
        return float(rp[0]), float(rp[1])
    return None


# ---------------------------------------------------------------------------
# Phase 1 — compute
# ---------------------------------------------------------------------------

def _open_pit(sess, keep_alive: str) -> str:
    r = sess.post(f"{ES_URL}/{PLACES_ALIAS}/_pit?keep_alive={keep_alive}")
    r.raise_for_status()
    return r.json()["id"]


def _close_pit(sess, pit_id: str) -> None:
    try:
        sess.delete(f"{ES_URL}/_pit", data=json.dumps({"id": pit_id}))
    except Exception:
        pass


def _paginate(sess, query: dict, source: list[str], batch: int,
              slice_id: int, slice_max: int, after: list | None = None,
              keep_alive: str = "30m") -> Iterable[tuple[dict, list]]:
    """Yield ``(hit, sort)`` via PIT + ``search_after``.

    Robust to slow batches: a PIT's keep_alive is refreshed on every search
    call, so a long-running page (large polygons → slow polyfill) cannot orphan
    the context the way a fixed scroll TTL did. ``after`` resumes a crashed run.
    """
    pit_id = _open_pit(sess, keep_alive)
    try:
        while True:
            body: dict[str, Any] = {
                "size": batch,
                "_source": source,
                "query": query,
                "sort": [{"_shard_doc": "asc"}],
                "pit": {"id": pit_id, "keep_alive": keep_alive},
                "track_total_hits": False,
            }
            if slice_max > 1:
                body["slice"] = {"id": slice_id, "max": slice_max}
            if after is not None:
                body["search_after"] = after
            r = sess.post(f"{ES_URL}/_search", data=json.dumps(body))
            r.raise_for_status()
            data = r.json()
            pit_id = data.get("pit_id", pit_id)  # PIT id may rotate
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break
            for h in hits:
                yield h, h.get("sort")
            after = hits[-1].get("sort")
    finally:
        _close_pit(sess, pit_id)


def compute(args) -> int:
    namespaces = [n.strip() for n in args.namespaces.split(",") if n.strip()]
    reader = GeomStoreReader(GEOM_STORE_DIR)
    sess = _session()

    should = [{"prefix": {"place_id": f"{ns}:"}} for ns in namespaces]
    query = {
        "bool": {
            "filter": [{"nested": {"path": "geometries",
                                   "query": {"term": {"geometries.has_geom": True}}}}],
            "should": should,
            "minimum_should_match": 1,
        }
    }
    source = ["place_id", "geometries.geometry_index", "geometries.has_geom",
              "geometries.repr_point", "geometries.h3_cover", "geometries.h3_centroid",
              "geometries.bounds"]

    # Resume: a per-slice cursor file holds the last processed search_after key
    # so a crash never restarts the (multi-hour) scan from zero. Output is
    # appended when resuming; the apply phase is idempotent on duplicates.
    cursor_path = f"{args.out}.cursor"
    after = None
    mode = "w"
    try:
        with open(cursor_path) as fh:
            after = json.load(fh)
            mode = "a"
            print(f"[slice {args.slice}/{args.of}] resuming after {after}", flush=True)
    except (FileNotFoundError, ValueError):
        after = None

    docs = changed = geoms = errors = skipped = 0
    t0 = time.time()
    with open(args.out, mode, encoding="utf-8") as out:
        for hit, sort in _paginate(sess, query, source, args.batch,
                                   args.slice, args.of, after=after):
            docs += 1
            src = hit.get("_source", {})
            pid = src.get("place_id")
            patch_geoms = []
            for idx, g in enumerate(src.get("geometries", []) or []):
                if not isinstance(g, dict) or not g.get("has_geom"):
                    continue
                geoms += 1
                gi = g.get("geometry_index", idx)
                # Sub-cell features keep their (correct) centroid cover — skip
                # the geom-store read + polyfill entirely.
                md = _bbox_maxdim_deg(g)
                if md is not None and md < SKIP_BBOX_DEG:
                    skipped += 1
                    continue
                ll = _repr_lonlat(g)
                if ll is None:
                    continue
                try:
                    gj = reader.get(f"{pid}_{gi}")
                except Exception:
                    gj = None
                    errors += 1
                if not (isinstance(gj, dict) and gj.get("type") and gj.get("coordinates")):
                    continue
                centroid, cover = compute_h3_fields(lon=ll[0], lat=ll[1], geojson_geom=gj)
                if not cover:
                    continue
                old = g.get("h3_cover") or []
                # Only emit when the cover actually changes (set compare).
                if set(cover) == set(old):
                    continue
                patch_geoms.append({"geometry_index": gi,
                                    "h3_centroid": centroid, "h3_cover": cover})
            if patch_geoms:
                changed += 1
                out.write(json.dumps({"place_id": pid, "geometries": patch_geoms}) + "\n")
            if docs % 10000 == 0:
                out.flush()
                with open(cursor_path, "w") as cf:
                    json.dump(sort, cf)  # checkpoint resume key
            if docs % 50000 == 0:
                rate = docs / max(time.time() - t0, 1e-6)
                print(f"[slice {args.slice}/{args.of}] scanned={docs} changed={changed} "
                      f"geoms={geoms} skipped={skipped} errors={errors} "
                      f"rate={rate:.0f}/s", flush=True)
    dt = time.time() - t0
    # Completed cleanly — drop the cursor so a future run starts fresh.
    try:
        import os
        os.remove(cursor_path)
    except OSError:
        pass
    print(f"[slice {args.slice}/{args.of}] DONE scanned={docs} changed={changed} "
          f"geoms={geoms} skipped={skipped} errors={errors} in {dt:.0f}s "
          f"-> {args.out}", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Phase 2 — apply (throttled, in-place scripted update)
# ---------------------------------------------------------------------------

_UPDATE_SCRIPT = (
    "if (ctx._source.geometries == null) { return; } "
    "for (g in ctx._source.geometries) { "
    "  if (g.geometry_index != null && params.u.containsKey(g.geometry_index.toString())) { "
    "    def x = params.u.get(g.geometry_index.toString()); "
    "    g.h3_cover = x.cover; g.h3_centroid = x.centroid; "
    "  } "
    "}"
)


def _iter_patch(patterns: list[str]) -> Iterable[dict]:
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)


def apply(args) -> int:
    sess = _session()
    # Resolve the concrete index behind the alias so the update is unambiguous.
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
        payload = "\n".join(batch_lines) + "\n"
        resp = sess.post(f"{ES_URL}/{index}/_bulk?refresh=false", data=payload)
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
        # Throttle: cap to ~rps docs/sec.
        elapsed = time.time() - t0
        target = sent / max(args.rps, 1)
        if target > elapsed:
            time.sleep(target - elapsed)
        if sent % (args.batch * 20) == 0:
            print(f"  applied sent={sent} ok={ok} failed={failed} "
                  f"rate={sent/max(time.time()-t0,1e-6):.0f}/s", flush=True)

    for rec in _iter_patch(patterns):
        pid = rec.get("place_id")
        u = {str(g["geometry_index"]): {"cover": g["h3_cover"], "centroid": g["h3_centroid"]}
             for g in rec.get("geometries", [])}
        if not pid or not u:
            continue
        batch_lines.append(json.dumps({"update": {"_id": pid}}))
        batch_lines.append(json.dumps({
            "script": {"source": _UPDATE_SCRIPT, "lang": "painless", "params": {"u": u}}
        }))
        if len(batch_lines) >= args.batch * 2:
            flush()
    flush()
    print(f"APPLY DONE sent={sent} ok={ok} failed={failed} in {time.time()-t0:.0f}s", flush=True)
    return 1 if failed else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Recompute/repair h3_cover in the live places index")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compute", help="recompute h3_cover from the geom store (read-only)")
    c.add_argument("--namespaces", default="osm,ohm")
    c.add_argument("--out", required=True)
    c.add_argument("--slice", type=int, default=0)
    c.add_argument("--of", type=int, default=1)
    c.add_argument("--batch", type=int, default=1000)
    c.set_defaults(func=compute)

    a = sub.add_parser("apply", help="apply the patch in place (throttled)")
    a.add_argument("--patch", required=True, help="patch file or glob")
    a.add_argument("--rps", type=int, default=1500, help="approx docs/sec cap")
    a.add_argument("--batch", type=int, default=500)
    a.set_defaults(func=apply)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
