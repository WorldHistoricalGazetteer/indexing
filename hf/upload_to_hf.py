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

import argparse
import os
import shutil
import sys
from pathlib import Path

HF_DIR  = Path(__file__).parent
ROOT    = HF_DIR.parent
ZENODO  = ROOT / "zenodo"


def build_upload_folder(staging: Path) -> None:
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
    cp(HF_DIR / "config.json")
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
    args = parser.parse_args()

    staging = Path(args.staging)

    print("=== Assembling upload folder ===")
    build_upload_folder(staging)

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

