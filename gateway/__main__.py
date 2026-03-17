# gateway/__main__.py
"""Run the gateway: python -m gateway"""

import uvicorn
from .config import GATEWAY_HOST, GATEWAY_PORT

uvicorn.run(
    "gateway.app:app",
    host=GATEWAY_HOST,
    port=GATEWAY_PORT,
    log_level="info",
    access_log=True,
)

