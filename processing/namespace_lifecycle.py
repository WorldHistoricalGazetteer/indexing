#!/usr/bin/env python3
"""Batch 11 — Selection-driven namespace + index lifecycle helpers.

Three concerns live here:

1. **Dated index naming + alias swap** — choose ``{base}_{run_id}`` names,
   swap aliases atomically, return the previous targets so the caller can
   queue them for retention.
2. **Retention sweep** — keep the latest N dated indices per alias and drop
   older ones (excluding whatever is currently aliased).
3. **Selection-driven cleanup** — given the run manifest's
   ``selected_namespaces``, delete leftover docs from previously-indexed
   namespaces that are no longer selected. This is the single chokepoint for
   removing namespaces from the live indices; ad-hoc per-namespace deletions
   are explicitly disallowed (Master Plan Batch 11).

The alias-swap helper is a thin re-export of
``processing.index_from_stage.swap_alias`` so callers have one import surface
for the lifecycle.

Usage::

    python -m processing.namespace_lifecycle --es-host URL retention --keep 2
    python -m processing.namespace_lifecycle --es-host URL cleanup --run-id <RUN_ID>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch

from processing.index_from_stage import (
    PLACES_ALIAS,
    index_name_for_run,
    swap_alias,
)
from processing.settings import (
    ES_HOST,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.staging_contract import is_relations_only
from processing.staging_orchestrator import load_run_manifest


# Index families managed here. Keep this in sync with the gateway's
# discovery patterns (``places_*``, ``toponyms_*``).
_MANAGED_FAMILIES: dict[str, str] = {
    "places": PLACES_ALIAS,
    "toponyms": "toponyms",
}


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _list_family_indices(es: Elasticsearch, base: str) -> list[str]:
    """List dated indices for a family, sorted newest-first.

    Sorting is lexicographic on the suffix; our run IDs / date suffixes are
    monotone-increasing so lex order = chronological order.
    """
    try:
        names = list(es.indices.get(index=f"{base}_*"))
    except Exception:
        return []
    return sorted(names, reverse=True)


def _alias_target(es: Elasticsearch, alias: str) -> str | None:
    try:
        return next(iter(es.indices.get_alias(name=alias)))
    except Exception:
        return None


def retention_sweep(
    es: Elasticsearch,
    *,
    families: dict[str, str] = _MANAGED_FAMILIES,
    keep: int = 2,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """For each managed family, delete dated indices older than the latest ``keep``.

    Always preserves the index that currently holds the alias (even if it
    falls outside the keep window). Returns ``{family: [deleted indices]}``.
    """
    deleted: dict[str, list[str]] = {}
    for base, alias in families.items():
        indices = _list_family_indices(es, base)
        if len(indices) <= keep:
            deleted[base] = []
            continue
        live = _alias_target(es, alias)
        retained = set(indices[:keep])
        if live:
            retained.add(live)
        to_delete = [name for name in indices[keep:] if name not in retained]
        if dry_run:
            deleted[base] = to_delete
            continue
        for name in to_delete:
            es.indices.delete(index=name)
        deleted[base] = to_delete
    return deleted


# ---------------------------------------------------------------------------
# Selection-driven cleanup
# ---------------------------------------------------------------------------


def selected_namespaces_from_manifest(manifest_path: Path) -> list[str]:
    manifest = load_run_manifest(manifest_path)
    return [
        ns for ns in manifest.get("selected_namespaces", [])
        if not is_relations_only(ns)
    ]


def delete_deselected_from_alias(
    es: Elasticsearch,
    *,
    alias: str,
    selected_namespaces: list[str],
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete any place doc in ``alias`` whose namespace is not in the selected set.

    Per Master Plan Batch 11: gazetteer removal happens through the selection
    file, not ad-hoc operations. This helper enforces that boundary by
    cleaning up the live index after a run-mode change shrinks the selected
    set.

    Returns ``{namespace: deleted_count}`` for every namespace that lost
    documents.
    """
    selected = set(selected_namespaces)
    # Discover currently-present namespaces via a terms aggregation on the
    # ``namespace`` field. Schemas keep namespace as a top-level keyword.
    aggs = {
        "namespaces": {
            "terms": {"field": "namespace", "size": 200}
        }
    }
    try:
        resp = es.search(index=alias, size=0, aggs=aggs)
    except Exception:
        return {}
    buckets = resp.get("aggregations", {}).get("namespaces", {}).get("buckets", [])
    present = {b["key"]: int(b["doc_count"]) for b in buckets}

    deselected = {ns: count for ns, count in present.items() if ns not in selected}
    if dry_run or not deselected:
        return deselected

    deleted: dict[str, int] = {}
    for ns, _ in deselected.items():
        body = {"query": {"prefix": {"place_id": f"{ns}:"}}}
        resp = es.options(request_timeout=3600).delete_by_query(
            index=alias,
            body=body,
            conflicts="proceed",
            refresh=True,
            slices="auto",
            wait_for_completion=True,
        )
        deleted[ns] = int(resp.get("deleted", 0))
    return deleted


# ---------------------------------------------------------------------------
# Alias swap (re-exported convenience)
# ---------------------------------------------------------------------------


def promote_index(
    es: Elasticsearch,
    *,
    run_id: str,
    family: str = "places",
    alias: str | None = None,
) -> list[str]:
    """Swap the alias for ``family`` to point at this run's dated index.

    Returns the list of previous alias targets (queue these for retention if
    desired).
    """
    alias = alias or _MANAGED_FAMILIES.get(family, family)
    if family == "places":
        new_index = index_name_for_run(run_id)
    else:
        new_index = f"{family}_{run_id}"
    return swap_alias(es, new_index=new_index, alias_name=alias)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _open_es(host: str) -> Elasticsearch:
    es = Elasticsearch(host, request_timeout=120, max_retries=3)
    if not es.ping():
        print(f"ERROR: cannot connect to ES at {host}", file=sys.stderr)
        sys.exit(1)
    return es


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Selection-driven namespace + index lifecycle (Batch 11)"
    )
    parser.add_argument("--es-host", default=ES_HOST)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ret = sub.add_parser("retention", help="Drop dated indices older than the latest N")
    p_ret.add_argument("--keep", type=int, default=2)
    p_ret.add_argument("--dry-run", action="store_true")

    p_clean = sub.add_parser("cleanup", help="Delete docs in deselected namespaces from the live alias")
    p_clean.add_argument("--run-id", required=True)
    p_clean.add_argument("--manifest-path")
    p_clean.add_argument("--alias", default=PLACES_ALIAS)
    p_clean.add_argument("--dry-run", action="store_true")

    p_swap = sub.add_parser("swap", help="Swap an alias to this run's dated index")
    p_swap.add_argument("--run-id", required=True)
    p_swap.add_argument("--family", default="places",
                        choices=sorted(_MANAGED_FAMILIES))

    args = parser.parse_args()

    es = _open_es(args.es_host)

    if args.cmd == "retention":
        result = retention_sweep(es, keep=args.keep, dry_run=args.dry_run)
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.cmd == "cleanup":
        if args.manifest_path:
            manifest_path = Path(args.manifest_path)
        else:
            manifest_path = Path(
                STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
                    runs_dir=STAGED_RUNS_DIR, run_id=args.run_id
                )
            )
        if not manifest_path.exists():
            print(f"Run manifest not found: {manifest_path}", file=sys.stderr)
            sys.exit(1)
        selected = selected_namespaces_from_manifest(manifest_path)
        result = delete_deselected_from_alias(
            es, alias=args.alias, selected_namespaces=selected, dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.cmd == "swap":
        previous = promote_index(es, run_id=args.run_id, family=args.family)
        print(json.dumps({"previous_targets": previous}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
