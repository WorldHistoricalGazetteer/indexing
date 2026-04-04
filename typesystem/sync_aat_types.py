# types/sync_aat_types.py

"""
Synchronise the ES `types` index with the Getty AAT explicit N-Triples dump.

Standalone version — no Django dependencies. Ported from the legacy
placetypes/management/commands/sync_aat_types.py.

Top-down approach: walks from broad entry points, excludes non-place
subtrees, and assigns multi-valued fclass from a configurable map.

Usage:
    python -m typesystem.sync_aat_types                     # download if new, parse, index to ES
    python -m typesystem.sync_aat_types --force              # re-download even if unchanged
    python -m typesystem.sync_aat_types --local /path/to/nt  # use local .nt files
    python -m typesystem.sync_aat_types --dry-run            # report counts without indexing
    python -m typesystem.sync_aat_types --api                # crawl via JSON API (slow fallback)
    python -m typesystem.sync_aat_types --es-host URL        # specify ES host (default: localhost:9200)
"""

import json
import logging
import re
import time
import zipfile
from collections import defaultdict, Counter
from datetime import datetime
from pathlib import Path

import requests

from typesystem.aat_config import (
    AAT_CACHE_DIR,
    AAT_DUMP_META_FILE,
    AAT_ENTRY_POINTS,
    AAT_EXCLUDED_SUBTREES,
    AAT_EXPLICIT_DUMP_URL,
    AAT_FCLASS_MAP,
    AAT_JSON_API,
    AAT_NT_HIERARCHICAL_RELS,
    AAT_NT_SCOPE_NOTES,
    AAT_NT_TERMS,
    AAT_SCOPE_NOTE_URI_PREFIX,
    AAT_TERM_URI_PREFIX,
    AAT_URI_PREFIX,
    GVP_BROADER_GENERIC,
    GVP_BROADER_PREFERRED,
    RDF_VALUE,
    SKOS_SCOPE_NOTE,
    SKOSXL_LITERAL_FORM,
    SKOSXL_PREF_LABEL,
)

logger = logging.getLogger(__name__)

# Regex for parsing N-Triples lines.
_NT_LINE_RE = re.compile(
    r'^<([^>]+)>\s+<([^>]+)>\s+'
    r'(?:<([^>]+)>'
    r'|"((?:[^"\\]|\\.)*)"'
    r'(?:@(\w[\w-]*))?'
    r'(?:\^\^<[^>]+>)?'
    r')\s*\.\s*$'
)

# Regex for \uXXXX and \UXXXXXXXX escapes in N-Triples string literals.
_NT_UNICODE_RE = re.compile(r'\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})')

ES_TYPES_INDEX = "types"


def _parse_nt_line(line):
    """Parse a single N-Triples line. Returns (subj, pred, obj_uri, literal, lang) or None."""
    m = _NT_LINE_RE.match(line)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)


def _decode_nt_unicode(s):
    """Decode N-Triples \\uXXXX / \\UXXXXXXXX escapes to proper Unicode."""
    if s is None:
        return s
    if '\\u' not in s and '\\U' not in s:
        return s
    return _NT_UNICODE_RE.sub(
        lambda m: chr(int(m.group(1) or m.group(2), 16)), s
    )


def _aat_id_from_uri(uri):
    """Extract integer AAT id from a URI."""
    if uri and uri.startswith(AAT_URI_PREFIX):
        tail = uri[len(AAT_URI_PREFIX):]
        if tail.isdigit():
            return int(tail)
    return None


# ============================================================================
# Download & extract
# ============================================================================

def _meta_path():
    return AAT_CACHE_DIR / AAT_DUMP_META_FILE


def _read_meta():
    mp = _meta_path()
    if mp.exists():
        with open(mp) as f:
            return json.load(f)
    return {}


def _write_meta(meta):
    with open(_meta_path(), 'w') as f:
        json.dump(meta, f, indent=2)


def download_if_needed(force=False):
    """Download AAT explicit dump if needed. Returns path to .nt directory."""
    AAT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    meta = _read_meta()
    headers = {}
    if not force:
        if meta.get('etag'):
            headers['If-None-Match'] = meta['etag']
        if meta.get('last_modified'):
            headers['If-Modified-Since'] = meta['last_modified']

    print(f"Checking {AAT_EXPLICIT_DUMP_URL} ...")
    try:
        resp = requests.head(AAT_EXPLICIT_DUMP_URL, headers=headers,
                             timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        raise RuntimeError(f"HEAD request failed: {e}")

    needed = [AAT_NT_HIERARCHICAL_RELS, AAT_NT_TERMS, AAT_NT_SCOPE_NOTES]

    if resp.status_code == 304 and not force:
        if all((AAT_CACHE_DIR / n).exists() for n in needed):
            print("AAT dump unchanged (using cached files).")
            return AAT_CACHE_DIR

    if resp.status_code not in (200, 302, 304):
        # Check if we have cached files to fall back on
        if all((AAT_CACHE_DIR / n).exists() for n in needed):
            print(f"HTTP {resp.status_code} but cached files exist — using those.")
            return AAT_CACHE_DIR
        raise RuntimeError(f"Unexpected status {resp.status_code}")

    print("Downloading AAT explicit dump (this may take a few minutes) ...")
    resp = requests.get(AAT_EXPLICIT_DUMP_URL, stream=True, timeout=600)
    resp.raise_for_status()

    zip_path = AAT_CACHE_DIR / "explicit.zip"
    total = int(resp.headers.get('content-length', 0))
    downloaded = 0
    with open(zip_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded * 100 // total
                print(f"\r  {pct}% ({downloaded // (1024*1024)} MB)", end='', flush=True)
    print()

    new_meta = {
        'etag': resp.headers.get('ETag', ''),
        'last_modified': resp.headers.get('Last-Modified', ''),
        'downloaded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    _write_meta(new_meta)

    print("Extracting ...")
    needed_set = set(needed)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for name in zf.namelist():
            if name in needed_set:
                print(f"  -> {name}")
                with zf.open(name) as src, \
                        open(AAT_CACHE_DIR / name, 'wb') as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)

    for name in needed:
        if not (AAT_CACHE_DIR / name).exists():
            raise RuntimeError(f"Expected file {name} not found in explicit.zip")

    print(f"Extracted to {AAT_CACHE_DIR}")
    return AAT_CACHE_DIR


# ============================================================================
# Parse: Hierarchy edges
# ============================================================================

def parse_hierarchy(nt_path):
    """
    Returns:
        preferred_parent: dict child -> canonical parent
        children:         dict parent -> set of children
        parents:          dict child -> set of ALL parents (for fclass)
    """
    preferred_parent = {}
    children = defaultdict(set)
    parents = defaultdict(set)
    line_count = 0

    with open(nt_path, 'r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line_count += 1
            if line_count % 100_000 == 0:
                print(f"\r  ... {line_count:,} lines", end='', flush=True)

            parsed = _parse_nt_line(line)
            if parsed is None:
                continue

            subj_uri, pred_uri, obj_uri, _literal, _lang = parsed

            if pred_uri not in (GVP_BROADER_PREFERRED, GVP_BROADER_GENERIC):
                continue
            if not obj_uri:
                continue

            child_id = _aat_id_from_uri(subj_uri)
            parent_id = _aat_id_from_uri(obj_uri)
            if child_id is None or parent_id is None:
                continue

            children[parent_id].add(child_id)
            parents[child_id].add(parent_id)

            if pred_uri == GVP_BROADER_PREFERRED:
                preferred_parent[child_id] = parent_id

    print(f"\r  {line_count:,} lines in {AAT_NT_HIERARCHICAL_RELS}")
    return preferred_parent, children, parents


# ============================================================================
# Parse: Labels (SKOS-XL two-hop) — multilingual
# ============================================================================

def parse_labels(nt_path):
    """
    Parse preferred labels from the AAT terms N-Triples file.

    Returns:
        en_labels:   dict  aat_id → English preferred label string
        all_labels:  dict  aat_id → {lang: label_string, ...}
    """
    # Step 1 hop: concept → prefLabel → term URI (per language)
    # Key: aat_id → {lang: term_uri}  (prefer plain 'xx' over 'xx-us' etc.)
    concept_terms = defaultdict(dict)  # aat_id → {lang_code: (term_uri, is_plain)}
    # Step 2 hop: term URI → literal form
    term_uri_to_literal = {}  # term_uri → (literal, lang)
    line_count = 0

    with open(nt_path, 'r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line_count += 1
            if line_count % 1_000_000 == 0:
                print(f"\r  ... {line_count:,} lines", end='', flush=True)

            parsed = _parse_nt_line(line)
            if parsed is None:
                continue

            subj_uri, pred_uri, obj_uri, literal, lang = parsed

            if pred_uri == SKOSXL_PREF_LABEL and obj_uri:
                if not obj_uri.startswith(AAT_TERM_URI_PREFIX):
                    continue
                # Extract language from term URI: .../term/NNNNN-lang
                suffix = obj_uri[len(AAT_TERM_URI_PREFIX):]
                dash_idx = suffix.find('-')
                if dash_idx < 0:
                    continue
                uri_lang = suffix[dash_idx + 1:]
                if not uri_lang:
                    continue
                # Normalise: "en" is plain, "en-us" is regional
                base_lang = uri_lang.split('-')[0]
                is_plain = (uri_lang == base_lang)
                concept_id = _aat_id_from_uri(subj_uri)
                if concept_id is None:
                    continue
                existing = concept_terms[concept_id].get(base_lang)
                if existing is None or (is_plain and not existing[1]):
                    concept_terms[concept_id][base_lang] = (obj_uri, is_plain)

            elif pred_uri == SKOSXL_LITERAL_FORM and literal:
                if subj_uri.startswith(AAT_TERM_URI_PREFIX):
                    if lang:
                        term_uri_to_literal[subj_uri] = (_decode_nt_unicode(literal), lang)

    print(f"\r  {line_count:,} lines in {AAT_NT_TERMS}")

    # Resolve: join concept→term_uri→literal for all languages
    en_labels = {}
    all_labels = defaultdict(dict)  # aat_id → {lang: text}

    for concept_id, lang_terms in concept_terms.items():
        for base_lang, (term_uri, _) in lang_terms.items():
            entry = term_uri_to_literal.get(term_uri)
            if entry:
                text, _ = entry
                all_labels[concept_id][base_lang] = text
                if base_lang == 'en':
                    en_labels[concept_id] = text

    lang_count = sum(len(v) for v in all_labels.values())
    en_count = len(en_labels)
    print(f"  -> {en_count:,} English labels, {lang_count:,} total across all languages")

    return en_labels, dict(all_labels)


# ============================================================================
# Parse: Scope notes (two-hop) — multilingual
# ============================================================================

def parse_notes(nt_path):
    """
    Parse scope notes from the AAT scope notes N-Triples file.

    Returns:
        en_notes:   dict  aat_id → English scope note string
        all_notes:  dict  aat_id → {lang: note_string, ...}
    """
    concept_to_note_uris = defaultdict(set)
    note_uri_to_text = {}  # note_uri → (text, lang)
    line_count = 0

    with open(nt_path, 'r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line_count += 1
            if line_count % 500_000 == 0:
                print(f"\r  ... {line_count:,} lines", end='', flush=True)

            parsed = _parse_nt_line(line)
            if parsed is None:
                continue

            subj_uri, pred_uri, obj_uri, literal, lang = parsed

            if pred_uri == SKOS_SCOPE_NOTE and obj_uri:
                concept_id = _aat_id_from_uri(subj_uri)
                if concept_id is not None:
                    concept_to_note_uris[concept_id].add(obj_uri)

            elif pred_uri == RDF_VALUE and literal:
                if subj_uri.startswith(AAT_SCOPE_NOTE_URI_PREFIX):
                    if lang:
                        note_uri_to_text[subj_uri] = (
                            _decode_nt_unicode(literal), lang.split('-')[0]
                        )

    print(f"\r  {line_count:,} lines in {AAT_NT_SCOPE_NOTES}")

    en_notes = {}
    all_notes = defaultdict(dict)  # aat_id → {lang: note_text}

    for concept_id, note_uris in concept_to_note_uris.items():
        for uri in sorted(note_uris):
            entry = note_uri_to_text.get(uri)
            if entry:
                text, lang_code = entry
                truncated = text[:3000]
                if lang_code not in all_notes[concept_id]:
                    all_notes[concept_id][lang_code] = truncated
                if lang_code == 'en' and concept_id not in en_notes:
                    en_notes[concept_id] = truncated

    lang_count = sum(len(v) for v in all_notes.values())
    en_count = len(en_notes)
    print(f"  -> {en_count:,} English notes, {lang_count:,} total across all languages")

    return en_notes, dict(all_notes)


# ============================================================================
# Compute fclasses for a concept via ancestor walk
# ============================================================================

def compute_fclasses(aat_id, parents_map, _cache=None):
    if _cache is None:
        _cache = {}
    if aat_id in _cache:
        return _cache[aat_id]

    fcs = set()

    if aat_id in AAT_FCLASS_MAP:
        fcs.add(AAT_FCLASS_MAP[aat_id])

    visited = {aat_id}
    stack = list(parents_map.get(aat_id, []))
    while stack:
        ancestor = stack.pop()
        if ancestor in visited:
            continue
        visited.add(ancestor)
        if ancestor in AAT_FCLASS_MAP:
            fcs.add(AAT_FCLASS_MAP[ancestor])
        for grandparent in parents_map.get(ancestor, []):
            if grandparent not in visited:
                stack.append(grandparent)

    result = sorted(fcs) if fcs else []
    _cache[aat_id] = result
    return result


# ============================================================================
# Walk hierarchy from entry points
# ============================================================================

def walk_hierarchy(preferred_parent, children, parents_map, labels, notes,
                   all_labels=None, all_notes=None):
    """BFS from each entry point downward."""
    result = []
    visited = set()
    fclass_cache = {}
    if all_labels is None:
        all_labels = {}
    if all_notes is None:
        all_notes = {}

    queue = []
    for ep in AAT_ENTRY_POINTS:
        queue.append((ep, None, str(ep), 0))

    excluded_count = 0

    while queue:
        aat_id, walk_parent_id, path, depth = queue.pop(0)
        if aat_id in visited:
            continue

        if aat_id in AAT_EXCLUDED_SUBTREES:
            excluded_count += 1
            visited.add(aat_id)
            continue

        visited.add(aat_id)

        canonical_parent = preferred_parent.get(aat_id, walk_parent_id)
        term = labels.get(aat_id, f"aat:{aat_id}")
        note = notes.get(aat_id, '')
        fclasses = compute_fclasses(aat_id, parents_map, fclass_cache)

        # Compute ancestors list from path
        ancestors = [int(x) for x in path.split('.')]

        entry = {
            'aat_id': aat_id,
            'parent_id': canonical_parent,
            'term': term[:100],
            'term_full': term[:100],
            'note': note[:3000],
            'fclasses': fclasses,
            'path': path,
            'ancestors': ancestors,
            'depth': depth,
            'is_place_type': True,
        }

        # Multilingual labels: {lang: label_string}
        ml_labels = all_labels.get(aat_id)
        if ml_labels:
            entry['labels'] = ml_labels

        # Multilingual notes: {lang: note_string}
        ml_notes = all_notes.get(aat_id)
        if ml_notes:
            entry['notes'] = ml_notes

        result.append(entry)

        for child_id in sorted(children.get(aat_id, [])):
            if child_id not in visited:
                child_path = f"{path}.{child_id}"
                queue.append((child_id, aat_id, child_path, depth + 1))

    if excluded_count:
        print(f"  (skipped {excluded_count} excluded subtree root(s))")

    return result


# ============================================================================
# API crawl (fallback)
# ============================================================================

_API_TIMEOUT = 30
_API_RETRY_WAIT = 2
_API_MAX_RETRIES = 3


def _fetch_concept_json(aat_id, session):
    url = AAT_JSON_API.format(aat_id=aat_id)
    for attempt in range(1, _API_MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=_API_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503):
                time.sleep(_API_RETRY_WAIT * attempt)
                continue
            return None
        except requests.RequestException:
            time.sleep(_API_RETRY_WAIT * attempt)
    return None


def _extract_label_and_note(data):
    """Extract English label and note, plus multilingual dicts."""
    label = data.get('_label', '')
    labels_ml = {}  # lang → label
    note = ''
    notes_ml = {}  # lang → note

    # Labels from identified_by
    for ident in data.get('identified_by', []):
        classes = [c.get('_label', '') for c in ident.get('classified_as', [])]
        if 'preferred term' not in classes:
            continue
        lang_list = ident.get('language', [])
        content = ident.get('content', '')
        if not content:
            continue
        for lang_obj in lang_list:
            lang_code = lang_obj.get('_label', '')
            if lang_code:
                labels_ml[lang_code] = content
                if lang_code == 'en':
                    label = content

    # Notes from subject_of
    for subj in data.get('subject_of', []):
        content = subj.get('content', '')
        if not content:
            continue
        lang_list = subj.get('language', [])
        for lang_obj in lang_list:
            lang_code = lang_obj.get('_label', '')
            if lang_code:
                notes_ml[lang_code] = content[:3000]
                if lang_code == 'en':
                    note = content[:3000]

    return label, note, labels_ml, notes_ml


def crawl_api():
    """BFS crawl via JSON API. Returns list of dicts."""
    print("Crawling AAT hierarchy via JSON API ...")
    session = requests.Session()
    session.headers.update({'Accept': 'application/json'})
    result = []
    visited = set()
    fetched = 0
    queue = []
    for ep in AAT_ENTRY_POINTS:
        queue.append((ep, None, str(ep), 0))
    while queue:
        aat_id, parent_id, path, depth = queue.pop(0)
        if aat_id in visited or aat_id in AAT_EXCLUDED_SUBTREES:
            continue
        visited.add(aat_id)
        data = _fetch_concept_json(aat_id, session)
        fetched += 1
        if fetched % 50 == 0:
            print(f"  ... fetched {fetched} concepts, {len(queue)} queued")
        if data is None:
            continue
        label, note, labels_ml, notes_ml = _extract_label_and_note(data)
        if not label:
            label = f"aat:{aat_id}"

        ancestors = [int(x) for x in path.split('.')]
        fclasses = sorted(set(
            fc for anc in ancestors
            if (fc := AAT_FCLASS_MAP.get(anc))
        )) or (list(AAT_FCLASS_MAP.get(aat_id, '')) or [])

        entry = {
            'aat_id': aat_id,
            'parent_id': parent_id,
            'term': label[:100],
            'term_full': label[:100],
            'note': note,
            'fclasses': fclasses,
            'path': path,
            'ancestors': ancestors,
            'depth': depth,
            'is_place_type': True,
        }
        if labels_ml:
            entry['labels'] = labels_ml
        if notes_ml:
            entry['notes'] = notes_ml

        result.append(entry)
        for child in sorted(data.get('narrower', []),
                            key=lambda c: c.get('id', '')):
            child_id = _aat_id_from_uri(child.get('id', ''))
            if child_id is not None and child_id not in visited:
                queue.append((child_id, aat_id, f"{path}.{child_id}",
                              depth + 1))
    print(f"  ... {fetched} API requests, {len(result)} concepts collected")
    return result


# ============================================================================
# Backfill missing labels via Getty API
# ============================================================================

def backfill_labels_from_api(gaps):
    session = requests.Session()
    session.headers.update({'Accept': 'application/json'})
    filled = 0
    failed = 0

    for i, pt in enumerate(gaps, 1):
        aat_id = pt['aat_id']
        data = _fetch_concept_json(aat_id, session)
        if data is None:
            failed += 1
            continue
        label, note, labels_ml, notes_ml = _extract_label_and_note(data)
        if label:
            pt['term'] = label[:100]
            pt['term_full'] = label[:100]
            filled += 1
        else:
            failed += 1
        if note and not pt.get('note'):
            pt['note'] = note[:3000]
        # Merge multilingual data from API into existing
        if labels_ml:
            existing_labels = pt.get('labels', {})
            existing_labels.update(labels_ml)
            pt['labels'] = existing_labels
        if notes_ml:
            existing_notes = pt.get('notes', {})
            existing_notes.update(notes_ml)
            pt['notes'] = existing_notes
        if i % 25 == 0:
            print(f"    ... {i}/{len(gaps)}: {filled} filled, {failed} missing")

    print(f"  -> backfilled {filled} label(s); {failed} unresolved")


# ============================================================================
# Index to Elasticsearch
# ============================================================================

def index_to_es(place_types, es_host, index_name=ES_TYPES_INDEX):
    """Bulk index place types to Elasticsearch."""
    from elasticsearch import Elasticsearch, helpers

    es = Elasticsearch(es_host, request_timeout=120)

    # Load schema
    schema_path = Path(__file__).parent.parent / "schemas" / "types.json"
    with open(schema_path) as f:
        schema = json.load(f)

    # Create timestamped index with alias swap
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    new_index = f"{index_name}_{timestamp}"

    print(f"Creating index {new_index} ...")
    es.indices.create(index=new_index, body=schema)

    # Prepare docs
    now = datetime.now(tz=__import__('datetime').timezone.utc).isoformat()
    actions = []
    for pt in place_types:
        doc = {
            'aat_id': pt['aat_id'],
            'parent_id': pt['parent_id'],
            'term': pt['term'],
            'term_full': pt['term_full'],
            'note': pt['note'],
            'fclasses': pt['fclasses'],
            'path': pt['path'],
            'ancestors': pt.get('ancestors', []),
            'depth': pt['depth'],
            'is_place_type': pt['is_place_type'],
            'indexed_at': now,
        }
        # Multilingual labels: {lang: label_string, ...}
        if pt.get('labels'):
            doc['labels'] = pt['labels']
        # Multilingual notes: {lang: note_string, ...}
        if pt.get('notes'):
            doc['notes'] = pt['notes']

        actions.append({
            '_index': new_index,
            '_id': f"aat:{pt['aat_id']}",
            '_source': doc,
        })

    # Bulk index
    print(f"Indexing {len(actions)} documents ...")
    success, errors = helpers.bulk(es, actions, stats_only=True,
                                   raise_on_error=False)
    print(f"  -> {success} indexed, {errors} errors")

    # Refresh
    es.indices.refresh(index=new_index)

    # Atomic alias swap
    print(f"Swapping alias '{index_name}' → {new_index} ...")
    alias_actions = []

    # Remove old alias targets
    try:
        existing = es.indices.get_alias(name=index_name)
        for old_index in existing:
            alias_actions.append({"remove": {"index": old_index, "alias": index_name}})
    except Exception:
        pass  # Alias doesn't exist yet

    alias_actions.append({"add": {"index": new_index, "alias": index_name}})

    es.indices.update_aliases(body={"actions": alias_actions})
    print(f"  -> Alias '{index_name}' now points to {new_index}")

    # Clean up old indices
    try:
        existing = es.indices.get_alias(name=index_name)
        for idx in existing:
            if idx != new_index:
                print(f"  -> Deleting old index {idx}")
                es.indices.delete(index=idx)
    except Exception:
        pass

    return new_index


# ============================================================================
# Reporting
# ============================================================================

def report(place_types):
    fclass_counts = Counter()
    for pt in place_types:
        for fc in (pt['fclasses'] or []):
            fclass_counts[fc] += 1
    no_fclass = sum(1 for pt in place_types if not pt['fclasses'])

    print("\nBreakdown by fclass:")
    for fc in sorted(fclass_counts):
        print(f"  {fc}: {fclass_counts[fc]:,} types")
    if no_fclass:
        print(f"  (no fclass): {no_fclass:,} types")

    multi = sum(1 for pt in place_types if pt['fclasses'] and len(pt['fclasses']) > 1)
    print(f"\nMulti-fclass concepts: {multi:,}")

    depths = Counter(pt['depth'] for pt in place_types)
    print("\nBy depth:")
    for d in sorted(depths):
        print(f"  depth {d}: {depths[d]:,}")

    no_label = [pt for pt in place_types if re.fullmatch(r'aat:\d+', pt['term'])]
    if no_label:
        print(f"\nMissing English labels: {len(no_label):,}")
        for pt in no_label[:20]:
            print(f"  aat:{pt['aat_id']}")
        if len(no_label) > 20:
            print(f"  ... and {len(no_label) - 20} more")

    # Multilingual stats
    with_labels = sum(1 for pt in place_types if pt.get('labels'))
    with_notes = sum(1 for pt in place_types if pt.get('notes'))
    all_langs = set()
    for pt in place_types:
        all_langs.update(pt.get('labels', {}).keys())
        all_langs.update(pt.get('notes', {}).keys())
    if all_langs:
        print(f"\nMultilingual coverage:")
        print(f"  Concepts with multilingual labels: {with_labels:,}")
        print(f"  Concepts with multilingual notes:  {with_notes:,}")
        print(f"  Languages represented: {len(all_langs)} — {', '.join(sorted(all_langs))}")


# ============================================================================
# Main
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Synchronise ES types index with AAT"
    )
    parser.add_argument('--force', action='store_true',
                        help='Re-download even if unchanged')
    parser.add_argument('--local', type=str, default=None,
                        help='Path to directory containing .nt files')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report counts without indexing')
    parser.add_argument('--api', action='store_true',
                        help='Crawl via JSON API instead of bulk dump')
    parser.add_argument('--es-host', type=str, default='http://localhost:9200',
                        help='Elasticsearch host URL')
    args = parser.parse_args()

    t0 = time.time()

    if args.api:
        place_types = crawl_api()
    else:
        # Step 1: Obtain the .nt files
        if args.local:
            nt_dir = Path(args.local)
            if not nt_dir.is_dir():
                raise RuntimeError(f"Not a directory: {args.local}")
            print(f"Using local directory: {nt_dir}")
        else:
            nt_dir = download_if_needed(args.force)

        # Step 2: Parse
        t1 = time.time()

        print("Parsing hierarchy edges ...")
        preferred_parent, children, parents = parse_hierarchy(
            nt_dir / AAT_NT_HIERARCHICAL_RELS)
        print(f"  -> {sum(len(v) for v in children.values()):,} edges, "
              f"{len(preferred_parent):,} preferred-parent links")

        print("Parsing labels (multilingual) ...")
        labels, all_labels = parse_labels(nt_dir / AAT_NT_TERMS)
        print(f"  -> {len(labels):,} English labels")

        print("Parsing scope notes (multilingual) ...")
        notes, all_notes = parse_notes(nt_dir / AAT_NT_SCOPE_NOTES)
        print(f"  -> {len(notes):,} English scope notes")

        # Step 3: Walk hierarchy
        print("Walking hierarchy from entry points ...")
        place_types = walk_hierarchy(
            preferred_parent, children, parents, labels, notes,
            all_labels, all_notes)

    t_parsed = time.time()
    print(f"\n{len(place_types):,} place-type concepts collected "
          f"in {t_parsed - t0:.1f}s")

    # Backfill missing labels
    gaps = [pt for pt in place_types if re.fullmatch(r'aat:\d+', pt['term'])]
    if gaps and not args.dry_run:
        print(f"\n{len(gaps)} concept(s) missing English labels — backfilling ...")
        backfill_labels_from_api(gaps)

    if args.dry_run:
        print("\n--- DRY RUN ---")
        report(place_types)
        return

    # Step 4: Index to ES
    new_index = index_to_es(place_types, args.es_host)

    t3 = time.time()
    print(f"\nDone. {len(place_types)} types indexed to {new_index} "
          f"in {t3 - t0:.1f}s total")


if __name__ == "__main__":
    main()










