"""Where torch should run, and what happens when it can't run there.

One function, because the shared thing is a POLICY, not a capability check.
`torch.cuda.is_available()` is one line and every caller can write it; what the
callers were getting wrong is what to DO with the answer:

    processing/reembed.py                    --device default "cuda", no check
    phonetics/inference/backfill_embeddings  --device default "cuda", no check
    phonetics/inference/update_es.py         --device default 'cuda' if available else 'cpu'

The third looks like the careful one and is the one to avoid. A silent fallback
from an explicitly-requested GPU to CPU is this repository's signature fault —
*a required input is absent, something plausible is substituted, and the stage
reports success* (`developer/postmortem-ingestion-faults.md`; eleven of sixteen
registered faults share that shape). Applied here it costs a day rather than a
wrong number: `reembed_canonical.sbatch` asks for `--gres=gpu:1
--cpus-per-task=4 --time=02:00:00`, so a shard that lost its GPU and fell back
would run at roughly a sixth of the intended rate inside a wall clock sized for
the GPU, and be killed by the time limit. The operator reads TIMEOUT and looks
at shard sizes, not at whether a GPU was ever acquired.

So the three device words mean three different things and the difference is the
point:

    "auto"  choose, and say which and why.  The right default for a CLI that
            may be run on a login node, a laptop, or an allocation.
    "cuda"  a DEMAND.  If there is no GPU this raises, with the reason — it
            never silently becomes "cpu".
    "cpu"   a demand too, and always satisfiable.

Nothing here imports torch at module scope. `processing/reembed.py` is imported
on **pitt**, which has no torch and no conda env, for its export and apply
phases; a top-level torch import would make this module unusable there.
"""
from __future__ import annotations

import os
import sys

__all__ = ["resolve_device", "configure_cpu_threads", "describe_device"]


class NoGPUAvailable(RuntimeError):
    """An explicit ``--device cuda`` was asked for on a host without one."""


def _cuda_diagnosis() -> str:
    """Why CUDA is unavailable, in the words that distinguish the causes.

    'CUDA not available' alone cannot tell an unallocated GPU (submit with
    --gres) from a driver/build mismatch (rebuild torch) from a masked device
    (CUDA_VISIBLE_DEVICES=""), and those have completely different fixes.
    """
    try:
        import torch
    except ImportError as exc:                       # pragma: no cover - host-dependent
        return f"torch is not importable here ({exc})"
    bits = [f"torch {torch.__version__}"]
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible is not None:
        bits.append(f"CUDA_VISIBLE_DEVICES={visible!r}"
                    + (" (empty = every device masked)" if visible.strip() == "" else ""))
    if os.environ.get("SLURM_JOB_ID"):
        gres = os.environ.get("SLURM_JOB_GRES") or os.environ.get("SBATCH_GRES") or "none"
        bits.append(f"inside Slurm job {os.environ['SLURM_JOB_ID']} with gres={gres}"
                    + ("; the allocation asked for no GPU" if gres == "none" else ""))
    else:
        bits.append("not inside a Slurm allocation (a CRC login node has no GPU "
                    "and must not run compute anyway)")
    if not torch.version.cuda:
        bits.append("this torch build is CPU-only (torch.version.cuda is None)")
    return "; ".join(bits)


def resolve_device(requested: str, *, purpose: str = "", stream=sys.stderr) -> str:
    """Return the torch device string to use, or raise rather than substitute.

    ``requested`` is "auto", "cpu", "cuda", or "cuda:N".  "auto" picks and says
    so; "cuda"/"cuda:N" is a demand and raises `NoGPUAvailable` if it cannot be
    met; "cpu" is returned unchanged.

    The chosen device is always announced, including when the answer is the
    boring one.  A log that only speaks up on the surprising path cannot be used
    to establish, after the fact, that the ordinary path was taken.
    """
    import torch                                     # deferred: see module docstring

    tag = f" for {purpose}" if purpose else ""
    if requested == "cpu":
        print(f"[device] cpu{tag} (requested explicitly)", file=stream, flush=True)
        return "cpu"

    available = torch.cuda.is_available()
    if requested == "auto":
        if available:
            name = torch.cuda.get_device_name(0)
            print(f"[device] cuda{tag} — auto-selected, {name}", file=stream, flush=True)
            return "cuda"
        print(f"[device] cpu{tag} — auto-selected because no GPU is visible: "
              f"{_cuda_diagnosis()}", file=stream, flush=True)
        return "cpu"

    if requested == "cuda" or requested.startswith("cuda:"):
        if available:
            index = int(requested.split(":", 1)[1]) if ":" in requested else 0
            count = torch.cuda.device_count()
            if index >= count:
                raise NoGPUAvailable(
                    f"{requested} was requested{tag} but this host has "
                    f"{count} visible GPU(s)")
            print(f"[device] {requested}{tag} — {torch.cuda.get_device_name(index)}",
                  file=stream, flush=True)
            return requested
        raise NoGPUAvailable(
            f"--device {requested} was requested{tag} and no GPU is available. "
            f"{_cuda_diagnosis()}. This is deliberately fatal rather than a "
            f"fallback to CPU: a fallback here runs at roughly a sixth of the "
            f"rate inside a wall clock sized for a GPU, and is reported as a "
            f"TIMEOUT rather than as a missing device. Pass --device auto to "
            f"choose whichever is present, or --device cpu to mean it.")

    raise ValueError(f"unrecognised --device {requested!r}: "
                     f"expected auto, cpu, cuda, or cuda:N")


def configure_cpu_threads(*, stream=sys.stderr) -> int:
    """Report the torch thread count in force, and bound it off Slurm.

    ⚠ THE FIX THIS WAS NAMED FOR IS NOT NEEDED ON CRC, and that was measured
    rather than assumed.  The premise was that torch defaults to
    ``os.cpu_count()`` — the whole NODE, not the allocation — so a 4-CPU task on
    a 64-core node would start 64 compute threads inside a 4-core cgroup.  A
    control run with ``OMP_NUM_THREADS`` deliberately unset (Slurm 11148408,
    htc-1024-n0, ``--cpus-per-task=4``) came back with
    ``torch.get_num_threads() == 4``: torch 2.9 reads the cgroup affinity by
    itself.  The hypothesis is refuted here.

    What survives is the REPORTING.  A throughput figure without its thread
    count cannot be compared to anything, and the measurements in
    ``/vast/ishi/reembed/bench/results.jsonl`` only mean something because each
    row carries the count that produced it.  The clamp stays as insurance for
    the case the control did not cover — running outside a Slurm allocation, on
    pitt or a laptop, where ``os.cpu_count()`` really is what torch takes — and
    an explicit ``OMP_NUM_THREADS`` always wins over both.

    Returns the count in force so a caller can log it beside its rate.
    """
    import torch

    allocated = os.environ.get("SLURM_CPUS_PER_TASK")
    explicit = os.environ.get("OMP_NUM_THREADS")
    if not explicit and allocated and allocated.isdigit():
        torch.set_num_threads(int(allocated))
        os.environ.setdefault("OMP_NUM_THREADS", allocated)
    threads = torch.get_num_threads()
    print(f"[device] torch threads={threads} "
          f"(SLURM_CPUS_PER_TASK={allocated or 'unset'}, "
          f"OMP_NUM_THREADS={explicit or 'unset'}, node has {os.cpu_count()} cpus)",
          file=stream, flush=True)
    return threads


def describe_device(device: str) -> dict:
    """A record of what actually ran, for a shard's metadata.

    The point is attribution after the fact: `reembed`'s ledger can say a shard
    ran on CPU at N threads or on a named GPU, so a rate that looks wrong can be
    explained without re-running it.
    """
    import torch

    out = {"device": device, "torch": torch.__version__,
           "host": os.uname().nodename,
           "slurm_job_id": os.environ.get("SLURM_JOB_ID", "")}
    if device.startswith("cuda"):
        index = int(device.split(":", 1)[1]) if ":" in device else 0
        out["gpu_name"] = torch.cuda.get_device_name(index)
    else:
        out["torch_threads"] = torch.get_num_threads()
        out["slurm_cpus_per_task"] = os.environ.get("SLURM_CPUS_PER_TASK", "")
    return out
