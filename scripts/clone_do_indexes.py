#!/usr/bin/env python3
"""
Clone indexes from DigitalOcean ES to Pitt CRC ES.

Uses ES _reindex with a remote source. Designed to be re-run:
each invocation deletes the target index and re-clones from DO.

Prerequisites:
  1. SSH tunnel from Pitt VM to DO ES (DO uses HTTPS):
       ssh -L 19200:localhost:9200 whg -N &
     (or set DO_ES_URL to wherever DO's ES is reachable from Pitt)

  2. Pitt ES must whitelist the remote host in elasticsearch.yml:
       reindex.remote.whitelist: "localhost:19200"
       reindex.ssl.verification_mode: none
     Then restart ES.

Usage:
  # Clone all DO indexes (interactive — lists indexes and confirms):
  python3 scripts/clone_do_indexes.py

  # Clone specific indexes:
  python3 scripts/clone_do_indexes.py --indexes whg_2025_11_12 wdgn_20240316 pub_v2

  # Non-interactive (e.g. from cron):
  python3 scripts/clone_do_indexes.py --indexes whg_2025_11_12 --yes

  # Skip indexes that already exist on Pitt (e.g. only clone new ones):
  python3 scripts/clone_do_indexes.py --skip-existing

  # Dry run (show what would be done):
  python3 scripts/clone_do_indexes.py --dry-run

Environment variables (or pass as CLI args):
  DO_ES_URL          DO ES URL as seen from Pitt (default: https://localhost:19200)
  DO_ES_USER         DO ES username (default: elastic)
  DO_ES_PASSWORD     DO ES password (required)
  PITT_ES_URL        Pitt ES URL (default: http://localhost:9201)
  PITT_ES_PASSWORD   Pitt ES password (reads from /ix1/ishi/es/config/elastic.password if unset)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from elasticsearch import Elasticsearch

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def get_pitt_password():
    """Read Pitt ES password from file or env."""
    pw = os.getenv("PITT_ES_PASSWORD")
    if pw:
        return pw
    pw_file = Path(os.getenv("IX1_BASE", "/ix1/ishi")) / "es" / "config" / "elastic.password"
    try:
        return pw_file.read_text().strip()
    except FileNotFoundError:
        return None


def parse_args():
    p = argparse.ArgumentParser(description="Clone DO ES indexes to Pitt ES")
    p.add_argument("--do-url", default=os.getenv("DO_ES_URL", "https://localhost:19200"),
                    help="DO ES URL as seen from Pitt (default: https://localhost:19200)")
    p.add_argument("--do-user", default=os.getenv("DO_ES_USER", "elastic"))
    p.add_argument("--do-password", default=os.getenv("DO_ES_PASSWORD"),
                    help="DO ES password (or set DO_ES_PASSWORD env var)")
    p.add_argument("--pitt-url", default=os.getenv("PITT_ES_URL", "http://localhost:9201"))
    p.add_argument("--pitt-password", default=get_pitt_password())
    p.add_argument("--indexes", nargs="+", default=None,
                    help="Specific index names to clone (default: all non-system indexes)")
    p.add_argument("--skip-existing", action="store_true",
                    help="Skip indexes that already exist on Pitt")
    p.add_argument("--exclude", nargs="+", default=[],
                    help="Index names/patterns to exclude (e.g. .kibana toponyms_*)")
    p.add_argument("--slices", default="auto",
                    help="Reindex parallelism (default: auto)")
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    p.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    return p.parse_args()


# ---------------------------------------------------------------------------
# DO ES client (httpx-based, avoids v9 client header incompatibility)
# ---------------------------------------------------------------------------

class DoEsClient:
    """Lightweight ES client using httpx — sends plain JSON headers compatible with ES 8.x."""

    def __init__(self, base_url, user, password):
        self.base_url = base_url.rstrip("/")
        self.auth = (user, password) if user and password else None
        self.client = httpx.Client(verify=False, timeout=300, auth=self.auth)

    def _get(self, path, params=None):
        r = self.client.get(f"{self.base_url}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def _text(self, path, params=None):
        r = self.client.get(f"{self.base_url}{path}", params=params)
        r.raise_for_status()
        return r.text

    def info(self):
        return self._get("/")

    def get_index_info(self):
        rows = self._get("/_cat/indices", params={"format": "json", "h": "index,docs.count,store.size,pri.store.size"})
        result = {}
        for row in rows:
            name = row["index"]
            if name.startswith("."):
                continue
            result[name] = {
                "docs": int(row.get("docs.count") or 0),
                "size": row.get("pri.store.size", "?"),
            }
        return result

    def get_mapping(self, index):
        return self._get(f"/{index}/_mapping")[index]["mappings"]

    def get_settings(self, index):
        raw = self._get(f"/{index}/_settings")[index]["settings"]["index"]
        keep = ["number_of_shards", "number_of_replicas", "codec",
                "sort.field", "sort.order", "max_result_window", "analysis"]
        return {k: raw[k] for k in keep if k in raw}

    def count(self, index):
        return self._get(f"/{index}/_count")["count"]

    def close(self):
        self.client.close()


# ---------------------------------------------------------------------------
# Pitt ES client (standard elasticsearch-py v9)
# ---------------------------------------------------------------------------

def connect_pitt(url, password):
    """Create an ES 9.x client for Pitt."""
    return Elasticsearch(url, basic_auth=("elastic", password), request_timeout=300)


def get_pitt_index_info(es):
    """Return dict of {index_name: {docs, size}}."""
    cat = es.cat.indices(format="json", h="index,docs.count,store.size,pri.store.size")
    result = {}
    for row in cat:
        name = row["index"]
        if name.startswith("."):
            continue
        result[name] = {
            "docs": int(row.get("docs.count") or 0),
            "size": row.get("pri.store.size", "?"),
        }
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_table(indexes: dict) -> str:
    """Format index info as a readable table."""
    lines = [f"  {'Index':<40} {'Docs':>12}  {'Size':>10}"]
    lines.append("  " + "-" * 66)
    for name in sorted(indexes):
        info = indexes[name]
        lines.append(f"  {name:<40} {info['docs']:>12,}  {info['size']:>10}")
    return "\n".join(lines)


def wait_for_task(es, task_id, index_name, poll_interval=10):
    """Poll a task until complete, printing progress."""
    while True:
        task = es.tasks.get(task_id=task_id)
        status = task.get("task", {}).get("status", {})
        total = status.get("total", 0)
        created = status.get("created", 0)
        if total > 0:
            pct = created / total * 100
            print(f"\r  {index_name}: {created:,}/{total:,} docs ({pct:.1f}%)", end="", flush=True)
        if task.get("completed"):
            print()  # newline
            if task.get("error"):
                print(f"  ERROR: {json.dumps(task['error'], indent=2)}")
                return False
            resp = task.get("response", {})
            failures = resp.get("failures", [])
            if failures:
                print(f"  WARNING: {len(failures)} failures during reindex")
                for f in failures[:3]:
                    print(f"    {json.dumps(f, indent=2)}")
            return True
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Clone logic
# ---------------------------------------------------------------------------

def clone_index(do_es, pitt_es, index_name, do_url, do_user, do_password, slices="auto"):
    """Clone a single index from DO to Pitt."""
    print(f"\n{'='*60}")
    print(f"Cloning: {index_name}")
    print(f"{'='*60}")

    # 1. Get mapping and settings from DO
    print(f"  Fetching mapping from DO...")
    mapping = do_es.get_mapping(index_name)
    settings = do_es.get_settings(index_name)

    # Use relaxed settings during bulk indexing
    settings["refresh_interval"] = "-1"
    settings["number_of_replicas"] = "0"

    # 2. Delete target on Pitt if it exists
    if pitt_es.indices.exists(index=index_name):
        print(f"  Deleting existing index on Pitt...")
        pitt_es.indices.delete(index=index_name, timeout="60s")
        for _ in range(30):
            if not pitt_es.indices.exists(index=index_name):
                break
            time.sleep(1)

    # 3. Create target index on Pitt with DO's mapping
    print(f"  Creating index on Pitt...")
    pitt_es.indices.create(
        index=index_name,
        body={"settings": settings, "mappings": mapping},
    )

    # 4. Reindex from remote
    print(f"  Starting reindex from DO...")
    remote = {"host": do_url}
    if do_user:
        remote["username"] = do_user
    if do_password:
        remote["password"] = do_password

    result = pitt_es.reindex(
        body={
            "source": {"remote": remote, "index": index_name},
            "dest": {"index": index_name},
        },
        wait_for_completion=False,
        timeout="24h",
    )

    task_id = result["task"]
    print(f"  Task ID: {task_id}")

    # 5. Wait for completion
    success = wait_for_task(pitt_es, task_id, index_name)

    # 6. Refresh and verify
    if success:
        pitt_es.indices.refresh(index=index_name)
        pitt_es.indices.put_settings(
            index=index_name,
            body={"index": {"refresh_interval": "1s"}},
        )
        do_count = do_es.count(index_name)
        pitt_count = pitt_es.count(index=index_name)["count"]
        match = "✓" if do_count == pitt_count else "✗ MISMATCH"
        print(f"  Doc counts — DO: {do_count:,}  Pitt: {pitt_count:,}  {match}")

    return success


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if not args.do_password:
        print("ERROR: DO ES password required. Set DO_ES_PASSWORD or use --do-password.")
        sys.exit(1)

    if not args.dry_run and not args.pitt_password:
        print("ERROR: Pitt ES password not found. Set PITT_ES_PASSWORD or check password file.")
        sys.exit(1)

    # Connect to DO (httpx-based client — no v9 header issues)
    print("Connecting to DO ES:", args.do_url)
    do_es = DoEsClient(args.do_url, args.do_user, args.do_password)
    try:
        do_info = do_es.info()
        print(f"  Version: {do_info['version']['number']}, cluster: {do_info['cluster_name']}")
    except Exception as e:
        print(f"  ERROR connecting to DO ES: {e}")
        print("  Is the SSH tunnel running?  ssh -L 19200:localhost:9200 whg -N &")
        sys.exit(1)

    # Connect to Pitt (skip if dry-run and no password)
    pitt_es = None
    pitt_indexes = {}
    if args.pitt_password:
        print("\nConnecting to Pitt ES:", args.pitt_url)
        pitt_es = connect_pitt(args.pitt_url, args.pitt_password)
        try:
            pitt_info = pitt_es.info()
            print(f"  Version: {pitt_info['version']['number']}, cluster: {pitt_info['cluster_name']}")
        except Exception as e:
            if not args.dry_run:
                print(f"  ERROR connecting to Pitt ES: {e}")
                sys.exit(1)
            print(f"  (Pitt ES not reachable — dry-run continues without Pitt inventory)")
            pitt_es = None
    elif args.dry_run:
        print("\n(Skipping Pitt ES connection — dry-run mode)")

    # Discover indexes
    do_indexes = do_es.get_index_info()
    if pitt_es:
        pitt_indexes = get_pitt_index_info(pitt_es)

    print(f"\n--- DO indexes ({len(do_indexes)}) ---")
    print(format_table(do_indexes))

    if pitt_indexes:
        print(f"\n--- Pitt indexes ({len(pitt_indexes)}) ---")
        print(format_table(pitt_indexes))

    # Determine which indexes to clone
    if args.indexes:
        to_clone = [i for i in args.indexes if i in do_indexes]
        missing = [i for i in args.indexes if i not in do_indexes]
        if missing:
            print(f"\nWARNING: indexes not found on DO: {', '.join(missing)}")
    else:
        to_clone = sorted(do_indexes.keys())

    # Apply exclusions
    if args.exclude:
        import fnmatch
        to_clone = [i for i in to_clone
                    if not any(fnmatch.fnmatch(i, pat) for pat in args.exclude)]

    # Apply --skip-existing
    if args.skip_existing:
        skipped = [i for i in to_clone if i in pitt_indexes]
        to_clone = [i for i in to_clone if i not in pitt_indexes]
        if skipped:
            print(f"\nSkipping (already on Pitt): {', '.join(skipped)}")

    if not to_clone:
        print("\nNothing to clone.")
        sys.exit(0)

    # Show plan
    print(f"\n{'='*60}")
    print(f"CLONE PLAN: {len(to_clone)} index(es) from DO → Pitt")
    print(f"{'='*60}")
    total_docs = 0
    for name in to_clone:
        info = do_indexes[name]
        exists = " (will replace)" if name in pitt_indexes else ""
        print(f"  {name:<40} {info['docs']:>12,}  {info['size']:>10}{exists}")
        total_docs += info["docs"]
    print(f"  {'TOTAL':<40} {total_docs:>12,}")

    if args.dry_run:
        print("\n(dry run — no changes made)")
        do_es.close()
        sys.exit(0)

    if not args.yes:
        reply = input("\nProceed? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            sys.exit(0)

    # Clone each index
    results = {}
    t0 = time.time()
    for name in to_clone:
        try:
            ok = clone_index(
                do_es, pitt_es, name,
                do_url=args.do_url,
                do_user=args.do_user,
                do_password=args.do_password,
                slices=args.slices,
            )
            results[name] = "OK" if ok else "FAILED"
        except Exception as e:
            print(f"  ERROR: {e}")
            results[name] = f"ERROR: {e}"

    elapsed = time.time() - t0
    do_es.close()

    # Summary
    print(f"\n{'='*60}")
    print(f"CLONE SUMMARY (elapsed: {elapsed/60:.1f} min)")
    print(f"{'='*60}")
    for name, status in results.items():
        print(f"  {name:<40} {status}")


if __name__ == "__main__":
    main()

