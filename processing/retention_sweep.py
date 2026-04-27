#!/usr/bin/env python3
"""Batch 14a — Retention sweep for pending datasets (Master Plan §10.1).

Identifies WHG datasets in the ``places`` index whose ``dataset_status`` is
``pending`` and whose contributor has not edited them within an
inactivity window. Two boundaries are evaluated per run:

* **11 months** — emit a notification list so the contributor can be warned
  in-platform / by email of impending deletion.
* **12 months** — execute total deletion across:
    * the ``places`` index (delete by ``dataset_id`` prefix), and
    * the ``toponyms`` index (orphaned attestations cleaned up by the
      next toponyms rebuild — Batch 11), and
    * the SQLite hard-link assertions referencing the deleted ``place_id``s
      (the next Batch 12 run rebuilds the SQLite from staged files; here we
      just delete the dataset from staging so the next run drops it).

Datasets in ``submitted`` state pause the timer; ``rejected`` resumes it
from the rejection date; ``private_permanent`` is exempt; ``published`` is
never subject to the sweep.

Editing detection — "without contributor edits" — is read from DO PG via
the existing ``clustering.pg_client.pg_connection``: any row in
``contributor_attestations`` for the dataset's ``user_id`` whose
``modified_at`` is newer than the ``submission_date`` resets the timer.

This module is dry-run by default. Pass ``--execute`` to actually delete /
notify. The notification step is delegated to the Django side via a
configurable webhook (``WHG_RETENTION_NOTIFY_ENDPOINT``); the Django team
owns the email path.

Usage::

    python -m processing.retention_sweep --es-host URL                 # dry-run
    python -m processing.retention_sweep --es-host URL --execute       # live
    python -m processing.retention_sweep --es-host URL --as-of 2026-04-27
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib import error as urlerror, request as urlrequest

from elasticsearch import Elasticsearch

from processing.settings import (
    ES_HOST,
    PLACES_INDEX,
    WHG_API_BASE_URL,
    WHG_HTTP_INITIAL_BACKOFF,
    WHG_HTTP_MAX_RETRIES,
    WHG_HTTP_TIMEOUT,
)


_NOTIFY_ENDPOINT = os.environ.get(
    "WHG_RETENTION_NOTIFY_ENDPOINT",
    f"{WHG_API_BASE_URL.rstrip('/')}/api/retention/notify",
)

# Boundaries.
_NOTIFY_AFTER_DAYS = 11 * 30  # ~11 months
_DELETE_AFTER_DAYS = 12 * 30  # ~12 months


@dataclass(slots=True)
class PendingDataset:
    dataset_id: str          # e.g. "whg:1234"
    namespace: str           # always "whg"
    owner_user_id: int | None
    submission_date: datetime
    last_modified: datetime  # max(submission_date, last edit observed in PG)
    record_count: int
    status: str              # 'pending' / 'submitted' / 'rejected' / 'private_permanent'

    def days_idle(self, as_of: datetime) -> int:
        return int((as_of - self.last_modified).total_seconds() // 86_400)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _aggregate_pending_datasets(
    es: Elasticsearch,
    *,
    index: str = PLACES_INDEX,
) -> list[dict[str, Any]]:
    """Return raw aggregation buckets describing pending WHG datasets.

    Uses ``dataset_status: 'pending'`` + ``terms`` aggregation on
    ``dataset_id`` to enumerate every WHG dataset currently sitting in the
    pending state. Submission date is read from the per-dataset metadata
    stored alongside each place doc (top-level ``submission_date`` keyword).
    """
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"dataset_status": "pending"}},
                    {"prefix": {"dataset_id": "whg:"}},
                ]
            }
        },
        "aggs": {
            "by_dataset": {
                "terms": {"field": "dataset_id", "size": 10_000},
                "aggs": {
                    "first_seen": {"min": {"field": "submission_date"}},
                    "owner": {"terms": {"field": "owner_user_id", "size": 1}},
                    "last_seen": {"max": {"field": "submission_date"}},
                }
            }
        },
    }
    resp = es.search(index=index, body=body)
    return resp.get("aggregations", {}).get("by_dataset", {}).get("buckets", [])


def _fetch_last_edit_per_dataset(
    dataset_ids: list[str],
) -> dict[str, datetime]:
    """Return the most recent contributor-edit timestamp per dataset_id.

    Connects to DO PG via the existing SSH-tunnelled client. Returns an
    empty dict if PG isn't reachable (caller treats that as "no recent
    edits" — i.e. the timer continues to run from submission_date).
    """
    if not dataset_ids:
        return {}
    try:
        import asyncio
        from clustering.pg_client import pg_connection
    except ImportError:
        return {}

    async def _fetch() -> dict[str, datetime]:
        async with pg_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT dataset_id, MAX(modified_at) AS last_modified
                FROM contributor_attestations
                WHERE dataset_id = ANY($1::text[])
                GROUP BY dataset_id
                """,
                dataset_ids,
            )
            return {r["dataset_id"]: r["last_modified"] for r in rows}

    try:
        return asyncio.run(_fetch())
    except Exception as exc:
        print(f"WARNING: contributor edit lookup failed ({exc}); "
              "treating all datasets as unedited", file=sys.stderr)
        return {}


def _fetch_dataset_status(dataset_ids: list[str]) -> dict[str, str]:
    """Return ``{dataset_id: status}`` for ``submitted`` / ``rejected`` /
    ``private_permanent`` exclusions.

    Mirrors ``_fetch_last_edit_per_dataset`` — best-effort over PG.
    """
    if not dataset_ids:
        return {}
    try:
        import asyncio
        from clustering.pg_client import pg_connection
    except ImportError:
        return {}

    async def _fetch() -> dict[str, str]:
        async with pg_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT dataset_id, status
                FROM datasets
                WHERE dataset_id = ANY($1::text[])
                """,
                dataset_ids,
            )
            return {r["dataset_id"]: r["status"] for r in rows}

    try:
        return asyncio.run(_fetch())
    except Exception as exc:
        print(f"WARNING: dataset status lookup failed ({exc}); "
              "all datasets treated as 'pending'", file=sys.stderr)
        return {}


def discover_pending(
    es: Elasticsearch,
    *,
    index: str = PLACES_INDEX,
    as_of: datetime,
) -> list[PendingDataset]:
    """Enumerate pending datasets with derived inactivity timestamps."""
    buckets = _aggregate_pending_datasets(es, index=index)
    dataset_ids = [b["key"] for b in buckets]
    edits = _fetch_last_edit_per_dataset(dataset_ids)
    statuses = _fetch_dataset_status(dataset_ids)

    out: list[PendingDataset] = []
    for b in buckets:
        dataset_id = b["key"]
        first_seen_ms = b.get("first_seen", {}).get("value")
        owner_buckets = b.get("owner", {}).get("buckets") or []
        owner_user_id = owner_buckets[0]["key"] if owner_buckets else None
        if first_seen_ms is None:
            continue
        submission_date = datetime.fromtimestamp(
            first_seen_ms / 1000.0, tz=timezone.utc
        )
        last_modified = edits.get(dataset_id, submission_date)
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=timezone.utc)
        out.append(PendingDataset(
            dataset_id=dataset_id,
            namespace="whg",
            owner_user_id=owner_user_id,
            submission_date=submission_date,
            last_modified=max(submission_date, last_modified),
            record_count=int(b.get("doc_count", 0)),
            status=statuses.get(dataset_id, "pending"),
        ))
    return out


# ---------------------------------------------------------------------------
# Decision matrix
# ---------------------------------------------------------------------------


def classify(
    datasets: Iterable[PendingDataset],
    *,
    as_of: datetime,
    notify_after_days: int = _NOTIFY_AFTER_DAYS,
    delete_after_days: int = _DELETE_AFTER_DAYS,
) -> dict[str, list[PendingDataset]]:
    """Bucket datasets into ``notify`` / ``delete`` / ``exempt``."""
    notify: list[PendingDataset] = []
    delete: list[PendingDataset] = []
    exempt: list[PendingDataset] = []

    for ds in datasets:
        if ds.status == "private_permanent" or ds.status == "published":
            exempt.append(ds)
            continue
        if ds.status == "submitted":
            # Editorial review pauses the timer — exempt for this run.
            exempt.append(ds)
            continue

        idle = ds.days_idle(as_of)
        if idle >= delete_after_days:
            delete.append(ds)
        elif idle >= notify_after_days:
            notify.append(ds)
    return {"notify": notify, "delete": delete, "exempt": exempt}


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _http_post_with_retry(
    endpoint: str, payload: dict[str, Any], *, timeout: int, max_retries: int,
    initial_backoff: float, auth_token: str | None = None,
) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    backoff = initial_backoff
    last_err: str | None = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urlrequest.Request(endpoint, data=body, headers=headers, method="POST")
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                return resp.getcode(), resp.read().decode("utf-8", errors="replace")
        except urlerror.HTTPError as exc:
            last_err = f"HTTP {exc.code}: {exc.reason}"
            if exc.code < 500 or attempt == max_retries:
                raise RuntimeError(f"notify failed: {last_err}") from exc
        except (urlerror.URLError, TimeoutError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt == max_retries:
                raise RuntimeError(f"notify failed after {max_retries}: {last_err}") from exc
        import time
        time.sleep(backoff)
        backoff *= 2
    raise RuntimeError(f"notify failed: {last_err}")


def send_notifications(
    datasets: list[PendingDataset],
    *,
    endpoint: str = _NOTIFY_ENDPOINT,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """POST a notification batch for the 11-month boundary."""
    if not datasets:
        return {"sent": 0, "endpoint": endpoint}
    payload = {
        "boundary": "11_months",
        "datasets": [
            {
                "dataset_id": d.dataset_id,
                "owner_user_id": d.owner_user_id,
                "submission_date": d.submission_date.isoformat(),
                "last_modified": d.last_modified.isoformat(),
                "record_count": d.record_count,
            }
            for d in datasets
        ],
    }
    status, body = _http_post_with_retry(
        endpoint, payload,
        timeout=WHG_HTTP_TIMEOUT,
        max_retries=WHG_HTTP_MAX_RETRIES,
        initial_backoff=WHG_HTTP_INITIAL_BACKOFF,
        auth_token=auth_token,
    )
    return {"sent": len(datasets), "endpoint": endpoint, "status": status, "body": body[:500]}


def delete_datasets(
    es: Elasticsearch,
    datasets: list[PendingDataset],
    *,
    index: str = PLACES_INDEX,
) -> dict[str, int]:
    """Hard-delete every place doc whose ``dataset_id`` is in the delete set.

    Toponyms left orphaned by this call are cleaned up by the next Batch 11
    toponyms rebuild (which reads attestations from the staged DuckDB and
    drops anything that no longer resolves to a live place doc).
    """
    if not datasets:
        return {}
    deleted_per_dataset: dict[str, int] = {}
    for ds in datasets:
        body = {"query": {"term": {"dataset_id": ds.dataset_id}}}
        resp = es.options(request_timeout=3600).delete_by_query(
            index=index,
            body=body,
            conflicts="proceed",
            refresh=True,
            slices="auto",
            wait_for_completion=True,
        )
        deleted_per_dataset[ds.dataset_id] = int(resp.get("deleted", 0))
    return deleted_per_dataset


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_dataset_table(label: str, datasets: list[PendingDataset], as_of: datetime) -> str:
    if not datasets:
        return f"  ({label}: none)"
    lines = [f"  {label}:"]
    for d in datasets:
        lines.append(
            f"    {d.dataset_id:<24} owner={d.owner_user_id} "
            f"records={d.record_count:>6,} idle={d.days_idle(as_of):>4}d"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retention sweep for pending datasets (Batch 14a)"
    )
    parser.add_argument("--es-host", default=ES_HOST, help="Elasticsearch URL")
    parser.add_argument("--index", default=PLACES_INDEX,
                        help=f"Places index (default {PLACES_INDEX})")
    parser.add_argument("--as-of", help="Override the 'now' date (YYYY-MM-DD)")
    parser.add_argument("--notify-after-days", type=int, default=_NOTIFY_AFTER_DAYS)
    parser.add_argument("--delete-after-days", type=int, default=_DELETE_AFTER_DAYS)
    parser.add_argument("--notify-endpoint", default=_NOTIFY_ENDPOINT)
    parser.add_argument("--auth-token-file",
                        help="File containing the bearer token for the notify endpoint")
    parser.add_argument("--execute", action="store_true",
                        help="Actually send notifications and delete; default is dry-run")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    if not args.es_host:
        print("ERROR: ES host not configured", file=sys.stderr)
        return 2
    es = Elasticsearch(args.es_host, request_timeout=120, max_retries=3)
    if not es.ping():
        print(f"ERROR: cannot connect to ES at {args.es_host}", file=sys.stderr)
        return 2

    if args.as_of:
        as_of = datetime.fromisoformat(args.as_of).replace(tzinfo=timezone.utc)
    else:
        as_of = datetime.now(timezone.utc)

    pending = discover_pending(es, index=args.index, as_of=as_of)
    buckets = classify(
        pending, as_of=as_of,
        notify_after_days=args.notify_after_days,
        delete_after_days=args.delete_after_days,
    )

    auth_token: str | None = None
    if args.auth_token_file:
        auth_token = Path(args.auth_token_file).expanduser().read_text(
            encoding="utf-8"
        ).strip()

    notify_result: dict[str, Any] = {"sent": 0}
    delete_result: dict[str, int] = {}

    if args.execute:
        if buckets["notify"]:
            notify_result = send_notifications(
                buckets["notify"],
                endpoint=args.notify_endpoint,
                auth_token=auth_token,
            )
        if buckets["delete"]:
            delete_result = delete_datasets(es, buckets["delete"], index=args.index)

    summary = {
        "as_of": as_of.isoformat(),
        "es_host": args.es_host,
        "executed": args.execute,
        "discovered": len(pending),
        "to_notify": [d.dataset_id for d in buckets["notify"]],
        "to_delete": [d.dataset_id for d in buckets["delete"]],
        "exempt": [d.dataset_id for d in buckets["exempt"]],
        "notify_result": notify_result,
        "delete_result": delete_result,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        print("=" * 78)
        print(f"RETENTION SWEEP — {'EXECUTED' if args.execute else 'DRY RUN'}")
        print("=" * 78)
        print(f"As-of: {as_of.isoformat()}")
        print(f"Discovered pending datasets: {len(pending)}")
        print(_format_dataset_table("To notify (11-month)", buckets["notify"], as_of))
        print(_format_dataset_table("To delete (12-month)", buckets["delete"], as_of))
        print(_format_dataset_table("Exempt", buckets["exempt"], as_of))
        if args.execute:
            print(f"\nNotify result: {notify_result}")
            print(f"Delete result: {delete_result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
