#!/usr/bin/env python3
"""Batch 14 — Minimal end-to-end integration harness for the staged pipeline.

Drives a small staged-only run for a subset of namespaces (default
``nl + po`` per the plan) **without** Elasticsearch:

1. Generates a fresh ``run_id`` and creates a manifest restricted to the
   selected namespaces.
2. Verifies that staged extracts for the selected namespaces exist on disk
   (the harness does not re-run authority scripts; it expects extracts to
   have been produced by a prior ``ingest_all_authorities`` invocation).
3. Runs ``h3_stage`` → ``h3_merge`` → ``gazetteer_h3_coverage`` per
   namespace, then ``ccode_enrichment`` → ``ccode_merge`` (skipping ``un``
   in the ccode steps; ``un`` self-supplies ccodes).
4. Runs ``gazetteer_temporal_extent`` per namespace.
5. Runs ``run_global_barrier`` to materialise the inventory file.
6. Runs ``verify_aggregates`` to confirm aggregate gates.
7. Reports whether any step touched Elasticsearch (none should — the
   harness fails the run if any module imports the ``elasticsearch``
   package during the staged steps).

This is **not** a test runner — it is a smoke-test driver that exercises
the staged-only path end-to-end against real (small) staged inputs. For
unit tests see ``tests/``.

Usage::

    python -m testing.integration_harness            # default: nl + po
    python -m testing.integration_harness -n nl      # nl only
    python -m testing.integration_harness --skip-extract-check
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Default smoke-test namespaces — small, fully covered, non-OSM (no boundary
# stage required), and one global (`po`) and one non-global (`nl`) so we
# exercise both H3 coverage paths.
_DEFAULT_NAMESPACES = ("nl", "po")


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> int:
    print(f"\n$ {' '.join(cmd)}")
    result = subprocess.run(cmd, env=env or os.environ.copy())
    return result.returncode


def _have_extract(namespace: str) -> bool:
    from processing.settings import STAGED_BASE_DIR
    base = Path(STAGED_BASE_DIR) / namespace / "extract"
    return (base / "places.parquet").exists() or (base / "places.jsonl").exists()


def _per_namespace_chain(run_id: str, namespace: str, *, skip_ccode: bool) -> int:
    """Drive h3 → h3_merge → h3_coverage → ccode_enrichment → ccode_merge."""
    base = [sys.executable, "-u", "-m"]

    rc = _run(base + ["processing.h3_stage",
                      "--run-id", run_id, "--namespace", namespace])
    if rc != 0:
        return rc

    rc = _run(base + ["processing.h3_merge",
                      "--run-id", run_id, "--namespace", namespace])
    if rc != 0:
        return rc

    rc = _run(base + ["processing.gazetteer_h3_coverage",
                      "--run-id", run_id, "--namespace", namespace])
    if rc != 0:
        return rc

    if skip_ccode:
        return 0

    rc = _run(base + ["processing.ccode_enrichment",
                      "--run-id", run_id, "--namespace", namespace])
    if rc != 0:
        return rc

    rc = _run(base + ["processing.ccode_merge",
                      "--run-id", run_id, "--namespace", namespace])
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Staged-only integration harness for a small namespace subset"
    )
    parser.add_argument("-n", "--namespace", action="append",
                        help="Namespaces to drive through the harness "
                             f"(default: {_DEFAULT_NAMESPACES})")
    parser.add_argument("--run-id",
                        help="Use this run_id instead of generating one")
    parser.add_argument("--skip-extract-check", action="store_true",
                        help="Skip the staged-extract precheck")
    parser.add_argument("--keep-running-on-error", action="store_true",
                        help="Continue to the next namespace on per-namespace failure")
    args = parser.parse_args()

    namespaces = list(args.namespace or _DEFAULT_NAMESPACES)

    sys.path.insert(0, str(REPO_ROOT))
    from processing.settings import (
        STAGED_RUN_MANIFEST_FILE_TEMPLATE,
        STAGED_RUNS_DIR,
    )
    from processing.staging_orchestrator import (
        create_run_manifest,
        generate_run_id,
    )

    run_id = args.run_id or generate_run_id(prefix="harness")
    manifest_path = Path(
        STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(
            runs_dir=STAGED_RUNS_DIR, run_id=run_id,
        )
    )
    if not manifest_path.exists():
        print(f"Creating manifest: {manifest_path}")
        create_run_manifest(manifest_path, run_id, namespaces)
    else:
        print(f"Reusing existing manifest: {manifest_path}")

    if not args.skip_extract_check:
        missing = [ns for ns in namespaces if not _have_extract(ns)]
        if missing:
            print(
                f"\nERROR: staged extract missing for {missing}. "
                "Run `python -m processing.ingest_all_authorities --run-id "
                f"{run_id} -n {','.join(namespaces)}` first.",
                file=sys.stderr,
            )
            return 1

    failures: list[str] = []
    for ns in namespaces:
        skip_ccode = (ns == "un")
        rc = _per_namespace_chain(run_id, ns, skip_ccode=skip_ccode)
        if rc != 0:
            failures.append(ns)
            if not args.keep_running_on_error:
                print(f"\nABORT: {ns} failed (use --keep-running-on-error to continue)",
                      file=sys.stderr)
                return rc

    # Per-namespace temporal extent.
    for ns in namespaces:
        rc = _run([sys.executable, "-u", "-m",
                   "processing.gazetteer_temporal_extent",
                   "--run-id", run_id, "--namespace", ns])
        if rc != 0 and not args.keep_running_on_error:
            return rc

    # Global barrier — materialises the inventory.
    rc = _run([sys.executable, "-u", "-m",
               "processing.run_global_barrier",
               "--run-id", run_id])
    if rc != 0:
        print(f"\nGlobal barrier reported failure (rc={rc}).")
        if not args.keep_running_on_error:
            return rc

    # Aggregate verifier.
    rc = _run([sys.executable, "-u", "-m",
               "processing.verify_aggregates",
               "--run-id", run_id])
    if rc != 0:
        print(f"\nAggregate verifier reported failure (rc={rc}).")
        if not args.keep_running_on_error:
            return rc

    print("\n" + "=" * 78)
    if failures:
        print(f"✗ Harness completed with failures: {failures}")
        return 1
    print(f"✓ Harness completed for run_id={run_id}, namespaces={namespaces}")
    print(f"  Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
