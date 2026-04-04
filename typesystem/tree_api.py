# types/tree_api.py

"""
Type tree API endpoints for the search UI type selector widget.

Ported from the legacy Django placetypes app to query ES instead of
PostgreSQL.  Designed to be mounted into the FastAPI gateway.

Endpoints:
    GET /api/types/tree              → root-level categories
    GET /api/types/tree/{aat_id}     → children of a node
    GET /api/types/search?q=...      → search types by label
    GET /api/types/{aat_id}/descendants → all descendant AAT IDs
    GET /api/types/categories        → fclass category → AAT IDs mapping

Usage (standalone test):
    python -m typesystem.tree_api --es-host URL

Mount into gateway:
    from typesystem.tree_api import create_router
    app.include_router(create_router(es_client), prefix="/api/types")
"""

import re
import unicodedata

from fastapi import APIRouter, Query

from typesystem.aat_config import (
    AAT_TREE_PROMOTE_TO_ROOT,
    AAT_TREE_SKIP_NODES,
    CATEGORY_LABELS,
)

TYPES_INDEX = "types"

_skip_and_promote = AAT_TREE_SKIP_NODES | AAT_TREE_PROMOTE_TO_ROOT


def _strip_diacritics(text):
    """Remove combining diacritical marks."""
    if not text:
        return text or ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if unicodedata.category(ch)[0] != "M")


def _clean_label(term, parent_label=None):
    """
    Clean an AAT term for tree-widget display.
    Returns (label, is_guide).
    """
    label = term.strip()
    is_guide = False

    if re.fullmatch(r'aat:\d+', label):
        return None, False

    cleaned = re.sub(r'\s*\(hierarchy name\s*\)\s*$', '', label)
    if cleaned != label:
        label = cleaned.strip().lower()

    if label.startswith('<') and label.endswith('>'):
        is_guide = True
        inner = label[1:-1].strip()
        by_match = re.search(r'\b(by\b.+)', inner)
        label = by_match.group(1) if by_match else inner

    if parent_label:
        stripped = re.sub(
            r'\s*\(' + re.escape(parent_label) + r'\)\s*$',
            '', label, flags=re.IGNORECASE,
        ).strip()
        if stripped:
            label = stripped

    return label, is_guide


async def _es_search(es, body, size=1000):
    """Run an ES search against the types index.

    Accepts a dict with query/sort/size/_source keys and translates
    to elasticsearch-py 9.x keyword arguments (body= is no longer supported).
    """
    kwargs = {}
    for key in ("query", "sort", "aggs"):
        if key in body:
            kwargs[key] = body[key]
    if "_source" in body:
        kwargs["source"] = body["_source"]
    kwargs["size"] = body.get("size", size)
    resp = await es.search(index=TYPES_INDEX, **kwargs)
    return [hit["_source"] for hit in resp["hits"]["hits"]]


async def _get_children(es, parent_path, parent_depth):
    """Get direct children of a node by path prefix + depth."""
    body = {
        "query": {
            "bool": {
                "must": [
                    {"prefix": {"path": f"{parent_path}."}},
                    {"term": {"depth": parent_depth + 1}},
                    {"term": {"is_place_type": True}},
                ]
            }
        },
        "sort": [{"term.keyword": "asc"}],
    }
    return await _es_search(es, body)


async def _has_children(es, node_path, node_depth):
    """Check if a node has visible children."""
    body = {
        "query": {
            "bool": {
                "must": [
                    {"prefix": {"path": f"{node_path}."}},
                    {"term": {"depth": node_depth + 1}},
                    {"term": {"is_place_type": True}},
                ],
                "must_not": [
                    {"terms": {"aat_id": list(_skip_and_promote)}},
                ],
            }
        },
        "size": 0,
    }
        resp = await es.search(
            index=TYPES_INDEX,
            query=body["query"],
            size=0,
        )
        if resp["hits"]["total"]["value"] > 0:
        return True

    # Check skip-node children
    skip_body = {
        "query": {
            "bool": {
                "must": [
                    {"prefix": {"path": f"{node_path}."}},
                    {"term": {"depth": node_depth + 1}},
                    {"term": {"is_place_type": True}},
                    {"terms": {"aat_id": list(AAT_TREE_SKIP_NODES)}},
                ]
            }
        },
    }
    skip_nodes = await _es_search(es, skip_body, size=10)
    for skip in skip_nodes:
        check_body = {
            "query": {
                "bool": {
                    "must": [
                        {"prefix": {"path": f"{skip['path']}."}},
                        {"term": {"depth": skip["depth"] + 1}},
                        {"term": {"is_place_type": True}},
                    ],
                    "must_not": [
                        {"terms": {"aat_id": list(AAT_TREE_PROMOTE_TO_ROOT)}},
                    ],
                }
            },
            "size": 0,
        }
        resp = await es.search(
            index=TYPES_INDEX,
            query=check_body["query"],
            size=0,
        )
        if resp["hits"]["total"]["value"] > 0:
            return True

    return False


def _to_node(doc, has_kids, parent_label=None):
    """Convert an ES doc to a jstree-compatible node dict."""
    text, guide = _clean_label(doc["term"], parent_label)
    if text is None:
        if doc.get("note"):
            first_sentence = re.split(r'[.;]', doc["note"], maxsplit=1)[0].strip()
            text = first_sentence[:80] or f"[unnamed type aat:{doc['aat_id']}]"
        else:
            text = f"[unnamed type aat:{doc['aat_id']}]"

    return {
        "id": f"aat:{doc['aat_id']}",
        "aat_id": doc["aat_id"],
        "text": text,
        "fclasses": doc.get("fclasses", []),
        "guide": guide,
        "children": True if has_kids else [],
    }


def create_router(es_client):
    """Create FastAPI router for the types tree API."""
    router = APIRouter()

    @router.get("/tree")
    async def type_tree_roots():
        """Top-level root categories."""
        # Depth-0 entry points
        body = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"depth": 0}},
                        {"term": {"is_place_type": True}},
                    ]
                }
            },
            "sort": [{"term.keyword": "asc"}],
        }
        roots = await _es_search(es_client, body)
        seen = {doc["aat_id"] for doc in roots}

        # Add promoted nodes
        if AAT_TREE_PROMOTE_TO_ROOT:
            promo_body = {
                "query": {
                    "bool": {
                        "must": [
                            {"terms": {"aat_id": list(AAT_TREE_PROMOTE_TO_ROOT)}},
                            {"term": {"is_place_type": True}},
                        ]
                    }
                },
            }
            promoted = await _es_search(es_client, promo_body)
            for doc in promoted:
                if doc["aat_id"] not in seen:
                    roots.append(doc)
                    seen.add(doc["aat_id"])

        nodes = []
        for doc in roots:
            has_kids = await _has_children(es_client, doc["path"], doc["depth"])
            nodes.append(_to_node(doc, has_kids))

        nodes.sort(key=lambda n: n["text"].lower())
        return nodes

    @router.get("/tree/{aat_id}")
    async def type_tree_children(aat_id: int):
        """Children of a specific node."""
        # Find the parent
        body = {
            "query": {"term": {"aat_id": aat_id}},
        }
        parents = await _es_search(es_client, body, size=1)
        if not parents:
            return []

        parent = parents[0]
        parent_label, _ = _clean_label(parent["term"])

        # Get direct children
        children = await _get_children(es_client, parent["path"], parent["depth"])

        # Process: skip promoted, reparent skip-nodes' children
        visible = []
        for child in children:
            if child["aat_id"] in AAT_TREE_PROMOTE_TO_ROOT:
                continue
            if child["aat_id"] in AAT_TREE_SKIP_NODES:
                # Reparent grandchildren
                grandchildren = await _get_children(
                    es_client, child["path"], child["depth"])
                for gc in grandchildren:
                    if gc["aat_id"] not in AAT_TREE_PROMOTE_TO_ROOT:
                        visible.append(gc)
                continue
            visible.append(child)

        visible.sort(key=lambda d: d["term"].lower())

        nodes = []
        for doc in visible:
            has_kids = await _has_children(es_client, doc["path"], doc["depth"])
            nodes.append(_to_node(doc, has_kids, parent_label))

        return nodes

    @router.get("/search")
    async def type_tree_search(q: str = Query(..., min_length=2)):
        """Search types by label."""
        body = {
            "query": {
                "bool": {
                    "must": [
                        {"match": {"term": q}},
                        {"term": {"is_place_type": True}},
                    ],
                    "must_not": [
                        {"prefix": {"term.keyword": "<"}},     # guide terms
                        {"regexp": {"term.keyword": "aat:[0-9]+"}},  # raw IDs
                    ],
                }
            },
            "sort": ["_score"],
            "size": 30,
        }
        docs = await _es_search(es_client, body, size=30)

        results = []
        for doc in docs:
            text, guide = _clean_label(doc["term"])
            if guide or text is None:
                continue
            ancestors = doc.get("ancestors", [])
            results.append({
                "aat_id": doc["aat_id"],
                "text": text,
                "ancestors": ancestors,
            })

        return results

    @router.get("/{aat_id}/descendants")
    async def type_descendants(aat_id: int, include_self: bool = True):
        """
        Return all descendant AAT IDs for a given concept.
        Used for query expansion in the search pipeline.
        """
        body = {
            "query": {"term": {"aat_id": aat_id}},
        }
        roots = await _es_search(es_client, body, size=1)
        if not roots:
            return [aat_id] if include_self else []

        root = roots[0]

        desc_body = {
            "query": {
                "bool": {
                    "must": [
                        {"prefix": {"path": f"{root['path']}."}},
                        {"term": {"is_place_type": True}},
                    ]
                }
            },
            "_source": ["aat_id"],
            "size": 10000,
        }
        resp = await es_client.search(
            index=TYPES_INDEX,
            query=desc_body["query"],
            source=["aat_id"],
            size=10000,
        )
        ids = [hit["_source"]["aat_id"] for hit in resp["hits"]["hits"]]

        if include_self:
            ids.append(aat_id)

        return sorted(set(ids))

    @router.get("/categories")
    async def type_categories():
        """
        Return fclass category → AAT identifier mappings.
        Used by the search UI for advanced type filtering.
        """
        result = []
        for fclass_code in sorted(CATEGORY_LABELS.keys()):
            label = CATEGORY_LABELS[fclass_code]
            body = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"fclasses": fclass_code}},
                            {"term": {"is_place_type": True}},
                        ]
                    }
                },
                "_source": ["aat_id"],
                "size": 10000,
            }
            resp = await es_client.search(
                index=TYPES_INDEX,
                query=body["query"],
                source=["aat_id"],
                size=10000,
            )
            identifiers = [
                f"aat:{hit['_source']['aat_id']}"
                for hit in resp["hits"]["hits"]
            ]
            result.append({
                "fclass": fclass_code,
                "label": label,
                "identifiers": identifiers,
                "count": len(identifiers),
            })

        return result

    return router


# ============================================================================
# Standalone test mode
# ============================================================================

if __name__ == "__main__":
    import argparse
    import asyncio
    from elasticsearch import AsyncElasticsearch

    parser = argparse.ArgumentParser(description="Test types tree API")
    parser.add_argument("--es-host", default="http://localhost:9200")
    parser.add_argument("--action", choices=["roots", "children", "search", "descendants"],
                        default="roots")
    parser.add_argument("--aat-id", type=int, help="AAT ID for children/descendants")
    parser.add_argument("--query", type=str, help="Search query")
    args = parser.parse_args()

    async def run():
        es = AsyncElasticsearch(args.es_host)
        try:
            if args.action == "roots":
                body = {
                    "query": {"bool": {"must": [
                        {"term": {"depth": 0}},
                        {"term": {"is_place_type": True}},
                    ]}},
                    "sort": [{"term.keyword": "asc"}],
                }
                docs = await _es_search(es, body)
                for doc in docs:
                    text, _ = _clean_label(doc["term"])
                    print(f"  aat:{doc['aat_id']}  {text}  fclasses={doc.get('fclasses', [])}")

            elif args.action == "search" and args.query:
                body = {
                    "query": {"match": {"term": args.query}},
                    "size": 20,
                }
                docs = await _es_search(es, body, size=20)
                for doc in docs:
                    text, _ = _clean_label(doc["term"])
                    print(f"  aat:{doc['aat_id']}  {text}")

            elif args.action == "children" and args.aat_id:
                body = {"query": {"term": {"aat_id": args.aat_id}}}
                parents = await _es_search(es, body, size=1)
                if parents:
                    parent = parents[0]
                    children = await _get_children(es, parent["path"], parent["depth"])
                    for doc in children:
                        text, _ = _clean_label(doc["term"])
                        print(f"  aat:{doc['aat_id']}  {text}")

            elif args.action == "descendants" and args.aat_id:
                body = {"query": {"term": {"aat_id": args.aat_id}}}
                roots = await _es_search(es, body, size=1)
                if roots:
                    root = roots[0]
                    desc_body = {
                        "query": {"prefix": {"path": f"{root['path']}."}},
                        "_source": ["aat_id", "term"],
                        "size": 100,
                    }
                    resp = await es.search(
                        index=TYPES_INDEX,
                        query=desc_body["query"],
                        source=["aat_id", "term"],
                        size=100,
                    )
                    for hit in resp["hits"]["hits"]:
                        doc = hit["_source"]
                        print(f"  aat:{doc['aat_id']}  {doc['term']}")
        finally:
            await es.close()

    asyncio.run(run())


