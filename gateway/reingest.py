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

**Auth**: none — the Pitt firewall whitelists the DO app server's IP for
this endpoint. (Bearer auth would be redundant and is reserved for the
inverse-direction inventory push at ``/api/registry/inventory``.)

**Architecture**: the gateway runs on the Pitt VM. Both endpoints SSH to
a CRC login node (``crc0`` by default) to invoke ``sbatch`` / ``sacct`` /
``squeue``. The CRC-side orchestrator script ``scripts/reingest.sbatch``
takes the namespace as ``$1`` and is the single point of authority for
what a "re-ingest" actually does for each namespace.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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
    """
    return f"reingest-{namespace}"


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


async def _job_status(job_id: str) -> Optional[dict[str, Any]]:
    """Fetch a job's current state. Returns the response dict or ``None``
    when the job_id isn't known to sacct (404 from the caller's POV)."""
    rc, out, err = await _ssh(
        [
            "sacct",
            "-M", CRC_CLUSTER,
            "-j", job_id,
            "-X", "-n",  # exclude job steps; no header
            "--parsable2",
            "--format=JobID,State,Start,End,ExitCode,Reason",
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
    fields = line[0].split("|")
    if len(fields) < 2:
        return None
    raw_state = fields[1]
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": _map_state(raw_state),
    }
    if len(fields) >= 3:
        if iso := _to_iso(fields[2]):
            payload["started_at"] = iso
    if len(fields) >= 4:
        if iso := _to_iso(fields[3]):
            payload["finished_at"] = iso
    # Surface a useful message when the job ended badly. ExitCode is
    # ``0:0`` on clean completion; Reason carries scheduler explanations.
    parts = []
    if len(fields) >= 5 and fields[4] and fields[4] != "0:0":
        parts.append(f"exit={fields[4]}")
    if len(fields) >= 6 and fields[5] and fields[5] not in ("None", "Dependency"):
        parts.append(fields[5])
    if parts and payload["status"] == "failed":
        payload["message"] = " ".join(parts)
    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/reingest", status_code=202, response_model=ReingestResponse,
             responses={
                 400: {"description": "Unknown namespace"},
                 409: {"description": "A re-ingest is already in flight"},
                 502: {"description": "Failed to submit Slurm job"},
                 504: {"description": "SSH to CRC login node timed out"},
             })
async def post_reingest(req: ReingestRequest) -> ReingestResponse:
    """Enqueue a re-ingest for the given namespace.

    Returns ``202 Accepted`` on success with the assigned ``job_id``.
    Returns ``409 Conflict`` (with the existing ``job_id`` in the body)
    when the same namespace already has a pending or running re-ingest;
    Django can adopt that id rather than treating it as an error.
    """
    ns = req.namespace.strip().lower()
    if ns not in VALID_NAMESPACES:
        raise HTTPException(400, f"unknown namespace: {req.namespace!r}")

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
