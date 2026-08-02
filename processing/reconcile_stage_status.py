#!/usr/bin/env python
"""Reconcile a run manifest's stage status with what is actually on disk.

Usage
-----
    python -m processing.reconcile_stage_status --run-id <RUN_ID>                  # report
    python -m processing.reconcile_stage_status --run-id <RUN_ID> --namespace gn --execute

Why this exists
---------------
The manifest records what the pipeline *believed* happened. Several times during
the place#164 rebuild it believed wrong — most damagingly when a fault in
``run_ingestion``'s post-subprocess bookkeeping made it return False for eleven
namespaces that had staged every document correctly. A namespace recorded
``failed`` is skipped by the global barrier and dropped from
``index_from_stage``, so a wrong manifest quietly shrinks the corpus.

Re-running the extract to correct the record costs hours (``osm`` is a 20.6 M-doc
planet parse) and risks more than it fixes. Reconciling is the right repair —
but it has to be **evidence-based**, not a blind setter, or it just moves the
lie from the pipeline into the operator's hands.

So this only ever promotes a stage to ``completed`` when the artefact that stage
is defined to produce exists and is non-empty, and it prints the evidence
(path and record count) for every change. It will not invent a completion, and
it will not demote or otherwise touch a stage whose artefact is missing.

It deliberately does **not** touch stages whose artefacts it cannot check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from processing.settings import (
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.stage_writers import write_stage_event
from processing.staging_orchestrator import (
    load_run_manifest,
    update_namespace_checkpoint,
    update_namespace_stage_status,
)

#: stage → the artefacts that stage is defined to produce, in preference order.
#: A stage counts as done when any one of them exists and is non-empty.
STAGE_ARTEFACTS: dict[str, tuple[str, ...]] = {
    "extract": ("extract/places.parquet", "extract/places.jsonl"),
    "update_patch": ("update_patch/places.update.jsonl",),
    "update_merge": ("update_merged/places.parquet", "update_merged/places.jsonl"),
    "boundary": ("boundary/places.boundary.jsonl",),
    "boundary_merge": ("boundary_merged/places.parquet", "boundary_merged/places.jsonl"),
    "h3": ("h3/places.h3.jsonl",),
    "h3_merge": ("h3_merged/places.parquet", "h3_merged/places.jsonl"),
    "ccode": ("ccode/places.ccode.jsonl",),
    "ccode_merge": ("final/places.parquet", "final/places.jsonl"),
}


def _count(path: Path) -> int:
    """Records in a staged artefact, or 0 if it can't be read."""
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq

            return pq.ParquetFile(path).metadata.num_rows
        except Exception:
            return 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def find_artefact(namespace: str, stage: str, staged_base: Path) -> tuple[Path, int] | None:
    """First non-empty artefact for ``stage``, with its record count."""
    for rel in STAGE_ARTEFACTS.get(stage, ()):
        path = staged_base / namespace / rel
        if path.is_file():
            n = _count(path)
            if n:
                return path, n
    return None


def reconcile(
    *,
    run_id: str,
    manifest_path: Path,
    namespaces: list[str] | None,
    stages: list[str],
    scripts: dict[str, str] | None = None,
    staged_base: Path | None = None,
    execute: bool = False,
) -> int:
    """Report (and with ``execute``, apply) evidence-backed status promotions.

    Returns the number of changes made or that would be made.
    """
    staged_base = staged_base or Path(STAGED_BASE_DIR)
    manifest = load_run_manifest(manifest_path)
    targets = namespaces or sorted(manifest.get("namespaces", {}))
    changes = 0

    for ns in targets:
        entry = manifest.get("namespaces", {}).get(ns)
        if entry is None:
            print(f"  {ns}: not in this run manifest — skipped")
            continue
        for stage in stages:
            current = entry.get("stages", {}).get(stage, "pending")
            if current == "completed":
                continue
            found = find_artefact(ns, stage, staged_base)
            if found is None:
                print(f"  {ns}/{stage}: {current} — no artefact on disk, LEFT ALONE")
                continue
            path, n = found
            changes += 1
            verb = "promoting" if execute else "would promote"
            print(f"  {ns}/{stage}: {current} → completed ({verb}; "
                  f"{n:,} records in {path.relative_to(staged_base)})")
            if execute:
                update_namespace_stage_status(manifest_path, ns, stage, "completed")
                # The manifest is not the only reader. `events.jsonl` is the
                # authoritative cross-run record — `stage_status_with_fallback`
                # consults it whenever the manifest lacks a passing status, and
                # some tooling reads it directly. Reconciling only the manifest
                # leaves the two disagreeing, which is confusing at best and
                # wrong for any consumer that trusts the event log.
                write_stage_event(
                    run_id=run_id, namespace=ns,
                    script_id="reconcile-stage-status", status="completed",
                    stage=stage,
                    metrics={"reconciled_from": str(path), "records": n},
                )

        for script_id, owner_ns in (scripts or {}).items():
            if owner_ns != ns:
                continue
            done = entry.get("scripts", {}).get(script_id, {}).get("status")
            if done == "completed":
                continue
            # A script checkpoint is only meaningful alongside its stage
            # artefact, so gate it on the same evidence.
            if find_artefact(ns, "extract", staged_base) is None:
                print(f"  {ns}/{script_id}: no extract artefact, LEFT ALONE")
                continue
            changes += 1
            verb = "checkpointing" if execute else "would checkpoint"
            print(f"  {ns}/{script_id}: {done or 'pending'} → completed ({verb})")
            if execute:
                update_namespace_checkpoint(manifest_path, ns, script_id, "completed")

    return changes


def reset(
    *,
    run_id: str,
    manifest_path: Path,
    namespaces: list[str] | None,
    stages: list[str],
    execute: bool = False,
) -> int:
    """Demote stages to ``pending`` so they re-run.

    Deliberately needs no evidence, unlike promotion: demoting can only cause
    work to be repeated, never cause a wrong state to be believed. The case it
    exists for is a stage whose *output* was discarded — e.g. dropping a
    staging index built under a superseded mapping, where every namespace's
    ``index`` stage still reads ``completed`` and ``index_from_stage`` would
    skip the lot.
    """
    manifest = load_run_manifest(manifest_path)
    targets = namespaces or sorted(manifest.get("namespaces", {}))
    changes = 0
    for ns in targets:
        entry = manifest.get("namespaces", {}).get(ns)
        if entry is None:
            continue
        for stage in stages:
            current = entry.get("stages", {}).get(stage)
            if current in (None, "pending"):
                continue
            changes += 1
            verb = "resetting" if execute else "would reset"
            print(f"  {ns}/{stage}: {current} → pending ({verb})")
            if execute:
                update_namespace_stage_status(manifest_path, ns, stage, "pending")
                write_stage_event(
                    run_id=run_id, namespace=ns,
                    script_id="reconcile-stage-status", status="pending", stage=stage,
                )
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Promote manifest stages to completed where the artefact proves it"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manifest-path")
    parser.add_argument("--namespace", action="append", default=[],
                        help="Restrict to namespace(s); repeatable")
    parser.add_argument("--stage", action="append", default=[],
                        help=f"Stage(s) to reconcile (default: extract). "
                             f"Known: {', '.join(STAGE_ARTEFACTS)}")
    parser.add_argument("--script", action="append", default=[],
                        help="Also checkpoint a script as completed, as ns:script_id "
                             "(e.g. gn:gn-places). Gated on the extract artefact existing.")
    parser.add_argument("--reset", action="store_true",
                        help="Demote the named --stage(s) to pending so they re-run, "
                             "instead of promoting them to completed")
    parser.add_argument("--execute", action="store_true",
                        help="Apply the changes (default is a report only)")
    args = parser.parse_args()

    manifest_path = Path(args.manifest_path) if args.manifest_path else Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(runs_dir=STAGED_RUNS_DIR, run_id=args.run_id)
    )
    if not manifest_path.exists():
        print(f"Run manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    # Scripts-only when --script is given without an explicit --stage. That
    # combination is how you say "this script ran, but the namespace is not
    # finished" — `gn` and `wd` each have a second script (gn-toponyms,
    # wikidata-geoshapes) that must still run, and marking `extract` completed
    # early would let the global barrier pass them without it.
    stages = args.stage or ([] if args.script else ["extract"])
    # STAGE_ARTEFACTS exists so a *promotion* can be checked against the file
    # that proves it. Reset needs no such proof, and the stages most worth
    # resetting produce no staged artefact at all — `index` writes to
    # Elasticsearch, so it is absent from the table and was being rejected.
    if not args.reset:
        unknown = [s for s in stages if s not in STAGE_ARTEFACTS]
        if unknown:
            print(f"Unknown stage(s) for promotion: {', '.join(unknown)}. "
                  f"Known: {', '.join(STAGE_ARTEFACTS)}", file=sys.stderr)
            sys.exit(1)

    scripts: dict[str, str] = {}
    for spec in args.script:
        if ":" not in spec:
            print(f"--script expects ns:script_id, got {spec!r}", file=sys.stderr)
            sys.exit(1)
        ns, script_id = spec.split(":", 1)
        scripts[script_id] = ns

    print(f"Manifest: {manifest_path}")
    if args.reset:
        if not args.stage:
            print("--reset requires an explicit --stage", file=sys.stderr)
            sys.exit(2)
        changes = reset(run_id=args.run_id, manifest_path=manifest_path,
                        namespaces=args.namespace or None, stages=stages,
                        execute=args.execute)
        if not changes:
            print("Nothing to reset.")
        elif not args.execute:
            print(f"\n{changes} change(s) — re-run with --execute to apply.")
        return
    changes = reconcile(
        run_id=args.run_id,
        manifest_path=manifest_path,
        namespaces=args.namespace or None,
        stages=stages,
        scripts=scripts,
        execute=args.execute,
    )
    if not changes:
        print("Nothing to reconcile.")
    elif not args.execute:
        print(f"\n{changes} change(s) — re-run with --execute to apply.")


if __name__ == "__main__":
    main()
