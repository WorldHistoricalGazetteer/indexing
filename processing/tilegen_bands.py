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
``tileserver/whg-context.style.json`` — adding a new band means editing
that JSON, no code change required.

Where-clause format (one supported shape, kept simple by design):
``{"property": <str>, "in": [<str>, ...]}`` matches features whose
property equals one of the listed values. ``where: null`` matches every
feature (used by single-band buckets).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Default location of the canonical style. Override by passing an
# explicit path to ``load_bands``.
DEFAULT_STYLE_PATH = (
    Path(__file__).parent.parent / "tileserver" / "whg-context.style.json"
)


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


def load_bands(
    style_path: Path | str | None = None,
) -> dict[str, list[Band]]:
    """Return ``{bucket_name: [Band, ...]}`` for every bucket declared in
    the style's ``metadata.whg:tilegen.buckets`` block. Buckets not
    listed there get no entry — callers fall back to the legacy
    single-bucket behaviour.

    Returns an empty dict on missing file / missing block. Callers must
    handle that as "no bands configured" (legacy single-pass mode).
    """
    path = Path(style_path) if style_path else DEFAULT_STYLE_PATH
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            style = json.load(f)
    except (OSError, json.JSONDecodeError):
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
