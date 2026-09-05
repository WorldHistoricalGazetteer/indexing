#!/usr/bin/env python
"""Canonical re-embed: recompute every toponym vector, write back only the wrong ones.

Why this exists
---------------
Until 5 September 2026 (`97a8b31`) two different tokenisers wrote into the
`toponyms` index. `update_es.py` used the canonical one; `backfill_embeddings.py`
used `hf/inference.py`'s, which fed raw codepoints to the same vocabulary — no
CJK/Kana romanisation, no Hangul→Jamo, no NFC, `' '` resolving to vocab id 12588,
and a script detector that counted digits. Both stamp `embedding_version` from
the same CLI argument, so **nothing in the index records which encoder wrote a
document**, and attribution had to be reconstructed by recomputing 4,000 vectors
by hand. This module is the fix for the documents and the fix for that: it
recomputes, and it leaves a ledger.

The question it asks is structural, not historical — *does this document's
stored vector match what the canonical tokeniser produces for its name?* — so it
needs no provenance field and cannot be fooled by a missing one.

It lives in `processing/` and not in `phonetics/inference/` for a reason that is
not filing: `phonetics/inference/__init__.py` imports `ToponymEncoder`, which
imports **torch**, and the export and apply phases run on **pitt**, which has no
torch and no conda env. A module in that package cannot be imported on the host
where two of its three phases must run.

Three phases, bridged by `/vast`, because prod ES is firewalled to **pitt**'s
localhost while the GPUs are on **CRC** and neither host can reach the other's
services:

    export   (pitt)      prod ES  → shard_NNNN.parquet   {toponym_id, name, lang, script, stored}
    compute  (CRC GPU)   shards   → diff_NNNN.parquet    {toponym_id, embedding} — DIFFERENCES ONLY
    apply    (pitt)      diffs    → bulk-update prod ES   + ledger.json

Everything is sharded and every shard is written atomically (temp file, then
rename) with a `.done` marker, so a re-run skips completed shards. That is not a
nicety: the compute runs on the **preempt** partition, where jobs are killed
mid-run by design, and `--requeue` is only safe because a half-written shard can
never appear at a final path.

Correctness gates — each has already caught something real
----------------------------------------------------------
1. **Positive control, and it aborts the run.** Names that tokenise identically
   under both encoders (single-word, non-CJK/Kana/Hangul, already NFC, not
   majority-non-alphabetic) MUST reproduce their stored vector at cosine
   ≥ 0.9996 — the int8 quantisation floor, measured at 0.99971 mean / 0.99963
   min over 574 such live documents. If they do not, the compute is using the
   wrong weights or the wrong tokeniser and every "difference" it reports is an
   artefact. The shard aborts and writes nothing.
2. **Quantise exactly as the index's writer does.** `np.round(e * 127.0)
   .astype(np.int8)` — `update_es.quantize_embeddings_to_bytes`, which has no
   clip. The gateway's `quantize_to_byte` clips; it cannot differ at
   max|component| 0.284, but this matches the writer, not the reader.
3. **Write only differences, and report the denominator.** "changed n of N
   examined", per stratum. A run that writes everything is both far more
   expensive and unfalsifiable.
4. **Verify the tokeniser you actually get** (not the one you imported).
   `SymphonymModel` is loaded from whichever `hf/inference.py` is on the path;
   this checks the canonical block in *that file* against
   `phonetics/tokenise.py` by hash. Check at the reader, not the writer.
5. **Throttle the ES write.** Prod search is live.

Usage
-----
    # 1. on pitt — reads prod ES, writes /vast
    python -m processing.reembed export \
        --es-host http://localhost:9201 --out-dir /vast/ishi/reembed/<run-id> --slices 64

    # 2. on a CRC GPU node, as a job array (see processing/reembed_canonical.sbatch)
    python -m processing.reembed compute \
        --in-dir /vast/ishi/reembed/<run-id> --shard-id $SLURM_ARRAY_TASK_ID --device cuda

    # 3. on pitt — writes prod ES (dry-run by default)
    python -m processing.reembed apply \
        --es-host http://localhost:9201 --in-dir /vast/ishi/reembed/<run-id> \
        --throttle 0.3 --execute
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

EMBEDDING_DIM = 128

# /vast, NOT /ix1. `/ix1` is a hard NFS mount: when it wedges, a read of the
# password blocks forever rather than failing, and it was wedged on the day this
# was written. `gateway/config.py:34` prefers the same /vast copy.
DEFAULT_ES_PASSWORD_FILE = "/vast/ishi/es/config/elastic.password"

#: The int8 quantisation floor. Two vectors that agree before quantisation
#: cannot disagree by more than this after it.
CONTROL_MIN_COSINE = 0.9996
#: Fraction of control rows that must clear it. Not 100%: a control document can
#: legitimately differ for reasons that have nothing to do with tokenisation (a
#: vector left behind by an older checkpoint, say). A wrong checkpoint or a wrong
#: tokeniser fails every one of them, not one in a thousand.
CONTROL_MIN_PASS_RATE = 0.99
#: Below this many control rows in a shard, the control is not evidence.
CONTROL_MIN_ROWS = 200

BEGIN_MARKER = "# --- BEGIN CANONICAL TOKENISER ---"
END_MARKER = "# --- END CANONICAL TOKENISER ---"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _es_client(es_host: str, password_file: str | None):
    from elasticsearch import Elasticsearch
    kwargs = {"request_timeout": 300}
    pf = Path(password_file) if password_file else None
    if pf and pf.exists():
        try:
            kwargs["basic_auth"] = ("elastic", pf.read_text().strip())
        except PermissionError:
            pass
    return Elasticsearch(es_host, **kwargs)


#: Refuse to start, or to continue, below this much free space on the output
#: volume. `/vast/ishi` is 1 TB SHARED WITH PRODUCTION ELASTICSEARCH, whose
#: flood-stage watermark is min(5%, 100GB) and therefore fires at ~51 GB free.
#: Crossing it does not fail this job — it puts every ES index into READ-ONLY,
#: which is a production outage caused by a background task. The floor sits well
#: above the watermark so the job dies first and prod does not notice.
#:
#: This run needs ~8 GB of export and less again of diffs, against ~187 GB of
#: usable headroom, so the guard is not expected to fire. It exists because a
#: preempted array is requeued, and a requeued task that does not skip its own
#: completed output writes more copies than anyone budgeted for.
DEFAULT_MIN_FREE_GB = 80.0


def check_free_space(path: Path, min_free_gb: float, context: str) -> float:
    """Free GB at `path`, aborting below the floor. One statvfs; call it often."""
    import shutil

    free_gb = shutil.disk_usage(path).free / (1024 ** 3)
    if free_gb < min_free_gb:
        raise SystemExit(
            f"ABORT ({context}): {free_gb:.1f} GB free on {path}, below the "
            f"{min_free_gb:.0f} GB floor. That volume is shared with production "
            f"Elasticsearch, whose flood-stage watermark puts every index into "
            f"READ-ONLY at ~51 GB free. Stopping this job is cheap; a prod outage "
            f"caused by a background task is not.")
    return free_gb


def shard_paths(base: Path, kind: str, shard_id: int) -> tuple[Path, Path, Path]:
    """``(final, temp, done)`` for one shard.

    Nothing ever writes to ``final``; a shard is written to ``temp`` and renamed,
    which is atomic within a filesystem. So a killed job leaves a temp file and
    no ``done`` marker, and the re-run redoes it — where a partially written
    final file would be silently adopted as complete.
    """
    final = base / f"{kind}_{shard_id:04d}.parquet"
    return final, final.with_suffix(".parquet.tmp"), final.with_suffix(".done")


def shard_is_complete(base: Path, kind: str, shard_id: int) -> bool:
    final, _, done = shard_paths(base, kind, shard_id)
    return done.exists() and final.exists()


def _finish_shard(final: Path, temp: Path, done: Path, meta: dict) -> None:
    os.replace(temp, final)
    done.write_text(json.dumps(meta, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# The candidate predicate and the positive control
# ---------------------------------------------------------------------------

ROMANISED_SCRIPTS = frozenset({"CJK", "HIRAGANA", "KATAKANA", "HANGUL"})


def is_candidate(name: str, script: str | None) -> bool:
    """Could the two tokenisers have disagreed about this name?

    True for the romanised/decomposed scripts (D1), for any name carrying a
    space (D2 — 58.41% of the index, not the 29.7% a gazetteer-name corpus
    suggests), and for anything not already in NFC (D1 again).

    Names outside this set are embedded anyway when ``--scope all`` is used: the
    predicate decides what MUST be checked, never what may be skipped, because a
    predicate that silently excludes a document is exactly how the defect being
    repaired was introduced.
    """
    if (script or "") in ROMANISED_SCRIPTS:
        return True
    if " " in name:
        return True
    return unicodedata.normalize("NFC", name) != name


def is_control(name: str, script: str | None) -> bool:
    """Is this a name both tokenisers must agree on, byte for byte?

    Single-word, not romanised or decomposed, already NFC — and **not majority
    non-alphabetic**, which is the D4 trap: 'S4630' and 'Q85423919' are
    single-word Latin names on which the two SCRIPT detectors disagree, so they
    are not controls even though every other test would call them one.
    """
    if not name or is_candidate(name, script):
        return False
    return sum(not c.isalpha() for c in name) / len(name) <= 0.5


def stratum_of(name: str, script: str | None) -> str:
    """The reporting bucket. Every examined row lands in exactly one."""
    if (script or "") in ROMANISED_SCRIPTS:
        return script
    if " " in name:
        return "multi-word"
    if unicodedata.normalize("NFC", name) != name:
        return "not-NFC"
    return "control"


# ---------------------------------------------------------------------------
# The pin — one tokeniser for the whole run, named by hash
# ---------------------------------------------------------------------------

PIN_FILE = "pin.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def cmd_pin(args) -> None:
    """Record the exact tokeniser this run must use, and abort if it moves.

    The compute phase runs on **preempt**, so its tasks are killed and requeued
    at arbitrary times, and it reads its tokeniser from the working tree of a
    repository three sessions share. On the day this was written HEAD named
    three different tokenisers between 13:39 and 15:10, one of which rewrote
    `hf/inference.py` from 469 lines to 659. A shard that starts before a commit
    and a shard requeued after it would then embed under different code — and
    the array would complete, report success, and produce per-shard counts that
    are each internally consistent. Nothing in the totals could show it.

    So the run pins the canonical block's sha256 once, here, and every shard
    checks the code it actually loaded against that. A mid-flight commit becomes
    an abort instead of a silent divergence.
    """
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    repo = _repo_root()
    # `str.isalpha()` is a property of the INTERPRETER's Unicode tables, not of
    # our code, and the canonical tokeniser's script detection filters on it:
    # 515 codepoints are alphabetic in Unicode 14.0.0 and not in 13.0.0 (all
    # Unicode 14 additions — Cypro-Minoan, Tangsa, Vithkuqi, Latin Ext-G,
    # Arabic Extended-B, Toto, Ethiopic Ext-B, Old Uyghur). A name containing
    # one of them gets a different script_id under the two, so a shard computed
    # under the wrong interpreter produces "differences" that are artefacts of
    # the interpreter and writes them back.
    #
    # Measured 5 Sep 2026: CRC conda `whg` is 3.11.13 / 14.0.0 — the index
    # writer — while pitt (system 3.9.25 and the reembed venv) is 13.0.0. The
    # export and apply phases do not tokenise, so 13.0.0 is harmless there; the
    # COMPUTE must be 14.0.0. Because `pin` is normally run on pitt, the
    # required version is stated explicitly rather than sampled from whichever
    # host happened to run it — sampling would pin 13.0.0 and abort every shard.
    required_unicode = args.unicodedata_version or unicodedata.unidata_version
    pin = {
        "tokeniser_block_sha256": _canonical_block_hash(repo / "phonetics" / "tokenise.py"),
        "hf_inference_block_sha256": _canonical_block_hash(repo / "hf" / "inference.py"),
        "checkpoint": _checkpoint_hash(Path(args.model_dir) if args.model_dir else repo / "hf"),
        "git_commit": _git_commit(out_dir),
        "unicodedata_version": required_unicode,
        "pinned_by_python": platform.python_version(),
        "pinned_by_unicodedata": unicodedata.unidata_version,
        "pinned_at": datetime.now(timezone.utc).isoformat(),
    }
    if pin["tokeniser_block_sha256"] != pin["hf_inference_block_sha256"]:
        raise SystemExit(
            "ABORT: phonetics/tokenise.py and hf/inference.py carry different "
            "canonical blocks. Re-vendor before pinning — pinning a tree that is "
            "already inconsistent pins the inconsistency.")
    if pin["git_commit"] in ("unknown", "staged-tree-no-git"):
        raise SystemExit(
            f"ABORT: cannot determine which commit this code came from. A pin "
            f"whose provenance field says 'unknown' records nothing — it is the "
            f"one field the whole run is answerable by. Write "
            f"{out_dir / 'staged_commit.json'} with the staged sha (stage does "
            f"this), or pin from a real checkout.")
    path = out_dir / PIN_FILE
    if path.exists():
        existing = json.loads(path.read_text())
        if existing["tokeniser_block_sha256"] != pin["tokeniser_block_sha256"]:
            raise SystemExit(
                f"ABORT: {path} already pins tokeniser "
                f"{existing['tokeniser_block_sha256'][:12]} (git "
                f"{existing['git_commit'][:8]}), but the tree now has "
                f"{pin['tokeniser_block_sha256'][:12]}. Shards already computed "
                f"used the pinned one. Start a NEW run directory rather than "
                f"mixing two tokenisers into one run.")
        print(f"[pin] unchanged: {path}")
        return
    path.write_text(json.dumps(pin, indent=2))
    print(f"[pin] tokeniser {pin['tokeniser_block_sha256'][:12]} · "
          f"git {pin['git_commit'][:8]} · {pin['checkpoint'][:24]}... → {path}")
    print(f"[pin] compute must run under unicodedata {required_unicode} "
          f"(this host has {unicodedata.unidata_version})")
    if required_unicode != unicodedata.unidata_version:
        print(f"[pin] that is deliberate: the tokeniser's script detection reads "
              f"str.isalpha(), so the COMPUTE has to match the interpreter that "
              f"wrote the index, not the one that pinned it.")


def cmd_stage(args) -> None:
    """Copy the code the run will use OUT of the shared working tree.

    Detecting that the tree moved is second best; not being able to see it move
    is better. `git archive <full sha>` into the run directory gives every task
    a private, immutable copy, so neither a commit by another session nor an
    editor save can reach a job in flight. A symbolic ref will not do — HEAD
    named three different tokenisers in 91 minutes on the day this was written —
    so the sha is resolved once here and recorded.
    """
    run_dir = Path(args.out_dir)
    code_dir = run_dir / "code"
    if code_dir.exists() and any(code_dir.iterdir()) and not args.force:
        raise SystemExit(
            f"ABORT: {code_dir} already holds a staged tree. Shards may already "
            f"have run against it. Use a new run directory, or --force if you are "
            f"certain nothing has been computed yet.")
    repo = _repo_root()
    sha = subprocess.run(["git", "rev-parse", args.commit], cwd=repo,
                         capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                           capture_output=True, text=True, check=True).stdout.strip()
    if dirty and not args.allow_dirty:
        raise SystemExit(
            f"ABORT: the working tree has uncommitted changes, so {args.commit} is "
            f"not what you are testing:\n{dirty}\nCommit them (explicit paths — "
            f"this tree is shared) or pass --allow-dirty to stage the committed "
            f"state deliberately.")
    code_dir.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(["git", "archive", sha], cwd=repo, stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(code_dir)], stdin=archive.stdout, check=True)
    archive.stdout.close()
    if archive.wait() != 0:
        raise SystemExit(f"ABORT: git archive {sha} failed")

    # `hf/vocab` and the weights are gitignored, so the archive has neither.
    # Symlink them in from the live checkout: the vocabulary and checkpoint are
    # inputs to the run, pinned by hash rather than by copy.
    for name in ("vocab", "model.safetensors", "final_model.pt"):
        src, dst = repo / "hf" / name, code_dir / "hf" / name
        if src.exists() and not dst.exists():
            dst.symlink_to(src.resolve())
    # The archive holds the COMMITTED tree, so a module that is still
    # uncommitted is simply absent from it — and the job would then run whatever
    # copy the PYTHONPATH happened to find, which is the shared working tree,
    # which is the thing staging exists to escape. Caught here rather than as an
    # ImportError on a GPU node an hour later.
    for required in ("processing/reembed.py", "phonetics/tokenise.py",
                     "hf/inference.py"):
        if not (code_dir / required).exists():
            raise SystemExit(
                f"ABORT: {required} is not in the staged tree — it is not committed "
                f"at {sha[:12]}. Commit it (explicit paths; this tree is shared) and "
                f"stage again. Staging a tree without the code it runs would send "
                f"every task back to the working tree.")

    (run_dir / "staged_commit.json").write_text(json.dumps({
        "commit": sha, "requested": args.commit, "dirty_at_stage": bool(dirty),
        "staged_at": datetime.now(timezone.utc).isoformat(),
        "code_dir": str(code_dir),
    }, indent=2))
    print(f"[stage] {sha[:12]} → {code_dir}")
    print(f"[stage] run every task with `cd {code_dir}` — NOT from {repo}")


def load_pin(run_dir: Path) -> dict:
    path = run_dir / PIN_FILE
    if not path.exists():
        raise SystemExit(
            f"ABORT: no {PIN_FILE} in {run_dir}. Run `reembed pin --out-dir {run_dir}` "
            f"first. Without it a preempted shard can be requeued onto different "
            f"code than the shards beside it, and nothing downstream could tell.")
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Phase: export  (PITT — reads prod ES)
# ---------------------------------------------------------------------------

def cmd_export(args) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    es = _es_client(args.es_host, args.es_password_file)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    free_gb = check_free_space(out_dir, args.min_free_gb, "export start")
    print(f"[export] {free_gb:.1f} GB free on {out_dir} "
          f"(floor {args.min_free_gb:.0f} GB)")
    total = es.count(index=args.index)["count"]
    print(f"[export] {args.index}: {total:,} toponyms; {args.slices} slices "
          f"(~{total // max(args.slices, 1):,} each)")

    schema = pa.schema([
        ("toponym_id", pa.string()), ("name", pa.string()),
        ("lang", pa.string()), ("script", pa.string()),
        ("stored", pa.list_(pa.int8(), EMBEDDING_DIM)),
    ])

    pit = es.open_point_in_time(index=args.index, keep_alive=args.keep_alive)
    pit_id = pit["id"]
    written_total = skipped_total = 0
    try:
        for slice_id in range(args.slices):
            if shard_is_complete(out_dir, "shard", slice_id):
                print(f"[export]   shard {slice_id:04d}: already complete, skipping")
                continue
            check_free_space(out_dir, args.min_free_gb, f"before shard {slice_id:04d}")
            final, temp, done = shard_paths(out_dir, "shard", slice_id)
            t0 = time.time()
            rows, written = [], 0
            skipped_no_name = skipped_bad_vector = 0
            writer = pq.ParquetWriter(temp, schema, compression="zstd")
            try:
                search_after = None
                while True:
                    body = {
                        "size": args.batch_size,
                        "pit": {"id": pit_id, "keep_alive": args.keep_alive},
                        "sort": [{"_shard_doc": "asc"}],
                        "_source": ["name", "lang", "script", "embedding"],
                        "track_total_hits": False,
                    }
                    if args.slices > 1:
                        body["slice"] = {"id": slice_id, "max": args.slices}
                    if search_after is not None:
                        body["search_after"] = search_after
                    resp = es.search(body=body)
                    hits = resp["hits"]["hits"]
                    if not hits:
                        break
                    pit_id = resp.get("pit_id", pit_id)
                    search_after = hits[-1]["sort"]
                    for hit in hits:
                        src = hit.get("_source", {})
                        name = src.get("name") or ""
                        emb = src.get("embedding")
                        # Counted, never silently dropped: `examined` downstream
                        # is only a denominator if what fell out of it is known.
                        # Measured 5 Sep 2026, both are 0 index-wide — which is
                        # why the identity below has to be asserted rather than
                        # assumed to stay true.
                        if not name.strip():
                            skipped_no_name += 1
                            continue
                        if not emb or len(emb) != EMBEDDING_DIM:
                            skipped_bad_vector += 1
                            continue
                        rows.append((hit["_id"], name, src.get("lang") or "und",
                                     src.get("script") or "", emb))
                    if len(rows) >= args.flush_rows:
                        writer.write_table(pa.Table.from_arrays(
                            [pa.array(c) for c in zip(*rows)], schema=schema))
                        written += len(rows)
                        rows = []
                    if args.throttle:
                        time.sleep(args.throttle)
                    if args.limit and written + len(rows) >= args.limit:
                        break
                if rows:
                    writer.write_table(pa.Table.from_arrays(
                        [pa.array(c) for c in zip(*rows)], schema=schema))
                    written += len(rows)
            finally:
                writer.close()
            _finish_shard(final, temp, done, {
                "slice": slice_id, "of": args.slices, "rows": written,
                "skipped_no_name": skipped_no_name,
                "skipped_bad_vector": skipped_bad_vector,
                "index": args.index, "seconds": round(time.time() - t0, 1),
                "written_at": datetime.now(timezone.utc).isoformat(),
            })
            written_total += written
            skipped_total += skipped_no_name + skipped_bad_vector
            print(f"[export]   shard {slice_id:04d}: {written:,} rows "
                  f"({time.time() - t0:.0f}s)", flush=True)
    finally:
        try:
            es.close_point_in_time(id=pit_id)
        except Exception as exc:  # a leaked PIT expires on its own
            print(f"[export] warning: could not close PIT ({exc})")

    manifest = {
        "index": args.index, "slices": args.slices, "rows": written_total,
        "skipped": skipped_total, "index_total": total,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "export_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[export] done: {written_total:,} exported + {skipped_total:,} skipped "
          f"= {written_total + skipped_total:,} of {total:,} in the index")
    if args.limit:
        print(f"[export] --limit {args.limit:,} was set, so this export is "
              f"DELIBERATELY partial and is a smoke test, not a run. Its manifest "
              f"must not be used as a denominator.")
        manifest["partial_limit"] = args.limit
        (out_dir / "export_manifest.json").write_text(json.dumps(manifest, indent=2))
    elif written_total + skipped_total != total:
        print(f"[export] ⚠ {total - written_total - skipped_total:,} documents were "
              f"neither exported nor skipped. The PIT may have expired mid-scroll, or "
              f"the index changed under the run. `rows` is the denominator every "
              f"downstream count is checked against, so this must be explained "
              f"before compute starts.")


# ---------------------------------------------------------------------------
# Phase: compute  (CRC GPU — no ES)
# ---------------------------------------------------------------------------

#: sha256 of the empty string. A hash pipeline that produced NOTHING still
#: produces this, and two broken producers agree with each other perfectly — so
#: it is rejected by name, as its own failure, rather than being allowed to
#: read as a version mismatch. The two need different fixes at 3am.
SHA256_OF_NOTHING = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _canonical_block_hash(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise SystemExit(
            f"ABORT: {path} is empty. Its hash would be {SHA256_OF_NOTHING[:12]} — "
            f"the hash of nothing — which two failed producers agree on perfectly. "
            f"The file is missing or truncated; this is a producer failure, not a "
            f"version mismatch.")
    block = text[text.index(BEGIN_MARKER):text.index(END_MARKER) + len(END_MARKER)]
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
    if digest == SHA256_OF_NOTHING:
        raise SystemExit(f"ABORT: the canonical block in {path} hashed to the hash "
                         f"of empty input. The producer failed; do not compare it.")
    return digest


def verify_tokeniser(pin: dict) -> str:
    """Gate 4, and it runs BEFORE the model is loaded or a GPU is touched.

    Reads the `hf/inference.py` that this process would actually import — the
    one beside the running module, which under a staged run is the staged copy
    and not the shared working tree — and checks its canonical block against
    both `phonetics/tokenise.py` and the run's pin.

    Three distinct failures, three distinct messages, because they need
    different fixes: no block at all (pre-fix code), a block that disagrees with
    its own tree (a half-finished re-vendor), and a block that disagrees with
    the pin (the tree moved under a run in flight).

    Cheap by design: it is a file hash, so a task that has been requeued onto
    the wrong code dies in milliseconds instead of after loading 33 MB of
    weights and embedding a million names.
    """
    repo = _repo_root()
    hf_inference = repo / "hf" / "inference.py"
    try:
        got = _canonical_block_hash(hf_inference)
    except ValueError:
        raise SystemExit(
            f"ABORT: {hf_inference} carries no canonical tokeniser block. That is "
            f"the pre-97a8b31 encoder — the one that WROTE the defective vectors. "
            f"Computing with it would faithfully reproduce them.")
    want_tree = _canonical_block_hash(repo / "phonetics" / "tokenise.py")
    if got != want_tree:
        raise SystemExit(
            f"ABORT: {hf_inference} ({got[:12]}) and phonetics/tokenise.py "
            f"({want_tree[:12]}) carry different canonical blocks. Re-vendor.")
    if got != pin["tokeniser_block_sha256"]:
        raise SystemExit(
            f"ABORT: this task would run tokeniser {got[:12]} but the run is "
            f"pinned to {pin['tokeniser_block_sha256'][:12]} (git "
            f"{pin['git_commit'][:8]}). The code moved under a run in flight — a "
            f"preempt requeue onto a changed tree. Shards already computed used "
            f"the pinned one; mixing the two would UNDER-count, because a shard "
            f"running post-fix code compares canonical against canonical, finds "
            f"nothing, and is indistinguishable from a clean shard.")
    want_unicode = pin.get("unicodedata_version")
    if want_unicode and unicodedata.unidata_version != want_unicode:
        raise SystemExit(
            f"ABORT: this task runs Python {platform.python_version()} with "
            f"unicodedata {unicodedata.unidata_version}, but the run is pinned to "
            f"{want_unicode}. The tokeniser's script detection filters on "
            f"str.isalpha(), which is the INTERPRETER's Unicode table: 515 "
            f"codepoints are alphabetic in 14.0.0 and not in 13.0.0, so this task "
            f"would assign a different script_id from the one the index holds and "
            f"write the resulting artefacts back as repairs. Activate the conda "
            f"`whg` env (3.11.13 / 14.0.0) — not the reembed venv, which exists "
            f"for the export and apply phases and does not tokenise.")
    print(f"[compute] tokeniser verified at the reader: {hf_inference} "
          f"block sha256 {got[:12]} == pin · unicodedata "
          f"{unicodedata.unidata_version} == pin")
    return got


def _load_model(device: str, model_dir: str | None):
    repo = _repo_root()
    hf_dir = repo / "hf"
    if str(hf_dir) not in sys.path:
        sys.path.insert(0, str(hf_dir))
    from inference import SymphonymModel
    md = Path(model_dir) if model_dir else hf_dir
    print(f"[compute] loading SymphonymModel from {md} on {device} ...")
    return SymphonymModel(model_dir=md, device=device)


def _checkpoint_hash(model_dir: Path) -> str:
    for name in ("model.safetensors", "final_model.pt"):
        path = model_dir / name
        if path.exists():
            digest = hashlib.sha256()
            with path.open("rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
            return f"{name}:{digest.hexdigest()}"
    return "unknown"


def quantize(embeddings):
    """Exactly `update_es.quantize_embeddings_to_bytes` — the INDEX's writer.

    No clip. The gateway's `quantize_to_byte` clips to [-128, 127] and cannot
    differ at the observed max |component| of 0.284, but this run's output has to
    be byte-comparable with what is already stored, so it copies the writer.
    """
    import numpy as np
    return np.round(np.asarray(embeddings, dtype=np.float32) * 127.0).astype(np.int8)


def check_positive_control(cosines, shard_id: int = -1) -> dict:
    """Gate 1. Raises rather than returning a verdict nobody has to read.

    The rows counted here are names both encoders tokenise identically, so their
    recomputed vector MUST reproduce the stored one to within int8 quantisation.
    If it does not, the checkpoint or the vocabulary is wrong and every
    "difference" the shard found is an artefact of that, not a defect in the
    index — so the run must stop before it writes, not report a number.

    Two ways to fail, and they mean different things: too FEW control rows means
    the shard cannot support the claim at all (silent, and the more dangerous of
    the two — a check with no subjects passes); too LOW a pass rate means the
    claim is refuted.
    """
    import numpy as np

    n = len(cosines)
    if n < CONTROL_MIN_ROWS:
        raise SystemExit(
            f"ABORT: only {n} control rows in shard {shard_id:04d} (need "
            f"{CONTROL_MIN_ROWS}). A control that thin is not evidence that the "
            f"weights are right, and every difference found would be unverified.")
    arr = np.asarray(cosines, dtype=np.float64)
    pass_rate = float((arr >= CONTROL_MIN_COSINE).mean())
    if pass_rate < CONTROL_MIN_PASS_RATE:
        raise SystemExit(
            f"ABORT: positive control failed — {pass_rate:.2%} of {n:,} rows "
            f"reproduce their stored vector, below {CONTROL_MIN_PASS_RATE:.0%}. "
            f"These names tokenise identically under both encoders, so they can "
            f"only disagree if the WEIGHTS or the vocabulary are wrong. Every "
            f"'difference' this shard found would be an artefact. Nothing written.")
    return {"rows": n, "pass_rate": pass_rate,
            "mean_cos": float(arr.mean()), "min_cos": float(arr.min())}


def cmd_compute(args) -> None:
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    in_dir = Path(args.in_dir)
    if not shard_is_complete(in_dir, "shard", args.shard_id):
        raise SystemExit(f"ABORT: export shard {args.shard_id:04d} is not complete "
                         f"in {in_dir} — nothing to compute from.")
    if shard_is_complete(in_dir, "diff", args.shard_id) and not args.force:
        print(f"[compute] diff shard {args.shard_id:04d} already complete — skipping "
              f"(this is what makes --requeue safe)")
        return

    pin = load_pin(in_dir)
    block_hash = verify_tokeniser(pin)      # before the GPU, before the weights
    free_gb = check_free_space(in_dir, args.min_free_gb, "compute start")
    print(f"[compute] {free_gb:.1f} GB free on {in_dir} "
          f"(floor {args.min_free_gb:.0f} GB)")
    model = _load_model(args.device, args.model_dir)
    repo = _repo_root()
    model_dir = Path(args.model_dir) if args.model_dir else (repo / "hf")

    src = pq.read_table(shard_paths(in_dir, "shard", args.shard_id)[0])
    names = src.column("name").to_pylist()
    langs = src.column("lang").to_pylist()
    scripts = src.column("script").to_pylist()
    ids = src.column("toponym_id").to_pylist()
    stored = np.asarray(src.column("stored").to_pylist(), dtype=np.int8)
    print(f"[compute] shard {args.shard_id:04d}: {len(ids):,} rows")

    keep = [i for i in range(len(ids))
            if args.scope == "all" or is_candidate(names[i], scripts[i])
            or is_control(names[i], scripts[i])]
    print(f"[compute]   embedding {len(keep):,} of {len(ids):,} (scope={args.scope})")

    diffs, examined, changed, control_cos = [], {}, {}, []
    # Broken out because the non-candidates are a TEST, not padding: a document
    # that tokenises identically under both encoders cannot change, so any
    # non-zero count here refutes the candidate predicate itself rather than
    # reporting a repair. Expected exactly 0.
    changed_candidate = changed_non_candidate = 0
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    for start in range(0, len(keep), args.batch_size):
        idx = keep[start:start + args.batch_size]
        embs = model.batch_embed([(names[i], langs[i]) for i in idx])
        quant = quantize(embs)
        for row, i in enumerate(idx):
            stratum = stratum_of(names[i], scripts[i])
            examined[stratum] = examined.get(stratum, 0) + 1
            if is_control(names[i], scripts[i]):
                a = quant[row].astype(np.float32)
                b = stored[i].astype(np.float32)
                denom = float(np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
                control_cos.append(float(np.dot(a, b) / denom))
            if not np.array_equal(quant[row], stored[i]):
                changed[stratum] = changed.get(stratum, 0) + 1
                if is_candidate(names[i], scripts[i]):
                    changed_candidate += 1
                else:
                    changed_non_candidate += 1
                diffs.append((ids[i], quant[row].tolist()))
        if start and start % (args.batch_size * 50) == 0:
            print(f"[compute]   {start:,}/{len(keep):,} "
                  f"({start / (time.time() - t0):.0f}/s)", flush=True)

    # --- Gate 1: the positive control, BEFORE anything is written -----------
    control = check_positive_control(control_cos, args.shard_id)
    print(f"[compute]   positive control: {control['pass_rate']:.4%} of "
          f"{control['rows']:,} rows at cos >= {CONTROL_MIN_COSINE} "
          f"(mean {control['mean_cos']:.5f}, min {control['min_cos']:.5f})")

    check_free_space(in_dir, args.min_free_gb, f"before writing shard {args.shard_id:04d}")
    final, temp, done = shard_paths(in_dir, "diff", args.shard_id)
    schema = pa.schema([("toponym_id", pa.string()),
                        ("embedding", pa.list_(pa.int8(), EMBEDDING_DIM))])
    table = pa.Table.from_arrays(
        [pa.array([d[0] for d in diffs], type=pa.string()),
         pa.array([d[1] for d in diffs], type=pa.list_(pa.int8(), EMBEDDING_DIM))],
        schema=schema)
    pq.write_table(table, temp, compression="zstd")
    meta = {
        # Flat scalars first: a verifier should not have to sum a dict to learn
        # whether a shard ran, and these are the fields that cannot be
        # reconstructed after the fact.
        "shard_id": args.shard_id,
        "status": "complete",
        "examined_count": len(keep),
        "changed_count": len(diffs),
        "changed_candidate": changed_candidate,
        "changed_non_candidate": changed_non_candidate,
        "tokeniser_sha256": block_hash,
        "unicodedata_version": unicodedata.unidata_version,
        "python_version": platform.python_version(),
        # SLURM_RESTART_COUNT is how many times Slurm requeued THIS task. On
        # preempt a non-zero value is routine, not a fault — but without it a
        # shard that ran twice and was recorded once is indistinguishable from
        # one that ran once, and a double-run inflates the denominator while
        # leaving the changed count right, so the run reads MORE complete than
        # it is.
        "attempt": int(os.environ.get("SLURM_RESTART_COUNT", "0")),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID", ""),
        "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID", ""),
        "hostname": os.uname().nodename,
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "output_path": str(final),
        "shard": args.shard_id, "scope": args.scope,
        "rows_in_shard": len(ids), "embedded": len(keep),
        "changed_total": len(diffs),
        "examined_by_stratum": examined, "changed_by_stratum": changed,
        "control": control,
        "tokeniser_block_sha256": block_hash,
        "pinned_tokeniser_sha256": pin["tokeniser_block_sha256"],
        "checkpoint": _checkpoint_hash(model_dir),
        "pinned_checkpoint": pin["checkpoint"],
        "git_commit": _git_commit(in_dir),
        "pinned_git_commit": pin["git_commit"],
        "seconds": round(time.time() - t0, 1),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    _finish_shard(final, temp, done, meta)
    for stratum in sorted(examined):
        print(f"[compute]   {stratum:<12} changed {changed.get(stratum, 0):>7,} "
              f"of {examined[stratum]:>9,} examined")
    print(f"[compute] shard {args.shard_id:04d} done: {len(diffs):,} differences "
          f"of {len(keep):,} embedded → {final}")


def _git_commit(run_dir: Path | None = None) -> str:
    """The commit this code came from — including when there is no repository.

    A staged tree is an extracted archive with no `.git`, so `git rev-parse`
    there reports the CRC checkout's HEAD if one happens to be above it, or
    nothing at all. Either is worse than useless in a provenance record: the
    first is confidently wrong. `stage` writes the resolved sha into
    `staged_commit.json` beside the run, and that file is authoritative whenever
    it exists — the commit is a property of the archive, not of wherever the
    archive was unpacked.
    """
    if run_dir is not None:
        staged = Path(run_dir) / "staged_commit.json"
        if staged.exists():
            try:
                commit = json.loads(staged.read_text()).get("commit")
                if commit:
                    return commit
            except (ValueError, OSError):
                pass
    try:
        root = _repo_root()
        if not (root / ".git").exists():
            # An extracted archive. Saying so beats reporting an unrelated HEAD.
            return "staged-tree-no-git"
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True, timeout=10,
                              check=True).stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Phase: apply  (PITT — writes prod ES)
# ---------------------------------------------------------------------------

def cmd_apply(args) -> None:
    import pyarrow.parquet as pq
    from elasticsearch import helpers as es_helpers

    in_dir = Path(args.in_dir)
    export_manifest = in_dir / "export_manifest.json"
    if not export_manifest.exists():
        raise SystemExit(f"ABORT: {export_manifest} missing — the export did not "
                         f"finish, so the shard set is unknown and 'all shards "
                         f"applied' cannot be asserted.")
    manifest = json.loads(export_manifest.read_text())
    if manifest.get("partial_limit"):
        raise SystemExit(
            f"ABORT: this export was run with --limit {manifest['partial_limit']:,} "
            f"and covers only {manifest['rows']:,} documents. It is a smoke test. "
            f"Applying it would repair a sample and report a run.")
    slices = manifest["slices"]

    missing = [i for i in range(slices) if not shard_is_complete(in_dir, "diff", i)]
    if missing and not args.allow_partial:
        raise SystemExit(
            f"ABORT: {len(missing)} of {slices} compute shards are incomplete "
            f"({missing[:8]}{'...' if len(missing) > 8 else ''}). Applying now would "
            f"repair part of the index and report success. Re-run the array, or pass "
            f"--allow-partial deliberately.")

    pin = load_pin(in_dir)
    metas, total = [], 0
    for i in range(slices):
        if not shard_is_complete(in_dir, "diff", i):
            continue
        meta = json.loads(shard_paths(in_dir, "diff", i)[2].read_text())
        # A shard computed under a different tokeniser than its neighbours is
        # not a smaller result, it is a wrong one — and it is invisible in the
        # totals, because each shard's own counts are internally consistent.
        if meta.get("tokeniser_block_sha256") != pin["tokeniser_block_sha256"]:
            raise SystemExit(
                f"ABORT: shard {i:04d} was computed with tokeniser "
                f"{str(meta.get('tokeniser_block_sha256'))[:12]}, but the run is "
                f"pinned to {pin['tokeniser_block_sha256'][:12]}. Recompute that "
                f"shard (--force) rather than applying a mixed run.")
        if pin.get("unicodedata_version") and \
                meta.get("unicodedata_version") != pin["unicodedata_version"]:
            raise SystemExit(
                f"ABORT: shard {i:04d} was computed under unicodedata "
                f"{meta.get('unicodedata_version')} against the pinned "
                f"{pin['unicodedata_version']}. Mixed Unicode tables across an "
                f"array are the requeue hazard with a different variable, and "
                f"equally invisible in the totals. Recompute that shard.")
        if meta.get("checkpoint") != pin["checkpoint"]:
            raise SystemExit(
                f"ABORT: shard {i:04d} used checkpoint {str(meta.get('checkpoint'))[:24]} "
                f"against the pinned {pin['checkpoint'][:24]}. Same reasoning.")
        metas.append(meta)
        total += meta["changed_total"]

    examined, changed = {}, {}
    for meta in metas:
        for k, v in meta["examined_by_stratum"].items():
            examined[k] = examined.get(k, 0) + v
        for k, v in meta["changed_by_stratum"].items():
            changed[k] = changed.get(k, 0) + v

    # "Nothing to fix" and "ran the wrong code" have identical signatures in a
    # run that writes only differences, so an empty shard is named rather than
    # summed away. The pin makes the wrong-code cause impossible; this makes the
    # remaining causes visible.
    examined_total = sum(m["examined_count"] for m in metas)
    expected = manifest["rows"]
    if len(metas) == slices and examined_total != expected:
        raise SystemExit(
            f"ABORT: shards examined {examined_total:,} documents but the export "
            f"wrote {expected:,}. Every shard is present, so this is not "
            f"truncation — a shard has been counted twice (check `attempt` in the "
            f"shard metas) or a shard read a file it did not write.")
    non_candidate_changed = sum(m.get("changed_non_candidate", 0) for m in metas)
    if non_candidate_changed:
        print(f"[apply] ⚠ {non_candidate_changed:,} NON-CANDIDATE documents changed. "
              f"Expected exactly 0: those names tokenise identically under both "
              f"encoders, so they cannot change. Either the candidate predicate is "
              f"wrong — and so is the 46,483,973 figure derived from it — or this "
              f"run touched documents it had no business touching. Those are "
              f"different diagnoses; do not apply until you know which.")

    empty = [m["shard"] for m in metas if m["changed_total"] == 0]
    print(f"[apply] {len(metas)} of {slices} shards; {total:,} documents differ")
    if empty:
        print(f"[apply] {len(empty)} shard(s) found NO differences at all: {empty[:16]}"
              f"{'...' if len(empty) > 16 else ''} — expected only if a shard is "
              f"genuinely all-clean; investigate before treating as a result.")
    for stratum in sorted(examined):
        n, d = changed.get(stratum, 0), examined[stratum]
        print(f"[apply]   {stratum:<12} {n:>7,} of {d:>9,} examined ({n / d:.3%})"
              if d else f"[apply]   {stratum:<12} 0 of 0")

    if not args.execute:
        print(f"[apply] DRY-RUN: would update {total:,} docs in {args.index} "
              f"(embedding + indexed_at; embedding_version left at "
              f"{manifest.get('embedding_version', 7)}), chunk={args.batch_size}, "
              f"throttle={args.throttle}s. No writes. Pass --execute to write.")
        return

    es = _es_client(args.es_host, args.es_password_file)
    now = datetime.now(timezone.utc).isoformat()
    es_opt = es.options(request_timeout=300)
    ok = errs = 0
    ledger_ids = []
    t0 = time.time()
    for i in range(slices):
        if not shard_is_complete(in_dir, "diff", i):
            continue
        table = pq.read_table(shard_paths(in_dir, "diff", i)[0])
        rows = list(zip(table.column("toponym_id").to_pylist(),
                        table.column("embedding").to_pylist()))
        for start in range(0, len(rows), args.batch_size):
            chunk = rows[start:start + args.batch_size]
            actions = [{"_op_type": "update", "_index": args.index, "_id": tid,
                        "doc": {"embedding": vec, "indexed_at": now}}
                       for tid, vec in chunk]
            c_ok, c_errs = es_helpers.bulk(es_opt, actions, raise_on_error=False,
                                           max_retries=3, initial_backoff=2)
            ok += c_ok
            errs += len(c_errs) if isinstance(c_errs, list) else c_errs
            ledger_ids.extend(tid for tid, _ in chunk)
            if args.throttle:
                time.sleep(args.throttle)
        print(f"[apply]   shard {i:04d}: {ok:,} ok / {errs:,} err", flush=True)

    es.indices.refresh(index=args.index)
    ledger = {
        "run_dir": str(in_dir),
        "index": args.index,
        "applied_at": now,
        "documents_updated": ok,
        "errors": errs,
        "examined_total": examined_total,
        "export_rows": expected,
        "changed_candidate": sum(m.get("changed_candidate", 0) for m in metas),
        "changed_non_candidate": non_candidate_changed,
        "examined_by_stratum": examined,
        "changed_by_stratum": changed,
        "shards": metas,
        "pin": pin,
        "git_commit": _git_commit(in_dir),
        "toponym_ids": ledger_ids,
    }
    ledger_path = in_dir / "ledger.json"
    tmp = ledger_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
    os.replace(tmp, ledger_path)
    print(f"[apply] done: ok={ok:,} errors={errs:,} ({time.time() - t0:.0f}s)")
    print(f"[apply] ledger → {ledger_path} ({len(ledger_ids):,} toponym_ids, "
          f"checkpoint + git commit + per-shard control results). "
          f"'What did this run touch?' is now answerable by reading a file.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    ps = sub.add_parser("stage", help="copy the code out of the shared tree (run FIRST)")
    ps.add_argument("--out-dir", required=True)
    ps.add_argument("--commit", default="HEAD",
                    help="resolved to a full sha and recorded; HEAD is fine at "
                         "stage time because it is resolved once, here")
    ps.add_argument("--allow-dirty", action="store_true")
    ps.add_argument("--force", action="store_true")
    ps.set_defaults(func=cmd_stage)

    pp = sub.add_parser("pin", help="pin the tokeniser + checkpoint for this run")
    pp.add_argument("--out-dir", required=True)
    pp.add_argument("--model-dir")
    pp.add_argument("--unicodedata-version", metavar="X.Y.Z",
                    help="the Unicode table the COMPUTE must run under (14.0.0 "
                         "for the conda whg env, which wrote the index). Defaults "
                         "to this host's, which is wrong whenever pin runs on "
                         "pitt and compute runs on CRC.")
    pp.set_defaults(func=cmd_pin)

    pe = sub.add_parser("export", help="prod ES → sharded parquet (run on pitt)")
    pe.add_argument("--es-host", required=True)
    pe.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    pe.add_argument("--index", default="toponyms")
    pe.add_argument("--out-dir", required=True)
    pe.add_argument("--slices", type=int, default=64)
    pe.add_argument("--batch-size", type=int, default=5000)
    pe.add_argument("--flush-rows", type=int, default=200_000)
    pe.add_argument("--keep-alive", default="30m")
    pe.add_argument("--throttle", type=float, default=0.0,
                    help="seconds between search pages, to pace live prod ES")
    pe.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB,
                    help="abort below this much free space; /vast is shared with "
                         "production ES, which goes READ-ONLY at ~51 GB free")
    pe.add_argument("--limit", type=int, default=0, help="stop after N rows per slice (smoke test)")
    pe.set_defaults(func=cmd_export)

    pc = sub.add_parser("compute", help="one shard → differences (run on a CRC GPU)")
    pc.add_argument("--in-dir", required=True)
    pc.add_argument("--shard-id", type=int, required=True)
    pc.add_argument("--device", default="cuda")
    pc.add_argument("--model-dir")
    pc.add_argument("--batch-size", type=int, default=1024)
    pc.add_argument("--scope", choices=("all", "candidates"), default="all",
                    help="'all' embeds every row (the candidate predicate then only "
                         "labels strata); 'candidates' embeds the candidate set plus "
                         "the controls")
    pc.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB,
                    help="abort below this much free space; /vast is shared with "
                         "production ES, which goes READ-ONLY at ~51 GB free")
    pc.add_argument("--force", action="store_true", help="recompute a completed shard")
    pc.set_defaults(func=cmd_compute)

    pa_ = sub.add_parser("apply", help="differences → bulk-update prod ES (run on pitt)")
    pa_.add_argument("--es-host", required=True)
    pa_.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    pa_.add_argument("--index", default="toponyms")
    pa_.add_argument("--in-dir", required=True)
    pa_.add_argument("--batch-size", type=int, default=2000)
    pa_.add_argument("--throttle", type=float, default=0.3,
                     help="seconds between bulk chunks (prod search is live)")
    pa_.add_argument("--allow-partial", action="store_true",
                     help="apply even though some compute shards are missing")
    pa_.add_argument("--execute", action="store_true", help="actually write (default: dry-run)")
    pa_.set_defaults(func=cmd_apply)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
