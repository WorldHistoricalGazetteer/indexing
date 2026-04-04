# typesystem/hitl_review.py

"""
Human-in-the-loop (HITL) interactive review tool for unmapped type entries.

Presents unmapped types in descending order of count (highest impact first).
For each item, queries the local ES types index for candidate AAT matches
using relaxed multi-field search, and offers the reviewer a choice:

  a–o    — Accept a candidate by letter (single keystroke)
  aat:ID — Type an AAT numeric ID directly (e.g. aat:300008347)
  /term  — Search AAT for a custom term
  Enter  — Skip (come back to it later)
  x      — Mark as explicitly excluded (no valid AAT concept)
  q      — Quit and save progress

Progress is saved automatically. Re-running the tool resumes where you left
off (already-reviewed items are skipped).

Usage:
    python -m typesystem.hitl_review --es-host http://localhost:9201
    python -m typesystem.hitl_review --es-host http://localhost:9201 --min-count 100
    python -m typesystem.hitl_review --es-host http://localhost:9201 --namespace wikidata
    python -m typesystem.hitl_review --stats
"""

import json
import os
import string
import sys
import tempfile
import textwrap
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
PROGRESS_FILE = DATA_DIR / "hitl_progress.json"

ES_TYPES_INDEX = "types"
MAX_CANDIDATES = 15  # a–o

# Letters used for candidate selection (a through o)
CANDIDATE_LETTERS = string.ascii_lowercase[:MAX_CANDIDATES]  # 'abcdefghijklmno'

# ── Namespace → (filename, namespace key) ────────────────────────────────────
NAMESPACES = [
    ("osm.json", "osm"),
    ("ohm.json", "ohm"),
    ("geonames.json", "geonames"),
    ("wikidata.json", "wikidata"),
    ("pleiades.json", "pleiades"),
]

# ── Generic / uninformative tag values (OSM/OHM) ────────────────────────────
GENERIC_VALUES = {"yes", "no", "true", "false", "unknown", "other", "none",
                  "fixme", "user_defined", "undefined", "unclassified"}


# ============================================================================
# I/O helpers (mirrors aat_mapper.py)
# ============================================================================

def load_data_file(name):
    path = DATA_DIR / name
    with open(path) as f:
        return json.load(f)


def save_data_file(name, data):
    """Atomic write: temp file + rename."""
    path = DATA_DIR / name
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def load_progress():
    """Load the set of already-reviewed item keys."""
    if PROGRESS_FILE.is_file():
        with open(PROGRESS_FILE) as f:
            data = json.load(f)
        return set(data.get("reviewed", [])), set(data.get("excluded", []))
    return set(), set()


def save_progress(reviewed, excluded):
    """Persist the set of reviewed/excluded item keys."""
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({
                "reviewed": sorted(reviewed),
                "excluded": sorted(excluded),
            }, f, indent=2)
        os.replace(tmp_path, PROGRESS_FILE)
    except BaseException:
        os.unlink(tmp_path)
        raise


# ============================================================================
# Collect unmapped entries from data files
# ============================================================================

def iter_values(data, namespace):
    """Yield (key_path, entry) for all value entries. Same logic as aat_mapper."""
    if namespace in ("osm", "ohm"):
        for tag_key, tag_data in data.items():
            if tag_key.startswith("_") or not isinstance(tag_data, dict):
                continue
            for entry in tag_data.get("values", []):
                yield f"{tag_key}={entry.get('value', '')}", entry
    elif namespace == "geonames":
        for fclass, fclass_data in data.items():
            if not isinstance(fclass_data, dict):
                continue
            for entry in fclass_data.get("values", []):
                yield f"{fclass}.{entry.get('value', '')}", entry
    elif namespace == "wikidata":
        for entry in data.get("values", []):
            yield entry.get("value", ""), entry
    elif namespace == "pleiades":
        for entry in data.get("values", []):
            yield entry.get("value", ""), entry
        for entry in data.get("deprecated", []):
            yield entry.get("value", ""), entry


def load_all_data(namespace_filter=None):
    """Load all data files into memory. Returns dict: filename → data."""
    loaded = {}
    for filename, namespace in NAMESPACES:
        if namespace_filter and namespace != namespace_filter:
            continue
        try:
            loaded[filename] = load_data_file(filename)
        except FileNotFoundError:
            continue
    return loaded


def collect_unmapped(all_data, min_count=0, namespace_filter=None):
    """
    Collect all unmapped entries from pre-loaded data.
    Returns list of (global_key, namespace, filename, entry, label, count)
    sorted by count descending.

    The entry dicts are references into all_data, so mutations persist.
    """
    items = []

    for filename, namespace in NAMESPACES:
        if namespace_filter and namespace != namespace_filter:
            continue
        data = all_data.get(filename)
        if data is None:
            continue

        for key_path, entry in iter_values(data, namespace):
            if "aat_mapping" in entry:
                continue

            count = entry.get("count", 0)
            if count < min_count:
                continue

            if namespace == "wikidata":
                label = entry.get("label", "")
            elif namespace == "pleiades":
                label = entry.get("label", entry.get("value", ""))
            elif namespace == "geonames":
                label = entry.get("name", entry.get("value", ""))
            else:
                label = entry.get("value", "")

            global_key = f"{namespace}:{key_path}"
            items.append((global_key, namespace, filename, entry, label, count))

    items.sort(key=lambda x: x[5], reverse=True)
    return items


# ============================================================================
# ES candidate search — single combined bool query
# ============================================================================

def _inflect(word):
    """Generate simple singular/plural variants of a word."""
    variants = set()
    if word.endswith("ies"):
        variants.add(word[:-3] + "y")
    elif word.endswith("s"):
        variants.add(word[:-1])
    else:
        variants.add(word + "s")
    if word.endswith("y") and not word.endswith("ey"):
        variants.add(word[:-1] + "ies")
    variants.discard(word)
    return variants


def _es_search_single(es, query_text, limit=MAX_CANDIDATES):
    """
    Search the types index using a single combined bool/should query.
    All phases are scored together so ES can rank properly.
    """
    if not query_text or len(query_text) < 2:
        return []

    clean = query_text.replace("_", " ")
    variants = _inflect(clean)
    all_forms = [clean] + sorted(variants)

    # Build a single bool/should query with boosted clauses
    should = []

    for form in all_forms:
        # Exact keyword match (highest boost)
        should.append({"term": {"term.keyword": {"value": form, "boost": 30}}})
        # Keyword prefix — matches "buildings (structures)" for query "buildings"
        should.append({"wildcard": {"term.keyword": {"value": f"{form}*", "boost": 20}}})
        # Phrase prefix on folded field
        should.append({"match_phrase_prefix": {
            "term.folded": {"query": form, "boost": 10}}})
        # Folded match, all tokens required
        should.append({"match": {
            "term.folded": {"query": form, "operator": "and", "boost": 5}}})

    # Relaxed multi-field (any token, catches partial/description matches)
    should.append({
        "multi_match": {
            "query": clean,
            "fields": ["term.folded^3", "note"],
            "type": "most_fields",
            "operator": "or",
            "boost": 1,
        }
    })

    resp = es.search(
        index=ES_TYPES_INDEX,
        size=limit,
        query={"bool": {"should": should, "minimum_should_match": 1}},
        source=["aat_id", "term", "note"],
    )

    results = []
    seen = set()
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        aid = src["aat_id"]
        if aid not in seen:
            seen.add(aid)
            results.append({
                "aat_id": aid,
                "term": src.get("term", ""),
                "note": src.get("note", ""),
                "score": hit["_score"],
            })

    return results[:limit]


def _es_description_search(es, description, limit=MAX_CANDIDATES):
    """
    Use More Like This to find AAT concepts whose term/note text
    overlaps with an entry's description. This catches semantic matches
    like "body of water" → "bodies of water" that keyword search misses.
    """
    if not description or len(description) < 10:
        return []

    resp = es.search(
        index=ES_TYPES_INDEX,
        size=limit,
        query={
            "more_like_this": {
                "fields": ["term", "note"],
                "like": description[:500],
                "min_term_freq": 1,
                "min_doc_freq": 1,
                "max_query_terms": 25,
                "minimum_should_match": "30%",
            }
        },
        source=["aat_id", "term", "note"],
    )

    results = []
    seen = set()
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        aid = src["aat_id"]
        if aid not in seen:
            seen.add(aid)
            results.append({
                "aat_id": aid,
                "term": src.get("term", ""),
                "note": src.get("note", ""),
                "score": hit["_score"],
            })
    return results[:limit]


def build_search_terms(global_key, namespace, entry, label):
    """
    Build an ordered list of search terms to try for candidate lookup.

    For OSM/OHM entries with generic values like 'yes', falls back to the
    tag key (e.g. 'building') and the description.
    """
    terms = []

    if namespace in ("osm", "ohm"):
        value = entry.get("value", "")
        tag_key = ""
        if ":" in global_key and "=" in global_key:
            tag_key = global_key.split(":", 1)[1].split("=", 1)[0]

        if value.lower() not in GENERIC_VALUES:
            terms.append(value)
        if tag_key:
            terms.append(tag_key)
    else:
        if label:
            terms.append(label)

    return terms


def search_candidates(es, global_key, namespace, entry, label):
    """
    Search the ES types index for AAT candidates matching an entry.
    Tries multiple search terms, then uses description MLT for semantic overlap.
    """
    terms = build_search_terms(global_key, namespace, entry, label)
    seen = set()
    results = []

    # Phase 1: keyword/text search on label, tag key, etc.
    for term in terms:
        if len(results) >= MAX_CANDIDATES:
            break
        for r in _es_search_single(es, term, MAX_CANDIDATES):
            if r["aat_id"] not in seen:
                seen.add(r["aat_id"])
                results.append(r)
                if len(results) >= MAX_CANDIDATES:
                    break

    # Phase 2: description → AAT note/term cross-match via MLT
    desc = entry.get("description", "")
    if desc and len(results) < MAX_CANDIDATES:
        for r in _es_description_search(es, desc, MAX_CANDIDATES):
            if r["aat_id"] not in seen:
                seen.add(r["aat_id"])
                results.append(r)
                if len(results) >= MAX_CANDIDATES:
                    break

    return results[:MAX_CANDIDATES]


def fetch_aat_by_id(es, aat_id):
    """Fetch a single AAT concept by numeric ID. Returns dict or None."""
    try:
        resp = es.get(index=ES_TYPES_INDEX, id=f"aat:{aat_id}",
                      source=["aat_id", "term", "note"])
        if resp.get("found"):
            return resp["_source"]
    except Exception:
        pass
    return None


# ============================================================================
# Display helpers
# ============================================================================

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"
RESET = "\033[0m"


def truncate(text, width=80):
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text if len(text) <= width else text[:width - 1] + "…"


def display_item(idx, total, global_key, namespace, label, count,
                 description=None):
    """Display the current item being reviewed."""
    print()
    print(f"{BOLD}{'─' * 78}{RESET}")
    print(f"{BOLD}[{idx}/{total}]{RESET}  {CYAN}{global_key}{RESET}")
    print(f"  Label: {BOLD}{label}{RESET}   Count: {YELLOW}{count:,}{RESET}"
          f"   Namespace: {namespace}")
    if description:
        wrapped = textwrap.fill(description, width=74,
                                initial_indent="  ", subsequent_indent="  ")
        print(f"{DIM}{wrapped}{RESET}")


def display_candidates(candidates):
    """Display lettered candidate list (a–o)."""
    if not candidates:
        print(f"  {DIM}(no candidates found){RESET}")
        return

    print()
    for i, c in enumerate(candidates):
        letter = CANDIDATE_LETTERS[i]
        note_snip = truncate(c.get("note", ""), 55)
        note_display = f"  {DIM}{note_snip}{RESET}" if note_snip else ""
        print(f"  {GREEN}{letter}{RESET}) aat:{c['aat_id']}  "
              f"{BOLD}{c['term']}{RESET}{note_display}")


def display_prompt():
    """Show the action prompt."""
    print()
    print(f"  {DIM}[a-o] accept  |  aat:ID  |  /term search  "
          f"|  Enter skip  |  x exclude  |  q quit{RESET}")


# ============================================================================
# Main interactive loop
# ============================================================================

def _accept_candidate(entry, candidate, global_key, reviewed):
    """Apply a candidate mapping to an entry."""
    entry["aat_mapping"] = {
        "aat_id": candidate["aat_id"],
        "aat_term": candidate["term"],
        "confidence": "reviewed",
        "source": "hitl_review",
    }
    reviewed.add(global_key)


def _accept_aat_id(es, entry, aat_id, global_key, reviewed):
    """Look up an AAT ID, confirm, and apply. Returns True if accepted."""
    concept = fetch_aat_by_id(es, aat_id)
    if not concept:
        print(f"  {RED}aat:{aat_id} not found in types index{RESET}")
        return False
    term = concept.get("term", f"aat:{aat_id}")
    note = truncate(concept.get("note", ""), 60)
    print(f"  Found: {BOLD}{term}{RESET}")
    if note:
        print(f"  {DIM}{note}{RESET}")
    confirm = input(f"  Accept? [Y/n] ").strip().lower()
    if confirm in ("", "y", "yes"):
        entry["aat_mapping"] = {
            "aat_id": aat_id,
            "aat_term": term,
            "confidence": "reviewed",
            "source": "hitl_review",
        }
        reviewed.add(global_key)
        print(f"  {GREEN}✓ Mapped → aat:{aat_id} ({term}){RESET}")
        return True
    return False


def run_review(es, min_count=0, namespace_filter=None):
    """Main HITL review loop."""
    reviewed, excluded = load_progress()
    print(f"Progress loaded: {len(reviewed)} reviewed, {len(excluded)} excluded")

    all_data = load_all_data(namespace_filter=namespace_filter)
    items = collect_unmapped(all_data, min_count=min_count,
                             namespace_filter=namespace_filter)
    print(f"Found {len(items)} unmapped items (min_count={min_count})")

    pending = [(gk, ns, fn, entry, label, count)
               for gk, ns, fn, entry, label, count in items
               if gk not in reviewed and gk not in excluded]
    print(f"Pending review: {len(pending)} items")

    if not pending:
        print("Nothing to review!")
        return

    total = len(pending)
    session_accepted = 0
    session_excluded = 0
    session_skipped = 0

    try:
        for idx, (global_key, namespace, filename, entry, label, count) \
                in enumerate(pending, 1):

            description = entry.get("description", entry.get("name", ""))
            display_item(idx, total, global_key, namespace, label, count,
                         description)

            candidates = search_candidates(es, global_key, namespace,
                                           entry, label)
            display_candidates(candidates)
            display_prompt()

            # Input loop — allows /search refinement before deciding
            while True:
                try:
                    choice = input(f"  {BOLD}>{RESET} ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n\nInterrupted — saving progress ...")
                    choice = "q"
                    break

                if choice.startswith("/"):
                    search_term = choice[1:].strip()
                    if search_term:
                        candidates = _es_search_single(es, search_term)
                        display_candidates(candidates)
                        display_prompt()
                    else:
                        print(f"  {DIM}Usage: /buildings  or  "
                              f"/fortifications{RESET}")
                    continue
                break

            # ── Process the choice ──────────────────────────────────

            if choice.lower() == "q":
                print("\nQuitting — saving progress ...")
                break

            elif choice.lower() == "x":
                excluded.add(global_key)
                entry["aat_mapping"] = {
                    "aat_id": None,
                    "aat_term": None,
                    "confidence": "excluded",
                    "source": "hitl_review",
                }
                session_excluded += 1
                print(f"  {RED}✗ Excluded{RESET}")

            elif choice == "":
                session_skipped += 1
                continue  # don't mark as reviewed — revisit later

            elif (len(choice) == 1
                  and choice.lower() in CANDIDATE_LETTERS):
                ci = CANDIDATE_LETTERS.index(choice.lower())
                if ci < len(candidates):
                    c = candidates[ci]
                    _accept_candidate(entry, c, global_key, reviewed)
                    session_accepted += 1
                    print(f"  {GREEN}✓ Mapped → aat:{c['aat_id']} "
                          f"({c['term']}){RESET}")
                else:
                    print(f"  {DIM}(no candidate {choice}){RESET}")
                    session_skipped += 1

            elif choice.startswith("aat:"):
                try:
                    aat_id = int(choice[4:].strip())
                except ValueError:
                    print(f"  {RED}Invalid AAT ID{RESET}")
                    session_skipped += 1
                    continue
                if _accept_aat_id(es, entry, aat_id, global_key, reviewed):
                    session_accepted += 1
                else:
                    session_skipped += 1

            else:
                # Try parsing as a bare AAT numeric ID
                try:
                    aat_id = int(choice)
                    if aat_id > 100000:
                        if _accept_aat_id(es, entry, aat_id, global_key,
                                          reviewed):
                            session_accepted += 1
                        else:
                            session_skipped += 1
                    else:
                        print(f"  {DIM}(unrecognised input){RESET}")
                        session_skipped += 1
                except ValueError:
                    print(f"  {DIM}(unrecognised input){RESET}")
                    session_skipped += 1

            # Auto-save every 25 decisions
            decisions = session_accepted + session_excluded
            if decisions > 0 and decisions % 25 == 0:
                _save_all(all_data, reviewed, excluded)
                print(f"  {DIM}(auto-saved){RESET}")

    finally:
        _save_all(all_data, reviewed, excluded)
        print()
        print(f"{'─' * 78}")
        print(f"Session summary:")
        print(f"  Accepted:  {GREEN}{session_accepted}{RESET}")
        print(f"  Excluded:  {RED}{session_excluded}{RESET}")
        print(f"  Skipped:   {DIM}{session_skipped}{RESET}")
        print(f"  Total reviewed: {len(reviewed)}  |  "
              f"Total excluded: {len(excluded)}")
        print(f"{'─' * 78}")


def _save_all(all_data, reviewed, excluded):
    """Save all modified data files and progress."""
    for filename, data in all_data.items():
        save_data_file(filename, data)
    save_progress(reviewed, excluded)


# ============================================================================
# Stats command
# ============================================================================

def show_stats(min_count=0):
    """Show unmapped item statistics without starting a review session."""
    reviewed, excluded = load_progress()

    print(f"{'=' * 70}")
    print(f"HITL REVIEW STATISTICS")
    print(f"{'=' * 70}")
    print(f"Progress: {len(reviewed)} reviewed, {len(excluded)} excluded")
    print()

    grand_total = 0
    grand_mapped = 0
    grand_unmapped = 0
    grand_pending = 0

    for filename, namespace in NAMESPACES:
        try:
            data = load_data_file(filename)
        except FileNotFoundError:
            continue

        total = 0
        mapped = 0
        unmapped = 0
        pending = 0
        unmapped_counts = []

        for key_path, entry in iter_values(data, namespace):
            total += 1
            if "aat_mapping" in entry:
                mapped += 1
            else:
                count = entry.get("count", 0)
                unmapped += 1
                gk = f"{namespace}:{key_path}"
                if (gk not in reviewed and gk not in excluded
                        and count >= min_count):
                    pending += 1
                    unmapped_counts.append(count)

        grand_total += total
        grand_mapped += mapped
        grand_unmapped += unmapped
        grand_pending += pending

        pct_mapped = (mapped / total * 100) if total else 0

        print(f"{filename} ({namespace}):")
        print(f"  Total: {total}  Mapped: {mapped} ({pct_mapped:.1f}%)  "
              f"Unmapped: {unmapped}  Pending: {pending}")
        if unmapped_counts:
            unmapped_counts.sort(reverse=True)
            print(f"  Pending count range: {unmapped_counts[0]:,} – "
                  f"{unmapped_counts[-1]:,}")
            brackets = [
                ("≥1000", sum(1 for c in unmapped_counts if c >= 1000)),
                ("≥100", sum(1 for c in unmapped_counts if c >= 100)),
                ("≥50", sum(1 for c in unmapped_counts if c >= 50)),
                ("≥10", sum(1 for c in unmapped_counts if c >= 10)),
            ]
            bracket_str = "  |  ".join(f"{lbl}: {n}"
                                       for lbl, n in brackets)
            print(f"  Brackets: {bracket_str}")
        print()

    pct = (grand_mapped / grand_total * 100) if grand_total else 0
    print(f"{'=' * 70}")
    print(f"TOTALS: {grand_mapped}/{grand_total} mapped ({pct:.1f}%)  "
          f"Unmapped: {grand_unmapped}  Pending review: {grand_pending}")
    print(f"{'=' * 70}")


# ============================================================================
# CLI
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Interactive HITL review of unmapped type entries"
    )
    parser.add_argument("--es-host",
        help="ES host with types index (required for review)")
    parser.add_argument("--min-count", type=int, default=100,
        help="Minimum count threshold (default: 100)")
    parser.add_argument("--namespace",
        choices=["osm", "ohm", "geonames", "wikidata", "pleiades"],
        help="Review only a specific namespace")
    parser.add_argument("--stats", action="store_true",
        help="Show statistics without starting a review session")
    parser.add_argument("--reset-progress", action="store_true",
        help="Clear all review progress (use with caution)")

    args = parser.parse_args()

    if args.reset_progress:
        if PROGRESS_FILE.is_file():
            PROGRESS_FILE.unlink()
            print("Progress file deleted.")
        else:
            print("No progress file found.")
        return

    if args.stats:
        show_stats(min_count=args.min_count)
        return

    if not args.es_host:
        parser.error("--es-host is required for review mode "
                     "(use --stats for offline stats)")

    from typesystem.es_client import create_client
    es = create_client(args.es_host)

    try:
        info = es.info()
        print(f"Connected to ES {info['version']['number']}")
    except Exception as e:
        print(f"ERROR: Cannot connect to ES at {args.es_host}: {e}",
              file=sys.stderr)
        sys.exit(1)

    try:
        count = es.count(index=ES_TYPES_INDEX)
        print(f"Types index: {count['count']} documents")
    except Exception as e:
        print(f"ERROR: Types index not available: {e}", file=sys.stderr)
        sys.exit(1)

    run_review(es, min_count=args.min_count, namespace_filter=args.namespace)


if __name__ == "__main__":
    main()


