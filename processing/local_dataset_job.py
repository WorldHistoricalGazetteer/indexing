#!/usr/bin/env python
"""VM-local runner for ONE contributed WHG dataset (place#341).

Launched detached by ``gateway.dataset_jobs.spawn`` on the Pitt VM — the host
that can reach production Elasticsearch and cannot reach Slurm — as::

    nice -n 19 ionice -c 3 python -m processing.local_dataset_job \\
        --job-id <ms-timestamp> --dataset <id> --action index|retract

It owns the job's state file (``running`` → ``completed`` | ``failed`` with a
human ``message`` and a machine ``reason``), hands itself the global lock the
gateway took at spawn, caps itself (``RLIMIT_AS``, ``REINGEST_JOB_RLIMIT_GB``,
default 4 GB; ``signal.alarm`` wall clock, ``REINGEST_JOB_TIMEOUT_S``, default
1800 s — on expiry the whole process group is killed and the job is marked
``failed`` / ``timeout``), and logs every step with its timing. It never prints
a token.

``--action index``:

1. **discover** — ``GET {WHG_API_BASE_URL}/reconcile/authority-datasets`` with
   the bearer from ``WHG_API_TOKEN`` or the token file, find the dataset,
   apply ``processing.whg_selection.is_indexable`` intersected with Django's
   own ``dataset_status`` (exactly :func:`whg_places.select_datasets`). Absent
   or not indexable → ``failed`` / ``not_indexable``; unreachable →
   ``discovery_unreachable``.
2. **stage** — ``authorities/whg-places.py``'s own
   ``stage_all_authority_datasets(dataset_filter=[id])`` into
   ``staged/whg/datasets/<id>/extract/``, with its geom-store staging pointed
   at a PRIVATE per-run directory. Nothing is merged into the main geom
   store. Zero docs → ``no_docs_staged``.
3. **overlay** — if that staging holds geometry entries (any non-``Point``
   geometry: polygons, lines, MultiPoints), they are packed by
   ``processing.geom_overlay.write_overlay`` into a NEW generation of the
   dataset's overlay, ``<GEOM_STORE_DIR>/overlays/whg-<id>/``, and made
   ``CURRENT`` atomically — BEFORE any enrichment and BEFORE any ES write.
   The lock token handed to the writer is this job's own global lock file.
   The generation (``manifest.generation``, ``count``, ``bytes``) is recorded
   in the state file and logged. A failure leaves the previous generation
   (if any) serving and ES untouched: over a cap (``GEOM_OVERLAY_MAX_*``) is
   ``overlay_budget``; anything else is ``stage_error``. The private staging
   directory is deleted once the overlay is written (or refused). The
   nightly ``scripts/compact_overlays_cron.sh`` folds overlays into the main
   store; this runner never does.
4. **enrich in-process** — mirrors ``h3_stage`` + ``h3_merge`` by CALLING
   them: ``h3_stage._build_h3_patch`` per doc, with its polygon lookup
   resolved through a fresh ``GeomStoreReader`` on ``GEOM_STORE_DIR`` (overlays
   on, rescanned after the swap) so a ``has_geom`` geometry's ``h3_cover`` is
   polyfilled from the polygon just stored; then ``h3_merge._apply_patch_to_doc``.
   Contributor ``ccodes`` are kept; missing ones are filled from the UN BNDA
   exact lookup (``UnCountryIndex.from_bnda_geojson`` +
   ``resolve_ccodes_for_doc_exact``, as ``backfill_ccodes resolve`` does).
   ``has_geom`` / ``geom_ref`` / ``geom_class`` are left exactly as staged.
   **Hard post-condition**: every geometry with ``has_geom=true`` must come
   back from ``reader.get(geom_ref)`` as a stored geometry, else
   ``stage_error`` naming up to five keys and nothing is published —
   ``has_geom`` is retrievability (place#145/#256), and this is where the
   promise is checked rather than assumed. The result is published
   atomically as ``staged/whg/datasets/<id>/final/places.jsonl`` (+ parquet
   when pyarrow's inference accepts it).
5. **index** — ``processing.index_namespace --namespace whg --dataset <id>
   --source-stage final --replace --es-host …`` as a DRY RUN first; its
   output must show docs to index > 0, 0 uncovered geometries and no gate
   refusal, else ``dry_run_unclean`` and nothing is executed. Then the same
   command with ``--execute --job-id <job_id>``; ``index_namespace`` writes
   ``indexed.json`` last (this runner never writes the marker), and a run
   that leaves no marker naming this job is ``index_error``.
   ``--allow-missing-h3`` is never passed. When the new staging held NO
   geometries but an older overlay exists, that stale overlay is removed
   only after the ES replace succeeded.

``--action retract``: re-discover; a dataset still present AND indexable is
``still_published`` (refused). Otherwise the per-dataset sidecar
``dataset.json`` is rewritten with ``dataset_status != 'published'`` so
``index_namespace.assert_dataset_retractable`` passes, and
``index_namespace … --retract --execute`` runs (it deletes the marker). Only
after that ES delete succeeds is the dataset's overlay removed
(``remove_overlay`` + ``prune_overlay_generations``); a failed delete leaves
the overlay serving.

``--kill <job_id>``: read the state, ``kill -- -<pgid>``, mark ``failed`` /
``killed``.

Every network / process step is injectable (:class:`Steps`) so the whole
flow is testable with a fake discovery, a fake stager and a fake ES.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from gateway import dataset_jobs as dj

logger = logging.getLogger("local_dataset_job")

REPO_ROOT = dj.REPO_ROOT
RLIMIT_ENV = "REINGEST_JOB_RLIMIT_GB"
DEFAULT_RLIMIT_GB = 4
ES_HOST_ENV = "REINGEST_ES_HOST"
DEFAULT_ES_HOST = "http://localhost:9201"
WHG_NAMESPACE = "whg"
#: The sidecar ``dataset_status`` a retraction records. Anything but
#: ``published`` satisfies ``index_namespace.dataset_indexable``'s refusal.
RETRACTED_STATUS = "retracted"
#: Discovery-record keys copied onto the per-dataset sidecar so
#: ``index_namespace`` consults the real ``is_indexable`` on them.
_SELECTION_KEYS = ("lifecycle_state", "public", "authority", "ds_status")


class JobFailed(Exception):
    """A fail-closed exit: ``reason`` is one of ``dataset_jobs.REASONS``."""

    def __init__(self, reason: str, message: str):
        if reason not in dj.REASONS:
            raise ValueError(f"unknown reason {reason!r}")
        self.reason = reason
        self.message = message
        super().__init__(f"{reason}: {message}")


# ---------------------------------------------------------------------------
# whg-places.py (hyphenated filename → importlib)
# ---------------------------------------------------------------------------

_whg_places = None


def load_whg_places():
    """The ``authorities/whg-places.py`` module, loaded once by path."""
    global _whg_places
    if _whg_places is None:
        if "whg_places" in sys.modules:
            _whg_places = sys.modules["whg_places"]
        else:
            path = REPO_ROOT / "authorities" / "whg-places.py"
            spec = importlib.util.spec_from_file_location("whg_places", str(path))
            module = importlib.util.module_from_spec(spec)
            sys.modules["whg_places"] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            _whg_places = module
    return _whg_places


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def staged_base_dir() -> Path:
    """``STAGED_BASE_DIR`` from the environment first — the same resolution
    ``write_staged_place_doc`` and the gateway use — else ``processing.settings``."""
    env = os.environ.get("STAGED_BASE_DIR")
    if env:
        return Path(env)
    from processing.settings import STAGED_BASE_DIR
    return Path(STAGED_BASE_DIR)


def dataset_dir(dataset_id: str) -> Path:
    return staged_base_dir() / WHG_NAMESPACE / "datasets" / dj.validate_dataset_id(dataset_id)


def sidecar_path(dataset_id: str) -> Path:
    return dataset_dir(dataset_id) / "dataset.json"


def write_sidecar(dataset_id: str, entry: dict) -> Path:
    path = sidecar_path(dataset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def geom_staging_parent() -> Path:
    from processing.settings import GEOM_STORE_STAGING_DIR
    return Path(os.environ.get("GEOM_STORE_STAGING_DIR") or GEOM_STORE_STAGING_DIR)


def geom_store_dir() -> Path:
    """The main geom store — where ``overlays/whg-<id>/`` lives and what the
    gateway's reader serves. Environment first, as the other paths here."""
    from processing.settings import GEOM_STORE_DIR
    return Path(os.environ.get("GEOM_STORE_DIR") or GEOM_STORE_DIR)


# ---------------------------------------------------------------------------
# Default (production) steps
# ---------------------------------------------------------------------------

def _discover_default(dataset_id: str) -> Optional[dict]:
    wp = load_whg_places()
    token = wp._resolve_token(None)
    session = wp._make_session(token)
    from processing.settings import WHG_API_BASE_URL
    datasets = wp.discover_authority_datasets(session, base_url=WHG_API_BASE_URL)
    wanted = int(dataset_id)
    for ds in datasets:
        if isinstance(ds, dict) and ds.get("id") == wanted:
            return ds
    return None


def _stage_default(dataset_id: str, geom_staging_dir: Path) -> dict:
    wp = load_whg_places()
    return wp.stage_all_authority_datasets(
        dataset_filter=[int(dataset_id)],
        geom_staging_dir=geom_staging_dir,
    )


def _country_index_default():
    from processing.ccode_enrichment import UnCountryIndex
    from processing.settings import UN_BNDA_COUNTRIES_FILE
    return UnCountryIndex.from_bnda_geojson(UN_BNDA_COUNTRIES_FILE)


def _run_index_subprocess(index_argv: list[str]) -> tuple[int, str]:
    """``python -m processing.index_namespace <argv>`` in the same interpreter,
    inheriting this process's rlimit and process group; returns ``(rc, output)``."""
    env = dj.job_env()
    proc = subprocess.run(
        [sys.executable, "-m", "processing.index_namespace", *index_argv],
        cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    return int(proc.returncode), proc.stdout or ""


def apply_rlimit(limit_bytes: int) -> None:
    """``RLIMIT_AS`` soft+hard, never above the current hard limit."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    target = limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
    resource.setrlimit(resource.RLIMIT_AS, (target, target))


@dataclass
class Steps:
    discover: Callable[[str], Optional[dict]] = _discover_default
    stage: Callable[[str, Path], dict] = _stage_default
    run_index: Callable[[list[str]], tuple[int, str]] = _run_index_subprocess
    country_index: Callable[[], Any] = _country_index_default
    killpg: Callable[[int, int], None] = os.killpg
    setrlimit: Callable[[int], None] = apply_rlimit
    alarm: Callable[[int], int] = signal.alarm
    exit: Callable[[int], Any] = os._exit


# ---------------------------------------------------------------------------
# Pure helpers (tested directly)
# ---------------------------------------------------------------------------

def indexable_decision(record: Optional[dict]) -> tuple[bool, str]:
    """``whg_places.select_datasets`` over one discovery record."""
    if not isinstance(record, dict):
        return False, "not returned by the discovery endpoint"
    wp = load_whg_places()
    to_stage, rejected = wp.select_datasets([record])
    if to_stage:
        return True, "published"
    if rejected:
        return False, str(rejected[0].get("reason"))
    return False, "discovery record has no integer id"


def stored_geometry_keys(doc: dict) -> list[str]:
    """The geom-store keys every ``has_geom`` geometry of *doc* promises —
    ``geom_ref`` as staged, else ``{place_id}_{geometry_index}`` (the same
    reading ``h3_stage.cover_geometry_for`` gives it)."""
    place_id = doc.get("place_id")
    out: list[str] = []
    for idx, g in enumerate(doc.get("geometries") or []):
        if not isinstance(g, dict) or not g.get("has_geom"):
            continue
        gi = g.get("geometry_index")
        out.append(g.get("geom_ref") or f"{place_id}_{idx if gi is None else gi}")
    return out


def is_stored_geometry(gj: Any) -> bool:
    """What ``GeomStoreReader.get`` returns for a key that IS stored: a GeoJSON
    geometry dict (a GeometryCollection carries ``geometries``, not
    ``coordinates``)."""
    return (isinstance(gj, dict) and bool(gj.get("type"))
            and bool(gj.get("coordinates") or gj.get("geometries")))


_SCAN_RE = re.compile(r"\[places\] scanned ([\d,]+) staged docs, ([\d,]+) with uncovered geometry")
_TO_INDEX_RE = re.compile(r"\[places\] docs to index: ([\d,]+)")
_REFUSE_MARK = "would REFUSE"
_GATE_OK_RE = re.compile(r"\[gate\] whg:\d+ indexable=True")


def dry_run_verdict(rc: int, output: str) -> tuple[bool, str, int]:
    """``(clean, detail, n_docs)`` from an ``index_namespace`` dry run.

    Clean means: exit 0, the gate said indexable, the scan found docs and no
    uncovered geometry, and nothing announced a refusal. Every number is read
    from the run's own output — this never re-derives a count of its own."""
    if rc != 0:
        return False, f"dry run exited {rc}", 0
    m = _SCAN_RE.search(output)
    if not m:
        return False, "dry run printed no scan line", 0
    n_docs = int(m.group(1).replace(",", ""))
    uncovered = int(m.group(2).replace(",", ""))
    m2 = _TO_INDEX_RE.search(output)
    to_index = int(m2.group(1).replace(",", "")) if m2 else n_docs
    if _REFUSE_MARK in output:
        return False, "dry run announced that --execute would refuse", n_docs
    if not _GATE_OK_RE.search(output):
        return False, "dry run did not report the dataset indexable", n_docs
    if n_docs <= 0 or to_index <= 0:
        return False, "dry run found 0 docs to index", n_docs
    if uncovered:
        return False, f"{uncovered} of {n_docs} docs have uncovered geometry", n_docs
    return True, f"{n_docs} docs, 0 uncovered", n_docs


def index_argv(dataset_id: str, es_host: str, *, execute: bool, job_id: Optional[str] = None,
               retract: bool = False) -> list[str]:
    argv = ["--namespace", WHG_NAMESPACE, "--dataset", dj.validate_dataset_id(dataset_id),
            "--es-host", es_host]
    if retract:
        argv.append("--retract")
    else:
        argv += ["--source-stage", "final", "--replace"]
    if execute:
        argv.append("--execute")
        if job_id:
            argv += ["--job-id", dj.validate_job_id(job_id)]
    return argv


def read_marker(dataset_id: str) -> Optional[dict]:
    path = dataset_dir(dataset_id) / "indexed.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def enrich_dataset(dataset_id: str, *, country_index_factory: Callable[[], Any],
                   reader: Any = None, overlay_label: str = "none",
                   log: logging.Logger = logger) -> dict[str, int]:
    """``extract/places.jsonl`` → ``final/places.jsonl`` (+ parquet), as the
    h3 / ccode chain would produce it.

    ``reader`` is the ``GeomStoreReader`` through which ``h3_stage`` resolves
    every ``has_geom`` polygon — the runner opens it on ``GEOM_STORE_DIR`` after
    writing the dataset's overlay. ``None`` means the dataset staged no
    geometry, so no ``has_geom`` entry may exist.

    **Hard post-condition** (the ``has_geom`` ⇒ stored-polygon guarantee):
    every ``has_geom=true`` geometry must come back from ``reader.get(geom_ref)``
    as a stored geometry. Otherwise :class:`JobFailed` (``stage_error``) names
    up to five of the missing keys, and nothing is published — the snapshot is
    written to temps and renamed only on clean exit, so a refusal leaves no
    half ``final/`` behind. ``has_geom`` / ``geom_ref`` / ``geom_class`` are
    never rewritten here."""
    from processing.ccode_enrichment import resolve_ccodes_for_doc_exact
    from processing.h3_merge import _apply_patch_to_doc
    from processing.h3_stage import _build_h3_patch
    from processing.index_namespace import _has_uncovered_geometry
    from processing.staged_parquet import atomic_staged_snapshot, normalize_for_parquet

    src = dataset_dir(dataset_id) / "extract" / "places.jsonl"
    if not src.is_file():
        raise JobFailed(dj.REASON_STAGE_ERROR, f"staging left no {src}")
    final_dir = dataset_dir(dataset_id) / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    stats: Counter = Counter()
    country_index = None
    missing: list[str] = []

    with atomic_staged_snapshot(final_dir / "places.jsonl", final_dir / "places.parquet",
                                label=f"whg:{dataset_id} final") as out:
        for doc in _iter_jsonl(src):
            stats["docs"] += 1
            # The post-condition, checked per key against the SAME reader the
            # h3 patch reads through. Sub-cell geometries skip the store in
            # _build_h3_patch (centroid-only cover), so this is the only read
            # that proves their polygon is retrievable.
            for key in stored_geometry_keys(doc):
                stats["geometries_has_geom"] += 1
                try:
                    gj = reader.get(key) if reader is not None else None
                except Exception as exc:  # noqa: BLE001 — a lookup error is a miss
                    log.warning("geom store lookup %s failed: %r", key, exc)
                    gj = None
                if is_stored_geometry(gj):
                    stats["geometries_stored_verified"] += 1
                else:
                    missing.append(key)
            if missing:
                # Keep reading so the count is of the whole dataset, but
                # nothing below matters for a doc whose promise is broken.
                continue

            patch, _seen, _with_h3 = _build_h3_patch(doc, reader=reader)
            if patch:
                updates = {int(g["geometry_index"]): {"h3_centroid": g["h3_centroid"],
                                                      "h3_cover": g["h3_cover"]}
                           for g in patch["geometries"]}
                doc, applied = _apply_patch_to_doc(doc, updates)
                stats["geometries_h3"] += applied

            if not doc.get("ccodes"):
                if country_index is None:
                    t0 = time.time()
                    country_index = country_index_factory()
                    log.info("country index built in %.1fs", time.time() - t0)
                ccodes, outcome = resolve_ccodes_for_doc_exact(doc, country_index, None)
                if ccodes:
                    doc = dict(doc)
                    doc["ccodes"] = ccodes
                    stats["ccodes_filled"] += 1
                else:
                    stats[f"ccodes_{outcome}"] += 1
            else:
                stats["ccodes_kept"] += 1

            if _has_uncovered_geometry(doc):
                stats["uncovered"] += 1
            out.write(json.dumps(normalize_for_parquet(doc), ensure_ascii=True) + "\n")

        if missing:
            shown = ", ".join(missing[:5])
            more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
            raise JobFailed(
                dj.REASON_STAGE_ERROR,
                f"{len(missing)} of {stats['geometries_has_geom']} has_geom geometries "
                f"have no stored geometry behind them (reader "
                f"{'open' if reader is not None else 'NOT OPEN'}, overlay {overlay_label}): "
                f"{shown}{more}")

    stats["parquet_written"] = int(bool(out.parquet_written))
    return dict(stats)


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, job_id: str, dataset_id: str, action: str, *, es_host: str,
                 steps: Optional[Steps] = None, timeout_s: Optional[int] = None,
                 rlimit_gb: Optional[float] = None, log: logging.Logger = logger):
        self.job_id = dj.validate_job_id(job_id)
        self.dataset_id = dj.validate_dataset_id(dataset_id)
        self.action = dj.validate_action(action)
        self.es_host = es_host
        self.steps = steps or Steps()
        self.timeout_s = int(timeout_s if timeout_s is not None else dj.timeout_s())
        env_gb = os.environ.get(RLIMIT_ENV)
        self.rlimit_gb = float(rlimit_gb if rlimit_gb is not None
                               else (env_gb if env_gb else DEFAULT_RLIMIT_GB))
        self.log = log
        self.phase = "start"
        self._terminating = False
        self._started = time.time()

    # -- state ---------------------------------------------------------------
    def _fail(self, reason: str, message: str) -> None:
        self.log.error("FAILED [%s] %s", reason, message)
        dj.mark_terminal(self.job_id, "failed", message=message, reason=reason)

    def _complete(self, message: str) -> None:
        self.log.info("COMPLETED %s", message)
        dj.mark_terminal(self.job_id, "completed", message=message)

    @contextmanager
    def step(self, name: str):
        self.phase = name
        t0 = time.time()
        self.log.info("--- %s: start", name)
        try:
            yield
        finally:
            self.log.info("--- %s: %.1fs", name, time.time() - t0)

    def _phase_reason(self) -> str:
        return {
            "discovery": dj.REASON_DISCOVERY_UNREACHABLE,
            "stage": dj.REASON_STAGE_ERROR,
            "overlay": dj.REASON_STAGE_ERROR,
            "enrich": dj.REASON_STAGE_ERROR,
            "sidecar": dj.REASON_STAGE_ERROR,
            "index-dry-run": dj.REASON_INDEX_ERROR,
            "index-execute": dj.REASON_INDEX_ERROR,
            "overlay-prune": dj.REASON_STAGE_ERROR,
            "overlay-remove": dj.REASON_STAGE_ERROR,
            "retract": dj.REASON_INDEX_ERROR,
        }.get(self.phase, dj.REASON_STAGE_ERROR)

    # -- overlay -------------------------------------------------------------
    def _write_overlay(self, geom_dir: Path) -> Optional[dict]:
        """Pack the private geom staging into a new CURRENT generation of
        ``overlays/whg-<id>/``; ``None`` when the staging holds no geometry.
        Any failure leaves the previous generation (if any) serving."""
        from processing import geom_overlay as go
        store = geom_store_dir()
        n_entries, n_bytes = go.staging_totals(geom_dir)
        previous = go.current_generation(store, self.dataset_id)
        if n_entries == 0:
            self.log.info("staging holds 0 geometry entries: no overlay written "
                          "(previous generation: %s)", previous)
            dj.update_state(self.job_id, overlay=None)
            return None
        # The registry serves at most GEOM_OVERLAY_MAX_OVERLAYS overlays, first
        # by dataset id — a write that cannot be served is checked here, before
        # anything is built, so it never displaces CURRENT.
        live = go.list_overlays(store)
        cap = go.max_overlays()
        if self.dataset_id not in live and len(live) >= cap:
            raise JobFailed(dj.REASON_OVERLAY_BUDGET,
                            f"{len(live)} overlays already present, cap {cap} "
                            f"({go.MAX_OVERLAYS_ENV}); whg:{self.dataset_id} would not be served")
        self.log.info("writing overlay whg-%s from %d staged geometries (%d bytes; "
                      "previous generation: %s)", self.dataset_id, n_entries, n_bytes, previous)
        try:
            manifest = go.write_overlay(store, self.dataset_id, geom_dir, self.job_id,
                                        lock_token=dj.lock_path())
        except go.OverlayBudgetExceeded as exc:
            raise JobFailed(dj.REASON_OVERLAY_BUDGET, str(exc)[:500])
        except go.OverlayEmpty as exc:
            raise JobFailed(dj.REASON_STAGE_ERROR,
                            f"overlay packed nothing although staging promised "
                            f"{n_entries} geometries: {exc}"[:500])
        except Exception as exc:  # noqa: BLE001 — lock token, I/O, consolidation
            raise JobFailed(dj.REASON_STAGE_ERROR,
                            f"overlay write failed: {exc.__class__.__name__}: {exc}"[:500])
        if not isinstance(manifest, dict) or not manifest.get("generation"):
            raise JobFailed(dj.REASON_STAGE_ERROR,
                            f"write_overlay returned no manifest ({manifest!r})"[:500])
        record = {"generation": manifest.get("generation"), "count": manifest.get("count"),
                  "bytes": manifest.get("bytes"), "previous": previous,
                  "store": str(store)}
        dj.update_state(self.job_id, overlay=record)
        self.log.info("overlay whg-%s generation %s is CURRENT (%s geometries, %s bytes; "
                      "replaced %s)", self.dataset_id, record["generation"], record["count"],
                      record["bytes"], previous)
        return manifest

    def _open_reader(self, manifest: dict):
        """A fresh ``GeomStoreReader`` on the main store, overlays on, rescanned
        now — and proven to be serving the generation just written."""
        from processing.geom_store import GeomStoreReader
        store = geom_store_dir()
        try:
            reader = GeomStoreReader(store, overlays=True)
        except Exception as exc:  # noqa: BLE001 — no index, unreadable, …
            raise JobFailed(dj.REASON_STAGE_ERROR,
                            f"cannot open the geom store at {store}: "
                            f"{exc.__class__.__name__}: {exc}"[:500])
        reader.tick(force=True)
        wanted = manifest.get("generation")
        served = reader.overlay_generations.get(self.dataset_id)
        if served != wanted:
            reader.close()
            raise JobFailed(dj.REASON_STAGE_ERROR,
                            f"overlay whg-{self.dataset_id}/{wanted} was written but the "
                            f"reader serves {served!r} — check the overlay directory and "
                            f"the GEOM_OVERLAY_* caps")
        self.log.info("geom store reader open on %s (main %s; serving overlay whg-%s/%s)",
                      store, reader.main_generation, self.dataset_id, served)
        return reader

    def _drop_overlay(self, *, keep: int, why: str) -> None:
        """Unlink ``CURRENT`` and prune superseded generations. Only ever
        called AFTER the ES write it follows has succeeded."""
        from processing import geom_overlay as go
        store = geom_store_dir()
        removed = go.remove_overlay(store, self.dataset_id)
        pruned = go.prune_overlay_generations(store, self.dataset_id, keep=keep)
        self.log.info("overlay whg-%s: CURRENT %s (%s); pruned %d generation dir(s)",
                      self.dataset_id, "removed" if removed else "was already absent", why,
                      len(pruned))

    # -- signals -------------------------------------------------------------
    def _kill_group(self) -> None:
        try:
            pgid = os.getpgid(0)
        except OSError:
            return
        try:
            self.steps.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def on_alarm(self, signum=None, frame=None) -> None:
        """Wall-clock cap: mark ``timeout``, drop the lock, kill the group."""
        if self._terminating:
            return
        self._terminating = True
        self._fail(dj.REASON_TIMEOUT, f"timeout after {self.timeout_s}s in phase {self.phase}")
        dj.release_lock(self.job_id)
        self._kill_group()
        self.steps.exit(124)

    def on_term(self, signum=None, frame=None) -> None:
        if self._terminating:
            return
        self._terminating = True
        dj.mark_terminal(self.job_id, "failed", reason=dj.REASON_KILLED,
                         message=f"killed by SIGTERM in phase {self.phase}")
        dj.release_lock(self.job_id)
        self._kill_group()
        self.steps.exit(143)

    def _install_limits(self) -> None:
        limit = int(self.rlimit_gb * (1 << 30))
        try:
            self.steps.setrlimit(limit)
            self.log.info("RLIMIT_AS set to %.1f GB", self.rlimit_gb)
        except (ValueError, OSError) as exc:
            self.log.warning("could not set RLIMIT_AS (%s); continuing uncapped", exc)
        try:
            signal.signal(signal.SIGALRM, self.on_alarm)
            signal.signal(signal.SIGTERM, self.on_term)
        except ValueError:
            # Not the main thread (tests) — the handlers are exercised directly.
            pass
        self.steps.alarm(self.timeout_s)
        self.log.info("wall-clock cap %ds", self.timeout_s)

    # -- actions -------------------------------------------------------------
    def _discover(self) -> Optional[dict]:
        with self.step("discovery"):
            try:
                record = self.steps.discover(self.dataset_id)
            except Exception as exc:  # noqa: BLE001 — any transport/parse failure
                raise JobFailed(dj.REASON_DISCOVERY_UNREACHABLE,
                                f"discovery failed: {exc.__class__.__name__}: {exc}"[:500])
            self.log.info("discovery: whg:%s %s", self.dataset_id,
                          "absent" if record is None else
                          f"present (place_count={record.get('place_count')}, "
                          f"lifecycle_state={record.get('lifecycle_state')!r}, "
                          f"dataset_status={record.get('dataset_status')!r})")
            return record

    def _sidecar_entry(self, record: Optional[dict], metrics: Optional[dict],
                       dataset_status: str) -> dict:
        wp = load_whg_places()
        meta = dict(metrics or {})
        if record:
            meta.setdefault("title", record.get("title"))
            meta.setdefault("description", record.get("description"))
            meta.setdefault("owner_user_id", record.get("owner_id"))
            meta.setdefault("web_item", record.get("web_item"))
        meta["dataset_status"] = dataset_status
        entry = wp._sidecar_entry(self.dataset_id, meta)
        if record:
            if "lifecycle_state" in record:
                for k in _SELECTION_KEYS:
                    entry[k] = record.get(k)
            else:
                for k in _SELECTION_KEYS[1:]:
                    if k in record:
                        entry[k] = record.get(k)
        entry["job_id"] = self.job_id
        entry["written_at"] = dj.now_iso()
        return entry

    def run_index(self) -> None:
        record = self._discover()
        ok, why = indexable_decision(record)
        if not ok:
            raise JobFailed(dj.REASON_NOT_INDEXABLE, f"whg:{self.dataset_id} not indexable: {why}")

        geom_dir = geom_staging_parent() / f"reingest-{self.job_id}"
        try:
            with self.step("stage"):
                geom_dir.mkdir(parents=True, exist_ok=True)
                try:
                    summary = self.steps.stage(self.dataset_id, geom_dir)
                except SystemExit as exc:
                    problems = getattr(exc, "problems", None)
                    if problems:
                        raise JobFailed(dj.REASON_NOT_INDEXABLE, "; ".join(map(str, problems)))
                    raise JobFailed(dj.REASON_STAGE_ERROR, f"staging exited {exc.code}")
                except Exception as exc:  # noqa: BLE001
                    raise JobFailed(dj.REASON_STAGE_ERROR,
                                    f"staging failed: {exc.__class__.__name__}: {exc}"[:500])
                metrics = (summary or {}).get("per_dataset", {}).get(self.dataset_id) or {}
                docs_written = int(metrics.get("docs_written") or 0)
                self.log.info("staged %d docs (features_seen=%s skipped=%s geom_store_entries=%s)",
                              docs_written, metrics.get("features_seen"),
                              metrics.get("docs_skipped"), (summary or {}).get("geom_store_entries"))
            if docs_written == 0:
                raise JobFailed(dj.REASON_NO_DOCS_STAGED,
                                f"0 docs staged for whg:{self.dataset_id} "
                                f"(discovery place_count={record.get('place_count')})")

            # BEFORE any enrichment and BEFORE any ES write: the polygons the
            # staged docs promise (has_geom) become retrievable here or the job
            # stops here, with the previous generation still serving.
            with self.step("overlay"):
                manifest = self._write_overlay(geom_dir)
        finally:
            shutil.rmtree(geom_dir, ignore_errors=True)
            self.log.info("private geom staging %s removed (nothing merged)", geom_dir)

        with self.step("sidecar"):
            path = write_sidecar(self.dataset_id,
                                 self._sidecar_entry(record, metrics, "published"))
            self.log.info("sidecar %s (dataset_status=published)", path)

        reader = None
        try:
            with self.step("enrich"):
                if manifest is not None:
                    reader = self._open_reader(manifest)
                stats = enrich_dataset(
                    self.dataset_id, country_index_factory=self.steps.country_index,
                    reader=reader,
                    overlay_label=(f"whg-{self.dataset_id}/{manifest['generation']}"
                                   if manifest else "none"),
                    log=self.log)
                self.log.info("enriched: %s", json.dumps(stats, sort_keys=True))
        finally:
            if reader is not None:
                reader.close()

        with self.step("index-dry-run"):
            argv = index_argv(self.dataset_id, self.es_host, execute=False)
            rc, out = self.steps.run_index(argv)
            self.log.info("index_namespace dry run rc=%d\n%s", rc, out.rstrip())
            clean, detail, n_docs = dry_run_verdict(rc, out)
            if not clean:
                raise JobFailed(dj.REASON_DRY_RUN_UNCLEAN, f"dry run unclean: {detail}")
            self.log.info("dry run clean: %s", detail)

        with self.step("index-execute"):
            argv = index_argv(self.dataset_id, self.es_host, execute=True, job_id=self.job_id)
            rc, out = self.steps.run_index(argv)
            self.log.info("index_namespace execute rc=%d\n%s", rc, out.rstrip())
            if rc != 0:
                raise JobFailed(dj.REASON_INDEX_ERROR, f"index_namespace --execute exited {rc}")
            marker = read_marker(self.dataset_id)
            if not marker or str(marker.get("job_id")) != self.job_id:
                raise JobFailed(dj.REASON_INDEX_ERROR,
                                "execute run left no indexed.json naming this job "
                                "(a bulk reported errors or a short count — see the log)")

        from processing import geom_overlay as go
        overlay_note = "no overlay"
        if manifest is not None:
            overlay_note = f"overlay {manifest['generation']} ({manifest.get('count')} geometries)"
            with self.step("overlay-prune"):
                pruned = go.prune_overlay_generations(geom_store_dir(), self.dataset_id)
                self.log.info("pruned %d superseded overlay generation dir(s)", len(pruned))
        elif go.current_generation(geom_store_dir(), self.dataset_id) is not None:
            # The new staging held no geometry, so no ES doc now promises a
            # polygon: the older overlay is stale. Removed only now, after the
            # replace landed — a failed replace leaves the old docs with the
            # polygons they still name.
            with self.step("overlay-remove"):
                self._drop_overlay(keep=0, why="re-indexed without geometry")
            overlay_note = "stale overlay removed"
        self._complete(f"indexed {marker.get('indexed_count')} docs of whg:{self.dataset_id} "
                       f"(dry run: {n_docs} docs; {overlay_note})")

    def run_retract(self) -> None:
        record = self._discover()
        ok, why = indexable_decision(record)
        if ok:
            raise JobFailed(dj.REASON_STILL_PUBLISHED,
                            f"whg:{self.dataset_id} is still published and indexable ({why}); "
                            f"retraction is for an unpublished or deleted dataset")
        with self.step("sidecar"):
            entry = self._sidecar_entry(record, None, RETRACTED_STATUS)
            entry["retract_reason"] = why
            path = write_sidecar(self.dataset_id, entry)
            self.log.info("sidecar %s (dataset_status=%s: %s)", path, RETRACTED_STATUS, why)
        with self.step("retract"):
            argv = index_argv(self.dataset_id, self.es_host, execute=True, job_id=self.job_id,
                              retract=True)
            rc, out = self.steps.run_index(argv)
            self.log.info("index_namespace retract rc=%d\n%s", rc, out.rstrip())
            if rc != 0:
                # The overlay stays: the docs that name its polygons are still live.
                raise JobFailed(dj.REASON_INDEX_ERROR, f"index_namespace --retract exited {rc}")
        with self.step("overlay-remove"):
            self._drop_overlay(keep=0, why="retracted")
        self._complete(f"retracted whg:{self.dataset_id} ({why}); overlay removed")

    def run(self) -> int:
        self.log.info("job %s: %s whg:%s (pid %d, pgid %d, es %s)", self.job_id, self.action,
                      self.dataset_id, os.getpid(), os.getpgid(0), self.es_host)
        self._install_limits()
        try:
            try:
                dj.rewrite_lock(self.job_id, os.getpid())
            except dj.LockConflict as exc:
                raise JobFailed(dj.REASON_LOCK_CONFLICT, str(exc))
            dj.update_state(self.job_id, status="running", started_at=dj.now_iso(),
                            pid=os.getpid(), pgid=os.getpgid(0))
            if self.action == "retract":
                self.run_retract()
            else:
                self.run_index()
            return 0
        except JobFailed as exc:
            self._fail(exc.reason, exc.message)
            return 1
        except Exception as exc:  # noqa: BLE001 — anything else is still a failed job
            self.log.exception("unexpected error in phase %s", self.phase)
            self._fail(self._phase_reason(),
                       f"{self.phase}: {exc.__class__.__name__}: {exc}"[:500])
            return 1
        finally:
            try:
                self.steps.alarm(0)
            except Exception:  # noqa: BLE001
                pass
            dj.release_lock(self.job_id)
            self.log.info("job %s finished in %.1fs", self.job_id, time.time() - self._started)


# ---------------------------------------------------------------------------
# --kill
# ---------------------------------------------------------------------------

def kill_job(job_id: str, *, killpg: Callable[[int, int], None] = os.killpg,
             out=sys.stdout) -> int:
    state = dj.read_state(job_id)
    if state is None:
        print(f"no such job: {job_id}", file=out)
        return 2
    if state.get("status") not in dj.ACTIVE_STATUSES:
        print(f"job {job_id} is already {state.get('status')}", file=out)
        return 1
    pgid = state.get("pgid")
    if isinstance(pgid, int) and pgid > 0:
        try:
            killpg(pgid, signal.SIGTERM)
            print(f"sent SIGTERM to process group {pgid}", file=out)
        except ProcessLookupError:
            print(f"process group {pgid} already gone", file=out)
    else:
        print(f"job {job_id} has no pgid recorded; marking failed only", file=out)
    dj.mark_terminal(job_id, "failed", reason=dj.REASON_KILLED,
                     message=f"killed via --kill (pgid {pgid})")
    dj.release_lock(job_id)
    print(f"job {job_id} marked failed/killed", file=out)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main(argv: Optional[list[str]] = None, *, steps: Optional[Steps] = None) -> int:
    ap = argparse.ArgumentParser(description="VM-local runner for one contributed WHG dataset")
    ap.add_argument("--job-id", help="job id assigned by the gateway (digits)")
    ap.add_argument("--dataset", help="numeric WHG dataset id")
    ap.add_argument("--action", choices=dj.ACTIONS, default=dj.DEFAULT_ACTION)
    ap.add_argument("--es-host", default=os.environ.get(ES_HOST_ENV) or DEFAULT_ES_HOST)
    ap.add_argument("--timeout", type=int, default=None,
                    help=f"wall-clock cap in seconds (default ${dj.TIMEOUT_ENV} or "
                         f"{dj.DEFAULT_TIMEOUT_S})")
    ap.add_argument("--rlimit-gb", type=float, default=None,
                    help=f"RLIMIT_AS in GB (default ${RLIMIT_ENV} or {DEFAULT_RLIMIT_GB})")
    ap.add_argument("--kill", metavar="JOB_ID",
                    help="kill the named job's process group and mark it failed/killed")
    args = ap.parse_args(argv)
    _configure_logging()

    if args.kill:
        return kill_job(args.kill, killpg=(steps.killpg if steps else os.killpg))
    if not args.job_id or not args.dataset:
        ap.error("--job-id and --dataset are required (or --kill JOB_ID)")
    if dj.read_state(args.job_id) is None:
        print(f"no state file for job {args.job_id} in {dj.state_dir()}", file=sys.stderr)
        return 2
    runner = Runner(args.job_id, args.dataset, args.action, es_host=args.es_host,
                    steps=steps, timeout_s=args.timeout, rlimit_gb=args.rlimit_gb)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
