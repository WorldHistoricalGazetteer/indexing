"""Prototype: GLOBALISE Places v3.0 workbook (doi:10.34894/UFFFNO) <-> PLATO place-centric JSON.

    python3 glob2plato.py forward places_v3.xlsx out.json
    python3 glob2plato.py compare places_v3.xlsx roundtripped.json|.jsonl

Sheets 3-6 are the record; Sheet 2 is an overview derived from them (checked separately), Sheet 7
the bibliography. A citation cell such as "(Coolhaas 1979, 24, 183; van Goor 2004, 202)" becomes one
PLATO citation per source, with its own locator, when splitting and re-joining gives back the exact
string; otherwise it is kept whole as one source title. Attestation @ids carry sheet + row.
"""
import json, re, sys, uuid
import openpyxl

NS = "https://data.globalise.huygens.knaw.nl/hdl:20.500.14722/"
V = NS + "vocab/"
PLATO = "https://w3id.org/plato#"
CERT = {"certain": 1.0, "uncertain": 0.5}
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


def num(v):
    return repr(v) if isinstance(v, float) else str(v)


def forward(path, out):
    S = sheets(path)
    ents, order = {}, []

    def ent(g, label=None):
        if g not in ents:
            ents[g] = {"@id": uri(g), "label": label or g, "attestations": []}
            order.append(g)
        if label:
            ents[g]["label"] = label
        return ents[g]

    for i, r in enumerate(S["Sheet3"], start=2):
        g = r["glob_id"]; e = ent(g, r["pref_label"]); b = f"{NS}att/s3/{i}/"
        props = [{"property": "http://purl.org/dc/terms/identifier", "label": "glob_id", "value": g}]
        if r["ccodes"]:
            props.append({"property": "http://www.wikidata.org/prop/direct/P297", "label": "ccodes", "value": r["ccodes"]})
        e["attestations"].append({"@id": b + "record", "properties": props})
        if r["latitude"] is not None:
            q = {"certainty": CERT[r["coord_certainty"].strip()], "certaintyNote": r["coord_certainty"]} if r["coord_certainty"] else None
            if r["longitude"] is not None:
                geom = {"wkt": f"POINT ({num(r['longitude'])} {num(r['latitude'])})", "reprPoint": [r["longitude"], r["latitude"]]}
                if q: geom["qualification"] = q
                a = {"@id": b + "coord", "geometries": [geom], **cite(r["coord_source"])}
            else:  # a latitude alone is not a geometry
                pv = {"property": V + "latitude", "label": "latitude only", "value": num(r["latitude"]), "sourceLabel": num(r["latitude"])}
                if q: pv["qualification"] = q
                a = {"@id": b + "coord", "properties": [pv], **cite(r["coord_source"])}
            e["attestations"].append(a)
        if r["coord_remarks"] or r["coord_remarks_source"]:
            e["attestations"].append({"@id": b + "coord-remark", **({"notes": r["coord_remarks"]} if r["coord_remarks"] else {}),
                                      "meta": {"targetAttestation": b + "coord", "metaType": V + "meta/remark"}, **cite(r["coord_remarks_source"])})
        if r["overall_remarks"] or r["overall_remarks_source"]:
            e["attestations"].append({"@id": b + "overall", **({"notes": r["overall_remarks"]} if r["overall_remarks"] else {}), **cite(r["overall_remarks_source"])})
        ids = []
        for col in IDCOLS:
            v = r[col].strip() if isinstance(r[col], str) else r[col]  # 4 cells end in a newline or space
            if v:
                obj = v if str(v).startswith("http") else f"{V}ext/{col}/{v}"
                ids.append({"@id": f"{b}identity/{col}", "subject": uri(g), "object": obj, "identityType": "closeMatch"})
        if ids:
            e["identityRelations"] = ids

    for i, r in enumerate(S["Sheet4"], start=2):
        b = f"{NS}att/s4/{i}"
        a = {"@id": b, "names": [{"toponym": r["label"]}], "formStatus": PLATO + ("Headword" if r["label_type"] == "PREF" else "Attested"), **cite(r["label_source"])}
        ent(r["glob_id"])["attestations"].append(a)
        if r["label_remarks"] or r["label_remarks_source"]:
            ent(r["glob_id"])["attestations"].append({"@id": b + "/remark", **({"notes": r["label_remarks"]} if r["label_remarks"] else {}),
                                                      "meta": {"targetAttestation": b, "metaType": V + "meta/remark"}, **cite(r["label_remarks_source"])})

    for i, r in enumerate(S["Sheet5"], start=2):
        a = {"@id": f"{NS}att/s5/{i}", "types": [{"label": r["place_type"], "sourceLabel": r["place_type"]}], **cite(r["place_type_source"])}
        if r["place_type_comment"]:
            a["notes"] = r["place_type_comment"]
        ent(r["glob_id"])["attestations"].append(a)

    for i, r in enumerate(S["Sheet6"], start=2):
        ent(r["parent_region"], r["parent_region_pref_label"])  # NEW_ADMIN_* are defined nowhere else
        rt = V + "relation/" + r["relation"].lower().replace(" ", "-")
        a = {"@id": f"{NS}att/s6/{i}", "relations": [{"relatesTo": uri(r["parent_region"]), "relationType": rt}], **cite(r["source"])}
        if r["remarks"]:
            a["notes"] = r["remarks"]
        ent(r["glob_id"])["attestations"].append(a)

    ses = [ents[g] for g in order]
    for e in ses:
        if not e["attestations"]:
            del e["attestations"]
    json.dump({"$schema": "https://w3id.org/plato/schemas/place-centric.schema.json", "profile": "place-centric",
               "gazetteer": {"@id": NS, "title": "GLOBALISE Places v3.0 (prototype conversion)", "licence": "https://creativecommons.org/licenses/by/4.0/"},
               "spatialEntities": ses}, open(out, "w"), ensure_ascii=False)
    ncit = sum(len(a.get("citations", [])) for e in ses for a in e.get("attestations", []))
    print(f"{len(ses)} spatial entities, {sum(len(e.get('attestations', [])) for e in ses)} attestations, {ncit} citations -> {out}")


def compare(path, rt):
    S = sheets(path)
    txt = open(rt).read()
    try:
        ses = json.loads(txt)["spatialEntities"]
    except json.JSONDecodeError:
        ses = [json.loads(l) for l in txt.splitlines()[1:] if l.strip() and "subject" not in json.loads(l)]
    byuri = {e["@id"]: e for e in ses}
    glob = {e["@id"]: e["@id"][len(NS + "place:"):] for e in ses}
    got = {}
    for e in ses:
        for ir in e.get("identityRelations") or []:
            m = re.match(re.escape(NS) + r"att/s3/(\d+)/identity/(\w+)$", ir["@id"])
            o = ir["object"]; pre = f"{V}ext/{m.group(2)}/"
            got[("Sheet3", int(m.group(1)), m.group(2))] = o[len(pre):] if o.startswith(pre) else o
        for a in e.get("attestations") or []:
            m = re.match(re.escape(NS) + r"att/(s\d)/(\d+)/?(.*)$", a["@id"]); sh, row, part = "Sheet" + m.group(1)[1], int(m.group(2)), m.group(3)
            put = lambda k, v: got.__setitem__((sh, row, k), v)
            put("glob_id", glob[e["@id"]])
            if sh == "Sheet3":
                put("pref_label", e["label"])
                if part == "record":
                    for p in a["properties"]:
                        if p["label"] == "ccodes": put("ccodes", p["value"])
                elif part == "coord":
                    if a.get("geometries"):
                        g = a["geometries"][0]; lon, lat = re.match(r"POINT \((\S+) (\S+)\)", g["wkt"]).groups(); put("longitude", lon)
                    else:
                        g = a["properties"][0]; lat = g["sourceLabel"]
                    put("latitude", lat); put("coord_source", uncite(a))
                    put("coord_certainty", (g.get("qualification") or {}).get("certaintyNote"))
                elif part == "coord-remark":
                    put("coord_remarks", a.get("notes")); put("coord_remarks_source", uncite(a))
                elif part == "overall":
                    put("overall_remarks", a.get("notes")); put("overall_remarks_source", uncite(a))
            elif sh == "Sheet4":
                put("pref_label", e["label"])
                if part == "remark":
                    put("label_remarks", a.get("notes")); put("label_remarks_source", uncite(a))
                else:
                    put("label", a["names"][0]["toponym"]); put("label_source", uncite(a))
                    put("label_type", "PREF" if a["formStatus"].endswith("Headword") else "ALT")
            elif sh == "Sheet5":
                put("pref_label", e["label"]); put("place_type", a["types"][0]["sourceLabel"])
                put("place_type_comment", a.get("notes")); put("place_type_source", uncite(a))
            elif sh == "Sheet6":
                rel = a["relations"][0]; tgt = byuri[rel["relatesTo"]]
                put("pref_label", e["label"]); put("relation", RELS[rel["relationType"]])
                put("parent_region", glob[tgt["@id"]]); put("parent_region_pref_label", tgt["label"])
                put("remarks", a.get("notes")); put("source", uncite(a))
    total = diffs = 0
    for sh in ("Sheet3", "Sheet4", "Sheet5", "Sheet6"):
        for i, r in enumerate(S[sh], start=2):
            for k, v in r.items():
                if v is None:
                    continue
                total += 1
                b = got.get((sh, i, k))
                if isinstance(v, str) and isinstance(b, str):
                    v, b = v.strip(), b.strip()
                same = (float(b) == v) if isinstance(v, (int, float)) and b is not None else (b == v)
                if not same:
                    diffs += 1
                    if diffs <= 10:
                        print(f"  DIFF {sh} row {i} {k}: {v!r} != {b!r}")
    print(f"compared {total} non-empty cells in sheets 3-6: {diffs} differ")
    return diffs


if __name__ == "__main__":
    sys.exit({"forward": forward, "compare": compare}[sys.argv[1]](*sys.argv[2:]) and 1 or 0)
