#!/usr/bin/env python3
"""Prove that every authority's licence actually landed in the WHG registry.

**Why this exists (place#157).** ``push_gazetteer_inventory`` sends
``license_spdx`` per authority, and the registry endpoint *skips and logs* any
id its own ``License`` table doesn't know — leaving that gazetteer with no
licence at all, silently, on both the Atlas and the public attribution API. On
2026-07-29 five authorities were in exactly that state (``chgis``, ``kain_par``,
``nl``, ``dgsd``, ``ukhc``) despite four of them having terms recorded in
``processing.settings``; a sixth (``un``) was worse — the skip left a *stale*
``custom-public-domain`` row in place, inherited from the retired Natural Earth
source, asserting a public-domain grant the UN has never made.

A push that "succeeded" is therefore not evidence the licences are right. This
module closes that loop by reading back the live resolver
(``GET /api/attribution/?namespaces=…``) and comparing what the registry says
against what ``AUTHORITIES`` declares:

* **missing**   — registry reports ``license: null`` (the grey "©" chip; means
  *terms unknown*, which for compliance is worse than terms that are merely
  restrictive).
* **mismatch**  — registry's ``spdx_id`` differs from ours (a stale or
  hand-edited row).
* **unknown-id**— we send a ``custom-*`` id with no definition in
  ``settings.CUSTOM_LICENCES`` (so nobody could seed it even in principle).
* **undeclared**— we send nothing at all for an authority that has one.

Usage::

    python -m processing.verify_licences                    # check prod
    python -m processing.verify_licences --endpoint https://dev.whgazetteer.org/api/attribution/
    python -m processing.verify_licences --seed-json        # emit the custom
                                                            # License rows whg3
                                                            # needs to create

Exit code is non-zero when anything fails to resolve, so it can gate a release
or run from cron.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib import error as urlerror, parse as urlparse, request as urlrequest

from processing.settings import (
    AUTHORITIES,
    CUSTOM_LICENCES,
    WHG_API_BASE_URL,
    WHG_HTTP_TIMEOUT,
)

DEFAULT_ENDPOINT = f"{WHG_API_BASE_URL.rstrip('/')}/api/attribution/"

# Namespaces with no data of their own to licence.
_NO_LICENCE_EXPECTED = {"loc"}  # relations-only; contributes no records

# whgazetteer.org fronts the API with a bot filter that 403s the default
# urllib agent.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0 Safari/537.36")


# ---------------------------------------------------------------------------
# What we declare
# ---------------------------------------------------------------------------


def declared_licences() -> dict[str, str]:
    """``{namespace: license_spdx}`` for every authority that declares one."""
    out: dict[str, str] = {}
    for auth in AUTHORITIES:
        ns = auth.get("namespace")
        spdx = (auth.get("license_spdx") or "").strip()
        if ns and spdx:
            out[ns] = spdx
    return out


def all_namespaces() -> list[str]:
    return [a["namespace"] for a in AUTHORITIES if a.get("namespace")]


def seed_rows() -> list[dict[str, Any]]:
    """The custom (non-SPDX) ``License`` rows the WHG registry must hold.

    Only the ids actually referenced by an authority are emitted — an unused
    definition is not something whg3 needs to create.
    """
    used = set(declared_licences().values())
    return [
        {"spdx_id": spdx, **defn}
        for spdx, defn in CUSTOM_LICENCES.items()
        if spdx in used
    ]


# ---------------------------------------------------------------------------
# What the registry says
# ---------------------------------------------------------------------------


def fetch_attribution(namespaces: list[str], *, endpoint: str,
                      timeout: int = WHG_HTTP_TIMEOUT) -> dict[str, Any]:
    """GET the public attribution resolver for ``namespaces``."""
    url = f"{endpoint}?{urlparse.urlencode({'namespaces': ','.join(namespaces)})}"
    req = urlrequest.Request(url, headers={"User-Agent": _UA})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}: {exc.reason}") from exc
    except (urlerror.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {url}: {exc}") from exc


def audit(resolved: dict[str, Any]) -> list[dict[str, str]]:
    """Compare the registry's answer with what ``AUTHORITIES`` declares.

    Returns one problem dict per affected namespace (empty ⇒ all clean).
    """
    declared = declared_licences()
    sources = resolved.get("sources") or {}
    problems: list[dict[str, str]] = []

    for ns in all_namespaces():
        if ns in _NO_LICENCE_EXPECTED:
            continue
        ours = declared.get(ns)
        entry = sources.get(ns)

        if ours and ours.startswith("custom-") and ours not in CUSTOM_LICENCES:
            problems.append({
                "namespace": ns, "kind": "unknown-id",
                "detail": f"AUTHORITIES sends '{ours}' but settings.CUSTOM_LICENCES "
                          f"does not define it — the registry cannot seed it",
            })

        if not ours:
            problems.append({
                "namespace": ns, "kind": "undeclared",
                "detail": "no license_spdx in AUTHORITIES — record the terms "
                          "(use a custom-* id if they are bespoke)",
            })
            continue

        if entry is None:
            problems.append({
                "namespace": ns, "kind": "absent",
                "detail": "namespace not present in the registry at all — has the "
                          "inventory been pushed?",
            })
            continue

        licence = entry.get("license")
        if not licence:
            problems.append({
                "namespace": ns, "kind": "missing",
                "detail": f"registry reports no licence; we send '{ours}'. Seed a "
                          f"License row for '{ours}' in whg3, then re-push.",
            })
            continue

        theirs = licence.get("spdx_id")
        if theirs != ours:
            problems.append({
                "namespace": ns, "kind": "mismatch",
                "detail": f"registry holds '{theirs}' but AUTHORITIES declares "
                          f"'{ours}' (stale row — the push skipped ours as unknown)",
            })
    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify each authority's licence resolved in the WHG registry",
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                        help=f"Attribution resolver URL (default: {DEFAULT_ENDPOINT})")
    parser.add_argument("--seed-json", action="store_true",
                        help="Print the custom License rows whg3 must create, "
                             "then exit (no network call)")
    parser.add_argument("--timeout", type=int, default=WHG_HTTP_TIMEOUT)
    args = parser.parse_args()

    if args.seed_json:
        print(json.dumps(seed_rows(), indent=2, ensure_ascii=False))
        return

    namespaces = [ns for ns in all_namespaces() if ns not in _NO_LICENCE_EXPECTED]
    try:
        resolved = fetch_attribution(namespaces, endpoint=args.endpoint,
                                     timeout=args.timeout)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    problems = audit(resolved)
    if not problems:
        print(f"OK — all {len(namespaces)} authorities resolve to the licence "
              f"declared in processing.settings.")
        return

    print(f"{len(problems)} licence problem(s) at {args.endpoint}:\n", file=sys.stderr)
    for p in problems:
        print(f"  [{p['kind']:<10}] {p['namespace']:<10} {p['detail']}", file=sys.stderr)
    if any(p["kind"] in ("missing", "mismatch") for p in problems):
        print("\nSeed the missing/custom License rows in whg3 with:\n"
              "  python -m processing.verify_licences --seed-json", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
