"""Verify the ``un`` geometries in the /vast geom store against the LIVE index.

Why this exists
---------------
``verify_un_rebuild.py`` proves that every staged ``geom_ref`` *resolves* to a
polygon. That is not the same as proving it resolves to the **right** polygon:
a rebuild that shifted keys by one would resolve all 247 and be wrong in all
247. The 9 August geom-store verification made exactly that mistake, which is
why plan-completion-2026-08-31 §2.1 asks for a bounds comparison rather than a
lookup count.

So this compares the store against a measure the extract did not produce — the
``bounds`` and ``repr_point`` already sitting in the live ``places`` index:

* **bounds** — ES holds the convex-hull bbox of the geometry at ingest, rounded
  to ``COORDINATE_PRECISION``. The stored polygon's own bbox must match it.
* **repr_point** — guaranteed by ``enrich_geometry`` to lie *within* the
  geometry. A point-in-polygon test against the stored shape is the sharper of
  the two checks: two neighbouring countries can share a bbox to 1e-5 and
  cannot both contain each other's representative point.

Usage::

    # on the VM, where ES is reachable (247 docs, trivial query):
    python -m processing.verify_un_geom_store dump --out /vast/ishi/staged/un_live.json

    # on a compute node, where the geom store is:
    python -m processing.verify_un_geom_store check --live /vast/ishi/staged/un_live.json

Exits non-zero if any doc is missing from the store, or if its bounds or
representative point disagree — so it can gate an sbatch step.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Bounds are stored rounded to COORDINATE_PRECISION (6 dp), so the worst honest
# disagreement is 5e-7 per coordinate. Anything above this is a real mismatch,
# not rounding.
BOUNDS_TOLERANCE_DEG = 1e-5

NAMESPACE = "un"


def dump_live(es_host: str, out_path: str, namespace: str = NAMESPACE) -> int:
    """Write ``[{place_id, geom_ref, bounds, repr_point, boundary_source}]`` from the live index."""
    from elasticsearch import Elasticsearch

    from processing.settings import ES_PASSWORD_FILE

    with open(ES_PASSWORD_FILE, encoding="utf-8") as fh:
        password = fh.read().strip()
    es = Elasticsearch(es_host, basic_auth=("elastic", password), request_timeout=60)
    docs = []
    resp = es.search(
        index="places",
        size=1000,
        query={"prefix": {"place_id": f"{namespace}:"}},
        source=["place_id", "geometries"],
    )
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        for g in src.get("geometries") or []:
            docs.append({
                "place_id": src["place_id"],
                "geom_ref": g.get("geom_ref"),
                "bounds": g.get("bounds"),
                "repr_point": g.get("repr_point"),
                "boundary_source": g.get("boundary_source"),
                "geom_class": g.get("geom_class"),
                "has_geom": g.get("has_geom"),
            })
    Path(out_path).write_text(json.dumps(docs, indent=1))
    print(f"dumped {len(docs)} live {namespace} geometry entries -> {out_path}")
    return len(docs)


def _store_key_count(store_dir: Path, namespace: str) -> int | None:
    """Count ``{namespace}:`` keys directly in index.sqlite (the plan's check)."""
    sqlite_path = store_dir / "index.sqlite"
    if not sqlite_path.exists():
        return None
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        (n,) = conn.execute(
            "SELECT count(*) FROM geom WHERE k >= ? AND k < ?",
            (f"{namespace}:", f"{namespace};"),
        ).fetchone()
        return n
    finally:
        conn.close()


def check(live_path: str, namespace: str = NAMESPACE) -> int:
    from shapely.geometry import Point

    from processing.geom_store import GeomStoreReader
    from processing.helpers import geojson_to_shapely
    from processing.settings import GEOM_STORE_DIR

    live = json.loads(Path(live_path).read_text())
    print(f"live {namespace} geometry entries: {len(live)}")

    store_dir = Path(GEOM_STORE_DIR)
    n_keys = _store_key_count(store_dir, namespace)
    print(f"index.sqlite '{namespace}:' keys: "
          f"{n_keys if n_keys is not None else 'index.sqlite ABSENT'}")

    reader = GeomStoreReader(GEOM_STORE_DIR)

    missing, bad_bounds, outside, unparseable = [], [], [], []
    worst = 0.0
    for entry in live:
        ref = entry.get("geom_ref")
        if not ref:
            continue
        raw = reader.get(ref)
        if raw is None:
            missing.append(ref)
            continue
        shp = geojson_to_shapely(raw)
        if shp is None or shp.is_empty:
            unparseable.append(ref)
            continue

        want = entry.get("bounds")
        if want and len(want) == 4:
            got = shp.bounds
            delta = max(abs(a - b) for a, b in zip(got, want))
            worst = max(worst, delta)
            if delta > BOUNDS_TOLERANCE_DEG:
                bad_bounds.append((ref, [round(v, 6) for v in got], want, round(delta, 6)))

        rp = entry.get("repr_point")
        if rp and not shp.contains(Point(rp["lon"], rp["lat"])):
            outside.append((ref, rp))

    print(f"resolved from store:  {len(live) - len(missing)}/{len(live)}")
    print(f"worst bounds delta:   {worst:.3e} deg (tolerance {BOUNDS_TOLERANCE_DEG:g})")

    ok = True
    for label, rows in (("MISSING from store", missing),
                        ("UNPARSEABLE", unparseable),
                        ("BOUNDS MISMATCH", bad_bounds),
                        ("repr_point OUTSIDE stored polygon", outside)):
        if rows:
            ok = False
            print(f"\n*** {label}: {len(rows)}")
            for row in rows[:20]:
                print(f"    {row}")
            if len(rows) > 20:
                print(f"    ... and {len(rows) - 20} more")

    if n_keys is not None and n_keys != len(live):
        ok = False
        print(f"\n*** KEY COUNT: index.sqlite holds {n_keys} '{namespace}:' keys, "
              f"live index expects {len(live)}")

    print("\nVERDICT: " + ("PASS — store agrees with the live index"
                           if ok else "FAIL — see above"))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="dump live bounds/repr_point from ES")
    d.add_argument("--es-host", default="http://localhost:9201")
    d.add_argument("--namespace", default=NAMESPACE)
    d.add_argument("--out", required=True)

    c = sub.add_parser("check", help="compare the geom store against that dump")
    c.add_argument("--live", required=True)
    c.add_argument("--namespace", default=NAMESPACE)

    args = parser.parse_args()
    if args.cmd == "dump":
        dump_live(args.es_host, args.out, args.namespace)
        return 0
    return check(args.live, args.namespace)


if __name__ == "__main__":
    sys.exit(main())
