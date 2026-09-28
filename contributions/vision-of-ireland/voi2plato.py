"""Vision of Ireland (Humphrey Southall's teaching database) <-> PLATO place-centric JSON.

    python3 voi2plato.py forward <vision-of-ireland checkout> out.json
    python3 voi2plato.py inverse <roundtripped .json|.jsonl> out-rebuilt.json
    python3 voi2plato.py compare <vision-of-ireland checkout> out-rebuilt.json

Source: the flattened views in `site/data/`, built by `build/01_export_views.sql` from the database
Southall's own scripts create. Nothing here re-models his data; the flattening is his repository's.

What the corpus is: 2,925 units on five levels (country, province, county, barony, parish), 3,455
names, 6,380 relations, and 255,760 statistical values for 1841 and 1851 in twelve nCubes.

The statistics are why this corpus is worth converting. Every value knows three things that PLATO
has no structured place for:

  the universe   in the nCube's label: "Families by Housing Class (Ireland)" counts FAMILIES,
                 "Persons aged 15+ by Sex and Irish 1841/51 Occupational Classification" counts
                 PERSONS AGED 15 AND OVER. Get the universe wrong and the figure means something
                 else, which is the distinction VoI's own documentation says is unrecoverable.
  the address    in the cell: "First Class", or "Female / Charity" where the cube has two
                 dimensions. It is a coordinate, not a property of the place.
  the denominator in a SIBLING nCube: "Total Population", "Total Families", "Total Houses" are
                 recorded for the same unit and year as the cubes they are the universe of.

This converter carries all three into the only slot PLATO offers, a PropertyValue, and the round
trip proves nothing is lost on the way. What it cannot do is say which of them is which. The
universe survives only inside a property URI this script invents, which is the finding.

`ul_auth`, the ultimate authority, is NOT here: `01_export_views.sql` selects `im_auth` only, so
the immediate/ultimate distinction cannot be evidenced from the exported views, only from
Southall's database.
"""
import json, glob, os, sys
from urllib.parse import quote, unquote

VOB = "https://www.visionofbritain.org.uk/unit/"
NS = "https://www.visionofbritain.org.uk/plato/voi/"
PLATO = "https://w3id.org/plato#"
# "Area (acres)" is the one cube whose measure has a unit PLATO can hold.
QUDT_ACRE = "http://qudt.org/vocab/unit/AC"
# VoI states every relation in one wording, "was a part of", 6,380 times. That is containment,
# so it maps to PLATO's own starter RelationType rather than to a type invented here.
CONTAINED_IN = PLATO + "ContainedIn"

# form_status is an open vocabulary ("starter concepts", and the JSON Schema takes any URI), so
# VoI's own statuses are minted as concepts rather than collapsed onto plato:Attested. Only
# Preferred has a PLATO concept; Alternate, Abbreviation and Official do not, and collapsing them
# would assert that an abbreviation and an official name are the same kind of form.
FORM_STATUS = {
    "P": PLATO + "Preferred",
    "A": NS + "formstatus/Alternate",
    "B": NS + "formstatus/Abbreviation",
    "O": NS + "formstatus/Official",
}


def load_units(root):
    units = {}
    for p in sorted(glob.glob(os.path.join(root, "site/data/units/*.json"))):
        units.update(json.load(open(p, encoding="utf-8")))
    return units


def authorities(root):
    return json.load(open(os.path.join(root, "site/data/authorities.json"), encoding="utf-8"))


def source_obj(code, auths):
    a = auths.get(code) or {}
    o = {"@id": NS + "authority/" + quote(code, safe=""), "authorityType": "source",
         "title": a.get("g_auth_title") or code}
    bits = [x for x in (a.get("g_auth_creator"), a.get("g_auth_title"), a.get("g_auth_publisher"),
                        a.get("g_auth_date")) if x]
    if bits:
        o["citation"] = ", ".join(bits)
    return o


def span(frm, to, label=None):
    s = {}
    if frm:
        s["startEarliest"] = f"{int(frm):04d}-01-01"
    if to:
        s["endLatest"] = f"{int(to):04d}-12-31"
    if label:
        s["label"] = str(label)
    return s or None


def prop_uri(table, cell):
    """The only place the universe survives. Reversible, and invented by this script."""
    return NS + "ncube/" + quote(table, safe="") + "/" + quote(cell, safe="")


def forward(root, out):
    units, auths = load_units(root), authorities(root)
    ses = []
    nname = nrel = nstat = 0
    for uid, u in units.items():
        e = {"@id": VOB + str(uid), "label": u.get("name") or str(uid), "attestations": []}
        b = f"{NS}att/{uid}/"

        if u.get("level"):
            e["attestations"].append({
                "@id": b + "level",
                "types": [{"label": u["level"], "sourceLabel": u["level"]}],
                **({"citations": [{"source": source_obj(u["source"], auths)}]} if u.get("source") else {}),
            })

        for i, n in enumerate(u.get("names") or []):
            a = {"@id": f"{b}name/{i}",
                 "names": [{"toponym": n["name"], "sourceLabel": n["name"],
                            **({"language": n["lang"]} if n.get("lang") else {})}]}
            if n.get("status") in FORM_STATUS:
                a["formStatus"] = FORM_STATUS[n["status"]]
            sp = span(n.get("from_year"), n.get("to_year"))
            if sp:
                a["timespans"] = [sp]
            if n.get("source"):
                a["citations"] = [{"source": source_obj(n["source"], auths)}]
            e["attestations"].append(a)
            nname += 1

        for i, r in enumerate(u.get("rels") or []):
            # One relation per attestation, as the ontology now requires, with the source's own
            # wording carried on the attestation rather than mapped away.
            a = {"@id": f"{b}rel/{i}",
                 "relations": [{"relatesTo": VOB + str(r["g_rel_to"]),
                                "relationType": CONTAINED_IN,
                                "relationLabel": r["rel_label"]}]}
            sp = span(r.get("from_year"), r.get("to_year"))
            if sp:
                a["timespans"] = [sp]
            if r.get("source"):
                a["citations"] = [{"source": source_obj(r["source"], auths)}]
            e["attestations"].append(a)
            nrel += 1

        for year, rowlist in sorted((u.get("stats") or {}).items()):
            for i, s in enumerate(rowlist):
                if s["value"] is None:
                    continue  # exactly one value in the corpus; PLATO drops null from RDF
                pv = {"property": prop_uri(s["table"], s["cell"]),
                      "label": s["cell"], "value": s["value"], "sourceLabel": s["cell"]}
                if s["table"] == "Area (acres)":
                    pv["unit"] = QUDT_ACRE
                a = {"@id": f"{b}stat/{year}/{i}",
                     "properties": [pv],
                     "timespans": [span(year, year, str(year))],
                     # The universe, in the source's own words. It has no structured home, so it
                     # is repeated here as free text purely so a reader can see it.
                     "notes": s["table"]}
                if s.get("src"):
                    a["citations"] = [{"source": source_obj(s["src"], auths)}]
                e["attestations"].append(a)
                nstat += 1

        ses.append(e)

    doc = {"$schema": "https://w3id.org/plato/schemas/place-centric.schema.json",
           "profile": "place-centric",
           "gazetteer": {"@id": NS,
                         "title": "Vision of Ireland (prototype conversion of the exported views)",
                         "licence": "https://creativecommons.org/licenses/by-sa/4.0/"},
           "spatialEntities": ses}
    with open(out, "w") as f:
        json.dump(doc, f, ensure_ascii=False)
    print(f"{len(ses)} spatial entities, {sum(len(e['attestations']) for e in ses)} attestations "
          f"({nname} names, {nrel} relations, {nstat} statistic values) -> {out}")


BY_URI = {v: k for k, v in FORM_STATUS.items()}


def load_ses(path):
    if path.endswith(".jsonl"):
        ses = []
        for line in open(path, encoding="utf-8"):
            o = json.loads(line)
            if "spatialEntities" in o:
                ses.extend(o["spatialEntities"])
            elif "attestations" in o or o.get("@id", "").startswith(VOB):
                ses.append(o)
        return ses
    return json.load(open(path, encoding="utf-8"))["spatialEntities"]


def inverse(rt, out):
    """Rebuild the source's own records, so a diff can fail."""
    rebuilt = {}
    for e in load_ses(rt):
        uid = e["@id"].rsplit("/", 1)[-1]
        rec = {"name": e.get("label"), "level": None, "names": [], "rels": [], "stats": {}}
        for a in e.get("attestations", []):
            aid = a["@id"]
            if aid.endswith("/level"):
                rec["level"] = a["types"][0]["label"]
            elif "/name/" in aid:
                n = a["names"][0]
                ts = (a.get("timespans") or [{}])[0]
                rec["names"].append({
                    "name": n["toponym"], "lang": n.get("lang") or n.get("language"),
                    "status": BY_URI.get(a.get("formStatus")),
                    "from_year": int(ts["startEarliest"][:4]) if ts.get("startEarliest") else None,
                    "to_year": int(ts["endLatest"][:4]) if ts.get("endLatest") else None,
                })
            elif "/rel/" in aid:
                ts = (a.get("timespans") or [{}])[0]
                rec["rels"].append({
                    "g_rel_to": int(a["relations"][0]["relatesTo"].rsplit("/", 1)[-1]),
                    "rel_label": a["relations"][0].get("relationLabel"),
                    "from_year": int(ts["startEarliest"][:4]) if ts.get("startEarliest") else None,
                    "to_year": int(ts["endLatest"][:4]) if ts.get("endLatest") else None,
                })
            elif "/stat/" in aid:
                year = aid.split("/stat/")[1].split("/")[0]
                p = a["properties"][0]
                tail = p["property"][len(NS) + len("ncube/"):]
                table, from_uri = (unquote(x) for x in tail.split("/", 1))
                # The cell label is read from the source's own words, not from the URI this
                # script minted, so that corrupting either is caught. They must agree.
                cell = p.get("sourceLabel")
                if cell != from_uri:
                    cell = f"<URI says {from_uri!r}, sourceLabel says {cell!r}>"
                rec["stats"].setdefault(year, []).append(
                    {"table": table, "cell": cell, "value": p["value"]})
        rebuilt[uid] = rec
    # Record which return file this was rebuilt from. A missing or empty one already fails
    # loudly; a STALE one from an earlier good run would not, so its size and time are carried
    # into the result and printed by compare.
    st = os.stat(rt)
    rebuilt["_from"] = {"path": os.path.abspath(rt), "bytes": st.st_size,
                        "mtime": __import__("datetime").datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}
    json.dump(rebuilt, open(out, "w"), ensure_ascii=False)
    print(f"  rebuilt from {rebuilt['_from']['path']} ({st.st_size:,} bytes, {rebuilt['_from']['mtime']})")
    print(f"rebuilt {len(rebuilt) - 1} units -> {out}")


def compare(root, rebuilt_path):
    units = load_units(root)
    got = json.load(open(rebuilt_path, encoding="utf-8"))
    diffs, checked = [], 0

    def eq(a, b):
        if isinstance(a, float) or isinstance(b, float):
            return a is not None and b is not None and abs(float(a) - float(b)) < 1e-9
        return a == b

    for uid, u in units.items():
        g = got.get(str(uid))
        if g is None:
            diffs.append(f"unit {uid} missing entirely")
            continue
        for field in ("name", "level"):
            if u.get(field):
                checked += 1
                if not eq(u.get(field), g.get(field)):
                    diffs.append(f"unit {uid} {field}: {u.get(field)!r} != {g.get(field)!r}")
        for i, n in enumerate(u.get("names") or []):
            h = g["names"][i] if i < len(g["names"]) else {}
            for k in ("name", "lang", "status", "from_year", "to_year"):
                if n.get(k) in (None, ""):
                    continue
                checked += 1
                if not eq(n.get(k), h.get(k)):
                    diffs.append(f"unit {uid} name[{i}].{k}: {n.get(k)!r} != {h.get(k)!r}")
        for i, r in enumerate(u.get("rels") or []):
            h = g["rels"][i] if i < len(g["rels"]) else {}
            for k in ("g_rel_to", "rel_label", "from_year", "to_year"):
                if r.get(k) in (None, ""):
                    continue
                checked += 1
                if not eq(r.get(k), h.get(k)):
                    diffs.append(f"unit {uid} rel[{i}].{k}: {r.get(k)!r} != {h.get(k)!r}")
        for year, rows in (u.get("stats") or {}).items():
            hrows = g["stats"].get(str(year), [])
            rows = [r for r in rows if r.get("value") is not None]
            for i, s in enumerate(rows):
                h = hrows[i] if i < len(hrows) else {}
                for k in ("table", "cell", "value"):
                    checked += 1
                    if not eq(s.get(k), h.get(k)):
                        diffs.append(f"unit {uid} stat {year}[{i}].{k}: {s.get(k)!r} != {h.get(k)!r}")

    print(f"{checked} non-empty values checked, {len(diffs)} differences")
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
        # Per-predicate controls on a subset: they establish that the comparison READS each
        # predicate, which does not depend on how many units carry it.
        got = json.load(open(sys.argv[3], encoding="utf-8"))
        got.pop("_from", None)
        keep = set(got)
        units = load_units(sys.argv[2])
        for k in [u for u in units if str(u) not in keep]:
            del units[k]
        import tempfile, os as _os
        d = tempfile.mkdtemp()
        _os.makedirs(_os.path.join(d, "site/data/units"), exist_ok=True)
        json.dump(units, open(_os.path.join(d, "site/data/units/all.json"), "w"), ensure_ascii=False)
        json.dump(authorities(sys.argv[2]),
                  open(_os.path.join(d, "site/data/authorities.json"), "w"), ensure_ascii=False)
        tmp = sys.argv[3] + ".sub"
        json.dump(got, open(tmp, "w"), ensure_ascii=False)
        sys.exit(compare(d, tmp))
    else:
        sys.exit("forward | inverse | compare")
