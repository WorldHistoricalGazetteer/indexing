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
"""

import sqlite3
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set

import h5py
import numpy as np
import orjson

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


class ToponymEnricher:
    """
    Scans the 'toponyms' index and computes phonetic features for supported languages.
    Updates the documents in-place so they can be retrieved quickly later.
    """

    def __init__(self, es_host='localhost:9200', index='toponyms'):
        self.es = Elasticsearch([normalize_es_host(es_host)], request_timeout=60)
        self.index = index
        self.ft = FeatureTable()
        self._epi_cache = {}

        print("Loading Epitran models...")
        for lang in Config.EPITRAN_LANGS:
            self._get_epitran(lang)

    def _get_epitran(self, lang_code):
        if lang_code not in self._epi_cache:
            epitran_code = Config.EPITRAN_LANGS.get(lang_code)
            try:
                self._epi_cache[lang_code] = epitran.Epitran(epitran_code) if epitran_code else None
            except:
                self._epi_cache[lang_code] = None
        return self._epi_cache[lang_code]

    def _compute_phonetics(self, name, lang):
        epi = self._get_epitran(lang)
        if not epi:
            return None

        try:
            ipa = epi.transliterate(name)
            if not ipa or not ipa.strip():
                return None

            features = self.ft.word_to_vector_list(ipa, numeric=True)
            if not features or len(features) == 0:
                return None

            if not all(isinstance(f, (list, tuple)) and len(f) == 24 for f in features):
                return None

            return {'ipa': ipa, 'features': features}
        except Exception:
            return None

    def run(self):
        print(f"Adding phonetic fields to index '{self.index}' mapping...")

        try:
            self.es.indices.put_mapping(index=self.index, body={
                "properties": {
                    "ipa_cached": {"type": "keyword", "index": False, "doc_values": False},
                    "features_cached_json": {"type": "keyword", "index": False, "doc_values": False}
                }
            })
        except Exception as e:
            print(f"Mapping update warning (might already exist): {e}")

        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"lang": list(Config.EPITRAN_LANGS.keys())}},
                        {"bool": {"must_not": {"exists": {"field": "ipa_cached"}}}}
                    ]
                }
            }
        }

        print("Scanning and enriching toponyms...")
        updates = []
        count = 0

        try:
            scanner = helpers.scan(
                self.es,
                query=query,
                index=self.index,
                _source=["name", "lang"],
                size=1000,
                scroll='2h'
            )

            for hit in scanner:
                doc_id = hit['_id']
                name = hit['_source'].get('name')
                lang = hit['_source'].get('lang')

                if not name or not lang:
                    continue

                result = self._compute_phonetics(name, lang)

                if result:
                    updates.append({
                        "_op_type": "update",
                        "_index": self.index,
                        "_id": doc_id,
                        "doc": {
                            "ipa_cached": result['ipa'],
                            "features_cached_json": orjson.dumps(
                                result['features'],
                                option=orjson.OPT_SERIALIZE_NUMPY
                            ).decode('utf-8')
                        }
                    })

                if len(updates) >= 500:
                    helpers.bulk(self.es, updates, request_timeout=60)
                    count += len(updates)
                    print(f"  Enriched {count} documents...", end='\r', flush=True)
                    updates = []

            if updates:
                helpers.bulk(self.es, updates)
                count += len(updates)

            print(f"\nDone! Enriched {count} documents.")

        except KeyboardInterrupt:
            print("\n\n[Warning] Process interrupted by user.")
        except Exception as e:
            print(f"\n\n[Error] Process crashed: {e}")

    def cleanup_invalid_phonetics(self):
        """
        Remove ipa_cached and features_cached_json from toponyms where
        the cached features are empty or invalid.

        This fixes earlier bugs where empty embeddings were stored.
        """
        print("Scanning for invalid cached phonetics...")

        # Find documents with ipa_cached but potentially invalid features
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

            # Check for empty IPA
            if not ipa or not ipa.strip():
                is_invalid = True

            # Check for empty or invalid features
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
        Optimized extraction with namespace filtering and pre-filtering.

        Args:
            output_path: HDF5 output file path
            namespaces: List of namespace prefixes to include (e.g., ['gn', 'wd'])
                       None means all namespaces
            max_docs: Maximum documents to process (for testing)
            checkpoint_every: Flush to disk every N documents
        """
        print("=" * 70)
        print("OPTIMIZED TRAINING DATA EXTRACTION")
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
            'items_written': 0,
            'pairs_written': 0,
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
        print()

        with h5py.File(output_path, 'w') as f:
            str_dtype = h5py.special_dtype(vlen=str)
            grp_items = f.create_group('items')
            grp_feats = f.create_group('features')
            grp_pairs = f.create_group('pairs_with_phonetic')

            # Pre-allocate datasets
            chunk_size = 100000

            dsets = {
                'toponym': grp_items.create_dataset('toponym', (chunk_size,), maxshape=(None,), dtype=str_dtype, chunks=True),
                'romanized': grp_items.create_dataset('romanized', (chunk_size,), maxshape=(None,), dtype=str_dtype, chunks=True),
                'lang': grp_items.create_dataset('lang', (chunk_size,), maxshape=(None,), dtype=str_dtype, chunks=True),
                'ipa': grp_items.create_dataset('ipa', (chunk_size,), maxshape=(None,), dtype=str_dtype, chunks=True),
                'cluster_id': grp_items.create_dataset('cluster_id', (chunk_size,), maxshape=(None,), dtype='i4', chunks=True),
                'has_phonetic': grp_items.create_dataset('has_phonetic', (chunk_size,), maxshape=(None,), dtype='bool', chunks=True),
                'p_anc': grp_pairs.create_dataset('anchor_idx', (chunk_size,), maxshape=(None,), dtype='i4', chunks=True),
                'p_pos': grp_pairs.create_dataset('positive_idx', (chunk_size,), maxshape=(None,), dtype='i4', chunks=True),
                'p_sim': grp_pairs.create_dataset('similarity', (chunk_size,), maxshape=(None,), dtype='f4', chunks=True),
            }

            counters = {'items': 0, 'pairs': 0}
            caps = {'items': chunk_size, 'pairs': chunk_size}

            def ensure_capacity(counter_name: str, dset_keys: List[str]):
                """Expand datasets if needed."""
                if counters[counter_name] >= caps[counter_name]:
                    new_cap = caps[counter_name] + chunk_size
                    for key in dset_keys:
                        dsets[key].resize((new_cap,))
                    caps[counter_name] = new_cap

            # Process places in batches
            search_after = None
            batch_size = 500

            start_time = time.time()
            last_report_time = start_time

            while True:
                # Build paginated query
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

                # Collect all candidate toponym_ids from this batch for batch MGET
                batch_candidates = []  # List of (hit_idx, tid, name, lang)

                for hit_idx, hit in enumerate(hits):
                    if max_docs and stats['docs_scanned'] >= max_docs:
                        break

                    stats['docs_scanned'] += 1
                    raw_toponyms = hit['_source'].get('toponyms', [])

                    if not raw_toponyms:
                        continue

                    # STAGE 1: Parse and filter to supported languages (fast, O(n))
                    seen_in_cluster = set()

                    for t in raw_toponyms:
                        if not isinstance(t, dict):
                            continue

                        tid = t.get('toponym_id', '')
                        if not tid or '@' not in tid:
                            continue

                        # Parse toponym_id
                        at_pos = tid.rfind('@')
                        if at_pos <= 0:
                            continue

                        name = tid[:at_pos].strip()
                        lang = tid[at_pos + 1:].strip()

                        if not name or not lang:
                            continue

                        stats['toponyms_checked'] += 1

                        # Filter: supported language only (fast set lookup)
                        if lang not in self.SUPPORTED_LANGS:
                            continue

                        stats['toponyms_supported_lang'] += 1

                        # Dedupe within cluster
                        key = (name.lower(), lang.lower())
                        if key in seen_in_cluster:
                            continue
                        seen_in_cluster.add(key)

                        batch_candidates.append((hit_idx, tid, name, lang))

                # STAGE 2: Batch fetch phonetics for all candidates
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

                    # STAGE 3: Process each place with valid phonetic toponyms
                    for hit_idx, phonetic_toponyms in hit_toponyms.items():
                        # Need at least 2 to form pairs
                        if len(phonetic_toponyms) < 2:
                            continue

                        stats['docs_with_clusters'] += 1
                        cluster_id = stats['docs_with_clusters']

                        # Write items and collect indices for pair generation
                        item_indices = []

                        for topo in phonetic_toponyms:
                            ensure_capacity('items', ['toponym', 'romanized', 'lang', 'ipa', 'cluster_id', 'has_phonetic'])

                            idx = counters['items']
                            dsets['toponym'][idx] = topo['name']
                            dsets['romanized'][idx] = anyascii(topo['name']).lower()
                            dsets['lang'][idx] = topo['lang']
                            dsets['ipa'][idx] = topo['ipa']
                            dsets['cluster_id'][idx] = cluster_id
                            dsets['has_phonetic'][idx] = True

                            # Store features
                            grp_feats.create_dataset(str(idx), data=np.array(topo['features'], dtype='f4'))

                            item_indices.append((idx, topo['ipa']))
                            counters['items'] += 1
                            stats['items_written'] += 1

                        # STAGE 4: Generate pairs (optimized loop)
                        n_items = len(item_indices)
                        sim_threshold = Config.SIMILARITY_THRESHOLD

                        for i in range(n_items - 1):
                            idx_a, ipa_a = item_indices[i]

                            for j in range(i + 1, n_items):
                                idx_b, ipa_b = item_indices[j]

                                sim = self.phonetic_similarity(ipa_a, ipa_b)

                                if sim < sim_threshold:
                                    continue

                                ensure_capacity('pairs', ['p_anc', 'p_pos', 'p_sim'])

                                pidx = counters['pairs']
                                dsets['p_anc'][pidx] = idx_a
                                dsets['p_pos'][pidx] = idx_b
                                dsets['p_sim'][pidx] = sim
                                counters['pairs'] += 1
                                stats['pairs_written'] += 1

                # Update cursor
                search_after = hits[-1]['sort']

                # Progress report every 5 seconds
                current_time = time.time()
                if current_time - last_report_time >= 5:
                    elapsed = current_time - start_time
                    rate = stats['docs_scanned'] / elapsed if elapsed > 0 else 0
                    eta = (total_docs - stats['docs_scanned']) / rate if rate > 0 else 0

                    print(f"\rDocs: {stats['docs_scanned']:,}/{total_docs:,} ({100*stats['docs_scanned']/total_docs:.1f}%) | "
                          f"Clusters: {stats['docs_with_clusters']:,} | "
                          f"Items: {stats['items_written']:,} | "
                          f"Pairs: {stats['pairs_written']:,} | "
                          f"Rate: {rate:.0f}/s | "
                          f"ETA: {eta/60:.0f}m",
                          end='', flush=True)
                    last_report_time = current_time

                # Checkpoint
                if stats['docs_scanned'] % checkpoint_every == 0:
                    f.flush()

                if max_docs and stats['docs_scanned'] >= max_docs:
                    break

            # Trim datasets to actual size
            print("\n\nTrimming datasets...")
            for key in ['toponym', 'romanized', 'lang', 'ipa', 'cluster_id', 'has_phonetic']:
                dsets[key].resize((counters['items'],))

            for key in ['p_anc', 'p_pos', 'p_sim']:
                dsets[key].resize((counters['pairs'],))

            # Write metadata
            f.attrs['total_items'] = counters['items']
            f.attrs['pairs_with_phonetic'] = counters['pairs']
            f.attrs['pairs_without_phonetic'] = 0
            f.attrs['similarity_threshold'] = Config.SIMILARITY_THRESHOLD
            f.attrs['namespaces'] = ','.join(namespaces) if namespaces else 'all'
            f.attrs['deduplicated'] = False

        # Final report
        elapsed = time.time() - start_time
        print("\n" + "=" * 70)
        print("EXTRACTION COMPLETE")
        print("=" * 70)
        print(f"Time elapsed: {elapsed/60:.1f} minutes")
        print()
        print(f"Documents scanned:        {stats['docs_scanned']:,}")
        print(f"Documents with clusters:  {stats['docs_with_clusters']:,}")
        print()
        print(f"Toponyms checked:         {stats['toponyms_checked']:,}")
        print(f"  Supported language:     {stats['toponyms_supported_lang']:,} ({100*stats['toponyms_supported_lang']/max(1,stats['toponyms_checked']):.1f}%)")
        print(f"  With valid phonetics:   {stats['toponyms_with_phonetics']:,} ({100*stats['toponyms_with_phonetics']/max(1,stats['toponyms_supported_lang']):.1f}%)")
        print()
        print(f"Items written:            {stats['items_written']:,}")
        print(f"Pairs written:            {stats['pairs_written']:,}")
        print()
        print(f"Output: {output_path}")


def deduplicate_hdf5(input_path: str, output_path: str):
    """Post-process HDF5 file to remove duplicate pairs using SQLite."""
    print("=" * 70)
    print("DEDUPLICATING HDF5 FILE")
    print("=" * 70)

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