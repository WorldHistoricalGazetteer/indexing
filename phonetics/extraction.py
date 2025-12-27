"""
Data Extraction from Elasticsearch for Phonetic Model Training.

Handles:
- Toponym enrichment (IPA + PanPhon features)
- Training data extraction (places → clusters → pairs)
- HDF5 streaming output
- Post-processing deduplication

v2 Optimizations:
- Namespace filtering (-n gn to extract GeoNames only)
- Pre-filter toponyms to Epitran-supported languages with valid cached phonetics
- Efficient pair generation loops

v3 Optimizations:
- Parallel toponym enrichment with multiprocessing
- Process by language (one Epitran model at a time)
- Parallel bulk updates to ES
"""

import sys
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from multiprocessing import Pool
from typing import Dict, List, Optional, Set

import h5py
import numpy as np
import orjson

from processing.utilities import create_checkpoint_snapshot

try:
    import epitran
    from panphon import FeatureTable
    import panphon.distance
except ImportError:
    raise ImportError("Please install epitran and panphon: pip install epitran panphon")

try:
    from anyascii import anyascii
except ImportError:
    raise ImportError("Please install anyascii: pip install anyascii")

try:
    from elasticsearch import Elasticsearch, helpers
    from elasticsearch.helpers import parallel_bulk, scan
except ImportError:
    raise ImportError("Please install elasticsearch: pip install elasticsearch")

from .config import Config


def normalize_es_host(es_host: str) -> str:
    """Ensure ES host has proper URL scheme."""
    if not es_host:
        return 'http://localhost:9200'
    if not es_host.startswith(('http://', 'https://')):
        return f'http://{es_host}'
    return es_host


# =============================================================================
# MULTIPROCESSING WORKER FUNCTIONS (must be at module level)
# =============================================================================

# Global variables for worker processes (initialized once per worker)
_worker_epi = None
_worker_ft = None
_worker_lang = None


def _init_enrichment_worker(lang_code):
    """Initialize Epitran and PanPhon for a worker process."""
    global _worker_epi, _worker_ft, _worker_lang

    epitran_code = Config.EPITRAN_LANGS.get(lang_code)
    if epitran_code:
        try:
            _worker_epi = epitran.Epitran(epitran_code)
        except Exception:
            _worker_epi = None
    else:
        _worker_epi = None

    _worker_ft = FeatureTable()
    _worker_lang = lang_code


def _process_toponym_for_enrichment(item):
    """
    Process a single toponym - runs in worker process.

    Args:
        item: tuple of (doc_id, name, lang)

    Returns:
        tuple of (doc_id, ipa, features_json) or (doc_id, None, None) on failure
    """
    global _worker_epi, _worker_ft

    doc_id, name, lang = item

    if _worker_epi is None:
        return (doc_id, None, None)

    try:
        # Transliterate to IPA
        ipa = _worker_epi.transliterate(name)
        if not ipa or not ipa.strip():
            return (doc_id, None, None)

        # Get PanPhon features
        features = _worker_ft.word_to_vector_list(ipa, numeric=True)
        if not features or len(features) == 0:
            return (doc_id, None, None)

        # Validate features
        if not all(isinstance(f, (list, tuple)) and len(f) == 24 for f in features):
            return (doc_id, None, None)

        # Serialize features
        features_json = orjson.dumps(features, option=orjson.OPT_SERIALIZE_NUMPY).decode('utf-8')

        return (doc_id, ipa, features_json)

    except Exception:
        return (doc_id, None, None)


# =============================================================================
# TOPONYM ENRICHER
# =============================================================================

class ToponymEnricher:
    """
    Scans the 'toponyms' index and computes phonetic features for supported languages.
    Updates the documents in-place so they can be retrieved quickly later.

    Optimized with:
    - Multiprocessing for Epitran/PanPhon (CPU-bound)
    - Process by language (one Epitran model at a time)
    - Parallel bulk updates to ES
    - Pre-filter to supported languages only
    """

    def __init__(self, es_host='localhost:9200', index='toponyms', num_workers=12, batch_size=5000):
        self.es = Elasticsearch([normalize_es_host(es_host)], request_timeout=120)
        self.index = index
        self.num_workers = num_workers
        self.batch_size = batch_size

    def run(self):
        print("=" * 80)
        print("TOPONYM ENRICHMENT (OPTIMIZED PARALLEL)")
        print("=" * 80)
        print(f"Workers: {self.num_workers}")
        print(f"Batch size: {self.batch_size}")
        print(f"Supported languages: {len(Config.EPITRAN_LANGS)}")
        print()
        sys.stdout.flush()

        # Ensure mapping has phonetic fields
        try:
            self.es.indices.put_mapping(index=self.index, body={
                "properties": {
                    "ipa_cached": {"type": "keyword", "index": False, "doc_values": False},
                    "features_cached_json": {"type": "keyword", "index": False, "doc_values": False}
                }
            })
        except Exception as e:
            print(f"Mapping update warning: {e}")

        start_time = datetime.now()
        total_enriched = 0
        total_skipped = 0
        total_processed = 0

        # Get counts per language
        print("Counting toponyms per language...")
        sys.stdout.flush()

        lang_counts = {}
        for lang in Config.EPITRAN_LANGS.keys():
            count_query = {
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"lang": lang}},
                            {"bool": {"must_not": {"exists": {"field": "ipa_cached"}}}}
                        ]
                    }
                }
            }
            try:
                resp = self.es.count(index=self.index, body=count_query)
                count = resp['count']
                if count > 0:
                    lang_counts[lang] = count
            except Exception:
                pass

        total_to_process = sum(lang_counts.values())
        print(f"Total toponyms to enrich: {total_to_process:,}")
        print(f"Languages with data: {len(lang_counts)}")
        print()

        if total_to_process == 0:
            print("Nothing to enrich!")
            return

        # Sort languages by count (largest first for better progress visibility)
        sorted_langs = sorted(lang_counts.items(), key=lambda x: -x[1])

        for lang_idx, (lang, lang_count) in enumerate(sorted_langs, 1):
            lang_start = datetime.now()
            lang_enriched = 0
            lang_skipped = 0

            print(f"\n[{lang_idx}/{len(sorted_langs)}] Processing '{lang}' ({lang_count:,} toponyms)...")
            sys.stdout.flush()

            query = {
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"lang": lang}},
                            {"bool": {"must_not": {"exists": {"field": "ipa_cached"}}}}
                        ]
                    }
                }
            }

            batch = []
            updates = []

            # Initialize worker pool for this language
            with Pool(processes=self.num_workers,
                      initializer=_init_enrichment_worker,
                      initargs=(lang,)) as pool:

                scanner = scan(
                    self.es,
                    query=query,
                    index=self.index,
                    _source=["name"],
                    size=self.batch_size,
                    scroll='30m'
                )

                for hit in scanner:
                    doc_id = hit['_id']
                    name = hit['_source'].get('name', '')

                    batch.append((doc_id, name, lang))

                    if len(batch) >= self.batch_size:
                        # Process batch in parallel
                        results = pool.map(_process_toponym_for_enrichment, batch)

                        # Collect updates
                        for doc_id, ipa, features_json in results:
                            if ipa is not None:
                                updates.append({
                                    "_op_type": "update",
                                    "_index": self.index,
                                    "_id": doc_id,
                                    "doc": {
                                        "ipa_cached": ipa,
                                        "features_cached_json": features_json
                                    }
                                })
                                lang_enriched += 1
                            else:
                                lang_skipped += 1

                        batch = []

                        # Bulk update to ES
                        if len(updates) >= self.batch_size:
                            for ok, result in parallel_bulk(self.es, updates, thread_count=4,
                                                            raise_on_error=False,
                                                            raise_on_exception=False):
                                pass
                            updates = []

                        # Progress
                        elapsed = (datetime.now() - start_time).total_seconds()
                        done = total_enriched + total_skipped + lang_enriched + lang_skipped
                        rate = done / elapsed if elapsed > 0 else 0
                        if rate > 0:
                            eta = (total_to_process - done) / rate
                            eta_str = str(timedelta(seconds=int(eta)))
                        else:
                            eta_str = "--:--:--"

                        print(f"\r    {lang}: {lang_enriched + lang_skipped:,}/{lang_count:,} | "
                              f"Total: {done:,}/{total_to_process:,} | "
                              f"Rate: {rate:,.0f}/s | ETA: {eta_str}    ",
                              end="", flush=True)

                # Process remaining batch
                if batch:
                    results = pool.map(_process_toponym_for_enrichment, batch)
                    for doc_id, ipa, features_json in results:
                        if ipa is not None:
                            updates.append({
                                "_op_type": "update",
                                "_index": self.index,
                                "_id": doc_id,
                                "doc": {
                                    "ipa_cached": ipa,
                                    "features_cached_json": features_json
                                }
                            })
                            lang_enriched += 1
                        else:
                            lang_skipped += 1

            # Final updates for this language
            if updates:
                for ok, result in parallel_bulk(self.es, updates, thread_count=4,
                                                raise_on_error=False,
                                                raise_on_exception=False):
                    pass

            total_enriched += lang_enriched
            total_skipped += lang_skipped
            total_processed += lang_enriched + lang_skipped

            lang_elapsed = datetime.now() - lang_start
            print(f"\n    Done: {lang_enriched:,} enriched, {lang_skipped:,} skipped "
                  f"in {str(lang_elapsed).split('.')[0]}")
            sys.stdout.flush()

        # Final refresh
        print("\nRefreshing index...")
        self.es.indices.refresh(index=self.index)

        # Create snapshot
        print("\nCreating checkpoint snapshot...")
        create_checkpoint_snapshot(self.es, 'phonetic_enrichment')

        elapsed = datetime.now() - start_time

        # Final count
        enriched_count = self.es.count(
            index=self.index,
            body={"query": {"exists": {"field": "ipa_cached"}}}
        )['count']

        print("\n" + "=" * 80)
        print("ENRICHMENT COMPLETE")
        print("=" * 80)
        print(f"  Total processed: {total_processed:,}")
        print(f"  Enriched: {total_enriched:,}")
        print(f"  Skipped: {total_skipped:,}")
        print(f"  Total with phonetics: {enriched_count:,}")
        print(f"  Time elapsed: {str(elapsed).split('.')[0]}")
        if elapsed.total_seconds() > 0:
            print(f"  Rate: {total_processed / elapsed.total_seconds():,.0f} toponyms/s")
        sys.stdout.flush()

    def cleanup_invalid_phonetics(self):
        """
        Remove ipa_cached and features_cached_json from toponyms where
        the cached features are empty or invalid.
        """
        print("Scanning for invalid cached phonetics...")

        query = {
            "query": {
                "exists": {"field": "ipa_cached"}
            }
        }

        updates = []
        checked = 0
        invalid = 0

        scanner = helpers.scan(
            self.es,
            query=query,
            index=self.index,
            _source=["ipa_cached", "features_cached_json"],
            size=1000,
            scroll='2h'
        )

        for hit in scanner:
            checked += 1

            if checked % 100000 == 0:
                print(f"  Checked {checked:,}, found {invalid:,} invalid...", end='\r', flush=True)

            doc_id = hit['_id']
            source = hit['_source']

            ipa = source.get('ipa_cached', '')
            features_json = source.get('features_cached_json', '')

            is_invalid = False

            if not ipa or not ipa.strip():
                is_invalid = True

            if features_json:
                try:
                    features = orjson.loads(features_json)
                    if not features or len(features) == 0:
                        is_invalid = True
                    elif not all(isinstance(f, (list, tuple)) and len(f) == 24 for f in features):
                        is_invalid = True
                except:
                    is_invalid = True
            else:
                is_invalid = True

            if is_invalid:
                invalid += 1
                updates.append({
                    "_op_type": "update",
                    "_index": self.index,
                    "_id": doc_id,
                    "doc": {
                        "ipa_cached": None,
                        "features_cached_json": None
                    }
                })

            if len(updates) >= 500:
                helpers.bulk(self.es, updates, request_timeout=60)
                updates = []

        if updates:
            helpers.bulk(self.es, updates, request_timeout=60)

        print(f"\nChecked {checked:,} documents, cleaned {invalid:,} invalid entries.")


# =============================================================================
# TRAINING DATA EXTRACTOR
# =============================================================================

class TrainingDataExtractor:
    """
    Extracts clusters from 'places' index and looks up phonetics from 'toponyms'.

    Optimized v2:
    - Namespace filtering (e.g., -n gn for GeoNames only)
    - Pre-filters toponyms to Epitran languages with valid cached phonetics
    - Efficient pair generation
    """

    # Pre-compute supported languages set for fast lookup
    SUPPORTED_LANGS: Set[str] = frozenset(Config.EPITRAN_LANGS.keys())

    def __init__(self, es_host: str = 'localhost:9200', index_name: str = 'places'):
        self.es = Elasticsearch([normalize_es_host(es_host)], request_timeout=120)
        self.index = index_name
        self.dst = panphon.distance.Distance()

        # Cache for phonetic data: toponym_id -> {ipa, features} or None
        self._phonetic_cache: Dict[str, Optional[Dict]] = {}

    def phonetic_similarity(self, ipa_a: str, ipa_b: str) -> float:
        """Compute phonetic similarity between two IPA strings."""
        if not ipa_a or not ipa_b:
            return 0.0
        try:
            fed = self.dst.feature_edit_distance(ipa_a, ipa_b)
            max_len = max(len(ipa_a), len(ipa_b)) / 3
            if max_len == 0:
                return 0.0
            return max(0.0, 1.0 - (fed / max(1, max_len)))
        except:
            return 0.0

    def _fetch_phonetics_batch(self, toponym_ids: List[str]) -> Dict[str, Dict]:
        """
        Fetch cached phonetics for a batch of toponym_ids.
        Returns dict mapping toponym_id to {ipa, features} for valid entries only.

        Uses caching to avoid redundant MGET calls.
        """
        result = {}
        ids_to_fetch = []

        # Check cache first
        for tid in toponym_ids:
            if tid in self._phonetic_cache:
                cached = self._phonetic_cache[tid]
                if cached is not None:
                    result[tid] = cached
            else:
                ids_to_fetch.append(tid)

        if not ids_to_fetch:
            return result

        # Batch MGET for uncached IDs
        try:
            resp = self.es.mget(
                index='toponyms',
                body={'ids': ids_to_fetch},
                _source=['ipa_cached', 'features_cached_json']
            )
        except Exception as e:
            print(f"\nWarning: MGET failed: {e}")
            # Mark all as None in cache
            for tid in ids_to_fetch:
                self._phonetic_cache[tid] = None
            return result

        # Process results
        for doc in resp.get('docs', []):
            tid = doc['_id']

            if not doc.get('found'):
                self._phonetic_cache[tid] = None
                continue

            source = doc.get('_source', {})
            ipa = source.get('ipa_cached', '')
            features_json = source.get('features_cached_json', '')

            # Validate IPA
            if not ipa or not ipa.strip():
                self._phonetic_cache[tid] = None
                continue

            # Validate features JSON
            if not features_json:
                self._phonetic_cache[tid] = None
                continue

            try:
                features = orjson.loads(features_json)
                # Validate features structure
                if not features or len(features) == 0:
                    self._phonetic_cache[tid] = None
                    continue
                # Check first and last feature for structure (faster than checking all)
                if not (isinstance(features[0], (list, tuple)) and len(features[0]) == 24):
                    self._phonetic_cache[tid] = None
                    continue
                if len(features) > 1 and not (isinstance(features[-1], (list, tuple)) and len(features[-1]) == 24):
                    self._phonetic_cache[tid] = None
                    continue

                entry = {'ipa': ipa, 'features': features}
                self._phonetic_cache[tid] = entry
                result[tid] = entry

            except Exception:
                self._phonetic_cache[tid] = None

        return result

    def extract_optimized(
        self,
        output_path: str,
        namespaces: Optional[List[str]] = None,
        max_docs: Optional[int] = None,
        checkpoint_every: int = 100000
    ):
        """
        Optimized extraction with inline SQLite deduplication.

        Single-pass extraction that:
        1. Streams places from ES
        2. Deduplicates items and pairs via SQLite (on disk)
        3. Writes unique data to HDF5 at the end

        Args:
            output_path: HDF5 output file path
            namespaces: List of namespace prefixes to include (e.g., ['gn', 'wd'])
                       None means all namespaces
            max_docs: Maximum documents to process (for testing)
            checkpoint_every: Commit SQLite every N documents
        """
        import os
        import tempfile

        print("=" * 70)
        print("TRAINING DATA EXTRACTION (WITH INLINE DEDUPLICATION)")
        print("=" * 70)
        print(f"Output: {output_path}")
        print(f"Namespaces: {namespaces if namespaces else 'ALL'}")
        print(f"Supported languages: {len(self.SUPPORTED_LANGS)}")
        print()

        stats = {
            'docs_scanned': 0,
            'docs_with_clusters': 0,
            'toponyms_checked': 0,
            'toponyms_supported_lang': 0,
            'toponyms_with_phonetics': 0,
            'items_new': 0,
            'items_existing': 0,
            'pairs_new': 0,
            'pairs_existing': 0,
        }

        # Build query with namespace filter
        if namespaces:
            should_clauses = [{"prefix": {"place_id": f"{ns}:"}} for ns in namespaces]
            query_body = {
                "query": {
                    "bool": {
                        "should": should_clauses,
                        "minimum_should_match": 1
                    }
                }
            }
            print(f"Query filter: place_id prefix in {namespaces}")
        else:
            query_body = {"query": {"match_all": {}}}

        # Count total documents for progress
        count_resp = self.es.count(index=self.index, body=query_body)
        total_docs = count_resp['count']
        print(f"Total documents to process: {total_docs:,}")

        # Create temp SQLite database for deduplication
        db_fd, db_path = tempfile.mkstemp(suffix='.db', prefix='phonetic_extract_')
        os.close(db_fd)
        print(f"Temp database: {db_path}")
        print()

        try:
            # =========================================================
            # PHASE 1: Extract from ES into SQLite (with deduplication)
            # =========================================================
            print("--- PHASE 1: Extracting to SQLite ---")

            db = sqlite3.connect(db_path)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            db.execute('PRAGMA cache_size=-64000')  # 64MB cache

            # Items table: unique toponyms keyed by toponym@lang
            db.execute('''
                CREATE TABLE items (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_key TEXT UNIQUE,
                    toponym TEXT,
                    romanized TEXT,
                    lang TEXT,
                    ipa TEXT,
                    features BLOB
                )
            ''')

            # Pairs table: unique pairs keyed by sorted item keys
            db.execute('''
                CREATE TABLE pairs (
                    pair_key TEXT PRIMARY KEY,
                    item_key_a TEXT,
                    item_key_b TEXT,
                    similarity REAL
                )
            ''')

            db.commit()
            cursor = db.cursor()

            # Process places in batches
            search_after = None
            batch_size = 500
            start_time = time.time()
            last_report_time = start_time
            last_commit_docs = 0

            while True:
                query = {
                    "size": batch_size,
                    **query_body,
                    "_source": ["toponyms"],
                    "sort": ["_doc"]
                }

                if search_after:
                    query["search_after"] = search_after

                # Execute with retry
                resp = None
                for attempt in range(3):
                    try:
                        resp = self.es.search(index=self.index, body=query, request_timeout=120)
                        break
                    except Exception as e:
                        if attempt < 2:
                            print(f"\nSearch failed (attempt {attempt + 1}): {e}")
                            time.sleep(5)
                        else:
                            raise

                if not resp:
                    break

                hits = resp['hits']['hits']
                if not hits:
                    break

                # Collect candidate toponym_ids for batch MGET
                batch_candidates = []

                for hit_idx, hit in enumerate(hits):
                    if max_docs and stats['docs_scanned'] >= max_docs:
                        break

                    stats['docs_scanned'] += 1
                    raw_toponyms = hit['_source'].get('toponyms', [])

                    if not raw_toponyms:
                        continue

                    seen_in_cluster = set()

                    for t in raw_toponyms:
                        if not isinstance(t, dict):
                            continue

                        tid = t.get('toponym_id', '')
                        if not tid or '@' not in tid:
                            continue

                        at_pos = tid.rfind('@')
                        if at_pos <= 0:
                            continue

                        name = tid[:at_pos].strip()
                        lang = tid[at_pos + 1:].strip()

                        if not name or not lang:
                            continue

                        stats['toponyms_checked'] += 1

                        if lang not in self.SUPPORTED_LANGS:
                            continue

                        stats['toponyms_supported_lang'] += 1

                        # Dedupe within cluster
                        key = (name.lower(), lang.lower())
                        if key in seen_in_cluster:
                            continue
                        seen_in_cluster.add(key)

                        batch_candidates.append((hit_idx, tid, name, lang))

                # Batch fetch phonetics
                if batch_candidates:
                    all_tids = [tid for _, tid, _, _ in batch_candidates]
                    phonetics = self._fetch_phonetics_batch(all_tids)

                    # Group by hit_idx
                    hit_toponyms = defaultdict(list)
                    for hit_idx, tid, name, lang in batch_candidates:
                        if tid in phonetics:
                            stats['toponyms_with_phonetics'] += 1
                            hit_toponyms[hit_idx].append({
                                'name': name,
                                'lang': lang,
                                'ipa': phonetics[tid]['ipa'],
                                'features': phonetics[tid]['features']
                            })

                    # Process each place
                    for hit_idx, phonetic_toponyms in hit_toponyms.items():
                        if len(phonetic_toponyms) < 2:
                            continue

                        stats['docs_with_clusters'] += 1

                        # Insert/get items and collect keys for pair generation
                        item_keys = []

                        for topo in phonetic_toponyms:
                            item_key = f"{topo['name'].lower()}@{topo['lang'].lower()}"

                            # Try to insert (will fail silently if exists)
                            try:
                                cursor.execute(
                                    '''INSERT INTO items (item_key, toponym, romanized, lang, ipa, features)
                                       VALUES (?, ?, ?, ?, ?, ?)''',
                                    (
                                        item_key,
                                        topo['name'],
                                        anyascii(topo['name']).lower(),
                                        topo['lang'],
                                        topo['ipa'],
                                        orjson.dumps(topo['features'], option=orjson.OPT_SERIALIZE_NUMPY)
                                    )
                                )
                                stats['items_new'] += 1
                            except sqlite3.IntegrityError:
                                stats['items_existing'] += 1

                            item_keys.append((item_key, topo['ipa']))

                        # Generate pairs
                        n_items = len(item_keys)
                        sim_threshold = Config.SIMILARITY_THRESHOLD

                        for i in range(n_items - 1):
                            key_a, ipa_a = item_keys[i]

                            for j in range(i + 1, n_items):
                                key_b, ipa_b = item_keys[j]

                                sim = self.phonetic_similarity(ipa_a, ipa_b)

                                if sim < sim_threshold:
                                    continue

                                # Create canonical pair key (sorted)
                                if key_a > key_b:
                                    key_a, key_b = key_b, key_a
                                pair_key = f"{key_a}|{key_b}"

                                try:
                                    cursor.execute(
                                        'INSERT INTO pairs (pair_key, item_key_a, item_key_b, similarity) VALUES (?, ?, ?, ?)',
                                        (pair_key, key_a, key_b, sim)
                                    )
                                    stats['pairs_new'] += 1
                                except sqlite3.IntegrityError:
                                    stats['pairs_existing'] += 1

                # Update cursor
                search_after = hits[-1]['sort']

                # Periodic commit
                if stats['docs_scanned'] - last_commit_docs >= checkpoint_every:
                    db.commit()
                    last_commit_docs = stats['docs_scanned']

                # Progress report
                current_time = time.time()
                if current_time - last_report_time >= 5:
                    elapsed = current_time - start_time
                    rate = stats['docs_scanned'] / elapsed if elapsed > 0 else 0
                    eta = (total_docs - stats['docs_scanned']) / rate if rate > 0 else 0

                    print(f"\rDocs: {stats['docs_scanned']:,}/{total_docs:,} ({100*stats['docs_scanned']/total_docs:.1f}%) | "
                          f"Items: {stats['items_new']:,} | "
                          f"Pairs: {stats['pairs_new']:,} | "
                          f"Deduped: {stats['items_existing']+stats['pairs_existing']:,} | "
                          f"Rate: {rate:.0f}/s | "
                          f"ETA: {eta/60:.0f}m",
                          end='', flush=True)
                    last_report_time = current_time

                if max_docs and stats['docs_scanned'] >= max_docs:
                    break

            # Final commit
            db.commit()

            # Get final counts
            unique_items = cursor.execute('SELECT COUNT(*) FROM items').fetchone()[0]
            unique_pairs = cursor.execute('SELECT COUNT(*) FROM pairs').fetchone()[0]

            phase1_elapsed = time.time() - start_time
            print(f"\n\nPhase 1 complete: {phase1_elapsed/60:.1f} minutes")
            print(f"  Unique items: {unique_items:,}")
            print(f"  Unique pairs: {unique_pairs:,}")

            # =========================================================
            # PHASE 2: Write deduplicated data to HDF5
            # =========================================================
            print("\n--- PHASE 2: Writing HDF5 ---")
            phase2_start = time.time()

            with h5py.File(output_path, 'w') as f:
                str_dtype = h5py.special_dtype(vlen=str)
                grp_items = f.create_group('items')
                grp_feats = f.create_group('features')
                grp_pairs = f.create_group('pairs_with_phonetic')

                # Create datasets with exact sizes
                dsets = {
                    'toponym': grp_items.create_dataset('toponym', (unique_items,), dtype=str_dtype),
                    'romanized': grp_items.create_dataset('romanized', (unique_items,), dtype=str_dtype),
                    'lang': grp_items.create_dataset('lang', (unique_items,), dtype=str_dtype),
                    'ipa': grp_items.create_dataset('ipa', (unique_items,), dtype=str_dtype),
                    'cluster_id': grp_items.create_dataset('cluster_id', (unique_items,), dtype='i4'),
                    'has_phonetic': grp_items.create_dataset('has_phonetic', (unique_items,), dtype='bool'),
                }

                # Build item_key -> index mapping and write items
                print("  Writing items...")
                item_key_to_idx = {}

                cursor.execute('SELECT item_id, item_key, toponym, romanized, lang, ipa, features FROM items ORDER BY item_id')

                for idx, (item_id, item_key, toponym, romanized, lang, ipa, features_blob) in enumerate(cursor):
                    if idx % 100000 == 0:
                        print(f"    {idx:,}/{unique_items:,}", end='\r')

                    item_key_to_idx[item_key] = idx
                    dsets['toponym'][idx] = toponym
                    dsets['romanized'][idx] = romanized
                    dsets['lang'][idx] = lang
                    dsets['ipa'][idx] = ipa
                    dsets['cluster_id'][idx] = 0  # Not tracking clusters in deduped output
                    dsets['has_phonetic'][idx] = True

                    # Write features
                    features = orjson.loads(features_blob)
                    grp_feats.create_dataset(str(idx), data=np.array(features, dtype='f4'))

                print(f"    {unique_items:,}/{unique_items:,} items written")

                # Write pairs
                print("  Writing pairs...")

                anchor_idx = np.zeros(unique_pairs, dtype='i4')
                positive_idx = np.zeros(unique_pairs, dtype='i4')
                similarity = np.zeros(unique_pairs, dtype='f4')

                cursor.execute('SELECT item_key_a, item_key_b, similarity FROM pairs')

                for idx, (key_a, key_b, sim) in enumerate(cursor):
                    if idx % 100000 == 0:
                        print(f"    {idx:,}/{unique_pairs:,}", end='\r')

                    anchor_idx[idx] = item_key_to_idx[key_a]
                    positive_idx[idx] = item_key_to_idx[key_b]
                    similarity[idx] = sim

                grp_pairs.create_dataset('anchor_idx', data=anchor_idx)
                grp_pairs.create_dataset('positive_idx', data=positive_idx)
                grp_pairs.create_dataset('similarity', data=similarity)

                print(f"    {unique_pairs:,}/{unique_pairs:,} pairs written")

                # Write metadata
                f.attrs['total_items'] = unique_items
                f.attrs['pairs_with_phonetic'] = unique_pairs
                f.attrs['pairs_without_phonetic'] = 0
                f.attrs['similarity_threshold'] = Config.SIMILARITY_THRESHOLD
                f.attrs['namespaces'] = ','.join(namespaces) if namespaces else 'all'
                f.attrs['deduplicated'] = True

            db.close()
            phase2_elapsed = time.time() - phase2_start

        finally:
            # Clean up temp database
            if os.path.exists(db_path):
                os.unlink(db_path)
                print(f"\nCleaned up temp database")

        # Final report
        total_elapsed = time.time() - start_time
        print("\n" + "=" * 70)
        print("EXTRACTION COMPLETE")
        print("=" * 70)
        print(f"Time elapsed: {total_elapsed/60:.1f} minutes (Phase 1: {phase1_elapsed/60:.1f}m, Phase 2: {phase2_elapsed/60:.1f}m)")
        print()
        print(f"Documents scanned:        {stats['docs_scanned']:,}")
        print(f"Documents with clusters:  {stats['docs_with_clusters']:,}")
        print()
        print(f"Toponyms checked:         {stats['toponyms_checked']:,}")
        print(f"  Supported language:     {stats['toponyms_supported_lang']:,} ({100*stats['toponyms_supported_lang']/max(1,stats['toponyms_checked']):.1f}%)")
        print(f"  With valid phonetics:   {stats['toponyms_with_phonetics']:,} ({100*stats['toponyms_with_phonetics']/max(1,stats['toponyms_supported_lang']):.1f}%)")
        print()
        print(f"Items (unique):           {unique_items:,} (deduped {stats['items_existing']:,})")
        print(f"Pairs (unique):           {unique_pairs:,} (deduped {stats['pairs_existing']:,})")
        print()
        print(f"Output: {output_path}")


# =============================================================================
# DEDUPLICATION (LEGACY - now integrated into extract_optimized)
# =============================================================================

def deduplicate_hdf5(input_path: str, output_path: str):
    """
    Post-process HDF5 file to remove duplicate pairs using SQLite.

    DEPRECATED: This is now handled inline by extract_optimized().
    Kept for backward compatibility with existing two-step workflows.
    """
    print("=" * 70)
    print("DEDUPLICATING HDF5 FILE")
    print("=" * 70)
    print("NOTE: This step is now integrated into extract_optimized().")
    print("      Consider using single-step extraction instead.")
    print()

    db = sqlite3.connect(':memory:')

    db.execute('''CREATE TABLE pairs (
        pair_key TEXT PRIMARY KEY,
        anchor_idx INTEGER,
        positive_idx INTEGER,
        similarity REAL
    )''')

    stats = {
        'original': 0,
        'unique': 0,
    }

    with h5py.File(input_path, 'r') as f_in:
        items = f_in['items']

        def make_pair_key(anc_idx, pos_idx):
            top_a = items['toponym'][anc_idx]
            lang_a = items['lang'][anc_idx]
            top_b = items['toponym'][pos_idx]
            lang_b = items['lang'][pos_idx]

            if isinstance(top_a, bytes):
                top_a = top_a.decode('utf-8')
            if isinstance(lang_a, bytes):
                lang_a = lang_a.decode('utf-8')
            if isinstance(top_b, bytes):
                top_b = top_b.decode('utf-8')
            if isinstance(lang_b, bytes):
                lang_b = lang_b.decode('utf-8')

            top_a = top_a.lower().strip()
            lang_a = lang_a.lower().strip()
            top_b = top_b.lower().strip()
            lang_b = lang_b.lower().strip()

            if (top_a, lang_a) > (top_b, lang_b):
                top_a, lang_a, top_b, lang_b = top_b, lang_b, top_a, lang_a

            return f"{top_a}@{lang_a}|{top_b}@{lang_b}"

        # Load pairs
        print("Loading pairs...")
        pairs = f_in['pairs_with_phonetic']
        stats['original'] = pairs['anchor_idx'].shape[0]

        cursor = db.cursor()
        batch = []
        batch_size = 10000

        for i in range(stats['original']):
            if i % 100000 == 0:
                print(f"  {i:,}/{stats['original']:,}", end='\r')

            anc_idx = int(pairs['anchor_idx'][i])
            pos_idx = int(pairs['positive_idx'][i])
            sim = float(pairs['similarity'][i])

            pair_key = make_pair_key(anc_idx, pos_idx)
            batch.append((pair_key, anc_idx, pos_idx, sim))

            if len(batch) >= batch_size:
                cursor.executemany(
                    'INSERT OR IGNORE INTO pairs VALUES (?, ?, ?, ?)',
                    batch
                )
                batch = []

        if batch:
            cursor.executemany(
                'INSERT OR IGNORE INTO pairs VALUES (?, ?, ?, ?)',
                batch
            )

        db.commit()
        stats['unique'] = cursor.execute('SELECT COUNT(*) FROM pairs').fetchone()[0]

        # Write deduplicated output
        print(f"\n\nWriting deduplicated file...")

        with h5py.File(output_path, 'w') as f_out:
            f_in.copy('items', f_out)
            f_in.copy('features', f_out)

            cursor.execute('SELECT anchor_idx, positive_idx, similarity FROM pairs ORDER BY anchor_idx')
            pair_data = cursor.fetchall()

            grp_p = f_out.create_group('pairs_with_phonetic')
            grp_p.create_dataset('anchor_idx', data=[p[0] for p in pair_data], dtype='i4')
            grp_p.create_dataset('positive_idx', data=[p[1] for p in pair_data], dtype='i4')
            grp_p.create_dataset('similarity', data=[p[2] for p in pair_data], dtype='f4')

            # Copy metadata
            f_out.attrs['total_items'] = f_in.attrs['total_items']
            f_out.attrs['pairs_with_phonetic'] = stats['unique']
            f_out.attrs['pairs_without_phonetic'] = 0
            f_out.attrs['similarity_threshold'] = f_in.attrs.get('similarity_threshold', 0.5)
            f_out.attrs['namespaces'] = f_in.attrs.get('namespaces', 'unknown')
            f_out.attrs['deduplicated'] = True

    db.close()

    duplicates = stats['original'] - stats['unique']
    print("\nDeduplication complete:")
    print(f"  Original pairs:  {stats['original']:,}")
    print(f"  Unique pairs:    {stats['unique']:,}")
    print(f"  Duplicates:      {duplicates:,} ({100*duplicates/max(1,stats['original']):.1f}%)")