# gateway/app.py
"""
WHG API Gateway

Runs on port 9200 (the only port open through the CRC firewall from DO).
Routes requests based on Host header:

  kibana.whgazetteer.org  →  Kibana  (localhost:5601)
  everything else         →  ES      (localhost:9201)

Custom API endpoints for chained queries can be added under /api/.

Usage:
  python -m gateway                     # production
  uvicorn gateway.app:app --reload      # development
"""

import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket
from starlette.responses import JSONResponse

from .config import (
    ES_BACKEND,
    KIBANA_BACKEND,
    KIBANA_HOST_KEYWORD,
    get_elastic_password,
)
from .proxy import proxy_http, proxy_websocket, close_http_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("gateway")


def _get_backend(host: str) -> str:
    """Route to backend based on Host header."""
    if KIBANA_HOST_KEYWORD in host:
        return KIBANA_BACKEND
    return ES_BACKEND


# ---- Application Lifecycle ----

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Gateway starting — ES: {ES_BACKEND}, Kibana: {KIBANA_BACKEND}")
    yield
    await close_http_client()
    logger.info("Gateway shut down")


app = FastAPI(
    title="WHG API Gateway",
    version="0.1.0",
    docs_url=None,  # Disable Swagger UI (we're a proxy)
    redoc_url=None,
    lifespan=lifespan,
)


# ---- Health Endpoint ----

@app.get("/api/health")
async def health():
    """Gateway health check — also reports ES backend status."""
    import httpx
    es_status = "unknown"
    try:
        auth = None
        password = get_elastic_password()
        if password:
            auth = ("elastic", password)
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{ES_BACKEND}/_cluster/health", auth=auth)
            es_health = r.json()
            es_status = es_health.get("status", "unknown")
    except Exception as e:
        es_status = f"error: {e}"

    return {
        "gateway": "ok",
        "elasticsearch": es_status,
        "backends": {
            "es": ES_BACKEND,
            "kibana": KIBANA_BACKEND,
        },
    }


# ---- Custom API Endpoints (add chained queries here) ----

# @app.get("/api/search/phonetic")
# async def phonetic_search(q: str, ...):
#     """Example: chain a text query with a KNN phonetic embedding query."""
#     pass


# ---- WebSocket Proxy (Kibana) ----

@app.websocket("/{path:path}")
async def websocket_proxy(ws: WebSocket, path: str):
    host = ws.headers.get("host", "")
    backend = _get_backend(host)
    await proxy_websocket(ws, backend, f"/{path}")


# ---- HTTP Catch-All Proxy ----

@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"],
)
async def http_proxy(request: Request, path: str):
    host = request.headers.get("host", "")
    backend = _get_backend(host)
    return await proxy_http(request, backend)


# Also handle root path
@app.api_route(
    "/",
    methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"],
    include_in_schema=False,
)
async def http_proxy_root(request: Request):
    host = request.headers.get("host", "")
    backend = _get_backend(host)
    return await proxy_http(request, backend)

