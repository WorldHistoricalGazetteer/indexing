#!/usr/bin/env python
"""Register/refresh tileset entries in the live TileServer GL config — safely.

Closes a pipeline gap (formerly an unrecorded manual edit): after the tile-gen
stage pushes new/modified ``<bucket>.mbtiles`` to the tileserver, the server's
``configs/config.json`` must be updated so it actually serves them, the server
restarted, and serving confirmed — all BEFORE the gazetteer inventory is pushed
to Django (so the Atlas never advertises a gazetteer whose tiles 404).

Done here as a single, safe rewrite:

* read the LIVE ``config.json`` and **preserve every existing entry** — basemaps,
  styles, ``datasets-*`` / ``collections-*`` (managed by the tileboss ``tiler.js``
  POST API), other gazetteer buckets, and all top-level options;
* merge in ``<bucket>: {"mbtiles": "<bucket>.mbtiles"}`` for each requested bucket
  whose mbtiles is actually present on the host — an **entry-level** merge, so any
  existing per-tileset fields (e.g. ``tilejson`` attribution) are kept;
* write back **atomically** (validate JSON on the host → timestamped backup → mv);
* restart via the on-host ``/srv/restart_services.sh``;
* **verify** each requested tileset serves (HTTP ``/data/<bucket>.json`` → 200).

Dry-run by default (prints the planned diff; touches nothing). Uses the tileserver
SSH key/host from settings — the same direct-key path ``restart_tileserver`` uses,
which works from CRC compute and from the pitt VM. Deliberately free of heavy
imports (no ``generate_tiles``) so it runs in either ``whg`` env.

    python -m processing.update_tileserver_config --bucket ukhc             # dry-run
    python -m processing.update_tileserver_config --bucket ukhc --execute   # apply
    python -m processing.update_tileserver_config --bucket gn --bucket wd --execute
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

from processing.settings import (
    TILESERVER_HOST, TILESERVER_USER, TILESERVER_SSH_KEY,
)

CONFIG_PATH = "/srv/tileserver/configs/config.json"
TILES_DIR = "/srv/tileserver/tiles"
HTTP_PORT = 8080
RESTART_SCRIPT = "cd /srv && bash restart_services.sh"


def _ssh_cmd(remote: str) -> list[str]:
    if not TILESERVER_SSH_KEY:
        raise SystemExit("TILESERVER_SSH_KEY is not configured")
    return [
        "ssh", "-i", TILESERVER_SSH_KEY,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        f"{TILESERVER_USER}@{TILESERVER_HOST}", remote,
    ]


def _ssh(remote: str, *, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        _ssh_cmd(remote),
        input=input_text, capture_output=True, text=True, timeout=timeout,
    )


def read_remote_config() -> dict:
    r = _ssh(f"cat {CONFIG_PATH}")
    if r.returncode != 0:
        raise SystemExit(f"Failed to read {CONFIG_PATH}: {r.stderr.strip()}")
    try:
        cfg = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Live config is not valid JSON: {exc}")
    if not isinstance(cfg.get("data"), dict):
        raise SystemExit("Live config has no 'data' object — refusing to touch it")
    return cfg


def remote_mbtiles_present(bucket: str) -> bool:
    r = _ssh(f"test -f {TILES_DIR}/{bucket}.mbtiles && echo OK")
    return r.returncode == 0 and "OK" in r.stdout


def plan_changes(cfg: dict, buckets: list[str]) -> tuple[dict, list[str], list[str], list[str]]:
    """Return (merged_cfg, added, updated, skipped_missing) — original cfg untouched."""
    merged = json.loads(json.dumps(cfg))  # deep copy
    data = merged["data"]
    added, updated, skipped = [], [], []
    for b in buckets:
        if not remote_mbtiles_present(b):
            skipped.append(b)
            continue
        mbt = f"{b}.mbtiles"
        if b in data:
            # Entry-level merge: keep any existing fields, (re)assert mbtiles.
            if not isinstance(data[b], dict):
                data[b] = {}
            data[b]["mbtiles"] = mbt
            updated.append(b)
        else:
            data[b] = {"mbtiles": mbt}
            added.append(b)
    return merged, added, updated, skipped


def write_remote_config(merged: dict) -> str:
    """Atomically replace the remote config: stdin→tmp, validate, backup, mv.

    Returns the remote path of the pre-change backup, so the caller can restore
    it quickly if the server fails to come back cleanly.
    """
    payload = json.dumps(merged, indent=2, ensure_ascii=False)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{CONFIG_PATH}.bak.{ts}"
    # One shell: write tmp from stdin, JSON-validate it, back up current, atomic mv.
    remote = (
        f'cfg={CONFIG_PATH}; tmp="$cfg.new.$$"; '
        f'cat > "$tmp" && '
        f'python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$tmp" && '
        f'cp -p "$cfg" "{backup}" && '
        f'mv "$tmp" "$cfg" && echo WROTE_OK'
    )
    r = _ssh(remote, input_text=payload)
    if r.returncode != 0 or "WROTE_OK" not in r.stdout:
        raise SystemExit(f"Atomic config write failed (original untouched): "
                         f"rc={r.returncode} err={r.stderr.strip()} out={r.stdout.strip()}")
    print(f"  config.json rewritten (backup kept: {backup})")
    return backup


def rollback_config(backup: str) -> bool:
    """Restore a previously-kept backup over config.json (keeping the rejected
    config aside for inspection), then restart. For fast recovery when the new
    config left the server unhealthy."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    remote = (
        f'cfg={CONFIG_PATH}; '
        f'test -f "{backup}" && '
        f'cp -p "$cfg" "$cfg.rejected.{ts}" && '
        f'cp -p "{backup}" "$cfg" && echo ROLLED_BACK'
    )
    r = _ssh(remote)
    if r.returncode != 0 or "ROLLED_BACK" not in r.stdout:
        print(f"  ✗ ROLLBACK FAILED — manual intervention needed. Backup: {backup}  "
              f"err={r.stderr.strip()}", file=sys.stderr)
        return False
    print(f"  rolled back to {backup} (rejected config saved as config.json.rejected.{ts})")
    return restart_tileserver()


def restart_tileserver() -> bool:
    """Run the on-host restart script. Restart + verify in SEPARATE ssh sessions
    (the script runs `sudo pkill -f`; a combined session would self-kill)."""
    r = _ssh(RESTART_SCRIPT, timeout=180)
    if r.returncode != 0:
        print(f"  restart_services.sh rc={r.returncode}: {r.stderr.strip()}", file=sys.stderr)
        return False
    # Separate session for the verify (argv must not carry pkill's patterns).
    v = _ssh("echo gl:$(pgrep -cf tileserver-gl-light) tiler:$(pgrep -cf /srv/tiler/tiler.js)")
    print(f"  services after restart: {v.stdout.strip()}")
    return "gl:0" not in v.stdout


def verify_serving(buckets: list[str], *, attempts: int = 10, delay: float = 3.0) -> dict[str, bool]:
    """Confirm each tileset serves by cur-ling ``/data/<bucket>.json`` on the
    tileserver's own localhost (via SSH), with retries — so the check works from
    any pipeline host (CRC compute nodes may lack outbound HTTP) and the server
    has a few seconds to come up after restart."""
    results: dict[str, bool] = {}
    for b in buckets:
        path = f"http://localhost:{HTTP_PORT}/data/{b}.json"
        ok = False
        for _ in range(attempts):
            r = _ssh(f"curl -s -o /dev/null -w '%{{http_code}}' {path}", timeout=30)
            if r.returncode == 0 and r.stdout.strip() == "200":
                ok = True
                break
            time.sleep(delay)
        results[b] = ok
        print(f"  serving {b}: {'OK (200)' if ok else 'NOT SERVED'}  (localhost:{HTTP_PORT}/data/{b}.json)")
    return results


def update_tileserver_config(buckets: list[str], *, execute: bool = False,
                             do_restart: bool = True, do_verify: bool = True) -> bool:
    buckets = [b for b in dict.fromkeys(buckets) if b]  # dedupe, keep order
    if not buckets:
        print("No buckets specified — nothing to do.")
        return True

    print(f"Tileserver: {TILESERVER_USER}@{TILESERVER_HOST}  config={CONFIG_PATH}")
    print(f"Buckets to register: {', '.join(buckets)}  mode={'EXECUTE' if execute else 'DRY-RUN'}")

    cfg = read_remote_config()
    before = len(cfg["data"])
    merged, added, updated, skipped = plan_changes(cfg, buckets)

    print(f"  existing data entries: {before}")
    print(f"  add:     {', '.join(added) or '-'}")
    print(f"  update:  {', '.join(updated) or '-'}")
    print(f"  skipped (mbtiles missing on host): {', '.join(skipped) or '-'}")
    if skipped:
        print("  NOTE: skipped buckets have no .mbtiles on the host — push tiles first.")

    if not (added or updated):
        print("Nothing to write (no present buckets to add/update).")
        # Still verify the requested-and-present ones serve, if executing.
        return all(verify_serving([b for b in buckets if b not in skipped]).values()) if (execute and do_verify) else True

    if not execute:
        print("DRY-RUN: would rewrite config (preserving all other entries), "
              "restart, and verify. No changes made.")
        return True

    backup = write_remote_config(merged)

    # Any failure below (server didn't come back, or a tileset doesn't serve)
    # triggers an immediate rollback to the pre-change backup so the live server
    # is restored to its known-good state quickly.
    failure_reason = None
    if do_restart:
        print("Restarting tileserver ...")
        if not restart_tileserver():
            failure_reason = "tileserver did not come back cleanly"

    if failure_reason is None and do_verify:
        print("Verifying serving ...")
        results = verify_serving([b for b in (added + updated)])
        failed = [b for b, ok in results.items() if not ok]
        if failed:
            failure_reason = f"tilesets not served after restart: {', '.join(failed)}"

    if failure_reason is not None:
        print(f"  ✗ {failure_reason} — rolling back ...", file=sys.stderr)
        restored = rollback_config(backup) if do_restart else False
        print(f"  rollback {'restored prior config' if restored else 'INCOMPLETE — check the host'}.",
              file=sys.stderr)
        return False

    print("Done — tileserver config updated and serving confirmed.")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", "-b", action="append", required=True,
                    help="Tileset/bucket name to register (repeatable). mbtiles must be "
                         "named <bucket>.mbtiles and already pushed to the host.")
    ap.add_argument("--execute", action="store_true", help="Apply (default: dry-run)")
    ap.add_argument("--no-restart", dest="restart", action="store_false",
                    help="Rewrite config but do not restart (not recommended)")
    ap.add_argument("--no-verify", dest="verify", action="store_false",
                    help="Skip post-restart HTTP serving check")
    args = ap.parse_args()
    ok = update_tileserver_config(args.bucket, execute=args.execute,
                                  do_restart=args.restart, do_verify=args.verify)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
