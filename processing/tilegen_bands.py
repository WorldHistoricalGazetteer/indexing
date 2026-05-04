"""Band specifications for multi-band tile generation.

A *band* is a partition of the features destined for one bucket; each
band gets its own ``tippecanoe`` run with band-specific min/max zoom,
and the resulting per-band mbtiles are ``tile-join``'d into the
canonical bucket mbtiles. The pattern lets us:

* Generate z=0/1 tiles for sparse subsets (e.g. continental admin) that
  would be impossible to produce in a single tippecanoe run on the full
  bucket (full bucket has too many features per low-zoom tile).
* Run tippecanoe in parallel across bands (4× per-bucket throughput on
  the heavy admin tilesets).
* Avoid wasting tile area on features the style won't render at given
  zooms.

The source of truth is the ``metadata.whg:tilegen.buckets`` block in
the canonical ``whg-context`` style.json, which lives in the **tileboss**
repo at ``tileserver/styles/whg-context/style.json``. Adding a new band
means editing that JSON in the tileboss repo (or regenerating it via
``scripts/build_whg_context_style.py`` here, which writes to the sibling
tileboss clone), no code change required.

How ``load_bands`` resolves the file:
  1. Caller-supplied ``style_path`` argument
  2. ``WHG_STYLE_PATH`` env var (file path or http(s) URL)
  3. Sibling tileboss clone at ``<indexing>/../tileboss/tileserver/styles/whg-context/style.json``
  4. HTTP fetch from the tileboss ``production`` branch on GitHub

Where-clause format (one supported shape, kept simple by design):
``{"property": <str>, "in": [<str>, ...]}`` matches features whose
property equals one of the listed values. ``where: null`` matches every
feature (used by single-band buckets).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen


# Sibling tileboss clone path (used as second-tier fallback). Operators
# can clone tileboss next to indexing for offline / local-dev use.
_SIBLING_TILEBOSS_STYLE = (
    Path(__file__).parent.parent.parent
    / "tileboss" / "tileserver" / "styles" / "whg-context" / "style.json"
)

# Last-resort fetch — the production-branch raw URL on GitHub. CDN-backed
# and byte-identical to what the live tileserver serves.
_GITHUB_RAW_STYLE_URL = (
    "https://raw.githubusercontent.com/WorldHistoricalGazetteer/tileboss"
    "/production/tileserver/styles/whg-context/style.json"
)

# Back-compat alias retained for callers that pass DEFAULT_STYLE_PATH
# explicitly. New code should call ``load_bands()`` with no args and let
# the resolver pick up the canonical location.
DEFAULT_STYLE_PATH = _SIBLING_TILEBOSS_STYLE


@dataclass(frozen=True)
class Band:
    """One band partition: name, zoom range, and a where-predicate."""
    name: str
    minzoom: int
    maxzoom: int
    where: dict[str, Any] | None = None

    def matches(self, feature: dict[str, Any]) -> bool:
        """True when the feature belongs in this band.

        ``feature`` is a GeoJSON-shaped dict with ``properties``. A
        ``where=None`` band matches every feature (single-band buckets).
        """
        if self.where is None:
            return True
        prop = self.where.get("property")
        values = self.where.get("in")
        if not prop or not values:
            return False
        feature_value = (feature.get("properties") or {}).get(prop)
        return feature_value in set(values)


def _read_one(src: str | Path) -> dict | None:
    """Read+parse one source (path or URL). Returns the dict or ``None``
    on any error — caller decides whether to fall through."""
    s = str(src)
    try:
        if s.startswith(("http://", "https://")):
            with urlopen(s, timeout=10) as resp:
                return json.loads(resp.read())
        p = Path(s)
        if not p.exists():
            return None
        with p.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_style_json(style_path: Path | str | None) -> dict | None:
    """Resolve the canonical whg-context style.json.

    When the caller supplies ``style_path`` it is **authoritative** — no
    fall-through. This preserves the contract that "if you point me at
    a specific file, I use it and only it" (broken file ⇒ empty bands).

    When ``style_path`` is None, walk the auto-detect chain:
      1. ``WHG_STYLE_PATH`` env var (path or http(s) URL)
      2. Sibling tileboss clone at ``../tileboss/...``
      3. HTTP fetch from the tileboss production branch on GitHub

    Returns the parsed JSON dict on success, ``None`` on any failure
    (caller treats as "no bands configured", legacy single-pass mode).
    """
    if style_path is not None:
        return _read_one(style_path)

    candidates: list[str | Path] = []
    env = os.environ.get("WHG_STYLE_PATH")
    if env:
        candidates.append(env)
    candidates.append(_SIBLING_TILEBOSS_STYLE)
    candidates.append(_GITHUB_RAW_STYLE_URL)

    for cand in candidates:
        result = _read_one(cand)
        if result is not None:
            return result
    return None


def load_bands(
    style_path: Path | str | None = None,
) -> dict[str, list[Band]]:
    """Return ``{bucket_name: [Band, ...]}`` for every bucket declared in
    the style's ``metadata.whg:tilegen.buckets`` block. Buckets not
    listed there get no entry — callers fall back to the legacy
    single-bucket behaviour.

    Returns an empty dict on missing file / missing block. Callers must
    handle that as "no bands configured" (legacy single-pass mode).

    Resolution order for the style file is documented in the module
    docstring; pass ``style_path`` to override.
    """
    style = _load_style_json(style_path)
    if style is None:
        return {}
    raw = (
        style.get("metadata", {})
        .get("whg:tilegen", {})
        .get("buckets", {})
    )
    out: dict[str, list[Band]] = {}
    for bucket, bands_spec in raw.items():
        bands = []
        for spec in bands_spec or ():
            bands.append(Band(
                name=spec["name"],
                minzoom=int(spec["minzoom"]),
                maxzoom=int(spec["maxzoom"]),
                where=spec.get("where"),
            ))
        if bands:
            out[bucket] = bands
    return out


def assign_band(feature: dict[str, Any], bands: list[Band]) -> Band | None:
    """Return the FIRST band whose where-predicate matches the feature,
    or ``None`` when no band matches (caller drops the feature)."""
    for band in bands:
        if band.matches(feature):
            return band
    return None
