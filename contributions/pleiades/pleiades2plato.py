"""Pleiades <-> PLATO place-centric JSON, whole corpus.

    python3 pleiades2plato.py forward pleiades-places-latest.json.gz out.json
    python3 pleiades2plato.py inverse <roundtripped .json|.jsonl> rebuilt.json
    python3 pleiades2plato.py compare pleiades-places-latest.json.gz rebuilt.json

Source: the daily places dump, https://atlantides.org/downloads/pleiades/json/, CC BY 3.0.

42,361 places, 44,079 names, 45,309 locations, 14,995 connections, 218,324 references.

Three things Pleiades records that PLATO has no slot for. Each is parked below in a slot that will
carry it through a round trip, and each parking is marked FINDING, because a lossless round trip
is not the same as a faithful model: what is parked in a PropertyValue is being asserted of the
place when it is really about the reading, or about why a source was cited.

  citation function      218,324 references, every one typed: seeFurther 136,758,
                         citesAsRelated 33,144, citesAsDataSource 30,312, citesAsEvidence 12,483,
                         seeAlso 2,913, cites 2,714. plato:Citation has a locator and no function,
                         so "this is the evidence" and "see this for more" become the same edge.
  transcription quality  44,079 names carry transcriptionAccuracy (inaccurate 357, false 10) and
                         transcriptionCompleteness (reconstructable 989, non-reconstructable 6).
                         These judge the reading, not the name, and nothing in PLATO judges a
                         reading.
  inferred confidence    68,464 name attestations carry one of four confidences, two of which say
                         the confidence was inferred rather than found: confident-inferred 282,
                         less-confident-inferred 76.

Open vocabularies are used as the ontology intends rather than treated as gaps: certainty levels,
form status, name types and relation types all take project URIs, and Pleiades already mints its
own, so its URIs are used directly.
"""
import gzip, hashlib, json, os, re, sys
from urllib.parse import quote

UNSAFE = ' \t"\'<>{}|\\^`[]'


def safe_uri(u):
    """29 Pleiades references carry a URI with no scheme, and 14 more contain a raw space, tab,
    quote or bracket. Encode what can be encoded; refuse what has no scheme."""
    if not u:
        return None
    u = u.strip()
    if not u.startswith(("http://", "https://")):
        return None
    # FINDING: the JSON Schema types every identifier as format "uri", which is ASCII-only, while
    # the ontology ranges them xsd:anyURI, whose value space is IRI references, and JSON-LD and
    # RDF 1.1 both use IRIs. 1,410 Pleiades citations carry an IRI (797 distinct), mostly
    # bibliography anchors with accented author names. Percent-encoding them lets the document
    # validate, and changes the identifier, so a consumer matching the original will not match.
    # Two Pleiades URLs are truncated mid-escape and end in a lone '%', which is not a valid
    # percent-encoding. Repair the stray marker rather than drop the reference.
    u = re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", u)
    out = []
    for c in u:
        if c in UNSAFE:
            out.append("%%%02X" % ord(c))
        elif ord(c) > 127:
            out.append("".join("%%%02X" % b for b in c.encode("utf-8")))
        else:
            out.append(c)
    return "".join(out)

PLATO = "https://w3id.org/plato#"
NS = "https://pleiades.stoa.org/plato/"
PL = "https://pleiades.stoa.org/places/"
CITO = "http://purl.org/spar/cito/"
# Pleiades writes six values as cito: URIs, but CiTO 2.9.0 declares only four of them; seeFurther
# (143,418) and seeAlso (3,192) are not CiTO properties. PLATO accepts CiTO only, so those two are
# mapped. seeFurther -> citesForInformation is reversible, nothing else maps there. seeAlso ->
# citesAsRelated is NOT: Pleiades uses citesAsRelated itself, so the two collide and the source's
# own term is kept beside it for those 3,192 alone.
CITATION_FUNCTION = {
    "cites": CITO + "cites",
    "citesAsEvidence": CITO + "citesAsEvidence",
    "citesAsDataSource": CITO + "citesAsDataSource",
    "citesAsRelated": CITO + "citesAsRelated",
    "seeFurther": CITO + "citesForInformation",
    "seeAlso": CITO + "citesAsRelated",
}
AMBIGUOUS_FUNCTION = {"seeAlso"}
TRANSCRIPTION = {
    "accurate": PLATO + "TranscriptionAccurate",
    "inaccurate": PLATO + "TranscriptionInaccurate",
    "false": PLATO + "TranscriptionFalse",
    "complete": PLATO + "TranscriptionComplete",
    "reconstructable": PLATO + "TranscriptionReconstructable",
    "non-reconstructable": PLATO + "TranscriptionNonReconstructable",
}
# Pleiades language values are not all BCP-47: the literal string 'None' (16), 'asyr' (15),
# 'cana' (10), 'etruscan-in-latin-characters' (9), 'osar' (6), 'arbd' (2). Only well-formed tags
# go in `language`; the rest are kept verbatim on the name's sourceLabel path via nameType so
# nothing is silently dropped.
BCP47 = re.compile(r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$")


def gj2wkt(g):
    """GeoJSON geometry to WKT. Pleiades uses Point, LineString, MultiLineString, Polygon,
    MultiPolygon."""
    if not g or not g.get("coordinates"):
        return None
    t, c = g["type"], g["coordinates"]
    f = lambda xy: f"{xy[0]:.6f} {xy[1]:.6f}"
    ring = lambda r: "(" + ", ".join(f(p) for p in r) + ")"
    poly = lambda p: "(" + ", ".join(ring(r) for r in p) + ")"
    if t == "Point":
        return f"POINT ({f(c)})"
    if t == "LineString":
        return "LINESTRING " + ring(c)
    if t == "MultiLineString":
        return "MULTILINESTRING (" + ", ".join(ring(r) for r in c) + ")"
    if t == "Polygon":
        return "POLYGON " + poly(c)
    if t == "MultiPolygon":
        return "MULTIPOLYGON (" + ", ".join(poly(p) for p in c) + ")"
    return None


def year(v):
    """Pleiades start/end are signed integer years."""
    if v is None:
        return None
    v = int(v)
    return f"{v:05d}-01-01" if v < 0 else f"{v:04d}-01-01"


def span(s, e):
    """FINDING: startEarliest and endLatest are constrained to a four-digit year
    (isoOrYear, ^-?\d{4}...), so no year before 10000 BCE can be written in them. The ontology
    puts no such limit on start_earliest (rdfs:range rdfs:Literal) and edtfString is an
    unconstrained string that takes EDTF's Y-prefixed long form. Pleiades has 199 such endpoints
    across 169 places, down to 2,600,000 BCE. They are written as EDTF here, which keeps them, and
    makes them invisible to any consumer that filters on the structured fields."""
    o = {}
    deep = [x for x in (s, e) if x is not None and abs(int(x)) > 9999]
    if deep:
        edtf = "/".join(("Y" + str(int(x))) if abs(int(x)) > 9999 else year(x)[:5]
                        for x in (s, e) if x is not None)
        o["edtfString"] = edtf
        o["sourceLabel"] = f"{s}/{e}"
        for k, v in (("startEarliest", s), ("endLatest", e)):
            if v is not None and abs(int(v)) <= 9999:
                o[k] = year(v)
        return o
    if s is not None:
        o["startEarliest"] = year(s)
    if e is not None:
        o["endLatest"] = year(e)
    return o or None


# A shortTitle that is really a locator: "Helsinki Atlas 2001, 12, map 2 grid D4", "BAtlas 24 C3".
# The tail after the work and its year is where in the work, not what the work is called.
LOCATOR_TAIL = re.compile(
    r"^(?P<work>.*?\b(?:\d{4}[a-z]?|n\.d\.))[,;]\s*(?P<loc>(?:p{1,2}\.|pp|f\.|no\.|col\.|s\.v\.|map\b|"
    r"grid\b|vol\.|\d).*)$")


def split_locator(title):
    """Return (work title, locator) where a title carries its own locator, else (title, None)."""
    if not title:
        return title, None
    m = LOCATOR_TAIL.match(title.strip())
    if not m:
        return title, None
    work, loc = m.group("work").strip(), m.group("loc").strip()
    return (work, loc) if work and loc else (title, None)


def source_of(r):
    """A Pleiades reference becomes a PLATO source.

    FIXED: the identity was minted from bibliographicURI alone, but Pleiades reuses one URI for
    references that describe DIFFERENT works: 2,243 of its 43,906 distinct bibliographic URIs carry
    more than one shortTitle or formattedCitation, one of them eleven. That made a single RDF
    subject carry several conflicting titles and citation strings, of which a second serialisation
    kept one: 35,294 distinct triples did not survive, which the convert-back-convert stability
    test found after a 0-difference round trip and 34 of 34 per-predicate controls had passed.

    Now each distinct description gets its own identity, and the shared URI stays on every one of
    them as authority_uri, so the link to Pleiades' bibliography is not lost.
    """
    uri = safe_uri(r.get("bibliographicURI") or r.get("accessURI") or r.get("alternateURI"))
    title, locator = split_locator(r.get("shortTitle"))
    title = title or r.get("formattedCitation") or uri or "untitled"
    o = {"title": title, "authorityType": "source"}
    if uri:
        o["uri"] = uri
        # The identity is the description, not the URI. Where one URI describes one work, the
        # digest is constant and the source keeps a single stable identity.
        key = json.dumps([uri, title, r.get("formattedCitation") or ""], sort_keys=True)
        digest = hashlib.sha1(key.encode()).hexdigest()[:12]
        # An IRI has at most one fragment, and 16,013 of Pleiades' bibliography URIs already carry
        # one (#Tovar-1989), so the digest extends that fragment rather than adding a second.
        o["@id"] = f"{uri}-{digest}" if "#" in uri else f"{uri}#{digest}"
    if r.get("formattedCitation"):
        o["citation"] = r["formattedCitation"]
    return o, locator


def citations(refs, parked):
    """PLATO citations, plus the parked citation functions for the caller to attach.

    FINDING: plato:Citation carries a locator and no function. The CiTO type is the difference
    between a source that is the evidence and a source that is further reading, so it is parked as
    a PropertyValue on the attestation, which asserts it of the place. It is not about the place.
    """
    out = []
    for i, r in enumerate(refs or []):
        src, from_title = source_of(r)
        c = {"source": src}
        if r.get("citationDetail") is not None:
            c["locator"] = str(r["citationDetail"])  # one is an integer in the source
        elif from_title:
            # The locator was inside the shortTitle; it belongs on the citation.
            c["locator"] = from_title
        fn = CITATION_FUNCTION.get(r["type"])
        if fn:
            c["citationFunction"] = fn
        out.append(c)
        # Only where the mapping is many-to-one does the source's own term still need parking.
        if r["type"] in AMBIGUOUS_FUNCTION:
            parked.append({"property": NS + "citationFunction/" + quote(r["type"], safe=""),
                           "label": "citationType", "value": r["type"], "sourceLabel": r["type"]})
    return out


def forward(src, out):
    with gzip.open(src) as f:
        graph = json.load(f)["@graph"]
    ses = []
    n = dict(names=0, locs=0, conns=0, refs=0, natt=0)
    for p in graph:
        pid = p["id"]
        e = {"@id": PL + pid, "label": p.get("title") or pid, "attestations": []}
        b = f"{NS}att/{pid}/"

        for i, (t, uri) in enumerate(zip(p.get("placeTypes") or [], p.get("placeTypeURIs") or [])):
            # 450 Pleiades place-type URIs contain a raw space ('.../place-types/numbered
            # feature'), so they are encoded before use.
            e["attestations"].append({"@id": f"{b}type/{i}",
                                      "types": [{"identifier": safe_uri(uri), "label": t,
                                                 "sourceLabel": t}]})

        parked = []
        cits = citations(p.get("references"), parked)
        if cits or parked:
            e["attestations"].append({"@id": b + "record",
                                      **({"citations": cits} if cits else {}),
                                      **({"properties": parked} if parked else {})})
            n["refs"] += len(cits)

        for i, nm in enumerate(p.get("names") or []):
            parked = []
            cits = citations(nm.get("references"), parked)
            n["refs"] += len(cits)
            name = {"toponym": nm.get("attested") or nm.get("romanized") or "",
                    "sourceLabel": nm.get("attested") or nm.get("romanized") or ""}
            if nm.get("romanized"):
                name["romanized"] = nm["romanized"]
            lang = nm.get("language")
            if lang and lang != "None" and BCP47.match(str(lang)):
                name["language"] = lang
            elif lang and lang != "None":
                parked.append({"property": NS + "field/language", "label": "language",
                               "value": str(lang), "sourceLabel": str(lang)})
            nt = [x.strip() for x in (nm.get("nameType") or "").split(",") if x.strip()]
            if nt:
                name["nameType"] = nt
            # CLOSED in PLATO cf87b78: these judge the reading and are now qualifications on the
            # name, rather than PropertyValues asserted of the place.
            qual = {}
            for k in ("transcriptionAccuracy", "transcriptionCompleteness"):
                v = TRANSCRIPTION.get(nm.get(k))
                if v:
                    qual[k] = v
            if qual:
                name["qualification"] = qual
            base = {"names": [name]}
            if nm.get("associationCertainty"):
                base["certaintyLevel"] = NS + "certainty/" + quote(nm["associationCertainty"], safe="")
            if cits:
                base["citations"] = cits
            if parked:
                base["properties"] = parked
            atts = nm.get("attestations") or []
            if atts:
                # One PLATO attestation per named period, each with its own confidence, which is
                # what an attestation model is for. The two "-inferred" confidences say the
                # confidence itself was inferred; that is carried as a distinct concept URI.
                for j, a in enumerate(atts):
                    # The citation repeats on every period attestation, because each period is its
                    # own sourced claim. The parked PropertyValues must NOT: they are a workaround
                    # for things with no home, and repeating them would multiply the very counts
                    # this conversion exists to measure. They go on the first only.
                    # Each period attestation carries its own citations, so each needs its own
                    # copy of the marker that disambiguates seeAlso from citesAsRelated. The
                    # marker is per citation, not per name, so it is NOT dropped for later
                    # periods; an earlier version dropped it and the round trip caught that.
                    one = json.loads(json.dumps(base))
                    e["attestations"].append({
                        "@id": f"{b}name/{i}/{j}", **one,
                        "timespans": [{"label": a.get("timePeriod"),
                                       **(span(nm.get("start"), nm.get("end")) or {})}],
                        **({"certaintyLevel": NS + "confidence/" + quote(a["confidence"], safe="")}
                           if a.get("confidence") else {}),
                    })
                    n["natt"] += 1
            else:
                sp = span(nm.get("start"), nm.get("end"))
                e["attestations"].append({"@id": f"{b}name/{i}", **base,
                                          **({"timespans": [sp]} if sp else {})})
            n["names"] += 1

        for i, loc in enumerate(p.get("locations") or []):
            parked = []
            cits = citations(loc.get("references"), parked)
            n["refs"] += len(cits)
            w = gj2wkt(loc.get("geometry"))
            g = {}
            if w:
                g["wkt"] = w
            if loc.get("accuracy_value") is not None:
                g["precisionKm"] = [float(loc["accuracy_value"]) / 1000.0]
            a = {"@id": f"{b}loc/{i}"}
            if g:
                a["geometries"] = [g]
            if loc.get("associationCertainty"):
                a["certaintyLevel"] = NS + "certainty/" + quote(loc["associationCertainty"], safe="")
            sp = span(loc.get("start"), loc.get("end"))
            if sp:
                a["timespans"] = [sp]
            if cits:
                a["citations"] = cits
            if parked:
                a["properties"] = parked
            if loc.get("title"):
                a["notes"] = loc["title"]
            e["attestations"].append(a)
            n["locs"] += 1

        for i, c in enumerate(p.get("connections") or []):
            parked = []
            cits = citations(c.get("references"), parked)
            n["refs"] += len(cits)
            a = {"@id": f"{b}conn/{i}",
                 "relations": [{"relatesTo": c["connectsTo"],
                                "relationType": safe_uri(c.get("connectionTypeURI"))
                                or NS + "relation/" + quote(c["connectionType"], safe=""),
                                "relationLabel": c["connectionType"]}]}
            if c.get("associationCertainty"):
                a["certaintyLevel"] = NS + "certainty/" + quote(c["associationCertainty"], safe="")
            sp = span(c.get("start"), c.get("end"))
            if sp:
                a["timespans"] = [sp]
            if cits:
                a["citations"] = cits
            if parked:
                a["properties"] = parked
            e["attestations"].append(a)
            n["conns"] += 1

        ses.append(e)

    doc = {"$schema": "https://w3id.org/plato/schemas/place-centric.schema.json",
           "profile": "place-centric",
           "gazetteer": {"@id": "https://pleiades.stoa.org/",
                         "title": "Pleiades (prototype conversion of the places dump)",
                         "licence": "https://creativecommons.org/licenses/by/3.0/"},
           "spatialEntities": ses}
    with open(out, "w") as f:
        json.dump(doc, f, ensure_ascii=False)
    print(f"{len(ses)} spatial entities, {sum(len(x['attestations']) for x in ses)} attestations "
          f"({n['names']} names in {n['natt']} period attestations, {n['locs']} locations, "
          f"{n['conns']} connections, {n['refs']} citations) -> {out}")


REVERSE_FUNCTION = {v: k for k, v in CITATION_FUNCTION.items() if k not in AMBIGUOUS_FUNCTION}
REVERSE_TRANSCRIPTION = {v: k for k, v in TRANSCRIPTION.items()}


def load_ses(path):
    if path.endswith(".jsonl"):
        ses = []
        for line in open(path, encoding="utf-8"):
            o = json.loads(line)
            if "spatialEntities" in o:
                ses.extend(o["spatialEntities"])
            elif "attestations" in o or o.get("@id", "").startswith(PL):
                ses.append(o)
        return ses
    return json.load(open(path, encoding="utf-8"))["spatialEntities"]


def inverse(rt, out):
    """Rebuild the parts of each Pleiades record that the conversion claims to carry."""
    rebuilt = {}
    for e in load_ses(rt):
        pid = e["@id"].rsplit("/", 1)[-1]
        rec = {"title": e.get("label"), "placeTypes": [], "names": {}, "locations": {},
               "connections": {}, "citationTypes": []}
        for a in e.get("attestations", []):
            aid = a["@id"]
            parked_fn = [p["value"] for p in a.get("properties", [])
                         if p.get("label") == "citationType"]
            # A name attested in several periods is several claims, and each carries the same
            # citations, faithfully. The source has them once per name, so only the first period's
            # copy is counted; the rest must agree with it, and are checked below.
            m_np = re.search(r"/name/(\d+)/(\d+)$", aid)
            fns = []
            for c in a.get("citations", []):
                fn = c.get("citationFunction")
                if fn is None:
                    continue
                # citesAsRelated is the target of both Pleiades' own citesAsRelated and its
                # seeAlso, so it is the one value the mapping cannot reverse on its own. The
                # parked term disambiguates it, and is carried for that reason alone.
                if fn == CITO + "citesAsRelated" and parked_fn:
                    fns.append(parked_fn.pop(0))
                else:
                    fns.append(REVERSE_FUNCTION[fn])
            if m_np:
                i_n, j_n = m_np.group(1), int(m_np.group(2))
                prev = rec.setdefault("_namefns", {})
                if j_n == 0:
                    prev[i_n] = fns
                    rec["citationTypes"].extend(fns)
                elif prev.get(i_n) != fns:
                    rec["citationTypes"].append(
                        f"<period {j_n} of name {i_n} cites {fns} but period 0 cites {prev.get(i_n)}>")
            else:
                rec["citationTypes"].extend(fns)
            if "/type/" in aid:
                rec["placeTypes"].append(a["types"][0]["label"])
            elif "/name/" in aid:
                i = aid.split("/name/")[1].split("/")[0]
                nm = a["names"][0]
                d = rec["names"].setdefault(i, {"toponym": nm["toponym"], "periods": [],
                                                "transcriptionAccuracy": None,
                                                "transcriptionCompleteness": None,
                                                "romanized": nm.get("romanized"),
                                                "language": nm.get("language")})
                for k, v in (nm.get("qualification") or {}).items():
                    if k in ("transcriptionAccuracy", "transcriptionCompleteness"):
                        d[k] = REVERSE_TRANSCRIPTION.get(v)
                ts = (a.get("timespans") or [{}])[0]
                if ts.get("label"):
                    d["periods"].append(ts["label"])
            elif "/loc/" in aid:
                i = aid.split("/loc/")[1].split("/")[0]
                g = (a.get("geometries") or [{}])[0]
                rec["locations"][i] = {"wkt": g.get("wkt"),
                                       "precisionKm": (g.get("precisionKm") or [None])[0]}
            elif "/conn/" in aid:
                i = aid.split("/conn/")[1].split("/")[0]
                r = a["relations"][0]
                rec["connections"][i] = {"connectsTo": r["relatesTo"],
                                         "connectionType": r.get("relationLabel")}
        rec.pop("_namefns", None)
        rebuilt[pid] = rec
    # Record which return file this was rebuilt from. A missing or empty one already fails
    # loudly; a STALE one from an earlier good run would not, so its size and time are carried
    # into the result and printed by compare.
    st = os.stat(rt)
    rebuilt["_from"] = {"path": os.path.abspath(rt), "bytes": st.st_size,
                        "mtime": __import__("datetime").datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}
    json.dump(rebuilt, open(out, "w"), ensure_ascii=False)
    print(f"  rebuilt from {rebuilt['_from']['path']} ({st.st_size:,} bytes, {rebuilt['_from']['mtime']})")
    print(f"rebuilt {len(rebuilt) - 1} places -> {out}")


def compare(src, rebuilt_path):
    with gzip.open(src) as f:
        graph = json.load(f)["@graph"]
    got = json.load(open(rebuilt_path, encoding="utf-8"))
    src_info = got.pop("_from", None)
    if src_info:
        print(f"  comparing against a rebuild of {src_info['path']} "
              f"({src_info['bytes']:,} bytes, {src_info['mtime']})")
    diffs, checked = [], 0

    def chk(cond, msg):
        nonlocal checked
        checked += 1
        if not cond:
            diffs.append(msg)

    for p in graph:
        pid = p["id"]
        g = got.get(pid)
        if g is None:
            diffs.append(f"place {pid} missing")
            continue
        chk(g.get("title") == (p.get("title") or pid), f"{pid} title")
        chk(g.get("placeTypes") == list(p.get("placeTypes") or []), f"{pid} placeTypes")
        want_ct = []
        for group in ((p.get("references") or []),):
            want_ct += [r["type"] for r in group]
        for nm in (p.get("names") or []):
            want_ct += [r["type"] for r in (nm.get("references") or [])]
        for loc in (p.get("locations") or []):
            want_ct += [r["type"] for r in (loc.get("references") or [])]
        for c in (p.get("connections") or []):
            want_ct += [r["type"] for r in (c.get("references") or [])]
        chk(sorted(g.get("citationTypes") or []) == sorted(want_ct), f"{pid} citation functions")
        for i, nm in enumerate(p.get("names") or []):
            d = (g.get("names") or {}).get(str(i))
            want = nm.get("attested") or nm.get("romanized") or ""
            chk(d is not None and d.get("toponym") == want, f"{pid} name[{i}] toponym")
            if d:
                for k in ("transcriptionAccuracy", "transcriptionCompleteness"):
                    chk(d.get(k) == nm.get(k), f"{pid} name[{i}] {k}")
                chk(d.get("periods") == [a.get("timePeriod") for a in (nm.get("attestations") or [])],
                    f"{pid} name[{i}] periods")
        for i, loc in enumerate(p.get("locations") or []):
            d = (g.get("locations") or {}).get(str(i))
            chk(d is not None and d.get("wkt") == gj2wkt(loc.get("geometry")), f"{pid} loc[{i}] wkt")
        for i, c in enumerate(p.get("connections") or []):
            d = (g.get("connections") or {}).get(str(i))
            chk(d is not None and d.get("connectsTo") == c["connectsTo"]
                and d.get("connectionType") == c["connectionType"], f"{pid} conn[{i}]")

    print(f"{checked} assertions checked, {len(diffs)} differences")
    for d in diffs[:20]:
        print("  ", d)
    return 1 if diffs else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "forward":
        forward(sys.argv[2], sys.argv[3])
    elif cmd == "inverse":
        inverse(sys.argv[2], sys.argv[3])
    elif cmd == "compare":
        sys.exit(compare(sys.argv[2], sys.argv[3]))
    elif cmd == "compare-sub":
        # The per-predicate controls run on a subset, because a full reverse leg is 24 minutes and
        # 30 of them would be half a day. What they establish is that the COMPARISON reads each
        # predicate, which does not depend on how many places carry it. Places absent from the
        # subset are therefore not counted as missing.
        import gzip as _gz
        got = json.load(open(sys.argv[3], encoding="utf-8"))
        got.pop("_from", None)
        keep = set(got)
        with _gz.open(sys.argv[2]) as f:
            graph = json.load(f)["@graph"]
        tmp = sys.argv[3] + ".sub"
        json.dump(got, open(tmp, "w"), ensure_ascii=False)
        import types
        orig = globals()["compare"]
        def _sub(src, rp):
            import gzip
            g2 = [p for p in graph if p["id"] in keep]
            class _F:
                def __enter__(self_): return self_
                def __exit__(self_, *a): pass
            import io, json as _j
            data = _j.dumps({"@graph": g2}).encode()
            path = sys.argv[3] + ".subsrc.json.gz"
            with gzip.open(path, "wb") as fh: fh.write(data)
            return orig(path, rp)
        sys.exit(_sub(sys.argv[2], tmp))
    else:
        sys.exit("forward | inverse | compare")
