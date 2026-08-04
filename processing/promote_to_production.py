"""Promote a completed staged rebuild into production.

A full rebuild is constructed in a disposable staging Elasticsearch on a Slurm
compute node (``source es.sh -staging-start``), never in the live cluster. This
module is the other half of that arrangement: it moves the finished indices
across and cuts over.

    snapshot(staging) -> restore(production) -> atomic alias swap

Three properties the ad-hoc version of this sequence kept failing to provide,
and which are the reason it is a committed tool rather than a runbook:

**Both indices move together.** ``places`` and ``toponyms`` are built from the
same corpus and reference each other — a toponym's ``attestations[]`` are
place_ids. Swapping one alias without the other leaves the gateway joining a
new toponym inventory onto an old place index, which does not error; it just
silently drops the hits whose ids only exist on one side. So both aliases move
in a **single** ``_aliases`` request, which Elasticsearch applies atomically,
and the tool refuses to swap at all if either side fails verification.

**Nothing is swapped that has not been counted.** Every stage is verified
against the source rather than against its own report: the snapshot against its
shard totals, the restore against the staging doc count, and — for ``places`` —
the presence of the ``extract_namespace`` ingest pipeline, which a restore does
not recreate and whose absence makes every subsequent write 400.

**It is resumable.** Each stage checks whether its output already exists and is
complete before doing anything, so an interrupted promotion is re-run rather
than unpicked. That matters because the expensive stages take tens of minutes
and staging ES has a wall clock.

Dry-run by default; ``--execute`` performs the work.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from elasticsearch import Elasticsearch

from processing.index_freshness import check_all, stale_namespaces
from processing.settings import (
    STAGING_REPO_NAME,
    get_staging_host,
    is_production_host,
)

DEFAULT_ALIASES = ("places", "toponyms")
DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"
PLACES_PIPELINE_ID = "extract_namespace"
PLACES_PIPELINE_SCHEMA = "schemas/places_pipeline.json"

POLL_SECONDS = 20


# --------------------------------------------------------------------------
# clients
# --------------------------------------------------------------------------

def make_client(url: str, password_file: str | None = None,
                timeout: int = 120) -> Elasticsearch:
    kwargs = {"request_timeout": timeout, "retry_on_timeout": True,
              "max_retries": 3}
    if password_file:
        p = Path(password_file)
        if p.exists():
            kwargs["basic_auth"] = ("elastic", p.read_text().strip())
    return Elasticsearch(url, **kwargs)


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

def resolve_staged_indices(staging: Elasticsearch, prod: Elasticsearch,
                           run_id: str, aliases: tuple[str, ...],
                           ) -> tuple[dict[str, str], dict[str, str]]:
    """Map each alias to the concrete index built for ``run_id``.

    Index names are ``{alias}_{run_id}`` lowercased, which is how
    ``index_from_stage`` and the toponym rebuild name their output.

    Returns ``(in_staging, already_in_prod)``. The second is not an error
    case: staging is disposable and wall-clocked, and ``places`` and
    ``toponyms`` are built hours apart, so an index transferred by an earlier
    (or interrupted) promotion legitimately sits in production awaiting its
    alias while its sibling is still being built in a *later* staging
    instance. Requiring both halves to coexist in one staging instance would
    force the two builds to share a wall clock for no reason — and the whole
    point of the atomic swap is that they need not.
    """
    suffix = run_id.lower()
    in_staging: dict[str, str] = {}
    in_prod: dict[str, str] = {}
    missing: list[str] = []
    for alias in aliases:
        name = f"{alias}_{suffix}"
        if staging.indices.exists(index=name):
            in_staging[alias] = name
        elif prod.indices.exists(index=name):
            in_prod[alias] = name
        else:
            missing.append(name)
    if missing:
        raise SystemExit(
            "Found in neither staging nor production: " + ", ".join(missing) +
            "\n  Staging holds: " +
            ", ".join(sorted(staging.indices.get(index="*").keys()))
        )
    return in_staging, in_prod


def doc_count(client: Elasticsearch, index: str) -> int:
    client.indices.refresh(index=index)
    return int(client.count(index=index)["count"])


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------

def ensure_snapshot(staging: Elasticsearch, repo: str, snapshot: str,
                    indices: list[str], execute: bool) -> bool:
    """Take (or confirm) a snapshot of exactly ``indices``. True if usable."""
    try:
        existing = staging.snapshot.get(repository=repo, snapshot=snapshot)
        info = existing["snapshots"][0]
        state, have = info["state"], set(info.get("indices", []))
        if state == "SUCCESS" and set(indices) <= have:
            print(f"  snapshot {snapshot}: already SUCCESS "
                  f"({len(have)} indices) — reusing")
            return True
        if state in ("IN_PROGRESS", "STARTED"):
            print(f"  snapshot {snapshot}: in progress — waiting")
            return wait_snapshot(staging, repo, snapshot)
        raise SystemExit(
            f"  snapshot {snapshot} exists but is unusable "
            f"(state={state}, indices={sorted(have)}).\n"
            "  Delete it or pass a different --snapshot-name."
        )
    except SystemExit:
        raise
    except Exception:
        pass  # not found — create below

    if not execute:
        print(f"  [dry-run] would snapshot {indices} -> {repo}/{snapshot}")
        return False
    print(f"  creating snapshot {repo}/{snapshot} of {indices}")
    staging.snapshot.create(
        repository=repo, snapshot=snapshot, wait_for_completion=False,
        indices=",".join(indices), include_global_state=False,
    )
    return wait_snapshot(staging, repo, snapshot)


def wait_snapshot(staging: Elasticsearch, repo: str, snapshot: str) -> bool:
    while True:
        status = staging.snapshot.status(repository=repo, snapshot=snapshot)
        snap = status["snapshots"][0]
        shards = snap["shards_stats"]
        if snap["state"] in ("SUCCESS", "FAILED", "PARTIAL"):
            ok = snap["state"] == "SUCCESS" and shards["failed"] == 0
            print(f"  snapshot {snapshot}: {snap['state']} "
                  f"({shards['done']}/{shards['total']} shards, "
                  f"{shards['failed']} failed)")
            return ok
        print(f"    … {shards['done']}/{shards['total']} shards")
        time.sleep(POLL_SECONDS)


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def ensure_restored(prod: Elasticsearch, repo: str, snapshot: str,
                    indices: list[str], execute: bool,
                    reuse_existing: bool = False) -> bool:
    """Restore ``indices`` into production.

    An index of the same name already in production is **not** assumed to be
    the same index. It is routinely an *earlier generation* of the same build:
    a rebuild that was restored, then found stale, then corrected in staging.
    Doc counts cannot distinguish the two (a re-run of ``update_merge`` adds
    names to existing places and leaves the count identical), so presence is
    treated as a conflict to be resolved deliberately rather than as evidence
    of correctness.

    ``reuse_existing`` accepts the resident copy — correct when a previous
    promotion of *this same* snapshot was interrupted after the restore.
    """
    resident = [i for i in indices if prod.indices.exists(index=i)]
    if resident and not reuse_existing:
        raise SystemExit(
            "Already in production: " + ", ".join(resident) + "\n"
            "  This is NOT proof it matches staging — an earlier generation of\n"
            "  the same build carries the same name and the same doc count.\n"
            "  Either delete it and re-run:\n"
            "    curl -XDELETE $PROD/" + resident[0] + "\n"
            "  or pass --reuse-existing if a previous promotion of this exact\n"
            "  snapshot was interrupted after its restore completed."
        )
    todo = [i for i in indices if i not in resident]
    for present in resident:
        print(f"  {present}: already present, --reuse-existing given — kept")
    if not todo:
        return True
    if not execute:
        print(f"  [dry-run] would restore {todo} from {repo}/{snapshot}")
        return False

    print(f"  restoring {todo}")
    prod.snapshot.restore(
        repository=repo, snapshot=snapshot, wait_for_completion=False,
        indices=",".join(todo), include_aliases=False,
        include_global_state=False,
    )
    return wait_recovery(prod, todo)


def wait_recovery(prod: Elasticsearch, indices: list[str]) -> bool:
    """Block until every shard of ``indices`` is recovered."""
    while True:
        health = prod.cluster.health(
            index=",".join(indices), wait_for_status="yellow", timeout="60s",
        )
        recovery = prod.indices.recovery(index=",".join(indices),
                                         active_only=True)
        active = sum(len(v["shards"]) for v in recovery.values())
        if active == 0 and not health.get("timed_out", False):
            print(f"  recovery complete ({health['status']})")
            return health["status"] in ("green", "yellow")
        pct = [s.get("index", {}).get("size", {}).get("percent", "?")
               for v in recovery.values() for s in v["shards"]]
        print(f"    … {active} shards recovering {pct}")
        time.sleep(POLL_SECONDS)


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def verify(staging: Elasticsearch, prod: Elasticsearch,
           in_staging: dict[str, str], in_prod: dict[str, str],
           ) -> tuple[bool, list[str]]:
    """Compare production against staging. Returns (ok, report lines)."""
    ok = True
    lines: list[str] = []
    resolved = {**in_staging, **in_prod}
    for alias, index in sorted(in_staging.items()):
        src = doc_count(staging, index)
        dst = doc_count(prod, index)
        match = src == dst
        ok &= match
        lines.append(f"  {alias:9s} {index}: staging {src:,} / prod {dst:,} "
                     f"{'OK' if match else '*** MISMATCH ***'}")
    for alias, index in sorted(in_prod.items()):
        # No staging copy to compare against — the source is gone. Assert what
        # can still be asserted: the index is there, healthy, and non-empty.
        dst = doc_count(prod, index)
        health = prod.cluster.health(index=index)["status"]
        good = dst > 0 and health in ("green", "yellow")
        ok &= good
        lines.append(f"  {alias:9s} {index}: prod {dst:,} ({health}), "
                     f"transferred earlier {'OK' if good else '*** BAD ***'}")

    # A restore does not recreate ingest pipelines, and the places index
    # carries default_pipeline=extract_namespace. If the pipeline is absent
    # every write to the promoted index 400s — silently, from the caller's
    # point of view, because nothing fails until something writes.
    if "places" in resolved:
        try:
            prod.ingest.get_pipeline(id=PLACES_PIPELINE_ID)
            lines.append(f"  pipeline  {PLACES_PIPELINE_ID}: present OK")
        except Exception:
            ok = False
            lines.append(
                f"  pipeline  {PLACES_PIPELINE_ID}: *** MISSING *** — PUT it "
                f"from {PLACES_PIPELINE_SCHEMA} before swapping")
    return ok, lines


# --------------------------------------------------------------------------
# alias swap
# --------------------------------------------------------------------------

def swap_aliases(prod: Elasticsearch, resolved: dict[str, str],
                 execute: bool) -> dict[str, str]:
    """Move every alias to its new index in one atomic request.

    Returns the previous alias targets so they can be named in the summary
    (and rolled back to by hand if something downstream turns out wrong).
    """
    actions: list[dict] = []
    previous: dict[str, str] = {}
    for alias, index in sorted(resolved.items()):
        try:
            current = prod.indices.get_alias(name=alias)
            for old_index in current:
                if old_index != index:
                    previous[alias] = old_index
                    actions.append({"remove": {"index": old_index,
                                               "alias": alias}})
        except Exception:
            pass  # alias does not exist yet
        actions.append({"add": {"index": index, "alias": alias}})

    for alias, index in sorted(resolved.items()):
        print(f"  {alias}: {previous.get(alias, '(none)')} -> {index}")
    if not execute:
        print("  [dry-run] alias swap not performed")
        return previous
    prod.indices.update_aliases(actions=actions)
    print("  aliases swapped atomically")
    return previous


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Promote a staged rebuild to production "
                    "(snapshot -> restore -> atomic alias swap)")
    ap.add_argument("--run-id", required=True,
                    help="Run ID; staged indices are {alias}_{run_id}")
    ap.add_argument("--aliases", default=",".join(DEFAULT_ALIASES),
                    help="Comma-separated aliases to promote "
                         f"(default: {','.join(DEFAULT_ALIASES)})")
    ap.add_argument("--staging-host", default=None,
                    help="Staging ES URL (default: from STAGING_INFO_FILE)")
    ap.add_argument("--prod-host", default="http://localhost:9201")
    ap.add_argument("--repo", default=STAGING_REPO_NAME)
    ap.add_argument("--snapshot-name", default=None,
                    help="Snapshot name (default: promote-{run_id})")
    ap.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    ap.add_argument("--skip-swap", action="store_true",
                    help="Snapshot, restore and verify, but leave aliases alone")
    ap.add_argument("--manifest-path", default=None,
                    help="Run manifest; enables the staged-source freshness "
                         "check (strongly recommended)")
    ap.add_argument("--reuse-existing", action="store_true",
                    help="Accept an index of the same name already in "
                         "production instead of failing (only when a previous "
                         "promotion of this exact snapshot was interrupted)")
    ap.add_argument("--allow-stale", action="store_true",
                    help="Promote even if a namespace's staged source changed "
                         "after it was indexed")
    ap.add_argument("--execute", action="store_true",
                    help="Perform the promotion (default: dry run)")
    args = ap.parse_args()

    aliases = tuple(a.strip() for a in args.aliases.split(",") if a.strip())
    staging_host = args.staging_host or get_staging_host()
    if not staging_host:
        raise SystemExit(
            "No staging ES found. Start one (`source es.sh -staging-start`) "
            "or pass --staging-host.")
    if is_production_host(staging_host):
        raise SystemExit(
            f"--staging-host {staging_host} looks like production. "
            "Promotion copies staging -> production; those cannot be the same "
            "cluster.")
    if not is_production_host(args.prod_host):
        print(f"WARNING: --prod-host {args.prod_host} is not recognised as "
              f"production.", file=sys.stderr)

    snapshot = args.snapshot_name or f"promote-{args.run_id.lower()}"
    staging = make_client(staging_host)
    prod = make_client(args.prod_host, args.es_password_file)

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"=== promote {args.run_id} ({mode}) ===")
    print(f"  staging: {staging_host}")
    print(f"  prod:    {args.prod_host}")

    print("\n[1/5] resolve staged indices")
    in_staging, in_prod = resolve_staged_indices(staging, prod, args.run_id,
                                                 aliases)
    resolved = {**in_staging, **in_prod}
    for alias, index in sorted(in_staging.items()):
        print(f"  {alias:9s} -> {index} ({doc_count(staging, index):,} docs, "
              f"in staging)")
    for alias, index in sorted(in_prod.items()):
        print(f"  {alias:9s} -> {index} ({doc_count(prod, index):,} docs, "
              f"already in production)")

    print("\n[2/5] snapshot staging")
    indices = sorted(in_staging.values())
    if not indices:
        print("  nothing left in staging — all indices already transferred")
    elif not ensure_snapshot(staging, args.repo, snapshot, indices,
                             args.execute):
        if args.execute:
            raise SystemExit("Snapshot did not complete cleanly — stopping.")
        print("  (dry run — later stages cannot be evaluated)")
        return

    print("\n[3/5] restore into production")
    if indices and not ensure_restored(prod, args.repo, snapshot, indices,
                                       args.execute, args.reuse_existing):
        raise SystemExit("Restore did not complete cleanly — stopping.")

    print("\n[4/5] verify")
    ok, lines = verify(staging, prod, in_staging, in_prod)
    for line in lines:
        print(line)

    # Doc counts cannot see a namespace re-enriched after it was indexed:
    # update_merge adds names to existing places, leaving the count identical.
    # gn shipped one toponym per place — 26.7M alternate names missing — with
    # every count matching. So compare the artefacts, not the totals.
    if args.manifest_path:
        manifest_data = json.loads(Path(args.manifest_path).read_text())
        names = sorted(manifest_data.get("namespaces", {}))
        stale = stale_namespaces(names, manifest_data)
        if stale:
            lines_stale = "\n".join(
                f"    {r['namespace']}: {r['detail']}"
                for r in check_all(names, manifest_data) if r["stale"])
            print(f"  freshness: *** {len(stale)} STALE *** \n{lines_stale}")
            if not args.allow_stale:
                ok = False
        else:
            print(f"  freshness: all {len(names)} namespaces current OK")
    else:
        print("  freshness: NOT CHECKED (pass --manifest-path)")

    if not ok:
        raise SystemExit(
            "Verification failed — aliases NOT swapped. Production still "
            "serves the previous indices.")

    print("\n[5/5] alias swap")
    if args.skip_swap:
        print("  --skip-swap: leaving aliases untouched")
        return
    previous = swap_aliases(prod, resolved, args.execute)

    if args.execute:
        print("\nPromoted. Previous targets (for rollback):")
        for alias, old in sorted(previous.items()):
            print(f"  {alias}: {old}")
        print("Restart the gateway so it re-reads the geom-store index: "
              "`es gateway-restart`")


if __name__ == "__main__":
    main()
