#!/usr/bin/env python3
"""
Decide what needs computing, and write a manifest the merge can check against.

INCREMENTAL BY CONSTRUCTION
---------------------------
A toponym needs work when it is absent from the store, or when its name_sha
differs from what the store recorded, or when a previous attempt failed
transiently. It does NOT need work when a previous run established that no
route exists -- that is a terminal answer, and retrying ~20M of them on every
pass is the difference between a top-up and a full re-run.

`--retry-status` reopens terminal answers deliberately, which is what you want
after adding an Epitran mode or lifting a quarantine.

SHARDING BY MODE, NOT BY ROW COUNT
----------------------------------
Instantiating an Epitran mode costs far more than transliterating a name, so
every row of a shard shares one mode and each worker loads exactly one.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from phonetics.ipa import routes as R
from phonetics.ipa import store as S

logger = logging.getLogger(__name__)

MAX_ROWS_PER_SHARD = 400_000


STAGED_ROOT = "/vast/ishi/staged"


def stale_inventory_namespaces(inventory_db: str,
                               staged_root: str = STAGED_ROOT) -> List[dict]:
    """Namespaces whose staged extract is NEWER than the inventory DuckDB.

    WHY THIS EXISTS. New toponyms do not enter the inventory when a namespace's
    extract finishes -- they enter when `rebuild_toponyms_index` re-runs its
    extract-to-DuckDB step. Run the planner in between and it compares the new
    corpus against an inventory that never saw it, finds nothing to do, and
    returns ZERO WORK. That is indistinguishable from "already up to date",
    which is Fault 12's exact shape: a stage reports success because its input
    never changed.

    Observed live on 6 Sep 2026: /vast/ishi/staged/tgn/extract/places.jsonl at
    08:38 against an inventory built 4 Aug, while `tgn-extract-nofetch` was
    still running.

    ⚠ Compares mtime to mtime, both from the same filesystem clock. It does NOT
    compare a run id to an mtime -- run ids are UTC and hosts are EDT, and that
    4-hour trap has already misattributed one regression in this project.
    """
    inv = Path(inventory_db)
    if not inv.exists():
        return []
    inv_mtime = inv.stat().st_mtime
    out = []
    root = Path(staged_root)
    if not root.is_dir():
        return []
    for ns_dir in sorted(root.iterdir()):
        if not ns_dir.is_dir():
            continue
        for rel in ("extract/places.jsonl", "final/places.parquet"):
            f = ns_dir / rel
            if f.exists() and f.stat().st_mtime > inv_mtime:
                out.append({
                    "namespace": ns_dir.name, "artefact": rel,
                    "artefact_mtime": f.stat().st_mtime,
                    "inventory_mtime": inv_mtime,
                    "newer_by_hours": round(
                        (f.stat().st_mtime - inv_mtime) / 3600.0, 2),
                })
                break
    return out



def inventory_provenance(inventory_db: str,
                         staged_root: str = STAGED_ROOT) -> List[dict]:
    """What the REBUILD says it read, checked against what is there now.

    Strictly stronger than comparing mtimes. `rebuild_toponyms_index` writes a
    `.vocabulary_sources.json` sidecar recording the exact staged artefact each
    namespace contributed (path, size, mtime), and
    `processing.index_freshness.check_vocabulary` compares that against the
    current artefact.

    The case mtime ordering CANNOT catch: a rebuild run against a STALE
    `final/`. That produces an inventory newer than everything, so an
    mtime-ordering guard falls silent -- while the inventory carries the old
    corpus. The fingerprint catches it, because the artefact it recorded is not
    the artefact present now.

    Returns the subset of results that should stop a run: stale, or unknown.
    ⚠ "unknown" is NOT "fine". The live 4 Aug inventory has no sidecar at all,
    so its provenance is unverifiable and must be treated as suspect rather
    than assumed good.
    """
    try:
        from processing.index_freshness import check_vocabulary
    except Exception as exc:      # pragma: no cover - import environment
        return [{"namespace": "*", "stale": False, "unknown": True,
                 "basis": "import-failed", "detail": str(exc)[:160]}]
    try:
        results = check_vocabulary(inventory_db, Path(staged_root))
    except Exception as exc:
        return [{"namespace": "*", "stale": False, "unknown": True,
                 "basis": "check-failed", "detail": str(exc)[:160]}]
    return [r for r in results if r.get("stale") or r.get("unknown")]


def inventory_lang_witness(inventory_db: str, namespace: str = "tgn") -> dict:
    """A SECOND witness, independent of the producer's own bookkeeping.

    The sidecar is written by the same process that builds the DB -- it is the
    producer's self-report, and a correct re-stamp satisfies it while the
    content is wrong. So check the content directly for a property only the new
    corpus has.

    `extract_namespace`, the places index's default_pipeline, DISCARDS any
    toponym whose language tag is empty, and 59.9% of tgn's carried one (Getty
    publishes untagged terms). The authority fix emits `lang or "und"`, which
    RE-KEYS them: `Dorkecestre@` and `Dorkecestre@und` are different
    toponym_ids. So an inventory built after the fix has a large `und`
    population for tgn and few empty tags; one built before has the reverse.

    Reports both counts in one query, so a zero always arrives with the
    non-zero beside it and cannot be mistaken for a broken predicate.
    """
    import duckdb
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{inventory_db}' AS inv (READ_ONLY)")
        row = con.execute("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE t.lang = 'und') AS und,
                   count(*) FILTER (WHERE t.lang IS NULL OR trim(t.lang) = '') AS empty
            FROM inv.toponyms t
            WHERE EXISTS (SELECT 1 FROM inv.toponym_namespaces n
                          WHERE n.toponym_id = t.toponym_id
                            AND n.namespace = ?)
        """, [namespace]).fetchone()
    except Exception as exc:
        return {"namespace": namespace, "error": str(exc)[:200]}
    finally:
        con.close()
    total, und, empty = row
    return {"namespace": namespace, "total": total, "und": und,
            "empty_lang": empty,
            "und_pct": round(100.0 * und / total, 3) if total else None,
            "empty_pct": round(100.0 * empty / total, 3) if total else None,
            "fix_appears_applied": bool(und > 0 and und > empty)}


def build_plan(inventory_db: str, store_db: str, out_dir: Path, run_id: str,
               retry_statuses: Optional[List[str]] = None,
               allow_quarantined: bool = False,
               max_rows_per_shard: int = MAX_ROWS_PER_SHARD,
               work_dir_override: Optional[Path] = None) -> Dict:
    import duckdb

    out_dir.mkdir(parents=True, exist_ok=True)
    table = R.RouteTable(allow_quarantined=allow_quarantined)

    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='60GB'")
    con.execute(f"ATTACH '{inventory_db}' AS inv (READ_ONLY)")
    con.execute(f"ATTACH '{store_db}' AS st")
    con.execute(S.DDL.replace("CREATE TABLE IF NOT EXISTS ",
                              "CREATE TABLE IF NOT EXISTS st."))

    retry = set(retry_statuses or [])
    # A status is "settled" unless we were asked to reopen it.
    settled = (S.TERMINAL_STATUSES | {S.STATUS_OK}) - retry
    settled_sql = ",".join(f"'{s}'" for s in sorted(settled)) or "''"

    # Everything needing work, in one pass. name_sha is recomputed in SQL so a
    # changed name invalidates the stored row without trusting the id format.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE work AS
        SELECT i.toponym_id, i.name, coalesce(i.lang,'') AS lang, i.script,
               substr(sha256(i.name), 1, 16) AS name_sha
        FROM inv.toponyms i
        LEFT JOIN st.ipa s USING (toponym_id)
        WHERE i.name IS NOT NULL AND i.name <> ''
          AND (
                s.toponym_id IS NULL
             OR s.name_sha IS DISTINCT FROM substr(sha256(i.name), 1, 16)
             OR s.status NOT IN ({settled_sql})
          )
    """)
    total_work = con.execute("SELECT count(*) FROM work").fetchone()[0]

    cells = con.execute(
        "SELECT lang, script, count(*) n FROM work GROUP BY lang, script "
        "ORDER BY n DESC").fetchall()

    shards: List[Dict] = []
    terminal_rows: Dict[str, int] = {}
    for lang, script, n in cells:
        route, status = table.resolve(lang, script)
        # Shard ids become FILENAMES, and 431 of the corpus's lang values are
        # not language codes -- '1510/', ' Acland St'. A '/' turns into a
        # directory separator and the write fails mid-array.
        ltok = R.shard_token(lang)
        stok = R.shard_token(script)
        if route is None:
            terminal_rows[status] = terminal_rows.get(status, 0) + n
            shards.append({
                "shard_id": f"terminal-{status}-{ltok}-{stok}",
                "lang": lang, "script": script, "backend": None, "mode": None,
                "status": status, "rows": n, "terminal": True,
            })
            continue
        n_parts = max(1, -(-n // max_rows_per_shard))
        for p in range(n_parts):
            shards.append({
                "shard_id": f"{route.backend}-{route.mode}-{ltok}-{stok}-{p:03d}",
                "lang": lang, "script": script,
                "backend": route.backend, "mode": route.mode,
                "status": "pending", "rows": n, "terminal": False,
                "part": p, "n_parts": n_parts,
            })

    stale = stale_inventory_namespaces(inventory_db)
    provenance = inventory_provenance(inventory_db)
    lang_witness = inventory_lang_witness(inventory_db)

    plan = {
        "run_id": run_id,
        "stale_inventory_namespaces": stale,
        "inventory_provenance_issues": provenance,
        "inventory_lang_witness": lang_witness,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inventory_db": inventory_db,
        "store_db": store_db,
        "rows_needing_work": total_work,
        "route_table": table.summary(),
        "retry_statuses": sorted(retry),
        "terminal_rows": terminal_rows,
        "computable_rows": total_work - sum(terminal_rows.values()),
        "shards": shards,
        "n_shards": len(shards),
        "n_compute_shards": sum(1 for s in shards if not s["terminal"]),
    }
    (out_dir / f"plan-{run_id}.json").write_text(json.dumps(plan, indent=2))

    # The work list itself, partitioned so a worker reads only its own shard.
    if work_dir_override is not None:
        # Re-planning against an already-exported work set: the shard NAMES may
        # need to change (a bad lang made an unwritable filename) while the
        # rows themselves are unchanged, and re-exporting 72.7M rows to fix a
        # naming bug is pure waste.
        work_dir = work_dir_override
        if not work_dir.exists():
            raise SystemExit(f"--reuse-work given but {work_dir} does not exist")
    else:
        work_dir = out_dir / f"work-{run_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"""
            COPY (SELECT * FROM work) TO '{work_dir}'
            (FORMAT PARQUET, PARTITION_BY (lang, script), OVERWRITE_OR_IGNORE 1)
        """)
    plan["work_dir"] = str(work_dir)
    (out_dir / f"plan-{run_id}.json").write_text(json.dumps(plan, indent=2))
    return plan


def main():
    ap = argparse.ArgumentParser(description="Plan an incremental IPA run")
    ap.add_argument("--inventory-db", required=True)
    ap.add_argument("--store-db", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--retry-status", action="append", default=[],
                    help="reopen a settled status, e.g. no_route after adding a mode")
    ap.add_argument("--allow-quarantined", action="store_true")
    ap.add_argument("--max-rows-per-shard", type=int, default=MAX_ROWS_PER_SHARD)
    ap.add_argument("--reuse-work", help="path to an existing work-<run> dir; skips re-exporting the row set")
    ap.add_argument("--allow-no-work", action="store_true",
                    help="accept a plan with zero rows to do; without it a "
                         "zero is REFUSED, because it cannot be told apart "
                         "from a stale inventory")
    ap.add_argument("--ignore-stale-inventory", action="store_true",
                    help="proceed even though a staged extract is newer "
                         "than the inventory DuckDB")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = build_plan(a.inventory_db, a.store_db, Path(a.out_dir), a.run_id,
                   a.retry_status, a.allow_quarantined, a.max_rows_per_shard,
                   Path(a.reuse_work) if a.reuse_work else None)
    terminal_total = sum(p["terminal_rows"].values())
    print(f"rows needing work : {p['rows_needing_work']:,}")
    print(f"  computable      : {p['computable_rows']:,}")
    print(f"  terminal        : {terminal_total:,} {p['terminal_rows']}")

    # A run that writes many rows and produces little IPA is the DESIGNED
    # behaviour, not a failure, and it needs saying here rather than in a
    # document nobody has open. A terminal row is written precisely so the
    # next run can tell "tried, nothing can be done" from "never looked" --
    # without it every re-run retries ~20M hopeless rows forever and no
    # coverage figure has a denominator.
    if terminal_total and terminal_total >= p["computable_rows"]:
        pct = 100.0 * terminal_total / max(p["rows_needing_work"], 1)
        print(f"\n  NOTE: {pct:.1f}% of this plan is terminal — it will write "
              f"{terminal_total:,} rows and produce no IPA for them.")
        print("  That is the intended outcome, not a failure: the row exists to "
              "record")
        print("  WHY there is no IPA, so the next run does not retry it and the "
              "coverage")
        print("  figure keeps its denominator. Judge the run by "
              "computable-vs-produced,")
        print("  not by rows-vs-IPA.")
    print(f"shards            : {p['n_shards']:,} "
          f"({p['n_compute_shards']:,} to compute)")
    print(f"-> {Path(a.out_dir)/f'plan-{a.run_id}.json'}")

    # Two independent witnesses, then the zero check. The provenance record
    # is the producer's self-report; the lang witness is our own observation of
    # the content. A correct re-stamp satisfies the first and not the second.
    prov = p.get("inventory_provenance_issues") or []
    if prov:
        print("\n\u26a0 INVENTORY PROVENANCE NOT CONFIRMED:")
        for d in prov[:10]:
            print(f"   {d['namespace']:<10} basis={d.get('basis'):<14} "
                  f"stale={d.get('stale')} unknown={d.get('unknown')} "
                  f"{d.get('detail','')[:60]}")
        if not a.ignore_stale_inventory:
            raise SystemExit(
                "refusing: the inventory's provenance is stale or unrecorded, "
                "so it cannot be shown to cover the current staged corpus. An "
                "inventory rebuilt from a STALE final/ looks newer than "
                "everything and would pass an mtime check. Pass "
                "--ignore-stale-inventory to override.")

    w = p.get("inventory_lang_witness") or {}
    if w.get("total"):
        print(f"\n  tgn lang witness: total={w['total']:,} "
              f"und={w['und']:,} ({w['und_pct']}%) "
              f"empty={w['empty_lang']:,} ({w['empty_pct']}%) "
              f"fix_applied={w['fix_appears_applied']}")

    # A zero here is ambiguous and must not pass silently.
    stale = p.get("stale_inventory_namespaces") or []
    if stale:
        print("\n\u26a0 INVENTORY IS BEHIND THE STAGED CORPUS:")
        for d in stale[:10]:
            print(f"   {d['namespace']:<10} {d['artefact']:<24} "
                  f"newer by {d['newer_by_hours']}h")
        if not a.ignore_stale_inventory:
            raise SystemExit(
                "refusing to plan against an inventory older than the staged "
                "corpus: rebuild_toponyms_index must re-run its "
                "extract-to-DuckDB step first, or this plan silently omits "
                "every new toponym. Pass --ignore-stale-inventory to override.")

    if p["rows_needing_work"] == 0 and not a.allow_no_work:
        raise SystemExit(
            "plan has ZERO rows to do. That is indistinguishable from a stale "
            "inventory, so it is refused rather than reported as success. "
            "Confirm the inventory actually contains what you expect, then "
            "pass --allow-no-work.")


if __name__ == "__main__":
    main()
