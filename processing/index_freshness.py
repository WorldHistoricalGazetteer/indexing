"""Detect namespaces whose index was built from a since-superseded artefact.

The failure this exists to catch, from the place#164 rebuild (4 August 2026):
``update_merge`` was re-run for ``gn``, ``wd`` and ``nl`` *after* those
namespaces had already been indexed. The chain rewrote each
``staged/<ns>/final/places.parquet``; nothing re-ran the index stage. The
manifest still said ``index: completed`` — true when written, false afterwards
— and carried no timestamp with which to notice.

**Doc counts cannot detect this.** ``update_merge`` adds names to *existing*
places, so the place count is byte-identical before and after. A staging-vs-
production comparison matched on all 27 namespaces while ``gn`` was missing
26.7 M GeoNames alternate names — one toponym per place instead of the real
inventory. Counts were the one measure guaranteed to look right either way.

So freshness is established from the artefacts themselves:

* **Preferred** — the index stage records a fingerprint (path, size, mtime) of
  the file it actually read, in its metrics. A later run compares that against
  the file now on disk.
* **Fallback** — for runs indexed before fingerprinting existed, compare the
  mtime of the newest ``final/places.*`` against the ``index/`` stage
  directory. Coarser, but it is what identified all three stale namespaces.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from processing.settings import STAGED_BASE_DIR

# Same priority chain as index_from_stage._STAGED_SOURCE_PRIORITY; duplicated
# rather than imported to keep this module free of that one's ES dependencies,
# so it can run anywhere (including as a preflight on a login node).
_SOURCE_PRIORITY = ("final", "h3_merged", "boundary_merged", "update_merged",
                    "extract")


def source_fingerprint(path: Path) -> dict[str, Any]:
    """Identify a staged artefact by what would change if it were rewritten."""
    st = path.stat()
    return {"path": str(path), "size": st.st_size, "mtime": st.st_mtime}


def current_source(namespace: str,
                   staged_base: Path | None = None) -> Path | None:
    """The artefact the index stage would read for ``namespace`` right now."""
    base = Path(staged_base or STAGED_BASE_DIR) / namespace
    for stage in _SOURCE_PRIORITY:
        for ext in ("parquet", "jsonl"):
            candidate = base / stage / f"places.{ext}"
            if candidate.exists():
                return candidate
    return None


def _newest_final_mtime(namespace: str,
                        staged_base: Path | None = None) -> float | None:
    base = Path(staged_base or STAGED_BASE_DIR) / namespace
    mtimes = [p.stat().st_mtime
              for stage in _SOURCE_PRIORITY
              for p in (base / stage).glob("places.*")
              if p.is_file()]
    return max(mtimes) if mtimes else None


def _index_stage_mtime(namespace: str,
                       staged_base: Path | None = None) -> float | None:
    d = Path(staged_base or STAGED_BASE_DIR) / namespace / "index"
    return d.stat().st_mtime if d.exists() else None


def check_namespace(namespace: str, manifest: dict[str, Any] | None = None,
                    staged_base: Path | None = None) -> dict[str, Any]:
    """Freshness verdict for one namespace.

    ``stale`` is True only on positive evidence that the source changed after
    indexing. ``unknown`` marks the cases we cannot speak to (never indexed, no
    artefact) so a caller can decide whether that is acceptable rather than
    having it silently fold into "fine".
    """
    result: dict[str, Any] = {"namespace": namespace, "stale": False,
                              "unknown": False, "basis": None, "detail": ""}

    recorded = None
    if manifest:
        ns_entry = (manifest.get("namespaces", {}).get(namespace) or {})
        # update_namespace_stage_status stores the stage VALUE as a bare status
        # string and puts metrics in a sibling `stage_metrics` map. Reading
        # only stages["index"]["metrics"] finds nothing and silently degrades
        # to the mtime fallback — which cannot clear after a re-index, because
        # nothing touches the index/ directory. Both shapes are accepted so
        # the check does not depend on that detail staying put.
        metrics = (ns_entry.get("stage_metrics") or {}).get("index") or {}
        recorded = metrics.get("source_fingerprint")
        if not recorded:
            idx = (ns_entry.get("stages", ns_entry) or {}).get("index")
            if isinstance(idx, dict):
                recorded = (idx.get("metrics") or {}).get("source_fingerprint")

    current = current_source(namespace, staged_base)
    if current is None:
        result.update(unknown=True, basis="no-artefact",
                      detail="no staged places.* found")
        return result

    if recorded:
        now = source_fingerprint(current)
        changed = (recorded.get("path") != now["path"]
                   or recorded.get("size") != now["size"]
                   or abs(float(recorded.get("mtime", 0)) - now["mtime"]) > 1.0)
        result.update(
            stale=changed, basis="fingerprint",
            detail=(f"indexed {recorded.get('path')} "
                    f"({recorded.get('size')} bytes, "
                    f"{_fmt(recorded.get('mtime'))}); "
                    f"now {now['path']} ({now['size']} bytes, "
                    f"{_fmt(now['mtime'])})") if changed else "fingerprint match",
        )
        return result

    src_mt = _newest_final_mtime(namespace, staged_base)
    idx_mt = _index_stage_mtime(namespace, staged_base)
    if src_mt is None or idx_mt is None:
        result.update(unknown=True, basis="mtime",
                      detail="no index/ stage directory to compare against")
        return result
    result.update(
        stale=src_mt > idx_mt, basis="mtime",
        detail=f"source {_fmt(src_mt)} vs indexed {_fmt(idx_mt)}",
    )
    return result


def _fmt(ts: float | None) -> str:
    if not ts:
        return "?"
    return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def check_all(namespaces: list[str], manifest: dict[str, Any] | None = None,
              staged_base: Path | None = None) -> list[dict[str, Any]]:
    return [check_namespace(ns, manifest, staged_base) for ns in namespaces]


def stale_namespaces(namespaces: list[str],
                     manifest: dict[str, Any] | None = None,
                     staged_base: Path | None = None) -> list[str]:
    return [r["namespace"] for r in check_all(namespaces, manifest, staged_base)
            if r["stale"]]


# ---------------------------------------------------------------------------
# Vocabulary (toponyms DuckDB) freshness
# ---------------------------------------------------------------------------
#
# The same fault, one stage over. The toponym vocabulary is built by scanning
# every namespace's staged final, and that scan takes hours — during which the
# corpus can be rewritten underneath it. It happened twice in the place#164
# rebuild: the vocabulary was built from `wd`'s superseded final (scan ended
# 04:11, final rewritten 04:53), and again from pre-merge `chgis`/`hgis`.
#
# Both turned out immaterial, but establishing that cost a 3.5-hour re-run and
# a bespoke comparison against a backup that happened to still exist. Recording
# what was scanned makes the question answerable in a second.

VOCABULARY_SOURCES_SUFFIX = ".sources.json"


def vocabulary_sources_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + VOCABULARY_SOURCES_SUFFIX)


def record_vocabulary_sources(db_path: str | Path, namespaces: list[str],
                              staged_base: Path | None = None) -> Path:
    """Record the artefact each namespace contributed to a vocabulary build."""
    import json
    out = {}
    for ns in namespaces:
        src = current_source(ns, staged_base)
        if src is not None:
            out[ns] = source_fingerprint(src)
    path = vocabulary_sources_path(db_path)
    path.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    return path


def check_vocabulary(db_path: str | Path,
                     staged_base: Path | None = None) -> list[dict[str, Any]]:
    """Which namespaces have been rewritten since the vocabulary was built?"""
    import json
    path = vocabulary_sources_path(db_path)
    if not path.exists():
        return [{"namespace": "*", "stale": False, "unknown": True,
                 "basis": "no-record",
                 "detail": f"no {path.name}; vocabulary provenance unknown"}]
    recorded = json.loads(path.read_text())
    results = []
    for ns, fp in sorted(recorded.items()):
        src = current_source(ns, staged_base)
        if src is None:
            results.append({"namespace": ns, "stale": False, "unknown": True,
                            "basis": "no-artefact", "detail": "source gone"})
            continue
        now = source_fingerprint(src)
        changed = (fp.get("path") != now["path"]
                   or fp.get("size") != now["size"]
                   or abs(float(fp.get("mtime", 0)) - now["mtime"]) > 1.0)
        results.append({
            "namespace": ns, "stale": changed, "unknown": False,
            "basis": "fingerprint",
            "detail": (f"scanned {_fmt(fp.get('mtime'))}, "
                       f"now {_fmt(now['mtime'])}") if changed else "match",
        })
    return results


def main() -> None:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(
        description="Report namespaces indexed from a since-superseded artefact")
    ap.add_argument("--manifest-path", help="Run manifest (enables fingerprint "
                                            "comparison; mtimes otherwise)")
    ap.add_argument("--namespace", action="append",
                    help="Restrict to namespace(s); default: all staged")
    ap.add_argument("--staged-base", default=None)
    ap.add_argument("--vocabulary", default=None,
                    help="Path to the toponyms DuckDB; checks which namespaces "
                         "were rewritten since it was built, instead of "
                         "checking the places index")
    args = ap.parse_args()

    if args.vocabulary:
        results = check_vocabulary(args.vocabulary,
                                   Path(args.staged_base) if args.staged_base
                                   else None)
        width = max((len(r["namespace"]) for r in results), default=9)
        for r in results:
            state = ("*** STALE ***" if r["stale"]
                     else "unknown" if r["unknown"] else "ok")
            print(f"{r['namespace']:{width}s}  {state:14s} [{r['basis']}] "
                  f"{r['detail']}")
        stale = [r["namespace"] for r in results if r["stale"]]
        print(f"\nvocabulary {args.vocabulary}: "
              f"stale sources: {', '.join(stale) if stale else 'none'}")
        sys.exit(1 if stale else 0)

    manifest = None
    if args.manifest_path:
        manifest = json.loads(Path(args.manifest_path).read_text())

    base = Path(args.staged_base or STAGED_BASE_DIR)
    if args.namespace:
        names = args.namespace
    elif manifest:
        names = sorted(manifest.get("namespaces", {}))
    else:
        names = sorted(p.name for p in base.iterdir()
                       if p.is_dir() and not p.name.startswith("_")
                       and p.name not in ("runs", "parallel-logs"))

    results = check_all(names, manifest, base)
    width = max((len(r["namespace"]) for r in results), default=9)
    for r in results:
        if r["stale"]:
            state = "*** STALE ***"
        elif r["unknown"]:
            state = "unknown"
        else:
            state = "ok"
        print(f"{r['namespace']:{width}s}  {state:14s} [{r['basis']}] "
              f"{r['detail']}")

    stale = [r["namespace"] for r in results if r["stale"]]
    print(f"\n{len(results)} namespaces checked; "
          f"stale: {', '.join(stale) if stale else 'none'}")
    sys.exit(1 if stale else 0)


if __name__ == "__main__":
    main()
