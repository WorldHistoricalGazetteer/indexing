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
from typing import List, Optional

from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Request, WebSocket
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from .config import (
    ES_BACKEND,
    KIBANA_BACKEND,
    KIBANA_HOST_KEYWORD,
    TOPONYMS_INDEX,
    get_elastic_password,
)
from .proxy import proxy_http, proxy_websocket, close_http_client
from .reconcile import router as reconcile_router

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
    # Pre-warm the Symphonym model (lazy import — won't crash if unavailable)
    try:
        from . import symphonym
        symphonym.get_model()
        logger.info("Symphonym model ready")
    except Exception as e:
        logger.warning(f"Symphonym model not available: {e} — /api/search/phonetic will fail")
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

# Mount reconciliation search router (must be before catch-all proxy routes)
app.include_router(reconcile_router)


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
        entry["namespaces"].update(src.get("namespaces", []))
        entry["attestations"].update(src.get("attestations", []))

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

