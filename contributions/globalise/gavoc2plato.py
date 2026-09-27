"""GAVOC atlas index CSV <-> PLATO place-centric JSON, and a round-trip check.

    python3 gavoc2plato.py forward gavoc-atlas-index.csv out.json
    python3 gavoc2plato.py compare gavoc-atlas-index.csv roundtripped.json|.jsonl

One index row = one attestation (@id carries the row id). Rows are grouped into SpatialEntities by
(present name, coordinates): the atlas editors' identification, and the same grouping the site's
"concepts" use. The coordinate string is kept verbatim as the geometry's sourceLabel, beside the
parsed numbers; a string that cannot be parsed ('??', '03-47S/13038E') is a geometry with only its
sourceLabel. The compare re-parses the text and requires it to agree with the numbers.
"""
import csv, json, re, sys, collections, hashlib

NS = "https://necessaryreunions.org/gavoc/"
V = "https://id.necessaryreunions.org/vocab/"
ATLAS = {"title": "Grote atlas van de Verenigde Oost-Indische Compagnie, vol. I: Atlas Isaak de Graaf",
         "citation": "Schilder, Moerman, Ormeling, van den Brink & Ferwerda. Voorburg: Asia Maior, 2006. ISBN 9789074861137"}
C = ["id", "Index page", "Oorspr. naam op de kaart/Original name on the map", "Tegenwoordige naam/Present name",
     "Soortnaam/Category", "Coördinaten/Coordinates", "Kaartvak/Map grid square", "Kaart/Map", "Pagina/Page"]
PART = re.compile(r"^(\d{1,3})(?:-(\d{1,2}))?(?:-(\d{1,2}))?\s*([NSEW])$")


def dms(tok):
    m = PART.match(tok.replace(" ", ""))
    if not m:
        return None
    d, mi, s, h = m.groups()
    v = int(d) + int(mi or 0) / 60 + int(s or 0) / 3600
    return (-v if h in "SW" else v), h


def parse(coord):
    """'09-58N/76-17E' -> [(lon, lat)], or [] if absent, or None if unparseable."""
    c = coord.strip()
    if c in ("", "-"):
        return []
    pts = []
    for part in c.split("+"):
        ll = part.strip().split("/")
        if len(ll) != 2:
            return None
        a, b = dms(ll[0]), dms(ll[1])
        if not a or not b or a[1] not in "NS" or b[1] not in "EW" or abs(a[0]) > 90 or abs(b[0]) > 180:
            return None
        pts.append((round(b[0], 6), round(a[0], 6)))
    return pts


def same(p, q):
    """Two [lon, lat] pairs equal to 15 significant digits."""
    return q is not None and len(q) == 2 and all(f"{float(a):.15g}" == f"{float(b):.15g}" for a, b in zip(p, q))


def precision(coord):
    """Degree-minute(-second) values are good to a minute of arc, ~1.85 km; whole degrees to ~111 km."""
    return 1.85 if re.search(r"\d-\d", coord) else 111.0


TITLE = "GAVOC atlas index (prototype conversion)"


def forward(src, out):
    R = list(csv.DictReader(open(src, encoding="utf-8")))
    groups, stats = collections.OrderedDict(), collections.Counter()
    for r in R:
        present, coord = r[C[3]].strip(), r[C[5]].strip()
        key = (present, coord) if present not in ("", "-") else ("row", r["id"])  # no identification: its own entity
        g = groups.setdefault(key, {"@id": NS + "e/" + hashlib.sha1(repr(key).encode()).hexdigest()[:12],
                                    "label": present if present not in ("", "-") else r[C[2]].strip(), "attestations": []})
        loc = f"map {r[C[7]].strip()}, square {r[C[6]].strip()}, {r[C[8]].strip()} (index p. {r[C[1]].strip()})"
        a = {"@id": f"{NS}row/{r['id']}", "names": [{"toponym": r[C[2]].strip()}],
             "citations": [{"source": ATLAS, "locator": loc}]}
        if r[C[4]].strip() not in ("", "-"):
            a["types"] = [{"label": r[C[4]].strip(), "sourceLabel": r[C[4]].strip()}]
        pts = parse(coord)
        if pts:
            gj = {"type": "Point", "coordinates": list(pts[0])} if len(pts) == 1 else {"type": "MultiPoint", "coordinates": [list(p) for p in pts]}
            # degree-minute(-second) values: a minute of arc is ~1.85 km
            a["geometries"] = [{"geojson": gj, "reprPoint": list(pts[0]), "sourceLabel": coord, "precisionKm": [precision(coord)]}]
            stats["parsed" if len(pts) == 1 else "multipoint"] += 1
        elif pts is None:  # printed but not parseable ('??', '03-47S/13038E'): the words only
            a["geometries"] = [{"sourceLabel": coord}]
            stats["unparseable"] += 1
        else:
            stats["absent"] += 1
        g["attestations"].append(a)
        # The present name is the atlas editors' modern identification, printed in the index: read in
        # the cited source, so Attested; the /present @id is what tells it from the name on the map.
        if r[C[3]].strip() not in ("", "-"):
            g["attestations"].append({"@id": f"{NS}row/{r['id']}/present", "names": [{"toponym": r[C[3]].strip()}],
                                      "formStatus": "https://w3id.org/plato#Attested",
                                      "citations": [{"source": ATLAS, "locator": loc}]})
    doc = {"$schema": "https://w3id.org/plato/schemas/place-centric.schema.json", "profile": "place-centric",
           "gazetteer": {"@id": NS, "title": TITLE}, "spatialEntities": list(groups.values())}
    json.dump(doc, open(out, "w"), ensure_ascii=False)
    print(f"{len(R)} rows -> {len(groups)} spatial entities; coordinates {dict(stats)}")
    bad = [r[C[5]] for r in R if parse(r[C[5]]) is None]
    print("unparseable coordinate strings (kept verbatim only):", len(bad), collections.Counter(bad).most_common(6))


def compare(src, rt):
    R = list(csv.DictReader(open(src, encoding="utf-8")))
    txt = open(rt).read()
    try:
        doc = json.loads(txt); head, ses = doc.get("gazetteer", {}), doc["spatialEntities"]
    except json.JSONDecodeError:
        lines = [json.loads(l) for l in txt.splitlines() if l.strip()]
        head, ses = lines[0].get("gazetteer", {}), lines[1:]
    rows, disagree = {}, []
    if head.get("title") != TITLE:
        disagree.append(("gazetteer", head))
    for e in ses:
        # The entity label is the present name, or the name on the map where there is none.
        names = {a["@id"].endswith("/present"): a["names"][0]["toponym"] for a in e.get("attestations", [])}
        if e.get("label") != names.get(True, names.get(False)):
            disagree.append((e["@id"], "label", e.get("label"), names))
        for a in e.get("attestations", []):
            m = re.match(re.escape(NS) + r"row/(\d+)(/present)?$", a["@id"])
            r = rows.setdefault(m.group(1), {"id": m.group(1)})
            c = a["citations"][0]
            if c.get("source") != ATLAS:
                disagree.append((m.group(1), "source", c.get("source")))
            loc = re.match(r"map (.*), square (.*), (.*) \(index p\. (.*)\)$", c["locator"])
            new = {C[7]: loc.group(1), C[6]: loc.group(2), C[8]: loc.group(3), C[1]: loc.group(4)}
            if any(k in r and r[k] != v for k, v in new.items()):  # both names of a row cite one locator
                disagree.append((m.group(1), "locator", c["locator"]))
            r.update(new)
            if m.group(2):
                r[C[3]] = a["names"][0]["toponym"]
            else:
                r[C[2]] = a["names"][0]["toponym"]
                ty = (a.get("types") or [{}])[0]
                if ty.get("label") != ty.get("sourceLabel"):
                    disagree.append((m.group(1), "type", ty))
                r[C[4]] = ty.get("sourceLabel", "-")
                gm = (a.get("geometries") or [{}])[0]
                r[C[5]] = gm.get("sourceLabel", "-")
                # The printed text and the stored numbers must agree: re-parse one, compare to the other.
                pts = parse(r[C[5]]) if gm else []
                if pts:
                    gj = gm.get("geojson") or {}
                    got = [gj.get("coordinates")] if gj.get("type") == "Point" else gj.get("coordinates")
                    ok = got is not None and len(got) == len(pts) and all(same(u, v) for u, v in zip(pts, got)) and same(pts[0], gm.get("reprPoint")) \
                        and gm.get("precisionKm") == [precision(r[C[5]])]
                else:
                    ok = not any(k in gm for k in ("geojson", "reprPoint", "precisionKm"))
                if not ok:
                    disagree.append((m.group(1), "coordinates", r[C[5]], gm.get("geojson"), gm.get("reprPoint")))
                r.setdefault(C[3], "-")
    total = diffs = 0
    for o in R:
        b = rows.get(o["id"], {})
        for k in C:
            total += 1
            ov = o[k].strip() or "-"
            if (b.get(k) or "-").strip() != ov:
                diffs += 1
                if diffs <= 10:
                    print(f"  DIFF row {o['id']} {k}: {o[k]!r} != {b.get(k)!r}")
    for d in disagree[:10]:
        print("  CARRIED TWICE, DISAGREES", d)
    print(f"compared {total} cells in {len(R)} rows: {diffs} differ (empty and '-' treated alike); "
          f"{len(disagree)} values whose two carriers disagree")
    return diffs + len(disagree)


if __name__ == "__main__":
    sys.exit({"forward": forward, "compare": compare}[sys.argv[1]](*sys.argv[2:]) and 1 or 0)
