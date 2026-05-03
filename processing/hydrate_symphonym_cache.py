"""One-off bootstrap: hydrate the Symphonym persistent cache from the
production ``toponyms_*`` ES index.

The persistent cache (``processing.settings.SYMPHONYM_CACHE_DB``) is keyed on
``(toponym_id, model_version, checkpoint_hash)`` and stores the int8
quantised embedding bytes verbatim. After a fresh corpus rebuild the cache
is empty for the current ``(model_version, checkpoint_hash)`` pair, so the
next ``update_es compute`` run pays the full GPU cost (~28 h for 67 M
toponyms).

Production ES already holds essentially all of those embeddings (computed
on a previous corpus build with the same Symphonym checkpoint). This
script scrolls the live index, converts each doc's ``embedding`` field
(``dense_vector`` / ``element_type=byte``) back into raw int8 bytes, and
writes them into the cache under the *current* checkpoint hash. The
subsequent ``update_es compute`` then becomes ~99 % cache hits and
finishes in minutes.

Pre-requisites:
* ``--checkpoint`` is the same model file the ES embeddings were
  generated with (this is what the user asserts when running this
  script). The script SHA-256s the checkpoint to compute the cache key.
  If the production embeddings were built with a *different* checkpoint,
  the hydrated rows will look like cache hits but the embeddings won't
  match what the model would produce now — the user must verify before
  running.
* ``--embedding-version`` matches the integer the cache uses for the
  same model. Defaults to ``7`` (current Symphonym).
* The production ES is reachable at ``--es-host`` (defaults to
  ``http://localhost:9200`` — run from the Pitt VM).

Usage::

    python -m processing.hydrate_symphonym_cache \
        --checkpoint /ix1/ishi/models/phonetic/checkpoints/v7/phase3_best.pt \
        --embedding-version 7

    # Dry run — count eligible rows without writing.
    python -m processing.hydrate_symphonym_cache \
        --checkpoint /ix1/ishi/models/phonetic/checkpoints/v7/phase3_best.pt \
        --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterator

import numpy as np

from elasticsearch import Elasticsearch
from elasticsearch.helpers import scan

from phonetics.inference.symphonym_cache import (
    cache_connection,
    compute_checkpoint_hash,
    insert_many,
)
from processing.settings import SYMPHONYM_CACHE_DB


logger = logging.getLogger("hydrate_symphonym_cache")


def _embedding_to_bytes(values: list[int]) -> bytes:
    """Convert an int8 list (as ES returns ``element_type=byte`` arrays)
    into the raw byte pattern the cache stores. ES returns signed ints in
    the ``[-128, 127]`` range, which numpy.int8 handles natively."""
    return np.array(values, dtype=np.int8).tobytes()


def _iter_es_embeddings(
    client: Elasticsearch,
    *,
    index: str,
    embedding_version: int | None,
    scroll_size: int,
) -> Iterator[tuple[str, bytes]]:
    """Yield ``(toponym_id, embedding_bytes)`` for every doc with a
    populated ``embedding`` field."""
    query: dict = {"bool": {"must": [{"exists": {"field": "embedding"}}]}}
    if embedding_version is not None:
        query["bool"]["filter"] = [{"term": {"embedding_version": embedding_version}}]

    body = {
        "_source": ["toponym_id", "embedding"],
        "query": query,
    }

    seen = 0
    skipped = 0
    for hit in scan(
        client, index=index, query=body, size=scroll_size,
        request_timeout=300, preserve_order=False,
    ):
        src = hit.get("_source") or {}
        toponym_id = src.get("toponym_id") or hit.get("_id")
        emb = src.get("embedding")
        if not toponym_id or not isinstance(emb, list) or not emb:
            skipped += 1
            continue
        try:
            yield (str(toponym_id), _embedding_to_bytes(emb))
        except (ValueError, TypeError):
            skipped += 1
            continue
        seen += 1
        if seen % 100_000 == 0:
            logger.info(f"  scrolled {seen:,} embeddings (skipped {skipped:,})")


def hydrate(
    *,
    es_host: str,
    es_password: str | None,
    index_pattern: str,
    checkpoint_path: Path,
    embedding_version: int,
    cache_db: Path,
    dry_run: bool,
    scroll_size: int,
    batch_size: int,
    require_version_match: bool,
) -> dict:
    checkpoint_hash = compute_checkpoint_hash(checkpoint_path)
    logger.info(f"Checkpoint: {checkpoint_path}")
    logger.info(f"Checkpoint hash (cache key): {checkpoint_hash[:16]}…")
    logger.info(f"Embedding version (cache key): {embedding_version}")
    logger.info(f"Cache DB: {cache_db}")
    logger.info(f"ES index pattern: {index_pattern}")
    logger.info(
        f"ES embedding_version filter: "
        f"{embedding_version if require_version_match else '(none — accept any)'}"
    )

    auth = ("elastic", es_password) if es_password else None
    client = Elasticsearch(es_host, basic_auth=auth, verify_certs=False, request_timeout=300)

    if dry_run:
        body = {"query": {"bool": {"must": [{"exists": {"field": "embedding"}}]}}}
        if require_version_match:
            body["query"]["bool"]["filter"] = [
                {"term": {"embedding_version": embedding_version}}
            ]
        count = client.count(index=index_pattern, body=body)["count"]
        logger.info(f"DRY RUN — eligible docs in {index_pattern}: {count:,}")
        return {"eligible": count, "inserted": 0, "dry_run": True}

    started = time.time()
    inserted = 0
    seen = 0
    cache_db.parent.mkdir(parents=True, exist_ok=True)

    rows_buffer: list[tuple[str, bytes]] = []
    with cache_connection(cache_db) as conn:
        version_filter = embedding_version if require_version_match else None
        for pair in _iter_es_embeddings(
            client, index=index_pattern,
            embedding_version=version_filter, scroll_size=scroll_size,
        ):
            rows_buffer.append(pair)
            seen += 1
            if len(rows_buffer) >= batch_size:
                inserted += insert_many(
                    conn, rows_buffer,
                    model_version=embedding_version,
                    checkpoint_hash=checkpoint_hash,
                )
                rows_buffer.clear()
        if rows_buffer:
            inserted += insert_many(
                conn, rows_buffer,
                model_version=embedding_version,
                checkpoint_hash=checkpoint_hash,
            )

    elapsed = time.time() - started
    logger.info(
        f"Hydration complete. seen={seen:,} inserted={inserted:,} "
        f"elapsed={elapsed:.0f}s rate={seen/elapsed if elapsed else 0:,.0f}/s"
    )
    return {"seen": seen, "inserted": inserted, "elapsed_seconds": round(elapsed, 1)}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Bootstrap Symphonym cache from production ES toponyms",
    )
    parser.add_argument("--es-host", default=os.getenv("PROD_ES_URL", "http://localhost:9200"))
    parser.add_argument("--es-password-file",
                        default="/ix1/ishi/es/config/elastic.password",
                        help="File containing the elastic user password (default: production location)")
    parser.add_argument("--index-pattern", default="toponyms_*",
                        help="ES index pattern to scroll (default: toponyms_*)")
    parser.add_argument("--checkpoint", type=Path, required=True,
                        help="Path to the Symphonym model checkpoint (phase3_best.pt). "
                             "SHA-256 of this file becomes the cache key.")
    parser.add_argument("--embedding-version", type=int, default=7,
                        help="Embedding version integer (matches the production "
                             "embedding_version field; default: 7)")
    parser.add_argument("--cache-db", type=Path, default=Path(SYMPHONYM_CACHE_DB),
                        help=f"DuckDB cache path (default: {SYMPHONYM_CACHE_DB})")
    parser.add_argument("--scroll-size", type=int, default=2000,
                        help="ES scroll batch size (default: 2000)")
    parser.add_argument("--batch-size", type=int, default=5000,
                        help="Cache insert batch size (default: 5000)")
    parser.add_argument(
        "--no-require-version-match", action="store_true",
        help=(
            "Don't filter ES docs by embedding_version. Use only when you "
            "know the production index has embeddings under a different "
            "version field but they were still computed with the same "
            "checkpoint."
        ),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Count eligible docs and exit without writing")
    args = parser.parse_args()

    if not args.checkpoint.exists():
        sys.exit(f"ERROR: checkpoint not found: {args.checkpoint}")

    es_password = None
    pw_file = Path(args.es_password_file)
    if pw_file.exists():
        es_password = pw_file.read_text().strip()
    else:
        logger.warning(f"ES password file not found at {pw_file} — "
                       "trying without auth")

    summary = hydrate(
        es_host=args.es_host,
        es_password=es_password,
        index_pattern=args.index_pattern,
        checkpoint_path=args.checkpoint,
        embedding_version=args.embedding_version,
        cache_db=args.cache_db,
        dry_run=args.dry_run,
        scroll_size=args.scroll_size,
        batch_size=args.batch_size,
        require_version_match=not args.no_require_version_match,
    )
    print(summary)


if __name__ == "__main__":
    main()
