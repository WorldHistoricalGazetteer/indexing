"""GLOBALISE Places v3.0 workbook (doi:10.34894/UFFFNO) <-> PLATO place-centric JSON.

    python3 glob2plato.py forward places_v3.xlsx out.json
    python3 glob2plato.py compare places_v3.xlsx roundtripped.json|.jsonl

Sheets 3-6 are the record; Sheet 2 is an overview derived from them, Sheet 7 the bibliography.
A citation cell such as "(Coolhaas 1979, 24, 183; van Goor 2004, 202)" becomes one PLATO citation
per source, with its own locator, when splitting and re-joining gives back the exact string;
otherwise it is kept whole as one source title. Attestation @ids carry sheet + row.

Normalisations the compare allows, and nothing else: surrounding whitespace; numbers equal to 15
significant digits (the precision every decimal -> double -> decimal round trip keeps; JSON-LD's
canonical xsd:double keeps 16, which drops the workbook's float noise such as 106.82041100000001);
ccodes compared as a list of codes (the workbook separates them with ',', ', ' or '|').
"""
import json, re, sys
import openpyxl

NS = "https://data.globalise.huygens.knaw.nl/hdl:20.500.14722/"
V = NS + "vocab/"
PLATO = "https://w3id.org/plato#"
LAT = "http://www.w3.org/2003/01/geo/wgs84_pos#lat"
LEVEL = {"certain": PLATO + "Certain", "uncertain": PLATO + "Uncertain"}
WORD = {v: k for k, v in LEVEL.items()}
ITEM = re.compile(r"^(.+?\b(?:\d{4}[a-z]?|n\.d\.))(?:, (.+))?$")
IDCOLS = ["esta_id", "geonames_id", "whg_id", "amh_id", "external_id", "wikidata_id", "tgn_id"]
RELS = {V + "relation/part-of": "Part Of", V + "relation/overlaps": "Overlaps"}


def sheets(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    out = {}
    for ws in wb.worksheets:
        h, *b = list(ws.iter_rows(values_only=True))
        out[ws.title.split()[0]] = [dict(zip(h, r)) for r in b]
    return out


def uri(g):
    return NS + "place:" + g


def num(v):
    return repr(v) if isinstance(v, float) else str(v)


def codes(cell):
    return [c.strip() for c in re.split(r"[,|]", cell)]


def same_num(a, b):
    return f"{float(a):.15g}" == f"{float(b):.15g}"


def split_cite(s):
    if not (s.startswith("(") and s.endswith(")")):
        return None
    out = []
    for part in s[1:-1].split("; "):
        m = ITEM.match(part)
        if not m:
            return None
        out.append((m.group(1), m.group(2)))
    return out if "(" + "; ".join(a + (", " + b if b is not None else "") for a, b in out) + ")" == s else None


def cite(s):
    if not s:
        return {}
    p = split_cite(s)
    if p:
        return {"citations": [{"source": {"title": a}, **({"locator": b} if b is not None else {})} for a, b in p]}
    return {"citations": [{"source": {"title": s}}]}


def uncite(a):
    cs = a.get("citations") or []
    if not cs:
        return None
    parts = [(c["source"]["title"], c.get("locator")) for c in cs]
    joined = "(" + "; ".join(t + (", " + l if l is not None else "") for t, l in parts) + ")"
    return joined if split_cite(joined) else parts[0][0]


def remark(aid, text, src, target):
    return {"@id": aid, **({"notes": text} if text else {}),
            "meta": {"targetAttestation": target, "metaType": PLATO + "Annotates"}, **cite(src)}


def forward(path, out):
    S = sheets(path)
    ents, order = {}, []

    def ent(g, label=None):
        if g not in ents:
            ents[g] = {"@id": uri(g), "label": label or g, "entityIdentifier": g, "namespace": "globalise", "attestations": []}
            order.append(g)
        if label:
            ents[g]["label"] = label
        return ents[g]

    for i, r in enumerate(S["Sheet3"], start=2):
        g = r["glob_id"]; e = ent(g, r["pref_label"]); b = f"{NS}att/s3/{i}/"
        if r["ccodes"]:
            e["ccodes"] = codes(r["ccodes"])
        if r["latitude"] is not None:
            lvl = {"certaintyLevel": LEVEL[r["coord_certainty"].strip()]} if r["coord_certainty"] else None
            if r["longitude"] is not None:
                geom = {"wkt": f"POINT ({num(r['longitude'])} {num(r['latitude'])})", "reprPoint": [r["longitude"], r["latitude"]]}
                if lvl:
                    geom["qualification"] = lvl
                e["attestations"].append({"@id": b + "coord", "geometries": [geom], **cite(r["coord_source"])})
            else:  # a latitude alone is not a location
                pv = {"property": LAT, "label": "latitude", "value": r["latitude"], "sourceLabel": num(r["latitude"])}
                if lvl:
                    pv["qualification"] = lvl
                e["attestations"].append({"@id": b + "coord", "properties": [pv], **cite(r["coord_source"])})
        if r["coord_remarks"] or r["coord_remarks_source"]:
            e["attestations"].append(remark(b + "coord-remark", r["coord_remarks"], r["coord_remarks_source"], b + "coord"))
        if r["overall_remarks"] or r["overall_remarks_source"]:
            e["attestations"].append({"@id": b + "overall", **({"notes": r["overall_remarks"]} if r["overall_remarks"] else {}), **cite(r["overall_remarks_source"])})
        ids = []
        for col in IDCOLS:
            v = r[col].strip() if isinstance(r[col], str) else r[col]  # 4 cells end in a newline or space
            if v:
                obj = v if str(v).startswith("http") else f"{V}ext/{col}/{v}"
                ids.append({"@id": f"{b}identity/{col}", "subject": uri(g), "object": obj, "identityType": "unspecified"})
        if ids:
            e["identityRelations"] = ids

    for i, r in enumerate(S["Sheet4"], start=2):
        b = f"{NS}att/s4/{i}"
        a = {"@id": b, "names": [{"toponym": r["label"]}], "formStatus": PLATO + ("Preferred" if r["label_type"] == "PREF" else "Attested"), **cite(r["label_source"])}
        ent(r["glob_id"])["attestations"].append(a)
        if r["label_remarks"] or r["label_remarks_source"]:
            ent(r["glob_id"])["attestations"].append(remark(b + "/remark", r["label_remarks"], r["label_remarks_source"], b))

    for i, r in enumerate(S["Sheet5"], start=2):
        a = {"@id": f"{NS}att/s5/{i}", "types": [{"label": r["place_type"], "sourceLabel": r["place_type"]}], **cite(r["place_type_source"])}
        if r["place_type_comment"]:
            a["notes"] = r["place_type_comment"]
        ent(r["glob_id"])["attestations"].append(a)

    for i, r in enumerate(S["Sheet6"], start=2):
        ent(r["parent_region"], r["parent_region_pref_label"])  # NEW_ADMIN_* are defined nowhere else
        rt = V + "relation/" + r["relation"].lower().replace(" ", "-")
        a = {"@id": f"{NS}att/s6/{i}", "relations": [{"relatesTo": uri(r["parent_region"]), "relationType": rt, "relationLabel": r["relation"]}], **cite(r["source"])}
        if r["remarks"]:
            a["notes"] = r["remarks"]
        ent(r["glob_id"])["attestations"].append(a)

    ses = [ents[g] for g in order]
    for e in ses:
        if not e["attestations"]:
            del e["attestations"]
    json.dump({"$schema": "https://w3id.org/plato/schemas/place-centric.schema.json", "profile": "place-centric",
               "gazetteer": {"@id": NS, "title": TITLE, "licence": LICENCE},
               "spatialEntities": ses}, open(out, "w"), ensure_ascii=False)
    ncit = sum(len(a.get("citations", [])) for e in ses for a in e.get("attestations", []))
    print(f"{len(ses)} spatial entities, {sum(len(e.get('attestations', [])) for e in ses)} attestations, {ncit} citations -> {out}")


TITLE = "GLOBALISE Places v3.0 (prototype conversion)"
LICENCE = "https://creativecommons.org/licenses/by/4.0/"


def load(rt):
    """(header, entities) from a place-centric JSON document or JSON Lines."""
    txt = open(rt).read()
    try:
        doc = json.loads(txt)
        return doc.get("gazetteer", {}), doc["spatialEntities"]
    except json.JSONDecodeError:
        lines = [json.loads(x) for x in txt.splitlines() if x.strip()]
        return lines[0].get("gazetteer", {}), [l for l in lines[1:] if "subject" not in l]


def compare(path, rt):
    S = sheets(path)
    head, ses = load(rt)
    byuri = {e["@id"]: e for e in ses}
    got, disagree = {}, []
    # Values the workbook does not hold, but the conversion asserts: each must be what was written.
    if head.get("title") != TITLE or head.get("licence") != LICENCE:
        disagree.append(("gazetteer", head))
    for e in ses:
        g = e.get("entityIdentifier")
        if e.get("namespace") != "globalise":
            disagree.append((g, "namespace", e.get("namespace")))
        for a in e.get("attestations") or []:
            if a.get("meta"):
                target = a["@id"].rsplit("/", 1)[0] + ("/coord" if a["@id"].endswith("/coord-remark") else "")
                if a["meta"].get("metaType") != PLATO + "Annotates" or a["meta"].get("targetAttestation") != target:
                    disagree.append((g, "meta", a["meta"]))
            for ty in a.get("types") or []:
                if ty.get("label") != ty.get("sourceLabel"):
                    disagree.append((g, "type", ty))
            for pv in a.get("properties") or []:
                if pv.get("property") != LAT or pv.get("label") != "latitude":
                    disagree.append((g, "property", pv))
        for ir in e.get("identityRelations") or []:
            m = re.match(re.escape(NS) + r"att/s3/(\d+)/identity/(\w+)$", ir["@id"])
            o = ir["object"]; pre = f"{V}ext/{m.group(2)}/"
            got[("Sheet3", int(m.group(1)), m.group(2))] = o[len(pre):] if o.startswith(pre) else o
        for a in e.get("attestations") or []:
            m = re.match(re.escape(NS) + r"att/(s\d)/(\d+)/?(.*)$", a["@id"]); sh, row, part = "Sheet" + m.group(1)[1], int(m.group(2)), m.group(3)
            put = lambda k, v: got.__setitem__((sh, row, k), v)
            put("glob_id", g)
            put("pref_label", e["label"])
            if sh == "Sheet3":
                if part == "coord":
                    if a.get("geometries"):
                        gm = a["geometries"][0]
                        wm = re.fullmatch(r"POINT \((\S+) (\S+)\)", gm["wkt"])
                        if not wm:
                            disagree.append((e["label"], "wkt", gm["wkt"])); continue
                        lon, lat = wm.groups()
                        # The parsed numbers and the text must agree, or the inverse is not checking both.
                        if not (same_num(lon, gm["reprPoint"][0]) and same_num(lat, gm["reprPoint"][1])):
                            disagree.append((e["label"], gm["wkt"], gm["reprPoint"]))
                        put("longitude", lon)
                    else:
                        gm = a["properties"][0]; lat = gm["sourceLabel"]
                        if not same_num(lat, gm["value"]):
                            disagree.append((e["label"], lat, gm["value"]))
                    put("latitude", lat); put("coord_source", uncite(a))
                    lv = (gm.get("qualification") or {}).get("certaintyLevel")
                    put("coord_certainty", WORD[lv] if lv else None)
                elif part == "coord-remark":
                    put("coord_remarks", a.get("notes")); put("coord_remarks_source", uncite(a))
                elif part == "overall":
                    put("overall_remarks", a.get("notes")); put("overall_remarks_source", uncite(a))
            elif sh == "Sheet4":
                if part == "remark":
                    put("label_remarks", a.get("notes")); put("label_remarks_source", uncite(a))
                else:
                    put("label", a["names"][0]["toponym"]); put("label_source", uncite(a))
                    put("label_type", {PLATO + "Preferred": "PREF", PLATO + "Attested": "ALT"}.get(a.get("formStatus")))
            elif sh == "Sheet5":
                put("place_type", a["types"][0]["sourceLabel"])
                put("place_type_comment", a.get("notes")); put("place_type_source", uncite(a))
            elif sh == "Sheet6":
                rel = a["relations"][0]; tgt = byuri[rel["relatesTo"]]
                if rel.get("relationLabel") != RELS.get(rel["relationType"]):  # the wording and the type must agree
                    disagree.append((e["label"], rel.get("relationLabel"), rel["relationType"]))
                put("relation", RELS.get(rel["relationType"])); put("parent_region", tgt.get("entityIdentifier"))
                put("parent_region_pref_label", tgt["label"])
                put("remarks", a.get("notes")); put("source", uncite(a))
    # A Sheet 3 row's id, label and country codes sit on the SpatialEntity itself, which a row with
    # no coordinates, remarks or links reaches by no attestation: look those up by the id.
    byid = {e.get("entityIdentifier"): e for e in ses}
    for i, r in enumerate(S["Sheet3"], start=2):
        e = byid.get(r["glob_id"], {})
        got[("Sheet3", i, "glob_id")] = e.get("entityIdentifier")
        got[("Sheet3", i, "pref_label")] = e.get("label")
        if e.get("ccodes"):
            got[("Sheet3", i, "ccodes")] = e["ccodes"]
    total = diffs = 0
    for sh in ("Sheet3", "Sheet4", "Sheet5", "Sheet6"):
        for i, r in enumerate(S[sh], start=2):
            for k, v in r.items():
                if v is None:
                    continue
                total += 1
                b = got.get((sh, i, k))
                if k == "ccodes":
                    same = b == codes(v)
                elif isinstance(v, (int, float)):
                    same = b is not None and same_num(b, v)
                else:
                    same = isinstance(b, str) and b.strip() == v.strip()
                if not same:
                    diffs += 1
                    if diffs <= 10:
                        print(f"  DIFF {sh} row {i} {k}: {v!r} != {b!r}")
    for d in disagree[:10]:
        print("  NUMBER != TEXT", d)
    print(f"compared {total} non-empty cells in sheets 3-6: {diffs} differ; {len(disagree)} values carried twice or asserted by the conversion that disagree")
    return diffs + len(disagree)


if __name__ == "__main__":
    sys.exit({"forward": forward, "compare": compare}[sys.argv[1]](*sys.argv[2:]) and 1 or 0)
