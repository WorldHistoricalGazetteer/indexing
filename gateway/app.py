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

import asyncio
import itertools
import logging
import os
import time
from typing import List, Optional

from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Request, WebSocket
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from .config import (
    ES_BACKEND,
    INFLIGHT_SWEEP_SECONDS,
    INFLIGHT_WARN_SECONDS,
    KIBANA_BACKEND,
    KIBANA_HOST_KEYWORD,
    SLOW_REQUEST_SECONDS,
    TOPONYMS_INDEX,
    get_elastic_password,
)
from .proxy import proxy_http, proxy_websocket, close_http_client
from .extend import router as extend_router
from .links import router as links_router
from .places import router as places_router
from .reconcile import router as reconcile_router
from .reingest import router as reingest_router
from .search import router as search_router

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


# ---- Request observability ----
# uvicorn's access log records a request only once it COMPLETES. When the gateway
# wedged on 2026-08-18 the request responsible therefore left no trace whatsoever:
# the last log line was an unrelated call that had finished normally, and the cause
# could not be identified even in principle. These two mechanisms close that gap.
#
#   * the middleware logs any request that finishes SLOWLY, and
#   * the sweeper reports requests still running, which is the case the access log
#     structurally cannot show.
#
# Honest limitation: if the event loop is starved by CPU-bound work rather than
# waiting on I/O, the sweeper is starved with it and stays silent — that is what
# scripts/gateway_dump.sh (py-spy) is for. The two are complements, not duplicates.

_inflight: dict[int, tuple[float, str, str, str]] = {}   # id -> (started, method, path, client)
_req_seq = itertools.count()


def _describe(request: Request) -> tuple[str, str]:
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query[:200]}"
    client = request.client.host if request.client else "-"
    return path, client


async def _inflight_sweeper():
    """Periodically name the requests that are still running."""
    while True:
        try:
            await asyncio.sleep(INFLIGHT_SWEEP_SECONDS)
            now = time.monotonic()
            stuck = [
                (now - started, method, path, client)
                for (started, method, path, client) in list(_inflight.values())
                if now - started >= INFLIGHT_WARN_SECONDS
            ]
            if stuck:
                stuck.sort(reverse=True)
                logger.warning(
                    "pid %d: %d request(s) still in flight after %.0fs: %s",
                    os.getpid(), len(stuck), INFLIGHT_WARN_SECONDS,
                    "; ".join(f"{age:.0f}s {m} {p} from {c}" for age, m, p, c in stuck[:5]),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — observability must never kill the server
            logger.exception("in-flight sweeper error")


# ---- Application Lifecycle ----

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Gateway starting — ES: {ES_BACKEND}, Kibana: {KIBANA_BACKEND}")
    # Pre-warm the Symphonym model (lazy import — won't crash if unavailable)
    try:
        from . import symphonym
        symphonym.get_model()
        logger.info("Symphonym model ready")
    except Exception as e:
        logger.warning(f"Symphonym model not available: {e} — /api/search/phonetic will fail")
    sweeper = asyncio.create_task(_inflight_sweeper())
    yield
    sweeper.cancel()
    try:
        await sweeper
    except asyncio.CancelledError:
        pass
    await close_http_client()
    logger.info("Gateway shut down")


app = FastAPI(
    title="WHG API Gateway",
    version="0.1.0",
    docs_url=None,  # Disable Swagger UI (we're a proxy)
    redoc_url=None,
    lifespan=lifespan,
)

@app.middleware("http")
async def track_request_duration(request: Request, call_next):
    """Register the request while it runs; log it if it finishes slowly."""
    rid = next(_req_seq)
    path, client = _describe(request)
    started = time.monotonic()
    _inflight[rid] = (started, request.method, path, client)
    try:
        return await call_next(request)
    finally:
        _inflight.pop(rid, None)
        elapsed = time.monotonic() - started
        if elapsed >= SLOW_REQUEST_SECONDS:
            logger.warning("pid %d: slow request %.1fs %s %s from %s",
                           os.getpid(), elapsed, request.method, path, client)


# Mount reconciliation search router (must be before catch-all proxy routes)
app.include_router(reconcile_router)
# Mount search + suggest router
app.include_router(search_router)
# Mount places data endpoint
app.include_router(places_router)
# Mount data extension endpoint (OpenRefine extend)
app.include_router(extend_router)
# Mount re-ingest endpoints (admin-triggered authority refresh)
app.include_router(reingest_router)
# Mount contributor hard-link receiver (POST/DELETE /api/links) — must precede
# the catch-all proxy below (routers registered here match first).
app.include_router(links_router)


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

    # Degraded stores, reported rather than inferred. Before place#241/#242 a
    # gateway serving without hard links, or without a phonetic model, looked
    # from outside exactly like one whose stores held nothing — and a gateway
    # WEDGED on an unreachable store could not report anything at all, because
    # it was blocked in a syscall instead of running code.
    degraded: dict = {}
    try:
        from . import symphonym
        degraded["symphonym"] = symphonym.status()
    except Exception as exc:  # pragma: no cover — diagnostics must never 500
        degraded["symphonym"] = {"error": str(exc)}
    try:
        from .hard_link_expansion import store_degraded, store_stats
        degraded["hard_links"] = {"degraded": store_degraded(), "stores": store_stats()}
    except Exception as exc:  # pragma: no cover
        degraded["hard_links"] = {"error": str(exc)}

    return {
        "gateway": "ok",
        "elasticsearch": es_status,
        "backends": {
            "es": ES_BACKEND,
            "kibana": KIBANA_BACKEND,
        },
        "stores": degraded,
    }


# ---- Custom API Endpoints ----

class PhoneticMatch(BaseModel):
    """A unique orthography with all its language attestations."""
    name: str
    score: float = Field(description="Best KNN similarity score across all language variants")
    langs: list[str] = Field(default=[], description="Languages this orthography appears in")
    scripts: list[str] = Field(default=[], description="Scripts detected for this orthography")
    namespaces: list[str] = []
    attestations: list[str] = []


class PhoneticSearchResponse(BaseModel):
    """Response from /api/search/phonetic."""
    query: str
    lang: str
    embedding: list[int] = Field(
        description="Int8 quantised query embedding (128-d)"
    )
    total: int
    matches: list[PhoneticMatch]


class EmbedRequest(BaseModel):
    """Request body for batch embedding."""
    items: list[tuple[str, str]] = Field(
        description="List of (name, lang) pairs to embed"
    )


class EmbedResponse(BaseModel):
    """Response from /api/embed."""
    count: int
    embeddings: list[list[int]] = Field(
        description="Int8 quantised embeddings (N × 128)"
    )


@app.get("/api/search/phonetic", response_model=PhoneticSearchResponse)
async def phonetic_search(
    q: str = Query(..., description="Query toponym (any script)"),
    lang: str = Query("und", description="ISO 639-1 language code"),
    k: int = Query(10, ge=1, le=100, description="Number of results"),
    num_candidates: int = Query(100, ge=10, le=1000, description="KNN candidate pool per shard"),
    namespace: Optional[str] = Query(None, description="Filter by namespace (e.g. gn, wd, tgn)"),
    script: Optional[str] = Query(None, description="Filter by script (e.g. LATIN, CYRILLIC)"),
):
    """
    Phonetic similarity search using Symphonym embeddings.

    Generates a Symphonym embedding for the query toponym, then performs
    an ES KNN search against the `embedding` field in the toponyms index
    to find phonetically similar place names across scripts and languages.

    Examples:
      /api/search/phonetic?q=London&lang=en
      /api/search/phonetic?q=Лондон&lang=ru&k=20
      /api/search/phonetic?q=القدس&lang=ar&namespace=wd
    """
    from . import symphonym

    # Build optional ES filter
    es_filter = None
    filter_clauses = []
    if namespace:
        filter_clauses.append({"term": {"namespaces": namespace}})
    if script:
        filter_clauses.append({"term": {"script": script}})
    if filter_clauses:
        es_filter = {"bool": {"must": filter_clauses}} if len(filter_clauses) > 1 else filter_clauses[0]

    # Over-fetch from ES so we get enough unique orthographies after aggregation
    es_k = min(k * 10, 1000)

    # Generate embedding and build KNN query
    query_body = symphonym.build_knn_query(
        name=q,
        lang=lang,
        k=es_k,
        num_candidates=max(num_candidates, es_k),
        extra_filter=es_filter,
    )
    query_embedding = query_body["knn"]["query_vector"]

    # Execute against ES
    import httpx
    auth = None
    password = get_elastic_password()
    if password:
        auth = ("elastic", password)

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{ES_BACKEND}/{TOPONYMS_INDEX}/_search",
            json=query_body,
            auth=auth,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        es_result = r.json()

    # Aggregate hits by orthography (name string)
    hits = es_result.get("hits", {}).get("hits", [])
    grouped: dict[str, dict] = {}
    for hit in hits:
        src = hit.get("_source", {})
        name = src.get("name", "")
        score = hit.get("_score", 0.0)

        if name not in grouped:
            grouped[name] = {
                "score": score,
                "langs": set(),
                "scripts": set(),
                "namespaces": set(),
                "attestations": set(),
            }
        entry = grouped[name]
        entry["score"] = max(entry["score"], score)
        if src.get("lang"):
            entry["langs"].add(src["lang"])
        if src.get("script"):
            entry["scripts"].add(src["script"])
        entry["namespaces"].update(src.get("namespaces") or [])
        entry["attestations"].update(src.get("attestations") or [])

    # Sort by best score, take top k
    sorted_names = sorted(grouped.items(), key=lambda x: x[1]["score"], reverse=True)[:k]

    matches = []
    for name, entry in sorted_names:
        matches.append(PhoneticMatch(
            name=name,
            score=entry["score"],
            langs=sorted(entry["langs"]),
            scripts=sorted(entry["scripts"]),
            namespaces=sorted(entry["namespaces"]),
            attestations=sorted(entry["attestations"]),
        ))

    return PhoneticSearchResponse(
        query=q,
        lang=lang,
        embedding=query_embedding,
        total=len(matches),
        matches=matches,
    )


@app.post("/api/embed", response_model=EmbedResponse)
async def embed_toponyms(body: EmbedRequest):
    """
    Generate Symphonym embeddings for a batch of toponyms.

    Accepts a list of (name, lang) pairs and returns int8 quantised
    128-dimensional embeddings suitable for ES indexing or client-side
    similarity computation.

    Request body:
      {"items": [["London", "en"], ["Лондон", "ru"], ["伦敦", "zh"]]}
    """
    from . import symphonym

    embeddings_float = symphonym.embed_batch(body.items)
    embeddings_byte = [
        symphonym.quantize_to_byte(embeddings_float[i])
        for i in range(len(embeddings_float))
    ]

    return EmbedResponse(
        count=len(embeddings_byte),
        embeddings=embeddings_byte,
    )


@app.get("/api/embed")
async def embed_single(
    q: str = Query(..., description="Toponym string"),
    lang: str = Query("und", description="ISO 639-1 language code"),
):
    """
    Generate a Symphonym embedding for a single toponym.

    Returns the int8 quantised 128-d embedding.

    Example: /api/embed?q=London&lang=en
    """
    from . import symphonym

    emb = symphonym.embed(q, lang=lang)
    return {
        "name": q,
        "lang": lang,
        "embedding": symphonym.quantize_to_byte(emb),
    }


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

