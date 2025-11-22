import os
import time
import httpx
from pathlib import Path
from urllib.parse import urlparse

from processing.settings import DATA_DIR, AUTHORITIES

ONE_YEAR = 365 * 24 * 3600


def _needs_update(path: Path, age: int) -> bool:
    if age == 0:
        return True
    try:
        st = path.stat()
    except FileNotFoundError:
        return True
    return (time.time() - st.st_mtime) > (age * 24 * 3600)


def _target_filename(file_cfg: dict, namespace: str) -> Path:
    url = file_cfg["url"]
    parsed = urlparse(url)

    # Basename from URL path
    basename = os.path.basename(parsed.path)

    # If URL ends with "/" or has no meaningful basename
    if not basename:
        # Use explicit deterministic name if supplied
        if "name" in file_cfg:
            basename = file_cfg["name"]
        else:
            # Fallback: last path segment, if any
            parts = parsed.path.rstrip("/").split("/")
            last = parts[-1] if parts and parts[-1] else None
            if last:
                basename = last
            else:
                basename = "download"

    return Path(f"{DATA_DIR}/authorities/{namespace}/{basename}")


def _download_with_resume(url: str, dest: Path, chunk_size: int = 1024 * 1024):
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Resolve redirects before starting a streamed download
    try:
        head = httpx.head(url, follow_redirects=True, timeout=10.0)
        if head.status_code >= 400:
            raise RuntimeError(f"HEAD failed for {url} (HTTP {head.status_code})")
        final_url = str(head.url)
    except httpx.RequestError as e:
        raise RuntimeError(f"Redirect resolution failed: {url}") from e

    temp = dest.with_suffix(dest.suffix + ".part")
    resume_pos = temp.stat().st_size if temp.exists() else 0

    headers = {"Range": f"bytes={resume_pos}-"} if resume_pos > 0 else {}

    try:
        # Now stream the resolved URL
        with httpx.stream("GET", final_url, headers=headers, timeout=60.0) as resp:
            if resp.status_code not in (200, 206):
                raise RuntimeError(
                    f"Download failed: {final_url} (HTTP {resp.status_code})"
                )

            mode = "ab" if resume_pos > 0 else "wb"
            with temp.open(mode) as f:
                for chunk in resp.iter_bytes(chunk_size=chunk_size):
                    f.write(chunk)

        temp.replace(dest)

    except httpx.RequestError as e:
        raise RuntimeError(f"HTTP error during download: {final_url}") from e



def update_authority_files(
        namespaces: str | None = None,
        authorities: list = AUTHORITIES,
        age: int = 365,
):
    """
    namespaces: comma-separated namespaces or None for all.
    age: days; 0 = force-refresh.
    """
    allowed = (
        {ns.strip() for ns in namespaces.split(",")}
        if namespaces
        else None
    )

    for auth in authorities:
        ns = auth["namespace"]
        if allowed and ns not in allowed:
            continue

        for file_cfg in auth.get("files", []):
            target = _target_filename(file_cfg, ns)
            target.parent.mkdir(parents=True, exist_ok=True)

            if _needs_update(target, age):
                _download_with_resume(file_cfg["url"], target)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--namespaces", default=None)
    parser.add_argument("-a", "--age", type=int, default=365)
    args = parser.parse_args()

    update_authority_files(
        namespaces=args.namespaces,
        age=args.age,
    )
