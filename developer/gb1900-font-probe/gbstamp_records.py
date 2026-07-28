"""Emit GB-STAMP records as W3C Web Annotations on IIIF canvases, and refuse to emit malformed ones.

NOT a new schema. Text on a map IS an annotation: the target is a region of an image, the body is the
transcribed string plus a gazetteer link plus a classification. That is the shape of the W3C Web Annotation
Data Model, and IIIF addresses the canvases the map libraries we depend on already serve. A parallel
vocabulary would duplicate a mature standard for no gain and strand us outside every IIIF viewer and
annotation tool in existence — including Annotorious and Recogito Studio, which are the human-in-the-loop
correction interface a spotter pipeline needs and which we would otherwise have to build.

Scale caveat, because anyone who knows the standard will ask: annotations are the interchange and provenance
format at the point of EXTRACTION AND CORRECTION. The attestation graph remains the internal model. The
documented mapping between them is the deliverable, not a re-serialisation of the whole index.

PROVENANCE IS THE POINT. W3C already separates `creator` (the agent responsible) from `generator` (the
software), and that separation expresses our three cases without inventing anything:

    volunteer read it            creator only              -- it IS the reference
    detector found it unaided    generator only            -- may be scored against GB1900
    detector prompted by a pin   creator AND generator     -- MUST NOT be scored: circular

The third is the one that matters. A prompted detection's text came FROM GB1900, so scoring it against
GB1900 returns a perfect result by construction. Carrying both agents records WHY it is circular — the human
is in its provenance chain — which a flat enum would not. `scorable_against_gb1900()` is then a property of
the record rather than something a later evaluation has to remember.

See gb-stamp/docs/data-model.md.
"""
import json
import re

CONTEXT = "http://www.w3.org/ns/anno.jsonld"
BASE = "https://whgazetteer.org/gb-stamp/anno"
MOTIVATIONS = ("transcribing", "classifying", "identifying", "linking", "commenting")


def software(uri, name):
    return {"id": uri, "type": "Software", "name": name}


def person(uri_or_name, name=None):
    a = {"type": "Person"}
    if str(uri_or_name).startswith("http"):
        a["id"] = uri_or_name
        if name:
            a["name"] = name
    else:
        a["name"] = uri_or_name
    return a


def svg_selector(polygon):
    pts = " ".join(f"{round(float(x), 2)},{round(float(y), 2)}" for x, y in polygon)
    return {"type": "SvgSelector", "value": f"<svg><polygon points='{pts}'/></svg>"}


def point_selector(xy, crs="EPSG:3857"):
    """A GB1900 pin: a point near a label's START, not its extent. Kept as its own selector so the
    difference is not asserted away — a record may have a pin and no region, or a region and no pin."""
    return {"type": "PointSelector", "x": round(float(xy[0]), 2), "y": round(float(xy[1]), 2),
            "conformsTo": crs}


def textual_body(value, purpose="transcribing", language=None, confidence=None):
    b = {"type": "TextualBody", "purpose": purpose, "value": value, "format": "text/plain"}
    if language:
        b["language"] = language
    if confidence is not None:
        b["confidence"] = round(float(confidence), 4)
    return b


def classifying_body(uri, label=None, confidence=None, alternatives=None):
    """Getty AAT, or one of our face URIs, with the runners-up.

    Several OS writing categories are engraved in an IDENTICAL face and are inseparable by design, so a
    single verdict would be false precision; the alternatives are how that degrades gracefully.
    """
    b = {"purpose": "classifying", "source": uri}
    if label:
        b["label"] = label
    if confidence is not None:
        b["confidence"] = round(float(confidence), 4)
    if alternatives:
        b["alternatives"] = [{"source": u, "confidence": round(float(c), 4)} for u, c in alternatives]
    return b


def identifying_body(uri):
    return {"purpose": "identifying", "source": uri}


def annotation(aid, bodies, target, motivation="transcribing",
               creator=None, generator=None, created=None, generated=None):
    if motivation not in MOTIVATIONS:
        raise ValueError(f"{aid}: motivation {motivation!r} is not a W3C motivation we use")
    a = {"@context": CONTEXT, "id": aid, "type": "Annotation", "motivation": motivation,
         "body": bodies if isinstance(bodies, list) else [bodies], "target": target}
    if creator:
        a["creator"] = creator
    if generator:
        a["generator"] = generator
    if created:
        a["created"] = created
    if generated:
        a["generated"] = generated
    return a


def canvas_target(canvas_uri, selectors):
    sel = selectors if isinstance(selectors, list) else [selectors]
    return {"source": canvas_uri, "selector": sel[0] if len(sel) == 1 else sel}


def ordered_target(member_ids):
    """oa:List — an ORDERED multiplicity. Reading order is part of what a label is: MOOR MIDDLETON is not
    the label MIDDLETON MOOR. W3C already has this construct; we do not need one of our own."""
    if not isinstance(member_ids, (list, tuple)) or not member_ids:
        raise ValueError("a label needs a non-empty ordered member list")
    return {"type": "List", "items": list(member_ids)}


def scorable_against_gb1900(anno):
    """False for anything a human had a hand in — which includes a prompted detection, whose text came
    from GB1900 and would therefore score perfectly against it by construction."""
    return bool(anno.get("generator")) and not anno.get("creator")


def validate(anno):
    """Raise on anything that would mislead downstream. Cheap, so it runs on every write."""
    aid = anno.get("id")
    if not aid or not str(aid).startswith("http"):
        raise ValueError(f"id must be a dereferenceable URI, got {aid!r}")
    if anno.get("type") != "Annotation":
        raise ValueError(f"{aid}: type must be Annotation")
    if anno.get("@context") != CONTEXT:
        raise ValueError(f"{aid}: missing or wrong @context")
    if not anno.get("body"):
        raise ValueError(f"{aid}: an annotation with no body says nothing")
    if not anno.get("target"):
        raise ValueError(f"{aid}: an annotation with no target is not located, so is not anything")
    if not (anno.get("creator") or anno.get("generator")):
        raise ValueError(f"{aid}: needs a creator or a generator — a record whose origin is unknown "
                         f"cannot be trusted, scored, or superseded")
    t = anno["target"]
    if isinstance(t, dict) and t.get("type") == "List" and not t.get("items"):
        raise ValueError(f"{aid}: an empty List target is not a label")
    return anno


class Writer:
    """Newline-delimited JSON, validated on the way out. NDJSON rather than a single annotation page
    because the pipeline streams; a page wrapper can be laid over it at publication."""

    def __init__(self, path):
        self.fh = open(path, "w")
        self.n = 0

    def write(self, anno):
        self.fh.write(json.dumps(validate(anno), ensure_ascii=False) + "\n")
        self.n += 1

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
