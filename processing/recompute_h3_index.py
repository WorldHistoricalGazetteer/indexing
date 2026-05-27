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


def _auth() -> tuple[str, str]:
    with open(ELASTIC_PASSWORD_FILE) as fh:
        return ("elastic", fh.read().strip())


def _session() -> requests.Session:
    s = requests.Session()
    s.auth = _auth()
    s.headers.update({"Content-Type": "application/json"})
    return s


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

def _scroll(sess, body: dict, scroll: str = "5m") -> Iterable[dict]:
    """Yield hits from a (possibly sliced) scroll."""
    r = sess.post(f"{ES_URL}/{PLACES_ALIAS}/_search?scroll={scroll}", data=json.dumps(body))
    r.raise_for_status()
    data = r.json()
    sid = data.get("_scroll_id")
    try:
        while True:
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break
            for h in hits:
                yield h
            r = sess.post(f"{ES_URL}/_search/scroll",
                          data=json.dumps({"scroll": scroll, "scroll_id": sid}))
            r.raise_for_status()
            data = r.json()
            sid = data.get("_scroll_id")
    finally:
        if sid:
            try:
                sess.delete(f"{ES_URL}/_search/scroll",
                            data=json.dumps({"scroll_id": [sid]}))
            except Exception:
                pass


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
    body: dict[str, Any] = {
        "size": args.batch,
        "_source": ["place_id", "geometries.geometry_index", "geometries.has_geom",
                    "geometries.repr_point", "geometries.h3_cover", "geometries.h3_centroid"],
        "query": query,
        "sort": ["_doc"],
    }
    if args.of > 1:
        body["slice"] = {"id": args.slice, "max": args.of}

    docs = changed = geoms = errors = 0
    t0 = time.time()
    with open(args.out, "w", encoding="utf-8") as out:
        for hit in _scroll(sess, body):
            docs += 1
            src = hit.get("_source", {})
            pid = src.get("place_id")
            patch_geoms = []
            for idx, g in enumerate(src.get("geometries", []) or []):
                if not isinstance(g, dict) or not g.get("has_geom"):
                    continue
                geoms += 1
                gi = g.get("geometry_index", idx)
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
            if docs % 50000 == 0:
                rate = docs / max(time.time() - t0, 1e-6)
                print(f"[slice {args.slice}/{args.of}] scanned={docs} changed={changed} "
                      f"geoms={geoms} errors={errors} rate={rate:.0f}/s", flush=True)
    dt = time.time() - t0
    print(f"[slice {args.slice}/{args.of}] DONE scanned={docs} changed={changed} "
          f"geoms={geoms} errors={errors} in {dt:.0f}s -> {args.out}", flush=True)
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
