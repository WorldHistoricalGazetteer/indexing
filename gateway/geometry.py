# gateway/geometry.py
"""
``GET /api/geometry/{place_id}`` — one place's authoritative geometry, read
from the geom store rather than reassembled from vector-tile fragments.

Why (developer/plan-atlas-data-architecture.md §5.3). The Atlas turns a
selected boundary into a search constraint by collecting that feature's tile
fragments and unioning them client-side (place#156). That finds only the
fragments in *loaded* tiles — a region larger than the viewport is silently
truncated — and the geometry is whatever simplification the current zoom
happens to carry. The full polygon has been readable from the gateway host
since place#165 put the store index into SQLite (gateway RSS +3.8 MB, not
+5.4 GB), so it can simply be served.

What this returns. The union of every stored geometry of the place, as one
GeoJSON geometry, with its bounds and the vertex count. A place's geometries
are keyed by their own ``geom_ref`` (``"<place_id>_<geometry_index>"`` for
the place's own, or ANOTHER place's key where the geometry is borrowed — og
places carry wd polygons, ``processing/interlink_ottgaz.py``). Point-only
places have nothing in the store and are a 404 with ``error: "no geometry"``,
which is a different answer from ``error: "not found"`` (no such place). A
place whose index entry promises a stored geometry the store cannot produce
is a 404 ``error: "geometry incomplete"`` naming the counts: a partial union
would be the very truncation this endpoint exists to remove.

Cost bound. This gateway also serves prod search and reconciliation, so no
request may hold a worker for long, whatever the polygon. Three guards, all
tunable by environment:

* ``GEOMETRY_MAX_RAW_VERTICES`` (1.5 M): checked on the WKB byte length
  (16 bytes per 2-D coordinate, so an upper bound on the count) before
  anything is parsed — over it is a 413, not a computation. (Measured: one non-topology simplify pass over a noisy
  1 M-vertex ring is ~1 s at the tolerance the size ratio implies, ~8 s at
  a fine one; a topology-preserving pass is 10× that again.)
* ``GEOMETRY_TIME_BUDGET_S`` (6 s): a per-request deadline that starts when
  the request arrives, is checked between every stage (queue, index read,
  store read, parse, union, each simplify pass, serialise) and aborts with a
  413 ``error: "geometry budget exceeded"``. A thread cannot be killed
  mid-pass, so the vertex cap is what keeps any single pass short; the
  deadline is what stops passes being queued behind it.
* ``GEOMETRY_CONCURRENCY`` (2): a semaphore in front of the worker thread. A
  burst of continent clicks queues here and is refused with 503 (and
  ``Retry-After``) once its own deadline passes, instead of filling the
  thread pool and the host's memory.

Size bound. The response is capped at ``max_bytes`` (default
``GEOMETRY_MAX_BYTES``, 1 MB; ceiling 5 MB). Compact GeoJSON at 6 decimals
costs ~22 bytes per vertex, so the cap is a vertex target. A geometry over
it is simplified CUMULATIVELY — each pass runs on the previous candidate,
never again on the original — with ``preserve_topology=False`` for the
coarse passes (same vertex counts as the topology-preserving form on a
ring, at a tenth of the cost), from a tolerance derived from the size
ratio rather than from a fixed fine fraction of the extent, then one
topology-preserving pass over the small result, repaired with
``make_valid`` if it needs it. The response says so (``simplified``,
``tolerance``). A caller may also ask for a tolerance directly. The first
tolerance is a model fitted to noisy rings; when it lands outside 55-100% of the
vertex target (a real coastline keeps far fewer vertices than it predicts), up to
``_MAX_REFINE`` bracketed retries move it into the band, a retry over the original
only if the deadline can afford it.

Licence. Geometry is source content, so this is a redistribution surface in
exactly the sense of place#269: an authority whose terms do not let WHG
re-serve its records (``redistributable: False`` in
``processing.settings.AUTHORITIES`` — kain_par, nl, chgis as of 2026-07-22)
gets a 451 naming the source and rights holder. The place's own namespace is
checked before Elasticsearch is touched; the LENDING namespace of each
borrowed geometry is checked once the index entry is read, so an og place
cannot hand out an nl polygon through its wd link. Indexing, search and
reconciliation remain permitted for every authority; handing a complete,
full-precision polygon to a browser as JSON is not any of those. The whg3
proxy applies the same determination from its registry copy, and the
refusal wins wherever the two disagree.

Contributed datasets (``whg:<dataset_id>:<src_id>``, place#319 follow-up).
The store holds their polygons too (the per-dataset tilesets are built from
it), but nothing on this host knows whether a dataset is public, embargoed,
or under a licence that permits redistribution: that is Django's data (the
``Dataset`` row, its collaborators, the registry's ``whg:<id>`` row), and it
changes without a re-index. So the DECISION stays in Django, and this
endpoint serves a ``whg:`` place only when the request carries a grant that
Django signed for exactly that place id, with the shared secret
``CRC_GATEWAY_API_KEY`` (the bearer Django already sends and this gateway
otherwise ignores). ``X-WHG-Geometry-Grant: v1.<expires>.<hmac-sha256 hex>``
over ``"geometry|<place_id>|<expires>"``; a grant lives at most
``GRANT_MAX_TTL_S``. A missing, malformed, expired or mis-signed grant, or a
gateway with no secret configured, is the same 451 as before: fail closed.
The grant covers the named dataset's own geometries only; a ``whg:`` polygon
borrowed from another dataset, or borrowed by an authority place, stays
withheld as undetermined. The Pitt firewall allow-lists the app host, so the
signature is not the only wall; it is the one that stops anyone on that
host, or a logged request, from asking for a dataset Django did not clear.

Fields are DECLARED on ``GeometryResponse``: FastAPI drops anything the
response model does not name (reference_gateway_response_models).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import time
from functools import lru_cache
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from . import spatial
from .config import ES_BACKEND, PLACES_INDEX
from .es_helpers import ES_HEADERS, es_auth

try:
    import shapely
    from shapely import wkb as _wkb
    from shapely.geometry import mapping as _mapping
    from shapely.ops import unary_union as _unary_union
    _SHAPELY_AVAILABLE = True
except Exception:  # pragma: no cover
    _SHAPELY_AVAILABLE = False

logger = logging.getLogger("gateway.geometry")

router = APIRouter(prefix="/api", tags=["Geometry"])

# Size bound on the serialised geometry (bytes of compact JSON).
GEOMETRY_MAX_BYTES_DEFAULT = int(os.getenv("GEOMETRY_MAX_BYTES", "1000000"))
GEOMETRY_MAX_BYTES_CEILING = 5_000_000
GEOMETRY_MAX_BYTES_FLOOR = 10_000
# Cost bounds (module docstring).
GEOMETRY_MAX_RAW_VERTICES = int(os.getenv("GEOMETRY_MAX_RAW_VERTICES", "1500000"))
GEOMETRY_TIME_BUDGET_S = float(os.getenv("GEOMETRY_TIME_BUDGET_S", "6"))
GEOMETRY_CONCURRENCY = int(os.getenv("GEOMETRY_CONCURRENCY", "2"))

COORD_DECIMALS = 6
# Compact JSON "[-12.345678,51.234567]," at 6 decimals — the size → vertex
# conversion the cap is applied through.
_JSON_BYTES_PER_VERTEX = 22
# WKB: 16 bytes per 2-D coordinate, plus headers — so bytes/16 bounds the
# vertex count from above before anything is parsed.
_WKB_BYTES_PER_VERTEX = 16
# Measured on noisy rings: Douglas–Peucker retains about
# extent_perimeter / (0.3 × tolerance) vertices at the reduction ratios a
# 1 MB cap implies, where the extent perimeter is 2 × (width + height) of
# the bounds — NOT ``geom.length``, which vertex noise inflates without
# bound (a 200k-vertex ring with 2% radial noise is 8,000° long). The first
# pass aims ~2× over the target so that one cheap cumulative pass lands it,
# giving close to the least-simplified fit; the loop adapts after.
_TOL_GAIN = 0.3
_AIM_OVER_TARGET = 2.0
_MAX_PASSES = 8
# Landing band for a fit, as a fraction of the vertex target (cap / 22): the
# first tolerance is retried from the original when it lands below _BAND_LOW,
# aiming at _BAND_AIM. The topology-preserving pass and the real byte count then
# move the result a few per cent, so the served size lands in 50-90% of max_bytes.
_BAND_LOW = 0.55
_BAND_AIM = 0.75
_MAX_REFINE = 6
# A retry is a pass over the original geometry: demand this many multiples of the
# first pass's duration left on the deadline before spending it.
_REFINE_TIME_FACTOR = 3.0

_ENTITY_PREFIX = "place:"

# Contributed data: served only under a Django-signed grant (module docstring).
WHG_NAMESPACE = "whg"
GRANT_HEADER = "X-WHG-Geometry-Grant"
GRANT_VERSION = "v1"
GRANT_SECRET_ENV = "CRC_GATEWAY_API_KEY"
# A grant Django mints is good for ~2 minutes; anything claiming longer than
# this is refused outright, so a leaked long-lived token cannot be minted at all.
GRANT_MAX_TTL_S = 600

_ES_SOURCE = [
    "place_id",
    "namespace",
    "geometries.geometry_index",
    "geometries.geom_class",
    "geometries.has_geom",
    "geometries.geom_ref",
    "geometries.source",
]


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

class GeometryResponse(BaseModel):
    place_id: str
    namespace: str = ""
    geometry: dict = Field(description="GeoJSON geometry: the union of the place's stored geometries")
    geometry_count: int = Field(description="Stored geometries merged into the response")
    geometry_expected: int = Field(description="Stored geometries the index entry promised (equals geometry_count: a shortfall is a 404)")
    bounds: Optional[list[float]] = Field(None, description="[west, south, east, north] of the FULL geometry")
    vertex_count: int = Field(description="Coordinate pairs in the returned geometry")
    raw_vertex_count: int = Field(description="Coordinate pairs in the stored geometry before any simplification")
    bytes: int = Field(description="Size of the compact-JSON geometry")
    max_bytes: int = Field(description="The cap this response was bounded to")
    simplified: bool = Field(description="Whether the geometry was simplified to fit, or at the caller's request")
    tolerance: Optional[float] = Field(None, description="Douglas–Peucker tolerance applied (degrees), if any")
    elapsed_ms: int = Field(description="Server time spent assembling the response")
    source: str = Field("geom-store", description="Where the geometry came from")
    dataset: Optional[str] = Field(None, description="For contributed data, the dataset the grant covered (whg:<id>)")


# ---------------------------------------------------------------------------
# Licence determination (processing.settings is the source of truth)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _authorities_by_namespace() -> dict[str, dict]:
    """``{namespace: authority dict}`` from ``processing.settings.AUTHORITIES``.

    Lazy and cached: the gateway only imports ``processing.settings`` on the
    paths that need it (``spatial.get_geom_reader`` does the same). A failure
    to import is raised, not swallowed — without the determination the
    endpoint must not serve, and the store would be unreachable through the
    same import anyway.
    """
    from processing.settings import AUTHORITIES
    return {a["namespace"]: a for a in AUTHORITIES if a.get("namespace")}


def non_redistributable_namespaces() -> frozenset[str]:
    """Namespaces whose authority says ``redistributable: False``.

    An unset flag means True (the convention ``processing.settings`` states
    and the inventory push applies), so only an explicit False withholds.
    """
    return frozenset(
        ns for ns, a in _authorities_by_namespace().items()
        if a.get("redistributable", True) is False
    )


def _undetermined_detail(namespace: str, place_id: str, lender: Optional[str] = None) -> dict:
    """451 for a namespace with no authority entry — contributed ``whg:<dataset>``
    data above all. The store holds those polygons (generate_tiles.py builds
    the per-dataset tilesets from it) and the index entry carries no licence,
    visibility or embargo, so a private, embargoed or no-derivatives dataset
    is indistinguishable here from a public CC0 one. Withheld, not guessed,
    until a per-dataset lookup exists (place#319 follow-up)."""
    what = f"geometry borrowed from {lender}" if lender else "geometry"
    return {
        "error": "source licence not determined",
        "detail": (f"'{namespace}' is not a registered authority, so this endpoint cannot "
                   f"determine the terms or visibility of its {what}; it is withheld rather "
                   f"than served on an assumption. Contributed datasets are not yet served here."),
        "id": place_id,
        "namespace": namespace,
    }


def _ungranted_detail(place_id: str, reason: str) -> dict:
    """451 for a contributed place whose request carried no grant this gateway
    can verify. Same ``error`` as the undetermined case — to a consumer it IS
    undetermined here — with ``grant`` naming why, for the Django log."""
    return {
        "error": "source licence not determined",
        "detail": ("Contributed data is served only under a grant signed by WHG for this "
                   "place, which this request did not carry (or which could not be "
                   f"verified: {reason}). The dataset's visibility and licence are "
                   "decided by WHG, not here."),
        "id": place_id,
        "namespace": WHG_NAMESPACE,
        "grant": reason,
    }


# ---------------------------------------------------------------------------
# Contributed-dataset grant (signed by Django; module docstring)
# ---------------------------------------------------------------------------

def contributed_dataset(place_id: str) -> Optional[str]:
    """``"whg:<dataset_id>"`` for a canonical contributed id
    ``whg:<dataset_id>:<src_id>``; None for anything else, including the
    pre-namespacing ``whg:<pk>`` form, which names no dataset."""
    parts = place_id.split(":", 2)
    if len(parts) != 3 or parts[0] != WHG_NAMESPACE or not parts[1].isdigit() or not parts[2]:
        return None
    return f"{WHG_NAMESPACE}:{parts[1]}"


def grant_message(place_id: str, expires: int) -> bytes:
    return f"geometry|{place_id}|{int(expires)}".encode("utf-8")


def sign_grant(place_id: str, expires: int, secret: str) -> str:
    """The grant Django mints: ``v1.<expires>.<hex>``. Here for the tests and
    as the reference the whg3 side (``api/dataset_access.py``) must match."""
    sig = hmac.new(secret.encode("utf-8"), grant_message(place_id, expires), hashlib.sha256).hexdigest()
    return f"{GRANT_VERSION}.{int(expires)}.{sig}"


def _grant_secret() -> str:
    # Read per request, not at import: the gateway's .env.local is the only
    # place it lives, and a test must be able to take it away.
    return os.getenv(GRANT_SECRET_ENV) or ""


def verify_grant(grant: Optional[str], place_id: str, now: Optional[float] = None) -> tuple[bool, str]:
    """``(ok, reason)``. Every refusal is closed: no secret on this host means
    no contributed geometry is served, whatever the header says."""
    secret = _grant_secret()
    if not secret:
        return False, "no secret configured"
    if not grant:
        return False, "missing"
    parts = grant.strip().split(".")
    if len(parts) != 3 or parts[0] != GRANT_VERSION or not parts[1].isdigit():
        return False, "malformed"
    expires = int(parts[1])
    now = time.time() if now is None else now
    if expires < now:
        return False, "expired"
    if expires - now > GRANT_MAX_TTL_S:
        return False, "too long-lived"
    expected = sign_grant(place_id, expires, secret).split(".")[2]
    if not hmac.compare_digest(expected, parts[2].lower()):
        return False, "bad signature"
    return True, "ok"


def _withheld_detail(namespace: str, place_id: str, lender: Optional[str] = None) -> dict:
    a = _authorities_by_namespace().get(namespace, {})
    name = a.get("dataset_name") or namespace
    borrowed = (f" This place's geometry is borrowed from {name}." if lender else "")
    return {
        "error": "source not redistributable",
        "detail": (f"{name} is indexed and searchable through WHG, but its terms do not "
                   f"permit WHG to redistribute its records, and geometry is source "
                   f"content.{borrowed} Obtain the data from the source under its own terms."),
        "id": place_id,
        "namespace": namespace,
        "source": {
            "name": a.get("dataset_name"),
            "rights_holder": a.get("rights_holder"),
            "source_url": a.get("source_url"),
            "license": a.get("license_spdx"),
        },
    }


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------

class Deadline:
    """Per-request budget, checked between stages (a stage cannot be interrupted)."""

    def __init__(self, budget_s: float):
        self.started = time.monotonic()
        self.until = self.started + budget_s
        self.budget_s = budget_s

    def remaining(self) -> float:
        return max(0.0, self.until - time.monotonic())

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def check(self, stage: str) -> None:
        if time.monotonic() > self.until:
            raise HTTPException(status_code=413, detail={
                "error": "geometry budget exceeded",
                "detail": (f"Assembling this geometry exceeded the {self.budget_s:g} s budget "
                           f"(at: {stage}). It is too large to serve whole; the tiles still carry it."),
                "stage": stage,
                "budget_s": self.budget_s,
            })


# ---------------------------------------------------------------------------
# Geometry assembly (runs in a worker thread — SQLite, pread, Shapely, json)
# ---------------------------------------------------------------------------

def _round_coords(coords: Any, nd: int = COORD_DECIMALS) -> Any:
    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (int, float)):
            return [round(float(c), nd) for c in coords]
        return [_round_coords(c, nd) for c in coords]
    return coords


def _vertex_count(coords: Any) -> int:
    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (int, float)):
            return 1
        return sum(_vertex_count(c) for c in coords)
    return 0


def vertex_count(gj: dict) -> int:
    """Coordinate pairs in a GeoJSON geometry, collections included."""
    if gj.get("type") == "GeometryCollection":
        return sum(vertex_count(g) for g in gj.get("geometries", []))
    return _vertex_count(gj.get("coordinates", []))


def _geojson(geom) -> dict:
    gj = _mapping(geom)
    out: dict = {"type": gj["type"]}
    if gj["type"] == "GeometryCollection":
        out["geometries"] = [_geojson(g) for g in geom.geoms]
    else:
        out["coordinates"] = _round_coords(gj["coordinates"])
    return out


def _size(gj: dict) -> int:
    return len(json.dumps(gj, separators=(",", ":")))


def _coords(geom) -> int:
    return int(shapely.get_num_coordinates(geom))


def _repair(geom):
    """A non-topology pass can self-intersect a ring; mend it, keeping only
    what is polygonal (a line sliver is not a region)."""
    if geom.is_valid:
        return geom
    fixed = shapely.make_valid(geom)
    if fixed.geom_type == "GeometryCollection":
        parts = [g for g in fixed.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        if parts:
            fixed = _unary_union(parts) if len(parts) > 1 else parts[0]
    return fixed


def _too_large(n: int, cap: int) -> HTTPException:
    return HTTPException(status_code=413, detail={
        "error": "geometry too large",
        "detail": (f"Stored geometry: about {n:,} vertices, above the {cap:,} this endpoint "
                   f"will serve whole. The tiles still carry it."),
        "vertex_count": n, "max_vertices": cap,
    })


def _next_tol(lo: Optional[tuple[float, int]], hi: Optional[tuple[float, int]], aim_n: int) -> Optional[float]:
    """Next tolerance to try for *aim_n* retained vertices.

    *lo* is the finest tolerance measured that kept too many vertices, *hi* the
    coarsest that kept too few; either may be unknown. Inside a bracket it
    interpolates in log-log space, clamped to the middle of the bracket (on a
    noisy ring the retained count falls off a cliff as the tolerance reaches the
    noise amplitude, so a bare power-law extrapolation can land on 75 vertices;
    a bracket cannot). Outside one it extrapolates with a power law of exponent
    0.7 (coastline-like rings) and takes at most a 4x step."""
    if lo and hi:
        (tl, nl), (th, nh) = lo, hi
        if th <= tl or nl <= 0 or nh <= 0 or nl <= nh:
            return None
        f = math.log(nl / aim_n) / math.log(nl / nh)
        f = min(0.85, max(0.15, f))
        return tl * (th / tl) ** f
    pt = lo or hi
    if not pt or pt[1] <= 0:
        return None
    t, n = pt
    step = (n / aim_n) ** (1.0 / 0.7)
    return t * min(4.0, max(0.25, step))


def bound_geometry(geom, tolerance: Optional[float], max_bytes: int,
                   deadline: Optional[Deadline] = None) -> tuple[dict, bool, Optional[float], int]:
    """Serialise *geom* within *max_bytes*, simplifying cumulatively if it must.

    Returns ``(geojson, simplified, tolerance, bytes)``. Raises
    ``HTTPException(413)`` if the geometry cannot be brought under the cap, or
    the deadline passes between passes.
    """
    check = deadline.check if deadline is not None else (lambda stage: None)
    target = max(64, max_bytes // _JSON_BYTES_PER_VERTEX)
    cand = geom
    tol: Optional[float] = None
    simplified = False
    n = _coords(cand)

    if tolerance:
        check("simplify")
        cand = cand.simplify(tolerance, preserve_topology=False)
        tol, simplified = tolerance, True
        n = _coords(cand)

    minx, miny, maxx, maxy = geom.bounds
    perimeter = 2 * ((maxx - minx) + (maxy - miny)) or 1e-9
    if n > target:
        # Won't fit by the byte estimate: coarse passes from a tolerance the
        # size ratio implies, each on the previous candidate.
        tol = max(tol or 0.0, perimeter / (_TOL_GAIN * _AIM_OVER_TARGET * target))
        check("simplify")
        first = cand
        t0 = time.monotonic()
        cand = cand.simplify(tol, preserve_topology=False)
        pass_s = time.monotonic() - t0
        simplified = True
        n = _coords(cand)
        # The tolerance model above is fitted to noisy rings. On a real coastline
        # (self-similar, with long smooth stretches) Douglas-Peucker keeps far
        # fewer vertices than it predicts (Russia, 98,904 raw vertices, landed at
        # 6% of a 1 MB cap). Undershooting is a lost-detail error the caller cannot
        # see, so when the first pass lands below the band, re-run from the
        # ORIGINAL at a tolerance interpolated in log-log space between the points
        # measured so far. Each retry costs a pass over the original, so it runs
        # only if the deadline can afford it with the final passes still to come.
        floor_n = int(target * _BAND_LOW)
        aim_n = int(target * _BAND_AIM)
        # lo: finest tolerance that kept too many vertices (with its candidate, the
        # source for any coarser pass); hi: coarsest that kept too few.
        lo: Optional[tuple[float, int]] = None
        hi: Optional[tuple[float, int]] = None
        lo_cand = first
        if n > target:
            lo, lo_cand = (tol, n), cand
        elif n < floor_n:
            hi = (tol, n)
        for _ in range(_MAX_REFINE):
            if floor_n <= n <= target:
                break
            tol_new = _next_tol(lo, hi, aim_n)
            if tol_new is None:
                break
            if lo_cand is first and deadline is not None \
                    and deadline.remaining() < _REFINE_TIME_FACTOR * max(pass_s, 0.01):
                break  # a pass over the original that the deadline cannot afford
            check("simplify")
            tol = tol_new
            cand = lo_cand.simplify(tol, preserve_topology=False)
            n = _coords(cand)
            if n > target:
                lo, lo_cand = (tol, n), cand
            elif n < floor_n:
                hi = (tol, n)
        for _ in range(_MAX_PASSES):
            if n <= target:
                break
            check("simplify")
            tol *= max(1.5, min(3.0, n / target))
            cand = cand.simplify(tol, preserve_topology=False)
            n = _coords(cand)

    # Final pass: topology-preserving over the (now small) candidate, then the
    # real byte test; the estimate can be off, so keep doubling if it is.
    for _ in range(_MAX_PASSES):
        check("serialise")
        final = _repair(cand.simplify(tol, preserve_topology=True)) if tol else cand
        if final.is_empty:
            break
        gj = _geojson(final)
        size = _size(gj)
        if size <= max_bytes:
            return gj, simplified, tol, size
        tol = (tol or perimeter / (_TOL_GAIN * target)) * 2
        cand = cand.simplify(tol, preserve_topology=False)
        simplified = True
    raise HTTPException(status_code=413, detail={
        "error": "geometry too large",
        "detail": f"The geometry could not be simplified under {max_bytes} bytes.",
        "bounds": [minx, miny, maxx, maxy],
        "max_bytes": max_bytes,
    })


def geom_keys_for(src: dict) -> list[tuple[str, str]]:
    """``(store key, lending namespace)`` per stored geometry of a place.

    The key is the entry's own ``geom_ref`` where the index records one — it
    is the only thing that names a BORROWED geometry (og → wd) — and the
    positional ``"<place_id>_<geometry_index>"`` otherwise. The lender is the
    key's namespace (a geom_ref always carries one); ``source`` is consulted
    only when the key does not say. ``has_geom: False`` entries are points
    carried inline as ``repr_point``; the store holds nothing for them.
    """
    pid = src.get("place_id")
    out: list[tuple[str, str]] = []
    if not pid:
        return out
    own_ns = pid.split(":", 1)[0]
    for idx, g in enumerate(src.get("geometries", []) or []):
        if not isinstance(g, dict):
            continue
        if g.get("has_geom") is False:
            continue
        key = g.get("geom_ref") or f"{pid}_{g.get('geometry_index', idx)}"
        lender = key.split(":", 1)[0] if ":" in key else (g.get("source") or own_ns)
        out.append((key, lender))
    return out


def _assemble(reader, place_id: str, keys: list[str], tolerance: Optional[float],
              max_bytes: int, deadline: Deadline) -> Optional[dict]:
    """Read, union and bound. Returns the response payload, or None when the
    index promised no stored geometry at all (a located-but-point-only place).
    Raises 404 ``geometry incomplete`` when it promised more than the store
    produced, and 413 when the geometry is over the vertex cap or the budget.
    """
    if not keys:
        return None
    raws: list[bytes] = []
    missing: list[str] = []
    total = 0
    byte_cap = GEOMETRY_MAX_RAW_VERTICES * _WKB_BYTES_PER_VERTEX
    for key in keys:
        deadline.check("store read")
        try:
            # Uncached on purpose: the reader's LRU is keyed by entry, not
            # bytes, and would fill with exactly the largest polygons.
            raw = reader.get_wkb(key, cached=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("geom-store read failed for %s: %s", key, exc)
            raw = None
        if not raw:
            missing.append(key)
            continue
        total += len(raw)
        if total > byte_cap:
            # bytes/16 bounds the vertex count from above (headers only add),
            # so a geometry refused here would also fail a post-parse count —
            # which is why there is no second check after parsing.
            raise _too_large(total // _WKB_BYTES_PER_VERTEX, GEOMETRY_MAX_RAW_VERTICES)
        raws.append(raw)
    if missing:
        logger.warning("geom-store incomplete for %s: %d of %d keys missing (%s)",
                       place_id, len(missing), len(keys), ", ".join(missing[:5]))
        raise HTTPException(status_code=404, detail={
            "error": "geometry incomplete", "id": place_id,
            "detail": (f"The index records {len(keys)} stored geometries for this place "
                       f"but the store could produce only {len(raws)}; a partial outline "
                       f"is not served."),
            "expected": len(keys), "found": len(raws),
        })

    deadline.check("parse")
    shapes = []
    for raw in raws:
        try:
            shp = _wkb.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("geom-store WKB unreadable for %s: %s", place_id, exc)
            raise HTTPException(status_code=404, detail={
                "error": "geometry incomplete", "id": place_id,
                "detail": "A stored geometry for this place could not be read.",
                "expected": len(keys), "found": len(shapes),
            })
        if shp is not None and not shp.is_empty:
            shapes.append(shp)
    if not shapes:
        return None
    raw_vertices = sum(_coords(s) for s in shapes)

    deadline.check("union")
    geom = _unary_union(shapes) if len(shapes) > 1 else shapes[0]
    minx, miny, maxx, maxy = geom.bounds
    gj, simplified, tol, size = bound_geometry(geom, tolerance, max_bytes, deadline)
    return {
        "geometry": gj,
        "geometry_count": len(shapes),
        "geometry_expected": len(keys),
        "bounds": [round(minx, COORD_DECIMALS), round(miny, COORD_DECIMALS),
                   round(maxx, COORD_DECIMALS), round(maxy, COORD_DECIMALS)],
        "vertex_count": vertex_count(gj),
        "raw_vertex_count": raw_vertices,
        "bytes": size,
        "max_bytes": max_bytes,
        "simplified": simplified,
        "tolerance": tol,
        "elapsed_ms": deadline.elapsed_ms(),
    }


# ---------------------------------------------------------------------------
# Concurrency limit (per event loop, so tests under asyncio.run() stay isolated)
# ---------------------------------------------------------------------------

_semaphores: dict[int, asyncio.Semaphore] = {}


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _semaphores.get(id(loop))
    if sem is None:
        sem = asyncio.Semaphore(GEOMETRY_CONCURRENCY)
        _semaphores[id(loop)] = sem
    return sem


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.get("/geometry/{place_id}", response_model=GeometryResponse)
async def place_geometry(
    place_id: str,
    tolerance: Optional[float] = Query(
        None, ge=0.0, le=10.0,
        description="Douglas–Peucker tolerance in degrees; omit to receive the full "
                    "geometry when it fits under max_bytes",
    ),
    max_bytes: int = Query(
        GEOMETRY_MAX_BYTES_DEFAULT, ge=GEOMETRY_MAX_BYTES_FLOOR, le=GEOMETRY_MAX_BYTES_CEILING,
        description="Response size cap on the serialised geometry",
    ),
    grant: Optional[str] = Header(
        None, alias=GRANT_HEADER,
        description="Contributed (whg:) data only: the grant WHG signed for this place id",
    ),
):
    """One place's stored geometry as GeoJSON (see the module docstring).

    Examples:
      /api/geometry/osm:r62149
      /api/geometry/ohm:r2660219?max_bytes=200000
      /api/geometry/clio:1234?tolerance=0.01
      /api/geometry/whg:1234:abc  (with ``X-WHG-Geometry-Grant`` from Django)

    404 ``error: "not found"`` (unknown place) / ``"no geometry"`` (point-only)
    / ``"geometry incomplete"`` (store short of the index); 413 ``"geometry too
    large"`` / ``"geometry budget exceeded"``; 451 withheld source, or a
    contributed place without a verifiable grant; 503 store unavailable or
    endpoint busy; 502 index failure.
    """
    deadline = Deadline(GEOMETRY_TIME_BUDGET_S)
    if not _SHAPELY_AVAILABLE:  # pragma: no cover
        raise HTTPException(status_code=503, detail={"error": "shapely unavailable"})
    pid = place_id[len(_ENTITY_PREFIX):] if place_id.startswith(_ENTITY_PREFIX) else place_id
    pid = pid.strip()
    if not pid or ":" not in pid:
        raise HTTPException(status_code=422, detail={"error": "place_id must be namespaced, e.g. osm:r62149"})
    namespace = pid.split(":", 1)[0]

    # Licence first: cheaper than ES, and a withheld source leaks nothing — not
    # even whether the id exists.
    try:
        authorities = _authorities_by_namespace()
        withheld = non_redistributable_namespaces()
    except Exception as exc:  # noqa: BLE001
        logger.error("licence determination unavailable: %s", exc)
        raise HTTPException(status_code=503, detail={"error": "licence determination unavailable"})
    granted_dataset: Optional[str] = None
    if namespace == WHG_NAMESPACE:
        # Contributed: Django decided, and says so with a grant bound to this
        # id. Verified before the index is asked, so an ungranted request
        # learns nothing — not even whether the id exists.
        granted_dataset = contributed_dataset(pid)
        ok, reason = verify_grant(grant, pid) if granted_dataset else (False, "not a canonical whg:<dataset>:<id>")
        if not ok:
            logger.info("geometry: contributed %s refused (%s)", pid, reason)
            raise HTTPException(status_code=451, detail=_ungranted_detail(pid, reason))
    elif namespace not in authorities:
        raise HTTPException(status_code=451, detail=_undetermined_detail(namespace, pid))
    elif namespace in withheld:
        raise HTTPException(status_code=451, detail=_withheld_detail(namespace, pid))

    reader = spatial.get_geom_reader()
    if reader is None:
        raise HTTPException(status_code=503, detail={"error": "geometry store unavailable"})

    sem = _semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=deadline.remaining())
    except asyncio.TimeoutError:
        logger.warning("geometry: busy, refused %s after %.1fs in queue", pid, deadline.budget_s)
        return JSONResponse(status_code=503, headers={"Retry-After": "5"}, content={"detail": {
            "error": "geometry endpoint busy",
            "detail": f"{GEOMETRY_CONCURRENCY} geometries are already being assembled; try again shortly.",
        }})
    try:
        try:
            async with httpx.AsyncClient(timeout=min(10.0, max(1.0, deadline.remaining()))) as client:
                resp = await client.post(
                    f"{ES_BACKEND}/{PLACES_INDEX}/_search",
                    json={"size": 1, "query": {"term": {"place_id": pid}}, "_source": _ES_SOURCE},
                    auth=es_auth(), headers=ES_HEADERS,
                )
                resp.raise_for_status()
                hits = resp.json().get("hits", {}).get("hits", [])
        except httpx.HTTPError as exc:
            logger.warning("ES lookup failed for %s: %s", pid, exc)
            raise HTTPException(status_code=502, detail={"error": "index unavailable"})
        if not hits:
            raise HTTPException(status_code=404, detail={"error": "not found", "id": pid})
        src = hits[0].get("_source", {})
        keyed = geom_keys_for(src)
        for key, lender in keyed:
            if lender == WHG_NAMESPACE:
                # The grant covers one dataset's own geometries. A polygon lent
                # by another contributed dataset, or to an authority place, has
                # no determination here — the same 451 as before the grant existed.
                if granted_dataset and key.startswith(granted_dataset + ":"):
                    continue
                raise HTTPException(status_code=451, detail=_undetermined_detail(lender, pid, lender=key))
            if lender not in authorities:
                raise HTTPException(status_code=451, detail=_undetermined_detail(lender, pid, lender=key))
            if lender in withheld:
                raise HTTPException(status_code=451, detail=_withheld_detail(lender, pid, lender=key))
        deadline.check("index read")
        payload = await asyncio.to_thread(
            _assemble, reader, pid, [k for k, _ in keyed], tolerance, max_bytes, deadline)
    finally:
        sem.release()
    if payload is None:
        raise HTTPException(status_code=404, detail={
            "error": "no geometry", "id": pid,
            "detail": "The index locates this place by a point only; no polygon or line is stored for it.",
        })
    return GeometryResponse(place_id=pid, namespace=src.get("namespace") or namespace,
                            dataset=granted_dataset, **payload)
