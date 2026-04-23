#!/usr/bin/env python
# processing/ingest_all_authorities.py

"""
Master script to ingest all authority data sources into Elasticsearch.

This script coordinates the ingestion of all configured authorities in the
optimal order, considering dependencies and data volume.

When you specify `-n gn`, it runs BOTH geonames-places AND geonames-toponyms.
When you specify `-n gn,wd`, it runs all GeoNames scripts then all Wikidata scripts.

21 Dec 2025 Final document counts by source:

  dp              2,599
  gb          1,174,449
  gn         13,378,039
  iv             24,000
  nl              4,343
  osm        18,113,756
  pl             34,085
  tgn         2,972,410
  un                257
  wd         11,456,496

  Total:     47,160,434

Unique Toponyms: 74,417,599 indexed in 1 day, 0:45:53
"""
import os
import json
import subprocess
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone
from elasticsearch import Elasticsearch

from processing.settings import (
    ES_HOST,
    DATA_DIR,
    AUTHORITIES,
    PLACES_INDEX,
    TOPONYMS_INDEX,
    GEOSHAPE_LOG_FILE,
    OSM_STATE_FILE,
    OHM_STATE_FILE,
    AUTHORITY_SELECTION_FILE,
    STAGED_BASE_DIR,
    STAGED_RUNS_DIR,
    STAGED_RUN_MANIFEST_FILE_TEMPLATE,
    STAGED_MANIFEST_FILENAME,
)
from processing.staging_orchestrator import (
    generate_run_id,
    resolve_selected_authorities,
    cleanup_deselected_staged_artefacts,
    create_run_manifest,
    load_run_manifest,
    update_namespace_checkpoint,
    finalize_run_manifest,
    resolve_run_manifest_path,
    summarize_run_manifest,
    list_run_manifest_paths,
    should_skip_script,
    mark_run_resumed,
    build_fanout_plan,
    check_completion_barrier,
    update_namespace_stage_status,
)
from processing.stage_writers import (
    estimate_wall_time_seconds,
    record_script_wall_time,
    write_namespace_places_snapshot_parquet,
    write_runtime_history_event,
    write_stage_event,
)
from processing.namespace_materialize import materialize_namespace_snapshot_manifest

es = Elasticsearch(ES_HOST) if ES_HOST else None

STATE_FILES = {
    'osm': OSM_STATE_FILE,
    'ohm': OHM_STATE_FILE,
}

# Full ingestion order with script IDs for tracking
# Format: (namespace, script_name, description, script_id)
INGESTION_ORDER = [
    ('osm', 'osm-places', 'OpenStreetMap', 'osm-places'),
    ('ohm', 'ohm-places', 'OpenHistoricalMap', 'ohm-places'),
    ('gn', 'geonames-places', 'GeoNames places', 'gn-places'),
    ('gn', 'geonames-toponyms', 'GeoNames toponyms (updates places)', 'gn-toponyms'),
    ('wd', 'wikidata-places', 'Wikidata places', 'wd-places'),
    ('tgn', 'tgn-places', 'Getty TGN', 'tgn-places'),
    ('pl', 'pleiades-places', 'Pleiades ancient places', 'pl-places'),
    ('un', 'un-countries', 'UN member countries', 'un-countries'),
    ('dp', 'dplace-places', 'D-PLACE linguistic data', 'dp-places'),
    ('nl', 'nativeland-places', 'Native Land territories', 'nl-places'),
    ('gb', 'gb1900-places', 'GB1900 British places', 'gb-places'),
    ('iv', 'indexvillaris-places', 'Index Villaris 1680', 'iv-places'),
    ('chgis', 'chgis.places', 'CHGIS/TGAZ historical Chinese places', 'chgis-places'),
    ('dgsd', 'dgsd.places', 'DGSD Song dynasty places', 'dgsd-places'),
    ('tm', 'trismegistos.places', 'Trismegistos ancient places', 'tm-places'),
    ('po', 'periodo-places', 'PeriodO temporal periods', 'po-places'),
    ('clio', 'cliopatria-places', 'Cliopatria historical polities', 'clio-places'),
    ('loc', 'loc-relations', 'Library of Congress relations (updates places)', 'loc-relations'),
    ('wd', 'wikidata-geoshapes', 'Wikidata geoshapes (updates places)', 'wd-geoshapes'),
]

FINAL_NAMESPACE_SCRIPT_ID = {
    "gn": "gn-toponyms",
    "wd": "wd-geoshapes",
}


def _is_namespace_snapshot_trigger(namespace: str, script_id: str) -> bool:
    """Return True when a namespace has reached its final local mutator.

    Most namespaces finalize after their `*-places` script. Namespaces with
    follow-up mutators (currently GeoNames and Wikidata) finalize only after the
    last namespace-local updater has run.
    """
    if namespace in FINAL_NAMESPACE_SCRIPT_ID:
        return script_id == FINAL_NAMESPACE_SCRIPT_ID[namespace]
    return script_id.endswith("-places")


def delete_existing_namespace(namespace):
    """
    Delete all places for a given authority namespace.
    """
    print(f"\nDeleting existing data for namespace '{namespace}'")
    sys.stdout.flush()

    query = {
        "query": {
            "prefix": {
                "place_id": f"{namespace}:"
            }
        }
    }

    resp = es.options(request_timeout=3600).delete_by_query(
        index=PLACES_INDEX,
        body=query,
        conflicts="proceed",
        refresh=True,
        slices="auto",
        wait_for_completion=True
    )

    deleted = resp.get("deleted", 0)
    print(f"  Deleted {deleted:,} places")
    sys.stdout.flush()
    return deleted


def check_elasticsearch():
    if es is None:
        print("✗ Elasticsearch host is not configured (ES_HOST is empty)")
        sys.stdout.flush()
        return False
    try:
        info = es.info()
        print(f"✓ Elasticsearch {info['version']['number']} is running")
        if not es.indices.exists(index=PLACES_INDEX):
            print(f"✗ '{PLACES_INDEX}' index does not exist")
            return False
        if not es.indices.exists(index=TOPONYMS_INDEX):
            print(f"✗ '{TOPONYMS_INDEX}' index does not exist")
            return False
        print("✓ Required indices exist")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"✗ Cannot connect to Elasticsearch: {e}")
        sys.stdout.flush()
        return False


def check_data_files():
    available = {}
    for auth in AUTHORITIES:
        namespace = auth['namespace']
        auth_dir = Path(DATA_DIR) / 'authorities' / namespace

        sys.stdout.write(f"  Checking {namespace}...")
        sys.stdout.flush()

        if not auth_dir.exists():
            available[namespace] = False
            print(" directory not found")
            continue

        try:
            # Check if any configured files exist
            files = auth.get('files', [])
            if not files:
                available[namespace] = False
                print(" no files configured")
                continue

            has_files = False
            for file_config in files:
                filename = file_config.get('name') or Path(file_config['url']).name
                filepath = auth_dir / filename
                if filepath.exists():
                    has_files = True
                    break

            available[namespace] = has_files
            print(" OK" if has_files else " files not found")

        except Exception as e:
            print(f" ERROR: {e}")
            available[namespace] = False

    sys.stdout.flush()
    return available


def get_index_counts():
    counts = {}
    total_count = 0

    for auth in AUTHORITIES:
        namespace = auth['namespace']
        dataset_name = auth['dataset_name']
        sys.stdout.write(f"  Counting '{dataset_name}'...")
        sys.stdout.flush()
        try:
            response = es.options(request_timeout=30).count(
                index=PLACES_INDEX,
                body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
            )
            count = response['count']
            counts[namespace] = count
            total_count += count

            print(f" {count:,}")
            sys.stdout.flush()
        except Exception as e:
            print(f" ERROR: {e}")
            sys.stdout.flush()
            counts[namespace] = 0

    print()
    print(f"  TOTAL PLACES: {total_count:,}")
    print()
    sys.stdout.flush()

    return counts


def run_ingestion(namespace, script_name, skip_existing=True, replace_existing=False):
    """
    Run a single ingestion script, handling skip/replace silently.
    """
    print(f"\n{'=' * 80}")
    print(f"INGESTING: {namespace.upper()} ({script_name})")
    print(f"{'=' * 80}")
    sys.stdout.flush()

    # For update scripts (toponyms, geoshapes, relations), always run
    update_scripts = ['geonames-toponyms', 'wikidata-geoshapes', 'loc-relations']
    is_update_script = script_name in update_scripts

    if not is_update_script:
        count = es.options(request_timeout=30).count(
            index=PLACES_INDEX,
            body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
        )['count']

        # If no docs exist for a resumable source, stale checkpoints can skip
        # large parts of the source file and create apparent missing docs.
        state_file = STATE_FILES.get(namespace)
        if count == 0 and state_file and os.path.exists(state_file):
            print(f"  Removing stale checkpoint for {namespace}: {state_file}")
            os.remove(state_file)

        if count > 0:
            if replace_existing:
                delete_existing_namespace(namespace)

                # Remove namespace checkpoint file when re-ingesting from scratch
                if state_file and os.path.exists(state_file):
                    os.remove(state_file)

            elif skip_existing:
                print(f"Skipping {namespace}: {count:,} places already exist")
                sys.stdout.flush()
                return True

    if script_name == 'wikidata-geoshapes':
        # Delete log of processed geoshapes from any previous run (does not delete cached geoshapes)
        if os.path.exists(GEOSHAPE_LOG_FILE):
            os.remove(GEOSHAPE_LOG_FILE)

    start_time = datetime.now()
    try:
        cmd = [sys.executable, "-u", "-m", f"authorities.{script_name}"]
        subprocess.run(cmd, check=True, stdout=sys.stdout, stderr=sys.stderr)
        es.indices.refresh(index=",".join([PLACES_INDEX, TOPONYMS_INDEX]))
        elapsed = datetime.now() - start_time
        print(f"\n✓ Completed in {str(elapsed).split('.')[0]}")
        sys.stdout.flush()

        if not is_update_script:
            count = es.options(request_timeout=30).count(
                index=PLACES_INDEX,
                body={'query': {'prefix': {'place_id': f"{namespace}:"}}}
            )['count']
            print(f"  Total {namespace.upper()} places: {count:,}")
            sys.stdout.flush()

        return True
    except subprocess.CalledProcessError as e:
        print(f"\n✗ Script failed with exit code {e.returncode}")
        sys.stdout.flush()
        return False
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        sys.stdout.flush()
        return False


def ingest_all(
    authorities_to_run=None,
    skip_existing=True,
    replace_existing=False,
    delete_only=False,
    run_manifest_path: Path | None = None,
    run_id: str | None = None,
    resume_run: bool = False,
    write_stage_snapshots: bool = False,
    materialize_namespace_manifest: bool = False,
    defer_h3: bool = True,
):
    """
    Run all configured ingestions in order.

    If authorities_to_run is specified, it should be namespace codes.
    All scripts for that namespace will be included in order.
    """
    ingestion_order = list(INGESTION_ORDER)

    # Filter by requested namespaces (includes ALL scripts for that namespace)
    if authorities_to_run:
        ingestion_order = [
            (ns, script, desc, script_id)
            for ns, script, desc, script_id in ingestion_order
            if ns in authorities_to_run
        ]

    print("\nWill operate on: " + (', '.join(authorities_to_run) if authorities_to_run else "all available authorities") + " (including all scripts for each)")
    print("\nPlanned ingestion order:")
    for i, (ns, script, desc, script_id) in enumerate(ingestion_order, 1):
        print(f"  {i}. {desc} ({script_id})")
    sys.stdout.flush()

    if not ingestion_order:
        print("\nNo authorities to process!")
        sys.stdout.flush()
        return {'successful': [], 'failed': [], 'skipped': []}

    if run_manifest_path and run_id:
        effective_namespaces = sorted(set(authorities_to_run or [ns for ns, *_ in ingestion_order]))
        if resume_run:
            manifest = load_run_manifest(run_manifest_path)
            plan = build_fanout_plan(effective_namespaces, manifest)
            print("\nResume fan-out plan:")
            print(f"  pending:   {', '.join(plan['pending']) or '-'}")
            print(f"  running:   {', '.join(plan['running']) or '-'}")
            print(f"  failed:    {', '.join(plan['failed']) or '-'}")
            print(f"  completed: {', '.join(plan['completed']) or '-'}")
            mark_run_resumed(run_manifest_path)
        else:
            create_run_manifest(run_manifest_path, run_id, effective_namespaces)

    if run_id:
        try:
            write_runtime_history_event(
                run_id=run_id,
                event="ingest_run",
                status="resumed" if resume_run else "started",
                details={
                    "selected_namespaces": authorities_to_run or sorted(set(ns for ns, *_ in ingestion_order)),
                    "defer_h3": defer_h3,
                },
            )
        except Exception as e:
            print(f"  Warning: failed to write runtime history event (run start): {e}")

    results = {'successful': [], 'failed': [], 'skipped': []}

    try:
        for ns, script, desc, script_id in ingestion_order:
            # Skip remaining scripts for a namespace that already failed
            if ns in results['failed']:
                continue

            if run_manifest_path and resume_run and should_skip_script(run_manifest_path, ns, script_id):
                print(f"Skipping {ns}/{script_id}: checkpoint already completed")
                continue

            auth_dir = Path(DATA_DIR) / 'authorities' / ns

            # Skip if no data files found (only check for the first script of each namespace)
            if script_id.endswith('-places') or script_id == 'loc-relations':
                # These authorities store their databases in the source tree, not DATA_DIR
                # Their places.py scripts auto-build the DB if missing
                SOURCE_TREE_DBS = {
                    'tm': ('trismegistos', 'tm_geo.db'),
                    'chgis': ('chgis', 'tgaz.db'),
                    'dgsd': ('dgsd', 'dgsd.db'),
                }
                # These authorities self-fetch their data at ingestion time
                SELF_FETCHING = {'po', 'clio'}

                if ns in SOURCE_TREE_DBS:
                    subdir, db_name = SOURCE_TREE_DBS[ns]
                    db_path = Path(__file__).resolve().parent.parent / 'authorities' / subdir / db_name
                    # Don't skip — the places.py script will auto-build if needed
                    if not db_path.exists():
                        print(f"\n  Note: {db_name} not found; will be built automatically during ingestion")
                        sys.stdout.flush()
                elif ns in SELF_FETCHING:
                    pass  # These scripts download their own data; don't skip
                elif not auth_dir.exists() or not any(auth_dir.iterdir()):
                    print(f"\n⚠ Skipping {ns}: No data files found")
                    sys.stdout.flush()
                    if ns not in results['skipped']:
                        results['skipped'].append(ns)
                    continue

            if delete_only:
                # Only delete for the first script of each namespace
                if script_id.endswith('-places') or script_id == 'loc-relations':
                    delete_existing_namespace(ns)
                    if ns not in results['successful']:
                        results['successful'].append(ns)
                continue

            if ns == 'loc':
                print(f"\nNOTE: LOC creates relations only, not new places")
                sys.stdout.flush()

            if run_manifest_path:
                update_namespace_checkpoint(run_manifest_path, ns, script_id, "running")
                update_namespace_stage_status(run_manifest_path, ns, "extract", "running")
            if run_id:
                try:
                    write_stage_event(
                        run_id=run_id,
                        namespace=ns,
                        script_id=script_id,
                        status="running",
                        stage="extract",
                    )
                except Exception as e:
                    print(f"  Warning: failed to write stage event ({ns}/{script_id}/running): {e}")
                try:
                    write_runtime_history_event(
                        run_id=run_id,
                        event="script",
                        status="running",
                        namespace=ns,
                        script_id=script_id,
                        stage="extract",
                    )
                except Exception as e:
                    print(f"  Warning: failed to write runtime history event ({ns}/{script_id}/running): {e}")

            _script_started_at = datetime.now(timezone.utc)
            success = run_ingestion(ns, script, skip_existing=skip_existing, replace_existing=replace_existing)
            _script_finished_at = datetime.now(timezone.utc)
            _wall_seconds = (_script_finished_at - _script_started_at).total_seconds()

            if run_id:
                try:
                    record_script_wall_time(
                        namespace=ns,
                        script_id=script_id,
                        run_id=run_id,
                        started_at=_script_started_at.isoformat(),
                        finished_at=_script_finished_at.isoformat(),
                        wall_seconds=_wall_seconds,
                        status="completed" if success else "failed",
                        slurm_job_id=os.environ.get("SLURM_JOB_ID"),
                    )
                except Exception as e:
                    print(f"  Warning: failed to record script wall time ({ns}/{script_id}): {e}")

            if success:
                if run_manifest_path:
                    update_namespace_checkpoint(run_manifest_path, ns, script_id, "completed")
                should_snapshot = _is_namespace_snapshot_trigger(ns, script_id)
                if write_stage_snapshots and should_snapshot and run_id and es is not None:
                    try:
                        snapshot_meta = write_namespace_places_snapshot_parquet(
                            es_client=es,
                            index_name=PLACES_INDEX,
                            namespace=ns,
                            run_id=run_id,
                        )
                        write_stage_event(
                            run_id=run_id,
                            namespace=ns,
                            script_id=script_id,
                            status="snapshot_written",
                            stage="extract",
                            metrics={
                                "docs_written": snapshot_meta.get("docs_written", 0),
                                "parquet_path": snapshot_meta.get("path"),
                            },
                        )
                    except Exception as e:
                        print(f"  Warning: failed to write staged snapshot ({ns}/{script_id}): {e}")
                if materialize_namespace_manifest and should_snapshot and run_id:
                    try:
                        materialize_namespace_snapshot_manifest(
                            namespace=ns,
                            run_id=run_id,
                            staged_base_dir=STAGED_BASE_DIR,
                            extract_stage="extract",
                            manifest_filename=STAGED_MANIFEST_FILENAME,
                        )
                        write_stage_event(
                            run_id=run_id,
                            namespace=ns,
                            script_id=script_id,
                            status="materialized",
                            stage="patch",
                        )
                    except Exception as e:
                        print(f"  Warning: failed to materialize namespace manifest ({ns}/{script_id}): {e}")
                if run_manifest_path and should_snapshot:
                    update_namespace_stage_status(run_manifest_path, ns, "extract", "completed")
                    if defer_h3:
                        update_namespace_stage_status(run_manifest_path, ns, "h3", "pending")
                if run_id and should_snapshot and defer_h3:
                    try:
                        write_stage_event(
                            run_id=run_id,
                            namespace=ns,
                            script_id=script_id,
                            status="pending",
                            stage="h3",
                        )
                    except Exception as e:
                        print(f"  Warning: failed to write stage event ({ns}/{script_id}/h3 pending): {e}")
                if run_id:
                    try:
                        write_stage_event(
                            run_id=run_id,
                            namespace=ns,
                            script_id=script_id,
                            status="completed",
                            stage="extract",
                        )
                    except Exception as e:
                        print(f"  Warning: failed to write stage event ({ns}/{script_id}/completed): {e}")
                    try:
                        write_runtime_history_event(
                            run_id=run_id,
                            event="script",
                            status="completed",
                            namespace=ns,
                            script_id=script_id,
                            stage="extract",
                            details={"snapshot_trigger": should_snapshot},
                        )
                    except Exception as e:
                        print(f"  Warning: failed to write runtime history event ({ns}/{script_id}/completed): {e}")
                if ns not in results['successful']:
                    results['successful'].append(ns)
            else:
                if run_manifest_path:
                    update_namespace_checkpoint(run_manifest_path, ns, script_id, "failed", error="script failed")
                    update_namespace_stage_status(run_manifest_path, ns, "extract", "failed", error="script failed")
                if run_id:
                    try:
                        write_stage_event(
                            run_id=run_id,
                            namespace=ns,
                            script_id=script_id,
                            status="failed",
                            stage="extract",
                            error="script failed",
                        )
                    except Exception as e:
                        print(f"  Warning: failed to write stage event ({ns}/{script_id}/failed): {e}")
                    try:
                        write_runtime_history_event(
                            run_id=run_id,
                            event="script",
                            status="failed",
                            namespace=ns,
                            script_id=script_id,
                            stage="extract",
                            error="script failed",
                        )
                    except Exception as e:
                        print(f"  Warning: failed to write runtime history event ({ns}/{script_id}/failed): {e}")
                if ns not in results['failed']:
                    results['failed'].append(ns)
                # Skip remaining scripts for this namespace, but continue with others
                print(f"Stopping further {ns} scripts due to failure")
                sys.stdout.flush()
                # Mark namespace as failed so subsequent scripts for the same ns are skipped
                continue

            time.sleep(2)
    except KeyboardInterrupt:
        if run_id:
            try:
                write_runtime_history_event(
                    run_id=run_id,
                    event="ingest_run",
                    status="aborted",
                )
            except Exception:
                pass
        if run_manifest_path:
            finalize_run_manifest(run_manifest_path, "aborted")
        raise

    print(f"\n{'=' * 80}")
    print("INGESTION SUMMARY")
    print(f"{'=' * 80}")

    print(f"\n✓ Successful: {', '.join(results['successful']) or 'None'}")
    print(f"⚠ Skipped: {', '.join(results['skipped']) or 'None'}")
    print(f"✗ Failed: {', '.join(results['failed']) or 'None'}")
    sys.stdout.flush()

    counts = get_index_counts()
    print("\nFinal document counts by source:")
    total = 0
    for ns in sorted(counts.keys()):
        if counts[ns] > 0:
            print(f"  {ns:8} {counts[ns]:>12,}")
            total += counts[ns]
    print(f"  {'Total:':8} {total:>12,}")

    print(f"\nTotal places in index: {es.count(index=PLACES_INDEX)['count']:,}")
    print(f"Total toponyms in index: {es.count(index=TOPONYMS_INDEX)['count']:,}")
    sys.stdout.flush()

    if run_manifest_path:
        final_status = "failed" if results['failed'] else "completed"
        final_manifest = finalize_run_manifest(run_manifest_path, final_status)
        done, incomplete = check_completion_barrier(final_manifest)
        if not done:
            print(f"\nBarrier incomplete (selected not fully completed): {', '.join(incomplete)}")

    if run_id:
        try:
            write_runtime_history_event(
                run_id=run_id,
                event="ingest_run",
                status="failed" if results['failed'] else "completed",
                details={
                    "successful": sorted(results['successful']),
                    "failed": sorted(results['failed']),
                    "skipped": sorted(results['skipped']),
                },
            )
        except Exception as e:
            print(f"  Warning: failed to write runtime history event (run finished): {e}")

    return results


def print_run_status(run_manifest_path: Path, as_json: bool = False) -> int:
    if not run_manifest_path.exists():
        print(f"Run manifest not found: {run_manifest_path}")
        return 1

    manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    summary = summarize_run_manifest(manifest)

    if as_json:
        payload = {
            "run_id": manifest.get("run_id"),
            "run_status": manifest.get("run_status", "in_progress"),
            "created_at": manifest.get("created_at"),
            "finished_at": manifest.get("finished_at"),
            "selected_namespaces": manifest.get("selected_namespaces", []),
            "summary": summary,
            "namespaces": manifest.get("namespaces", {}),
            "path": str(run_manifest_path),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Run ID: {manifest.get('run_id')}")
    print(f"Status: {manifest.get('run_status', 'in_progress')}")
    print(f"Created: {manifest.get('created_at')}")
    print(f"Finished: {manifest.get('finished_at', '-')}")
    print("Namespaces:")
    print(f"  total={summary['total_namespaces']} completed={summary['completed']} "
          f"failed={summary['failed']} running={summary['running']} pending={summary['pending']}")
    return 0


def main():
    parser = argparse.ArgumentParser(description='Ingest all authority data sources into Elasticsearch')
    parser.add_argument('-n', '--namespaces',
                        help='Comma-separated list of namespaces to ingest/delete (runs ALL scripts for each namespace in order)')
    parser.add_argument('--check-only', action='store_true', help='Only check data availability, don\'t run ingestion')
    parser.add_argument('--skip-counts', action='store_true', help='Skip counting existing documents (faster startup)')
    parser.add_argument('--prepare-production', action='store_true', help='Run production preparation after ingestion')
    parser.add_argument('--selection-file', default=AUTHORITY_SELECTION_FILE,
                        help='Authority checkbox markdown file (default: authority-selection.md)')
    parser.add_argument('--skip-selection-file', action='store_true',
                        help='Ignore authority-selection file and use CLI namespaces/all')
    parser.add_argument('--skip-stage-cleanup', action='store_true',
                        help='Do not remove staged artefacts for deselected authorities')
    parser.add_argument('--run-status', nargs='?', const='latest',
                        help='Show run-manifest status (optional RUN_ID; default latest) and exit')
    parser.add_argument('--run-status-json', action='store_true',
                        help='With --run-status, print JSON instead of text')
    parser.add_argument('--list-runs', action='store_true',
                        help='List recent run manifests and exit')
    parser.add_argument('--resume-run', nargs='?', const='latest',
                        help='Resume from run manifest (optional RUN_ID; default latest)')
    parser.add_argument('--run-id',
                        help='Explicit run id for new runs (useful for external orchestration/dependencies)')
    parser.add_argument('--write-stage-snapshots', action='store_true',
                        help='After each namespace reaches its final local mutator, write namespace staged extract snapshots (Parquet)')
    parser.add_argument('--materialize-namespace-manifest', action='store_true',
                        help='After snapshot writing, build namespace materialized snapshot manifest')
    # H3 is deferred by default (run as a separate Slurm array after all authority
    # extract stages complete).  Pass --inline-h3 to compute H3 during ingestion
    # instead (not recommended for large authorities such as osm/ohm).
    parser.add_argument('--inline-h3', action='store_true', default=False,
                        help='Compute H3 inline during ingestion (default: defer to separate Slurm array job via h3_stage.py)')
    parser.add_argument('-d', '--delete-only', action='store_true',
                        help='Delete specified authorities without ingesting')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--skip-existing', action='store_true', help='Skip authorities that already have data (default)')
    group.add_argument('-r', '--replace-existing', action='store_true', help='Delete existing data before re-ingesting')
    args = parser.parse_args()

    skip_existing = not args.replace_existing

    if args.list_runs:
        paths = list_run_manifest_paths(Path(STAGED_RUNS_DIR), limit=20)
        if not paths:
            print(f"No run manifests found in {STAGED_RUNS_DIR}")
            sys.exit(1)
        print(f"Latest run manifests in {STAGED_RUNS_DIR}:")
        for p in paths:
            print(f"  - {p.stem}")
        sys.exit(0)

    if args.run_status is not None:
        run_id = None if args.run_status == 'latest' else args.run_status
        manifest_path = resolve_run_manifest_path(Path(STAGED_RUNS_DIR), run_id=run_id)
        if manifest_path is None:
            if run_id:
                print(f"No run manifest found for run id '{run_id}' in {STAGED_RUNS_DIR}")
            else:
                print(f"No run manifests found in {STAGED_RUNS_DIR}")
            sys.exit(1)
        sys.exit(print_run_status(manifest_path, as_json=args.run_status_json))

    print("=" * 80)
    print("AUTHORITY DATA INGESTION COORDINATOR")
    print("=" * 80)
    sys.stdout.flush()

    if not check_elasticsearch():
        sys.exit(1)

    print("\nChecking available data files:")
    sys.stdout.flush()
    available = check_data_files()

    if not args.skip_counts:
        print("\nCurrent index counts:")
        sys.stdout.flush()
        counts = get_index_counts()
    else:
        print("\nSkipping index counts (--skip-counts specified)")
        sys.stdout.flush()
        counts = {}

    if args.check_only:
        print("\nCheck complete (--check-only specified)")
        sys.stdout.flush()
        return

    all_namespaces = list(dict.fromkeys(ns for ns, *_ in INGESTION_ORDER))
    use_selection = not args.skip_selection_file and not args.namespaces

    if args.namespaces:
        namespaces = [ns.strip() for ns in args.namespaces.split(',')]
    elif use_selection:
        selection_file = Path(args.selection_file)
        namespaces = resolve_selected_authorities(selection_file, all_namespaces)
        if not namespaces:
            print(f"\nNo selected local authorities found in {selection_file}; falling back to all.")
            namespaces = all_namespaces

        if not args.skip_stage_cleanup:
            removed = cleanup_deselected_staged_artefacts(
                staged_base_dir=Path(STAGED_BASE_DIR),
                selected_namespaces=namespaces,
                known_namespaces=all_namespaces,
            )
            if removed:
                print(f"Removed staged artefacts for deselected authorities: {', '.join(sorted(removed))}")
    else:
        namespaces = None

    resume_mode = args.resume_run is not None
    if resume_mode:
        if args.run_id:
            print("--run-id cannot be used with --resume-run")
            sys.exit(1)
        resume_run_id = None if args.resume_run == 'latest' else args.resume_run
        resolved = resolve_run_manifest_path(Path(STAGED_RUNS_DIR), run_id=resume_run_id)
        if resolved is None:
            if resume_run_id:
                print(f"No run manifest found for run id '{resume_run_id}' in {STAGED_RUNS_DIR}")
            else:
                print(f"No run manifests found in {STAGED_RUNS_DIR}")
            sys.exit(1)
        run_manifest_path = resolved
        run_manifest = load_run_manifest(run_manifest_path)
        run_id = run_manifest.get("run_id")
    else:
        run_id = args.run_id or generate_run_id()
        run_manifest_path = Path(
            STAGED_RUN_MANIFEST_FILE_TEMPLATE.format(runs_dir=STAGED_RUNS_DIR, run_id=run_id)
        )

    print(f"\nRun ID: {run_id}")
    print(f"Run manifest: {run_manifest_path}")

    results = ingest_all(namespaces, skip_existing=skip_existing,
                         replace_existing=args.replace_existing,
                         delete_only=args.delete_only,
                         run_manifest_path=run_manifest_path,
                         run_id=run_id,
                         resume_run=resume_mode,
                         write_stage_snapshots=args.write_stage_snapshots,
                         materialize_namespace_manifest=args.materialize_namespace_manifest,
                         defer_h3=not args.inline_h3)

    if not args.delete_only and not namespaces:
        # Only run deduplication if processing all authorities
        # deduplicate_and_index_toponyms(es, PLACES_INDEX, TOPONYMS_INDEX)
        # Superseded by /phonetics/extraction/rebuild_toponyms_index.py
        pass
    elif not args.delete_only:
        print("\nNote: Skipping toponyms deduplication (only runs when processing all authorities)")
        print("      Run without -n flag to deduplicate toponyms across all authorities")
        sys.stdout.flush()

    if results and results.get('failed'):
        sys.exit(2)


if __name__ == "__main__":
    main()