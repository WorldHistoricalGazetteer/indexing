# typesystem/hitl_review.py

"""
Human-in-the-loop (HITL) interactive review tool for unmapped type entries.

Presents unmapped types in descending order of count (highest impact first).
For each item, queries the local ES types index for candidate AAT matches
using relaxed multi-field search, and offers the reviewer a choice:

  [1-5]  — Accept a candidate by number
  aat:ID — Type an AAT numeric ID directly (e.g. 300008347)
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
import sys
import tempfile
import textwrap
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
PROGRESS_FILE = DATA_DIR / "hitl_progress.json"

ES_TYPES_INDEX = "types"

# ── Namespace → (filename, namespace key) ────────────────────────────────────
NAMESPACES = [
    ("osm.json", "osm"),
    ("ohm.json", "ohm"),
    ("geonames.json", "geonames"),
    ("wikidata.json", "wikidata"),
    ("pleiades.json", "pleiades"),
]


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

            # Determine count
            count = entry.get("count", 0)
            if count < min_count:
                continue

            # Determine display label
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

    # Sort by count descending
    items.sort(key=lambda x: x[5], reverse=True)
    return items


# ── Generic / uninformative tag values (OSM/OHM) ────────────────────────────
GENERIC_VALUES = {"yes", "no", "true", "false", "unknown", "other", "none",
                  "fixme", "user_defined", "undefined", "unclassified"}


# ============================================================================
# ES candidate search (relaxed matching)
# ============================================================================

def _es_search_single(es, query_text, limit, seen):
    """Run the multi-phase search for a single query string. Returns new results."""
    if not query_text or len(query_text) < 2:
        return []

    clean = query_text.replace("_", " ")
    results = []

    def _collect(resp, match_type):
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
                    "match_type": match_type,
                })

    # Phase 1: exact keyword match
    resp = es.search(
        index=ES_TYPES_INDEX, size=limit,
        query={"term": {"term.keyword": clean}},
        source=["aat_id", "term", "note"],
    )
    _collect(resp, "exact")
    if len(results) >= limit:
        return results[:limit]

    # Phase 1b: try simple plural/singular variants on keyword
    variants = set()
    if clean.endswith("s"):
        variants.add(clean[:-1])          # buildings → building
    else:
        variants.add(clean + "s")          # building → buildings
    if clean.endswith("ies"):
        variants.add(clean[:-3] + "y")     # cities → city
    elif clean.endswith("y"):
        variants.add(clean[:-1] + "ies")   # city → cities
    for v in variants:
        if len(results) >= limit:
            break
        resp = es.search(
            index=ES_TYPES_INDEX, size=limit,
            query={"term": {"term.keyword": v}},
            source=["aat_id", "term", "note"],
        )
        _collect(resp, "exact")
    if len(results) >= limit:
        return results[:limit]

    # Phase 2: match_phrase_prefix — prefers terms starting with query
    # Try original + all variants
    for phrase in [clean] + sorted(variants):
        if len(results) >= limit:
            break
        resp = es.search(
            index=ES_TYPES_INDEX, size=limit,
            query={"match_phrase_prefix": {"term.folded": {"query": phrase}}},
            source=["aat_id", "term", "note"],
        )
        _collect(resp, "prefix")
    if len(results) >= limit:
        return results[:limit]

    # Phase 3: folded match (all tokens required)
    resp = es.search(
        index=ES_TYPES_INDEX, size=limit,
        query={"match": {"term.folded": {"query": clean, "operator": "and"}}},
        source=["aat_id", "term", "note"],
    )
    _collect(resp, "folded")
    if len(results) >= limit:
        return results[:limit]

    # Phase 4: relaxed multi-field (any token, term boosted over note)
    resp = es.search(
        index=ES_TYPES_INDEX, size=limit * 2,
        query={
            "multi_match": {
                "query": clean,
                "fields": ["term.folded^3", "note"],
                "type": "most_fields",
                "operator": "or",
            }
        },
        source=["aat_id", "term", "note"],
    )
    _collect(resp, "relaxed")

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
        # Extract tag key from global_key  (e.g. "osm:building=yes" → "building")
        tag_key = ""
        if ":" in global_key and "=" in global_key:
            tag_key = global_key.split(":", 1)[1].split("=", 0 + 1)[0]

        if value.lower() not in GENERIC_VALUES:
            terms.append(value)
        if tag_key:
            terms.append(tag_key)
    else:
        if label:
            terms.append(label)

    # Always try the description as a last resort
    desc = entry.get("description", "")
    if desc and len(desc) < 120:
        terms.append(desc)

    return terms


def search_candidates(es, global_key, namespace, entry, label, limit=10):
    """
    Search the ES types index for AAT candidates matching an entry.

    Tries multiple search terms in priority order (label, tag key,
    description) and merges deduplicated results.
    """
    terms = build_search_terms(global_key, namespace, entry, label)
    seen = set()
    results = []

    for term in terms:
        if len(results) >= limit:
            break
        new = _es_search_single(es, term, limit - len(results), seen)
        results.extend(new)

    return results[:limit]


def fetch_aat_by_id(es, aat_id):
    """Fetch a single AAT concept by numeric ID. Returns dict or None."""
    try:
        resp = es.get(index=ES_TYPES_INDEX, id=f"aat:{aat_id}", source=["aat_id", "term", "note"])
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
    if len(text) <= width:
        return text
    return text[:width - 1] + "…"


def display_item(idx, total, global_key, namespace, label, count, description=None):
    """Display the current item being reviewed."""
    print()
    print(f"{BOLD}{'─' * 78}{RESET}")
    print(f"{BOLD}[{idx}/{total}]{RESET}  {CYAN}{global_key}{RESET}")
    print(f"  Label: {BOLD}{label}{RESET}   Count: {YELLOW}{count:,}{RESET}   Namespace: {namespace}")
    if description:
        wrapped = textwrap.fill(description, width=74, initial_indent="  ", subsequent_indent="  ")
        print(f"{DIM}{wrapped}{RESET}")


def display_candidates(candidates):
    """Display numbered candidate list."""
    if not candidates:
        print(f"  {DIM}(no candidates found){RESET}")
        return

    print()
    for i, c in enumerate(candidates, 1):
        match_tag = f"[{c['match_type']}]"
        note_snip = truncate(c.get("note", ""), 60)
        note_display = f"  {DIM}{note_snip}{RESET}" if note_snip else ""
        print(f"  {GREEN}{i}{RESET}) aat:{c['aat_id']}  {BOLD}{c['term']}{RESET}  "
              f"{DIM}{match_tag}{RESET}{note_display}")


def display_prompt():
    """Show the action prompt."""
    print()
    print(f"  {DIM}[1-10] accept  |  aat:ID  |  /term search  |  Enter skip  |  x exclude  |  q quit{RESET}")


# ============================================================================
# Main interactive loop
# ============================================================================

def run_review(es, min_count=0, namespace_filter=None):
    """Main HITL review loop."""
    reviewed, excluded = load_progress()
    print(f"Progress loaded: {len(reviewed)} reviewed, {len(excluded)} excluded")

    # Load all data files once — entry references stay valid for mutation
    all_data = load_all_data(namespace_filter=namespace_filter)
    items = collect_unmapped(all_data, min_count=min_count, namespace_filter=namespace_filter)
    print(f"Found {len(items)} unmapped items (min_count={min_count})")

    # Filter out already-reviewed items
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
        for idx, (global_key, namespace, filename, entry, label, count) in enumerate(pending, 1):

            # Get description for context
            description = entry.get("description", entry.get("name", ""))

            # Display item
            display_item(idx, total, global_key, namespace, label, count, description)

            # Search for candidates
            candidates = search_candidates(es, global_key, namespace, entry, label)
            display_candidates(candidates)
            display_prompt()

            # Get user input — loop to allow /search refinement
            while True:
                try:
                    choice = input(f"  {BOLD}>{RESET} ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n\nInterrupted — saving progress ...")
                    choice = "q"
                    break

                if choice.startswith("/"):
                    # Manual search: re-query with user-provided term
                    search_term = choice[1:].strip()
                    if search_term:
                        candidates = _es_search_single(es, search_term, 10, set())
                        display_candidates(candidates)
                        display_prompt()
                        continue
                    else:
                        print(f"  {DIM}Usage: /buildings  or  /fortifications{RESET}")
                        continue
                break  # any non-/ input exits the search loop

            if choice.lower() == "q":
                print("\nQuitting — saving progress ...")
                break

            elif choice.lower() == "x":
                # Explicitly exclude — no valid AAT concept
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
                # Skip
                session_skipped += 1
                continue  # Don't mark as reviewed — can come back

            elif choice.isdigit() and 1 <= int(choice) <= len(candidates):
                # Accept a candidate
                c = candidates[int(choice) - 1]
                entry["aat_mapping"] = {
                    "aat_id": c["aat_id"],
                    "aat_term": c["term"],
                    "confidence": "reviewed",
                    "source": "hitl_review",
                }
                reviewed.add(global_key)
                session_accepted += 1
                print(f"  {GREEN}✓ Mapped → aat:{c['aat_id']} ({c['term']}){RESET}")

            elif choice.startswith("aat:") or (choice.isdigit() and len(choice) > 5):
                # Direct AAT ID entry
                try:
                    aat_id = int(choice.replace("aat:", "").strip())
                except ValueError:
                    print(f"  {RED}Invalid AAT ID{RESET}")
                    session_skipped += 1
                    continue

                # Look up the concept
                concept = fetch_aat_by_id(es, aat_id)
                if concept:
                    term = concept.get("term", f"aat:{aat_id}")
                    print(f"  Found: {BOLD}{term}{RESET}")
                    confirm = input(f"  Accept? [Y/n] ").strip().lower()
                    if confirm in ("", "y", "yes"):
                        entry["aat_mapping"] = {
                            "aat_id": aat_id,
                            "aat_term": term,
                            "confidence": "reviewed",
                            "source": "hitl_review",
                        }
                        reviewed.add(global_key)
                        session_accepted += 1
                        print(f"  {GREEN}✓ Mapped → aat:{aat_id} ({term}){RESET}")
                    else:
                        session_skipped += 1
                else:
                    print(f"  {RED}aat:{aat_id} not found in types index{RESET}")
                    session_skipped += 1

            else:
                # Try parsing as a bare large number (AAT ID)
                try:
                    aat_id = int(choice)
                    if aat_id > 100000:
                        concept = fetch_aat_by_id(es, aat_id)
                        if concept:
                            term = concept.get("term", f"aat:{aat_id}")
                            print(f"  Found: {BOLD}{term}{RESET}")
                            confirm = input(f"  Accept? [Y/n] ").strip().lower()
                            if confirm in ("", "y", "yes"):
                                entry["aat_mapping"] = {
                                    "aat_id": aat_id,
                                    "aat_term": term,
                                    "confidence": "reviewed",
                                    "source": "hitl_review",
                                }
                                reviewed.add(global_key)
                                session_accepted += 1
                                print(f"  {GREEN}✓ Mapped → aat:{aat_id} ({term}){RESET}")
                            else:
                                session_skipped += 1
                        else:
                            print(f"  {RED}aat:{aat_id} not found{RESET}")
                            session_skipped += 1
                    else:
                        print(f"  {DIM}(unrecognised input — skipping){RESET}")
                        session_skipped += 1
                except ValueError:
                    print(f"  {DIM}(unrecognised input — skipping){RESET}")
                    session_skipped += 1

            # Auto-save every 25 accepted/excluded items
            if (session_accepted + session_excluded) > 0 and (session_accepted + session_excluded) % 25 == 0:
                _save_all(all_data, reviewed, excluded)
                print(f"  {DIM}(auto-saved){RESET}")

    finally:
        # Always save on exit
        _save_all(all_data, reviewed, excluded)
        print()
        print(f"{'─' * 78}")
        print(f"Session summary:")
        print(f"  Accepted:  {GREEN}{session_accepted}{RESET}")
        print(f"  Excluded:  {RED}{session_excluded}{RESET}")
        print(f"  Skipped:   {DIM}{session_skipped}{RESET}")
        print(f"  Total reviewed: {len(reviewed)}  |  Total excluded: {len(excluded)}")
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
                if gk not in reviewed and gk not in excluded and count >= min_count:
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
            print(f"  Pending count range: {unmapped_counts[0]:,} – {unmapped_counts[-1]:,}")
            # Show count brackets
            brackets = [
                ("≥1000", sum(1 for c in unmapped_counts if c >= 1000)),
                ("≥100", sum(1 for c in unmapped_counts if c >= 100)),
                ("≥50", sum(1 for c in unmapped_counts if c >= 50)),
                ("≥10", sum(1 for c in unmapped_counts if c >= 10)),
            ]
            bracket_str = "  |  ".join(f"{label}: {n}" for label, n in brackets)
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
        parser.error("--es-host is required for review mode (use --stats for offline stats)")

    from typesystem.es_client import create_client
    es = create_client(args.es_host)

    # Verify ES connection and types index
    try:
        info = es.info()
        print(f"Connected to ES {info['version']['number']}")
    except Exception as e:
        print(f"ERROR: Cannot connect to ES at {args.es_host}: {e}", file=sys.stderr)
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














