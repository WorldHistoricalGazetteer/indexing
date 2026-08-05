#!/usr/bin/env python
"""Regression suite for the `un` country boundaries.

Two boundary-source migrations have each fixed a class of defect that a third
could silently reintroduce, because nothing downstream complains: a wrong
country polygon produces confidently wrong `ccodes`, not an error.

Natural Earth → BNDA (2026) fixed:
  * ``-99`` dropout — countries the source declined to code at all;
  * sliver polygons along shared borders;
  * **antimeridian wrap** — a part spanning nearly the whole globe because the
    ring was not split at ±180;
  * **spurious overlap** — a Netherlands polygon overlapping Great Britain, so
    British places resolved as Dutch.

BNDA → geoBoundaries HPSC (place#173) fixed coastal fidelity: 232 → 73,663
vertices per country on average.

The checks below are deliberately **data-derived** rather than a list of
hand-picked coordinates. Curating "this point is in that country" by hand is
how two errors got into this very analysis — a Chatham Islands point 200 m
offshore, and an Attu point on the wrong side of the dateline. Asking instead
"does every country's own representative point resolve to itself?" needs no
outside knowledge, cannot be got wrong the same way, and catches the overlap
defect directly: if a Dutch polygon covers Great Britain, Britain's own
representative point resolves to both.

Run it on a compute node (it loads every country's full geometry):

    sbatch processing/verify_un_geometries.sbatch

Exit status is non-zero if any HARD check fails. Overlap findings are reported
for **review, not thresholded** — disputed zones are now legitimately claimed
by more than one country (see ``processing/data/disputed_claims.json``), so an
overlap is a question, not a verdict.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

from shapely.geometry import Point
from shapely.prepared import prep

from processing.ccode_enrichment import (
    _UnGeometryCache,
    _load_un_records,
    build_un_prefilter,
    split_by_tier,
)

# A part spanning more than this many degrees of longitude is the antimeridian
# signature: no country's single landmass part is half the globe wide. Russia,
# the widest, spans ~171° in total and far less per part once split at ±180.
MAX_PART_LON_SPAN_DEG = 180.0

# Below this, a country outline is too coarse for point-in-polygon work at the
# coast. BNDA averaged 232 vertices/country; geoBoundaries HPSC averages
# 73,663. The floor is set well under the latter to flag a source regression
# without tripping on genuinely tiny states.
MIN_MEAN_VERTICES_PER_COUNTRY = 5_000

# Beyond this latitude a polygon is a polar cap, for which spanning every
# longitude is correct rather than a wrap.
POLAR_LAT_DEG = 85.0


def _parts(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if hasattr(geom, "geoms"):
        return [g for g in geom.geoms]
    return [geom]


def _vertex_count(geom) -> int:
    total = 0
    for part in _parts(geom):
        ext = getattr(part, "exterior", None)
        if ext is not None:
            total += len(ext.coords)
            total += sum(len(r.coords) for r in part.interiors)
    return total


def load_geometries() -> tuple[dict[str, list], list[dict[str, Any]]]:
    """(ccode → [shapely geoms], raw un records) for the PRIMARY tier."""
    records = _load_un_records()
    primary, _fallback = split_by_tier(records)
    _cells, ccode_to_geoms = build_un_prefilter(primary)
    cache = _UnGeometryCache(ccode_to_geoms)
    geoms = {cc: cache.geoms_for(cc) for cc in sorted(ccode_to_geoms)}
    return {cc: g for cc, g in geoms.items() if g}, primary


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_antimeridian(geoms: dict[str, list]) -> list[str]:
    """HARD. No single part may span more than half the globe.

    The Natural Earth defect: a ring crossing ±180 written as a single part
    running the long way round, so its bbox covered most of the planet and its
    containment test answered yes almost everywhere.

    A polar cap is exempt. Antarctica encircles the South Pole, so spanning
    every longitude is not a wrap but the only way to draw it — the first run
    of this suite flagged ``AQ`` at 360.0° and was wrong to.
    """
    failures = []
    for ccode, parts_of in sorted(geoms.items()):
        for geom in parts_of:
            for part in _parts(geom):
                minx, miny, maxx, maxy = part.bounds
                span = maxx - minx
                if span <= MAX_PART_LON_SPAN_DEG:
                    continue
                if miny <= -POLAR_LAT_DEG or maxy >= POLAR_LAT_DEG:
                    continue  # polar cap: all longitudes converge
                failures.append(
                    f"{ccode}: part spans {span:.1f}° of longitude "
                    f"({minx:.3f} → {maxx:.3f}) — antimeridian wrap")
    return failures


def check_hull_fallback(geoms: dict[str, list]) -> list[str]:
    """HARD. No country may be standing in for itself with a convex hull.

    ``_UnGeometryCache._load`` falls back to the staged ``hull`` when the geom
    store has no entry for a ``geom_ref``. That fallback is silent, and a hull
    is catastrophic for containment: Jamaica's hull swallows open sea, and a
    hull of any country with a concave border claims its neighbours' land.

    The first run of this suite reported JM at 16 vertices, BN 24, BA 40, BE
    46, KE 65 — against a 75,586 mean. Vertex count alone is ambiguous (a
    genuinely tiny state is legitimately simple), so this tests the actual
    property: a real country outline is never equal to its own convex hull.
    """
    failures = []
    for ccode, parts_of in sorted(geoms.items()):
        for geom in parts_of:
            if geom.is_empty or geom.area <= 0:
                continue
            hull = geom.convex_hull
            if hull.area <= 0:
                continue
            # equals_exact with a generous tolerance: a hull round-tripped
            # through GeoJSON and 6-dp rounding is not bit-identical.
            if geom.equals(hull) or (geom.area / hull.area) > 0.9999:
                failures.append(
                    f"{ccode}: geometry IS its convex hull "
                    f"({_vertex_count(geom)} vertices) — geom-store lookup "
                    f"missed, silently substituting a hull")
    return failures


def check_repr_point_self_resolution(
    geoms: dict[str, list],
    records: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """HARD (self) + REVIEW (extras).

    Every country's own representative point must fall inside its own polygon.
    Failure means the geometry and its metadata have parted company.

    A point resolving into *other* countries too is the spurious-overlap
    signature — the NL-over-GB defect. Reported for review, because disputed
    territories are now deliberately multi-claimed.
    """
    prepared: dict[str, list] = {
        cc: [prep(g) for g in gs] for cc, gs in geoms.items()
    }

    repr_points: dict[str, list[Point]] = defaultdict(list)
    for doc in records:
        ccodes = doc.get("ccodes") or []
        if not ccodes or not isinstance(ccodes[0], str):
            continue
        for geom in (doc.get("geometries") or []):
            rp = geom.get("repr_point") if isinstance(geom, dict) else None
            if isinstance(rp, dict):
                try:
                    repr_points[ccodes[0]].append(
                        Point(float(rp["lon"]), float(rp["lat"])))
                except (KeyError, TypeError, ValueError):
                    continue

    self_failures: list[str] = []
    overlaps: list[str] = []

    for ccode, points in sorted(repr_points.items()):
        if ccode not in prepared:
            continue
        for i, pt in enumerate(points):
            if not any(p.intersects(pt) for p in prepared[ccode]):
                self_failures.append(
                    f"{ccode}: repr_point #{i} ({pt.x:.4f}, {pt.y:.4f}) "
                    f"is OUTSIDE its own geometry")
                continue
            others = [
                other for other, preps in prepared.items()
                if other != ccode and any(p.intersects(pt) for p in preps)
            ]
            if others:
                overlaps.append(
                    f"{ccode}: repr_point #{i} ({pt.x:.4f}, {pt.y:.4f}) "
                    f"also falls inside {', '.join(sorted(others))}")

    return self_failures, overlaps


def check_coastal_fidelity(geoms: dict[str, list]) -> tuple[list[str], dict]:
    """HARD on the mean; REPORTS the tail, which the mean hides.

    geoBoundaries gbOpen ADM0 aggregates whatever each national source
    publishes, so detail varies across five orders of magnitude: NZ 2,668,713
    vertices, JM 16. The mean (75,586) therefore flatters the corpus badly —
    the median is 10,575, and 38 countries sit at or below BNDA-class detail
    (BNDA averaged 232), among them Belgium at 46, Kenya at 65 and Argentina
    at 365. For those countries the migration bought no precision at all.

    Quoting the mean alone is how that went unnoticed, so the tail is reported
    explicitly rather than summarised away.
    """
    counts = {cc: sum(_vertex_count(g) for g in gs)
              for cc, gs in geoms.items()}
    if not counts:
        return ["no country geometries loaded at all"], {}
    mean = sum(counts.values()) / len(counts)
    ordered = sorted(counts.values())
    median = ordered[len(ordered) // 2]
    failures = []
    if mean < MIN_MEAN_VERTICES_PER_COUNTRY:
        failures.append(
            f"mean vertices/country {mean:,.0f} is below the floor "
            f"{MIN_MEAN_VERTICES_PER_COUNTRY:,} — coarse-outline regression")
    stats = {
        "countries": len(counts),
        "total_vertices": sum(counts.values()),
        "mean_vertices": round(mean, 1),
        "median_vertices": median,
        "per_country": counts,
        # BNDA averaged 232 vertices/country. Countries at or under that gained
        # nothing from the migration and are the candidates for a future
        # upgrade — recorded so the claim stays honest.
        "no_better_than_bnda": sorted(
            [(cc, n) for cc, n in counts.items() if n <= 232],
            key=lambda t: t[1]),
        "least_detailed": sorted(counts.items(), key=lambda t: t[1])[:40],
        "under_1000_vertices": sorted(
            [(cc, n) for cc, n in counts.items() if n < 1000],
            key=lambda t: t[1]),
        "most_detailed": sorted(counts.items(), key=lambda t: -t[1])[:10],
    }
    return failures, stats


def check_against_baseline(
    counts: dict[str, int],
    baseline_path: str,
    *,
    drop_fraction: float = 0.5,
) -> tuple[list[str], list[str]]:
    """REVIEW. Diff per-country outline detail against a recorded release.

    geoBoundaries rewrites country attribution and geometry between releases.
    The LFS manifest pins WHICH release we hold; this pins what that release
    was actually like, so an upgrade that silently coarsens a country — or
    drops one — is visible instead of being absorbed into a moved mean.

    Returns (regressions, additions_and_removals).
    """
    try:
        with open(baseline_path, encoding="utf-8") as fh:
            baseline = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return ([f"no readable baseline at {baseline_path} — "
                 f"write one with --write-baseline"], [])

    prev = baseline.get("per_country", {})
    regressions, membership = [], []

    for cc, was in sorted(prev.items()):
        now = counts.get(cc)
        if now is None:
            membership.append(f"{cc}: present in baseline ({was:,} vertices), "
                              f"ABSENT now")
        elif was > 0 and now < was * drop_fraction:
            regressions.append(
                f"{cc}: {was:,} → {now:,} vertices "
                f"({100 * (1 - now / was):.0f}% less detail)")

    for cc in sorted(set(counts) - set(prev)):
        membership.append(f"{cc}: NEW ({counts[cc]:,} vertices)")

    return regressions, membership


def check_outlying_territories(geoms: dict[str, list]) -> list[str]:
    """REVIEW. Countries reduced to one part have lost their islands.

    Not hard-failed: which territories carry the parent's ISO code is a
    source's editorial choice, not an error. But a country that HAD outlying
    parts and now has one is the shape of a silent loss.
    """
    single = [cc for cc, gs in geoms.items()
              if sum(len(_parts(g)) for g in gs) == 1]
    return sorted(single)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json-out", help="Write the full report as JSON")
    ap.add_argument("--skip-overlap", action="store_true",
                    help="Skip repr-point cross-resolution (the slow check)")
    ap.add_argument("--baseline",
                    help="Compare per-country outline detail against this "
                         "recorded release baseline")
    ap.add_argument("--write-baseline",
                    help="Write the current per-country detail as a baseline "
                         "for the next release to be compared against")
    args = ap.parse_args()

    print("Loading `un` primary-tier geometries…", flush=True)
    geoms, records = load_geometries()
    print(f"  {len(geoms)} countries, "
          f"{sum(len(_parts(g)) for gs in geoms.values() for g in gs):,} parts",
          flush=True)

    report: dict[str, Any] = {}
    hard_failures: list[str] = []

    print("\n[1/4] antimeridian — no part may span >180° longitude", flush=True)
    am = check_antimeridian(geoms)
    report["antimeridian"] = am
    hard_failures += am
    print(f"  {'FAIL: ' + str(len(am)) if am else 'pass'}")
    for line in am[:10]:
        print(f"    {line}")

    print("\n[1b/4] hull fallback — no country may be its own convex hull",
          flush=True)
    hulls = check_hull_fallback(geoms)
    report["hull_fallback"] = hulls
    hard_failures += hulls
    print(f"  {'FAIL: ' + str(len(hulls)) if hulls else 'pass'}")
    for line in hulls[:25]:
        print(f"    {line}")

    print("\n[2/4] coastal fidelity — outline detail floor", flush=True)
    cf, stats = check_coastal_fidelity(geoms)
    report["coastal_fidelity"] = {"failures": cf, "stats": stats}
    hard_failures += cf
    print(f"  {'FAIL' if cf else 'pass'}: mean "
          f"{stats.get('mean_vertices', 0):,.0f}, median "
          f"{stats.get('median_vertices', 0):,} vertices/country "
          f"across {stats.get('countries', 0)}")
    for line in cf:
        print(f"    {line}")
    nb = stats.get("no_better_than_bnda", [])
    print(f"  review: {len(nb)} countries at or below BNDA-class detail "
          f"(<=232 vertices) — the migration bought them nothing:")
    print(f"    {', '.join(f'{cc}({n})' for cc, n in nb[:25])}"
          f"{' …' if len(nb) > 25 else ''}")

    counts = stats.get("per_country", {})
    if args.write_baseline:
        with open(args.write_baseline, "w", encoding="utf-8") as fh:
            json.dump({"per_country": counts}, fh, indent=2, sort_keys=True)
        print(f"  baseline written to {args.write_baseline}")
    if args.baseline:
        print("\n[2b/4] release baseline — detail regressions (review)",
              flush=True)
        regs, membership = check_against_baseline(counts, args.baseline)
        report["baseline_regressions"] = regs
        report["baseline_membership"] = membership
        print(f"  {len(regs)} regression(s), {len(membership)} membership "
              f"change(s)")
        for line in (regs + membership)[:30]:
            print(f"    {line}")

    print("\n[3/4] outlying territories — single-part countries (review)",
          flush=True)
    single = check_outlying_territories(geoms)
    report["single_part_countries"] = single
    print(f"  {len(single)} single-part: {', '.join(single[:25])}"
          f"{' …' if len(single) > 25 else ''}")

    if args.skip_overlap:
        print("\n[4/4] repr-point resolution — SKIPPED")
    else:
        print("\n[4/4] repr-point resolution — self (hard) + others (review)",
              flush=True)
        self_fail, overlaps = check_repr_point_self_resolution(geoms, records)
        report["repr_point_self_failures"] = self_fail
        report["repr_point_overlaps"] = overlaps
        hard_failures += self_fail
        print(f"  self-resolution: {'FAIL: ' + str(len(self_fail)) if self_fail else 'pass'}")
        for line in self_fail[:15]:
            print(f"    {line}")
        print(f"  cross-resolution (review): {len(overlaps)}")
        for line in overlaps[:25]:
            print(f"    {line}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nReport written to {args.json_out}")

    print("\n" + "=" * 70)
    if hard_failures:
        print(f"HARD FAILURES: {len(hard_failures)}")
        return 1
    print("All hard checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
