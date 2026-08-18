# gateway/__main__.py
"""Run the gateway: python -m gateway"""

import uvicorn
from .config import GATEWAY_HOST, GATEWAY_PORT, GATEWAY_WORKERS

# More than one worker so a single wedged request cannot take the service down. On
# 2026-08-18 one hung request left the (single-worker) gateway alive, spinning, and
# answering nothing on 9200 — which, because Django reaches the legacy indexes
# through this process, took production search and reconciliation with it. With N
# workers the supervisor keeps the others serving, and the watchdog still restarts
# the process if every worker goes.
#
# Each worker loads its own Symphonym model (~650MB), so this is memory traded for
# survivability; the host has ample headroom (62GB, 8 cores).
uvicorn.run(
    "gateway.app:app",
    host=GATEWAY_HOST,
    port=GATEWAY_PORT,
    workers=GATEWAY_WORKERS,
    log_level="info",
    access_log=True,
)
