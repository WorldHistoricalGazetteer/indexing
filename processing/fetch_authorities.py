import os
import time
import httpx
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from processing.settings import DATA_DIR, AUTHORITIES

ONE_YEAR = 365 * 24 * 3600

NL_KEY_FILE = Path(f"{DATA_DIR}/authorities/nl/.nl_api_key")

def get_native_land_key(cli_key: str | None = None) -> str | None:
    """
    Returns the Native Land API key to use:
    1. Use CLI-provided key if given.
    2. Else, load cached key if it exists.
    3. Else, return None and warn if NL files are requested.
    """
    if cli_key:
        # Cache key
        NL_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        NL_KEY_FILE.write_text(cli_key)
        return cli_key

    if NL_KEY_FILE.exists():
        return NL_KEY_FILE.read_text().strip()

    # Check if NL namespace is requested
    nl_requested = any(
        auth["namespace"] == "nl"
        for auth in AUTHORITIES
    )
    if nl_requested:
        print("[WARN] No Native Land API key found. NL datasets will be skipped.")
    return None


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

            if not _needs_update(target, age):
                continue

            url = file_cfg["url"]

            try:
                _download_with_resume(url, target)
            except Exception as exc:
                print(f"[WARN] Skipping update for {ns}:{target.name} — {exc}")
                # IMPORTANT: continue with next file; do NOT delete or replace anything.
                continue


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--namespaces", default=None)
    parser.add_argument("-a", "--age", type=int, default=365)
    parser.add_argument("--nl-api-key", default=None, help="Native Land API Key")
    args = parser.parse_args()

    nl_key = get_native_land_key(args.nl_api_key)

    # Inject key into NativeLand URLs if available
    if nl_key:
        for auth in AUTHORITIES:
            if auth["namespace"] != "nl":
                continue
            for file_cfg in auth.get("files", []):
                url_parts = list(urlparse(file_cfg["url"]))
                query = dict(parse_qsl(url_parts[4]))
                query["key"] = nl_key
                url_parts[4] = urlencode(query)
                file_cfg["url"] = urlunparse(url_parts)

    update_authority_files(
        namespaces=args.namespaces,
        age=args.age,
    )
