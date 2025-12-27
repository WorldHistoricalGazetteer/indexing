"""
Optimized deduplicate_and_index_toponyms function.

Optimizations:
1. Composite aggregation page size increased to 65K (ES max)
2. Parallel bulk indexing with 8 threads
3. Parse name/lang in Python, skip pipeline
4. Larger batch size (100K)
5. Refresh disabled during indexing
"""

import sys
from datetime import datetime, timedelta
from elasticsearch import Elasticsearch
from elasticsearch.helpers import parallel_bulk


def deduplicate_and_index_toponyms(es, places_index, toponyms_index):
    """
    Extract unique toponym_ids from places.toponyms (nested)
    and index them into the toponyms index.

    Optimized for speed with parallel bulk and no pipeline.
    """
    print("\n" + "=" * 80)
    print("DEDUPLICATING AND INDEXING TOPONYMS (OPTIMIZED)")
    print("=" * 80)
    sys.stdout.flush()

    start_time = datetime.now()
    now = datetime.utcnow().isoformat() + "Z"

    # Ensure refresh is disabled
    es.indices.put_settings(index=toponyms_index, body={"index.refresh_interval": "-1"})

    # Estimate total
    print("Estimating total unique toponyms...")
    sys.stdout.flush()

    count_query = {
        "size": 0,
        "aggs": {
            "toponyms_nested": {
                "nested": {"path": "toponyms"},
                "aggs": {
                    "unique_count": {
                        "cardinality": {
                            "field": "toponyms.toponym_id",
                            "precision_threshold": 40000
                        }
                    }
                }
            }
        }
    }
    count_resp = es.search(index=places_index, body=count_query, request_timeout=300)
    estimated_total = count_resp["aggregations"]["toponyms_nested"]["unique_count"]["value"]
    page_size = 65000  # ES max for composite
    estimated_pages = max(1, int(estimated_total / page_size) + 1)
    print(f"Estimated unique toponyms: ~{estimated_total:,}")
    print(f"Estimated pages: ~{estimated_pages:,} (at {page_size:,} per page)\n")
    sys.stdout.flush()

    # Composite aggregation query
    query = {
        "size": 0,
        "aggs": {
            "toponyms_nested": {
                "nested": {"path": "toponyms"},
                "aggs": {
                    "unique_toponyms": {
                        "composite": {
                            "size": page_size,
                            "sources": [
                                {"toponym": {"terms": {"field": "toponyms.toponym_id"}}}
                            ]
                        }
                    }
                }
            }
        }
    }

    after_key = None
    page = 0
    indexed_count = 0
    skipped_count = 0
    batch = []
    BATCH_SIZE = 100000

    def generate_actions():
        """Generator for parallel_bulk."""
        for doc in batch:
            yield doc

    def flush_batch():
        """Index current batch using parallel_bulk."""
        nonlocal indexed_count, batch
        if not batch:
            return

        success = 0
        for ok, result in parallel_bulk(es, generate_actions(), thread_count=8,
                                        raise_on_error=False, raise_on_exception=False):
            if ok:
                success += 1
        indexed_count += success
        batch = []

    try:
        while True:
            page += 1
            if after_key:
                query["aggs"]["toponyms_nested"]["aggs"]["unique_toponyms"]["composite"]["after"] = after_key

            resp = es.search(index=places_index, body=query, request_timeout=600)
            agg = resp["aggregations"]["toponyms_nested"]["unique_toponyms"]
            buckets = agg["buckets"]

            if not buckets:
                break

            percent = (page / estimated_pages) * 100 if estimated_pages > 0 else 0
            elapsed = (datetime.now() - start_time).total_seconds()
            rate = page / elapsed if elapsed > 0 else 0
            topo_rate = (indexed_count + len(batch)) / elapsed if elapsed > 0 else 0

            if rate > 0 and page < estimated_pages:
                remaining_pages = estimated_pages - page
                eta_seconds = int(remaining_pages / rate)
                eta_str = str(timedelta(seconds=eta_seconds))
            else:
                eta_str = "--:--:--"

            print(f"\r  Page {page:,}/{estimated_pages:,} ({percent:.1f}%) | "
                  f"Indexed: {indexed_count:,} | "
                  f"Rate: {topo_rate:,.0f}/s | "
                  f"ETA: {eta_str}    ",
                  end="", flush=True)

            for bucket in buckets:
                toponym_id = bucket["key"]["toponym"]

                # Skip if too long (512 byte ES limit)
                if len(toponym_id.encode("utf-8")) > 500:
                    skipped_count += 1
                    continue

                # Parse name and lang in Python (skip pipeline)
                at_pos = toponym_id.rfind('@')
                if at_pos <= 0:
                    skipped_count += 1
                    continue

                name = toponym_id[:at_pos]
                lang_part = toponym_id[at_pos + 1:]

                if not name or not lang_part:
                    skipped_count += 1
                    continue

                # Handle lang variants (e.g., "en-GB")
                if '-' in lang_part:
                    lang_parts = lang_part.split('-', 1)
                    lang = lang_parts[0]
                    lang_variant = lang_parts[1]
                else:
                    lang = lang_part
                    lang_variant = None

                doc = {
                    "_index": toponyms_index,
                    "_id": toponym_id,
                    "_source": {
                        "toponym_id": toponym_id,
                        "name": name,
                        "name_lower": name.lower(),
                        "lang": lang,
                        "indexed_at": now
                    }
                }

                if lang_variant:
                    doc["_source"]["lang_variant"] = lang_variant

                batch.append(doc)

            # Flush when batch is large enough
            if len(batch) >= BATCH_SIZE:
                flush_batch()

            after_key = agg.get("after_key")
            if not after_key:
                break

        # Final batch
        flush_batch()

    except Exception as e:
        print(f"\nERROR during toponym deduplication: {e}")
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        return False

    # Re-enable refresh and refresh
    es.indices.put_settings(index=toponyms_index, body={"index.refresh_interval": "1s"})
    es.indices.refresh(index=toponyms_index)

    elapsed = datetime.now() - start_time
    final_count = es.count(index=toponyms_index)["count"]

    print("\n\n✓ TOPONYM DEDUPLICATION COMPLETE")
    print(f"  Indexed: {indexed_count:,}")
    print(f"  Skipped: {skipped_count:,}")
    print(f"  Total in index: {final_count:,}")
    print(f"  Time elapsed: {str(elapsed).split('.')[0]}")
    sys.stdout.flush()

    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--es-host", default="http://localhost:9200")
    parser.add_argument("--places-index", default="places")
    parser.add_argument("--toponyms-index", default="toponyms")
    args = parser.parse_args()

    es = Elasticsearch(args.es_host, request_timeout=120)
    deduplicate_and_index_toponyms(es, args.places_index, args.toponyms_index)