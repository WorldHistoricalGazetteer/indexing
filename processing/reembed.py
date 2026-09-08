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
import random
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# Stdlib-only at module scope on purpose (`processing.device` imports torch
# lazily): export and apply run on pitt, which has neither torch nor conda.
from processing.device import configure_cpu_threads, describe_device, resolve_device

EMBEDDING_DIM = 128

# /vast, NOT /ix1. `/ix1` is a hard NFS mount: when it wedges, a read of the
# password blocks forever rather than failing, and it was wedged on the day this
# was written. `gateway/config.py:34` prefers the same /vast copy.
DEFAULT_ES_PASSWORD_FILE = "/vast/ishi/es/config/elastic.password"

#: A component-wise difference of 1 is quantisation noise, not a difference.
#:
#: Both vectors are int8 quantisations of some float32 vector. The index was
#: written on different hardware from the one recomputing it, so fp32
#: accumulation differs in the last bits and a component sitting near a rounding
#: boundary lands on either side of it. That is not a defect and rewriting it
#: would be a large pointless write to a live index.
#:
#: The value is not a chosen threshold — it is read off an empty gap. Over
#: shard 0000 (283,609 documents, 1,364 differing by any amount):
#:
#:     max|delta| == 1   n=1,163   cosine >= 0.999876
#:     max|delta| == 2   n=0                              <- nothing here
#:     max|delta| >= 3   n=  201   cosine <= 0.996868
#:
#: Two independent statistics separate the same two populations with nothing in
#: between: no document differs by exactly 2, and the cosine band
#: (0.996868, 0.999876) is empty. A structural discriminator, per
#: `~/.claude/memory/structural_beats_historical_discriminator.md`, rather than a
#: number someone picked.
MATERIAL_DELTA = 2

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

#: 🛑 THE TOKENISER IS FOUR FILES, AND ONLY TWO OF THEM CARRY A CANONICAL BLOCK.
#: `verify_tokeniser` hashes `phonetics/tokenise.py` against `hf/inference.py`,
#: so a partial application that updates those two and misses these two passes
#: EVERY existing check. Found by indexing-04 by asking which partial the gate
#: cannot see, rather than which partial is most likely — the answer was the one
#: outside the gate, not the one inside it.
#:
#: These two get only fragments of the D-A/D5 patch (`char_vocab.py` casefold +
#: NFKC; `script_detection.py` the fold-before-counting line), which is exactly
#: why nobody thought of them as tokeniser files. `script_detection.py` matters
#: most: D5 changes script ASSIGNMENT, and assignment is consumed well outside
#: the tokeniser — rebuild_toponyms_index, index_namespace, inference/search,
#: ipa/routes — so it moves what `is_script_mismatch` accepts.
#:
#: ⚠ WHOLE-FILE hash, deliberately, and it will fire on a comment-only edit.
#: That false positive costs one re-pin; the false NEGATIVE costs 73M names
#: embedded under a tokenisation half of the code does not implement, with
#: nothing raised. There is no principled subset to hash — the "behaviourally
#: relevant part" of these two files is not delimited by anything, which is the
#: reason they never got a canonical block in the first place.
AUX_TOKENISER_FILES = (
    "phonetics/vocab/char_vocab.py",
    "phonetics/utils/script_detection.py",
)


def _aux_tokeniser_hashes(repo: Path) -> dict:
    """Whole-file sha256 of the tokeniser files that carry no canonical block."""
    out = {}
    for rel in AUX_TOKENISER_FILES:
        path = repo / rel
        if not path.exists():
            raise SystemExit(
                f"ABORT: {path} is missing. It is one of the four files the "
                f"tokeniser is implemented across; its absence is not an empty "
                f"contribution, it is a tree that cannot tokenise.")
        data = path.read_bytes()
        if not data.strip():
            raise SystemExit(
                f"ABORT: {path} is empty; its hash would be the hash of nothing "
                f"({SHA256_OF_NOTHING[:12]}), which two failed producers agree "
                f"on perfectly.")
        out[rel] = hashlib.sha256(data).hexdigest()
    return out



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
    # The temp name is UNIQUE PER PROCESS, not per shard. Two tasks working the
    # same shard at once — a requeued task racing the original, or a manual
    # resubmission overlapping a running array — would otherwise interleave
    # writes into one temp path and then both rename it. The result is a file
    # that is present, non-empty, carries a .done marker and is corrupt, which
    # is worse than either task simply failing.
    temp = base / f".{kind}_{shard_id:04d}.{os.getpid()}.{os.environ.get('SLURM_JOB_ID', 'local')}.tmp"
    return final, temp, final.with_suffix(".done")


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

#: D7. Scripts the PRE-FIX detector recognised and the canonical one does not.
#: (D5 is the FB00-FB17 precedence defect and D6 the interpreter's Unicode
#: version — both separate, both in developer/plan-symphonym-v8.md.)
#:
#: `hf/inference.py`'s old table carried GURMUKHI (U+0A00-U+0A7F);
#: `phonetics/utils/script_detection.py` — the table the index was written
#: with, and the one now vendored — has no Gurmukhi entry at all, so Punjabi
#: names score OTHER. GUJARATI is in the table and GURMUKHI is not, so a
#: re-implementer reads the absence as a bug and "fixes" it — and a differential
#: corpus generated from the implementer's OWN table can never catch that,
#: because it would not generate the cases.
#: A document embedded by the backfill therefore carries a
#: GURMUKHI script id that the canonical tokeniser can never reproduce, while
#: nothing about the NAME marks it out: single word, already NFC, no digits, and
#: both detectors agree on OTHER today because only one of them ever disagreed.
#:
#: Found by `changed_non_candidate` going from 0 at 38% of the corpus to 12 at
#: 100% — twelve Punjabi country names (ਪਾਕਿਸਤਾਨ, ਨੇਪਾਲ, ਬੰਗਲਾਦੇਸ਼ …) at cosine
#: 0.80-0.98. They are the clearest direct evidence in the run that
#: backfill-written documents exist, and they were invisible to every other
#: check because the predicate had no reason to look at them.
LEGACY_ONLY_SCRIPT_RANGES = ((0x0A00, 0x0A7F),)   # Gurmukhi


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
    if unicodedata.normalize("NFC", name) != name:
        return True
    # D4. The pre-fix gateway's script detector counted EVERY character, while
    # the canonical one counts only `str.isalpha()`, so any digit or punctuation
    # mark shifts the balance between them and can change the script id — which
    # changes the vector without changing a single character token.
    #
    # This clause was missing, and `--scope all` found it: shard 0000 turned up
    # 'SO-10731' and 'SZ-1555' as documents that genuinely differ (cosine 0.938
    # and 0.943, max|delta| 11 and 12) while the predicate called them
    # non-candidates. Deliberately over-inclusive — it fires on any hyphen or
    # apostrophe — because the cost of including a document that turns out to
    # match is one comparison, and the cost of excluding one that does not is a
    # defect nobody looks for again.
    #
    # Combining MARKS are excluded: a Thai vowel sign is not alphabetic, but it
    # sits inside the Thai block, so the legacy detector counted it as THAI just
    # as the canonical one reaches THAI from the letters. The two agree and
    # 'กรุงเทพ' is not a D4 name. Digits and punctuation are the shifting class,
    # because they fall outside every script range and the legacy detector
    # counted them as OTHER.
    if any(not c.isalpha() and c != " "
           and unicodedata.category(c)[0] != "M" for c in name):
        return True
    # D7, above.
    return any(any(lo <= ord(c) <= hi for lo, hi in LEGACY_ONLY_SCRIPT_RANGES)
               for c in name)


def is_control(name: str, script: str | None) -> bool:
    """Is this a name both tokenisers must agree on, byte for byte?

    Single-word, not romanised or decomposed, already NFC — and **not majority
    non-alphabetic**, which is the D4 trap: 'S4630' and 'Q85423919' are
    single-word Latin names on which the two SCRIPT detectors disagree, so they
    are not controls even though every other test would call them one.
    """
    # 🛑 DERIVED, not re-derived. This used to compute the answer a second time
    # — `not is_candidate(...)` plus the majority-non-alphabetic guard — while
    # `stratum_of` computed it from its own cascade, and the two agreed only by
    # inspection. They did not agree: 9 of 22,000 real toponyms (0.04%) were
    # `control` by stratum and False here, every one Burmese, Gujarati or
    # Bengali, where combining marks, viramas and asat characters push the
    # non-alphabetic share past 0.5.
    #
    # ⚠ The equivalence TEST passed throughout, because the corpus it ran over
    # contained no name of that shape. An equivalence asserted over inputs that
    # cannot disagree certifies nothing, and that is the one failure a pinning
    # test exists to prevent. Found by indexing-04 against real rows.
    return stratum_of(name, script) in CONTROL_STRATA


#: The control FAMILY. `stratum_of(...) in CONTROL_STRATA` is exactly
#: `is_control(...)` — the split is a refinement of one definition, not a second
#: one, so the census and the gate can never disagree about who the controls are.
CONTROL_STRATA = ("control", "control-case", "control-nfkc")


def stratum_of(name: str, script: str | None) -> str:
    """The reporting bucket. Every examined row lands in exactly one."""
    if (script or "") in ROMANISED_SCRIPTS:
        return script
    if " " in name:
        return "multi-word"
    if unicodedata.normalize("NFC", name) != name:
        return "not-NFC"
    # The majority-non-alphabetic guard, which `is_control` used to own
    # privately. It lives here now because this is the single classifier;
    # `punctuated` is where it belongs, since that is what the guard is aimed at.
    if name and sum(not c.isalpha() for c in name) / len(name) > 0.5:
        return "punctuated"
    if is_candidate(name, script):
        # D4: single-word, already NFC, non-romanised, and still a candidate —
        # so it carries a digit or a punctuation mark. Split out because the
        # bucket it used to fall into is called "control", and a stratum named
        # control must contain only documents that CANNOT change. The first
        # partial census reported 184 changes in "control", which read as a
        # contradiction and was in fact this mislabelling.
        return "punctuated"
    # 🛑 AND THE SAME MISLABELLING AGAIN, one change later. `is_candidate` tests
    # NFC and nothing tests NFKC, so under D-A/D5 a name that MUST change still
    # landed in a bucket called `control` — 391 of 6,720 sampled control rows
    # (5.82%), all Thai, all U+0E33 SARA AM, which NFKC decomposes. The census
    # would have reported changes in `control` exactly as it did once before.
    #
    # ⚠ Split HERE and not in `is_candidate`, which is what the finding proposed.
    # Adding an NFKC clause there would make these names candidates, so they
    # would leave `is_control` and Gate 1b would lose the only witness it has
    # that D5 landed — under D5-alone it would have zero must-change rows and
    # abort for want of subjects. The label was wrong; the POPULATION was right.
    #
    # Independent of the tokeniser regime by construction, so `control` means
    # "cannot change under either D-A or D5" whichever of them is in force, and
    # the census does not need to know which.
    if unicodedata.normalize("NFKC", name) != name:
        return "control-nfkc"
    if name.casefold() != name:
        return "control-case"
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
        "aux_tokeniser_sha256": _aux_tokeniser_hashes(repo),
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

    marker = json.dumps({
        "commit": sha, "requested": args.commit, "dirty_at_stage": bool(dirty),
        "staged_at": datetime.now(timezone.utc).isoformat(),
        "code_dir": str(code_dir),
    }, indent=2)
    # Written into the code tree AND beside the run. The copy inside the code
    # tree is the one `_git_commit` trusts, because it travels with the code it
    # names and cannot go stale while the code moves.
    (code_dir / "staged_commit.json").write_text(marker)
    (run_dir / "staged_commit.json").write_text(marker)
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

#: Seeded so a read-back sample is reproducible from the ledger — an
#: unreproducible spot check cannot be re-run against the same documents.
_rng = random.Random(20260908)

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
    # THE CONVENTION, and there is exactly one: sha256 over the block INCLUDING
    # both marker lines and EXCLUDING every line beginning "# CANONICAL-BLOCK".
    # The block carries its own sha256 in those lines (`0cf0a88`) so a repo that
    # vendors it — whg3 and London_Customs_Accounts both do — can identify its
    # copy without this repo on disk; a stamp cannot include itself, so the
    # stamp lines come out before hashing. Until this line existed, reembed
    # hashed WITH them and the embedded stamp excluded them: two conventions for
    # hashing one block, each internally consistent, disagreeing with nothing to
    # notice. That is the fault the stamp was added to prevent, so reembed
    # follows the stamp's rule rather than keeping a private one.
    block = "\n".join(line for line in block.split("\n")
                      if not line.startswith("# CANONICAL-BLOCK"))
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
    # The two files with no canonical block. Checked at the READER, from the
    # repo this process would actually import, for the same reason the block is:
    # a task requeued onto a changed tree must die here, not after the weights.
    want_aux = pin.get("aux_tokeniser_sha256")
    if want_aux is None:
        raise SystemExit(
            "ABORT: this run's pin carries no `aux_tokeniser_sha256`, so it "
            "predates the check that two of the four tokeniser files were never "
            "gated at all. Re-pin. Treating the absent key as 'nothing to check' "
            "is precisely the fault this gate exists to catch — an absent input "
            "read as an empty one.")
    got_aux = _aux_tokeniser_hashes(repo)
    for rel in AUX_TOKENISER_FILES:
        if got_aux[rel] != want_aux.get(rel):
            raise SystemExit(
                f"ABORT: {rel} hashes {got_aux[rel][:12]} but the run is pinned "
                f"to {str(want_aux.get(rel))[:12]}. The tokeniser is implemented "
                f"across four files and this is one of the two carrying no "
                f"canonical block, so a partial update reaching only the gated "
                f"pair would otherwise have run clean. Shards already computed "
                f"used the pinned tree; mixing tokenisations inside one KNN "
                f"space is worse than applying none, because every cosine stays "
                f"a plausible number.")

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
          f"{unicodedata.unidata_version} == pin · "
          f"{len(AUX_TOKENISER_FILES)} un-blocked files == pin")
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


#: Under a case-folding tokeniser, a control that REPRODUCES its stored vector
#: is evidence the fold did not land. At most this fraction may reproduce.
CONTROL_MAX_UNCHANGED_RATE = 0.01

#: ⚠ `--max-error-rate` is RELATIVE, so at 65M documents 1% licenses 652,935
#: absolute failures. It trips fast on an early burst (small denominator) and
#: never on a steady drip — the shape that produces a half-updated corpus. Any
#: real failure mode produces far more than this, so it costs nothing and closes
#: the drip.
MAX_ABSOLUTE_ERRORS = 5000

#: n=40 detects a 1% failure rate with 33.1% probability. 3,000 detects 0.1% at
#: 95% — still trivial against 65M writes, which is the only scale that matters
#: when choosing it.
READBACK_SAMPLE = 3000

#: 🛑 D-A AND D5 DIFFER IN BLAST RADIUS BY 556x, so a uniform sample cannot
#: witness D5. Measured over 10,000 live toponyms: casefold changes 88.99% of
#: them, NFKC-compatibility-only changes 0.16% (~117,567 corpus-wide). A uniform
#: 3,000-row read-back therefore contains ~4.8 D5 documents, and 187,500 uniform
#: rows would be needed for 300. And if D5 silently failed to apply, the COMBINED
#: change rate moves 88.99% -> 88.83% — inside the noise of any aggregate gate.
#:
#: So D5 gets its own denominator and its own draw, selected by construction
#: rather than by luck. Compute names them (it is the only phase holding the
#: names); apply reads them back. Sizing and measurements from indexing-04.
#: 🛑 KEYED ON COMPATIBILITY CLASS, NOT ON ROWS. The first version took the
#: first 8 compatibility-only rows a shard yielded — and document order is
#: script-clumped, so on many shards those 8 are all Thai. Measured by
#: indexing-04 over the export's own slicing (PIT + `_shard_doc`): slice 1's
#: first compatibility-only rows are THAI:7, while a corpus-wide RANDOM draw of
#: the same population is CYRILLIC 4, LATIN 4, THAI 4, KATAKANA 2, CJK 1,
#: GREEK 1. Head-of-shard is clumped; random is not.
#:
#: ⚠ Only 4 of Unicode's 17 compatibility classes fire in this corpus and they
#: are DIFFERENT MECHANISMS — Thai U+0E33 grows the string, `<wide>` folds
#: fullwidth punctuation, `<compat>` turns № into "No", `<super>` lifts a digit.
#: An all-Thai read-back validates one mechanism and reports coverage of D5:
#: a sample that cannot fail for the classes it never draws.
#:
#: Keying on class also turns the empty-sample flag into something sharper —
#: "no `<wide>` rows in any shard" is a finding, where "8 rows, all Thai" reads
#: as success. Filling the quota is not at risk: the export slices by `_id`
#: HASH (no `field` in the slice body), which is uniform with respect to
#: content, so each shard holds thousands of eligible rows.
D5_SAMPLE_PER_CLASS = 2


def is_compatibility_only(name: str) -> bool:
    """D5's population: NFC leaves it alone, NFKC does not."""
    return (unicodedata.normalize("NFC", name) == name
            and unicodedata.normalize("NFKC", name) != name)


def compatibility_classes(name: str) -> set:
    """The Unicode compatibility tags present, e.g. `{"<wide>", "<compat>"}`.

    The mechanism, not the script: two names can both be `control-nfkc` and
    change for entirely unrelated reasons, and a sample spanning scripts is not
    the same as one spanning mechanisms.
    """
    out = set()
    for ch in name:
        dec = unicodedata.decomposition(ch)
        if dec.startswith("<"):
            out.add(dec.split()[0])
    return out


def _failed_ids(c_errs) -> set:
    """The ids ES did NOT write, from whatever shape `bulk` returned.

    `raise_on_error=False` yields a list of per-item error dicts; some client
    versions return a bare count instead, and a count cannot name its ids — so
    that case is reported rather than silently treated as "none failed".
    """
    if not c_errs:
        return set()
    if not isinstance(c_errs, list):
        raise SystemExit(
            f"ABORT: the ES client reported {c_errs} bulk failures as a COUNT "
            f"rather than a list, so the failing ids cannot be named. Recording "
            f"them as applied is what produces a half-updated corpus; stopping "
            f"instead. Pin a client version whose `bulk` returns error details.")
    out = set()
    for e in c_errs:
        if isinstance(e, dict):
            for _op, body in e.items():
                if isinstance(body, dict) and body.get("_id"):
                    out.add(body["_id"])
    return out


def tokeniser_folds_case() -> bool:
    """Does the tokeniser this process would import apply D-A's fold?

    Probed, not configured. A flag would be one more thing that can disagree
    with the code, and this run already has four files' worth of that.

    🛑 BOTH REGIMES, because they are separable in code even though the plan
    argues they must land together. The first version probed casefolding alone,
    so a tokeniser adding NFKC WITHOUT casefold — which is D5 by itself —
    answered False, every control fell into `stable`, and Gate 1b went inert
    exactly when a fold-only change shipped. A gate that disarms itself on one
    of the two changes it exists to witness is worse than no gate, because the
    run reports a healthy control either way. Found by indexing-04.
    """
    return any(tokeniser_folds())


def tokeniser_folds() -> tuple:
    """`(folds_case, folds_compat)` — the two regimes, reported SEPARATELY.

    🛑 OR-ing them reaches the right verdict with the wrong diagnosis. Under D5
    alone, 88.50% of must-change rows reproduce and Gate 1b aborts saying "the
    fold is not working", which sends the operator to the checkpoint and the
    weights when the true cause is that NFKC landed and casefold did not. It
    aborts on every shard, so that is a whole debugging session spent in the
    wrong file. Measured by indexing-04:

        D-A + D5 (intended)        0/3,401   0.00%   PASS
        D5 alone (NFKC only)   3,010/3,401  88.50%   ABORTS, wrong reason given
        D-A alone (case only)    391/3,401  11.50%   ABORTS, wrong reason given
    """
    # ⚠ The compatibility probe is a FULLWIDTH letter, not the ﬁ ligature.
    # `str.casefold()` performs FULL case folding, which already decomposes
    # U+FB01 to "fi" — so a ligature probe answers True under casefold alone and
    # cannot separate the two regimes at all. U+FF34 casefolds to the fullwidth
    # lowercase U+FF54 and only NFKC maps it to ASCII "T", so it moves under
    # exactly one of them. Caught by the test rather than by reading.
    #
    # 🛑 That the ligature moves under casefold ALONE is §40.3's interaction in
    # one line: NFKC is not needed to shift `ﬁ`, so D-A relocates D5's motivating
    # example whether or not anyone intends it.
    from phonetics.tokenise import preprocess_text
    return (preprocess_text("A") == preprocess_text("a"),
            preprocess_text("\uff34") == preprocess_text("T"))


def control_must_change(stratum: str, folds_case: bool, folds_compat: bool) -> bool:
    """Must this control row's vector change, given what the tokeniser does?

    ⚠ There is no `control_stratum` any more. It classified names a SECOND time,
    alongside `stratum_of`, which already encodes the answer: `control` is stable
    under either regime, `control-case` changes iff casefold is active,
    `control-nfkc` iff NFKC is active. Deriving the routing here removes the
    second definition rather than repairing it — one classifier, with the regime
    applied at the point of use. indexing-04's observation, and it is better than
    making the second classifier regime-aware, which is what I had proposed.
    """
    if stratum == "control-case":
        return folds_case
    if stratum == "control-nfkc":
        return folds_compat
    return False


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


def check_negative_control(cosines, shard_id: int = -1) -> dict:
    """Gate 1b — the DISAGREEING witness, and it only exists under D-A.

    🛑 Gate 1 alone cannot see whether the tokeniser change landed. It asks
    "did these names reproduce their stored vector?", and a run with the fold
    silently absent answers yes to everything — a check that passes hardest
    precisely when the thing it is gating did not happen.

    Measured on 3,000 live toponyms, 8 Sep 2026: **94.55% of control rows change
    under casefold**, so under D-A the OLD gate projects a 5.45% pass rate
    against a 99% floor and every shard aborts having written nothing. Gate 1
    was built for D1/D2/D4/D7 — narrow fixes where the control genuinely cannot
    change — and D-A inverts its premise rather than stressing it.

    ⚠ The cheap repair (redefine the control as case-invariant names) was
    MEASURED AND REJECTED: only 51 of 936 sampled controls are case-invariant,
    and they are dominated by scripts with no case at all — ARABIC 21, THAI 6,
    GEORGIAN 4, TELUGU 4, TAMIL 3. That set clears `CONTROL_MIN_ROWS` per shard
    and is still blind to the change being shipped, which is worse than failing
    the row check: a gate that cannot fail is unfalsifiable from inside.
    """
    import numpy as np

    n = len(cosines)
    if n < CONTROL_MIN_ROWS:
        raise SystemExit(
            f"ABORT: only {n} must-change control rows in shard {shard_id:04d} "
            f"(need {CONTROL_MIN_ROWS}). Under a case-folding tokeniser these are "
            f"the ONLY witness that the fold landed; without enough of them the "
            f"shard cannot tell a correct run from one tokenising the old way.")
    arr = np.asarray(cosines, dtype=np.float64)
    unchanged = float((arr >= CONTROL_MIN_COSINE).mean())
    if unchanged > CONTROL_MAX_UNCHANGED_RATE:
        raise SystemExit(
            f"ABORT: negative control failed — {unchanged:.2%} of {n:,} names "
            f"that MUST change still reproduce their stored vector, above "
            f"{CONTROL_MAX_UNCHANGED_RATE:.0%}. The tokeniser reports that it "
            f"folds case, yet these names embed exactly as the index already "
            f"holds them. The likely cause is a PARTIAL application across the "
            f"four tokeniser files: `phonetics/tokenise.py` folding while the "
            f"encoder actually used does not. Nothing written.")
    return {"rows": n, "unchanged_rate": unchanged,
            "mean_cos": float(arr.mean()), "max_cos": float(arr.max())}


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
    device = resolve_device(args.device, purpose=f"reembed shard {args.shard_id:04d}")
    if device == "cpu":
        configure_cpu_threads()
    model = _load_model(device, args.model_dir)
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

    d5_by_class: dict = {}
    folds_case, folds_compat = tokeniser_folds()
    if folds_case != folds_compat:
        # Named directly rather than inferred from a control failure, because
        # the control failure describes it wrongly and aborts on every shard.
        raise SystemExit(
            f"ABORT: the tokeniser folds case={folds_case} and compatibility "
            f"forms={folds_compat}. D-A and D5 must land TOGETHER — NFKC moves "
            f"script assignment whether or not anyone intends D5, so half of "
            f"this patch is a tokeniser no version of the index was written "
            f"with. This is the direct test; without it Gate 1b would abort on "
            f"every shard saying the fold is not working, and send you to the "
            f"checkpoint and the weights instead of to the four tokeniser "
            f"files.")
    # When the tokeniser does NOT fold, every control belongs to the stable
    # stratum and this is byte-identical to the pre-D-A behaviour — no D1/D2/D4/
    # D7 run changes meaning because of this split.
    diffs, examined, changed, control_cos, control_change_cos = [], {}, {}, [], []
    # Broken out because the non-candidates are a TEST, not padding: a document
    # that tokenises identically under both encoders cannot change, so any
    # non-zero count here refutes the candidate predicate itself rather than
    # reporting a repair. Expected exactly 0.
    changed_candidate = changed_non_candidate = 0
    noise = {}
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
                cos = float(np.dot(a, b) / denom)
                if control_must_change(stratum, folds_case, folds_compat):
                    control_change_cos.append(cos)
                else:
                    control_cos.append(cos)
            delta = int(np.abs(quant[row].astype(np.int16)
                                - stored[i].astype(np.int16)).max())
            if delta and delta < MATERIAL_DELTA:
                noise[stratum] = noise.get(stratum, 0) + 1
            if delta >= MATERIAL_DELTA:
                changed[stratum] = changed.get(stratum, 0) + 1
                if is_candidate(names[i], scripts[i]):
                    changed_candidate += 1
                else:
                    changed_non_candidate += 1
                diffs.append((ids[i], quant[row].tolist()))
                if is_compatibility_only(names[i]):
                    for cls in compatibility_classes(names[i]):
                        bucket = d5_by_class.setdefault(cls, [])
                        if len(bucket) < D5_SAMPLE_PER_CLASS:
                            bucket.append(ids[i])
        if start and start % (args.batch_size * 50) == 0:
            print(f"[compute]   {start:,}/{len(keep):,} "
                  f"({start / (time.time() - t0):.0f}/s)", flush=True)

    # --- Gate 1: the positive control, BEFORE anything is written -----------
    control = check_positive_control(control_cos, args.shard_id)
    print(f"[compute]   positive control: {control['pass_rate']:.4%} of "
          f"{control['rows']:,} rows at cos >= {CONTROL_MIN_COSINE} "
          f"(mean {control['mean_cos']:.5f}, min {control['min_cos']:.5f})")
    neg = None
    if folds_case or folds_compat:
        # --- Gate 1b: and the names that MUST have changed ------------------
        neg = check_negative_control(control_change_cos, args.shard_id)
        print(f"[compute]   negative control: {neg['unchanged_rate']:.4%} of "
              f"{neg['rows']:,} must-change rows still reproduce their stored "
              f"vector (max cos {neg['max_cos']:.5f}) — the fold landed")
    else:
        print(f"[compute]   tokeniser folds neither case nor compatibility "
              f"forms; every control is in the stable stratum and Gate 1b does "
              f"not apply")

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
        # WHICH device, not which was requested. A rate that looks wrong in the
        # ledger is otherwise unattributable after the shard has been written.
        "device": describe_device(device),
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "output_path": str(final),
        "shard": args.shard_id, "scope": args.scope,
        "rows_in_shard": len(ids), "embedded": len(keep),
        "changed_total": len(diffs),
        "examined_by_stratum": examined, "changed_by_stratum": changed,
        # Documents that differ by exactly one int8 step: quantisation noise
        # from recomputing on different hardware, NOT rewritten. Counted so the
        # decision to exclude them is visible and checkable rather than implied
        # by a criterion buried in the code.
        "noise_by_stratum": noise,
        "noise_count": sum(noise.values()),
        "material_delta": MATERIAL_DELTA,
        "control": control,
        "tokeniser_block_sha256": block_hash,
        "pinned_tokeniser_sha256": pin["tokeniser_block_sha256"],
        "checkpoint": _checkpoint_hash(model_dir),
        "pinned_checkpoint": pin["checkpoint"],
        "git_commit": _git_commit(in_dir),
        "pinned_git_commit": pin["git_commit"],
        "seconds": round(time.time() - t0, 1),
        "d5_sample_ids": sorted({i for v in d5_by_class.values() for i in v}),
        # Which MECHANISMS this shard actually contained, so an absent class is
        # visible as an absence rather than inferred from a sample's diversity.
        "d5_classes_seen": {k: len(v) for k, v in sorted(d5_by_class.items())},
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    _finish_shard(final, temp, done, meta)
    for stratum in sorted(examined):
        print(f"[compute]   {stratum:<12} changed {changed.get(stratum, 0):>7,} "
              f"of {examined[stratum]:>9,} examined")
    if folds_compat:
        print(f"[compute]   D5 sample by compatibility class: "
              f"{ {k: len(v) for k, v in sorted(d5_by_class.items())} or 'NONE' }")
    print(f"[compute] shard {args.shard_id:04d} done: {len(diffs):,} MATERIAL "
          f"differences (max|delta| >= {MATERIAL_DELTA}) of {len(keep):,} embedded; "
          f"{sum(noise.values()):,} more differ by one int8 step and are "
          f"quantisation noise, not rewritten → {final}")


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
    # The CODE tree first, because the commit is a property of the code being
    # executed and not of the directory the outputs go in. This ordering is a
    # bug fix: the run's shard directory held a COPY of staged_commit.json made
    # early on, the authoritative one beside it was updated when the code was
    # re-staged, and 98 shards recorded the superseded commit while running the
    # newer code. The provenance field was confidently wrong — exactly the
    # failure the field exists to prevent, arriving through a stale copy rather
    # than through a missing file. `stage` now writes the marker INTO the code
    # tree, where it cannot be separated from what it describes.
    for candidate in ((_repo_root(),) if run_dir is None
                      else (_repo_root(), Path(run_dir))):
        staged = candidate / "staged_commit.json"
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

def _verify_written(es, index: str, sample_ids, rows) -> dict:
    """Read a sample straight back out of ES and compare it with what we sent.

    A bulk response saying `ok` means Elasticsearch accepted the request, not
    that the vector in the index is the one intended — and this pipeline exists
    because a stored vector was not what anyone assumed. So the claim is checked
    at the reader.

    🛑 RETURNS A DICT, NOT A SENTENCE. It used to return a string that was
    printed and filed in `ledger.json` and examined by no code path: a run in
    which every vector came back wrong printed "0 of 3,000 match, 3,000
    MISMATCH" and exited 0, with that string recorded as the run's
    `read_back_check`. Gate 1's own docstring, one screen above, says it "raises
    rather than returning a verdict nobody has to read" — and this was precisely
    such a verdict. Everything hardened in this pipeline is a PRODUCER; the
    read-back is the only CONSUMER, and it was the one component whose output
    nothing acted on. Found by indexing-04 auditing the instrument I had been
    taking on faith all day.

    ⚠ `missing` used to conflate two unrelated defects: the document has no
    embedding in ES (a real write failure, or `_source` excluding the field) and
    we hold no expected vector for that id (caller bookkeeping). Both printed as
    "unreadable", which is why "0 of 20 match, 20 unreadable" on a CORRECT write
    of 100,960 documents cost an hour — the label could not say which it was.
    """
    if not sample_ids:
        return {"sampled": 0, "matched": 0, "mismatched": 0,
                "no_vector": 0, "no_expected": 0, "not_returned": 0,
                "summary": "no sample"}
    wanted = dict(rows)
    try:
        got = es.mget(index=index, ids=list(sample_ids), _source=["embedding"])
    except Exception as exc:                       # never fail the check itself
        return {"sampled": len(sample_ids), "error": str(exc),
                "summary": f"could not read back ({exc})"}
    matched = mismatched = no_vector = no_expected = 0
    for doc in got.get("docs", []):
        vec = (doc.get("_source") or {}).get("embedding")
        expected = wanted.get(doc.get("_id"))
        if expected is None:
            no_expected += 1          # our bookkeeping, not the index's content
        elif vec is None:
            no_vector += 1            # the document really has no embedding
        elif list(vec) == list(expected):
            matched += 1
        else:
            mismatched += 1
    # A truncated mget under load is exactly what a 65M-document run creates,
    # and it otherwise reads as a partial match rather than an incomplete read.
    not_returned = len(sample_ids) - len(got.get("docs", []))
    summary = (f"{matched} of {len(sample_ids)} match"
               + (f", {mismatched} MISMATCH" if mismatched else "")
               + (f", {no_vector} with NO EMBEDDING" if no_vector else "")
               + (f", {no_expected} ids we held no expected vector for "
                  f"(sample/rows disagree)" if no_expected else "")
               + (f", {not_returned} NOT RETURNED by mget" if not_returned else ""))
    return {"sampled": len(sample_ids), "matched": matched,
            "mismatched": mismatched, "no_vector": no_vector,
            "no_expected": no_expected, "not_returned": not_returned,
            "summary": summary}


def enforce_read_back(result: dict, label: str, wrote_this_run: bool) -> None:
    """Gate 5. Raises, because the write is the irreversible step.

    The distinction that matters is whether THIS invocation wrote anything. A
    fully-resumed run legitimately has no sample — every shard's marker existed
    and the loop skipped them all — and since resuming is now a routine path,
    that is the normal ending for the last invocation of a campaign, i.e. the
    one whose ledger is the record. ⚠ "This run verified nothing because it
    wrote nothing" and "this run wrote and took no sample" must not both print
    as a bland "no sample".
    """
    if result.get("error"):
        raise SystemExit(
            f"ABORT: the {label} read-back could not run ({result['error']}). "
            f"An unverified write of this size is not a completed one.")
    if not result.get("sampled"):
        if wrote_this_run:
            raise SystemExit(
                f"ABORT: {label} wrote documents this run and took NO read-back "
                f"sample. That is a bug in the sampler, not an empty result.")
        print(f"[apply] {label}: this invocation wrote nothing (fully resumed), "
              f"so it verified nothing. That is expected — but this ledger is "
              f"NOT evidence the corpus is correct; the ledgers of the "
              f"invocations that did the writing are.")
        return
    bad = (result["mismatched"] + result["no_vector"]
           + result["no_expected"] + result["not_returned"])
    if bad:
        raise SystemExit(
            f"ABORT: {label} read-back — {result['summary']}. The write is the "
            f"one irreversible step here, so this exits non-zero rather than "
            f"printing a verdict into a ledger nobody reads.")


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
        # The parquet and the .done marker can come from DIFFERENT PROCESSES.
        # `shard_paths` gives each process a unique temp name and both then
        # `os.replace` onto the same final path, so two tasks working one shard
        # — a requeue racing its original, or a deliberate CPU-array/GPU-array
        # race — can interleave as A.parquet, B.parquet, B.done, A.done, leaving
        # a marker written by one and data written by the other. That is benign
        # while both computed the same thing, and a CPU task and a GPU task do
        # NOT: this run already counts documents differing by exactly one int8
        # step as hardware quantisation noise. So the meta's `changed_total`
        # would describe a shard that is not the one about to be applied, and
        # every count downstream of it would be quietly about the wrong file.
        rows = pq.read_metadata(shard_paths(in_dir, "diff", i)[0]).num_rows
        if rows != meta["changed_total"]:
            raise SystemExit(
                f"ABORT: shard {i:04d}'s marker says {meta['changed_total']:,} "
                f"changed documents but its parquet holds {rows:,}. The marker "
                f"and the data were written by different processes (marker: job "
                f"{meta.get('slurm_job_id')} attempt {meta.get('attempt')} on "
                f"{meta.get('hostname')}). Recompute that shard with --force "
                f"rather than applying counts that describe a different file.")
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
    failed_ids: list = []
    d5_sample: list = []
    seen_rows = 0
    verify_sample: list = []          # (toponym_id, vector) spanning the run
    applied_dir = in_dir / "applied"
    applied_dir.mkdir(exist_ok=True)
    t0 = time.time()

    # The write is the one irreversible step in the pipeline, so it is
    # checkpointed PER SHARD rather than summarised at the end:
    #
    #   * a marker is written after each shard, so a crash, a dropped SSH
    #     session or an ES restart leaves a record of exactly what landed —
    #     the previous version wrote the ledger only on success, which meant a
    #     failure halfway through 100,960 documents left no record at all;
    #   * a shard already marked applied is SKIPPED, so re-running resumes
    #     instead of rewriting (the updates are idempotent, but a rerun that
    #     silently repeats 100k writes against a live index is not free);
    #   * `--canary N` stops after N documents so the write path can be proved
    #     on a small population before the rest follows;
    #   * the run ABORTS if the error rate exceeds `--max-error-rate`, rather
    #     than ploughing through all 256 shards and reporting the wreck at the
    #     end. Prod ES is serving live search and there is currently no working
    #     watchdog behind it.
    for i in range(slices):
        if not shard_is_complete(in_dir, "diff", i):
            continue
        marker = applied_dir / f"applied_{i:04d}.json"
        if marker.exists():
            prior = json.loads(marker.read_text())
            # 🛑 `partial` was WRITTEN on abort and READ nowhere: the predicate
            # was a bare `marker.exists()`, so resuming SKIPPED the shard it had
            # stopped inside and left the remainder permanently unapplied — while
            # the totals stayed coherent, because `ok` is carried over from the
            # marker. The abort message says "Re-run to resume" and the resume
            # path did the opposite of what the message promised. Found by
            # indexing-04 reading the two halves against each other rather than
            # each on its own. Redoing the shard is safe: the updates are
            # idempotent, which is the whole reason a partial can be re-run.
            if not prior.get("partial"):
                ok += prior["ok"]
                errs += prior["errors"]
                continue
            print(f"[apply]   shard {i:04d} was left PARTIAL "
                  f"({prior['ok']:,} applied of {len(prior.get('toponym_ids', [])):,} "
                  f"attempted); redoing it in full — updates are idempotent.")
        table = pq.read_table(shard_paths(in_dir, "diff", i)[0])
        rows = list(zip(table.column("toponym_id").to_pylist(),
                        table.column("embedding").to_pylist()))
        s_ok = s_err = 0
        s_ids = []
        s_failed: list = []
        for start in range(0, len(rows), args.batch_size):
            chunk = rows[start:start + args.batch_size]
            actions = [{"_op_type": "update", "_index": args.index, "_id": tid,
                        "doc": {"embedding": vec, "indexed_at": now}}
                       for tid, vec in chunk]
            c_ok, c_errs = es_helpers.bulk(es_opt, actions, raise_on_error=False,
                                           max_retries=3, initial_backoff=2)
            n_err = len(c_errs) if isinstance(c_errs, list) else c_errs
            s_ok += c_ok
            s_err += n_err
            # 🛑 THIS TOOK THE WHOLE CHUNK REGARDLESS OF `c_errs`, so the marker
            # and ledger recorded every id as applied whether or not ES wrote
            # it. A sub-threshold error drip — 0.9% against a 1% rate — never
            # trips the gate and leaves ~588k documents on their OLD vectors,
            # RECORDED AS APPLIED. That is the mixed-corpus state we agreed
            # cannot be repaired after the fact, arriving through the one
            # artefact you would use to repair it. Worse than the partial
            # marker, because that at least stops. Found by indexing-04.
            failed = _failed_ids(c_errs)
            s_failed.extend(failed)
            s_ids.extend(tid for tid, _ in chunk if tid not in failed)
            if args.throttle:
                time.sleep(args.throttle)

            attempted = ok + errs + s_ok + s_err
            if errs + s_err > MAX_ABSOLUTE_ERRORS:
                marker.write_text(json.dumps(
                    {"shard": i, "ok": s_ok, "errors": s_err, "partial": True,
                     "toponym_ids": s_ids, "failed_ids": s_failed, "at": now}))
                raise SystemExit(
                    f"ABORT: {errs + s_err:,} absolute failures exceeds "
                    f"{MAX_ABSOLUTE_ERRORS:,}. The RATE is still "
                    f"{(errs + s_err) / attempted:.2%} and would not have "
                    f"tripped — which is the point: a steady sub-threshold drip "
                    f"leaves documents on their old vectors and never fires. "
                    f"Failing ids are named in {applied_dir}.")
            if attempted and (errs + s_err) / attempted > args.max_error_rate:
                marker.write_text(json.dumps(
                    {"shard": i, "ok": s_ok, "errors": s_err, "partial": True,
                     "toponym_ids": s_ids, "failed_ids": s_failed, "at": now}))
                raise SystemExit(
                    f"ABORT: error rate {(errs + s_err) / attempted:.2%} exceeds "
                    f"--max-error-rate {args.max_error_rate:.2%} after "
                    f"{attempted:,} documents. Stopped mid-shard {i:04d}; what "
                    f"landed is recorded in {applied_dir}. Re-run to resume once "
                    f"the cause is understood — do not raise the threshold to "
                    f"get past it.")

        marker.write_text(json.dumps({"shard": i, "ok": s_ok, "errors": s_err,
                                      "partial": False, "toponym_ids": s_ids,
                                      "failed_ids": s_failed, "at": now}))
        ok += s_ok
        errs += s_err
        ledger_ids.extend(s_ids)
        failed_ids.extend(s_failed)
        # 🛑 This was `rows[0]` of the first 40 shards: a FIXED POSITION, never
        # random, and never the tail of the run — which is exactly where
        # cumulative merge pressure would show. Even unbiased, n=40 detects a 1%
        # failure rate with 33.1% probability and 0.1% with 3.9%. Reservoir
        # sampling over every written row costs nothing next to 65M writes and
        # detects 0.1% at 95% confidence. Sizing from indexing-04.
        for r in rows:
            seen_rows += 1
            if len(verify_sample) < READBACK_SAMPLE:
                verify_sample.append(r)
            else:
                j = _rng.randrange(seen_rows)
                if j < READBACK_SAMPLE:
                    verify_sample[j] = r
        # The stratified half. Named by compute, so this is selection by
        # construction rather than by luck: the uniform reservoir above would
        # hold ~4.8 of these across the whole run.
        want = set(metas[i].get("d5_sample_ids") or [])
        if want:
            d5_sample.extend(r for r in rows if r[0] in want)
        print(f"[apply]   shard {i:04d}: {ok:,} ok / {errs:,} err", flush=True)

        if args.canary and ok >= args.canary:
            es.indices.refresh(index=args.index)
            canary_rows = verify_sample or rows[:20]
            verified = _verify_written(es, args.index,
                                       [t for t, _ in canary_rows], canary_rows)
            enforce_read_back(verified, "canary", wrote_this_run=True)
            verified = verified["summary"]
            print(f"[apply] CANARY: stopped after {ok:,} documents "
                  f"({i + 1} shard(s)). Read-back check: {verified}. "
                  f"Re-run without --canary to continue; completed shards will "
                  f"be skipped.")
            return

    es.indices.refresh(index=args.index)
    # Verify against a sample COLLECTED AS WE WROTE, spanning many shards.
    #
    # The first version of this took its ids from the head of the ledger and its
    # expected vectors from `table` — the last shard's, left over from the loop.
    # The two never intersected, so a completely successful run of 100,960
    # documents reported "0 of 20 match, 20 unreadable". It failed loudly rather
    # than silently, which is the right direction for a bug in a verifier, and
    # it still cost an hour of doubt about a correct write.
    readback = _verify_written(es, args.index, [t for t, _ in verify_sample],
                               verify_sample)
    enforce_read_back(readback, "main sample", wrote_this_run=bool(ok or errs))
    print(f"[apply] read-back check on {len(verify_sample)} documents "
          f"across {len({t for t, _ in verify_sample})} ids from "
          f"{len(metas)} shards: {readback['summary']}")
    d5_readback = None
    if d5_sample:
        d5_readback = _verify_written(es, args.index, [t for t, _ in d5_sample],
                                      d5_sample)
        enforce_read_back(d5_readback, "D5 stratified sample",
                          wrote_this_run=True)
        print(f"[apply] D5 stratified read-back on {len(d5_sample)} "
              f"compatibility-only documents: {d5_readback['summary']}")
    else:
        # Said out loud rather than left as an absent line, because "no D5 rows"
        # and "D5 never applied" produce the same silence.
        print(f"[apply] NO D5-stratified sample: no shard named any "
              f"compatibility-only document. Under a tokeniser that applies "
              f"NFKC this is a RED FLAG, not an empty set — ~0.16% of the "
              f"corpus should qualify. Under one that does not, it is expected.")
    ledger = {
        "run_dir": str(in_dir),
        "index": args.index,
        "applied_at": now,
        "documents_updated": ok,
        "errors": errs,
        "read_back_check": readback,
        "d5_read_back_check": d5_readback,
        "d5_sample_size": len(d5_sample),
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
        "failed_ids": failed_ids,
    }
    ledger_path = in_dir / "ledger.json"
    tmp = ledger_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
    os.replace(tmp, ledger_path)
    print(f"[apply] done: ok={ok:,} errors={errs:,} ({time.time() - t0:.0f}s)")
    print(f"[apply] ledger → {ledger_path} ({len(ledger_ids):,} toponym_ids, "
          f"checkpoint + git commit + per-shard control results). "
          f"'What did this run touch?' is now answerable by reading a file.")
    if failed_ids:
        # Non-zero, because a run that left documents on their old vectors has
        # produced a MIXED corpus and the caller must not read exit 0 as done.
        # The ids are on disk, so the repair is a re-run over `failed_ids`
        # rather than a re-run over everything.
        raise SystemExit(
            f"EXIT NON-ZERO: {len(failed_ids):,} of {len(ledger_ids) + len(failed_ids):,} "
            f"documents were NOT written and are still on their old vectors. "
            f"The rate was {errs / max(ok + errs, 1):.3%}, below every threshold, "
            f"which is exactly why this has to be an exit code rather than a "
            f"log line. Their ids are in {ledger_path} under `failed_ids`; "
            f"re-run over those rather than over the whole corpus.")


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
    pc.add_argument("--device", default="auto",
                    help="auto (default: GPU if one is visible, else CPU, "
                         "announced either way) | cuda | cuda:N | cpu. "
                         "An explicit 'cuda' is a DEMAND and aborts if no GPU "
                         "is present — it never falls back silently, because a "
                         "CPU shard inside a GPU-sized wall clock is reported as "
                         "a TIMEOUT, not as a missing device.")
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
    pa_.add_argument("--canary", type=int, default=0, metavar="N",
                     help="stop after roughly N documents and read a sample back, "
                          "so the write path is proved on a small population "
                          "first; re-run without it to continue")
    pa_.add_argument("--max-error-rate", type=float, default=0.01,
                     help="abort if the cumulative bulk error rate exceeds this "
                          "(default 1%%). Do not raise it to get past a failure.")
    pa_.add_argument("--execute", action="store_true", help="actually write (default: dry-run)")
    pa_.set_defaults(func=cmd_apply)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
