# gateway/reingest.py
"""
Re-ingest endpoint for the WHG API gateway.

Implements the contract documented in
``whg3/api/reingest.py`` (Django-side caller). Two endpoints:

* ``POST /api/registry/reingest``  — enqueue a re-ingest for one
  authority namespace. Submits a Slurm job on a CRC login node and
  returns the job_id. Refuses with 409 if a re-ingest is already
  pending/running for that namespace (Django can adopt the existing
  job_id from the response body).

* ``GET /api/registry/reingest/{job_id}`` — poll the job's current
  state. Maps Slurm states onto the small vocabulary the contract
  defines (``queued`` / ``running`` / ``completed`` / ``failed``).
  For a dataset job (Slurm name ``reingest-whg-<id>``) the response also
  carries ``indexed`` / ``indexed_count``, read from the marker
  ``{STAGED_BASE_DIR}/whg/datasets/<id>/indexed.json`` that
  ``processing.index_namespace`` writes as the last step of a zero-error
  execute run. ``indexed`` is true only when that marker exists, parses,
  carries the expected schema and dataset id, AND names this job_id; any
  other state — missing, corrupt, another job's — is ``false`` with a null
  count, never an error. ``status`` is never derived from the marker:
  ``completed`` still means only "the job exited 0". Plain-namespace jobs
  carry neither key, so existing consumers see nothing new.

**Auth**: none for a plain authority namespace — the Pitt firewall
whitelists the DO app server's IP for this endpoint. (Bearer auth would be
redundant and is reserved for the inverse-direction inventory push at
``/api/registry/inventory``.)

**Contributed datasets (place#341)**: ``namespace`` may also be
``whg:<id>`` (``^whg:\\d+$``), which re-stages and re-indexes ONE contributed
dataset. That path alone requires ``Authorization: Bearer <token>`` with the
token in ``REINGEST_DATASET_TOKEN``: 503 when the gateway has no token
configured (fail closed), 401 when the presented one differs (constant-time
compare). The job name is ``reingest-whg-<id>`` so the squeue guard works
per dataset. The id is validated before any ssh argv is built. The
plain-namespace path is unchanged and still unauthenticated.

**Architecture**: the gateway runs on the Pitt VM. Both endpoints SSH to
a CRC login node (``crc0`` by default) to invoke ``sbatch`` / ``sacct`` /
``squeue``. The CRC-side orchestrator script ``scripts/reingest.sbatch``
takes the namespace as ``$1`` and is the single point of authority for
what a "re-ingest" actually does for each namespace.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, model_serializer

logger = logging.getLogger("gateway.reingest")

router = APIRouter(prefix="/api/registry", tags=["Reingest"])


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# CRC login node we ssh from the Pitt VM. Override per-deployment via env.
CRC_LOGIN_HOST = os.environ.get("REINGEST_CRC_HOST", "crc0")

# Path on CRC to the orchestrator sbatch (lives in this repo's scripts/ dir
# and is rsynced to /vast/ishi/elastic/scripts/ on each deploy).
CRC_SBATCH_SCRIPT = os.environ.get(
    "REINGEST_SBATCH_SCRIPT",
    "/vast/ishi/elastic/scripts/reingest.sbatch",
)

# Slurm cluster the jobs run on. htc covers everything we need today.
CRC_CLUSTER = os.environ.get("REINGEST_CRC_CLUSTER", "htc")

# SSH timeouts (seconds). POST is async-fire-then-block-on-sbatch which is
# cheap; GET is just sacct which is even cheaper. Conservative caps so a
# stalled login node returns a 504 rather than wedging the gateway worker.
SSH_SUBMIT_TIMEOUT = int(os.environ.get("REINGEST_SSH_SUBMIT_TIMEOUT", "30"))
SSH_QUERY_TIMEOUT = int(os.environ.get("REINGEST_SSH_QUERY_TIMEOUT", "15"))

# Whitelist of namespaces accepted by the endpoint. Hardcoded here so the
# gateway doesn't have to import the heavy ``processing.settings`` module
# (and so unknown ids get rejected before any Slurm cost is paid). Update
# in lockstep with ``processing.settings.AUTHORITIES``.
VALID_NAMESPACES = frozenset({
    "chgis", "clio", "dgsd", "dp", "gb", "gn", "iv", "nl", "ohm",
    "osm", "pl", "po", "tgn", "tm", "un", "wd",
})

# One contributed WHG dataset: ``whg:<numeric id>`` and nothing else. The
# fullmatch is the injection guard — the id reaches an ssh argv, a Slurm job
# name and a shell ``case`` branch, and this regex is the only thing between
# the request body and all three.
_DATASET_NS_RE = re.compile(r"^whg:(\d+)$")

# Env var holding the shared secret Django presents for ``whg:<id>`` requests.
REINGEST_DATASET_TOKEN_ENV = "REINGEST_DATASET_TOKEN"

# The Slurm job name a dataset job carries (see ``_job_name_for``). Fullmatch
# only: it is the one path from a sacct field to a filesystem path below.
_DATASET_JOB_NAME_RE = re.compile(r"^reingest-whg-(\d+)$")

# The marker ``processing.index_namespace`` writes after a zero-error execute
# run. Schema string and filename are duplicated here rather than imported so
# the gateway never pulls in ``processing.settings`` (heavy, and it resolves
# paths at import time). Keep in step with ``index_namespace.INDEXED_MARKER_*``.
INDEXED_MARKER_SCHEMA = "whg-dataset-indexed/1"
INDEXED_MARKER_FILENAME = "indexed.json"
# A real marker is ~200 bytes; anything larger is not one, and is not read.
INDEXED_MARKER_MAX_BYTES = 4096


def parse_dataset_namespace(namespace: str) -> Optional[str]:
    """``"whg:12"`` → ``"12"``; anything else → ``None``."""
    m = _DATASET_NS_RE.fullmatch(namespace)
    return m.group(1) if m else None


def dataset_id_from_job_name(job_name: str) -> Optional[str]:
    """``"reingest-whg-12"`` → ``"12"``; any other job name → ``None``."""
    m = _DATASET_JOB_NAME_RE.fullmatch((job_name or "").strip())
    return m.group(1) if m else None


def staged_base_dir() -> Path:
    """The staged tree root, resolved exactly as
    ``processing.whg_id_map.default_id_map_path`` does (``STAGED_BASE_DIR``,
    else ``$IX3_BASE/staged``) — read per call so a test can repoint it."""
    return Path(os.environ.get(
        "STAGED_BASE_DIR",
        os.path.join(os.environ.get("IX3_BASE", "/vast/ishi"), "staged"),
    ))


def indexed_marker_path(dataset_id: str) -> Path:
    """``{staged}/whg/datasets/<id>/indexed.json``. ``dataset_id`` must already
    be digits-only (it comes from ``dataset_id_from_job_name`` or
    ``parse_dataset_namespace``); re-checked here so no other caller can route
    an unvalidated string into a path."""
    if not re.fullmatch(r"\d+", dataset_id or ""):
        raise ValueError(f"dataset id is not digits: {dataset_id!r}")
    return staged_base_dir() / "whg" / "datasets" / dataset_id / INDEXED_MARKER_FILENAME


def _bare_job_id(job_id: Any) -> str:
    """``"4242_3"`` → ``"4242"``: the array suffix plays no part in matching."""
    return str(job_id).strip().split("_", 1)[0]


def read_indexed_marker(dataset_id: str, job_id: str) -> tuple[bool, Optional[int]]:
    """``(indexed, indexed_count)`` for one dataset as seen by ``job_id``.

    True only when the marker is present, within the size cap, valid JSON of
    the expected schema, names this dataset AND names this job. Every other
    outcome is ``(False, None)`` — a missing or damaged marker is a fact about
    the dataset's indexed state, not a failure of the status poll.
    """
    try:
        path = indexed_marker_path(dataset_id)
        size = path.stat().st_size
        if size > INDEXED_MARKER_MAX_BYTES:
            logger.warning("indexed marker %s is %d bytes (> %d); ignoring",
                           path, size, INDEXED_MARKER_MAX_BYTES)
            return False, None
        with path.open("rb") as fh:
            raw = fh.read(INDEXED_MARKER_MAX_BYTES + 1)
        marker = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return False, None
    except (OSError, ValueError) as exc:
        logger.warning("indexed marker for whg:%s unreadable: %s", dataset_id, exc)
        return False, None
    if not isinstance(marker, dict):
        return False, None
    if marker.get("schema") != INDEXED_MARKER_SCHEMA:
        return False, None
    if str(marker.get("dataset_id")) != dataset_id:
        return False, None
    marker_job = marker.get("job_id")
    if marker_job is None or _bare_job_id(marker_job) != _bare_job_id(job_id):
        return False, None
    count = marker.get("indexed_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return False, None
    return True, count


def _require_dataset_token(authorization: Optional[str]) -> None:
    """Bearer check for the ``whg:<id>`` path only.

    * no token configured on the gateway → 503 (fail closed: the path is
      disabled, not open);
    * header missing, not ``Bearer``, or token differs → 401. The compare is
      constant-time so a wrong token's length or prefix cannot be probed.
    """
    expected = os.environ.get(REINGEST_DATASET_TOKEN_ENV, "")
    if not expected:
        raise HTTPException(
            503, f"dataset re-ingest is not enabled on this gateway "
                 f"({REINGEST_DATASET_TOKEN_ENV} is unset)")
    presented = ""
    if authorization:
        scheme, _, value = authorization.strip().partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()
    if not presented or not hmac.compare_digest(
            presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "invalid or missing bearer token")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ReingestRequest(BaseModel):
    namespace: str = Field(..., min_length=1, max_length=16,
                           description="Registry id of the authority to re-ingest")


class ReingestResponse(BaseModel):
    job_id: str
    status: str


class ReingestStatusResponse(BaseModel):
    job_id: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    message: Optional[str] = None
    # Dataset jobs only. ``indexed`` is None for a plain-namespace job and the
    # serializer drops BOTH keys then; for a dataset job it is always a bool
    # and both keys are always present (``indexed_count`` null when false).
    indexed: Optional[bool] = None
    indexed_count: Optional[int] = None

    @model_serializer(mode="wrap")
    def _omit_indexed_for_plain_jobs(self, handler):
        data = handler(self)
        if self.indexed is None:
            data.pop("indexed", None)
            data.pop("indexed_count", None)
        return data


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


async def _ssh(argv: list[str], *, timeout: int) -> tuple[int, str, str]:
    """Run ``argv`` via ssh on ``CRC_LOGIN_HOST``. Returns ``(rc, stdout, stderr)``.

    ``argv`` is the remote command + its arguments; the ssh wrapper is added
    here. ``BatchMode=yes`` ensures a missing key fails fast rather than
    prompting for a password (which would hang).
    """
    full = [
        "ssh", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        CRC_LOGIN_HOST,
        # Quote each remote token so spaces/specials don't get re-split by
        # the remote shell. ssh joins argv with spaces, but we control them.
        *argv,
    ]
    proc = await asyncio.create_subprocess_exec(
        *full,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(
            504, f"SSH to {CRC_LOGIN_HOST} timed out after {timeout}s",
        )
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def _job_name_for(namespace: str) -> str:
    """Stable Slurm job name per namespace.

    No timestamp suffix — the absence of a timestamp lets us use ``squeue
    -n <name>`` to detect an existing pending/running job for the same
    namespace (the concurrency guard required by the Django-side contract).
    Slurm permits multiple jobs sharing the same name; squeue without a
    state filter will surface any active one.

    A contributed dataset ``whg:12`` becomes ``reingest-whg-12`` — the colon
    is replaced so the name is one shell-safe token, and so the dataset guard
    is independent of a whole-``whg`` job's (``reingest-whg``).
    """
    return f"reingest-{namespace.replace(':', '-')}"


_SBATCH_JOBID_RE = re.compile(r"Submitted batch job (\d+)")


# ---------------------------------------------------------------------------
# Slurm queries
# ---------------------------------------------------------------------------


async def _existing_active_job(namespace: str) -> Optional[str]:
    """Return the Slurm JobID of any pending/running re-ingest for the
    given namespace, or ``None`` if nothing active.

    Uses ``squeue -n <name>`` which by definition returns only jobs that
    are pending, running, completing, or otherwise not yet terminal.
    """
    rc, out, err = await _ssh(
        [
            "squeue",
            "-M", CRC_CLUSTER,
            "-n", _job_name_for(namespace),
            "-h",
            "-o", "%i",
        ],
        timeout=SSH_QUERY_TIMEOUT,
    )
    if rc != 0:
        # Don't fail the request because the concurrency check failed —
        # just log and let the submit attempt go through. Slurm's own
        # job-name dedup is a soft guarantee anyway.
        logger.warning("squeue concurrency check failed (rc=%d): %s",
                       rc, err.strip()[:200])
        return None
    line = out.strip()
    if not line:
        return None
    # squeue with -h emits one job per line; we only need the first.
    return line.splitlines()[0].strip() or None


async def _submit_reingest(namespace: str) -> str:
    """Submit the re-ingest sbatch and return the new job's id."""
    rc, out, err = await _ssh(
        [
            "sbatch",
            "-M", CRC_CLUSTER,
            f"--job-name={_job_name_for(namespace)}",
            CRC_SBATCH_SCRIPT,
            namespace,
        ],
        timeout=SSH_SUBMIT_TIMEOUT,
    )
    if rc != 0:
        logger.error("sbatch failed for %s (rc=%d): %s", namespace, rc, err.strip())
        raise HTTPException(
            502,
            f"sbatch failed (rc={rc}): {err.strip()[:300] or out.strip()[:300]}",
        )
    m = _SBATCH_JOBID_RE.search(out)
    if not m:
        logger.error("could not parse sbatch output: %r", out)
        raise HTTPException(502, f"could not parse sbatch output: {out!r}")
    return m.group(1)


# Map raw Slurm states (https://slurm.schedmd.com/squeue.html#lbAG) onto
# the four-value vocabulary the Django-side contract uses.
_SLURM_STATE_MAP = {
    "PENDING": "queued",
    "REQUEUED": "queued",
    "RESIZING": "queued",
    "CONFIGURING": "queued",
    "RUNNING": "running",
    "SUSPENDED": "running",
    "COMPLETING": "running",
    "STOPPED": "running",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CANCELLED": "failed",
    "TIMEOUT": "failed",
    "NODE_FAIL": "failed",
    "OUT_OF_MEMORY": "failed",
    "BOOT_FAIL": "failed",
    "DEADLINE": "failed",
    "PREEMPTED": "failed",
    "REVOKED": "failed",
    "SPECIAL_EXIT": "failed",
}


def _map_state(slurm_state: str) -> str:
    """Map Slurm's State field to the contract vocabulary.

    Slurm sometimes annotates states (e.g. ``CANCELLED+by 12345``) so we
    take only the first whitespace-/+-delimited token. Unknown states fall
    back to ``failed`` so callers don't get stuck adopting an unrecognised
    in-flight value.
    """
    head = re.split(r"[\s+]", slurm_state.strip(), maxsplit=1)[0].upper()
    return _SLURM_STATE_MAP.get(head, "failed")


def _to_iso(slurm_time: str) -> Optional[str]:
    """Slurm prints times like ``2026-05-04T17:42:11`` (naive local).
    Treat as UTC for the gateway response (the cluster runs UTC)."""
    if not slurm_time or slurm_time in ("Unknown", "N/A"):
        return None
    try:
        dt = datetime.fromisoformat(slurm_time)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


# The sacct columns we ask for, in the order we ask for them. Parsing zips the
# output against THIS tuple, so adding or reordering a column here is the
# whole change — nothing below indexes a field by position.
_SACCT_FIELDS = ("JobID", "JobName", "State", "Start", "End", "ExitCode", "Reason")


def _parse_sacct_line(line: str) -> Optional[dict[str, str]]:
    """One ``--parsable2`` row → ``{field: value}``; ``None`` if it has fewer
    columns than ``State``. ``Reason`` is last and may itself contain ``|``, so
    the split is capped at the column count."""
    fields = line.split("|", len(_SACCT_FIELDS) - 1)
    row = dict(zip(_SACCT_FIELDS, fields))
    if "State" not in row:
        return None
    return row


async def _job_status(job_id: str) -> Optional[dict[str, Any]]:
    """Fetch a job's current state. Returns the response dict or ``None``
    when the job_id isn't known to sacct (404 from the caller's POV).

    For a dataset job (JobName ``reingest-whg-<id>``) the dict also carries
    ``indexed`` / ``indexed_count`` from the marker; ``status`` never does."""
    rc, out, err = await _ssh(
        [
            "sacct",
            "-M", CRC_CLUSTER,
            "-j", job_id,
            "-X", "-n",  # exclude job steps; no header
            "--parsable2",
            "--format=" + ",".join(_SACCT_FIELDS),
        ],
        timeout=SSH_QUERY_TIMEOUT,
    )
    if rc != 0:
        logger.warning("sacct lookup failed for %s (rc=%d): %s",
                       job_id, rc, err.strip()[:200])
        # Fall through — return None so caller emits 404 rather than 5xx.
        return None
    line = out.strip().splitlines()
    if not line:
        return None
    row = _parse_sacct_line(line[0])
    if row is None:
        return None
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": _map_state(row["State"]),
    }
    if iso := _to_iso(row.get("Start", "")):
        payload["started_at"] = iso
    if iso := _to_iso(row.get("End", "")):
        payload["finished_at"] = iso
    # Surface a useful message when the job ended badly. ExitCode is
    # ``0:0`` on clean completion; Reason carries scheduler explanations.
    parts = []
    exit_code = row.get("ExitCode", "")
    if exit_code and exit_code != "0:0":
        parts.append(f"exit={exit_code}")
    reason = row.get("Reason", "")
    if reason and reason not in ("None", "Dependency"):
        parts.append(reason)
    if parts and payload["status"] == "failed":
        payload["message"] = " ".join(parts)
    # Dataset jobs: the marker answers "did it land", independently of status.
    dataset_id = dataset_id_from_job_name(row.get("JobName", ""))
    if dataset_id is not None:
        indexed, count = read_indexed_marker(dataset_id, job_id)
        payload["indexed"] = indexed
        payload["indexed_count"] = count
    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/reingest", status_code=202, response_model=ReingestResponse,
             responses={
                 400: {"description": "Unknown namespace / malformed dataset id"},
                 401: {"description": "whg:<id> only — bad or missing bearer token"},
                 409: {"description": "A re-ingest is already in flight"},
                 502: {"description": "Failed to submit Slurm job"},
                 503: {"description": "whg:<id> only — dataset re-ingest not enabled"},
                 504: {"description": "SSH to CRC login node timed out"},
             })
async def post_reingest(
    req: ReingestRequest,
    authorization: Optional[str] = Header(default=None),
) -> ReingestResponse:
    """Enqueue a re-ingest for the given namespace, or for one contributed
    dataset when ``namespace`` is ``whg:<id>``.

    Returns ``202 Accepted`` on success with the assigned ``job_id``.
    Returns ``409 Conflict`` (with the existing ``job_id`` in the body)
    when the same namespace already has a pending or running re-ingest;
    Django can adopt that id rather than treating it as an error.
    """
    ns = req.namespace.strip().lower()
    if ns not in VALID_NAMESPACES:
        # Not a plain namespace: the only other acceptable shape is a
        # contributed dataset, validated BEFORE anything else (so a malformed
        # id is a 400 and never reaches the token check, squeue or sbatch).
        dataset_id = parse_dataset_namespace(ns)
        if dataset_id is None:
            raise HTTPException(400, f"unknown namespace: {req.namespace!r}")
        _require_dataset_token(authorization)
        ns = f"whg:{dataset_id}"

    existing = await _existing_active_job(ns)
    if existing:
        # Surface the existing id so the Django side can adopt it. We use
        # an HTTPException with a structured detail; FastAPI emits it as
        # ``{"detail": {...}}``. The contract expects the body shape
        # ``{"error": "...", "job_id": "..."}`` — wrap both.
        raise HTTPException(
            409,
            detail={
                "error": f"re-ingest for {ns!r} is already in flight",
                "job_id": existing,
            },
        )

    job_id = await _submit_reingest(ns)
    logger.info("reingest %s submitted as job %s", ns, job_id)
    return ReingestResponse(job_id=job_id, status="queued")


@router.get("/reingest/{job_id}", response_model=ReingestStatusResponse,
            responses={
                404: {"description": "Job id not known to Slurm"},
                504: {"description": "SSH to CRC login node timed out"},
            })
async def get_reingest_status(job_id: str) -> ReingestStatusResponse:
    """Poll the gateway for a running re-ingest's current state.

    Maps Slurm's State onto the four-value vocabulary the contract uses.
    A 404 here intentionally signals to Django that the job is no longer
    tracked — staff can then retry without being blocked by a stale
    ``running`` row.
    """
    # Reject obvious garbage early — Slurm job ids are always digits, with
    # an optional ``_<arrayindex>`` suffix and an optional ``.<step>`` we
    # never use here. Anything else is a typo at best, an injection attempt
    # at worst.
    if not re.fullmatch(r"\d+(?:_\d+)?", job_id):
        raise HTTPException(400, f"malformed job_id: {job_id!r}")

    payload = await _job_status(job_id)
    if payload is None:
        raise HTTPException(404, f"job not found: {job_id}")
    return ReingestStatusResponse(**payload)
