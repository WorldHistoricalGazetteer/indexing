"""Emit GB-STAMP records in the interchange shape, and refuse to emit ones that are not.

The shape is our instantiation of the MapText Data Model sketched at the Open Maps Meeting (Nov 2024) —
see gb-stamp/docs/data-model.md. That sketch is a proposal with no serialisation, so this is a concrete
proposal back, and the point of writing it as code rather than prose is that the pipeline then has to obey
it. A data model kept only in a document drifts from what is actually written.

The field that earns its place is `provenance.mode`:

    manual     a GB1900 volunteer read this        — it IS the reference
    automatic  a detector found it unaided         — may be scored against GB1900
    prompted   a detector was told where to look   — MUST NOT be scored against GB1900

A prompted detection's text comes FROM GB1900, so scoring it against GB1900 returns a perfect result by
construction. `scorable_against_gb1900()` makes that a property of the record rather than something a
future evaluation has to remember.
"""
import json
import re

CONTEXT = "https://worldhistoricalgazetteer.github.io/gb-stamp/data-model"
MODES = ("manual", "automatic", "prompted")
TYPES = ("TextAnnotation", "CombinedAnnotation", "SymbolAnnotation")


def _pt(xy):
    return {"type": "Point", "coordinates": [round(float(xy[0]), 2), round(float(xy[1]), 2)]}


def provenance(mode, agent, tool, date=None):
    if mode not in MODES:
        raise ValueError(f"provenance.mode must be one of {MODES}, not {mode!r}")
    p = {"mode": mode, "agent": agent, "tool": tool}
    if date:
        p["date"] = date
    return p


def text_annotation(rid, text, confidence, prov, polygon=None, baseline=None,
                    reference_point=None, semantic_type=None, crs="EPSG:3857"):
    """One word found on the map, or one volunteer transcription.

    A record may legitimately have geometry and no reference point (a detection nobody pinned) or a
    reference point and no geometry (a volunteer pin nothing was found at) — but not neither, because then
    it is not located and cannot be anything.
    """
    if polygon is None and reference_point is None:
        raise ValueError(f"{rid}: an annotation needs a geometry or a reference point")
    r = {"id": rid, "type": "TextAnnotation",
         "transcription": {"text": text, "confidence": round(float(confidence), 4)},
         "provenance": prov}
    tgt = {"crs": crs}
    if polygon is not None:
        tgt["geometry"] = {"type": "Polygon",
                           "coordinates": [[[round(float(x), 2), round(float(y), 2)] for x, y in polygon]]}
    if baseline is not None:
        # Kept separate from the outline: on a curved label the outline's bounding rectangle points ACROSS
        # the curve, so the baseline is the honest carrier of direction.
        tgt["baseline"] = {"type": "LineString",
                           "coordinates": [[round(float(x), 2), round(float(y), 2)] for x, y in baseline]}
    if len(tgt) > 1:
        r["target"] = tgt
    if reference_point is not None:
        r["reference_point"] = _pt(reference_point)
    if semantic_type:
        r["semantic_type"] = semantic_type
    return r


def combined_annotation(rid, items, text, confidence, prov, lines=1,
                        reference_point=None, semantic_type=None):
    """A whole label. `items` is ORDERED: reading order is part of what the label is."""
    if not isinstance(items, (list, tuple)) or len(items) < 1:
        raise ValueError(f"{rid}: a CombinedAnnotation needs an ordered, non-empty item list")
    r = {"id": rid, "type": "CombinedAnnotation", "items": list(items), "lines": int(lines),
         "transcription": {"text": text, "confidence": round(float(confidence), 4)},
         "provenance": prov}
    if reference_point is not None:
        r["reference_point"] = _pt(reference_point)
    if semantic_type:
        r["semantic_type"] = semantic_type
    return r


def semantic_type(label, uri=None, confidence=None, alternatives=None):
    """Getty AAT as label + URI, with the ranked runners-up.

    Several OS writing categories are engraved in an identical face and are inseparable by design, so a
    single verdict would be false precision. The alternatives list is how that degrades gracefully.
    """
    s = {"label": label}
    if uri:
        s["uri"] = uri
    if confidence is not None:
        s["confidence"] = round(float(confidence), 4)
    if alternatives:
        s["alternatives"] = [{"label": a, "confidence": round(float(c), 4)} for a, c in alternatives]
    return s


def scorable_against_gb1900(rec):
    """False for anything whose text came from GB1900 in the first place."""
    return (rec.get("provenance") or {}).get("mode") == "automatic"


def validate(rec):
    """Raise on anything that would be misleading downstream. Cheap, so it runs on every write."""
    rid = rec.get("id")
    if not rid or not re.match(r"^[a-z]+:[a-zA-Z0-9/_.-]+$", str(rid)):
        raise ValueError(f"id must be a namespaced identifier, got {rid!r}")
    if rec.get("type") not in TYPES:
        raise ValueError(f"{rid}: type must be one of {TYPES}")
    p = rec.get("provenance") or {}
    if p.get("mode") not in MODES:
        raise ValueError(f"{rid}: provenance.mode must be one of {MODES}")
    if not p.get("tool"):
        raise ValueError(f"{rid}: provenance.tool is required — a record whose origin is unknown "
                         f"cannot be trusted or superseded")
    t = rec.get("transcription") or {}
    if "text" not in t:
        raise ValueError(f"{rid}: transcription.text is required")
    if rec["type"] == "CombinedAnnotation" and not rec.get("items"):
        raise ValueError(f"{rid}: a CombinedAnnotation without items is not a label")
    if rec["type"] != "CombinedAnnotation" and not (rec.get("target") or rec.get("reference_point")):
        raise ValueError(f"{rid}: needs a target geometry or a reference point")
    return rec


class Writer:
    """Newline-delimited JSON, validated on the way out."""

    def __init__(self, path):
        self.fh = open(path, "w")
        self.n = 0

    def write(self, rec):
        self.fh.write(json.dumps(validate(rec), ensure_ascii=False) + "\n")
        self.n += 1

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
