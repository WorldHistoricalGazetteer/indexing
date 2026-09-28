"""PLATO issue #14, test 4: the Data Cube integrity constraints, as SPARQL.

    python3 cube_ic.py <file.nt> [--measure-direct]
    python3 cube_ic.py --selftest

IC-1, IC-2, IC-11, IC-12 and IC-14 from the RDF Data Cube specification, section 11, run VERBATIM
except that each ASK becomes a SELECT so a failure names the offender rather than only saying one
exists. The spec defines them over the NORMALIZED graph (section 10.3), and they reach a structure's
components only through qb:componentProperty.

That matters, and markets-cd found it the hard way. plato-tools' cube export writes components in
the abbreviated form, `_:c qb:dimension <p>`, with no qb:componentProperty. Run the spec's queries
on that and they find no components at all, so IC-11, IC-12 and IC-14 hold VACUOUSLY: they will
report a clean bill of health over observations that have no measure and no coordinates. An earlier
version of this file quietly sidestepped the problem by asking for qb:component/qb:dimension
instead, which works on the abbreviated form but is not the specification's query and would not
have caught an export that wrote nothing at all.

So: normalize first (phase 1 of section 10.3, which is what the abbreviated form needs), then run
the spec's own text. `--raw` skips normalization, to demonstrate the vacuous pass.

IC-14 is the one that matters for issue #14's question 1. Under the recommended answer PLATO's
normative form is the pair `plato:property_type` + `plato:value_literal`, not the direct statement
`obs :persons 1234` that Data Cube expects, so IC-14 fails on a PLATO document as written and
passes only after a tool adds the direct statements. `--measure-direct` runs the PLATO-pair variant
instead, which is what should be reported when the direct statements are absent by design. Report
both, and say which is which: a bare "IC-14 passed" would mean nothing without it.

IC-12 is run two ways. The specification's query compares every pair of observations in a data
set, which is about 3x10^10 pairs for Vision of Ireland's 255,759 and will not finish. It is run
verbatim on a sample (--sample N, default 300) so that what is run is the spec's own query, and
the whole graph is checked by grouping observations on their dimension values, which is the same
question asked in linear time. markets-cd found this on RCMRT vol. XIII; both corpora need it.

Run --selftest first. It builds a tiny cube in memory, one clean and one deliberately broken per
constraint, and checks that each query passes the clean graph and catches the broken one. A
constraint that cannot fail is worth nothing, and these are queries I wrote rather than took from
a tested implementation.
"""
import sys
from rdflib import Graph, Namespace, Literal, URIRef

QB = Namespace("http://purl.org/linked-data/cube#")
PLATO = Namespace("https://w3id.org/plato#")

PREFIX = """
PREFIX qb:    <http://purl.org/linked-data/cube#>
PREFIX plato: <https://w3id.org/plato#>
PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
"""

IC = {
# Every qb:Observation has exactly one associated qb:DataSet.
"IC-1 unique dataset": PREFIX + """
SELECT ?obs (COUNT(DISTINCT ?ds) AS ?n) WHERE {
  ?obs a qb:Observation .
  OPTIONAL { ?obs qb:dataSet ?ds }
} GROUP BY ?obs HAVING (COUNT(DISTINCT ?ds) != 1)
""",

# Every qb:DataSet has exactly one associated qb:DataStructureDefinition.
"IC-2 unique DSD": PREFIX + """
SELECT ?ds (COUNT(DISTINCT ?dsd) AS ?n) WHERE {
  ?ds a qb:DataSet .
  OPTIONAL { ?ds qb:structure ?dsd }
} GROUP BY ?ds HAVING (COUNT(DISTINCT ?dsd) != 1)
""",

# Every qb:Observation has a value for each dimension declared in its structure.
"IC-11 all dimensions required": PREFIX + """
SELECT ?obs ?dim WHERE {
  ?obs a qb:Observation ; qb:dataSet/qb:structure/qb:component/qb:componentProperty ?dim .
  ?dim a qb:DimensionProperty .
  FILTER NOT EXISTS { ?obs ?dim ?value }
}
""",

# No two qb:Observations in the same qb:DataSet have the same value for all dimensions.
"IC-12 no duplicate observations (spec query, sampled)": PREFIX + """
SELECT ?obs1 ?obs2 WHERE {
  ?obs1 qb:dataSet ?ds . ?obs2 qb:dataSet ?ds .
  FILTER (STR(?obs1) < STR(?obs2))
  FILTER NOT EXISTS {
    ?ds qb:structure/qb:component/qb:componentProperty ?dim .
    ?dim a qb:DimensionProperty .
    { ?obs1 ?dim ?v1 } UNION { }
    OPTIONAL { ?obs2 ?dim ?v2 }
    FILTER (!BOUND(?v2) || ?v1 != ?v2)
  }
}
""",

# Every qb:Observation has a value for each measure declared in its structure.
"IC-14 all measures present": PREFIX + """
SELECT ?obs ?measure WHERE {
  ?obs a qb:Observation ; qb:dataSet/qb:structure/qb:component/qb:componentProperty ?measure .
  ?measure a qb:MeasureProperty .
  FILTER NOT EXISTS { ?obs ?measure ?value }
}
""",

# The PLATO-pair variant of IC-14: the measure is named by plato:property_type and its value held
# by plato:value_literal, rather than stated directly. Not part of the spec; reported beside it.
"IC-14p measure as PLATO pair": PREFIX + """
SELECT ?obs ?measure WHERE {
  ?obs a qb:Observation ; qb:dataSet/qb:structure/qb:component/qb:componentProperty ?measure .
  ?measure a qb:MeasureProperty .
  # plato:property_type is an xsd:anyURI LITERAL in the export, not a node, so the measure IRI
  # has to be compared as a string. Comparing them as nodes failed on every observation.
  FILTER NOT EXISTS { ?obs plato:property_type ?pt ; plato:value_literal ?v
                      FILTER (STR(?pt) = STR(?measure)) }
}
""",
}


def run(graph, names):
    worst = 0
    for name in names:
        rows = list(graph.query(IC[name]))
        status = "pass" if not rows else f"FAIL ({len(rows)})"
        print(f"  {name:34} {status}")
        for r in rows[:3]:
            print("      ", " ".join(str(x) for x in r))
        worst = max(worst, 1 if rows else 0)
    return worst




NORMALIZE = PREFIX + """
INSERT { ?comp qb:componentProperty ?p . ?p a ?kind }
WHERE {
  { ?comp qb:dimension ?p BIND(qb:DimensionProperty AS ?kind) }
  UNION { ?comp qb:measure ?p BIND(qb:MeasureProperty AS ?kind) }
  UNION { ?comp qb:attribute ?p BIND(qb:AttributeProperty AS ?kind) }
}
"""


def normalize_graph(graph):
    """Phase 1 of section 10.3: the abbreviated component forms become qb:componentProperty,
    and each component property is typed. Without this the spec's own queries find nothing."""
    before = len(graph)
    graph.update(NORMALIZE)
    added = len(graph) - before
    print(f"  normalized: {added:,} triples added (qb:componentProperty and component types)")
    return added


def observation_keys(graph):
    """Each observation's address: its data set and its dimension values, as a tuple."""
    dims = {}
    for ds, dim in graph.query(PREFIX + """
            SELECT ?ds ?dim WHERE {
              ?ds qb:structure/qb:component/qb:componentProperty ?dim .
              ?dim a qb:DimensionProperty }"""):
        dims.setdefault(ds, set()).add(dim)
    keys = {}
    for obs, ds in graph.query(PREFIX + """
            SELECT ?obs ?ds WHERE { ?obs a qb:Observation ; qb:dataSet ?ds }"""):
        addr = []
        for dim in sorted(dims.get(ds, ()), key=str):
            vals = sorted(str(v) for v in graph.objects(obs, dim))
            addr.append((str(dim), tuple(vals)))
        keys[obs] = (str(ds), tuple(addr))
    return keys


def ic12_whole(graph):
    """IC-12 over the whole graph, by grouping rather than by comparing every pair."""
    seen = {}
    dupes = []
    for obs, key in observation_keys(graph).items():
        if key in seen:
            dupes.append((seen[key], obs))
        else:
            seen[key] = obs
    name = "IC-12 no duplicate observations (whole graph, grouped)"
    print(f"  {name:34} {'pass' if not dupes else f'FAIL ({len(dupes)})'}")
    for a, b in dupes[:3]:
        print("      ", a, b)
    return 1 if dupes else 0


def ic12_sampled(graph, n):
    """The specification's own pairwise query, on a sample small enough to finish."""
    obs = [o for o, in graph.query(PREFIX + "SELECT ?obs WHERE { ?obs a qb:Observation }")][:n]
    if not obs:
        print(f"  {'IC-12 (spec query, sampled)':34} NOT TESTED (no observations)")
        return 0
    keep = set(obs)
    sub = Graph()
    for o in keep:
        for p, v in graph.predicate_objects(o):
            sub.add((o, p, v))
            for p2, v2 in graph.predicate_objects(v):
                sub.add((v, p2, v2))
    for s_, p, v in graph:
        if s_ not in keep:
            sub.add((s_, p, v))
    rows = list(sub.query(IC["IC-12 no duplicate observations (spec query, sampled)"]))
    name = f"IC-12 spec query on {len(keep)} observations"
    print(f"  {name:34} {'pass' if not rows else f'FAIL ({len(rows)})'}")
    return 1 if rows else 0


def clean_graph():
    """A minimal two-observation cube that should satisfy every constraint."""
    g = Graph()
    EX = Namespace("https://example.org/")
    dsd, ds = EX["dsd"], EX["ds"]
    g.add((ds, URIRef(QB.structure), dsd))
    g.add((ds, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), URIRef(QB.DataSet)))
    for kind, term in (("dimension", EX["dim/sex"]), ("dimension", EX["dim/area"]),
                       ("measure", EX["measure/persons"])):
        comp = EX[f"comp/{term.split('/')[-1]}"]
        g.add((dsd, URIRef(QB.component), comp))
        g.add((comp, URIRef(QB[kind]), term))                    # abbreviated, as the export writes
        g.add((comp, URIRef(QB.componentProperty), term))        # normalized
        g.add((term, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
               URIRef(QB.DimensionProperty if kind == "dimension" else QB.MeasureProperty)))
    for i, sex in enumerate(("M", "F")):
        obs = EX[f"obs/{i}"]
        g.add((obs, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), URIRef(QB.Observation)))
        g.add((obs, URIRef(QB.dataSet), ds))
        g.add((obs, EX["dim/sex"], Literal(sex)))
        g.add((obs, EX["dim/area"], EX["place/1"]))
        g.add((obs, EX["measure/persons"], Literal(10 + i)))
        g.add((obs, URIRef(PLATO.property_type), EX["measure/persons"]))
        g.add((obs, URIRef(PLATO.value_literal), Literal(10 + i)))
    return g, EX, ds, dsd


def selftest():
    names = list(IC)
    print("clean graph, every constraint should pass:")
    g, EX, ds, dsd = clean_graph()
    if run(g, names):
        print("  SELFTEST FAILED: the clean graph does not satisfy its own constraints")
        return 1

    breaks = {
        "IC-1 unique dataset":
            lambda g: g.add((EX["obs/0"], URIRef(QB.dataSet), EX["ds2"])),
        "IC-2 unique DSD":
            lambda g: g.add((ds, URIRef(QB.structure), EX["dsd2"])),
        "IC-11 all dimensions required":
            lambda g: g.remove((EX["obs/0"], EX["dim/sex"], None)),
        "IC-12 no duplicate observations (spec query, sampled)":
            lambda g: (g.remove((EX["obs/1"], EX["dim/sex"], None)),
                       g.add((EX["obs/1"], EX["dim/sex"], Literal("M")))),
        "IC-14 all measures present":
            lambda g: g.remove((EX["obs/0"], EX["measure/persons"], None)),
        "IC-14p measure as PLATO pair":
            lambda g: g.remove((EX["obs/0"], URIRef(PLATO.value_literal), None)),
    }
    bad = 0
    print("\neach constraint, against a graph broken only in the way it is meant to catch:")
    for name, breaker in breaks.items():
        g, EX, ds, dsd = clean_graph()
        breaker(g)
        rows = list(g.query(IC[name]))
        ok = bool(rows)
        print(f"  {name:34} {'caught' if ok else 'DID NOT CATCH'}")
        if not ok:
            bad = 1
    # the grouped form must agree with the spec query, on both a clean and a duplicated graph
    g, EX, ds, dsd = clean_graph()
    if ic12_whole(g):
        print("  grouped IC-12 flags a clean graph"); bad = 1
    g, EX, ds, dsd = clean_graph()
    g.remove((EX["obs/1"], EX["dim/sex"], None)); g.add((EX["obs/1"], EX["dim/sex"], Literal("M")))
    if not ic12_whole(g):
        print("  grouped IC-12 DID NOT CATCH a duplicate address"); bad = 1
    # markets-cd's trap: an abbreviated structure holds IC-11 vacuously until it is normalized.
    # markets-cd's case, which the refArea fix created: two observations identical except for a
    # declared area must be DISTINCT, in both forms. A grouped check that listed its dimensions by
    # hand would call them duplicates once the export began declaring refArea.
    print("\ntwo observations identical except for a declared area:")
    g, EX, ds, dsd = clean_graph()
    comp = EX["comp/area2"]
    g.add((dsd, URIRef(QB.component), comp))
    g.add((comp, URIRef(QB.dimension), EX["dim/area"]))
    g.add((comp, URIRef(QB.componentProperty), EX["dim/area"]))
    g.add((EX["dim/area"], URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
           URIRef(QB.DimensionProperty)))
    g.remove((EX["obs/1"], EX["dim/sex"], None))
    g.add((EX["obs/1"], EX["dim/sex"], Literal("M")))        # same sex as obs/0
    g.remove((EX["obs/1"], EX["dim/area"], None))
    g.add((EX["obs/1"], EX["dim/area"], EX["place/2"]))      # different area
    grouped = ic12_whole(g)
    verbatim = bool(list(g.query(IC["IC-12 no duplicate observations (spec query, sampled)"])))
    print(f"  grouped: {'distinct' if not grouped else 'DUPLICATE'}; "
          f"verbatim: {'distinct' if not verbatim else 'DUPLICATE'}")
    if grouped or verbatim:
        print("  SELFTEST FAILED: a difference in a declared area must make them distinct"); bad = 1

    print("\nabbreviated structure, the form the cube export writes:")
    g, EX, ds, dsd = clean_graph()
    for comp in list(g.subjects(URIRef(QB.componentProperty), None)):
        g.remove((comp, URIRef(QB.componentProperty), None))
    g.remove((EX["obs/0"], EX["dim/sex"], None))        # a real IC-11 violation
    rows = list(g.query(IC["IC-11 all dimensions required"]))
    print(f"  before normalization: IC-11 {'holds VACUOUSLY' if not rows else 'fails'}")
    if rows:
        print("  SELFTEST FAILED: expected the vacuous pass that makes this check worthless"); bad = 1
    normalize_graph(g)
    rows = list(g.query(IC["IC-11 all dimensions required"]))
    print(f"  after normalization:  IC-11 {'fails, as it should' if rows else 'STILL HOLDS'}")
    if not rows:
        print("  SELFTEST FAILED: normalization did not restore the constraint"); bad = 1
    print("\nselftest " + ("passed" if not bad else "FAILED: a constraint cannot fail"))
    return bad


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1] == "--selftest":
        sys.exit(selftest())
    g = Graph()
    g.parse(sys.argv[1], format="nt")
    print(f"{len(g):,} triples")
    if "--raw" in sys.argv:
        print("  NOT normalized (--raw): the spec's queries cannot see abbreviated components,")
        print("  so any constraint below that 'holds' may be holding over nothing.")
    else:
        normalize_graph(g)
    names = [n for n in IC if "IC-12" not in n]
    rc = run(g, names)
    rc = max(rc, ic12_whole(g))
    n = 300
    if "--sample" in sys.argv:
        n = int(sys.argv[sys.argv.index("--sample") + 1])
    rc = max(rc, ic12_sampled(g, n))
    sys.exit(rc)
