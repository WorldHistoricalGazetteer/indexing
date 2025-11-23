# processing/fetch_authorities.py

import os
import sys
import time
import httpx
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from datetime import datetime, timedelta

from processing.settings import DATA_DIR, AUTHORITIES

ONE_YEAR = 365 * 24 * 3600
NL_KEY_FILE = Path(f"{DATA_DIR}/authorities/nl/.nl_api_key")


def log_message(message, level="INFO"):
    """Print timestamped log message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}", flush=True)


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
        log_message(f"Cached Native Land API key to {NL_KEY_FILE}")
        return cli_key

    if NL_KEY_FILE.exists():
        key = NL_KEY_FILE.read_text().strip()
        log_message(f"Loaded Native Land API key from {NL_KEY_FILE}")
        return key

    # Check if NL namespace is requested
    nl_requested = any(
        auth["namespace"] == "nl"
        for auth in AUTHORITIES
    )
    if nl_requested:
        log_message("No Native Land API key found. NL datasets will be skipped.", "WARN")
    return None


def format_size(bytes_size):
    """Format bytes as human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} PB"


def _needs_update(path: Path, age: int) -> bool:
    """Check if file needs updating based on age in days."""
    if age == 0:
        log_message(f"Force refresh requested for {path.name}")
        return True

    try:
        st = path.stat()
        file_age_days = (time.time() - st.st_mtime) / (24 * 3600)

        if file_age_days > age:
            log_message(f"{path.name} is {file_age_days:.0f} days old (threshold: {age} days)")
            return True
        else:
            log_message(f"{path.name} is {file_age_days:.0f} days old, skipping update")
            return False
    except FileNotFoundError:
        log_message(f"{path.name} not found, will download")
        return True


def _target_filename(file_cfg: dict, namespace: str) -> Path:
    """Determine target filename for download."""
    # Priority: deterministic name in config
    if "name" in file_cfg:
        basename = file_cfg["name"]
    else:
        url = file_cfg["url"]
        parsed = urlparse(url)
        basename = os.path.basename(parsed.path.rstrip("/"))
        if not basename:
            basename = "download"

    return Path(f"{DATA_DIR}/authorities/{namespace}/{basename}")


def _download_with_resume(url: str, dest: Path, chunk_size: int = 1024 * 1024):
    """Download file with resume capability and progress reporting."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    log_message(f"Starting download: {url}")
    log_message(f"Destination: {dest}")

    # Resolve redirects before starting a streamed download
    try:
        log_message("Resolving redirects...")
        head = httpx.head(url, follow_redirects=True, timeout=30.0)
        if head.status_code >= 400:
            raise RuntimeError(f"HEAD failed for {url} (HTTP {head.status_code})")
        final_url = str(head.url)

        if final_url != url:
            log_message(f"Redirected to: {final_url}")

        # Try to get content length
        content_length = head.headers.get('content-length')
        if content_length:
            total_size = int(content_length)
            log_message(f"File size: {format_size(total_size)}")
        else:
            total_size = None
            log_message("File size: Unknown")

    except httpx.RequestError as e:
        raise RuntimeError(f"Redirect resolution failed: {url}") from e

    temp = dest.with_suffix(dest.suffix + ".part")
    resume_pos = temp.stat().st_size if temp.exists() else 0

    if resume_pos > 0:
        log_message(f"Resuming from: {format_size(resume_pos)}")
        headers = {"Range": f"bytes={resume_pos}-"}
    else:
        headers = {}

    try:
        # Now stream the resolved URL
        start_time = time.time()
        last_report_time = start_time
        downloaded = resume_pos

        with httpx.stream("GET", final_url, headers=headers, timeout=60.0, follow_redirects=True) as resp:
            if resp.status_code not in (200, 206):
                raise RuntimeError(
                    f"Download failed: {final_url} (HTTP {resp.status_code})"
                )

            mode = "ab" if resume_pos > 0 else "wb"
            with temp.open(mode) as f:
                for chunk in resp.iter_bytes(chunk_size=chunk_size):
                    f.write(chunk)
                    downloaded += len(chunk)

                    # Progress reporting every 5 seconds
                    current_time = time.time()
                    if current_time - last_report_time >= 5:
                        elapsed = current_time - start_time
                        if elapsed > 0:
                            speed = (downloaded - resume_pos) / elapsed
                            if total_size:
                                percent = (downloaded / total_size) * 100
                                remaining = (total_size - downloaded) / speed if speed > 0 else 0
                                log_message(
                                    f"Progress: {format_size(downloaded)}/{format_size(total_size)} "
                                    f"({percent:.1f}%) - Speed: {format_size(speed)}/s - "
                                    f"ETA: {timedelta(seconds=int(remaining))}"
                                )
                            else:
                                log_message(
                                    f"Downloaded: {format_size(downloaded)} - "
                                    f"Speed: {format_size(speed)}/s"
                                )
                        last_report_time = current_time

        # Move completed file
        temp.replace(dest)

        elapsed = time.time() - start_time
        avg_speed = (downloaded - resume_pos) / elapsed if elapsed > 0 else 0
        log_message(
            f"Download complete: {format_size(downloaded)} in {timedelta(seconds=int(elapsed))} "
            f"(avg speed: {format_size(avg_speed)}/s)"
        )

    except httpx.RequestError as e:
        raise RuntimeError(f"HTTP error during download: {final_url}") from e
    except KeyboardInterrupt:
        log_message("Download interrupted by user", "WARN")
        raise
    except Exception as e:
        log_message(f"Unexpected error during download: {e}", "ERROR")
        raise


def update_authority_files(
        namespaces: str | None = None,
        authorities: list = AUTHORITIES,
        age: int = 365,
):
    """
    Update authority files.

    Args:
        namespaces: comma-separated namespaces or None for all.
        age: days; 0 = force-refresh.
    """
    allowed = (
        {ns.strip() for ns in namespaces.split(",")}
        if namespaces
        else None
    )

    # Count totals
    total_authorities = len([a for a in authorities if not allowed or a["namespace"] in allowed])
    total_files = sum(
        len(auth.get("files", []))
        for auth in authorities
        if not allowed or auth["namespace"] in allowed
    )

    log_message("=" * 80)
    log_message("AUTHORITY FILE UPDATE")
    log_message("=" * 80)
    log_message(f"Authorities to process: {total_authorities}")
    log_message(f"Total files to check: {total_files}")
    log_message(f"Age threshold: {age} days (0 = force refresh)")
    log_message(f"Namespaces filter: {namespaces or 'All'}")
    log_message("-" * 80)

    downloaded = 0
    skipped = 0
    errors = 0

    for auth in authorities:
        ns = auth["namespace"]
        if allowed and ns not in allowed:
            continue

        log_message(f"\nProcessing authority: {auth['dataset_name']} (namespace: {ns})")

        for i, file_cfg in enumerate(auth.get("files", []), 1):
            target = _target_filename(file_cfg, ns)
            target.parent.mkdir(parents=True, exist_ok=True)

            log_message(f"\nFile {i}/{len(auth.get('files', []))}: {target.name}")

            if not _needs_update(target, age):
                skipped += 1
                continue

            url = file_cfg["url"]

            try:
                _download_with_resume(url, target)
                downloaded += 1

                # Verify file was created and has content
                if target.exists():
                    size = target.stat().st_size
                    log_message(f"✓ Successfully downloaded: {target.name} ({format_size(size)})")
                else:
                    log_message(f"✗ File not created: {target.name}", "ERROR")
                    errors += 1

            except KeyboardInterrupt:
                log_message("Update cancelled by user", "WARN")
                sys.exit(1)
            except Exception as exc:
                errors += 1
                log_message(f"✗ Failed to download {ns}:{target.name} — {exc}", "ERROR")
                # Continue with next file

    # Final summary
    log_message("\n" + "=" * 80)
    log_message("UPDATE COMPLETE")
    log_message("=" * 80)
    log_message(f"Downloaded: {downloaded} files")
    log_message(f"Skipped (up-to-date): {skipped} files")
    log_message(f"Errors: {errors} files")

    if errors > 0:
        log_message(f"\n{errors} files failed to download. Check logs for details.", "WARN")
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Update authority source files"
    )
    parser.add_argument(
        "-n", "--namespaces",
        default=None,
        help="Comma-separated list of namespaces to update (default: all)"
    )
    parser.add_argument(
        "-a", "--age",
        type=int,
        default=365,
        help="Update files older than this many days (0 = force all)"
    )
    parser.add_argument(
        "--nl-api-key",
        default=None,
        help="Native Land API Key"
    )

    args = parser.parse_args()

    # Setup Native Land key if provided
    nl_key = get_native_land_key(args.nl_api_key)

    # Inject key into NativeLand URLs if available
    if nl_key:
        log_message("Injecting Native Land API key into URLs")
        for auth in AUTHORITIES:
            if auth["namespace"] != "nl":
                continue
            for file_cfg in auth.get("files", []):
                url_parts = list(urlparse(file_cfg["url"]))
                query = dict(parse_qsl(url_parts[4]))
                query["key"] = nl_key
                url_parts[4] = urlencode(query)
                file_cfg["url"] = urlunparse(url_parts)

    try:
        update_authority_files(
            namespaces=args.namespaces,
            age=args.age,
        )
    except KeyboardInterrupt:
        log_message("\nUpdate cancelled by user", "WARN")
        sys.exit(1)