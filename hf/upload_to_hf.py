"""
Upload Symphonym v7 to HuggingFace Hub.

Prerequisites
-------------
    pip install huggingface_hub safetensors
    huggingface-cli login          # or set HF_TOKEN env var

Usage
-----
    python hf/upload_to_hf.py --repo YOUR_USERNAME/symphonym-v7

    # Dry run (list files only):
    python hf/upload_to_hf.py --repo YOUR_USERNAME/symphonym-v7 --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HF_DIR  = Path(__file__).parent
ROOT    = HF_DIR.parent
ZENODO  = ROOT / "zenodo"

DEFAULT_ES_HOST = os.getenv("PROD_ES_URL", "http://localhost:9201")
DEFAULT_PASSWORD_FILE = os.getenv(
    "ES_PASSWORD_FILE", "/ix1/ishi/es/config/elastic.password"
)


def derive_index_stats(es_host: str, password_file: str, index: str = "toponyms") -> dict:
    """Measure the index statistics that config.json publishes.

    These used to be hard-coded in hf/config.json, where they went stale the
    moment the corpus was rebuilt: the deposit shipped "total_toponyms":
    66924548 at "embedding_coverage": 1.0 against a live index of 72,703,777.
    A published number that nobody recomputes is a claim about a corpus that no
    longer exists.

    Raises rather than returning a fallback. Shipping a plausible substitute for
    a measurement we could not take is the exact fault this repository's
    postmortem is about, and a deposit is the worst place to do it: the number
    outlives the session that wrote it and gets cited.
    """
    from elasticsearch import Elasticsearch

    kw = {"request_timeout": 300}
    pf = Path(password_file)
    if pf.exists():
        try:
            kw["basic_auth"] = ("elastic", pf.read_text().strip())
        except PermissionError:
            pass

    es = Elasticsearch(es_host, **kw)

    total = es.count(index=index)["count"]
    with_emb = es.count(
        index=index, query={"exists": {"field": "embedding"}}
    )["count"]

    # Resolve the alias to the concrete index, so the figure carries provenance
    # and a later reader can tell WHICH generation was measured.
    concrete = sorted(es.indices.get_alias(name=index).keys()) or [index]

    if total == 0:
        raise RuntimeError(
            f"{index!r} on {es_host} reports 0 documents. Refusing to publish a "
            f"coverage figure derived from an empty or wrong index."
        )
    if with_emb == 0:
        # `exists` on a dense_vector has behaved differently across ES majors.
        # A zero here is far more likely to mean "the query does not work on
        # this field" than "no toponym has an embedding" — and silently
        # publishing 0.0 coverage would be worse than failing.
        raise RuntimeError(
            f"{index!r} reports {total:,} documents but 0 with an `embedding` "
            f"field. That is almost certainly an exists-query problem on a "
            f"dense_vector rather than a real 0% coverage. Check by hand before "
            f"publishing."
        )

    return {
        "total_toponyms": total,
        "embedding_coverage": round(with_emb / total, 4),
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "measured_from": ",".join(concrete),
    }


#: Where a training run leaves its own account of the corpus it consumed.
#: `es -train-model` writes this beside the checkpoint; the repo copy under
#: `zenodo/` is the v7 run's.
TRAINING_STATS = ("coverage_stats.json",)


def read_training_provenance(stats_dir: Path | None) -> dict | None:
    """The corpus a model was trained on, lifted from the training run's own stats.

    🛑 WHY THIS EXISTS. Before it, a shipped Symphonym model recorded NOTHING
    about its training corpus — `config.json` was `{"version": "v7"}` and
    `phase3_metrics.json` held losses and epochs. So "what corpus produced this
    model?" was unanswerable from the model, and the standing rule that no
    training data may be generated from an index predating a given fix bound to
    nothing at all.

    ⚠ It took a forensic dig to answer it once, and only luck made that possible:
    v7's training pairs are gone from `/vast`, and the question was settled only
    because a COPY of the run's `coverage_stats.json` happened to survive in
    `zenodo/` — a different directory, kept for a different reason, that nobody
    was preserving on purpose. `ipa_backends` in that file is the single field
    that established v7 trained on CharsiuG2P output while `cmn` was a defective
    tag, i.e. on Chinese Han toponyms carrying Japanese readings.

    ⚠ A stamp is worth nothing until something can catch it being false — but
    here there was not even a stamp to falsify. This is the cheap half; the
    check that reads it belongs with whatever consumes the model.
    """
    if stats_dir is None:
        return None
    src = None
    for name in TRAINING_STATS:
        cand = stats_dir / name
        if cand.exists():
            src = cand
            break
    if src is None:
        print(f"  training provenance: NO {TRAINING_STATS[0]} under {stats_dir} — "
              f"OMITTED. An absent block is visible; a wrong one is not.")
        return None
    d = json.loads(src.read_text())
    prov = {
        "stats_file": str(src),
        "total_toponyms": d.get("total_toponyms"),
        "with_ipa": d.get("with_ipa"),
        "with_panphon_embedding": d.get("with_panphon_embedding"),
        "panphon_coverage_pct": d.get("panphon_coverage_pct"),
        "training_namespaces": d.get("training_namespaces"),
        # 🛑 THE LOAD-BEARING FIELD. Which G2P backends produced the IPA the
        # model learned from — the one thing that made v7's contamination
        # answerable at all.
        "ipa_backends": d.get("ipa_backends"),
        "ipa_from_db_cache": d.get("from_db_cache"),
        "ipa_from_epitran": d.get("from_epitran"),
        "ipa_from_precomputed": d.get("from_precomputed"),
        "stats_mtime_utc": datetime.utcfromtimestamp(src.stat().st_mtime)
                                   .isoformat() + "Z",
        "extraction_commit": _git_commit_of(src.parent),
    }
    missing = [k for k in ("total_toponyms", "ipa_backends") if not prov.get(k)]
    if missing:
        print(f"  ⚠ training provenance INCOMPLETE — missing {missing}. "
              f"Shipping it anyway so the gap is visible in the artefact.")
    return prov


def _git_commit_of(path: Path) -> str:
    """The commit of the tree the stats came from, or an explicit 'unknown'."""
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def write_config(dest: Path, index_stats: dict | None,
                 training_stats_dir: Path | None = None) -> None:
    """Copy hf/config.json, filling `index` from live measurements and
    `training_corpus` from the training run's own stats.

    The repo copy deliberately carries NO total_toponyms / embedding_coverage —
    so there is no stale value in the tree that could be shipped by accident.
    The failure mode is therefore "absent", which is visible, rather than
    "wrong", which is not.
    """
    cfg = json.loads((HF_DIR / "config.json").read_text())
    block = cfg.setdefault("index", {})
    block.pop("_note", None)

    if index_stats is None:
        print("  config.json: index statistics OMITTED (--index-stats=omit)")
    else:
        block.update(index_stats)
        print(f"  config.json: total_toponyms  {index_stats['total_toponyms']:,}")
        print(f"               coverage        {index_stats['embedding_coverage']:.4f}")
        print(f"               measured_from   {index_stats['measured_from']}")
        print(f"               as_of           {index_stats['as_of']}")

    prov = read_training_provenance(training_stats_dir)
    if prov is not None:
        cfg["training_corpus"] = prov
        print(f"  config.json: training corpus  {prov.get('total_toponyms')} toponyms, "
              f"ipa_backends={prov.get('ipa_backends')}")
    else:
        cfg.pop("training_corpus", None)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def build_upload_folder(staging: Path, index_stats: dict | None = None,
                        training_stats_dir: Path | None = None) -> None:
    """Assemble all files for upload into a single staging directory."""
    staging.mkdir(parents=True, exist_ok=True)

    def cp(src: Path, dst_name: str = None):
        dst = staging / (dst_name or src.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        elif src.exists():
            shutil.copy2(src, dst)
        else:
            print(f"  WARNING: {src} not found — skipping")

    # Core files from hf/
    cp(HF_DIR / "README.md")
    write_config(staging / "config.json", index_stats, training_stats_dir)
    cp(HF_DIR / "requirements.txt")
    cp(HF_DIR / "inference.py")

    # Model weights — prefer safetensors
    st = HF_DIR / "model.safetensors"
    pt = ZENODO / "models" / "final_model.pt"
    if st.exists():
        cp(st)
    elif pt.exists():
        print("  WARNING: model.safetensors not found; uploading final_model.pt instead.")
        print("           Run hf/convert_to_safetensors.py first for best results.")
        cp(pt)
    else:
        print("  ERROR: No model weights found.  Aborting.")
        sys.exit(1)

    # Vocabularies
    for f in ["char_vocab.json", "lang_vocab.json", "script_vocab.json"]:
        cp(ZENODO / "vocab" / f, f"vocab/{f}")

    # Evaluation results
    for f in [
        "mehdie_results_v7_ranking.json",
        "symphonym_v7_pairs_test_report.json",
    ]:
        cp(ZENODO / "evaluation" / f, f"evaluation/{f}")

    # Training stats
    for f in ["coverage_stats.json", "phase1_metrics.json",
              "phase2_metrics.json", "phase3_metrics.json"]:
        cp(ZENODO / "training_stats" / f, f"training_stats/{f}")

    # Epitran extensions
    cp(ZENODO / "epitran_extensions", "epitran_extensions")

    print(f"\nStaging directory: {staging}")
    total = sum(f.stat().st_size for f in staging.rglob("*") if f.is_file())
    print(f"Total size: {total / 1e6:.1f} MB")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo",    default="docuracy/symphonym-v7", help="HuggingFace repo id (default: docuracy/symphonym-v7)")
    parser.add_argument("--staging", default=str(HF_DIR / "_upload_staging"),
                        help="Temporary staging directory (default: hf/_upload_staging)")
    parser.add_argument("--dry-run", action="store_true", help="Assemble files but do not upload")
    parser.add_argument("--es-host", default=DEFAULT_ES_HOST,
                        help=f"Elasticsearch to measure the index from (default: {DEFAULT_ES_HOST})")
    parser.add_argument("--es-password-file", default=DEFAULT_PASSWORD_FILE,
                        help="File holding the `elastic` password")
    parser.add_argument("--toponyms-index", default="toponyms",
                        help="Alias or index to measure (default: toponyms)")
    parser.add_argument("--training-stats-dir", default=str(HF_DIR.parent / "zenodo" / "training_stats"),
                        help="directory holding the TRAINING run's coverage_stats.json; its "
                             "ipa_backends/row counts are stamped into config.json as "
                             "`training_corpus`. Pass '' to omit — an absent block is "
                             "visible, a wrong one is not.")
    parser.add_argument("--index-stats", choices=["derive", "omit"], default="derive",
                        help="derive (default): measure total_toponyms and embedding_coverage "
                             "from the live index, and ABORT if it cannot be reached. "
                             "omit: publish with those fields absent. There is deliberately no "
                             "option to publish a remembered value.")
    args = parser.parse_args()

    staging = Path(args.staging)

    index_stats = None
    if args.index_stats == "derive":
        print(f"=== Measuring {args.toponyms_index} on {args.es_host} ===")
        try:
            index_stats = derive_index_stats(
                args.es_host, args.es_password_file, args.toponyms_index
            )
        except Exception as e:
            print(f"ERROR: could not derive index statistics: {e}")
            print()
            print("  Publishing is REFUSED rather than falling back to a remembered")
            print("  figure. The deposit previously shipped 66,924,548 toponyms at 100%")
            print("  coverage against a live 72,703,777 precisely because a stale number")
            print("  was easier to keep than to recompute.")
            print()
            print("  Point --es-host at a reachable cluster, or pass --index-stats=omit")
            print("  to publish with those fields absent.")
            sys.exit(1)

    print("=== Assembling upload folder ===")
    tsd = Path(args.training_stats_dir) if args.training_stats_dir else None
    build_upload_folder(staging, index_stats, tsd)

    if args.dry_run:
        print("\nDry run — files assembled but not uploaded.")
        return

    print(f"\n=== Uploading to {args.repo} ===")
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("ERROR: huggingface_hub not installed.  Run: pip install huggingface_hub")
        sys.exit(1)

    api = HfApi()

    # Create repo if it doesn't exist
    try:
        api.create_repo(repo_id=args.repo, repo_type="model", exist_ok=True)
        print(f"Repository ready: https://huggingface.co/{args.repo}")
    except Exception as e:
        print(f"Could not create repo: {e}")
        sys.exit(1)

    api.upload_folder(
        folder_path=str(staging),
        repo_id=args.repo,
        repo_type="model",
        commit_message="Upload Symphonym v7 model, vocabularies, and evaluation results",
    )
    print(f"\nUpload complete: https://huggingface.co/{args.repo}")


if __name__ == "__main__":
    main()

