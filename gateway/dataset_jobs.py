# gateway/dataset_jobs.py
"""VM-local detached jobs for ONE contributed WHG dataset (place#341).

The gateway runs on the Pitt VM as user ``gazetteer`` with no Slurm and no
route to CRC, so a ``POST /api/registry/reingest`` for ``whg:<id>`` cannot
``sbatch`` anything. Instead it launches a detached, low-priority process on
the VM itself — ``python -m processing.local_dataset_job`` — and tracks it
through a small on-disk state directory. This module is the state machine and
the launcher; it is imported by the async gateway route AND by the runner, so
it must stay light: stdlib only, no ``processing.settings``, no Elasticsearch.

On disk (``REINGEST_STATE_DIR``, default ``<repo>/.reingest_jobs/``):

* ``<job_id>.json`` — one file per job, written atomically (tmp + rename):
  ``job_id, namespace, dataset_id, action, status, reason, pid, pgid,
  created_at, started_at, finished_at, message, log``. ``status`` is one of
  ``queued | running | completed | failed``; ``reason`` is a stable
  machine-readable code (:data:`REASONS`) that is ``null`` unless ``failed``.
* ``.lock`` — ONE global lock (a dataset job is a serial activity on a VM that
  also hosts production ES). Taken with ``O_CREAT|O_EXCL`` — ``flock`` is
  unusable on ``/vast`` — and carrying ``{pid, job_id, namespace, started}``.
  It is stale, and broken on the next attempt, when its pid is dead or it is
  older than the wall-clock cap plus a grace period.

Job ids are millisecond timestamps (``int(time.time()*1000)``): thirteen
digits, so they pass the GET route's ``\\d+`` regex and
``index_namespace.validate_job_id``, and they never collide with a Slurm id
(:data:`LOCAL_JOB_ID_MIN` is the dividing line the GET route uses to decide
which store to read).

A state file stuck in ``running``/``queued`` whose pid is dead is reported as
``failed`` with reason ``runner_died`` the first time it is read — a job is
never left running forever because its runner was OOM-killed between two
state writes. The marker ``indexed.json`` that ``processing.index_namespace``
writes is a SEPARATE signal and is never consulted for ``status``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

STATE_DIR_ENV = "REINGEST_STATE_DIR"
LOG_DIR_ENV = "REINGEST_LOG_DIR"
JOB_PYTHON_ENV = "REINGEST_JOB_PYTHON"
TIMEOUT_ENV = "REINGEST_JOB_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 1800
#: Added to the wall-clock cap before an apparently-live lock is called stale.
LOCK_GRACE_S = 300
#: A ``queued`` state with no pid yet is the gateway between "state written"
#: and "Popen returned"; older than this with still no pid, the gateway died.
SPAWN_GRACE_S = 120
#: Local job ids are ms timestamps (>= 10**12 since 2001); Slurm ids are far smaller.
LOCAL_JOB_ID_MIN = 10 ** 12
LOCK_FILENAME = ".lock"
RUNNER_MODULE = "processing.local_dataset_job"
STATE_SCHEMA = "whg-dataset-job/1"

ACTIONS = ("index", "retract")
DEFAULT_ACTION = "index"
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("completed", "failed")

# -- machine-readable failure reasons ------------------------------------------
REASON_AREAL_GEOMETRY = "areal_geometry"
REASON_NOT_INDEXABLE = "not_indexable"
REASON_STILL_PUBLISHED = "still_published"
REASON_NO_DOCS_STAGED = "no_docs_staged"
REASON_DRY_RUN_UNCLEAN = "dry_run_unclean"
REASON_TIMEOUT = "timeout"
REASON_KILLED = "killed"
REASON_RUNNER_DIED = "runner_died"
REASON_DISCOVERY_UNREACHABLE = "discovery_unreachable"
REASON_STAGE_ERROR = "stage_error"
REASON_INDEX_ERROR = "index_error"
#: Not in the agreed list: the runner found the global lock held by ANOTHER
#: live job when it started (only reachable if the gateway died between
#: taking the lock and recording the child's pid, and a second POST then
#: broke the lock). Fail-closed and counted rather than folded into a code
#: that would misdescribe it.
REASON_LOCK_CONFLICT = "lock_conflict"

REASONS = frozenset({
    REASON_AREAL_GEOMETRY, REASON_NOT_INDEXABLE, REASON_STILL_PUBLISHED,
    REASON_NO_DOCS_STAGED, REASON_DRY_RUN_UNCLEAN, REASON_TIMEOUT, REASON_KILLED,
    REASON_RUNNER_DIED, REASON_DISCOVERY_UNREACHABLE, REASON_STAGE_ERROR,
    REASON_INDEX_ERROR, REASON_LOCK_CONFLICT,
})


class JobActive(Exception):
    """A job is already queued/running; the gateway answers 409 with these."""

    def __init__(self, job_id: Optional[str], namespace: Optional[str]):
        self.job_id = job_id
        self.namespace = namespace
        super().__init__(f"job {job_id} for {namespace} is active")


class LockConflict(Exception):
    """The lock is held by a different, live job."""


# ---------------------------------------------------------------------------
# Paths and small helpers
# ---------------------------------------------------------------------------

def state_dir() -> Path:
    return Path(os.environ.get(STATE_DIR_ENV) or (REPO_ROOT / ".reingest_jobs"))


def log_dir() -> Path:
    return Path(os.environ.get(LOG_DIR_ENV) or (REPO_ROOT / "logs"))


def lock_path() -> Path:
    return state_dir() / LOCK_FILENAME


def state_path(job_id: str) -> Path:
    return state_dir() / f"{validate_job_id(job_id)}.json"


def log_path_for(job_id: str) -> Path:
    return log_dir() / f"dataset-job-{validate_job_id(job_id)}.log"


def timeout_s() -> int:
    try:
        return max(1, int(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(text: Any) -> Optional[datetime]:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_s(text: Any) -> Optional[float]:
    dt = _parse_iso(text)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def validate_job_id(job_id: Any) -> str:
    jid = str(job_id).strip()
    if not jid.isdigit():
        raise ValueError(f"job id is not digits: {job_id!r}")
    return jid


def validate_dataset_id(dataset_id: Any) -> str:
    ds = str(dataset_id).strip()
    if not ds.isdigit():
        raise ValueError(f"dataset id is not digits: {dataset_id!r}")
    return ds


def validate_action(action: Any) -> str:
    act = DEFAULT_ACTION if action is None else str(action).strip().lower()
    if act not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}, got {action!r}")
    return act


def is_local_job_id(job_id: Any) -> bool:
    """Digits and >= :data:`LOCAL_JOB_ID_MIN` (an array suffix is ignored)."""
    bare = str(job_id).strip().split("_", 1)[0]
    return bare.isdigit() and int(bare) >= LOCAL_JOB_ID_MIN


def new_job_id() -> str:
    """``int(time.time()*1000)`` as a string, bumped past any id already on disk."""
    jid = int(time.time() * 1000)
    while state_path(str(jid)).exists():
        jid += 1
    return str(jid)


def pid_alive(pid: Any) -> bool:
    """``os.kill(pid, 0)``: a process we may not signal still counts as alive."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------

def read_state(job_id: str) -> Optional[dict]:
    """The raw state file, or ``None`` when absent/unreadable. No side effects."""
    try:
        return _read_json(state_path(job_id))
    except ValueError:
        return None


def create_state(job_id: str, dataset_id: str, action: str) -> dict:
    state = {
        "schema": STATE_SCHEMA,
        "job_id": validate_job_id(job_id),
        "namespace": f"whg:{validate_dataset_id(dataset_id)}",
        "dataset_id": validate_dataset_id(dataset_id),
        "action": validate_action(action),
        "status": "queued",
        "reason": None,
        "pid": None,
        "pgid": None,
        "created_at": now_iso(),
        "started_at": None,
        "finished_at": None,
        "message": None,
        "log": str(log_path_for(job_id)),
    }
    _write_atomic(state_path(job_id), state)
    return state


def update_state(job_id: str, **changes: Any) -> dict:
    """Merge ``changes`` into the state file atomically; returns the new state.

    A ``status`` outside the vocabulary, or a ``reason`` outside
    :data:`REASONS`, is a programming error and raises rather than being
    written for the gateway to pass through.
    """
    state = read_state(job_id)
    if state is None:
        raise FileNotFoundError(f"no state file for job {job_id}")
    if "status" in changes and changes["status"] not in ACTIVE_STATUSES + TERMINAL_STATUSES:
        raise ValueError(f"bad status {changes['status']!r}")
    if changes.get("reason") is not None and changes["reason"] not in REASONS:
        raise ValueError(f"bad reason {changes['reason']!r}")
    state.update(changes)
    if state.get("status") != "failed":
        state["reason"] = None
    _write_atomic(state_path(job_id), state)
    return state


def mark_terminal(job_id: str, status: str, *, message: Optional[str],
                  reason: Optional[str] = None) -> Optional[dict]:
    """Set a terminal status, unless one is already recorded (first writer wins
    — a ``--kill`` and the runner's own SIGTERM handler both reach here)."""
    state = read_state(job_id)
    if state is None:
        return None
    if state.get("status") in TERMINAL_STATUSES:
        return state
    return update_state(job_id, status=status, reason=reason, message=message,
                        finished_at=now_iso())


def list_job_ids() -> list[str]:
    d = state_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if p.stem.isdigit())


def load_job(job_id: str) -> Optional[dict]:
    """The state as the gateway should report it.

    An active state whose runner is gone — pid dead, or no pid recorded within
    :data:`SPAWN_GRACE_S` of creation — is rewritten as ``failed`` /
    ``runner_died`` here, and the lock it may still hold is released, so a
    crashed runner is never reported ``running`` forever.
    """
    state = read_state(job_id)
    if state is None:
        return None
    if state.get("status") in ACTIVE_STATUSES:
        pid = state.get("pid")
        if pid is not None:
            dead = not pid_alive(pid)
        else:
            age = _age_s(state.get("created_at"))
            dead = state.get("status") == "queued" and (age is None or age > SPAWN_GRACE_S)
        if dead:
            state = update_state(job_id, status="failed", reason=REASON_RUNNER_DIED,
                                 message="runner died", finished_at=now_iso())
            release_lock(job_id)
    return state


def active_job() -> Optional[dict]:
    """The queued/running job with a live runner, or ``None``."""
    live = [j for j in (load_job(jid) for jid in list_job_ids())
            if j and j.get("status") in ACTIVE_STATUSES]
    if not live:
        return None
    lock = read_lock()
    if lock:
        for j in live:
            if j.get("job_id") == lock.get("job_id"):
                return j
    live.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
    return live[0]


# ---------------------------------------------------------------------------
# The global lock
# ---------------------------------------------------------------------------

def read_lock() -> Optional[dict]:
    return _read_json(lock_path())


def lock_is_stale(lock: Optional[dict], *, path: Optional[Path] = None) -> bool:
    """Dead holder, or older than the wall-clock cap + grace.

    An unparsable lock is one being written right now unless it is old. A
    dead lock pid whose job's state file names a LIVE pid is the hand-over
    window (gateway took the lock, spawned, and died before recording the
    child) — not stale.
    """
    path = path or lock_path()
    if lock is None:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age > LOCK_GRACE_S
    age = _age_s(lock.get("started"))
    if age is not None and age > timeout_s() + LOCK_GRACE_S:
        return True
    if pid_alive(lock.get("pid")):
        return False
    job_id = lock.get("job_id")
    if isinstance(job_id, str) and job_id.isdigit():
        state = read_state(job_id)
        if state and pid_alive(state.get("pid")):
            return False
    return True


def _write_lock(payload: dict, *, exclusive: bool) -> bool:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        return True
    _write_atomic(path, payload)
    return True


def acquire_lock(job_id: str, namespace: str, pid: int) -> bool:
    """Take the lock for ``job_id``; break a stale one once. ``False`` if held."""
    payload = {"pid": int(pid), "job_id": validate_job_id(job_id),
               "namespace": namespace, "started": now_iso()}
    for attempt in (1, 2):
        if _write_lock(payload, exclusive=True):
            return True
        if attempt == 1 and lock_is_stale(read_lock()):
            try:
                lock_path().unlink()
            except FileNotFoundError:
                pass
            continue
        return False
    return False


def release_lock(job_id: str) -> bool:
    """Unlink the lock if it names ``job_id`` (or is unreadable). ``True`` if removed."""
    lock = read_lock()
    if lock is not None and lock.get("job_id") != str(job_id):
        return False
    try:
        lock_path().unlink()
    except FileNotFoundError:
        return False
    return True


def rewrite_lock(job_id: str, pid: int) -> dict:
    """Hand the lock to ``pid`` — the gateway after ``Popen``, the runner at
    start. Re-acquires if nobody holds it; raises :class:`LockConflict` if a
    different live job does."""
    jid = validate_job_id(job_id)
    lock = read_lock()
    if lock is None or lock.get("job_id") == jid:
        namespace = (lock or {}).get("namespace")
        if namespace is None:
            state = read_state(jid) or {}
            namespace = state.get("namespace")
        payload = {"pid": int(pid), "job_id": jid, "namespace": namespace,
                   "started": (lock or {}).get("started") or now_iso()}
        _write_lock(payload, exclusive=False)
        return payload
    if lock_is_stale(lock):
        try:
            lock_path().unlink()
        except FileNotFoundError:
            pass
        if acquire_lock(jid, (read_state(jid) or {}).get("namespace") or "", pid):
            return read_lock() or {}
    raise LockConflict(f"lock held by job {lock.get('job_id')} (pid {lock.get('pid')})")


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def job_python() -> str:
    return os.environ.get(JOB_PYTHON_ENV) or sys.executable


def build_argv(job_id: str, dataset_id: str, action: str, *, which=shutil.which) -> list[str]:
    """``nice -n 19 ionice -c 3 <python> -m processing.local_dataset_job ...``;
    ``ionice`` (and ``nice``) are dropped when absent rather than failing."""
    argv: list[str] = []
    if which("nice"):
        argv += ["nice", "-n", "19"]
    if which("ionice"):
        argv += ["ionice", "-c", "3"]
    argv += [job_python(), "-m", RUNNER_MODULE,
             "--job-id", validate_job_id(job_id),
             "--dataset", validate_dataset_id(dataset_id),
             "--action", validate_action(action)]
    return argv


def _default_elastic_pass_file() -> str:
    """Same preference as ``gateway.config`` (the /vast copy survives an /ix1
    outage), without importing it — this module must stay import-light."""
    ix3 = os.environ.get("IX3_BASE", "/vast/ishi")
    ix1 = os.environ.get("IX1_BASE", "/ix1/ishi")
    vast = f"{ix3}/es/config/elastic.password"
    return vast if os.path.exists(vast) else f"{ix1}/es/config/elastic.password"


def job_env() -> dict[str, str]:
    env = dict(os.environ)
    root = str(REPO_ROOT)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("ELASTIC_PASS_FILE", _default_elastic_pass_file())
    return env


def _reap_in_background(proc: Any) -> None:
    """Wait on the child from a daemon thread so it is reaped and never a
    zombie, without the request (or the event loop) waiting on it."""
    def _wait():
        try:
            proc.wait()
        except Exception:  # noqa: BLE001 — reaping is best-effort
            pass
    threading.Thread(target=_wait, name=f"reap-{getattr(proc, 'pid', '?')}", daemon=True).start()


def spawn(job_id: str, dataset_id: str, action: str, *, popen=subprocess.Popen,
          which=shutil.which) -> dict:
    """Launch the runner detached (own session, so neither a gateway restart nor
    a SIGHUP reaches it), stdout+stderr appended to the job log. Plain,
    blocking-but-fast function: the route calls it via ``asyncio.to_thread``.
    Returns ``{pid, pgid, log, argv}``."""
    log_file = log_path_for(job_id)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    argv = build_argv(job_id, dataset_id, action, which=which)
    with log_file.open("ab") as log_fh:
        proc = popen(
            argv,
            cwd=str(REPO_ROOT),
            env=job_env(),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    _reap_in_background(proc)
    pid = int(proc.pid)
    return {"pid": pid, "pgid": pid, "log": str(log_file), "argv": argv}


def start_job(dataset_id: str, action: str = DEFAULT_ACTION, *, spawn_fn=None) -> dict:
    """Lock → state(queued) → spawn → record pid. Raises :class:`JobActive`
    when another job is queued/running (the 409 case); any launch failure
    marks the new state ``failed`` and releases the lock before propagating."""
    ds = validate_dataset_id(dataset_id)
    act = validate_action(action)
    namespace = f"whg:{ds}"
    existing = active_job()
    if existing:
        raise JobActive(existing.get("job_id"), existing.get("namespace"))
    job_id = new_job_id()
    if not acquire_lock(job_id, namespace, os.getpid()):
        lock = read_lock() or {}
        holder = lock.get("job_id")
        holder_state = read_state(holder) if isinstance(holder, str) and holder.isdigit() else None
        raise JobActive(holder, (holder_state or {}).get("namespace") or lock.get("namespace"))
    try:
        create_state(job_id, ds, act)
        info = (spawn_fn or spawn)(job_id, ds, act)
    except BaseException as exc:
        if read_state(job_id) is not None:
            update_state(job_id, status="failed", reason=REASON_RUNNER_DIED,
                         message=f"spawn failed: {exc!r}"[:500], finished_at=now_iso())
        release_lock(job_id)
        raise
    rewrite_lock(job_id, info["pid"])
    return update_state(job_id, pid=info["pid"], pgid=info["pgid"], log=info["log"])


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def status_payload(state: dict) -> dict:
    """The GET body fields the state file answers (``indexed`` is the route's)."""
    return {
        "job_id": state.get("job_id"),
        "status": state.get("status"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "message": state.get("message"),
        "reason": state.get("reason") if state.get("status") == "failed" else None,
    }
