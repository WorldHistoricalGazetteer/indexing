"""The ``whg`` place-id map — one implementation of the id rule, plus a join.

``authorities/whg-places.py`` mints ``whg:<dataset_id>:<src_id>`` for each
contributed record, because that is the identifier WHG's own reconciliation
service emits (place#183, #172). Two edge cases make the rule un-derivable from
the database alone: a record with no ``src_id`` falls back to the WHG place key,
and a *repeat* ``src_id`` within one dataset takes a fourth segment — and which
occurrence is the repeat depends on **stream order**, which no SQL ``ORDER BY``
over the LPF feed is guaranteed to reproduce.

So the extract writes down what it minted, and every other consumer joins
through that record instead of re-deriving it. The alternative — reimplementing
the rule as a Postgres window function — would put the id rule in two
implementations that must agree forever, which is the drift this campaign exists
to stop. See ``developer/plan-completion-2026-08-31.md`` §2.3.

The join has a second, larger effect. ``contributor_replay`` harvests hard links
for every dataset whose ``ds_status`` is late-curation, while ingestion accepts
only ``authority=True AND public`` datasets — 89 datasets referenced against 48
indexed. Minting ids in SQL therefore emitted edges for places that were never
indexed: measured 31 August 2026, **10,732 of 13,466 distinct ``whg:`` endpoints
in the published overlay (79.7%) dangle**. Joining through the map makes an edge
expressible only for a place that actually exists, and turns those silent
danglers into a counted, reported drop.

File format
-----------

``{STAGED_BASE_DIR}/whg/extract/id_map.jsonl`` — JSON Lines, written beside the
staged tree the extract produced, never by hand:

* a **meta** line ``{"_meta": {"schema", "run_id", "namespace", "generated_at"}}``
  opens each writer session, so the artefact carries its own run stamp and a file
  built by two different runs says so rather than looking like one;
* then one **record** line per staged place:
  ``{"dataset_key": "1052", "place_key": "6954931", "place_id": "whg:1052:8"}``.

Appending matches ``write_staged_place_doc``: a resumed or per-dataset run adds
to the file, and on a repeated key the last record wins.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = "whg-id-map/1"
NAMESPACE = "whg"
ID_MAP_FILENAME = "id_map.jsonl"


class IdMapUnavailable(RuntimeError):
    """The id map is missing, empty or malformed.

    Raised rather than falling back to minting ids, so a consumer can never
    quietly become a second implementation of the rule.
    """


def default_id_map_path(staged_base: str | os.PathLike[str] | None = None) -> Path:
    """``{STAGED_BASE_DIR}/whg/extract/id_map.jsonl``.

    Resolves ``STAGED_BASE_DIR`` the same way ``write_staged_place_doc`` does,
    from the environment, so a staged-mode run and an ad-hoc one agree.
    """
    if staged_base is None:
        staged_base = os.environ.get(
            "STAGED_BASE_DIR",
            os.path.join(os.environ.get("IX3_BASE", "/vast/ishi"), "staged"),
        )
    return Path(staged_base) / NAMESPACE / "extract" / ID_MAP_FILENAME


def _key(dataset_key: Any, place_key: Any) -> tuple[str, str]:
    """Normalise a ``(dataset, place)`` pair to strings.

    Postgres hands these over as ints and JSON round-trips them as strings; a
    join that compared the two forms would match nothing while looking healthy.
    """
    return (str(dataset_key), str(place_key))


# ---------------------------------------------------------------------------
# Writing (the whg extract)
# ---------------------------------------------------------------------------


class IdMapWriter:
    """Append-mode writer, opened once per extract invocation.

    Used as a context manager::

        with IdMapWriter(default_id_map_path(), run_id=run_id) as idmap:
            ...
            idmap.record(dataset_sub_id, entity_id, place_id)
    """

    def __init__(self, path: str | os.PathLike[str], *, run_id: str | None = None):
        self.path = Path(path)
        self.run_id = run_id or os.environ.get("WHG_RUN_ID") or "ad-hoc"
        self.count = 0
        self._fh = None

    def __enter__(self) -> "IdMapWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(json.dumps({"_meta": {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "namespace": NAMESPACE,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }}, ensure_ascii=True) + "\n")
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def record(self, dataset_key: Any, place_key: Any, place_id: str) -> None:
        if self._fh is None:
            raise RuntimeError("IdMapWriter used outside its context manager")
        ds, pk = _key(dataset_key, place_key)
        self._fh.write(json.dumps({
            "dataset_key": ds, "place_key": pk, "place_id": place_id,
        }, ensure_ascii=True) + "\n")
        self.count += 1


# ---------------------------------------------------------------------------
# Reading (contributor_replay and any other consumer)
# ---------------------------------------------------------------------------


class WhgIdMap:
    """A loaded id map: ``(dataset_key, place_key) → place_id``, plus the
    inverse membership test the attestation path needs."""

    def __init__(self, meta: list[dict[str, Any]], by_key: dict[tuple[str, str], str]):
        self.meta = meta
        self.by_key = by_key
        self.place_ids = frozenset(by_key.values())

    # -- provenance ---------------------------------------------------------

    @property
    def run_ids(self) -> list[str]:
        """Every run id stamped into the file. More than one means the map was
        built by more than one run — legitimate for a resumed or per-dataset
        extract, and worth seeing rather than averaging away."""
        seen: list[str] = []
        for m in self.meta:
            rid = str(m.get("run_id") or "")
            if rid and rid not in seen:
                seen.append(rid)
        return seen

    def __len__(self) -> int:
        return len(self.by_key)

    # -- the join ------------------------------------------------------------

    def resolve(self, dataset_key: Any, place_key: Any) -> str | None:
        """The current ``place_id`` for a WHG ``(dataset, place)`` pair, or
        ``None`` when that place was never indexed."""
        return self.by_key.get(_key(dataset_key, place_key))

    def resolve_legacy_id(self, place_id: str) -> tuple[str | None, str]:
        """Reconcile an id that some *other* system already minted.

        Returns ``(resolved_id, disposition)`` where disposition is one of:

        ``not_whg``
            Not a ``whg:`` id at all — an authority concordance such as
            ``wd:Q90``. Passed through untouched.
        ``already_current``
            The id is one this map minted. Left **exactly** as it is. This test
            runs first and unconditionally, because remapping an id that is
            already in ``whg:<dataset>:<src_id>`` form would corrupt a correct
            edge — the one direction in which getting this wrong is destructive.
        ``remapped``
            The id is in the legacy ``whg:<dataset>:<place key>`` form and the
            map knows the place; the current id is returned.
        ``unmatched``
            A ``whg:`` id this map cannot account for either way. Dropped.
        """
        if not isinstance(place_id, str) or not place_id.startswith(f"{NAMESPACE}:"):
            return (place_id if isinstance(place_id, str) else None, "not_whg")
        if place_id in self.place_ids:
            return (place_id, "already_current")
        parts = place_id.split(":")
        if len(parts) >= 3:
            resolved = self.resolve(parts[1], ":".join(parts[2:]))
            if resolved is not None:
                return (resolved, "remapped")
        return (None, "unmatched")

    @staticmethod
    def dataset_of(place_id: str) -> str:
        """``whg:1052:8`` → ``1052``; used to attribute a drop to a dataset."""
        parts = str(place_id).split(":")
        return parts[1] if len(parts) >= 2 else "?"


def _iter_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8") as fh:
        yield from fh


def load_id_map(path: str | os.PathLike[str]) -> WhgIdMap:
    """Load and validate the id map, or raise :class:`IdMapUnavailable`.

    There is deliberately no "carry on without it" branch: a consumer that
    silently proceeded without the map would mint ids of its own, which is the
    defect the map was introduced to remove.
    """
    p = Path(path)
    if not p.is_file():
        raise IdMapUnavailable(
            f"whg id map not found at {p}. It is written by the whg extract "
            f"(authorities/whg-places.py); run that first, or point --id-map at "
            f"the staged tree of the run whose ids you mean to join against."
        )
    meta: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], str] = {}
    for lineno, line in enumerate(_iter_lines(p), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise IdMapUnavailable(f"{p}:{lineno}: not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise IdMapUnavailable(f"{p}:{lineno}: expected an object")
        if "_meta" in obj:
            m = obj["_meta"]
            if not isinstance(m, dict):
                raise IdMapUnavailable(f"{p}:{lineno}: _meta must be an object")
            schema = m.get("schema")
            if schema != SCHEMA:
                raise IdMapUnavailable(
                    f"{p}:{lineno}: unsupported id-map schema {schema!r} "
                    f"(this build reads {SCHEMA!r})"
                )
            meta.append(m)
            continue
        try:
            ds = obj["dataset_key"]
            pk = obj["place_key"]
            pid = obj["place_id"]
        except KeyError as exc:
            raise IdMapUnavailable(
                f"{p}:{lineno}: record missing {exc.args[0]!r}") from exc
        if not isinstance(pid, str) or not pid:
            raise IdMapUnavailable(f"{p}:{lineno}: place_id must be a non-empty string")
        by_key[_key(ds, pk)] = pid
    if not meta:
        raise IdMapUnavailable(
            f"{p}: no _meta line — the file carries no run stamp, so there is no "
            f"way to tell which extract's ids it holds"
        )
    if not by_key:
        raise IdMapUnavailable(f"{p}: no id records")
    return WhgIdMap(meta, by_key)
