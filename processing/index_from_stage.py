#!/usr/bin/env python3
"""Batch 11 — Load ``places`` exclusively from final enriched staged snapshots.

Streams per-namespace ``staged/{ns}/final/places.parquet`` (or the most-enriched
fallback) into a fresh dated ES index named ``places_<run_id>``. Once every
selected per-gazetteer namespace has loaded, the alias ``places`` is swapped to
the new index and the previous targets are queued for retention sweep
(``processing/namespace_lifecycle.py``).

Per Master Plan E.2 #3, every loaded doc carries top-level ``dataset_status``
and ``dataset_id``; both fields are present in the staged corpus already
(populated by ``processing/helpers.write_staged_place_doc``).

Per-namespace stage status (`index`) is updated in the run manifest so resumes
skip namespaces that already loaded successfully.

Toponym indexing is handled by ``phonetics/inference/update_es.py index`` which
already reads from the staged DuckDB + Symphonym embeddings Parquet — see
Batch 11 plan for the full sequencing.

Usage::

    python -m processing.index_from_stage --run-id <RUN_ID> --es-host <URL>
    python -m processing.index_from_stage --run-id <RUN_ID> --namespace osm --es-host <URL>
    python -m processing.index_from_stage --run-id <RUN_ID> --skip-alias-swap
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import pyarrow.parquet as pq
from elasticsearch import Elasticsearch, helpers

from processing.settings import (
    ES_HOST,
    is_production_host,
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.index_freshness import (
    check_namespace,
    source_fingerprint,
    stale_namespaces,
)
from processing.stage_writers import (
    record_script_wall_time,
    write_runtime_history_event,
    write_stage_event,
)
from processing.staging_contract import is_relations_only
from processing.staged_parquet import drop_nulls_for_parquet, strip_hull
from processing.staging_orchestrator import (
    load_run_manifest,
    stage_status_with_fallback,
    update_namespace_stage_status,
)


# Stage chain preferred for the index source. ``final/`` carries the merged
# ccode patches, but earlier stages are accepted when ccode enrichment is
# legitimately skipped (e.g. ``un`` provides ccodes itself).
_STAGED_SOURCE_PRIORITY = (
    "final",
    "h3_merged",
    "boundary_merged",
    "update_merged",
    "extract",
)

PLACES_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "places.json"
PLACES_PIPELINE_NAME = "extract_namespace"
PLACES_ALIAS = "places"

# Same literal the other production-facing tools use (apply_ccode_patch,
# apply_update_patch, promote_to_production).
DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"


def _staged_namespace_source(namespace: str) -> Path | None:
    base = Path(STAGED_BASE_DIR) / namespace
    for stage in _STAGED_SOURCE_PRIORITY:
        parquet = base / stage / "places.parquet"
        if parquet.exists():
            return parquet
        jsonl = base / stage / "places.jsonl"
        if jsonl.exists():
            return jsonl
    return None


def _iter_staged_docs(path: Path, batch_size: int = 2000) -> Iterator[dict[str, Any]]:
    if path.suffix == ".parquet":
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=batch_size):
            for row in batch.to_pylist():
                if isinstance(row, dict):
                    # ⚠ PARQUET ONLY, and deliberately not the JSONL branch below.
                    # A parquet struct column has ONE schema for the whole file,
                    # so reading it back materialises every key any row used, as
                    # an explicit null on the rows that lacked it. A timespan
                    # written `{"start": {"latest": 2026}}` returns
                    # `{"start": {"in": None, "latest": 2026}}` if any other doc
                    # in the same file carried `in` — which is why the null
                    # pattern in the live index is a fingerprint of what SHARED
                    # a staged file rather than of the record itself: osm and nl
                    # are clean, tgn carries `in: null`, wd carries all three,
                    # ohm carries a wholly null `end`.
                    #
                    # `drop_nulls_for_parquet` is applied on the WRITE side
                    # already; this is the missing half. The JSONL branch is
                    # left alone on purpose — `normalize_for_parquet` puts
                    # deliberate `None`s there (empty lists) and stripping them
                    # would be a different change with no reported defect.
                    yield drop_nulls_for_parquet(strip_hull(row))
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield strip_hull(json.loads(line))


def _count_staged_docs(path: Path) -> int:
    if path.suffix == ".parquet":
        return pq.ParquetFile(path).metadata.num_rows
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def index_name_for_run(run_id: str) -> str:
    """Return the ES index name for a run.

    Uses the canonical ``places_<run_id>`` shape so the gateway's
    ``places_*`` pattern picks it up. ES rejects uppercase in index
    names, so the run_id is lowercased here (run_ids commonly carry an
    ISO timestamp like ``...T130000Z``).
    """
    return f"places_{run_id}".lower()


# ---------------------------------------------------------------------------
# Index lifecycle
# ---------------------------------------------------------------------------


def ensure_index(
    es: Elasticsearch,
    index_name: str,
    *,
    schema_path: Path = PLACES_SCHEMA_PATH,
    pipeline: str | None = PLACES_PIPELINE_NAME,
    refresh_disabled: bool = True,
) -> None:
    """Create the dated index if it doesn't already exist.

    Pre-existing indices are left untouched so resumes append rather than
    overwrite. Refresh is disabled by default during bulk loading; the caller
    re-enables it after the load finishes.
    """
    if es.indices.exists(index=index_name):
        return

    with open(schema_path) as fh:
        schema = json.load(fh)

    if pipeline:
        schema.setdefault("settings", {})["default_pipeline"] = pipeline
    if refresh_disabled:
        schema.setdefault("settings", {})["refresh_interval"] = "-1"

    es.indices.create(index=index_name, body=schema)


def finalize_index(es: Elasticsearch, index_name: str) -> int:
    """Re-enable refresh, force a refresh, and return the doc count."""
    es.indices.put_settings(
        index=index_name, body={"index": {"refresh_interval": "1s"}}
    )
    es.indices.refresh(index=index_name)
    return int(es.count(index=index_name)["count"])


def swap_alias(
    es: Elasticsearch,
    *,
    new_index: str,
    alias_name: str = PLACES_ALIAS,
) -> list[str]:
    """Atomically swap ``alias_name`` to point at ``new_index`` only.

    Returns the list of indices that previously held the alias (so the caller
    can hand them to the retention sweep).
    """
    actions: list[dict[str, Any]] = []
    previous: list[str] = []
    try:
        current = es.indices.get_alias(name=alias_name)
        for old in current:
            previous.append(old)
            actions.append({"remove": {"index": old, "alias": alias_name}})
    except Exception:
        pass  # Alias doesn't yet exist.
    # If a CONCRETE index exists with the same name as the alias (e.g. the
    # empty placeholder created by staging --no-snapshot), atomically drop
    # it in the same _aliases call so the add doesn't fail with
    # invalid_alias_name_exception. Use ``exists_alias`` to distinguish
    # — ``indices.exists(index=name)`` is True for both concrete indices
    # AND aliases with the same name, so checking only that would try to
    # remove_index an alias and fail with illegal_argument_exception.
    if (
        es.indices.exists(index=alias_name)
        and not es.indices.exists_alias(name=alias_name)
        and alias_name not in previous
    ):
        actions.append({"remove_index": {"index": alias_name}})
    actions.append({"add": {"index": new_index, "alias": alias_name}})
    es.indices.update_aliases(body={"actions": actions})
    return previous


# ---------------------------------------------------------------------------
# Bulk load
# ---------------------------------------------------------------------------


def _action_stream(
    docs: Iterable[dict[str, Any]],
    *,
    index_name: str,
) -> Iterator[dict[str, Any]]:
    for doc in docs:
        place_id = doc.get("place_id")
        if not place_id:
            continue
        # Trust the staged doc as-is; downstream ES ingest pipeline (places_pipeline)
        # extracts namespace from place_id and applies any default fields.
        yield {
            "_op_type": "index",
            "_index": index_name,
            "_id": place_id,
            "_source": doc,
        }


def load_namespace(
    es: Elasticsearch,
    *,
    namespace: str,
    index_name: str,
    batch_size: int = 1000,
    chunk_size: int = 1000,
    request_timeout: int = 300,
) -> dict[str, Any]:
    """Bulk-load a single namespace's staged final snapshot into ``index_name``.

    Returns metrics dict. Raises ``FileNotFoundError`` if no staged snapshot
    exists for the namespace.
    """
    src = _staged_namespace_source(namespace)
    if src is None:
        raise FileNotFoundError(
            f"No staged snapshot found for namespace '{namespace}'. "
            "Run extract / boundary_merge / h3_merge / ccode_merge first."
        )

    # Fingerprint the artefact actually read, so a later run can tell whether
    # the index it produced has since been superseded. Doc counts cannot: a
    # re-run of update_merge adds names to existing places and leaves the place
    # count identical, which is how gn shipped missing 26.7M alternate names
    # while every count matched (see processing/index_freshness.py).
    fingerprint = source_fingerprint(src)
    total = _count_staged_docs(src)
    started = time.monotonic()
    indexed = 0
    errors = 0
    error_samples: list[str] = []

    actions = _action_stream(_iter_staged_docs(src, batch_size=batch_size),
                             index_name=index_name)

    for ok, item in helpers.streaming_bulk(
        es.options(request_timeout=request_timeout),
        actions,
        chunk_size=chunk_size,
        raise_on_error=False,
        max_retries=2,
        initial_backoff=2,
    ):
        if ok:
            indexed += 1
        else:
            errors += 1
            if len(error_samples) < 5:
                error_samples.append(json.dumps(item)[:500])

    return {
        "namespace": namespace,
        "source_path": str(src),
        "source_fingerprint": fingerprint,
        "docs_in_source": total,
        "docs_indexed": indexed,
        "errors": errors,
        "error_samples": error_samples,
        "wall_seconds": round(time.monotonic() - started, 1),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _eligible_namespaces(manifest: dict, only: list[str] | None = None) -> list[str]:
    """Return per-gazetteer namespaces ready for indexing.

    A namespace is eligible when its ``ccode_merge`` stage is ``completed``
    or ``skipped`` (i.e. it has a final snapshot), its ``aat_enrich`` stage is
    ``completed`` or ``skipped`` (so the indexed docs carry folded AAT — this is
    the hard gate that prevents indexing un-enriched namespaces; aat_enrich is a
    Batch 9 stage), and its ``index`` stage is not yet ``completed``. ``un`` is
    treated like the rest because its final snapshot is the extract —
    ccode_merge is legitimately skipped there.
    """
    out: list[str] = []
    for ns in manifest.get("selected_namespaces", []):
        if is_relations_only(ns):
            continue
        if only and ns not in only:
            continue
        # Use the event-log fallback so a stale manifest (e.g. a fresh
        # run_id whose own manifest never observed an earlier completion)
        # doesn't drop a namespace whose final/places.parquet is on disk.
        ccode = stage_status_with_fallback(manifest, ns, "ccode_merge")
        if ccode not in ("completed", "skipped"):
            continue
        aat = stage_status_with_fallback(manifest, ns, "aat_enrich")
        if aat not in ("completed", "skipped"):
            continue
        if stage_status_with_fallback(manifest, ns, "index") == "completed":
            # "completed" records what was true when it was written. If the
            # staged source has been rewritten since (a re-run of update_merge,
            # a repair), the namespace needs indexing again — so a stale
            # completion must not skip it, or the pipeline resumes into
            # precisely the state the freshness check exists to catch. gn, wd
            # and nl sat at index:completed for two days holding pre-merge data.
            if not check_namespace(ns, manifest)["stale"]:
                continue
            print(f"  {ns}: index marked completed but its staged source has "
                  f"changed since — re-indexing")
        out.append(ns)
    return out


def _report_collapsed_ids(
    es: Elasticsearch,
    index_name: str,
    per_namespace: dict[str, dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """Compare staged rows against resulting documents, per namespace.

    A shortfall means duplicate ``place_id``s in the staged snapshot: the later
    row overwrote the earlier one and both were reported as indexed. Returns
    ``{ns: {"staged": n, "documents": m, "lost": n - m}}`` for the namespaces
    that lost rows.

    This does not fail the load. A duplicate place_id is a defect in the
    *source* extract (an authority script emitting one row per geometry instead
    of accumulating them into one document), not in the indexing, and it is
    routinely pre-existing. Making it loud is the point.
    """
    collapses: dict[str, dict[str, int]] = {}
    for ns, metrics in sorted(per_namespace.items()):
        staged = metrics.get("docs_in_source")
        if not staged:
            continue
        try:
            docs = int(es.count(index=index_name,
                                query={"term": {"namespace": ns}})["count"])
        except Exception as exc:
            print(f"  ! {ns}: could not verify document count ({exc})")
            continue
        if docs < staged:
            lost = staged - docs
            collapses[ns] = {"staged": staged, "documents": docs, "lost": lost}
            print(f"  ! {ns}: {staged:,} staged rows produced only {docs:,} "
                  f"documents — {lost:,} lost to duplicate place_ids "
                  f"(the extract emits multiple rows per place)")
        metrics["documents_in_index"] = docs
    if collapses:
        total = sum(c["lost"] for c in collapses.values())
        print(f"  ! {total:,} staged rows across {len(collapses)} namespace(s) "
              f"did not become documents. This is a source-extract defect, not "
              f"an indexing failure; it does not block the load.")
    return collapses


def run_index_from_stage(
    *,
    run_id: str,
    es: Elasticsearch,
    manifest_path: Path | None = None,
    namespaces: list[str] | None = None,
    swap_alias_on_complete: bool = True,
    request_timeout: int = 300,
    chunk_size: int = 1000,
) -> dict[str, Any]:
    """Run the full Batch 11 places-load for ``run_id``.

    When ``manifest_path`` is provided, eligibility and stage status are
    reconciled against the manifest. ``namespaces`` further restricts the set.
    The alias swap runs only when *every* eligible namespace has loaded
    successfully in this invocation (or ``swap_alias_on_complete=False`` is
    passed for staged rollouts).
    """
    manifest = load_run_manifest(manifest_path) if manifest_path else None
    if manifest is not None:
        eligible = _eligible_namespaces(manifest, only=namespaces)
    else:
        if not namespaces:
            raise ValueError(
                "Must pass either --manifest-path (for eligibility) or --namespaces explicitly"
            )
        eligible = list(namespaces)

    if not eligible:
        print("No namespaces eligible for indexing.")
        return {"index_name": None, "namespaces": [], "alias_swapped": False}

    index_name = index_name_for_run(run_id)
    print(f"Target index: {index_name}")
    print(f"Namespaces ({len(eligible)}): {', '.join(eligible)}")

    ensure_index(es, index_name)

    per_namespace: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    for ns in eligible:
        print(f"\n--- Indexing {ns} ---")
        ns_started = datetime.now(timezone.utc)
        if manifest_path is not None:
            update_namespace_stage_status(manifest_path, ns, "index", "running")
        write_stage_event(
            run_id=run_id,
            namespace=ns,
            script_id="index-from-stage",
            status="running",
            stage="index",
        )
        write_runtime_history_event(
            run_id=run_id, event="index", status="running", namespace=ns, stage="index",
        )
        try:
            metrics = load_namespace(
                es,
                namespace=ns,
                index_name=index_name,
                chunk_size=chunk_size,
                request_timeout=request_timeout,
            )
        except Exception as exc:
            failures.append(ns)
            err = f"{type(exc).__name__}: {exc}"
            print(f"  ✗ {ns}: {err}")
            if manifest_path is not None:
                update_namespace_stage_status(
                    manifest_path, ns, "index", "failed", error=err
                )
            write_stage_event(
                run_id=run_id, namespace=ns, script_id="index-from-stage",
                status="failed", stage="index", error=err,
            )
            continue

        per_namespace[ns] = metrics
        print(
            f"  ✓ {ns}: {metrics['docs_indexed']:,}/{metrics['docs_in_source']:,} "
            f"docs ({metrics['errors']:,} errors, {metrics['wall_seconds']}s)"
        )
        if manifest_path is not None:
            update_namespace_stage_status(
                manifest_path, ns, "index", "completed", metrics=metrics
            )
        write_stage_event(
            run_id=run_id, namespace=ns, script_id="index-from-stage",
            status="completed", stage="index", metrics=metrics,
        )
        ns_finished = datetime.now(timezone.utc)
        try:
            record_script_wall_time(
                namespace=ns,
                script_id="index-from-stage",
                run_id=run_id,
                started_at=ns_started.isoformat(),
                finished_at=ns_finished.isoformat(),
                wall_seconds=(ns_finished - ns_started).total_seconds(),
                status="completed",
                slurm_job_id=os.getenv("SLURM_JOB_ID"),
                extra={"docs_indexed": metrics["docs_indexed"]},
            )
        except Exception:
            pass
        write_runtime_history_event(
            run_id=run_id, event="index", status="completed", namespace=ns,
            stage="index", details=metrics,
        )

    final_count = finalize_index(es, index_name)
    print(f"\nFinalized {index_name}: {final_count:,} docs")

    # Bulk writes key on place_id as _id, so two staged rows sharing a place_id
    # produce two SUCCESSFUL writes and one document — the second silently
    # overwrites the first. docs_indexed counts writes, not documents, so a
    # namespace can report a clean load while losing rows. chgis does exactly
    # this: 82,117 staged rows, 127 place_ids duplicated across 825 rows, all
    # differing in `geometries`, leaving 81,292 documents. Comparing the
    # resulting index against the staged row count is the only way to see it.
    collapses = _report_collapsed_ids(es, index_name, per_namespace)

    # Refuse to publish an index whose sources moved underneath it. This is
    # the last point at which a stale build is still private; past the swap it
    # is what users search.
    stale = stale_namespaces(eligible, manifest)
    if stale:
        print(f"\nSTALE: {', '.join(stale)} — the staged source changed after "
              f"these were indexed. Re-index them before swapping the alias:\n"
              f"  python -m processing.index_from_stage --run-id {run_id} "
              f"{' '.join('--namespace ' + n for n in stale)} --skip-alias-swap")

    swapped = False
    previous: list[str] = []
    if swap_alias_on_complete and not failures and not stale:
        previous = swap_alias(es, new_index=index_name)
        swapped = True
        print(f"Alias '{PLACES_ALIAS}' → {index_name} (replaced: {previous or '∅'})")

    return {
        "index_name": index_name,
        "namespaces_loaded": list(per_namespace),
        "namespaces_failed": failures,
        "per_namespace_metrics": per_namespace,
        "doc_count": final_count,
        "collapsed_place_ids": collapses,
        "alias_swapped": swapped,
        "previous_alias_targets": previous,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch 11 — load places into ES from staged final snapshots"
    )
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--manifest-path", help="Explicit manifest path")
    parser.add_argument("--namespace", action="append",
                        help="Restrict to namespace(s); pass multiple times")
    parser.add_argument("--es-host", default=ES_HOST, help="Elasticsearch URL")
    parser.add_argument("--chunk-size", type=int, default=1000,
                        help="ES bulk request chunk size")
    parser.add_argument("--request-timeout", type=int, default=300)
    parser.add_argument("--skip-alias-swap", action="store_true",
                        help="Do not swap the 'places' alias on completion (staged rollout)")
    parser.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE,
                        help="File holding the `elastic` password. Staging runs "
                             "without security; production requires it.")
    parser.add_argument("--allow-production", action="store_true",
                        help="Permit --es-host to address production (normally refused; "
                             "a full rebuild belongs in staging, promoted by snapshot)")
    args = parser.parse_args()

    # A full rebuild is built in a disposable staging ES and promoted with
    # processing.promote_to_production (snapshot both indices -> restore ->
    # atomic alias swap). Building straight into production would leave a
    # partially loaded index reachable under the live alias for hours, and it
    # is an easy accident: ES_HOST is unset on the VM, so --es-host defaults to
    # None and a well-meaning "http://localhost:9201" gets typed in.
    if not args.es_host:
        print("No Elasticsearch host resolved. Start staging ES "
              "(`source es.sh -staging-start`) or pass --es-host.", file=sys.stderr)
        sys.exit(2)
    if is_production_host(args.es_host) and not args.allow_production:
        print(f"Refusing to build into production ({args.es_host}).\n"
              "  A full rebuild goes into staging, then:\n"
              "    python -m processing.promote_to_production --run-id <RUN_ID> --execute\n"
              "  Pass --allow-production only if you genuinely mean to write live.",
              file=sys.stderr)
        sys.exit(2)

    if args.manifest_path:
        manifest_path = Path(args.manifest_path)
    else:
        manifest_path = Path(
            STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
                runs_dir=STAGED_RUNS_DIR, run_id=args.run_id
            )
        )
    if not manifest_path.exists() and not args.namespace:
        print(f"Run manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    # Staging runs without security, so this client was built unauthenticated —
    # which meant --allow-production could never actually work: production
    # requires basic auth, so ping() failed with a bare "cannot connect", which
    # reads as a network problem rather than a missing credential. Repairing a
    # single namespace in the live index is a legitimate operation (it is how
    # `un`'s stale h3_cover was fixed on 6 Aug 2026), so it needs to be possible.
    es_kwargs: dict[str, Any] = {
        "request_timeout": args.request_timeout,
        "max_retries": 3,
    }
    pw_file = Path(args.es_password_file) if args.es_password_file else None
    if pw_file and pw_file.exists():
        es_kwargs["basic_auth"] = ("elastic",
                                   pw_file.read_text(encoding="utf-8").strip())

    es = Elasticsearch(args.es_host, **es_kwargs)
    if not es.ping():
        hint = "" if "basic_auth" in es_kwargs else (
            f"\n  No credentials were sent. If this host requires auth, pass "
            f"--es-password-file (default {DEFAULT_ES_PASSWORD_FILE}).")
        print(f"ERROR: cannot connect to ES at {args.es_host}{hint}",
              file=sys.stderr)
        sys.exit(1)

    summary = run_index_from_stage(
        run_id=args.run_id,
        es=es,
        manifest_path=manifest_path if manifest_path.exists() else None,
        namespaces=args.namespace,
        swap_alias_on_complete=not args.skip_alias_swap,
        request_timeout=args.request_timeout,
        chunk_size=args.chunk_size,
    )
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
