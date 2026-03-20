# clustering/harvest/contributor_links.py
"""
Phase 1B — Harvest contributor reconciliation links from the WHG
PostgreSQL database on the DigitalOcean VM.

Based on RECON_NOTES §8.2.7 findings:
- Primary table: ``place_link`` (2.2M rows)
- ``jsonb->>'identifier'`` contains namespaced target IDs (e.g. wd:Q90, gn:745044)
  or full URLs (https://www.wikidata.org/wiki/Q90)
- ``place_id`` is a Django FK → ``places`` table (integer PK)
- Source places from contributed datasets are namespaced in ES as
  ``whg:place:{django_pk}`` (e.g. ``whg:place:169687``).  These are not
  yet indexed but the mapping is pre-wired so that Phase 1B will begin
  producing pairwise links as soon as WHG places appear in the ES index.
- The ``place_link.created`` timestamp enables incremental harvesting
- Only links where both source and target resolve to ES namespaces are useful
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import asyncpg
from tqdm import tqdm

from ..config import KNOWN_ES_NAMESPACES, ClusterConfig
from ..schemas import PairwiseDoc

logger = logging.getLogger("clustering.harvest.contributor_links")

# ---------------------------------------------------------------------------
# Identifier normalisation
# ---------------------------------------------------------------------------

# Patterns for full-URL identifiers that need mapping to namespace:id
_URL_PATTERNS = [
    (re.compile(r"https?://(?:www\.)?wikidata\.org/(?:wiki|entity)/(Q\d+)"), "wd"),
    (re.compile(r"https?://(?:www\.)?geonames\.org/(\d+)"), "gn"),
    (re.compile(r"https?://pleiades\.stoa\.org/places/(\d+)"), "pl"),
]


def normalise_identifier(raw: str) -> str | None:
    """
    Normalise a place_link identifier to ``namespace:id`` format.

    Returns None if the identifier cannot be mapped to a known ES namespace.
    """
    if not raw:
        return None

    # Already namespaced?
    if ":" in raw and not raw.startswith("http"):
        ns = raw.split(":")[0]
        if ns in KNOWN_ES_NAMESPACES:
            return raw
        return None  # unknown namespace (viaf, loc, gnd, etc.)

    # Try URL patterns
    for pattern, ns in _URL_PATTERNS:
        m = pattern.match(raw)
        if m:
            return f"{ns}:{m.group(1)}"

    return None


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

_QUERY_ALL_LINKS = """
    SELECT
        pl.id,
        pl.place_id AS django_place_id,
        pl.jsonb->>'type' AS link_type,
        pl.jsonb->>'identifier' AS identifier,
        pl.task_id,
        pl.created,
        p.dataset,
        p.src_id
    FROM place_link pl
    JOIN places p ON p.id = pl.place_id
    WHERE pl.jsonb->>'type' IN ('closeMatch', 'exactMatch')
      AND pl.jsonb->>'identifier' IS NOT NULL
"""

_QUERY_INCREMENTAL_SUFFIX = """
      AND pl.created > $1
"""


async def harvest_contributor_links(
    conn: asyncpg.Connection,
    cfg: ClusterConfig,
    since: Optional[datetime] = None,
) -> list[PairwiseDoc]:
    """
    Query the WHG PostgreSQL database for contributor reconciliation links
    and return pairwise docs.

    Args:
        conn: asyncpg connection (via SSH tunnel).
        cfg: Cluster configuration.
        since: If provided, only process links created after this timestamp.

    Returns:
        List of PairwiseDoc instances (deduplicated by canonical pair).
    """
    if since is not None:
        query = _QUERY_ALL_LINKS + _QUERY_INCREMENTAL_SUFFIX
        rows = await conn.fetch(query, since)
    else:
        rows = await conn.fetch(_QUERY_ALL_LINKS)

    logger.info("Phase 1B: fetched %d place_link rows from PostgreSQL", len(rows))

    pairs: dict[str, PairwiseDoc] = {}
    skipped_no_target = 0
    skipped_no_source = 0

    pbar = tqdm(
        total=len(rows),
        desc="Phase 1B: contributor links",
        unit="row",
        miniinterval=2.0,
    )

    for row in rows:
        # Normalise target identifier
        raw_identifier = row["identifier"]
        target_id = normalise_identifier(raw_identifier)
        if target_id is None:
            skipped_no_target += 1
            pbar.update(1)
            continue

        # Map source place to ES place_id.
        # For authority datasets the ES place_id is {namespace}:{src_id}.
        # For contributed datasets it is whg:place:{django_pk}.
        dataset_label = row["dataset"]
        src_id = row["src_id"]
        django_pk = row["django_place_id"]

        source_id = _map_source_to_es_id(dataset_label, src_id, django_pk)
        if source_id is None:
            skipped_no_source += 1
            pbar.update(1)
            continue

        source_ns = PairwiseDoc.extract_namespace(source_id)
        target_ns = PairwiseDoc.extract_namespace(target_id)

        # Must be cross-namespace
        if source_ns == target_ns:
            pbar.update(1)
            continue

        pid_a, pid_b = PairwiseDoc.canonical_pair(source_id, target_id)
        doc_id = PairwiseDoc.make_id(pid_a, pid_b)

        if doc_id not in pairs:
            pairs[doc_id] = PairwiseDoc(
                place_id_a=pid_a,
                place_id_b=pid_b,
                namespace_a=PairwiseDoc.extract_namespace(pid_a),
                namespace_b=PairwiseDoc.extract_namespace(pid_b),
                score=1.0,
                link_class="contributor_sameAs",
                link_method="whg_reconciliation",
                algorithm_version=cfg.algorithm_version,
            )
        else:
            existing = pairs[doc_id]
            if "whg_reconciliation" not in existing.link_method:
                existing.link_method = f"{existing.link_method},whg_reconciliation"

        pbar.update(1)

    pbar.close()

    logger.info(
        "Phase 1B complete: %d unique pairs, "
        "%d skipped (unmappable target), %d skipped (unmappable source)",
        len(pairs),
        skipped_no_target,
        skipped_no_source,
    )
    return list(pairs.values())


# Dataset label → ES namespace mapping
# Only authority datasets whose labels match ES namespace prefixes.
_DATASET_NS_MAP = {
    "geonames": "gn",
    "wikidata": "wd",
    "osm": "osm",
    "tgn": "tgn",
    "pleiades": "pl",
    "gb1900": "gb",
    "indexvillaris": "iv",
    "nativeland": "nl",
    "dplace": "dp",
    "un_countries": "un",
}


def _map_source_to_es_id(
    dataset_label: str, src_id: str, django_pk: int
) -> str:
    """
    Map a Django ``(dataset, src_id, pk)`` triple to an ES ``place_id``.

    - **Authority datasets** whose label matches a known ES namespace are
      mapped directly: ``{namespace}:{src_id}`` (e.g. ``gn:745044``).
    - **Contributed datasets** (user uploads) are mapped via the Django
      primary key: ``whg:place:{django_pk}`` (e.g. ``whg:place:169687``).
      These will resolve in ES once WHG places are indexed.

    Always returns a value — contributed places fall through to the
    ``whg:place:`` mapping.  The caller is responsible for checking
    whether the resulting place_id actually exists in the ES index
    if strict validation is required.
    """
    # Check direct namespace match (dataset label IS the namespace)
    if dataset_label in KNOWN_ES_NAMESPACES:
        return f"{dataset_label}:{src_id}"

    # Check mapped dataset labels (e.g. "geonames" → "gn")
    ns = _DATASET_NS_MAP.get(dataset_label.lower())
    if ns:
        return f"{ns}:{src_id}"

    # Contributed dataset → whg:place:{django_pk}
    return f"whg:place:{django_pk}"

