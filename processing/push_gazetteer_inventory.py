#!/usr/bin/env python3
"""Batch 11 — Build the gazetteer inventory and push it to Django.

Inputs:

* ``staged/runs/{run_id}.inventory.json`` (Batch 8) — per-namespace stage
  status and on-disk artefact paths.
* ``staged/_aggregates/{ns}.h3_coverage.json`` (Batch 6) — compacted H3 cell
  set, or ``"global"`` sentinel for global gazetteers.
* ``staged/_aggregates/{ns}.temporal_extent.json`` (Batch 9) —
  ``[min(start_year), max(end_year)]`` (each may be ``null``).
* ``processing.settings.AUTHORITIES`` — ``dataset_name`` / ``citation`` plus the
  structured attribution keys (``citation_text``, ``license_spdx``,
  ``license_url``, ``rights_holder``, ``source_url``, ``contributors``).

Output payload (one entry per per-gazetteer namespace; LOC excluded —
relations-only). The Phase 4 attribution keys are OPTIONAL — only those set in
``AUTHORITIES`` are sent (the endpoint leaves omitted fields untouched, and
skips+logs an unknown ``license_spdx`` rather than failing)::

    {
      "id": "gn",
      "name": "GeoNames",
      "description": "GeoNames geographical database. https://www.geonames.org/",
      "namespace": "gn",
      "class": "authority",
      "owner_user_id": null,
      "record_count": 13000000,
      "status": "published",
      "h3_coverage": "global",
      "temporal_extent": [-2000, 2025],
      "citation_text": "GeoNames geographical database, Unxos GmbH.",
      "license_spdx": "CC-BY-4.0",
      "license_url": "https://creativecommons.org/licenses/by/4.0/",
      "rights_holder": "Unxos GmbH",
      "source_url": "https://www.geonames.org/"
    }

Push semantics:

* Idempotent upsert. The Django endpoint contract is TBD; this module
  defaults to ``POST {WHG_INVENTORY_ENDPOINT}`` with the full list as one
  body. Override via ``--endpoint`` or ``WHG_INVENTORY_ENDPOINT`` env var.
* Retries with exponential backoff on 5xx and connection errors.
* ``--dry-run`` prints the payload to stdout instead of sending it.
* Gating: this CLI refuses to push unless the run inventory file (Batch 8
  output) exists, every selected per-gazetteer namespace has a
  ``temporal_extent`` aggregate, and (when ``--require-hardlink-marker``)
  the Batch 12 ship-to-Pitt completion marker is present.

Usage::

    python -m processing.push_gazetteer_inventory --run-id <RUN_ID> --dry-run
    python -m processing.push_gazetteer_inventory --run-id <RUN_ID> \
        --endpoint https://whgazetteer.org/api/registry/inventory \
        --auth-token-file ~/.whg/inventory.token
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error as urlerror, request as urlrequest

from processing.settings import (
    AUTHORITIES,
    STAGED_BASE_DIR,
    STAGED_RUNS_DIR,
    WHG_API_TOKEN,
    WHG_API_TOKEN_FILE,
    WHG_DEV_API_TOKEN,
    WHG_DEV_API_TOKEN_FILE,
    WHG_DEV_INVENTORY_ENDPOINT,
    WHG_HTTP_INITIAL_BACKOFF,
    WHG_HTTP_MAX_RETRIES,
    WHG_HTTP_TIMEOUT,
    WHG_INVENTORY_ENDPOINT,
)
from processing.staging_contract import (
    AGGREGATE_H3_COVERAGE_FILENAME_TEMPLATE,
    AGGREGATE_TEMPORAL_EXTENT_FILENAME_TEMPLATE,
    GLOBAL_COVERAGE_NAMESPACES,
    H3_COVERAGE_GLOBAL_SENTINEL,
    is_relations_only,
)

def is_endpoint_reachable(url: str, *, timeout: float = 5.0) -> bool:
    """Quick preflight: is the host answering HTTP at all?

    Returns True for ANY HTTP response (200/4xx all count — the server is
    up and reachable). Returns False on connection-refused, DNS failure,
    timeout, or 5xx. Used by the dev-mirror push so a transiently-down
    dev server doesn't fail the whole pipeline.
    """
    req = urlrequest.Request(url, method="HEAD")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.getcode() < 500
    except urlerror.HTTPError as exc:
        # Server answered with an HTTP error — it's UP, just unhappy.
        # 5xx counts as down; 4xx (auth/method-not-allowed) counts as up.
        return exc.code < 500
    except Exception:
        # Connection refused, DNS error, timeout — server is down/unreachable.
        return False


def _read_token(path: str | Path | None) -> str | None:
    """Best-effort token file read; returns ``None`` on missing/empty/unreadable.

    Tolerates ``PermissionError`` (the secrets dir is often mode-restricted, so
    even ``exists()`` can raise EACCES) — the caller falls back to the env token
    or the shared prod token.
    """
    if not path:
        return None
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return None
        txt = p.read_text(encoding="utf-8").strip()
        return txt or None
    except OSError:
        return None


# Default Django endpoint — settings-derived but env-overridable.
_DEFAULT_ENDPOINT = WHG_INVENTORY_ENDPOINT

# Marker dropped by submit_hardlinks_slurm after a successful ship-to-Pitt.
_HARDLINK_SHIP_MARKER_TEMPLATE = "{runs_dir}/{run_id}.hardlink_ship.json"


# ---------------------------------------------------------------------------
# Authority metadata lookup
# ---------------------------------------------------------------------------


def _authority_meta(namespace: str) -> dict[str, Any]:
    """Look up name + citation + structured attribution for a namespace.

    ``description`` stays = the legacy free-text ``citation`` blob (back-compat /
    prose); the structured Phase 4 keys (``citation_text``, ``license_spdx``, …)
    are surfaced separately. Returns sensible defaults when the authority isn't
    registered (e.g. a namespace ingested via an out-of-tree script).
    """
    for auth in AUTHORITIES:
        if auth.get("namespace") == namespace:
            return {
                "name": auth.get("dataset_name") or namespace.upper(),
                # description = a prose blurb if provided, else the legacy citation
                # blob (back-compat). The structured citation is `citation_text`.
                "description": auth.get("description") or auth.get("citation"),
                "citation_text": auth.get("citation_text"),
                "license_spdx": auth.get("license_spdx"),
                "license_url": auth.get("license_url"),
                "rights_holder": auth.get("rights_holder"),
                "source_url": auth.get("source_url"),
                "image": auth.get("image"),
                "contributors": auth.get("contributors") or [],
            }
    return {"name": namespace.upper(), "description": None}


def _last_modified(namespace: str) -> str | None:
    """Best-effort last-modified (ISO date) for the gazetteer = the mtime of the
    namespace's temporal_extent aggregate, written when it was last ingested."""
    p = _temporal_extent_path(namespace)
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).date().isoformat()
    except (OSError, ValueError):
        return None


def _attribution_fields(meta: dict[str, Any]) -> dict[str, Any]:
    """Return only the attribution + standardized-rendering keys that are set.

    The WHG endpoint leaves omitted fields untouched (it never clobbers an
    existing registry value with a null), so we must NOT send empty keys —
    only forward what ``AUTHORITIES`` actually defines. Unknown ``license_spdx``
    is skipped+logged WHG-side, so sending a not-yet-seeded SPDX is harmless.
    """
    out: dict[str, Any] = {}
    for k in ("citation_text", "license_spdx", "license_url",
              "rights_holder", "source_url", "image"):
        if meta.get(k):
            out[k] = meta[k]
    if meta.get("contributors"):
        out["contributors"] = meta["contributors"]
    return out


# ---------------------------------------------------------------------------
# Aggregate readers
# ---------------------------------------------------------------------------


def _h3_coverage_path(namespace: str) -> Path:
    return Path(STAGED_BASE_DIR) / "_aggregates" / (
        AGGREGATE_H3_COVERAGE_FILENAME_TEMPLATE.format(namespace=namespace)
    )


def _temporal_extent_path(namespace: str) -> Path:
    return Path(STAGED_BASE_DIR) / "_aggregates" / (
        AGGREGATE_TEMPORAL_EXTENT_FILENAME_TEMPLATE.format(namespace=namespace)
    )


def _read_h3_coverage(namespace: str) -> Any:
    """Return the H3 coverage payload — either a sentinel string or cell list."""
    if namespace in GLOBAL_COVERAGE_NAMESPACES:
        # Always return the sentinel for global namespaces, even if the file
        # is missing — the Batch 6 compactor would have written it as such.
        path = _h3_coverage_path(namespace)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload.get("coverage", H3_COVERAGE_GLOBAL_SENTINEL)
        return H3_COVERAGE_GLOBAL_SENTINEL

    path = _h3_coverage_path(namespace)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    coverage = payload.get("coverage")
    if coverage == H3_COVERAGE_GLOBAL_SENTINEL:
        return H3_COVERAGE_GLOBAL_SENTINEL
    return coverage if isinstance(coverage, list) else []


def _read_temporal_extent(namespace: str) -> tuple[int | None, int | None]:
    path = _temporal_extent_path(namespace)
    if not path.exists():
        return (None, None)
    payload = json.loads(path.read_text(encoding="utf-8"))
    extent = payload.get("temporal_extent") or [None, None]
    return (extent[0], extent[1])


def _read_record_count(namespace: str) -> int:
    """Pull record_count from the temporal_extent aggregate (recorded there).

    Fallback to 0 when the aggregate is missing.
    """
    path = _temporal_extent_path(namespace)
    if not path.exists():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    rc = payload.get("record_count")
    return int(rc) if isinstance(rc, int) else 0


# ---------------------------------------------------------------------------
# Inventory builder
# ---------------------------------------------------------------------------


_WHG_NAMESPACE = "whg"
_WHG_DATASETS_SIDECAR = "whg.datasets.json"


def _whg_datasets_sidecar() -> Path:
    return Path(STAGED_BASE_DIR) / "_aggregates" / _WHG_DATASETS_SIDECAR


def _whg_per_dataset_h3() -> dict[str, list[str]]:
    """Per-dataset H3 coverage for whg, from the staged h3 patch.

    The namespace-level aggregate is shared by every dataset, which both
    bloats the inventory payload ~48× (a 413 at the registry) and is
    spatially wrong — each dataset would claim the whole whg footprint,
    defeating the registry's spatial filter. Instead, group the
    ``h3_cover`` / ``h3_centroid`` cells of ``staged/whg/h3/places.h3.jsonl``
    by the dataset prefix of each ``place_id`` (``whg:<dataset>:<entity>``)
    and compact per dataset. Returns ``{"whg:<dataset>": [cells…]}``.
    """
    import h3 as _h3
    # Coarsen every cell to res 3 (~69 km hexes): the registry uses coverage
    # only for a "does this gazetteer have data near here" spatial filter, so
    # fine resolution is wasted bytes. A globally-spread dataset (e.g. GEBCO
    # undersea names) would otherwise carry ~234k cells (~4 MB) and trip the
    # per-request body-size limit even alone.
    _COARSE_RES = 3
    patch = Path(STAGED_BASE_DIR) / _WHG_NAMESPACE / "h3" / "places.h3.jsonl"
    if not patch.exists():
        return {}
    by_ds: dict[str, set[str]] = {}
    with patch.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            parts = (doc.get("place_id") or "").split(":")
            if len(parts) < 3:
                continue
            ds_id = f"{parts[0]}:{parts[1]}"  # whg:<dataset>
            coarse = by_ds.setdefault(ds_id, set())
            for g in doc.get("geometries") or []:
                for c in (g.get("h3_cover") or []):
                    _add_coarse(c, coarse, _COARSE_RES, _h3)
                cen = g.get("h3_centroid")
                if cen:
                    _add_coarse(cen, coarse, _COARSE_RES, _h3)
    return {ds: sorted(cells) for ds, cells in by_ds.items()}


def _add_coarse(cell: str, out: set[str], res: int, _h3) -> None:
    """Add ``cell`` (or its res-``res`` parent if finer) to ``out``."""
    try:
        r = _h3.get_resolution(cell)
        out.add(cell if r <= res else _h3.cell_to_parent(cell, res))
    except Exception:
        pass


def _expand_whg_dataset_entries(
    h3_coverage: Any,
    temporal_extent: list[int | None],
    per_dataset_h3: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Read the Batch 4c Phase 4 sidecar (written by ``whg-places.py``) and
    fan out one inventory entry per WHG Dataset/Collection.

    Per-dataset metadata (``id``, ``name``, ``description``,
    ``owner_user_id``, ``dataset_status``, ``record_count``) comes from the
    sidecar. ``h3_coverage`` is the dataset's own footprint when
    ``per_dataset_h3`` supplies it (see :func:`_whg_per_dataset_h3`),
    otherwise the shared namespace aggregate; ``temporal_extent`` is shared.
    """
    sidecar = _whg_datasets_sidecar()
    if not sidecar.exists():
        return []
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    per_ds = per_dataset_h3 or {}
    out: list[dict[str, Any]] = []
    for ds in payload.get("datasets") or []:
        if not isinstance(ds, dict) or "id" not in ds:
            continue
        if per_ds:
            # Per-dataset footprints available: a dataset absent from the map
            # has no renderable geometry, so its coverage is empty — NOT the
            # shared namespace aggregate (that would wrongly claim global
            # extent and re-bloat the payload).
            coverage = per_ds.get(ds["id"], [])
        else:
            # No per-dataset data at all (patch missing) — fall back to the
            # shared namespace aggregate so the push still surfaces something.
            coverage = h3_coverage
        out.append({
            "id": ds["id"],                                 # e.g. "whg:1234"
            "name": ds.get("name") or ds["id"],
            "description": ds.get("description"),
            "namespace": _WHG_NAMESPACE,
            "class": "dataset",
            "owner_user_id": ds.get("owner_user_id"),
            "record_count": int(ds.get("record_count") or 0),
            "status": str(ds.get("dataset_status") or "pending"),
            "h3_coverage": coverage,
            "temporal_extent": temporal_extent,
        })
    return out


def build_inventory_payload(
    run_inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the per-namespace inventory entries from the Batch 8 inventory file.

    Most namespaces produce one entry (``class='authority'``). The ``whg``
    namespace is fanned out into one entry per Dataset/Collection from the
    sidecar written by ``authorities/whg-places.py``; if the sidecar is
    missing the bulk ``whg`` entry is emitted instead so the push still
    surfaces *something* on first runs.
    """
    entries: list[dict[str, Any]] = []
    for ns_entry in run_inventory.get("per_gazetteer", []):
        ns = ns_entry["namespace"]
        if is_relations_only(ns):
            continue  # Defensive: barrier already excludes these.
        meta = _authority_meta(ns)
        start, end = _read_temporal_extent(ns)
        h3 = _read_h3_coverage(ns)

        if ns == _WHG_NAMESPACE:
            fanned = _expand_whg_dataset_entries(h3, [start, end])
            if fanned:
                entries.extend(fanned)
                continue
            # Fall through to a bulk 'whg' entry if no sidecar exists yet.

        entry = {
            "id": ns,
            "name": meta["name"],
            "description": meta["description"],
            "namespace": ns,
            "class": "dataset" if ns == _WHG_NAMESPACE else "authority",
            "owner_user_id": None,
            "record_count": _read_record_count(ns),
            "status": "published",
            "h3_coverage": h3,
            "temporal_extent": [start, end],
        }
        entry.update(_attribution_fields(meta))
        lm = _last_modified(ns)
        if lm:
            entry["last_modified"] = lm
        entries.append(entry)
    return entries


def build_single_authority_entry(namespace: str) -> dict[str, Any]:
    """Build one ``class='authority'`` inventory entry from the per-namespace
    aggregates — for an incremental single-namespace push (no full-run inventory).

    Requires the namespace's ``h3_coverage`` + ``temporal_extent`` aggregates to
    exist (generate them with ``processing.gazetteer_h3_coverage`` and
    ``processing.gazetteer_temporal_extent`` from the staged ``final`` snapshot).
    """
    if is_relations_only(namespace):
        raise RuntimeError(f"{namespace} is relations-only — it has no inventory entry")
    if namespace not in GLOBAL_COVERAGE_NAMESPACES and not _h3_coverage_path(namespace).exists():
        raise RuntimeError(
            f"Missing h3_coverage aggregate for {namespace}: {_h3_coverage_path(namespace)}. "
            f"Run `python -m processing.gazetteer_h3_coverage --run-id <RUN_ID> --namespace {namespace}`."
        )
    if not _temporal_extent_path(namespace).exists():
        raise RuntimeError(
            f"Missing temporal_extent aggregate for {namespace}: {_temporal_extent_path(namespace)}. "
            f"Run `python -m processing.gazetteer_temporal_extent --run-id <RUN_ID> --namespace {namespace}`."
        )
    meta = _authority_meta(namespace)
    start, end = _read_temporal_extent(namespace)
    entry = {
        "id": namespace,
        "name": meta["name"],
        "description": meta["description"],
        "namespace": namespace,
        "class": "authority",
        "owner_user_id": None,
        "record_count": _read_record_count(namespace),
        "status": "published",
        "h3_coverage": _read_h3_coverage(namespace),
        "temporal_extent": list(_read_temporal_extent(namespace)),
    }
    entry.update(_attribution_fields(meta))
    lm = _last_modified(namespace)
    if lm:
        entry["last_modified"] = lm
    return entry


def assert_tilesets_served(payload: list[dict[str, Any]], *, skip: bool = False) -> None:
    """Hard ordering gate (the contract: confirm serving BEFORE the manifest push).

    For every ``class='authority'`` entry — each of which has a per-namespace
    tileset ``<ns>.mbtiles`` — confirm the tileserver actually serves
    ``/data/<ns>.json``; refuse the push otherwise. The inventory push is a manual
    final step (not Slurm-scheduled), so this runtime precondition is the
    enforceable form of "tilesets served before Django is told the gazetteer
    exists". Reuses the SSH localhost-curl verifier (works from CRC compute / pitt).
    """
    namespaces = list(dict.fromkeys(
        e["namespace"] for e in payload if e.get("class") == "authority" and e.get("namespace")
    ))
    if not namespaces:
        return
    if skip:
        print(f"WARNING: --skip-tileserver-check set; NOT confirming tilesets serve "
              f"for {', '.join(namespaces)} before the manifest push.", file=sys.stderr)
        return
    from processing.update_tileserver_config import verify_serving
    print(f"Preflight: confirming tilesets serve for {', '.join(namespaces)} ...")
    results = verify_serving(namespaces)
    missing = [n for n, ok in results.items() if not ok]
    if missing:
        raise RuntimeError(
            f"Refusing inventory push — tileset(s) not served: {', '.join(missing)}. "
            f"Push tiles and run `python -m processing.update_tileserver_config "
            f"--bucket {' --bucket '.join(missing)} --execute` first "
            f"(or pass --skip-tileserver-check to override)."
        )


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def _hardlink_marker_path(run_id: str) -> Path:
    return Path(_HARDLINK_SHIP_MARKER_TEMPLATE.format(
        runs_dir=STAGED_RUNS_DIR, run_id=run_id
    ))


def assert_ready_to_push(
    run_id: str,
    *,
    require_hardlink_marker: bool,
) -> Path:
    """Validate inputs and return the run inventory path.

    Raises ``RuntimeError`` if the inventory file is missing, any selected
    per-gazetteer namespace lacks its temporal_extent aggregate, or (when
    requested) the Batch 12 ship-to-Pitt marker is absent.
    """
    inventory_path = Path(STAGED_RUNS_DIR) / f"{run_id}.inventory.json"
    if not inventory_path.exists():
        raise RuntimeError(
            f"Run inventory not found: {inventory_path}. "
            "Run `processing.run_global_barrier --run-id <RUN_ID>` first."
        )
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    missing: list[str] = []
    for ns_entry in inventory.get("per_gazetteer", []):
        ns = ns_entry["namespace"]
        if is_relations_only(ns):
            continue
        if not _temporal_extent_path(ns).exists():
            missing.append(ns)
    if missing:
        raise RuntimeError(
            f"Missing temporal_extent aggregate for: {', '.join(missing)}. "
            "Run Batch 9 (`processing.submit_batch9_slurm`) before pushing."
        )
    if require_hardlink_marker and not _hardlink_marker_path(run_id).exists():
        raise RuntimeError(
            f"Batch 12 ship-to-Pitt marker not found at "
            f"{_hardlink_marker_path(run_id)}; refusing to push inventory "
            "(Master Plan: gate the push on hard-link DB swap)."
        )
    return inventory_path


# ---------------------------------------------------------------------------
# HTTP push
# ---------------------------------------------------------------------------


def _batch_by_cells(
    payload: list[dict[str, Any]], *, max_cells: int = 20000
) -> list[list[dict[str, Any]]]:
    """Split the inventory payload into batches bounded by total h3 cells, so no
    single HTTP request exceeds the registry's body-size limit (nginx default
    ~1 MB). The whg per-dataset fan-out can total hundreds of thousands of
    cells across all datasets. A single row bigger than the budget still goes
    out on its own (the endpoint must accept at least one full row)."""
    batches: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    cur_cells = 0
    for entry in payload:
        cov = entry.get("h3_coverage")
        n = len(cov) if isinstance(cov, list) else 0
        if cur and cur_cells + n > max_cells:
            batches.append(cur)
            cur, cur_cells = [], 0
        cur.append(entry)
        cur_cells += n
    if cur:
        batches.append(cur)
    return batches


def push_inventory(
    payload: list[dict[str, Any]],
    *,
    endpoint: str,
    auth_token: str | None = None,
    method: str = "POST",
    timeout: int = WHG_HTTP_TIMEOUT,
    max_retries: int = WHG_HTTP_MAX_RETRIES,
    initial_backoff: float = WHG_HTTP_INITIAL_BACKOFF,
) -> tuple[int, str]:
    """POST/PUT the payload, retrying on 5xx + connection errors.

    Returns ``(status_code, response_body)`` from the last successful HTTP
    call. Raises ``RuntimeError`` if all attempts fail.
    """
    body = json.dumps({"gazetteers": payload}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    # An API POST should never legitimately redirect — a 3xx (e.g. Django
    # bouncing an unauthenticated request to /admin/login) silently
    # masquerades as success when urllib follows the redirect to a 200
    # login page. Build an opener with no redirect handler so 3xx
    # surfaces as HTTPError and the caller sees the real failure.
    class _NoRedirect(urlrequest.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # urllib treats this as "give up redirecting"
    opener = urlrequest.build_opener(_NoRedirect())

    last_err: str | None = None
    backoff = initial_backoff
    for attempt in range(1, max_retries + 1):
        try:
            req = urlrequest.Request(
                endpoint, data=body, headers=headers, method=method,
            )
            with opener.open(req, timeout=timeout) as resp:
                status = resp.getcode()
                if status >= 300:
                    # urllib didn't raise on 3xx because we suppressed the
                    # redirect; do it ourselves so the caller's normal
                    # error path runs.
                    raise urlerror.HTTPError(
                        endpoint, status,
                        f"Unexpected redirect ({status}) — endpoint may "
                        f"be requiring session auth instead of bearer token",
                        resp.headers, None,
                    )
                resp_body = resp.read().decode("utf-8", errors="replace")
                return status, resp_body
        except urlerror.HTTPError as exc:
            last_err = f"HTTP {exc.code}: {exc.reason}"
            if exc.code < 500 or attempt == max_retries:
                raise RuntimeError(
                    f"Inventory push failed (HTTP {exc.code}): {exc.reason}"
                ) from exc
        except (urlerror.URLError, TimeoutError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt == max_retries:
                raise RuntimeError(
                    f"Inventory push failed after {max_retries} attempts: {last_err}"
                ) from exc
        time.sleep(backoff)
        backoff *= 2

    raise RuntimeError(f"Inventory push failed: {last_err}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and push the WHG gazetteer inventory to Django"
    )
    parser.add_argument("--run-id", help="Run ID (required for a full-run push)")
    parser.add_argument("--namespace",
                        help="Push a single authority namespace incrementally "
                             "(no full-run inventory needed; builds the entry from "
                             "that namespace's aggregates). Mutually exclusive with a "
                             "full-run push.")
    parser.add_argument("--skip-tileserver-check", action="store_true",
                        help="Override the preflight that refuses to push unless each "
                             "authority's tileset is served (not recommended)")
    parser.add_argument("--endpoint", default=_DEFAULT_ENDPOINT,
                        help="Django registry endpoint URL")
    parser.add_argument("--method", default="POST", choices=("POST", "PUT"))
    parser.add_argument("--auth-token-file",
                        default=WHG_API_TOKEN_FILE,
                        help="File containing the bearer token "
                             "(default: WHG_API_TOKEN_FILE / settings)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the payload instead of pushing")
    parser.add_argument("--require-hardlink-marker", action="store_true",
                        help="Refuse to push unless the Batch 12 ship-to-Pitt "
                             "marker exists for this run_id")
    parser.add_argument("--timeout", type=int, default=WHG_HTTP_TIMEOUT)
    parser.add_argument("--dev-endpoint", default=WHG_DEV_INVENTORY_ENDPOINT,
                        help="Dev-server registry endpoint URL "
                             "(mirrors the prod push; pass empty string to disable)")
    parser.add_argument("--dev-auth-token-file",
                        default=WHG_DEV_API_TOKEN_FILE,
                        help="File containing the dev-server bearer token "
                             "(falls back to --auth-token-file if missing)")
    parser.add_argument("--no-dev-push", action="store_true",
                        help="Skip the dev-server mirror push entirely")
    args = parser.parse_args()

    if bool(args.namespace) == bool(args.run_id):
        print("ERROR: pass exactly one of --namespace (single incremental push) "
              "or --run-id (full-run push).", file=sys.stderr)
        sys.exit(1)

    try:
        if args.namespace:
            # Incremental single-namespace push — build the entry from that
            # namespace's aggregates; no full-run inventory / barrier needed.
            bulk = build_single_authority_entry(args.namespace)
            if args.namespace == _WHG_NAMESPACE:
                # whg fans out into one registry row per contributed dataset
                # (sidecar written by whg-places.py). Each row carries its own
                # per-dataset h3 footprint (not the shared namespace aggregate)
                # so the registry's spatial filter is accurate and the payload
                # stays small. temporal_extent is shared. Mirrors the full-run
                # path (build_inventory_payload).
                fanned = _expand_whg_dataset_entries(
                    bulk["h3_coverage"], bulk["temporal_extent"],
                    per_dataset_h3=_whg_per_dataset_h3(),
                )
                payload = fanned or [bulk]
            else:
                payload = [bulk]
        else:
            inventory_path = assert_ready_to_push(
                args.run_id,
                require_hardlink_marker=args.require_hardlink_marker,
            )
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            payload = build_inventory_payload(inventory)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(json.dumps({"endpoint": args.endpoint,
                          "method": args.method,
                          "gazetteers": payload},
                         indent=2, sort_keys=True))
        return

    # Hard ordering gate: tilesets must be served before Django is told the
    # gazetteer exists.
    try:
        assert_tilesets_served(payload, skip=args.skip_tileserver_check)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # Token: prefer the environment (WHG_API_TOKEN in .env.local) over the file.
    auth_token = WHG_API_TOKEN or _read_token(args.auth_token_file)
    dev_token = WHG_DEV_API_TOKEN or _read_token(args.dev_auth_token_file) or auth_token

    # Both stacks are first-class push targets. The prod ↔ dev registries
    # MUST stay in sync because the Atlas cutover in prod is gradual, so a
    # one-sided update would drift them (as happened on 2026-05-04, when a
    # dev-only follow-up added an osm_misc row prod never received). We push
    # the same payload to every target and exit non-zero if ANY target
    # fails; the push is an idempotent upsert keyed by id, so re-running
    # after a transient failure simply re-syncs. Use --no-dev-push (or an
    # empty --dev-endpoint) to deliberately target prod only.
    targets: list[tuple[str, str, str | None]] = [
        ("prod", args.endpoint, auth_token),
    ]
    if not args.no_dev_push and args.dev_endpoint:
        targets.append(("dev", args.dev_endpoint, dev_token))

    # Chunk so no single request trips the registry's body-size limit (413).
    # The upsert is idempotent per id, so a mid-run failure re-syncs on re-run.
    batches = _batch_by_cells(payload)

    failures: list[str] = []
    for label, endpoint, token in targets:
        # An unreachable target is a failure, not a silent skip — keeping
        # the two registries in sync is the whole point.
        if not is_endpoint_reachable(endpoint):
            print(f"{label} {endpoint} → unreachable", file=sys.stderr)
            failures.append(f"{label}: unreachable")
            continue
        pushed = 0
        for i, batch in enumerate(batches, 1):
            try:
                status, body = push_inventory(
                    batch,
                    endpoint=endpoint,
                    method=args.method,
                    auth_token=token,
                    timeout=args.timeout,
                )
                pushed += len(batch)
                print(f"{label} {endpoint} [batch {i}/{len(batches)}, "
                      f"{len(batch)} rows] → HTTP {status}")
                if body:
                    print(body[:400])
            except RuntimeError as exc:
                print(f"{label} {endpoint} [batch {i}/{len(batches)}] "
                      f"→ FAILED: {exc}", file=sys.stderr)
                failures.append(f"{label} (batch {i}, {pushed}/{len(payload)} "
                                f"rows pushed): {exc}")
                break

    if failures:
        print(
            f"ERROR: inventory push incomplete — {len(failures)} of "
            f"{len(targets)} target(s) failed; registries may be out of sync. "
            f"Re-run to resync (push is idempotent). "
            f"Failures: {'; '.join(failures)}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
