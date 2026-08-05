#!/usr/bin/env python3
"""Fetch geoBoundaries ADM0 country polygons, with a pinned, checkable release.

Why this is a module rather than a one-off script: the `un` country geometries
determine the ccode of every place in the corpus, so a silent change of source
or release silently rewrites country attribution corpus-wide. The 21 July 2026
ccode backfill was exactly that — a data decision made outside the ingest path,
invisible afterwards, and reverted without warning by the next rebuild.

Two things make geoBoundaries awkward to fetch, both handled here:

**geoboundaries.org is unreliable.** Confirmed down (ECONNREFUSED) on
5 August 2026. The GitHub repository is the durable route.

**GitHub serves Git LFS pointers, not data.** ``raw.githubusercontent.com``
returns a ~128-byte stub for every release file, so a naive download of 247
countries yields 247 stubs that parse as neither GeoJSON nor an error. The
retrieval below disables the LFS smudge filter, reads the *pointer* files to
capture their sha256 oids, and only then pulls the real blobs.

Capturing the oids first is deliberate: they are per-file checksums of the exact
release, recorded in ``adm0_lfs_manifest.json`` alongside the repository commit.
That makes "which boundaries is production built from?" answerable, and staleness
detectable, **without re-downloading anything** — the pointers are 128 bytes.

**HPSC, not HPSCGS.** We take the unsimplified per-country files, deliberately
excluding ``_simplified`` and the globally-standardised CGAZ build. HPSCGS
resolves overlapping claims by leaving disputed zones *blank*, which would give
places in Jammu and Kashmir, Aksai Chin, Arunachal Pradesh and the Kuril Islands
no country code at all. WHG's settled policy is the opposite — attest *all*
claimants (see ``ccode_enrichment.BNDA_DISPUTED_CLAIMANTS`` and
``_filter_by_containment``'s keep-all-matches). Overlapping per-country claims
feed that logic directly.

Usage::

    python -m processing.fetch_geoboundaries --dest /vast/ishi/data/authorities/geoboundaries
    python -m processing.fetch_geoboundaries --verify-only   # checksums, no download
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_URL = "https://github.com/wmgeolab/geoBoundaries"
RELEASE_SET = "gbOpen"
ADM_LEVEL = "ADM0"

# geoBoundaries gbOpen is CC BY 4.0 — redistribution permitted, including
# commercially; acknowledgement is the only requirement. That is a strictly
# better position than the incumbent UN BNDA, which carries NO explicit grant
# of rights at all (settings.CUSTOM_LICENCES['custom-un-geodata'] records
# permits_commercial as None, i.e. unknown).
LICENSE_SPDX = "CC-BY-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
CITATION = (
    "Runfola, D. et al. (2020) geoBoundaries: A global database of political "
    "administrative boundaries. PLoS ONE 15(4): e0231866. "
    "https://www.geoboundaries.org"
)

_POINTER_RE = re.compile(r"oid sha256:([0-9a-f]{64}).*?size (\d+)", re.S)
_POINTER_MAX_BYTES = 1024


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True):
    print(f"  $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=cwd, check=check, text=True)


def ensure_git_lfs() -> None:
    if shutil.which("git-lfs"):
        return
    print("git-lfs not found; installing into the active conda env")
    _run(["conda", "install", "-y", "-q", "-c", "conda-forge", "git-lfs"])
    if not shutil.which("git-lfs"):
        raise RuntimeError(
            "git-lfs is required. Without it every release file downloads as a "
            "~128-byte pointer stub that silently parses as neither GeoJSON nor "
            "an error."
        )


def clone_or_update(repo_dir: Path) -> None:
    """Blobless, sparse, smudge-disabled clone of just the gbOpen tree."""
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
    if (repo_dir / ".git").exists():
        print(f"reusing existing clone at {repo_dir}")
        subprocess.run(["git", "fetch", "--depth", "1", "origin"],
                       cwd=repo_dir, env=env, check=False, text=True)
        return
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout",
         REPO_URL, str(repo_dir)], env=env, check=True, text=True)
    _run(["git", "sparse-checkout", "init", "--cone"], cwd=repo_dir)
    _run(["git", "sparse-checkout", "set", f"releaseData/{RELEASE_SET}"],
         cwd=repo_dir)
    subprocess.run(["git", "checkout"], cwd=repo_dir, env=env, check=True,
                   text=True)


def adm0_files(repo_dir: Path) -> list[Path]:
    """Unsimplified ADM0 GeoJSONs — HPSC, one per country."""
    pattern = f"releaseData/{RELEASE_SET}/*/{ADM_LEVEL}/geoBoundaries-*-{ADM_LEVEL}.geojson"
    return sorted(p for p in repo_dir.glob(pattern)
                  if "_simplified" not in p.name)


def read_pointer(path: Path) -> dict | None:
    """Return {'oid', 'size'} if ``path`` is an LFS pointer, else None."""
    if path.stat().st_size > _POINTER_MAX_BYTES:
        return None
    match = _POINTER_RE.search(path.read_text(errors="ignore"))
    if not match:
        return None
    return {"oid": match.group(1), "size": int(match.group(2))}


def build_manifest(repo_dir: Path) -> dict:
    """Release identity + per-country checksums, from the pointers."""
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir,
                            capture_output=True, text=True).stdout.strip()
    entries: dict[str, dict] = {}
    for path in adm0_files(repo_dir):
        iso3 = path.parts[-3]
        pointer = read_pointer(path)
        if pointer:
            entries[iso3] = {"file": path.name, **pointer}
        else:
            # Already materialised: hash it so the manifest is still complete.
            import hashlib
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries[iso3] = {"file": path.name, "oid": digest,
                             "size": path.stat().st_size}
    return {
        "source": "geoBoundaries",
        "release_set": RELEASE_SET,
        "adm_level": ADM_LEVEL,
        "variant": "HPSC (unsimplified; NOT HPSCGS/CGAZ — see module docstring)",
        "repo": REPO_URL,
        "commit": commit,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "license_spdx": LICENSE_SPDX,
        "license_url": LICENSE_URL,
        "citation": CITATION,
        "countries": entries,
    }


def pull_data(repo_dir: Path) -> None:
    include = (f"releaseData/{RELEASE_SET}/*/{ADM_LEVEL}/"
               f"geoBoundaries-*-{ADM_LEVEL}.geojson")
    _run(["git", "lfs", "pull", f"--include={include}",
          "--exclude=*_simplified*"], cwd=repo_dir)


def verify(repo_dir: Path, manifest: dict) -> tuple[int, list[str]]:
    """Confirm every file is real data, and matches its recorded oid."""
    import hashlib
    bad: list[str] = []
    ok = 0
    for iso3, entry in sorted(manifest["countries"].items()):
        path = repo_dir / f"releaseData/{RELEASE_SET}/{iso3}/{ADM_LEVEL}/{entry['file']}"
        if not path.exists():
            bad.append(f"{iso3}: missing")
            continue
        if read_pointer(path) is not None:
            bad.append(f"{iso3}: still an LFS pointer (data not pulled)")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["oid"]:
            bad.append(f"{iso3}: sha256 {digest[:12]}… != recorded "
                       f"{entry['oid'][:12]}…")
            continue
        ok += 1
    return ok, bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", default="/vast/ishi/data/authorities/geoboundaries")
    ap.add_argument("--verify-only", action="store_true",
                    help="Check the existing checkout against its manifest; "
                         "downloads nothing")
    args = ap.parse_args()

    dest = Path(args.dest)
    repo_dir = dest / "repo"
    manifest_path = dest / "adm0_lfs_manifest.json"

    if args.verify_only:
        if not manifest_path.exists():
            print(f"no manifest at {manifest_path}", file=sys.stderr)
            sys.exit(2)
        manifest = json.loads(manifest_path.read_text())
        ok, bad = verify(repo_dir, manifest)
        print(f"release {manifest['commit'][:12]}… retrieved "
              f"{manifest['retrieved_at']}")
        print(f"verified {ok}/{len(manifest['countries'])} countries")
        for line in bad[:20]:
            print(f"  {line}")
        sys.exit(1 if bad else 0)

    ensure_git_lfs()
    clone_or_update(repo_dir)

    # Manifest BEFORE the pull: the pointers carry the release's own checksums,
    # so this costs 128 bytes per country rather than a re-download.
    manifest = build_manifest(repo_dir)
    dest.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    total = sum(c["size"] for c in manifest["countries"].values())
    print(f"\nrelease {manifest['commit'][:12]}…  "
          f"{len(manifest['countries'])} countries, {total / 1e6:.1f} MB")
    print(f"manifest -> {manifest_path}")

    pull_data(repo_dir)

    ok, bad = verify(repo_dir, manifest)
    print(f"\nverified {ok}/{len(manifest['countries'])} countries")
    for line in bad[:20]:
        print(f"  {line}")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
