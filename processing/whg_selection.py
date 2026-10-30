"""The one rule for "is this contributed WHG dataset indexable?" (place#341).

Until October 2026 the rule ``authority AND public AND ds_status in
{accessioning, indexed}`` lived in three places that each had to agree:
Django's ``AuthorityDatasetsView`` (which the extract trusts through
``dataset_status``), ``processing.retention_sweep._fetch_dataset_status``
(re-implemented in SQL), and a *different* rule in
``clustering.harvest.contributor_replay``. Phase 4 of place#341 adds a
``lifecycle_state`` to whg3's Dataset, and a v4 state, once set, must outrank
the legacy fields everywhere at once — so the rule now lives here and the
consumers call it.

Contract confirmed by whg3 (30 Oct 2026):

* enum: ``uploaded, validating, validation_failed, draft, submitted,
  in_review, changes_requested, declined, accepted, published, withdrawn``;
* ``NULL`` / key absent = **legacy** — no backfill, no default. ``draft`` is a
  real v4 state and never a legacy marker, so :data:`LEGACY_MARKERS` is empty;
* a published v4 dataset is **not** ``authority=True`` — ``authority`` is the
  legacy eligibility flag and must not be required of the ``published`` branch.

Rules, in order:

1. ``lifecycle_state == "published"`` → selected **iff** ``public`` is true;
   a published-but-not-public row is rejected with reason
   ``published_not_public``.
2. ``lifecycle_state`` absent / ``None`` / one of :data:`LEGACY_MARKERS` →
   the legacy rule: ``authority AND public AND ds_status in``
   :data:`LEGACY_INDEXABLE_DS_STATUSES`.
3. ``lifecycle_state`` in :data:`NON_PUBLISHED_LIFECYCLE_STATES` → never
   selected, **whatever** ``ds_status`` / ``public`` / ``authority`` say.
4. Any other string → not selected, reason ``unknown_lifecycle_state``. Fail
   closed; the caller counts these, because an unknown state is most likely
   an enum value this file has not been told about yet.

Every state set is a single named constant at the top of the module so an
enum change is a one-line edit. Everything here is pure: dicts in,
``(bool, reason)`` out, no I/O.
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = [
    "PUBLISHED_LIFECYCLE_STATE",
    "NON_PUBLISHED_LIFECYCLE_STATES",
    "KNOWN_LIFECYCLE_STATES",
    "LEGACY_MARKERS",
    "LEGACY_INDEXABLE_DS_STATUSES",
    "REASON_PUBLISHED",
    "REASON_PUBLISHED_NOT_PUBLIC",
    "REASON_LEGACY_INDEXABLE",
    "REASON_LEGACY_NOT_INDEXABLE",
    "REASON_NOT_PUBLISHED",
    "REASON_UNKNOWN_LIFECYCLE_STATE",
    "is_indexable",
    "reason_bucket",
    "partition",
    "summarise",
]

# -- the v4 lifecycle (whg3) --------------------------------------------------

#: The only lifecycle state that can select a dataset on its own (with ``public``).
PUBLISHED_LIFECYCLE_STATE = "published"

#: Lifecycle states known to mean "not published". A dataset in any of these is
#: never indexed, even when its legacy ``ds_status`` is ``accessioning`` — a
#: v4 state, once set, outranks the legacy field. A value missing from here is
#: refused anyway (rule 4), just under a different, counted reason.
NON_PUBLISHED_LIFECYCLE_STATES: frozenset[str] = frozenset({
    "uploaded",
    "validating",
    "validation_failed",
    "draft",
    "submitted",
    "in_review",
    "changes_requested",
    "declined",
    "accepted",
    "withdrawn",
})

#: The whole confirmed enum, for anything that wants to validate a value.
KNOWN_LIFECYCLE_STATES: frozenset[str] = (
    NON_PUBLISHED_LIFECYCLE_STATES | {PUBLISHED_LIFECYCLE_STATE}
)

#: Lifecycle values to treat exactly like an absent ``lifecycle_state`` — i.e.
#: fall through to the legacy rule. **Empty by contract**: whg3 writes no
#: default onto legacy rows, and ``draft`` is a real state, not a marker.
LEGACY_MARKERS: frozenset[str] = frozenset()

# -- the legacy (v3) rule -----------------------------------------------------

#: ``ds_status`` values that the legacy rule accepts (with ``authority`` and
#: ``public`` both true).
LEGACY_INDEXABLE_DS_STATUSES: frozenset[str] = frozenset({"accessioning", "indexed"})

# -- reasons ------------------------------------------------------------------

REASON_PUBLISHED = "published"
REASON_PUBLISHED_NOT_PUBLIC = "published_not_public"
REASON_LEGACY_INDEXABLE = "legacy_indexable"
REASON_LEGACY_NOT_INDEXABLE = "legacy_not_indexable"
REASON_NOT_PUBLISHED = "not_published"
REASON_UNKNOWN_LIFECYCLE_STATE = "unknown_lifecycle_state"


def _lifecycle_state(ds: dict[str, Any]) -> str | None:
    """Normalise ``lifecycle_state``: ``None`` for absent / null / blank, else
    the lower-cased, stripped string (an enum value is never case-sensitive on
    the wire, and a stray space must not turn ``published`` into unknown)."""
    raw = ds.get("lifecycle_state")
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def _legacy_indexable(ds: dict[str, Any]) -> tuple[bool, str]:
    authority = bool(ds.get("authority"))
    public = bool(ds.get("public"))
    ds_status = ds.get("ds_status")
    status_ok = ds_status in LEGACY_INDEXABLE_DS_STATUSES
    if authority and public and status_ok:
        return True, REASON_LEGACY_INDEXABLE
    missing = [
        name for name, held in (
            ("authority", authority),
            ("public", public),
            (f"ds_status in {sorted(LEGACY_INDEXABLE_DS_STATUSES)}", status_ok),
        ) if not held
    ]
    return False, f"{REASON_LEGACY_NOT_INDEXABLE}: {'; '.join(missing)}"


def is_indexable(ds: dict[str, Any]) -> tuple[bool, str]:
    """``(selected, reason)`` for one dataset record.

    ``ds`` is the discovery-endpoint shape (``lifecycle_state``, ``ds_status``,
    ``public``, ``authority``; other keys ignored) or an equivalent row from
    Postgres. Reasons are stable strings; the token before any ``:`` is one of
    the ``REASON_*`` constants, so a caller can bucket on it with
    :func:`reason_bucket`.
    """
    if not isinstance(ds, dict):
        raise TypeError(f"is_indexable expects a dict, got {type(ds).__name__}")
    state = _lifecycle_state(ds)
    if state is None or state in LEGACY_MARKERS:
        return _legacy_indexable(ds)
    if state == PUBLISHED_LIFECYCLE_STATE:
        if bool(ds.get("public")):
            return True, REASON_PUBLISHED
        return False, f"{REASON_PUBLISHED_NOT_PUBLIC}: public={ds.get('public')!r}"
    if state in NON_PUBLISHED_LIFECYCLE_STATES:
        return False, f"{REASON_NOT_PUBLISHED}: lifecycle_state={state}"
    return False, f"{REASON_UNKNOWN_LIFECYCLE_STATE}: lifecycle_state={state!r}"


def reason_bucket(reason: str) -> str:
    """``"legacy_not_indexable: public"`` → ``"legacy_not_indexable"``."""
    return reason.split(":", 1)[0].strip()


def partition(
    datasets: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """Split ``datasets`` into ``(selected, rejected)`` where ``rejected`` is a
    list of ``(dataset, reason)`` pairs, preserving input order."""
    selected: list[dict[str, Any]] = []
    rejected: list[tuple[dict[str, Any], str]] = []
    for ds in datasets:
        ok, reason = is_indexable(ds)
        if ok:
            selected.append(ds)
        else:
            rejected.append((ds, reason))
    return selected, rejected


def summarise(datasets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """A counted summary for a run log.

    Every dataset lands in exactly one ``by_reason`` bucket, so the bucket
    counts sum to ``examined`` — the denominator travels with the figures.
    ``unknown_lifecycle_state`` is broken out on its own (with the ids) because
    it is the fail-closed case the caller is expected to notice rather than
    average away.
    """
    datasets = list(datasets)
    buckets: dict[str, int] = {}
    selected = rejected = 0
    unknown_ids: list[Any] = []
    for ds in datasets:
        ok, reason = is_indexable(ds)
        b = reason_bucket(reason)
        buckets[b] = buckets.get(b, 0) + 1
        if ok:
            selected += 1
        else:
            rejected += 1
            if b == REASON_UNKNOWN_LIFECYCLE_STATE:
                unknown_ids.append(ds.get("id"))
    return {
        "examined": len(datasets),
        "selected": selected,
        "rejected": rejected,
        "by_reason": dict(sorted(buckets.items())),
        "unknown_lifecycle_state": len(unknown_ids),
        "unknown_lifecycle_state_ids": unknown_ids,
    }
