import os
import time
import httpx
from pathlib import Path
from urllib.parse import urlparse

from processing.settings import DATA_DIR

ONE_YEAR = 365 * 24 * 3600

def _needs_update(path: Path, age: int) -> bool:
    if age == 0:
        return True
    if not path.exists():
        return True
    return (time.time() - path.stat().st_mtime) > (age * 24 * 3600)


def _target_filename(file_cfg: dict, namespace: str) -> Path:
    # Priority: explicit file_name or local_name; else filename from URL.
    if "file_name" in file_cfg:
        return Path(file_cfg["file_name"])
    if "local_name" in file_cfg:
        return Path(f"{DATA_DIR}/{namespace}/{file_cfg['local_name']}")
    # fallback: use basename of URL
    url = file_cfg["url"]
    name = os.path.basename(urlparse(url).path) or "download"
    return Path(f"{DATA_DIR}/{namespace}/{name}")


def _download_with_resume(url: str, dest: Path, chunk_size: int = 1024 * 1024):
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(dest.suffix + ".part")

    resume_pos = 0
    if temp.exists():
        resume_pos = temp.stat().st_size

    headers = {}
    if resume_pos > 0:
        headers["Range"] = f"bytes={resume_pos}-"

    with httpx.stream("GET", url, headers=headers, timeout=60.0) as resp:
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"Download failed: {url} (HTTP {resp.status_code})")

        mode = "ab" if resume_pos > 0 else "wb"
        with temp.open(mode) as f:
            for chunk in resp.iter_bytes(chunk_size=chunk_size):
                f.write(chunk)

    # move into place atomically
    temp.replace(dest)


def update_authority_files(
    authorities: list,
    age: int = 365,
    namespaces: str | None = None,
):
    """
    age: days; 0 = force-refresh.
    namespaces: comma-separated namespaces or None for all.
    """
    if namespaces:
        allowed = set(ns.strip() for ns in namespaces.split(","))
    else:
        allowed = None

    for auth in authorities:
        ns = auth["namespace"]
        if allowed is not None and ns not in allowed:
            continue

        for file_cfg in auth.get("files", []):
            target = _target_filename(file_cfg, ns)

            if _needs_update(target, age):
                url = file_cfg["url"]
                _download_with_resume(url, target)
