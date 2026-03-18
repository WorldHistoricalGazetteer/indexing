#!/usr/bin/env python3
"""
Switch the DO Django app's Elasticsearch backend between local (DO) and remote (Pitt).

This script SSHes into the DO server and modifies the WHG Django
``local_settings.py`` so that ES queries are directed at the Pitt CRC
gateway (index.whgazetteer.org:9200) instead of the local DO ES instance.

The switch is fully reversible: a backup of the original settings is
stored on DO and a JSON state file records what was changed so that
``--revert`` can restore the original configuration exactly.

Architecture
~~~~~~~~~~~~
DO runs WHG in Docker via docker-compose.  Settings chain::

    env_template.py  →  load_env.py  →  .env/.env  →  docker-compose env
                                                           ↓
                                         Django settings.py reads os.environ
                                                           ↓
                                    local_settings.py overrides (Python-level)

We modify ``local_settings.py`` because it is a Python-level override that
takes effect inside the running container without touching the env pipeline.
This makes the change surgical and easy to revert.

The web container is restarted with::

    cd ~/sites/whgazetteer-org && \\
    docker-compose -f docker-compose-autocontext.yml \\
        --env-file ./.env/.env restart web

Prerequisites
~~~~~~~~~~~~~
  1. DNS: ``index.whgazetteer.org`` resolves to the Pitt CRC gateway
     (gazetteer.crcd.pitt.edu) and port 9200 is reachable from DO.
  2. SSH: the alias ``whg`` in ~/.ssh/config connects to the DO server.
  3. Pitt ES password is readable (auto-read from Pitt password file,
     or set PITT_ES_PASSWORD env var).

Usage::

  # Show current state and what would change (no modifications):
  python3 scripts/switch_do_es_backend.py --check

  # Switch DO → Pitt (interactive):
  python3 scripts/switch_do_es_backend.py --switch-to pitt

  # Switch back to local ES:
  python3 scripts/switch_do_es_backend.py --switch-to local
  # — or equivalently —
  python3 scripts/switch_do_es_backend.py --revert

  # Non-interactive:
  python3 scripts/switch_do_es_backend.py --switch-to pitt --yes

  # Dry run (show plan, no changes):
  python3 scripts/switch_do_es_backend.py --switch-to pitt --dry-run

  # Skip web-container restart (just change settings):
  python3 scripts/switch_do_es_backend.py --switch-to pitt --no-restart

Environment variables:
  PITT_ES_PASSWORD   Pitt ES password (auto-read from password file if unset)
  PITT_ES_HOST       Pitt ES gateway hostname (default: index.whgazetteer.org)
  PITT_ES_PORT       Pitt ES gateway port (default: 9200)
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DO_SSH_HOST = "whg"

# Paths on DO
DO_SITE_DIR = "/home/whgadmin/sites/whgazetteer-org"
DO_SETTINGS_PATH = f"{DO_SITE_DIR}/whg/local_settings.py"
DO_ENV_TEMPLATE = f"{DO_SITE_DIR}/server-admin/env_template.py"
DO_DOTENV = f"{DO_SITE_DIR}/.env/.env"
DO_SWITCH_STATE = f"{DO_SITE_DIR}/.es_backend_state.json"

# Docker-compose restart (only restarts the web container — not Celery, etc.)
DOCKER_COMPOSE = (
    f"cd {DO_SITE_DIR} && "
    "docker-compose -f docker-compose-autocontext.yml "
    "--env-file ./.env/.env"
)
RESTART_WEB_CMD = f"{DOCKER_COMPOSE} restart web"
RESTART_ALL_CMD = f"{DOCKER_COMPOSE} restart"

# Default Pitt gateway endpoint (DNS CNAME → gazetteer.crcd.pitt.edu)
PITT_ES_HOST = os.getenv("PITT_ES_HOST", "index.whgazetteer.org")
PITT_ES_PORT = int(os.getenv("PITT_ES_PORT", "9200"))
PITT_ES_USER = "elastic"


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_run(cmd, timeout=60, check=True):
    """Run a command on DO via SSH and return CompletedProcess."""
    result = subprocess.run(
        ["ssh", DO_SSH_HOST, cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"SSH command failed (rc={result.returncode}):\n"
            f"  cmd: {cmd}\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return result


def ssh_read_file(remote_path):
    """Read a file on DO via SSH."""
    return ssh_run(f"cat {remote_path}").stdout


def ssh_write_file(remote_path, content):
    """Write content to a file on DO via SSH (base64-safe)."""
    b64 = base64.b64encode(content.encode()).decode()
    ssh_run(
        f"echo '{b64}' | base64 -d > {remote_path}",
        timeout=15,
    )


def ssh_file_exists(remote_path):
    """Check if a file exists on DO."""
    return ssh_run(f"test -f {remote_path}", check=False).returncode == 0


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def get_pitt_password():
    """Read Pitt ES password from file or env."""
    pw = os.getenv("PITT_ES_PASSWORD")
    if pw:
        return pw
    pw_file = (
        Path(os.getenv("IX1_BASE", "/ix1/ishi")) / "es" / "config" / "elastic.password"
    )
    try:
        return pw_file.read_text().strip()
    except FileNotFoundError:
        return None


def extract_password(text, var="ELASTIC_PASSWORD"):
    """Extract a password assignment from Python/env text."""
    # Python: ELASTIC_PASSWORD = 'xxx' or "xxx"
    m = re.search(
        rf"^{var}\s*=\s*['\"](.+?)['\"]", text, re.MULTILINE
    )
    if m:
        return m.group(1)
    # Env: ELASTIC_PASSWORD=xxx (no quotes or with quotes)
    m = re.search(
        rf"^{var}=(.+)$", text, re.MULTILINE
    )
    if m:
        return m.group(1).strip().strip("'\"")
    return None


# ---------------------------------------------------------------------------
# Settings discovery
# ---------------------------------------------------------------------------

# Regex patterns for ES-related settings (Python and env-file styles)
ES_PATTERNS = {
    "ELASTIC_PASSWORD": re.compile(
        r"^(ELASTIC_PASSWORD)\s*=\s*['\"]?(.+?)['\"]?\s*$", re.MULTILINE
    ),
    "ES_CONN": re.compile(
        r"^(ES_CONN)\s*=\s*['\"]?(.+?)['\"]?\s*$", re.MULTILINE
    ),
    "ES_HOST": re.compile(
        r"^(ES_HOST)\s*=\s*['\"]?(.+?)['\"]?\s*$", re.MULTILINE
    ),
    "ES_PORT": re.compile(
        r"^(ES_PORT)\s*=\s*['\"]?(.+?)['\"]?\s*$", re.MULTILINE
    ),
    "ELASTICSEARCH_URL": re.compile(
        r"^(ELASTICSEARCH_URL)\s*=\s*['\"]?(.+?)['\"]?\s*$", re.MULTILINE
    ),
    "ELASTICSEARCH_DSL_hosts": re.compile(
        r"['\"]hosts['\"]\s*:\s*\[?\s*['\"](.+?)['\"]", re.MULTILINE
    ),
    # Bare localhost:9200 references (host networking to DO ES)
    "localhost_9200": re.compile(
        r"(localhost:9200|127\.0\.0\.1:9200)", re.MULTILINE
    ),
}


def discover_es_settings(text):
    """Find ES-related settings in text.  Returns {name: [matches]}."""
    found = {}
    for name, pat in ES_PATTERNS.items():
        matches = pat.findall(text)
        if matches:
            found[name] = matches
    return found


def print_discovered(label, text):
    """Discover and print ES settings in a file."""
    found = discover_es_settings(text)
    if not found:
        print(f"  {label}: (no ES settings found)")
        return found
    for name, matches in found.items():
        for m in matches:
            val = m[-1] if isinstance(m, tuple) else m
            # Mask passwords
            display = val if "PASSWORD" not in name else val[:3] + "***"
            print(f"  {label}: {name} = {display}")
    return found


# ---------------------------------------------------------------------------
# Connectivity checks
# ---------------------------------------------------------------------------

def check_do_to_pitt(pitt_password):
    """From DO, verify that Pitt ES gateway is reachable."""
    print(f"  Checking DO → Pitt ({PITT_ES_HOST}:{PITT_ES_PORT})...")
    cmd = (
        f"curl -s -o /dev/null -w '%{{http_code}}' "
        f"-u '{PITT_ES_USER}:{pitt_password}' "
        f"'http://{PITT_ES_HOST}:{PITT_ES_PORT}/_cluster/health' "
        f"--connect-timeout 10 --max-time 15"
    )
    result = ssh_run(cmd, timeout=30, check=False)
    code = result.stdout.strip().strip("'")
    if code == "200":
        print("    ✓ Pitt ES reachable (HTTP 200)")
        return True
    print(f"    ✗ Pitt ES unreachable (HTTP {code})")
    if result.stderr.strip():
        print(f"      {result.stderr.strip()}")
    return False


def check_do_local_es(settings_text=None):
    """From DO, verify that the bare-metal ES on DO is reachable.

    ES runs directly on the DO host (not in Docker).  The Docker
    containers reach it via ``ES_HOST`` (the host's external IP).
    We try the configured ES_HOST first, then localhost as a fallback,
    with and without basic-auth.
    """
    print("  Checking DO local ES (bare-metal)...")

    # Discover ES_HOST and password from settings if available
    es_host = "144.126.204.70"  # known default
    es_port = "9200"
    do_password = None
    if settings_text:
        m = re.search(r"^ES_HOST\s*=\s*['\"](.+?)['\"]", settings_text, re.MULTILINE)
        if m:
            es_host = m.group(1)
        m = re.search(r"^ES_PORT\s*=\s*['\"]?(\d+)", settings_text, re.MULTILINE)
        if m:
            es_port = m.group(1)
        do_password = extract_password(settings_text)

    # Build list of (host:port, auth_flag) attempts
    targets = [f"{es_host}:{es_port}"]
    if es_host != "localhost":
        targets.append(f"localhost:{es_port}")

    for host in targets:
        for auth in ([f"-u 'elastic:{do_password}'"] if do_password else []) + [""]:
            cmd = (
                f"curl -s -o /dev/null -w '%{{http_code}}' {auth} "
                f"'http://{host}/_cluster/health' "
                "--connect-timeout 5 --max-time 10"
            )
            result = ssh_run(cmd, timeout=20, check=False)
            code = result.stdout.strip().strip("'")
            if code == "200":
                print(f"    ✓ Local ES reachable at {host} (HTTP 200)")
                return True
            if code == "401":
                # ES is there but needs auth — still counts as reachable
                print(f"    ✓ Local ES reachable at {host} (HTTP 401, auth required)")
                return True
    print("    ✗ Local ES unreachable")
    return False


def check_do_docker():
    """Check docker containers on DO."""
    print("  Checking Docker containers...")
    result = ssh_run(
        f"cd {DO_SITE_DIR} && docker-compose "
        "-f docker-compose-autocontext.yml --env-file ./.env/.env ps 2>/dev/null "
        "|| docker ps --format '{{{{.Names}}}} {{{{.Status}}}}'",
        timeout=20, check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.strip().split("\n")[:8]:
            print(f"    {line.strip()[:80]}")
        return True
    print("    ⚠  Could not list containers")
    return False


def check_do_django_health():
    """Quick health check of the Django app on DO."""
    print("  Checking Django app...")
    cmd = (
        "curl -s -o /dev/null -w '%{http_code}' "
        "'http://localhost/api/' "
        "--connect-timeout 5 --max-time 10"
    )
    result = ssh_run(cmd, timeout=20, check=False)
    code = result.stdout.strip().strip("'")
    if code and code != "000":
        print(f"    ✓ Django responding (HTTP {code})")
        return True
    print(f"    ⚠  Django not responding (HTTP {code})")
    return False


# ---------------------------------------------------------------------------
# State management (JSON sidecar on DO)
# ---------------------------------------------------------------------------

def read_switch_state():
    """Read the switch state file from DO (if it exists)."""
    if not ssh_file_exists(DO_SWITCH_STATE):
        return None
    try:
        return json.loads(ssh_read_file(DO_SWITCH_STATE))
    except Exception:
        return None


def write_switch_state(state):
    """Write switch state JSON to DO."""
    ssh_write_file(DO_SWITCH_STATE, json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Settings rewriting  (targets local_settings.py)
# ---------------------------------------------------------------------------

BLOCK_START = "# --- ES Backend Switch (managed by switch_do_es_backend.py) ---"
BLOCK_END   = "# --- End ES Backend Switch ---"


def build_pitt_block(pitt_password, original_password):
    """Python block to inject into local_settings.py for Pitt ES.

    Overrides ES_HOST, ES_PORT, ELASTIC_PASSWORD and reconstructs
    ES_CONN as an ``Elasticsearch(...)`` client pointing at the Pitt
    gateway — matching the constructor style used in the original
    local_settings.py.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return textwrap.dedent(f"""\
        {BLOCK_START}
        # Switched to Pitt CRC Elasticsearch on {ts}
        # Original ELASTIC_PASSWORD preserved as _DO_ELASTIC_PASSWORD below.
        # To revert:  python3 scripts/switch_do_es_backend.py --revert
        #         or: es -do-revert
        _DO_ELASTIC_PASSWORD = '{original_password}'  # original DO password (for revert)
        ELASTIC_PASSWORD = '{pitt_password}'
        ES_HOST = '{PITT_ES_HOST}'
        ES_PORT = {PITT_ES_PORT}
        from elasticsearch import Elasticsearch as _Es
        ES_CONN = _Es(
            [f'http://{{ES_HOST}}:{{ES_PORT}}'],
            basic_auth=('elastic', ELASTIC_PASSWORD),
            request_timeout=30,
        )
        {BLOCK_END}
    """)


def build_local_block(original_password):
    """Marker block after reverting to local ES."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return textwrap.dedent(f"""\
        {BLOCK_START}
        # Reverted to local DO Elasticsearch on {ts}
        ELASTIC_PASSWORD = '{original_password}'
        {BLOCK_END}
    """)


def remove_managed_block(text):
    """Strip any previously injected managed block."""
    pat = re.compile(
        rf"^{re.escape(BLOCK_START)}.*?^{re.escape(BLOCK_END)}\s*\n?",
        re.MULTILINE | re.DOTALL,
    )
    return pat.sub("", text)


# Names whose top-level assignments we comment out
_COMMENT_VARS = {"ELASTIC_PASSWORD", "ES_CONN", "ES_HOST", "ES_PORT", "ELASTICSEARCH_URL"}


def comment_out_originals(text):
    """Comment out ES setting assignments, including multi-line ones.

    Handles single-line assignments like ``ES_HOST = '...'`` as well as
    multi-line constructors like::

        ES_CONN = Elasticsearch(
            ...
        )

    Returns (new_text, [first_line_of_each_commented_block]).
    """
    commented = []
    out = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        # Check if this line starts a relevant assignment
        m = re.match(r"^(\w+)\s*=", stripped)
        if m and m.group(1) in _COMMENT_VARS:
            commented.append(stripped)
            # Determine whether this is a multi-line statement by checking
            # if parens/brackets are balanced on this line.
            depth = _paren_depth(lines[i])
            out.append(f"# [es-switch-commented] {lines[i]}")
            # Keep commenting continuation lines until parens are balanced
            while depth > 0 and i + 1 < len(lines):
                i += 1
                depth += _paren_depth(lines[i])
                out.append(f"# [es-switch-commented] {lines[i]}")
        else:
            out.append(lines[i])
        i += 1
    return "\n".join(out), commented


def _paren_depth(line):
    """Net bracket/paren depth change for *line* (ignoring strings)."""
    depth = 0
    for ch in line:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
    return depth


def uncomment_originals(text):
    """Restore lines previously commented by comment_out_originals."""
    return re.sub(
        r"^# \[es-switch-commented] (.*)$",
        r"\1",
        text,
        flags=re.MULTILINE,
    )


def apply_pitt_backend(text, pitt_pw, orig_pw):
    """Return (new_text, commented_lines) with Pitt ES settings applied."""
    text = remove_managed_block(text)
    text, commented = comment_out_originals(text)
    block = build_pitt_block(pitt_pw, orig_pw)
    return text.rstrip("\n") + "\n\n" + block + "\n", commented


def apply_local_backend(text, orig_pw):
    """Return new_text with local ES settings restored."""
    text = remove_managed_block(text)
    text = uncomment_originals(text)
    block = build_local_block(orig_pw)
    return text.rstrip("\n") + "\n\n" + block + "\n"


# ---------------------------------------------------------------------------
# Docker restart
# ---------------------------------------------------------------------------

def restart_web_container(restart_cmd=None):
    """Restart the web Docker container on DO."""
    cmd = restart_cmd or RESTART_WEB_CMD
    print(f"  Running: {cmd}")
    result = ssh_run(cmd, timeout=120, check=False)
    if result.returncode == 0:
        print("    ✓ Container restarted")
        if result.stdout.strip():
            for line in result.stdout.strip().split("\n")[:3]:
                print(f"    {line.strip()}")
        return True
    print(f"    ✗ Restart failed (rc={result.returncode})")
    if result.stderr.strip():
        print(f"      {result.stderr.strip()[:200]}")
    print("    ⚠  Restart manually on DO:")
    print(f"      {RESTART_WEB_CMD}")
    return False


# ---------------------------------------------------------------------------
# Main operations
# ---------------------------------------------------------------------------

def do_check(args):
    """Show current state without making changes."""
    print("=" * 60)
    print("ES BACKEND STATUS CHECK")
    print("=" * 60)

    # Read settings files
    print("\n--- Settings discovery ---")
    ls_text = None
    files_to_check = {
        "local_settings.py": DO_SETTINGS_PATH,
        "env_template.py":   DO_ENV_TEMPLATE,
        ".env/.env":         DO_DOTENV,
    }
    for label, path in files_to_check.items():
        try:
            text = ssh_read_file(path)
            if label == "local_settings.py":
                ls_text = text
            print_discovered(label, text)
        except Exception:
            print(f"  {label}: (not found or unreadable)")

    # Check for managed block in local_settings.py
    if ls_text is not None:
        if BLOCK_START in ls_text:
            print("\n  ℹ  Managed block present (settings were modified by this script)")
        else:
            print("\n  ℹ  No managed block in local_settings.py")

    # Switch state
    print("\n--- Switch state ---")
    state = read_switch_state()
    if state:
        print(f"  Current backend: {state.get('backend', 'unknown')}")
        print(f"  Switched at:     {state.get('timestamp', 'unknown')}")
        print(f"  Backup file:     {state.get('backup_path', 'none')}")
    else:
        print("  No switch state file (never switched, or state cleared)")

    # Connectivity
    print("\n--- Connectivity ---")
    check_do_local_es(ls_text)

    pitt_pw = get_pitt_password()
    if pitt_pw:
        check_do_to_pitt(pitt_pw)
    else:
        print("  ⚠  Cannot check Pitt connectivity (no password available)")

    print("\n--- Docker ---")
    check_do_docker()
    check_do_django_health()


def do_switch_to_pitt(args):
    """Switch DO to use Pitt ES backend."""
    pitt_password = args.pitt_password or get_pitt_password()
    if not pitt_password:
        print("ERROR: Pitt ES password required.")
        print("  Set PITT_ES_PASSWORD or ensure /ix1/ishi/es/config/elastic.password exists.")
        sys.exit(1)

    print("=" * 60)
    print("SWITCH DO → PITT ES")
    print("=" * 60)

    # 1. Read current settings
    print("\n1. Reading current settings...")
    settings_text = ssh_read_file(DO_SETTINGS_PATH)
    print_discovered("local_settings.py", settings_text)

    # Also show env_template for awareness
    try:
        env_text = ssh_read_file(DO_ENV_TEMPLATE)
        print_discovered("env_template.py", env_text)
    except Exception:
        pass

    # Extract original DO password
    original_password = extract_password(settings_text)
    if not original_password:
        # Try .env/.env
        try:
            dotenv_text = ssh_read_file(DO_DOTENV)
            original_password = extract_password(dotenv_text)
            if original_password:
                print("  (ELASTIC_PASSWORD found in .env/.env)")
        except Exception:
            pass
    if not original_password:
        state = read_switch_state()
        if state and state.get("original_password"):
            original_password = state["original_password"]
            print("  (Using original password from switch state)")
    if not original_password:
        print("  ⚠  Could not find ELASTIC_PASSWORD.")
        original_password = input(
            "  Enter original DO ES password (or Enter to skip): "
        ).strip() or ""

    # 2. Pre-flight checks
    print("\n2. Pre-flight checks...")
    pitt_ok = check_do_to_pitt(pitt_password)
    local_ok = check_do_local_es(settings_text)

    if not pitt_ok:
        print("\n  ✗ Pitt ES is not reachable from DO. Cannot proceed.")
        print("    Check DNS, firewall, and that the gateway is running on Pitt.")
        sys.exit(1)

    if not local_ok:
        print("  ⚠  Local ES is not reachable (may already be stopped).")

    # 3. Show plan
    print(f"\n3. Plan:")
    print(f"  • Back up {DO_SETTINGS_PATH}")
    print(f"  • Comment out original ES settings in local_settings.py")
    print(f"  • Add managed block pointing to http://{PITT_ES_HOST}:{PITT_ES_PORT}")
    print(f"  • Update ELASTIC_PASSWORD to Pitt credentials")
    if not args.no_restart:
        print(f"  • Restart web container via docker-compose")
    print(f"  • Verify Django health")

    if args.dry_run:
        new_text, commented = apply_pitt_backend(
            settings_text, pitt_password, original_password
        )
        print(f"\n  (dry run — would comment out: {commented})")
        print(f"\n  Managed block that would be added:")
        block = build_pitt_block(pitt_password, original_password)
        for line in block.split("\n"):
            print(f"    {line}")
        print(f"\n  Restart command that would run:")
        print(f"    {RESTART_WEB_CMD}")
        print("\n(dry run — no changes made)")
        return

    if not args.yes:
        reply = input("\nProceed? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            sys.exit(0)

    # 4. Backup
    print("\n4. Creating backup...")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{DO_SETTINGS_PATH}.pre_pitt_{ts}"
    ssh_run(f"cp {DO_SETTINGS_PATH} {backup_path}")
    print(f"  ✓ Backed up to {backup_path}")

    # 5. Apply changes
    print("\n5. Applying Pitt ES settings to local_settings.py...")
    new_text, commented = apply_pitt_backend(
        settings_text, pitt_password, original_password
    )
    ssh_write_file(DO_SETTINGS_PATH, new_text)
    print(f"  ✓ Settings updated (commented out: {commented})")

    # 6. Write state
    state = {
        "backend": "pitt",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backup_path": backup_path,
        "original_password": original_password,
        "pitt_host": PITT_ES_HOST,
        "pitt_port": PITT_ES_PORT,
    }
    write_switch_state(state)
    print(f"  ✓ State saved to {DO_SWITCH_STATE}")

    # 7. Restart
    if not args.no_restart:
        print("\n6. Restarting web container...")
        restart_web_container(args.restart_cmd)
        print("  Waiting 5s for container to settle...")
        time.sleep(5)

    # 8. Post-switch health check
    print("\n7. Post-switch verification...")
    check_do_django_health()

    print("\n" + "=" * 60)
    print("SWITCH COMPLETE: DO is now using Pitt ES")
    print("=" * 60)
    print(f"\n  To revert:  python3 scripts/switch_do_es_backend.py --revert")
    print(f"       or:    es -do-revert")
    print(f"  To check:   python3 scripts/switch_do_es_backend.py --check")
    print(f"       or:    es -do-check")


def do_switch_to_local(args):
    """Switch DO back to local ES (revert)."""
    print("=" * 60)
    print("REVERT: DO → LOCAL ES")
    print("=" * 60)

    # Read current state
    state = read_switch_state()
    if state and state.get("backend") == "local":
        print("\n  ℹ  DO is already set to local ES (according to state file).")
        if not args.yes:
            reply = input("  Proceed anyway? [y/N] ").strip().lower()
            if reply != "y":
                print("Aborted.")
                return

    # 1. Read current settings
    print("\n1. Reading current settings...")
    settings_text = ssh_read_file(DO_SETTINGS_PATH)
    print_discovered("local_settings.py", settings_text)

    # Determine original password (check multiple sources)
    original_password = None
    if state and state.get("original_password"):
        original_password = state["original_password"]
        print("  (Original DO password found in state file)")
    if not original_password:
        m = re.search(r"_DO_ELASTIC_PASSWORD\s*=\s*'(.+?)'", settings_text)
        if m:
            original_password = m.group(1)
            print("  (Original DO password found in managed block)")
    if not original_password and state and state.get("backup_path"):
        try:
            backup_text = ssh_read_file(state["backup_path"])
            original_password = extract_password(backup_text)
            if original_password:
                print("  (Original DO password found in backup file)")
        except Exception:
            pass
    if not original_password:
        print("  ⚠  Could not determine original DO ES password.")
        original_password = input("  Enter original DO ES password: ").strip()
        if not original_password:
            print("ERROR: Cannot revert without original password.")
            sys.exit(1)

    # Prefer restoring the backup file (exact restore) if available
    use_backup = False
    backup_path = state.get("backup_path") if state else None
    if backup_path and ssh_file_exists(backup_path):
        print(f"\n  Backup file available: {backup_path}")
        use_backup = True

    # 2. Show plan
    print(f"\n2. Plan:")
    if use_backup:
        print(f"  • Restore {backup_path} → {DO_SETTINGS_PATH}")
    else:
        print(f"  • Remove managed block from local_settings.py")
        print(f"  • Uncomment original ES settings")
        print(f"  • Restore ELASTIC_PASSWORD to DO value")
    if not args.no_restart:
        print(f"  • Restart web container via docker-compose")
    print(f"  • Verify Django health")

    if args.dry_run:
        if not use_backup:
            new_text = apply_local_backend(settings_text, original_password)
            print("\n  Resulting local_settings.py tail:")
            for line in new_text.strip().split("\n")[-8:]:
                print(f"    {line}")
        print(f"\n  Restart command that would run:")
        print(f"    {RESTART_WEB_CMD}")
        print("\n(dry run — no changes made)")
        return

    if not args.yes:
        reply = input("\nProceed? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            sys.exit(0)

    # 3. Apply revert
    print("\n3. Reverting settings...")
    if use_backup:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ssh_run(f"cp {DO_SETTINGS_PATH} {DO_SETTINGS_PATH}.pre_revert_{ts}")
        ssh_run(f"cp {backup_path} {DO_SETTINGS_PATH}")
        print("  ✓ Restored from backup")
    else:
        new_text = apply_local_backend(settings_text, original_password)
        ssh_write_file(DO_SETTINGS_PATH, new_text)
        print("  ✓ Settings updated (removed managed block, restored originals)")

    # Update state
    state = {
        "backend": "local",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "original_password": original_password,
        "reverted_from": "pitt",
    }
    write_switch_state(state)
    print(f"  ✓ State updated")

    # 4. Restart
    if not args.no_restart:
        print("\n4. Restarting web container...")
        restart_web_container(args.restart_cmd)
        print("  Waiting 5s for container to settle...")
        time.sleep(5)

    # 5. Post-revert checks
    print("\n5. Post-revert verification...")
    try:
        restored_text = ssh_read_file(DO_SETTINGS_PATH)
    except Exception:
        restored_text = None
    check_do_local_es(restored_text)
    check_do_django_health()

    print("\n" + "=" * 60)
    print("REVERT COMPLETE: DO is now using local ES")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Switch DO Django ES backend between local and Pitt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --check                      # show current state
              %(prog)s --switch-to pitt              # switch to Pitt ES
              %(prog)s --switch-to local             # switch back to local ES
              %(prog)s --revert                      # alias for --switch-to local
              %(prog)s --switch-to pitt --dry-run    # show plan without changes
              %(prog)s --switch-to pitt --no-restart # change settings only
        """),
    )

    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--check", action="store_true",
        help="Show current state without making changes",
    )
    action.add_argument(
        "--switch-to", choices=["pitt", "local"],
        help="Switch ES backend to pitt or local",
    )
    action.add_argument(
        "--revert", action="store_true",
        help="Revert to local ES (alias for --switch-to local)",
    )

    p.add_argument(
        "--pitt-password", default=None,
        help="Pitt ES password (auto-detected from Pitt password file if unset)",
    )
    p.add_argument(
        "--restart-cmd", default=None,
        help=(
            "Custom command to restart Django on DO "
            "(default: docker-compose ... restart web)"
        ),
    )
    p.add_argument(
        "--no-restart", action="store_true",
        help="Skip container restart (just change settings files)",
    )
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    p.add_argument("--dry-run", action="store_true", help="Show plan without executing")

    return p.parse_args()


def main():
    args = parse_args()

    # Verify SSH connectivity first
    print("Connecting to DO server...")
    try:
        result = ssh_run("echo ok", timeout=10)
        if result.stdout.strip() != "ok":
            raise RuntimeError("Unexpected SSH output")
        print(f"  ✓ SSH to {DO_SSH_HOST} OK\n")
    except Exception as e:
        print(f"  ✗ Cannot SSH to {DO_SSH_HOST}: {e}")
        print("  Check your ~/.ssh/config and SSH keys.")
        sys.exit(1)

    if args.check:
        do_check(args)
    elif args.revert or args.switch_to == "local":
        do_switch_to_local(args)
    elif args.switch_to == "pitt":
        do_switch_to_pitt(args)


if __name__ == "__main__":
    main()


