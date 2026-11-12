# gateway/threads.py
"""Cap the CPU math libraries' thread pools for the SERVING process.

Measured 2026-11-12 on the production gateway (2 uvicorn workers + Elasticsearch
sharing 8 cores), with unique fuzzy ``/api/reconcile`` queries:

    clients   default           OMP/MKL_NUM_THREADS=1
       4      4.3 q/s           8.5 q/s    (gateway CPU 388% -> 42%)
       8      3.4 q/s          10.6 q/s
      16      3.4 q/s (p95 7.5s) 11.3 q/s (p95 1.9s)

Each worker's torch/OpenMP pool (one thread per core by default) oversubscribed
the box against Elasticsearch and against the other worker, and burned CPU on
thread contention rather than useful work. One query needs far less than 8
threads of math; concurrency across requests is what the workers are for. One
thread per worker removed the waste and moved the bottleneck to Elasticsearch,
where it belongs.

Applied only by ``python -m gateway``. Do NOT move this into the package
``__init__``: batch jobs (``phonetics/inference/backfill_embeddings.py``) import
``gateway.*`` and genuinely want every core. An explicit environment value always
wins, so an operator can still override it for the service.
"""

from __future__ import annotations

import os

_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS")


def cap_cpu_threads(default: str = "1") -> None:
    """Set the thread-pool caps unless the environment already chose a value.

    Must run before torch (or numpy/MKL) is first imported in this process, and
    before the uvicorn supervisor spawns its workers, which inherit ``os.environ``.
    """
    for var in _VARS:
        os.environ.setdefault(var, default)
