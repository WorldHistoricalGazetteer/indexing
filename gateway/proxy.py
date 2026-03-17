# gateway/proxy.py
"""
Async reverse proxy utilities for HTTP and WebSocket connections.
"""

import asyncio
import logging

import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect
import websockets.client
import websockets.exceptions

logger = logging.getLogger("gateway.proxy")

# Shared async HTTP client (connection pooling)
_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _http_client


async def close_http_client():
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


# ---- HTTP Proxy ----

# Headers that should not be forwarded between client and backend
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host",  # We set this explicitly for the backend
})


def _filter_headers(headers, extra_skip=frozenset()):
    """Filter hop-by-hop and other headers that shouldn't be proxied."""
    skip = HOP_BY_HOP | extra_skip
    return [(k, v) for k, v in headers.items() if k.lower() not in skip]


async def proxy_http(request: Request, backend_url: str) -> Response:
    """
    Proxy an HTTP request to a backend, streaming the response back.
    """
    client = await get_http_client()

    # Build the backend URL
    path = request.url.path
    query = request.url.query
    url = f"{backend_url}{path}"
    if query:
        url = f"{url}?{query}"

    # Forward headers (filtering hop-by-hop)
    headers = dict(_filter_headers(request.headers))
    headers["host"] = backend_url.split("//", 1)[1]  # e.g. "localhost:9201"

    # Stream the request body
    body = await request.body()

    try:
        backend_response = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        return Response(
            content=f'{{"error":"Backend unavailable: {backend_url}"}}',
            status_code=502,
            media_type="application/json",
        )
    except httpx.TimeoutException:
        return Response(
            content=f'{{"error":"Backend timeout: {backend_url}"}}',
            status_code=504,
            media_type="application/json",
        )

    # Build response headers
    response_headers = dict(_filter_headers(backend_response.headers))

    return Response(
        content=backend_response.content,
        status_code=backend_response.status_code,
        headers=response_headers,
    )


# ---- WebSocket Proxy ----

async def proxy_websocket(ws_client: WebSocket, backend_url: str, path: str):
    """
    Proxy a WebSocket connection, forwarding messages in both directions.
    Required for Kibana real-time features.
    """
    await ws_client.accept()

    # Build ws:// URL from http:// backend URL
    ws_url = backend_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}{path}"

    try:
        async with websockets.client.connect(ws_url) as ws_backend:

            async def client_to_backend():
                try:
                    while True:
                        data = await ws_client.receive_text()
                        await ws_backend.send(data)
                except WebSocketDisconnect:
                    await ws_backend.close()
                except Exception:
                    pass

            async def backend_to_client():
                try:
                    async for message in ws_backend:
                        if isinstance(message, str):
                            await ws_client.send_text(message)
                        else:
                            await ws_client.send_bytes(message)
                except websockets.exceptions.ConnectionClosed:
                    await ws_client.close()
                except Exception:
                    pass

            # Run both directions concurrently
            await asyncio.gather(
                client_to_backend(),
                backend_to_client(),
                return_exceptions=True,
            )
    except Exception as e:
        logger.warning(f"WebSocket proxy error: {e}")
        try:
            await ws_client.close(code=1011)
        except Exception:
            pass

