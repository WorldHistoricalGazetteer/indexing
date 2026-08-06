#!/usr/bin/env python
"""Publish a harvested hard-link overlay over shared storage, and mark it done.

The ship-to-Pitt path in ``submit_hardlinks_slurm`` rsyncs over ssh, which
cannot work from a CRC compute node: the Pitt VM is firewalled from them on
**both** 9200 and 22 (verified 6 Aug 2026 — ``curl`` exit 28, ``ssh`` connect
timeout). It is also unnecessary — ``/ix1`` is mounted on the compute nodes and
on the VM, and ``PITT_HARDLINK_DIR`` is where the gateway reads its batch
overlay — so publication is a rename within one filesystem.

Three steps, in the same order and with the same semantics as the ssh path:

1. ``finalise_local`` — WAL checkpoint + OPTIMIZE, returning the row count.
2. ``publish_local`` — atomic same-filesystem replace, previous kept as
   ``.previous``.
3. ``prune_live_delta_local`` — drop live-delta rows asserted at or before the
   harvest-start cutoff, since they are now folded into the published overlay.
   Rows asserted *during* the build survive. **Best-effort**: a prune failure
   must never block the completion marker, because the publish already
   succeeded and the live delta simply carries to the next run.

Then write ``staged/runs/{run_id}.hardlink_ship.json``, which
``push_gazetteer_inventory --require-hardlink-marker`` checks.

Exists as its own entry point so the submitter and an operator recovering a
completed harvest run **the same code** — a harvest is ~40 minutes and should
not have to be repeated just to publish it.

Usage::

    python -m processing.publish_hardlinks --run-id <RUN_ID> \\
        --db-path /ix1/ishi/hardlinks/hard_links_<RUN_ID>.sqlite \\
        --cutoff 2026-08-06T18:23:55.168502+00:00 [--execute]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clustering.sqlite_overlay import (
    finalise_local,
    prune_live_delta_local,
    publish_local,
)
from processing.settings import (
    IX1_BASE,
    IX3_BASE,
    PITT_HARDLINK_DIR,
    PITT_HARDLINK_FILENAME,
    STAGED_RUNS_DIR,
)

DEFAULT_LIVE_DB = f"{IX3_BASE}/hardlinks/hard_links_live.sqlite"


def publish(
    *,
    run_id: str,
    db_path: Path,
    target_dir: str = PITT_HARDLINK_DIR,
    target_filename: str = PITT_HARDLINK_FILENAME,
    live_db: str = DEFAULT_LIVE_DB,
    cutoff_iso: str | None = None,
    skip_prune: bool = False,
    marker_path: Path | None = None,
    execute: bool = False,
) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"harvested overlay not found: {db_path}")

    marker = marker_path or Path(STAGED_RUNS_DIR) / f"{run_id}.hardlink_ship.json"
    target = Path(target_dir) / target_filename

    if not execute:
        return {
            "dry_run": True,
            "would_publish": str(db_path),
            "to": str(target),
            "would_prune": None if skip_prune else live_db,
            "cutoff": cutoff_iso,
            "marker": str(marker),
        }

    row_count = finalise_local(db_path)
    result = publish_local(local_db=db_path, target_dir=target_dir,
                           target_filename=target_filename)
    result["row_count"] = row_count
    result["run_id"] = run_id

    if skip_prune or not cutoff_iso:
        result["prune"] = {"skipped": True, "cutoff": cutoff_iso}
    else:
        # Best-effort, exactly as in the ssh path: the publish has already
        # happened, so a prune failure must not fail the job.
        try:
            result["prune"] = prune_live_delta_local(
                live_db_path=live_db, cutoff_iso=cutoff_iso)
        except Exception as exc:  # noqa: BLE001 - deliberately swallowed
            result["prune"] = {"error": str(exc), "cutoff": cutoff_iso}
            print(f"WARNING: live-delta prune failed: {exc}", file=sys.stderr)

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(result, indent=2, sort_keys=True))
    result["marker_path"] = str(marker)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--db-path", help="Harvested overlay (default: "
                                      "$IX1/hardlinks/hard_links_<run_id>.sqlite)")
    ap.add_argument("--target-dir", default=PITT_HARDLINK_DIR)
    ap.add_argument("--target-filename", default=PITT_HARDLINK_FILENAME)
    ap.add_argument("--live-db", default=DEFAULT_LIVE_DB)
    ap.add_argument("--cutoff", help="Harvest-start ISO timestamp for the "
                                     "live-delta prune")
    ap.add_argument("--skip-prune", action="store_true")
    ap.add_argument("--marker-path")
    ap.add_argument("--execute", action="store_true",
                    help="Publish (default: report only)")
    args = ap.parse_args()

    db_path = (Path(args.db_path) if args.db_path
               else Path(IX1_BASE) / "hardlinks" / f"hard_links_{args.run_id}.sqlite")

    result = publish(
        run_id=args.run_id,
        db_path=db_path,
        target_dir=args.target_dir,
        target_filename=args.target_filename,
        live_db=args.live_db,
        cutoff_iso=args.cutoff,
        skip_prune=args.skip_prune,
        marker_path=Path(args.marker_path) if args.marker_path else None,
        execute=args.execute,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
