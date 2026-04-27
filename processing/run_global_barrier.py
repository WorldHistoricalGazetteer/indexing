#!/usr/bin/env python3
"""Batch 8 — Global Barrier check + run-level inventory materialisation.

Confirms every selected per-gazetteer namespace has completed the required
preprocessing stages (extract → boundary_merge → h3 → h3_merge → h3_coverage
→ ccode → ccode_merge), then writes the run-level gazetteer inventory file
that Batch 9 (toponyms + temporal extents) and Batch 11 (indexing + inventory
push) consume.

Failure modes:

* If any selected gazetteer is missing, incomplete, or has stale prerequisite
  artefacts, the script prints a per-namespace report and exits with a
  non-zero status. The inventory file is **not** written.
* If the run manifest cannot be loaded the script exits ``2``.

Usage::

    python -m processing.run_global_barrier --run-id <RUN_ID>
    python -m processing.run_global_barrier --run-id <RUN_ID> --json
    python -m processing.run_global_barrier --run-id <RUN_ID> --no-write-inventory
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from processing.settings import (
    STAGED_BASE_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_RUNS_DIR,
)
from processing.staging_orchestrator import (
    GLOBAL_BARRIER_REQUIRED_STAGES,
    check_global_barrier,
    format_global_barrier_report,
    load_run_manifest,
    materialise_gazetteer_inventory,
    resolve_run_manifest_path,
    write_gazetteer_inventory,
)


def _inventory_path_for(run_id: str) -> Path:
    return Path(STAGED_RUNS_DIR) / f"{run_id}.inventory.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Batch 8 global barrier check for a run manifest"
    )
    parser.add_argument("--run-id", help="Run ID (default: latest manifest in STAGED_RUNS_DIR)")
    parser.add_argument("--manifest-path", help="Explicit manifest path")
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a JSON report instead of the text table",
    )
    parser.add_argument(
        "--no-write-inventory", action="store_true",
        help="Skip writing the run-level gazetteer inventory file even on pass",
    )
    parser.add_argument(
        "--required-stages", nargs="+",
        default=list(GLOBAL_BARRIER_REQUIRED_STAGES),
        help="Override the default set of required stages",
    )
    args = parser.parse_args()

    if args.manifest_path:
        manifest_path = Path(args.manifest_path)
    else:
        resolved = resolve_run_manifest_path(
            Path(STAGED_RUNS_DIR), run_id=args.run_id
        )
        if resolved is None:
            print(
                "ERROR: no run manifest found "
                f"({'run_id=' + args.run_id if args.run_id else 'looking for latest'})",
                file=sys.stderr,
            )
            return 2
        manifest_path = resolved

    if not manifest_path.exists():
        print(f"ERROR: run manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    try:
        manifest = load_run_manifest(manifest_path)
    except Exception as exc:
        print(f"ERROR: failed to load manifest {manifest_path}: {exc}", file=sys.stderr)
        return 2

    is_complete, report = check_global_barrier(
        manifest, required_stages=tuple(args.required_stages)
    )

    if args.json:
        payload = {
            "run_id": manifest.get("run_id"),
            "manifest_path": str(manifest_path),
            "is_complete": is_complete,
            "report": report,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Run manifest: {manifest_path}")
        print(format_global_barrier_report(is_complete, report))

    if not is_complete:
        return 1

    if not args.no_write_inventory:
        run_id = manifest.get("run_id")
        if not run_id:
            print(
                "ERROR: manifest is missing run_id; cannot write inventory file",
                file=sys.stderr,
            )
            return 2
        inventory = materialise_gazetteer_inventory(
            manifest, staged_base_dir=Path(STAGED_BASE_DIR)
        )
        out_path = _inventory_path_for(run_id)
        write_gazetteer_inventory(out_path, inventory)
        print(f"\nGazetteer inventory written: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
