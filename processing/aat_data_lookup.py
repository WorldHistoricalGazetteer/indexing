# processing/aat_data_lookup.py

"""File-based AAT lookup tables for use on CRC compute nodes.

Slurm jobs can't reach the production ES ``types`` index (firewalled), so the
ingestion-time AAT augmentation reads from the JSON snapshots that
``scripts/types.sh --build-vocabs`` writes to ``typesystem/data/`` on the
shared ``/ix1`` mount instead. The shape of the in-memory dicts mirrors what
``processing.aat_lookup`` produces from ES so the augmenter doesn't care
which source the mappings came from.

Files consumed (all under ``typesystem/data/``):

* ``geonames.json``        — keyed by feature class (``A``, ``H``, ``P``…)
* ``osm.json`` / ``ohm.json`` — keyed by tag key (``place``, ``natural``…)
* ``wikidata.json``        — flat ``values`` list of P31 Q-items
* ``pleiades.json``        — flat ``values`` list of place-type identifiers
* ``aat_hierarchy.json``   — ``{aat_id_str: {"path": "/…", "label_en": "…"}}``;
  built by ``typesystem/build_aat_hierarchy_file.py`` from the production
  ``types`` index. Required so each augmented ``types[]`` entry can carry
  ``aat_paths`` for hierarchy-prefix search at query time.

Native-key conventions match what each authority script puts in the staged
doc's ``types[]`` entries:

| ``label`` value           | Vocab key | Lookup key on the doc                |
|---------------------------|-----------|--------------------------------------|
| ``"osm"``                 | osm       | ``sourceLabel`` (e.g. ``place=city``) |
| ``"ohm"``                 | ohm       | ``sourceLabel``                      |
| ``"wikidata"``            | wd        | ``identifier`` (e.g. ``Q515``)       |
| ``"pleiades"``            | pleiades  | ``identifier`` (e.g. ``settlement``) |
| ``"un"``                  | un        | ``identifier`` (``country``/``continent``) |
| ``"chgis"``               | chgis     | ``identifier`` (CHGIS ftype name_en) |
| ``"A"``/``"H"``/``"P"``…  | gn        | ``sourceLabel`` (e.g. ``P.PPL``)     |

``un.json`` / ``chgis.json`` use the same flat ``values`` shape as
``wikidata.json``/``pleiades.json`` and are matched on the doc's
``identifier``. (CHGIS ``sourceLabel`` is the native-script ``name_vn``, so
matching MUST use ``identifier`` = ``name_en``, which is the key in
``chgis.json``.)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# All vocab keys used by aat_enrich. The strings here also drive the
# per-vocab loader dispatch in ``load_all_aat_mappings``.
VOCABS: tuple[str, ...] = ("osm", "ohm", "gn", "wd", "pleiades", "un", "chgis")

# The single-letter geonames feature classes. Used to recognise gn-typed
# entries from a doc's ``types[i].label`` field at augmentation time.
GEONAMES_FEATURE_CLASSES: frozenset[str] = frozenset(
    {"A", "H", "L", "P", "R", "S", "T", "U", "V"}
)


def _aat_id_from_mapping(mapping: dict | None) -> int | None:
    """Extract a positive integer ``aat_id`` from a vocab entry's ``aat_mapping`` dict."""
    if not isinstance(mapping, dict):
        return None
    aat_id = mapping.get("aat_id")
    if aat_id is None:
        return None
    try:
        aat_id = int(aat_id)
    except (TypeError, ValueError):
        return None
    return aat_id if aat_id > 0 else None


def _load_keyed_by_tag(path: Path) -> dict[str, list[int]]:
    """Loader for osm/ohm-shaped files (``{tag_key: {"values": [...]}, ...}``).

    Lookup key is the doc's ``sourceLabel`` (``"{tag_key}={value}"``).
    """
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[int]] = {}
    for tag_key, tag_data in data.items():
        if not isinstance(tag_data, dict):
            continue
        for entry in tag_data.get("values") or ():
            value = entry.get("value")
            aat_id = _aat_id_from_mapping(entry.get("aat_mapping"))
            if value and aat_id is not None:
                out.setdefault(f"{tag_key}={value}", []).append(aat_id)
    return out


def _load_geonames(path: Path) -> dict[str, list[int]]:
    """Loader for geonames.json. Lookup key is the doc's ``sourceLabel``
    (``"{feature_class}.{code}"``)."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[int]] = {}
    for fclass, fclass_data in data.items():
        if not isinstance(fclass_data, dict):
            continue
        for entry in fclass_data.get("values") or ():
            code = entry.get("value")
            aat_id = _aat_id_from_mapping(entry.get("aat_mapping"))
            if code and aat_id is not None:
                out.setdefault(f"{fclass}.{code}", []).append(aat_id)
    return out


def _load_flat_values(path: Path) -> dict[str, list[int]]:
    """Loader for wikidata/pleiades-shaped files (top-level ``values`` list).

    Lookup key is the doc's ``identifier``.
    """
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[int]] = {}
    # Pleiades carries a "deprecated" list of retired type ids that still appear
    # on live places (e.g. fort/province/church) — load their mappings too.
    # wd/un/chgis have no "deprecated" key, so this is a no-op for them.
    for entry in list(data.get("values") or ()) + list(data.get("deprecated") or ()):
        key = entry.get("value") or entry.get("identifier")
        aat_id = _aat_id_from_mapping(entry.get("aat_mapping"))
        if key and aat_id is not None:
            out.setdefault(str(key), []).append(aat_id)
    return out


def load_all_aat_mappings(data_dir: Path) -> dict[str, dict[str, list[int]]]:
    """Load every vocab's native-key → ``[aat_id, …]`` lookup from disk.

    A missing or empty vocab file is logged-by-omission (the returned dict
    is empty for that vocab) rather than raising, so a partial refresh
    still produces useful augmentation for the vocabs that *did* refresh.
    """
    data_dir = Path(data_dir)
    return {
        "osm": _load_keyed_by_tag(data_dir / "osm.json"),
        "ohm": _load_keyed_by_tag(data_dir / "ohm.json"),
        "gn": _load_geonames(data_dir / "geonames.json"),
        "wd": _load_flat_values(data_dir / "wikidata.json"),
        "pleiades": _load_flat_values(data_dir / "pleiades.json"),
        "un": _load_flat_values(data_dir / "un.json"),
        "chgis": _load_flat_values(data_dir / "chgis.json"),
    }


def load_aat_hierarchy(data_dir: Path) -> dict[int, dict[str, Any]]:
    """Load the ``aat_id → {"path": str, "label_en": str}`` map.

    Produced by ``typesystem/build_aat_hierarchy_file.py``. The augmenter
    treats this file as required because per-doc ``aat_paths`` enrichment
    (the whole reason for storing AAT data on places at all — hierarchy
    filters at query time) needs it.
    """
    path = data_dir / "aat_hierarchy.json"
    if not path.exists():
        raise FileNotFoundError(
            f"AAT hierarchy file not found: {path}. "
            f"Run `typesystem.build_aat_hierarchy_file` against production ES "
            f"to produce it (typically as part of the pre-rebuild prep phase)."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {int(k): v for k, v in raw.items() if isinstance(v, dict)}


def vocab_for_type_entry(label: str | None) -> str | None:
    """Map a doc's ``types[i].label`` field to its vocab key."""
    if not isinstance(label, str):
        return None
    if label == "osm":
        return "osm"
    if label == "ohm":
        return "ohm"
    if label == "wikidata":
        return "wd"
    if label == "pleiades":
        return "pleiades"
    if label == "un":
        return "un"
    if label == "chgis":
        return "chgis"
    if label in GEONAMES_FEATURE_CLASSES:
        return "gn"
    return None


def lookup_key_for_type_entry(vocab: str, type_entry: dict) -> str | None:
    """Pull the right lookup key off a ``types[]`` entry for the given vocab."""
    if vocab in ("osm", "ohm", "gn"):
        key = type_entry.get("sourceLabel")
    elif vocab in ("wd", "pleiades", "un", "chgis"):
        key = type_entry.get("identifier")
    else:
        return None
    return str(key) if key else None
